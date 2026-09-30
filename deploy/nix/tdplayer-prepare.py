"""tdplayer-prepare <staging> — ready a freshly pushed artifact before it goes live.

Run as root over ssh by the desktop app (deploy_engine.push pre_restart) on the
new staging dir, before the `current` symlink swap. For a Python-host artifact
(schedule.json "format": "toxc-host/1" with host.python.requirements) it builds
the project's venv and links it as <project>/.venv, where the runtime's host
co-process looks first, and hands the project folder to the player's service
user (the project writes there, as it does next to its .toe). Anything else:
nothing to do.

Venvs live in /var/lib/tdplayer/venvs/<hash of python + requirements>, built in a
temp dir and renamed into place, so a failed install never leaves a half venv a
later deploy would trust, and an unchanged requirements file reuses the last
one (no network, instant). Wheels come from <project>/wheels when the app
shipped a wheelhouse (offline install), else from PyPI; binary-only either way —
there is no compiler on the box.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.environ.get("TDPLAYER_ROOT", "/var/lib/tdplayer")
PLAYER_USER = os.environ.get("TDPLAYER_USER", "tdplayer")  # services.sbcApps.tdplayer.user
KEEP = 3


def log(msg):
    print(f"tdplayer-prepare: {msg}", file=sys.stderr, flush=True)


def main(argv):
    if len(argv) != 2:
        print("usage: tdplayer-prepare <staging-dir>", file=sys.stderr)
        return 2
    staging = os.path.abspath(argv[1])
    try:
        with open(os.path.join(staging, "schedule.json")) as fh:
            sched = json.load(fh)
    except (OSError, ValueError) as e:
        log(f"no readable schedule.json ({e}); nothing to prepare")
        return 0
    host = sched.get("host") or {}
    req_rel = (host.get("python") or {}).get("requirements")
    if not req_rel:
        return 0
    project = os.path.join(staging, host.get("project_folder", "project"))
    req = os.path.join(project, req_rel)
    if not os.path.isfile(req):
        log(f"requirements {req_rel!r} missing from the artifact")
        return 1
    with open(req, "rb") as fh:
        body = fh.read()
    extra = jit_requirements(host.get("python") or {}, body)
    wheels_dir = os.path.join(project, "wheels")
    if extra and os.path.isdir(wheels_dir):  # offline: only what the wheelhouse has
        have = [f.lower() for f in os.listdir(wheels_dir)]
        extra = [p for p in extra if any(f.startswith(p + "-") for f in have)]
    if extra:
        body += (
            "\n# td-deploy: native kernels (tdhost/jit.py)\n" + "\n".join(extra) + "\n"
        ).encode()
    key = hashlib.sha256(sys.version.encode() + b"\0" + body).hexdigest()[:16]
    venvs = os.path.join(ROOT, "venvs")
    venv = os.path.join(venvs, key)
    os.makedirs(venvs, exist_ok=True)
    if not os.path.exists(os.path.join(venv, ".complete")):
        log(f"building venv {key} from {req_rel}")
        tmp = tempfile.mkdtemp(prefix=f".{key}-", dir=venvs)
        try:
            subprocess.run([sys.executable, "-m", "venv", tmp], check=True)
            pip = [
                os.path.join(tmp, "bin", "python"),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
            ]
            wheels = os.path.join(project, "wheels")
            if os.path.isdir(wheels):
                pip += ["--no-index", "--find-links", wheels]
            else:
                pip += ["--only-binary=:all:", "--cache-dir", os.path.join(ROOT, "pip-cache")]
            subprocess.run(pip + ["-r", req], check=True, stdout=sys.stderr)
            if extra:
                # best effort: without them the project runs as plain Python
                r = subprocess.run(pip + extra, stdout=sys.stderr)
                if r.returncode:
                    log(f"couldn't install {' '.join(extra)}; native kernels off")
            open(os.path.join(tmp, ".complete"), "w").close()
            # The venv records its own path in bin/ scripts; rebuild those at the
            # final location by recreating the (already populated) venv there.
            if os.path.exists(venv):
                shutil.rmtree(venv)
            os.rename(tmp, venv)
            subprocess.run([sys.executable, "-m", "venv", venv], check=True)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        subprocess.run(["chmod", "-R", "a+rX", venv], check=True)
    else:
        log(f"reusing venv {key}")
    link = os.path.join(project, ".venv")
    if os.path.islink(link) or os.path.exists(link):
        if os.path.isdir(link) and not os.path.islink(link):
            shutil.rmtree(link)
        else:
            os.remove(link)
    os.symlink(venv, link)
    os.utime(venv)
    _prune(venvs, keep=venv)
    _give_project_to_player(project)
    return 0


JIT_PACKAGES = ("numba", "scipy")  # scipy: Numba's np.linalg / BLAS matmul


def jit_requirements(python: dict, body: bytes) -> list:
    """Packages the host's native-kernel compiler needs, unless the project opted
    out ({"python": {"jit": false}}) or already lists them."""
    cfg = python.get("jit", True)
    if cfg is False or (isinstance(cfg, dict) and cfg.get("enabled") is False):
        return []
    import re

    listed = {
        re.split(r"[\s<>=!~;\[]", line.strip(), maxsplit=1)[0].lower()
        for line in body.decode(errors="replace").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    return [p for p in JIT_PACKAGES if p not in listed]


def _give_project_to_player(project):
    """The project folder is the project's working directory on the player, as
    it is in TouchDesigner: its Python writes logs/, caches, recordings there. The
    push lands it root-owned, so hand it to the runtime's service user. (-R doesn't
    follow symlinks: the shared, root-owned venv behind .venv stays untouched.)"""
    import pwd

    try:
        pw = pwd.getpwnam(PLAYER_USER)
    except KeyError:
        log(f"no {PLAYER_USER!r} user; leaving {project} root-owned")
        return
    subprocess.run(["chown", "-R", "-h", f"{pw.pw_uid}:{pw.pw_gid}", project], check=True)


def _prune(venvs, keep):
    """Drop all but the KEEP most recently used venvs (and stale temp dirs)."""
    entries = []
    for name in os.listdir(venvs):
        p = os.path.join(venvs, name)
        if name.startswith("."):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isdir(p):
            entries.append((os.path.getmtime(p), p))
    entries.sort(reverse=True)
    for _, p in entries[KEEP:]:
        if p != keep:
            shutil.rmtree(p, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
