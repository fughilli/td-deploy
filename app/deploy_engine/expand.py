"""Expand a .toe/.tox to the toeexpand `*.dir` tree.

Production: run TouchDesigner's local `toeexpand` (the app runs on the same machine
as TD). Dev/container fallback: the Mac host bridge (hostbridge/td_host_server.py),
used when there's no local toeexpand — so the engine is testable without TD.

toeexpand discovery is ported from hostbridge/td_host_server.py; toeexpand exits
rc=1 on SUCCESS (prints "expanded into …" on stderr) — not an error.
"""
from __future__ import annotations

import glob
import io
import os
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request

from .progress import Progress

# --- local toeexpand discovery (ported from td_host_server._candidate_td_dirs) ---

def _candidate_td_dirs() -> list[str]:
    pats = [
        "/Applications/TouchDesigner*.app/Contents/MacOS",            # macOS
        "/Applications/Derivative/TouchDesigner*.app/Contents/MacOS",
        "C:/Program Files/Derivative/TouchDesigner*/bin",             # Windows
        os.path.expanduser("~/TouchDesigner*/bin"),                   # Linux-ish
    ]
    out: list[str] = []
    for p in pats:
        out.extend(sorted(glob.glob(p)))
    return out


def discover_toeexpand() -> str | None:
    env = os.environ.get("TOEEXPAND")
    if env and os.path.isfile(env):
        return env
    for d in _candidate_td_dirs():
        for n in ("toeexpand", "toeexpand.exe"):
            cand = os.path.join(d, n)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return None


def _find_dir(root: str) -> str:
    for dp, dns, _fn in os.walk(root):
        for d in dns:
            if d.endswith(".dir"):
                return os.path.join(dp, d)
    raise RuntimeError(f"no *.dir produced under {root}: {sorted(os.listdir(root))}")


def _expand_local(exe: str, toe_path: str, workdir: str, progress: Progress) -> str:
    """Run `toeexpand <copy-of-toe>` in workdir; return the produced *.dir."""
    name = os.path.basename(toe_path)
    dst = os.path.join(workdir, name)
    with open(toe_path, "rb") as s, open(dst, "wb") as d:
        d.write(s.read())
    progress.log(f"toeexpand {name}")
    # rc=1 is success for toeexpand; only a crash (no *.dir) is a real error.
    subprocess.run([exe, name], cwd=workdir, capture_output=True)
    return _find_dir(workdir)


# --- bridge fallback (ported from cli.py) ---

def _host_token() -> str | None:
    t = os.environ.get("TOXC_HOST_TOKEN")
    if t:
        return t.strip()
    try:
        with open("/workspace/credentials/toxc_host_token.txt") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def http_get_bytes(url: str, data: bytes | None = None, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    tok = _host_token()
    if tok:
        req.add_header("X-Auth-Token", tok)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _expand_bridge(toe_path: str, workdir: str, bridge: str, progress: Progress) -> str:
    name = os.path.basename(toe_path)
    progress.log(f"toeexpand via bridge {bridge}")
    if os.path.isfile(toe_path):
        with open(toe_path, "rb") as fh:
            body = fh.read()
        url = f"http://{bridge}/expand?name={urllib.parse.quote(name)}&format=tar"
        tgz = http_get_bytes(url, data=body)
    else:
        url = f"http://{bridge}/expand_local?path={urllib.parse.quote(toe_path)}&format=tar"
        tgz = http_get_bytes(url)
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tf:
        tf.extractall(workdir)
    return _find_dir(workdir)


def expand(toe_path: str, workdir: str | None = None, *, bridge: str | None = None,
           progress: Progress = Progress()) -> str:
    """Return the path to the expanded `*.dir`. Prefers local toeexpand; falls back
    to the bridge when no local toeexpand and `bridge` is given."""
    workdir = workdir or tempfile.mkdtemp(prefix="toxc_expand_")
    os.makedirs(workdir, exist_ok=True)
    exe = discover_toeexpand()
    if exe:
        return _expand_local(exe, toe_path, workdir, progress)
    if bridge:
        return _expand_bridge(toe_path, workdir, bridge, progress)
    raise RuntimeError(
        "toeexpand not found (install TouchDesigner) and no --bridge fallback given"
    )
