"""Enumerate *removable* disks safely on macOS / Windows / Linux.

Flashing writes raw blocks to a whole device — pointing it at the wrong disk
destroys data. The single most important safety property here is: **only ever
surface removable / external media**, never the system disk. Each OS backend
filters to external/USB/SD before a device is ever shown to the user, and the
parsers are split from the command execution so they can be unit-tested against
captured `diskutil`/`Get-Disk`/`lsblk` output with no hardware.
"""
from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Disk:
    id: str          # device path to write: /dev/rdiskN, \\.\PhysicalDriveN, /dev/sdX
    name: str        # human label (model / volume name)
    size: int        # bytes
    removable: bool  # always True for anything we surface
    bus: str = ""    # USB / SD / internal ...

    @property
    def size_gb(self) -> float:
        return self.size / 1e9

    def to_dict(self) -> dict:
        d = asdict(self)
        d["size_gb"] = round(self.size_gb, 1)
        return d


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


# ---------------------------------------------------------------- macOS --------
def parse_macos(list_plist: bytes, info_plists: dict[str, bytes]) -> list[Disk]:
    """`diskutil list -plist external physical` + per-disk `diskutil info -plist`."""
    top = plistlib.loads(list_plist)
    out: list[Disk] = []
    for dev in top.get("WholeDisks", []):
        info = plistlib.loads(info_plists[dev])
        if info.get("Internal", True):        # hard guard: never internal disks
            continue
        if not info.get("RemovableMediaOrExternalDevice", info.get("Ejectable", True)):
            continue
        name = info.get("MediaName") or info.get("IORegistryEntryName") or dev
        out.append(Disk(
            id=f"/dev/r{dev}",                 # raw node = much faster writes
            name=str(name),
            size=int(info.get("TotalSize") or info.get("Size") or 0),
            removable=True,
            bus=str(info.get("BusProtocol", "")),
        ))
    return out


def _list_macos() -> list[Disk]:
    list_plist = _run(["diskutil", "list", "-plist", "external", "physical"]).encode()
    top = plistlib.loads(list_plist)
    infos = {dev: _run(["diskutil", "info", "-plist", dev]).encode()
             for dev in top.get("WholeDisks", [])}
    return parse_macos(list_plist, infos)


# -------------------------------------------------------------- Windows --------
_PS_LIST = (
    "Get-Disk | Where-Object { $_.BusType -in 'USB','SD','SDCard' } | "
    "Select-Object Number,FriendlyName,Size,BusType,IsSystem,IsBoot | ConvertTo-Json -Depth 3"
)


def parse_windows(js: str) -> list[Disk]:
    data = json.loads(js) if js.strip() else []
    if isinstance(data, dict):
        data = [data]
    out: list[Disk] = []
    for d in data:
        if d.get("IsSystem") or d.get("IsBoot"):   # hard guard
            continue
        out.append(Disk(
            id=f"\\\\.\\PhysicalDrive{d['Number']}",
            name=str(d.get("FriendlyName") or f"Disk {d['Number']}"),
            size=int(d.get("Size") or 0),
            removable=True,
            bus=str(d.get("BusType", "")),
        ))
    return out


def _list_windows() -> list[Disk]:
    return parse_windows(_run(["powershell", "-NoProfile", "-Command", _PS_LIST]))


# ---------------------------------------------------------------- Linux --------
def parse_linux(js: str) -> list[Disk]:
    """`lsblk -J -b -o NAME,MODEL,SIZE,RM,TYPE,HOTPLUG` — RM/HOTPLUG => removable."""
    data = json.loads(js)
    out: list[Disk] = []
    for d in data.get("blockdevices", []):
        if d.get("type") != "disk":
            continue
        if not (d.get("rm") or d.get("hotplug")):  # hard guard: removable only
            continue
        out.append(Disk(
            id=f"/dev/{d['name']}",
            name=str(d.get("model") or d["name"]).strip(),
            size=int(d.get("size") or 0),
            removable=True,
            bus="usb" if d.get("hotplug") else "",
        ))
    return out


def _list_linux() -> list[Disk]:
    return parse_linux(_run(["lsblk", "-J", "-b", "-o", "NAME,MODEL,SIZE,RM,TYPE,HOTPLUG"]))


def list_disks() -> list[Disk]:
    """Removable/external disks only, for the current OS."""
    if sys.platform == "darwin":
        return _list_macos()
    if sys.platform.startswith("win"):
        return _list_windows()
    return _list_linux()


def find_disk(disk_id: str) -> Optional[Disk]:
    """Re-enumerate and confirm `disk_id` is still a surfaced removable disk.

    Called right before writing: guarantees we never write to an id that isn't in
    the current removable set (e.g. the SD was swapped for the system disk id)."""
    return next((d for d in list_disks() if d.id == disk_id), None)
