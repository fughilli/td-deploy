"""Push a finished artifact to a running Pi and make it live: rsync to a staging
dir, atomically repoint the `current` symlink, restart the runtime service, prune
old staging dirs. The deploy key logs in as root, so no on-Pi sudo is needed.

  /var/lib/tdplayer/staging-<ts>/   <- rsync target
  /var/lib/tdplayer/current         -> staging-<ts>   (atomic ln -sfn)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

from . import _paths
from .progress import Progress

REMOTE_BASE = "/var/lib/tdplayer"
DEFAULT_SERVICE = "sbc-tdplayer"


def default_key() -> str:
    return os.path.join(_paths.REPO_ROOT, "deploy", "secrets", "deploy_key")


def _ssh_base(key: str) -> list[str]:
    return ["ssh", "-i", key, "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]


def push(art_dir: str, pi_host: str, *, user: str = "root", key: str | None = None,
         service: str = DEFAULT_SERVICE, keep: int = 3,
         progress: Progress = Progress()) -> str:
    key = key or default_key()
    target = f"{user}@{pi_host}"
    ts = time.strftime("%Y%m%d-%H%M%S")
    staging = f"{REMOTE_BASE}/staging-{ts}"
    ssh = _ssh_base(key)

    progress.phase("push", 0.0, f"{target}:{staging}")
    subprocess.run(ssh + [target, f"mkdir -p {REMOTE_BASE}"], check=True)

    src = art_dir.rstrip("/") + "/"
    ssh_cmd = " ".join(_ssh_base(key))
    if shutil.which("rsync"):
        subprocess.run(["rsync", "-a", "--delete", "-e", ssh_cmd, src, f"{target}:{staging}/"],
                       check=True)
    else:  # Windows fallback: scp -r (no --delete; staging is fresh each time)
        progress.log("rsync not found; scp -r fallback")
        subprocess.run(ssh + [target, f"mkdir -p {staging}"], check=True)
        scp = ["scp", "-r", "-i", key, "-o", "IdentitiesOnly=yes",
               "-o", "StrictHostKeyChecking=accept-new"]
        for entry in os.listdir(art_dir):
            subprocess.run(scp + [os.path.join(art_dir, entry), f"{target}:{staging}/"], check=True)

    progress.phase("restart", 0.0, service)
    # world-readable (the service runs as the tdplayer user), atomic swap, restart,
    # prune all but the newest `keep` staging dirs.
    remote = (
        f"chmod -R a+rX {staging} && "
        f"ln -sfn {staging} {REMOTE_BASE}/current && "
        f"systemctl restart {service} && "
        f"ls -1dt {REMOTE_BASE}/staging-* 2>/dev/null | tail -n +{keep + 1} | xargs -r rm -rf"
    )
    subprocess.run(ssh + [target, remote], check=True)
    progress.phase("restart", 1.0, "live")
    return staging
