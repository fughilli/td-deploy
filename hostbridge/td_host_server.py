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

POST /deploy   (requires --token) — run the ONE Pi live-deploy on the HOST, async
    JSON { host?: "tdplayer.local", keep_builder?: false, builder_disk?: "/abs/path" }
      -> runs exactly: bazel run //deploy:tdplayer_pi3.deploy_live -- [--keep-builder] <host>
      builder_disk sets $SBC_BUILDER_DISK (the macOS aarch64 builder VM's disk).
    Also: {"action":"kill","id":"deploy-1"}. Returns { id, cmd, cwd }. This runs a
    single fixed command in --workspace (NOT arbitrary exec), so a container can
    drive the host-only deploy (the aarch64 image build needs the Mac's builder).
GET  /deploy?id=<id>[&from=<n>]
    Poll a job: { running, rc, elapsed, lines: [...from offset n], next, nlines }.
    No id -> list jobs. Tail with `from` = the previous response's `next`.

Config (env or flags; flags win)
    TOEEXPAND / --toeexpand   path to the toeexpand binary
    TOECOLLAPSE / --toecollapse
    TD_APP / --td-app         path to the TouchDesigner executable
    TOXC_HOST_TOKEN / --token shared secret; clients send header X-Auth-Token.
                              REQUIRED to enable POST /deploy.
    TOXC_WORKSPACE / --workspace     repo checkout /deploy runs bazel in
    TOXC_BAZEL / --bazel             path to bazel (default: PATH lookup)
"""

import argparse
import base64
import glob
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ----------------------------------------------------------------------------- discovery


def _candidate_td_dirs():
    """macOS + Windows + Linux install locations for TouchDesigner."""
    pats = [
        "/Applications/TouchDesigner*.app/Contents/MacOS",  # macOS
        "/Applications/Derivative/TouchDesigner*.app/Contents/MacOS",
        "C:/Program Files/Derivative/TouchDesigner*/bin",  # Windows
        os.path.expanduser("~/TouchDesigner*/bin"),  # Linux-ish
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
            dirs, ["TouchDesigner", "TouchDesigner099", "touchd"]
        )
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


# ----------------------------------------------------------------------------- deploy jobs
# Run the ONE Pi live-deploy on the HOST (the Mac) so the container can drive it:
# the aarch64 image build needs the Mac's nix builder and the source tree lives
# here. It's long-running, so it's a start/poll job model rather than one blocking
# request. This deliberately runs a single fixed bazel command (not arbitrary
# exec) and requires the auth token — see Handler.deploy_start.

_JOBS = {}
_JOBS_LOCK = threading.Lock()
_JOB_SEQ = [0]

# The only command this endpoint will run (plus the target host as the last arg).
_DEPLOY_TARGET = "//deploy:tdplayer_pi3.deploy_live"
_DEFAULT_HOST = "tdplayer.local"
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")  # hostname / IPv4 — no shell metachars


def _start_job(label, cmd, cwd, env=None):
    with _JOBS_LOCK:
        _JOB_SEQ[0] += 1
        jid = "%s-%d" % (label, _JOB_SEQ[0])
    job = {
        "id": jid,
        "label": label,
        "cmd": cmd,
        "cwd": cwd,
        "lines": [],
        "rc": None,
        "done": False,
        "started": time.time(),
        "ended": None,
        "proc": None,
    }
    with _JOBS_LOCK:
        _JOBS[jid] = job

    def _run_job():
        try:
            p = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as e:  # noqa: BLE001 - report launch failure to the client
            with _JOBS_LOCK:
                job["lines"].append("[exec] failed to start: %r" % (e,))
                job["rc"] = 127
                job["done"] = True
                job["ended"] = time.time()
            return
        with _JOBS_LOCK:
            job["proc"] = p
        for line in p.stdout:  # streams until the process exits
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
        if u.path == "/deploy":
            return self.deploy_poll(parse_qs(u.query))
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
            if u.path == "/deploy":
                return self.deploy_start()
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
            "deploy_enabled": bool(c.get("token")),  # /deploy requires a token
            "deploy_target": _DEPLOY_TARGET,
            "workspace": c.get("workspace"),
            "bazel": (c.get("bazel") or shutil.which("bazel")),
        }
        return self._send_json(info)

    def expand(self, q):
        exe = self.cfg.get("toeexpand")
        if not exe:
            return self._send_json(
                {"error": "toeexpand not found; set TOEEXPAND or --toeexpand"}, 501
            )
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
                {"error": "toeexpand not found; set TOEEXPAND or --toeexpand"}, 501
            )
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
                return self._send_json(
                    {
                        "rc": rc,
                        "stdout": out.decode("utf-8", "replace"),
                        "stderr": err.decode("utf-8", "replace"),
                        "files": _dir_to_json(produced),
                    }
                )
            tar = _tar_dir_bytes(produced)
            return self._send_bytes(
                tar,
                "application/gzip",
                extra={
                    "X-Toeexpand-RC": str(rc),
                    "Content-Disposition": f'attachment; filename="{name}.expanded.tgz"',
                },
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def collapse(self, q):
        exe = self.cfg.get("toecollapse")
        if not exe:
            return self._send_json(
                {"error": "toecollapse not found; set TOECOLLAPSE or --toecollapse"}, 501
            )
        name = os.path.basename((q.get("name", ["out.tox"])[0]) or "out.tox")
        data = self._read_body()
        work = tempfile.mkdtemp(prefix="toxc_collapse_")
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                tf.extractall(work)  # trusted: our own expand output
            rc, out, err = _run([exe, name], cwd=work)
            result = os.path.join(work, name)
            if rc != 0 or not os.path.isfile(result):
                return self._send_json(
                    {
                        "error": "toecollapse failed",
                        "rc": rc,
                        "stdout": out.decode("utf-8", "replace"),
                        "stderr": err.decode("utf-8", "replace"),
                    },
                    500,
                )
            with open(result, "rb") as fh:
                return self._send_bytes(
                    fh.read(),
                    "application/octet-stream",
                    extra={"Content-Disposition": f'attachment; filename="{name}"'},
                )
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
            data,
            "application/octet-stream",
            extra={"Content-Disposition": f'attachment; filename="{os.path.basename(path)}"'},
        )

    def render(self):
        """EXPERIMENTAL headless TD render for conformance. See _HEADLESS_RENDER_NOTES."""
        td = self.cfg.get("td_app")
        if not td:
            return self._send_json({"error": "TouchDesigner not found; set TD_APP"}, 501)
        req = json.loads(self._read_body() or b"{}")
        if "tox" not in req or "op" not in req:
            return self._send_json({"error": "need JSON {tox: base64, op: '/path/to/top'}"}, 400)
        # Headless cook-and-save is version/OS sensitive; wire per host and flip this on.
        return self._send_json(
            {
                "error": "headless render not wired on this host",
                "how": _HEADLESS_RENDER_NOTES,
            },
            501,
        )

    # -- deploy (the one Pi live-deploy, on the host) -------------------------
    def _deploy_gate(self):
        """Deploy runs a build/switch on the host, so it always requires a token.
        Returns an error dict+code to send, or None if allowed."""
        if not self.cfg.get("token"):
            return {
                "error": "deploy requires an auth token; start the bridge with"
                " --token <secret> (and send X-Auth-Token)"
            }, 403
        return None

    def deploy_start(self):
        """POST /deploy {host?, keep_builder?, builder_disk?} — Pi live-deploy on host.

        Runs exactly `bazel run //deploy:tdplayer_pi3.deploy_live -- [--keep-builder]
        <host>` in the configured --workspace (nothing else). `builder_disk` sets
        $SBC_BUILDER_DISK for the build (the macOS aarch64 builder VM's disk image).
        Async: returns {id}; poll GET /deploy?id=<id>. `{"action":"kill"}` stops it.
        """
        gate = self._deploy_gate()
        if gate:
            return self._send_json(*gate)
        req = json.loads(self._read_body() or b"{}")
        if req.get("action") == "kill":
            return self._deploy_kill(req.get("id"))
        host = req.get("host") or _DEFAULT_HOST
        if not isinstance(host, str) or not _HOST_RE.match(host):
            return self._send_json({"error": "invalid host %r" % (host,)}, 400)
        bazel = self.cfg.get("bazel") or shutil.which("bazel")
        if not bazel:
            return self._send_json({"error": "bazel not found on PATH"}, 501)
        cwd = self.cfg.get("workspace") or os.getcwd()
        if not os.path.isdir(os.path.join(cwd, "deploy")):
            return self._send_json({"error": "workspace has no deploy/ dir: %s" % cwd}, 500)
        # Optional builder disk override -> env (safe: goes in the env dict, not the
        # shell). Inherits the bridge's env so an exported SBC_BUILDER_DISK works too.
        env = None
        builder_disk = req.get("builder_disk")
        if builder_disk is not None:
            if not (isinstance(builder_disk, str) and os.path.isabs(builder_disk)):
                return self._send_json({"error": "builder_disk must be an absolute path"}, 400)
            env = os.environ.copy()
            env["SBC_BUILDER_DISK"] = builder_disk
        cmd = [bazel, "run", _DEPLOY_TARGET, "--"]
        if req.get("keep_builder"):
            cmd += ["--keep-builder"]
        cmd += [host]
        job = _start_job("deploy", cmd, cwd, env=env)
        return self._send_json(
            {"id": job["id"], "cmd": job["cmd"], "cwd": cwd, "builder_disk": builder_disk}
        )

    def deploy_poll(self, q):
        """GET /deploy?id=<id>[&from=<n>] — poll (new output lines from offset
        `from`). No id lists deploy jobs."""
        gate = self._deploy_gate()
        if gate:
            return self._send_json(*gate)
        jid = q.get("id", [None])[0]
        if not jid:
            with _JOBS_LOCK:
                jobs = [
                    {
                        "id": j["id"],
                        "running": not j["done"],
                        "rc": j["rc"],
                        "nlines": len(j["lines"]),
                        "started": j["started"],
                    }
                    for j in _JOBS.values()
                ]
            return self._send_json({"jobs": jobs})
        frm = int((q.get("from", ["0"])[0]) or 0)
        with _JOBS_LOCK:
            job = _JOBS.get(jid)
            if not job:
                return self._send_json({"error": "no such job: %s" % jid}, 404)
            lines = job["lines"][frm:]
            end = job["ended"] or time.time()
            resp = {
                "id": jid,
                "cmd": job["cmd"],
                "running": not job["done"],
                "rc": job["rc"],
                "from": frm,
                "next": frm + len(lines),
                "nlines": len(job["lines"]),
                "elapsed": round(end - job["started"], 1),
                "lines": lines,
            }
        return self._send_json(resp)

    def _deploy_kill(self, jid):
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
    # POST /deploy runs the ONE fixed Pi live-deploy (bazel run …deploy_live) in
    # this workspace on the host. It requires --token (it builds/switches here).
    ap.add_argument(
        "--workspace",
        default=os.environ.get("TOXC_WORKSPACE")
        or os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        help="repo checkout /deploy runs bazel in (default: this file's repo)",
    )
    ap.add_argument(
        "--bazel", default=os.environ.get("TOXC_BAZEL"), help="path to bazel (default: PATH lookup)"
    )
    args = ap.parse_args()

    cfg = discover(
        {
            "toeexpand": args.toeexpand,
            "toecollapse": args.toecollapse,
            "td_app": args.td_app,
            "token": args.token,
            "workspace": os.path.abspath(os.path.expanduser(args.workspace)),
            "bazel": args.bazel,
        }
    )
    Handler.cfg = cfg

    print(f"[toxc-host] {platform.platform()}  python {sys.version.split()[0]}")
    print(f"[toxc-host] toeexpand   : {cfg.get('toeexpand') or 'NOT FOUND'}")
    print(f"[toxc-host] toecollapse : {cfg.get('toecollapse') or 'NOT FOUND'}")
    print(f"[toxc-host] TouchDesigner: {cfg.get('td_app') or 'NOT FOUND'}")
    if cfg.get("token"):
        print("[toxc-host] auth token REQUIRED (X-Auth-Token)")
        print(f"[toxc-host] /deploy ENABLED: {_DEPLOY_TARGET} in {cfg.get('workspace')}")
        print(f"[toxc-host]   bazel: {cfg.get('bazel') or shutil.which('bazel') or 'NOT FOUND'}")
    else:
        print("[toxc-host] /deploy DISABLED (set --token to enable the Pi live-deploy)")
    print(f"[toxc-host] listening on http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
