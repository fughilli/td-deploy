"""Flash a base image to a removable disk, safely and with progress.

Public API:
    from deploy_engine.flasher import list_disks, flash
    for d in list_disks(): print(d.name, d.size_gb)
    flash(image_path, disk.id, on_progress=cb)   # raises on wrong/missing disk

Safety model:
  * `list_disks()` only ever returns removable/external media (see disks.py).
  * `flash()` re-checks the target is still a surfaced removable disk immediately
    before writing (`require_removable`), so a stale id can't hit the system disk.
  * The actual byte-copy runs in a minimal elevated worker (rawwrite.py); this
    module stays unprivileged and just tails the worker's progress file.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Callable, Optional

from .disks import Disk, find_disk, list_disks
from .elevate import elevated_command, worker_argv

OnProgress = Callable[[float, str], None]


def _noop(_f: float, _m: str) -> None:
    pass


def _device_hint(disk: Disk) -> Optional[str]:
    """OS-specific pre-write handle: mac wants /dev/diskN to unmount, win wants the
    disk number, linux nothing."""
    if sys.platform == "darwin":
        return disk.id.replace("/dev/r", "/dev/")   # rdiskN -> diskN for unmountDisk
    if sys.platform.startswith("win"):
        m = re.search(r"(\d+)$", disk.id)
        return m.group(1) if m else None
    return None


def require_removable(disk_id: str) -> Disk:
    """Confirm `disk_id` is *still* a removable disk right now, or refuse."""
    disk = find_disk(disk_id)
    if disk is None:
        raise ValueError(f"{disk_id!r} is not a removable disk (refusing to write)")
    return disk


def _tail_progress(path: str, proc: subprocess.Popen, on_progress: OnProgress) -> None:
    """Poll the worker's progress file until it finishes; raise on ERROR."""
    seen = 0
    error: Optional[str] = None
    done = False
    while True:
        alive = proc.poll() is None
        try:
            with open(path) as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        for line in lines[seen:]:
            if line.startswith("ERROR"):
                error = line[6:].strip()
            elif line.startswith("DONE"):
                done = True
                on_progress(1.0, "written")
            else:
                parts = line.split()
                if len(parts) == 2 and parts[1] != "0":
                    d, t = int(parts[0]), int(parts[1])
                    on_progress(d / t, f"{d >> 20} / {t >> 20} MiB")
        seen = len(lines)
        if error:
            raise RuntimeError(f"flash failed: {error}")
        if done or not alive:
            break
        time.sleep(0.2)
    rc = proc.wait()
    if not done and rc != 0:
        raise RuntimeError(f"flash worker exited with code {rc} "
                           "(elevation cancelled or write failed)")


def flash(image: str, disk_id: str, *, on_progress: OnProgress = _noop) -> Disk:
    """Write `image` to removable `disk_id` (raw), elevating for the byte-copy.

    Returns the Disk that was written. Raises if the target is not a currently
    removable disk, if elevation is cancelled, or on any write error.
    """
    if not os.path.exists(image):
        raise FileNotFoundError(image)
    disk = require_removable(disk_id)            # hard guard, re-checked live
    on_progress(0.0, f"preparing {disk.name} ({disk.size_gb:.1f} GB)")

    pfd, progress_file = tempfile.mkstemp(prefix="tdflash_", suffix=".progress")
    os.close(pfd)
    open(progress_file, "w").close()
    try:
        argv = worker_argv(image, disk.id, progress_file)
        cmd = elevated_command(argv, _device_hint(disk))
        proc = subprocess.Popen(cmd, stdout=sys.stderr, stderr=sys.stderr)
        _tail_progress(progress_file, proc, on_progress)
    finally:
        try:
            os.remove(progress_file)
        except OSError:
            pass
    return disk


__all__ = ["Disk", "list_disks", "find_disk", "require_removable", "flash"]
