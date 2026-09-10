"""Build the privileged command that runs the raw-write worker as root/admin.

The worker is re-invoked as *this same program*: the frozen sidecar binary with a
`--raw-write` flag (so the elevated process is the same code the user already
trusts, no external interpreter), or `python <rawwrite.py>` in dev.
"""
from __future__ import annotations

import os
import shlex
import sys


def worker_argv(image: str, device: str, progress_file: str) -> list[str]:
    """Unprivileged form of the worker command (before elevation wrapping)."""
    args = [image, device, progress_file]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--raw-write", *args]
    rawwrite = os.path.join(os.path.dirname(__file__), "rawwrite.py")
    return [sys.executable, rawwrite, *args]


def _applescript_quote(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def elevated_macos(argv: list[str], unmount_disk: str | None) -> list[str]:
    """osascript wrapper → one Touch-ID/password prompt runs the whole thing as root.

    `unmount_disk` (e.g. /dev/disk4) is force-unmounted first — writing the raw
    node fails while volumes are mounted."""
    inner = ""
    if unmount_disk:
        inner += f"diskutil unmountDisk force {shlex.quote(unmount_disk)}; "
    inner += " ".join(shlex.quote(a) for a in argv)
    script = f'do shell script "{_applescript_quote(inner)}" with administrator privileges'
    return ["osascript", "-e", script]


def elevated_windows(argv: list[str], disk_number: str | None) -> list[str]:
    """UAC-elevated PowerShell: offline+clear the disk, then run the worker."""
    exe = argv[0]
    rest = argv[1:]
    arg_list = ",".join("'" + a.replace("'", "''") + "'" for a in rest)
    pre = ""
    if disk_number is not None:
        pre = (f"Set-Disk -Number {disk_number} -IsOffline $true; "
               f"Set-Disk -Number {disk_number} -IsReadOnly $false; ")
    ps = (
        pre +
        f"$p = Start-Process -FilePath '{exe}' -ArgumentList {arg_list} "
        f"-Verb RunAs -Wait -PassThru; exit $p.ExitCode"
    )
    return ["powershell", "-NoProfile", "-Command", ps]


def elevated_linux(argv: list[str], _disk: str | None) -> list[str]:
    """Dev/testing only: prefer pkexec (GUI prompt), fall back to sudo -n."""
    from shutil import which
    if which("pkexec"):
        return ["pkexec", *argv]
    return ["sudo", "-n", *argv]


def elevated_command(argv: list[str], device_hint: str | None) -> list[str]:
    if sys.platform == "darwin":
        return elevated_macos(argv, device_hint)
    if sys.platform.startswith("win"):
        return elevated_windows(argv, device_hint)
    return elevated_linux(argv, device_hint)
