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

Config (env or flags; flags win)
    TOEEXPAND / --toeexpand   path to the toeexpand binary
    TOECOLLAPSE / --toecollapse
    TD_APP / --td-app         path to the TouchDesigner executable
    TOXC_HOST_TOKEN / --token optional shared secret; if set, clients must send
                              header  X-Auth-Token: <token>
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
    args = ap.parse_args()

    cfg = discover({
        "toeexpand": args.toeexpand,
        "toecollapse": args.toecollapse,
        "td_app": args.td_app,
        "token": args.token,
    })
    Handler.cfg = cfg

    print(f"[toxc-host] {platform.platform()}  python {sys.version.split()[0]}")
    print(f"[toxc-host] toeexpand   : {cfg.get('toeexpand')  or 'NOT FOUND'}")
    print(f"[toxc-host] toecollapse : {cfg.get('toecollapse') or 'NOT FOUND'}")
    print(f"[toxc-host] TouchDesigner: {cfg.get('td_app')     or 'NOT FOUND'}")
    if cfg.get("token"):
        print("[toxc-host] auth token REQUIRED (X-Auth-Token)")
    print(f"[toxc-host] listening on http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
