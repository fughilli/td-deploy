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
OnWarn = Callable[[str], None]


def _noop(_f: float, _m: str) -> None:
    pass


def _device_hint(disk: Disk) -> Optional[str]:
    """OS-specific pre-write handle: mac wants /dev/diskN to unmount, win wants the
    disk number, linux nothing."""
    if sys.platform == "darwin":
        return disk.id.replace("/dev/r", "/dev/")  # rdiskN -> diskN for unmountDisk
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
        raise RuntimeError(
            f"flash worker exited with code {rc} " "(elevation cancelled or write failed)"
        )


def _boot_disk_id(disk: Disk) -> str:
    """The whole-disk node whose FAT boot partition we mount post-write.

    The raw write targets the fast raw node on macOS (/dev/rdiskN); mounting wants
    the buffered node (/dev/diskN)."""
    if sys.platform == "darwin":
        return disk.id.replace("/dev/r", "/dev/")
    return disk.id


def _find_fat_mount_macos(base: str) -> Optional[str]:
    """After `diskutil mountDisk /dev/diskN`, find the FAT boot volume it surfaced.

    NixOS SD images label their FAT boot partition; we don't know the label, so pick
    the mounted volume from this disk that actually contains the boot layout (a
    `cmdline.txt`/`config.txt` next to where we drop td-hostname), falling back to the
    first FAT partition of the disk."""
    try:
        out = subprocess.run(
            ["diskutil", "list", "-plist", base],
            capture_output=True,
            check=True,
        ).stdout
        import plistlib

        top = plistlib.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    parts = []
    for dev in top.get("AllDisksAndPartitions", []):
        for p in dev.get("Partitions", []):
            parts.append(p)
    fat_mounts = []
    for p in parts:
        mp = p.get("MountPoint")
        if not mp:
            continue
        content = str(p.get("Content", "")).lower()
        if "fat" in content or "windows" in content or "efi" in content:
            fat_mounts.append(mp)
    for mp in fat_mounts:
        if os.path.exists(os.path.join(mp, "config.txt")) or os.path.exists(
            os.path.join(mp, "cmdline.txt")
        ):
            return mp
    return fat_mounts[0] if fat_mounts else None


def _apply_boot_config(
    disk: Disk,
    hostname: Optional[str],
    networks,
    on_warn: OnWarn,
    authorized_keys=None,
) -> None:
    """Best-effort: mount the freshly-written card's FAT boot partition and drop the
    flash-time config onto it, then unmount. NEVER raises — the raw write already
    succeeded, so a mount/write failure must not fail the flash; it logs a warning."""
    from .. import flash_config

    if not hostname and not networks and not authorized_keys:
        return

    base = _boot_disk_id(disk)
    try:
        if sys.platform == "darwin":
            # Removable FAT mounts under /Volumes owned by the user — no sudo needed
            # once the elevated raw write is done.
            subprocess.run(
                ["diskutil", "mountDisk", base],
                capture_output=True,
                check=True,
                timeout=60,
            )
            mount_dir = _find_fat_mount_macos(base)
            if not mount_dir:
                on_warn("could not locate the card's boot partition; skipped hostname/WiFi")
                return
            try:
                flash_config.write_boot_config(
                    mount_dir,
                    hostname=hostname,
                    networks=networks,
                    authorized_keys=authorized_keys,
                )
            finally:
                subprocess.run(["diskutil", "eject", base], capture_output=True, timeout=60)
        elif sys.platform.startswith("win"):
            # Windows auto-mounts the FAT partition at a drive letter after the write.
            mount_dir = _find_fat_mount_windows(disk)
            if not mount_dir:
                on_warn("could not locate the card's boot partition; skipped hostname/WiFi")
                return
            flash_config.write_boot_config(
                mount_dir,
                hostname=hostname,
                networks=networks,
                authorized_keys=authorized_keys,
            )
        else:
            _apply_boot_config_linux(disk, hostname, networks, on_warn, authorized_keys)
    except Exception as e:  # noqa: BLE001 - best-effort, never fatal
        on_warn(f"flashed OK, but writing hostname/WiFi to the card failed: {e}")


def _find_fat_mount_windows(disk: Disk):
    """Return the drive letter (path) of the first FAT partition on `disk`, or None."""
    m = re.search(r"(\d+)$", disk.id)
    if not m:
        return None
    num = m.group(1)
    ps = (
        f"Get-Partition -DiskNumber {num} | Get-Volume | "
        "Where-Object { $_.FileSystem -in 'FAT','FAT32' -and $_.DriveLetter } | "
        "Select-Object -First 1 -ExpandProperty DriveLetter"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{out}:\\" if out else None


def _apply_boot_config_linux(
    disk: Disk, hostname, networks, on_warn: OnWarn, authorized_keys=None
) -> None:
    """Linux best-effort mount. The first partition of the SD is the FAT boot part;
    mount it to a temp dir (may need udisks/pkexec-free auto-mount), write, unmount."""
    from .. import flash_config

    part = disk.id + ("p1" if re.search(r"\d$", disk.id) else "1")
    if not os.path.exists(part):
        on_warn("could not locate the card's boot partition; skipped hostname/WiFi")
        return
    mnt = tempfile.mkdtemp(prefix="tdboot_")
    mounted = False
    try:
        try:
            subprocess.run(["mount", part, mnt], capture_output=True, check=True, timeout=60)
            mounted = True
        except (OSError, subprocess.SubprocessError):
            # Fall back to udisksctl (user-mount, no root) — path unknown, so bail
            # to a warning rather than guessing.
            on_warn("could not mount the card's boot partition; skipped hostname/WiFi")
            return
        flash_config.write_boot_config(
            mnt, hostname=hostname, networks=networks, authorized_keys=authorized_keys
        )
    finally:
        if mounted:
            subprocess.run(["umount", mnt], capture_output=True, timeout=60)
        try:
            os.rmdir(mnt)
        except OSError:
            pass


def flash(
    image: str,
    disk_id: str,
    *,
    on_progress: OnProgress = _noop,
    hostname: Optional[str] = None,
    networks=None,
    authorized_keys=None,
    on_warn: OnWarn = None,
) -> Disk:
    """Write `image` to removable `disk_id` (raw), elevating for the byte-copy.

    The flash-time hostname + WiFi keyfiles + `authorized_keys` (the active deploy
    keys' public lines) ride along, depending on the image kind:

    * a Pi SD image (`.img`): after the raw write, best-effort mounts the card's
      FAT boot partition and drops them onto it (see flash_config);
    * an x86 installer (`.iso`): they're written into the ISO's EFI FAT partition
      as TDCONFIG.JSN *during* the raw write, as block patches computed here from
      the image (see isoconfig) — nothing to mount.

    Either step is NON-FATAL: on failure it emits a warning via `on_warn` and the
    flash still succeeds — a good raw write is never bricked by a failed
    customization drop.

    Returns the Disk that was written. Raises if the target is not a currently
    removable disk, if elevation is cancelled, or on any raw-write error.
    """
    if not os.path.exists(image):
        raise FileNotFoundError(image)
    disk = require_removable(disk_id)  # hard guard, re-checked live
    warn = on_warn or (lambda _m: None)
    if disk.size and os.path.getsize(image) > disk.size:
        raise ValueError(
            f"{disk.name} ({disk.size_gb:.1f} GB) is smaller than the image "
            f"({os.path.getsize(image) / 1e9:.1f} GB)"
        )
    on_progress(0.0, f"preparing {disk.name} ({disk.size_gb:.1f} GB)")

    installer = is_installer_image(image)
    patch_file = None
    if installer:
        patch_file = _installer_patch_file(image, hostname, networks, authorized_keys, warn)

    pfd, progress_file = tempfile.mkstemp(prefix="tdflash_", suffix=".progress")
    os.close(pfd)
    open(progress_file, "w").close()
    try:
        argv = worker_argv(image, disk.id, progress_file, patch_file)
        cmd = elevated_command(argv, _device_hint(disk))
        proc = subprocess.Popen(cmd, stdout=sys.stderr, stderr=sys.stderr)
        _tail_progress(progress_file, proc, on_progress)
    finally:
        for f in (progress_file, patch_file):
            if f:
                try:
                    os.remove(f)
                except OSError:
                    pass

    # Raw write succeeded — now apply per-card config (best-effort, non-fatal).
    if not installer:
        _apply_boot_config(disk, hostname, networks, warn, authorized_keys)
    return disk


def is_installer_image(image: str) -> bool:
    """An x86 install-USB image (a hybrid ISO) rather than a Pi SD image."""
    name = image.lower()
    if name.endswith(".iso"):
        return True
    try:
        with open(image, "rb") as fh:
            fh.seek(0x8001)
            return fh.read(5) == b"CD001"  # ISO 9660 primary volume descriptor
    except OSError:
        return False


def _installer_patch_file(image, hostname, networks, authorized_keys, on_warn) -> Optional[str]:
    """Render the flash-time config into block patches for the raw writer; None
    (with a warning) when there is nothing to write or it can't be placed."""
    from . import isoconfig

    try:
        payload = isoconfig.render_payload(hostname, networks, authorized_keys)
        if payload is None:
            return None
        patches = isoconfig.config_patches(image, payload)
        fd, path = tempfile.mkstemp(prefix="tdflash_", suffix=".patch")
        os.close(fd)
        isoconfig.write_patch_file(path, patches)
        os.chmod(path, 0o600)  # carries the WiFi PSK; root reads it regardless
        return path
    except Exception as e:  # noqa: BLE001 - best-effort, never fatal
        on_warn(f"could not add hostname/WiFi/deploy key to the installer USB: {e}")
        return None


__all__ = ["Disk", "list_disks", "find_disk", "require_removable", "flash", "is_installer_image"]
