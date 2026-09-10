#!/usr/bin/env python3
"""
td_host_server.py — a zero-dependency HTTP bridge that exposes TouchDesigner's
host-only tools (toeexpand / toecollapse, and — experimentally — headless render)
to a remote client (e.g. the toxc pipeline running in a container).

Runs on the Mac where TouchDesigner is installed. Pure Python stdlib, no pip deps.

    python3 td_host_server.py --port 8770
    # then, from the client:
    curl -s http://<mac-ip>:8770/health | python3 -m json.tool
    curl -s --data-binary @project.tox 'http://<mac-ip>:8770/expand?name=project.tox&format=json'

Endpoints
---------
GET  /health
    Report platform, discovered toeexpand / TouchDesigner paths, and versions.

POST /expand?name=<file.tox>[&format=tar|json]
    Body = raw .tox/.toe bytes. Runs toeexpand in an isolated temp dir and returns
    everything it produced.
      format=tar  (default): application/gzip tarball of the expanded tree.
      format=json          : JSON { files: {relpath: {kind:text|base64, data}}, stdout, stderr, rc }

POST /collapse?name=<out.tox>
    Body = gzip tarball of a previously-expanded tree. Runs toecollapse, returns the
    resulting .toe/.tox bytes. (Round-trip / sanity aid.)

POST /render   (EXPERIMENTAL — TD-as-oracle for conformance)
    JSON { tox: <base64>, op: "/path/to/top", width?, height?, params?: {..} }
    Drives TouchDesigner headless to cook <op> and save a PNG. Returns image/png.
    Returns 501 with guidance if a headless render path isn't wired on this host.

POST /exec     (requires --allow-exec) — run bazel/git on the HOST, async
    JSON { tool: "bazel"|"git", args: [...] }  e.g.
      {"tool":"bazel","args":["run","//deploy:tdplayer_pi3.deploy_live","--","tdplayer.local"]}
      {"tool":"git","args":["pull"]}
      {"action":"kill","id":"bazel-3"}
    Returns { id, cmd, cwd }. Runs in --workspace; only the allowlisted tool +
    subcommand is permitted. Lets a container drive host-only builds/deploys.
GET  /exec?id=<id>[&from=<n>]
    Poll a job: { running, rc, elapsed, lines: [...from offset n], next, nlines }.
    No id -> list jobs. Tail with `from` = the previous response's `next`.

Config (env or flags; flags win)
    TOEEXPAND / --toeexpand   path to the toeexpand binary
    TOECOLLAPSE / --toecollapse
    TD_APP / --td-app         path to the TouchDesigner executable
    TOXC_HOST_TOKEN / --token optional shared secret; if set, clients must send
                              header  X-Auth-Token: <token>
    TOXC_ALLOW_EXEC / --allow-exec   enable POST /exec (off by default; RCE surface)
    TOXC_WORKSPACE / --workspace     repo checkout /exec runs bazel/git in
    TOXC_BAZEL / --bazel             path to bazel (default: PATH lookup)
"""

import argparse
import base64
import glob
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ----------------------------------------------------------------------------- discovery

def _candidate_td_dirs():
    """macOS + Windows + Linux install locations for TouchDesigner."""
    pats = [
        "/Applications/TouchDesigner*.app/Contents/MacOS",           # macOS
        "/Applications/Derivative/TouchDesigner*.app/Contents/MacOS",
        "C:/Program Files/Derivative/TouchDesigner*/bin",            # Windows
        os.path.expanduser("~/TouchDesigner*/bin"),                  # Linux-ish
    ]
    out = []
    for p in pats:
        out.extend(sorted(glob.glob(p)))
    return out


def _which_in(dirs, names):
    for d in dirs:
        for n in names:
            for cand in (os.path.join(d, n), os.path.join(d, n + ".exe")):
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    return cand
    return None


def discover(cfg):
    dirs = _candidate_td_dirs()
    if cfg.get("toeexpand") is None:
        cfg["toeexpand"] = os.environ.get("TOEEXPAND") or _which_in(dirs, ["toeexpand"])
    if cfg.get("toecollapse") is None:
        cfg["toecollapse"] = os.environ.get("TOECOLLAPSE") or _which_in(dirs, ["toecollapse"])
    if cfg.get("td_app") is None:
        cfg["td_app"] = os.environ.get("TD_APP") or _which_in(
            dirs, ["TouchDesigner", "TouchDesigner099", "touchd"])
    return cfg


# ----------------------------------------------------------------------------- helpers

def _run(cmd, cwd=None, timeout=120):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _tar_dir_bytes(root):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in sorted(os.listdir(root)):
            tf.add(os.path.join(root, name), arcname=name)
    return buf.getvalue()


def _dir_to_json(root):
    files = {}
    for dirpath, _dirs, names in os.walk(root):
        for n in sorted(names):
            full = os.path.join(dirpath, n)
            rel = os.path.relpath(full, root)
            with open(full, "rb") as fh:
                raw = fh.read()
            try:
                files[rel] = {"kind": "text", "data": raw.decode("utf-8")}
            except UnicodeDecodeError:
                files[rel] = {"kind": "base64", "data": base64.b64encode(raw).decode("ascii")}
    return files


# ----------------------------------------------------------------------------- exec jobs
# Run bazel/git ON THE HOST (the Mac) so the container can drive deploys + builds
# it can't run itself (the aarch64 image build needs the Mac's nix builder, and
# the source tree lives here). Long-running, so it's a start/poll job model rather
# than one blocking request. Gated behind --allow-exec (it's an RCE surface); only
# an allowlisted tool + subcommand runs, always in the configured workspace dir.

_JOBS = {}
_JOBS_LOCK = threading.Lock()
_JOB_SEQ = [0]

# tool -> allowed first argument (subcommand). Args are passed as a list (no shell).
_TOOL_SUBCMDS = {
    "bazel": {"run", "build", "test", "query", "cquery", "aquery", "clean",
              "info", "version", "mod", "fetch", "shutdown"},
    "git": {"pull", "fetch", "status", "log", "rev-parse", "diff", "show",
            "checkout", "switch", "branch", "stash", "remote", "reset"},
}


def _start_job(tool, tool_bin, args, cwd):
    with _JOBS_LOCK:
        _JOB_SEQ[0] += 1
        jid = "%s-%d" % (tool, _JOB_SEQ[0])
    cmd = [tool_bin] + list(args)
    job = {"id": jid, "tool": tool, "cmd": cmd, "cwd": cwd, "lines": [],
           "rc": None, "done": False, "started": time.time(), "ended": None,
           "proc": None}
    with _JOBS_LOCK:
        _JOBS[jid] = job

    def _run_job():
        try:
            p = subprocess.Popen(
                cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
        except Exception as e:  # noqa: BLE001 - report launch failure to the client
            with _JOBS_LOCK:
                job["lines"].append("[exec] failed to start: %r" % (e,))
                job["rc"] = 127
                job["done"] = True
                job["ended"] = time.time()
            return
        with _JOBS_LOCK:
            job["proc"] = p
        for line in p.stdout:                      # streams until the process exits
            with _JOBS_LOCK:
                job["lines"].append(line.rstrip("\n"))
        p.wait()
        with _JOBS_LOCK:
            job["rc"] = p.returncode
            job["done"] = True
            job["ended"] = time.time()

    threading.Thread(target=_run_job, daemon=True).start()
    return job


# ----------------------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "toxc-host/0.1"
    cfg = {}

    # -- plumbing --------------------------------------------------------------
    def _auth_ok(self):
        tok = self.cfg.get("token")
        if not tok:
            return True
        return self.headers.get("X-Auth-Token") == tok

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- routing ---------------------------------------------------------------
    def do_GET(self):
        if not self._auth_ok():
            return self._send_json({"error": "unauthorized"}, 401)
        u = urlparse(self.path)
        if u.path == "/health":
            return self.health()
        if u.path == "/readfile":
            return self.readfile(parse_qs(u.query))
        if u.path == "/exec":
            return self.exec_poll(parse_qs(u.query))
        return self._send_json({"error": "not found", "path": u.path}, 404)

    def do_POST(self):
        if not self._auth_ok():
            return self._send_json({"error": "unauthorized"}, 401)
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/expand":
                return self.expand(q)
            if u.path == "/expand_local":
                return self.expand_local(q)
            if u.path == "/collapse":
                return self.collapse(q)
            if u.path == "/render":
                return self.render()
            if u.path == "/exec":
                return self.exec_start()
        except subprocess.TimeoutExpired:
            return self._send_json({"error": "timeout"}, 504)
        except Exception as e:  # noqa: BLE001 - surface failures to the client
            return self._send_json({"error": repr(e)}, 500)
        return self._send_json({"error": "not found", "path": u.path}, 404)

    # -- endpoints -------------------------------------------------------------
    def health(self):
        c = self.cfg
        info = {
            "ok": True,
            "server": self.server_version,
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "toeexpand": c.get("toeexpand"),
            "toeexpand_found": bool(c.get("toeexpand")),
            "toecollapse": c.get("toecollapse"),
            "td_app": c.get("td_app"),
            "td_found": bool(c.get("td_app")),
            "candidates": _candidate_td_dirs(),
            "exec_allowed": bool(c.get("allow_exec")),
            "workspace": c.get("workspace"),
            "bazel": (c.get("tools", {}).get("bazel") or shutil.which("bazel")),
        }
        return self._send_json(info)

    def expand(self, q):
        exe = self.cfg.get("toeexpand")
        if not exe:
            return self._send_json(
                {"error": "toeexpand not found; set TOEEXPAND or --toeexpand"}, 501)
        name = (q.get("name", ["project.tox"])[0]) or "project.tox"
        name = os.path.basename(name)
        fmt = q.get("format", ["tar"])[0]
        data = self._read_body()
        if not data:
            return self._send_json({"error": "empty body; POST raw .tox/.toe bytes"}, 400)
        return self._expand_bytes(name, data, fmt)

    def expand_local(self, q):
        """Expand a .toe/.tox already present on the host filesystem (by path).
        This is what the toxc pipeline calls: the project lives on the Mac with TD."""
        exe = self.cfg.get("toeexpand")
        if not exe:
            return self._send_json(
                {"error": "toeexpand not found; set TOEEXPAND or --toeexpand"}, 501)
        raw_path = (q.get("path", [""])[0]) or ""
        path = os.path.expanduser(raw_path)
        if not raw_path:
            return self._send_json({"error": "need ?path=/abs/or/~/file.toe"}, 400)
        if not os.path.isfile(path):
            return self._send_json({"error": f"no such file: {path}"}, 404)
        fmt = q.get("format", ["tar"])[0]
        with open(path, "rb") as fh:
            data = fh.read()
        return self._expand_bytes(os.path.basename(path), data, fmt)

    def _expand_bytes(self, name, data, fmt):
        exe = self.cfg.get("toeexpand")
        work = tempfile.mkdtemp(prefix="toxc_expand_")
        try:
            src = os.path.join(work, name)
            with open(src, "wb") as fh:
                fh.write(data)
            before = set(os.listdir(work))
            rc, out, err = _run([exe, name], cwd=work)
            # Collect everything toeexpand produced (don't assume its output naming).
            produced = os.path.join(work, "_expanded")
            os.makedirs(produced, exist_ok=True)
            for entry in os.listdir(work):
                if entry == name or entry == "_expanded" or entry in before:
                    continue
                shutil.move(os.path.join(work, entry), os.path.join(produced, entry))

            if fmt == "json":
                return self._send_json({
                    "rc": rc,
                    "stdout": out.decode("utf-8", "replace"),
                    "stderr": err.decode("utf-8", "replace"),
                    "files": _dir_to_json(produced),
                })
            tar = _tar_dir_bytes(produced)
            return self._send_bytes(
                tar, "application/gzip",
                extra={"X-Toeexpand-RC": str(rc),
                       "Content-Disposition": f'attachment; filename="{name}.expanded.tgz"'})
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def collapse(self, q):
        exe = self.cfg.get("toecollapse")
        if not exe:
            return self._send_json(
                {"error": "toecollapse not found; set TOECOLLAPSE or --toecollapse"}, 501)
        name = os.path.basename((q.get("name", ["out.tox"])[0]) or "out.tox")
        data = self._read_body()
        work = tempfile.mkdtemp(prefix="toxc_collapse_")
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                tf.extractall(work)  # trusted: our own expand output
            rc, out, err = _run([exe, name], cwd=work)
            result = os.path.join(work, name)
            if rc != 0 or not os.path.isfile(result):
                return self._send_json({
                    "error": "toecollapse failed", "rc": rc,
                    "stdout": out.decode("utf-8", "replace"),
                    "stderr": err.decode("utf-8", "replace")}, 500)
            with open(result, "rb") as fh:
                return self._send_bytes(fh.read(), "application/octet-stream",
                                        extra={"Content-Disposition": f'attachment; filename="{name}"'})
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def readfile(self, q):
        """Serve a host-side asset by path (e.g. a moviefilein/sprite-sheet the
        toxc runtime needs). Read-only fetch of files already on the Mac."""
        raw_path = (q.get("path", [""])[0]) or ""
        path = os.path.expanduser(raw_path)
        if not raw_path:
            return self._send_json({"error": "need ?path=..."}, 400)
        if not os.path.isfile(path):
            return self._send_json({"error": f"no such file: {path}"}, 404)
        with open(path, "rb") as fh:
            data = fh.read()
        return self._send_bytes(
            data, "application/octet-stream",
            extra={"Content-Disposition": f'attachment; filename="{os.path.basename(path)}"'})

    def render(self):
        """EXPERIMENTAL headless TD render for conformance. See _HEADLESS_RENDER_NOTES."""
        td = self.cfg.get("td_app")
        if not td:
            return self._send_json({"error": "TouchDesigner not found; set TD_APP"}, 501)
        req = json.loads(self._read_body() or b"{}")
        if "tox" not in req or "op" not in req:
            return self._send_json({"error": "need JSON {tox: base64, op: '/path/to/top'}"}, 400)
        # Headless cook-and-save is version/OS sensitive; wire per host and flip this on.
        return self._send_json({
            "error": "headless render not wired on this host",
            "how": _HEADLESS_RENDER_NOTES,
        }, 501)

    # -- exec (bazel/git on the host) -----------------------------------------
    def exec_start(self):
        """POST /exec  {tool, args[, action]} — run bazel/git on the host, async.

        Body JSON:
          {"tool": "bazel", "args": ["run", "//deploy:tdplayer_pi3.deploy_live",
                                     "--", "tdplayer.local"]}
          {"tool": "git",   "args": ["pull"]}
          {"action": "kill", "id": "bazel-3"}
        Returns {id, cmd, cwd}; poll GET /exec?id=<id>. Runs in the configured
        workspace; only the allowlisted tool + subcommand is permitted.
        """
        if not self.cfg.get("allow_exec"):
            return self._send_json(
                {"error": "exec disabled; start the bridge with --allow-exec"}, 403)
        req = json.loads(self._read_body() or b"{}")
        if req.get("action") == "kill":
            return self._exec_kill(req.get("id"))
        tool = req.get("tool")
        args = req.get("args") or []
        if tool not in _TOOL_SUBCMDS:
            return self._send_json(
                {"error": "tool must be one of %s" % sorted(_TOOL_SUBCMDS)}, 400)
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            return self._send_json({"error": "args must be a list of strings"}, 400)
        if not args or args[0] not in _TOOL_SUBCMDS[tool]:
            return self._send_json(
                {"error": "%s subcommand must be one of %s"
                          % (tool, sorted(_TOOL_SUBCMDS[tool]))}, 400)
        tool_bin = self.cfg.get("tools", {}).get(tool) or shutil.which(tool)
        if not tool_bin:
            return self._send_json({"error": "%s not found on PATH" % tool}, 501)
        cwd = self.cfg.get("workspace") or os.getcwd()
        if not os.path.isdir(cwd):
            return self._send_json({"error": "workspace not a dir: %s" % cwd}, 500)
        job = _start_job(tool, tool_bin, args, cwd)
        return self._send_json({"id": job["id"], "cmd": job["cmd"], "cwd": cwd})

    def exec_poll(self, q):
        """GET /exec?id=<id>[&from=<n>] — poll a job (returns new output lines from
        offset `from`). GET /exec with no id lists jobs."""
        if not self.cfg.get("allow_exec"):
            return self._send_json(
                {"error": "exec disabled; start the bridge with --allow-exec"}, 403)
        jid = (q.get("id", [None])[0])
        if not jid:
            with _JOBS_LOCK:
                jobs = [{"id": j["id"], "tool": j["tool"], "running": not j["done"],
                         "rc": j["rc"], "nlines": len(j["lines"]),
                         "started": j["started"]} for j in _JOBS.values()]
            return self._send_json({"jobs": jobs})
        frm = int((q.get("from", ["0"])[0]) or 0)
        with _JOBS_LOCK:
            job = _JOBS.get(jid)
            if not job:
                return self._send_json({"error": "no such job: %s" % jid}, 404)
            lines = job["lines"][frm:]
            end = job["ended"] or time.time()
            resp = {
                "id": jid, "cmd": job["cmd"], "running": not job["done"],
                "rc": job["rc"], "from": frm, "next": frm + len(lines),
                "nlines": len(job["lines"]),
                "elapsed": round(end - job["started"], 1), "lines": lines,
            }
        return self._send_json(resp)

    def _exec_kill(self, jid):
        with _JOBS_LOCK:
            job = _JOBS.get(jid)
            proc = job["proc"] if job else None
        if not job:
            return self._send_json({"error": "no such job: %s" % jid}, 404)
        if proc and job["rc"] is None:
            proc.terminate()
        return self._send_json({"id": jid, "killed": True})


_HEADLESS_RENDER_NOTES = (
    "Drive TD via a generated startup script that op(<op>).save('out.png') then project.quit(). "
    "Launch: TouchDesigner <project.toe> and have the project's Execute DAT run the save on start, "
    "or use TouchEngine. macOS headless needs a window server session; run under the user's GUI "
    "session or a virtual display. Fill in Handler.render once the host recipe is confirmed."
)


def main():
    ap = argparse.ArgumentParser(description="TouchDesigner host bridge for toxc")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--toeexpand", default=None)
    ap.add_argument("--toecollapse", default=None)
    ap.add_argument("--td-app", dest="td_app", default=None)
    ap.add_argument("--token", default=os.environ.get("TOXC_HOST_TOKEN"))
    # Host exec (bazel/git in the workspace) — off unless --allow-exec, since it
    # can run repo targets (e.g. deploy scripts) on this machine.
    ap.add_argument("--allow-exec", action="store_true",
                    default=bool(os.environ.get("TOXC_ALLOW_EXEC")),
                    help="enable POST /exec to run bazel/git in the workspace")
    ap.add_argument("--workspace",
                    default=os.environ.get("TOXC_WORKSPACE")
                    or os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="repo checkout /exec runs bazel/git in (default: this file's repo)")
    ap.add_argument("--bazel", default=os.environ.get("TOXC_BAZEL"),
                    help="path to bazel (default: PATH lookup)")
    args = ap.parse_args()

    cfg = discover({
        "toeexpand": args.toeexpand,
        "toecollapse": args.toecollapse,
        "td_app": args.td_app,
        "token": args.token,
        "allow_exec": args.allow_exec,
        "workspace": os.path.abspath(os.path.expanduser(args.workspace)),
        "tools": {"bazel": args.bazel} if args.bazel else {},
    })
    Handler.cfg = cfg

    print(f"[toxc-host] {platform.platform()}  python {sys.version.split()[0]}")
    print(f"[toxc-host] toeexpand   : {cfg.get('toeexpand')  or 'NOT FOUND'}")
    print(f"[toxc-host] toecollapse : {cfg.get('toecollapse') or 'NOT FOUND'}")
    print(f"[toxc-host] TouchDesigner: {cfg.get('td_app')     or 'NOT FOUND'}")
    if cfg.get("allow_exec"):
        print(f"[toxc-host] EXEC ENABLED (bazel/git) in {cfg.get('workspace')}")
        print(f"[toxc-host]   bazel: {cfg.get('tools', {}).get('bazel') or shutil.which('bazel') or 'NOT FOUND'}")
        if not cfg.get("token"):
            print("[toxc-host]   WARNING: /exec is open (no --token set) — anyone on"
                  " the LAN can run bazel/git here. Set --token for safety.")
    else:
        print("[toxc-host] exec disabled (pass --allow-exec to enable POST /exec)")
    if cfg.get("token"):
        print("[toxc-host] auth token REQUIRED (X-Auth-Token)")
    print(f"[toxc-host] listening on http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
