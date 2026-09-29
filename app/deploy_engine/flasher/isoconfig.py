"""Flash-time config for the x86_64 installer USB, written INTO the image's EFI
FAT partition as byte patches applied during the raw write.

The Pi SD image has a writable FAT `/boot/firmware` the flasher mounts after the
write and drops files onto (flash_config.write_boot_config). The x86 installer is
an ISO: the ISO 9660 filesystem is read-only and gone once the box is installed,
and its one FAT filesystem — the EFI boot image `efiboot.img` (label EFIBOOT),
exposed as a GPT/MBR partition of the hybrid ISO — isn't auto-mounted by macOS or
Windows (it's an EFI system partition). So instead of mounting anything, this
module edits that FAT in pure Python *in the image stream*:

    patches = config_patches(iso_path, payload)     # unprivileged, from the file
    rawwrite(..., patches)                          # applied as the bytes stream by

and the installed system picks the file up: deploy/nix/flash-config.nix imports
`TDCONFIG.JSN` from /dev/disk/by-label/EFIBOOT during nixos-install (the install
chroot runs the new system's activation with the USB attached) into
/var/lib/td-flash-config, and applies it on every boot.

Patches are whole, aligned blocks (default 4 KiB — raw disk nodes such as macOS's
/dev/rdiskN only take block-aligned writes) holding the image's own bytes with
the edits applied, so applying them is a plain overwrite of that range of the
stream. Supports FAT12/16/32 with 8.3 names in the root directory (all we need);
stdlib only.
"""

from __future__ import annotations

import json
import math
import struct
import time
from typing import BinaryIO, Iterable, List, Optional, Tuple

from .rawwrite import PATCH_MAGIC, apply_patches, read_patch_file  # noqa: F401 (re-exported)

CONFIG_NAME = "TDCONFIG.JSN"
EFI_LABEL = "EFIBOOT"
BLOCK = 4096

Patch = Tuple[int, bytes]


# ----------------------------------------------------------------- payload
def render_payload(
    hostname: Optional[str] = None,
    networks: Optional[Iterable[dict]] = None,
    authorized_keys: Optional[Iterable[str]] = None,
) -> Optional[bytes]:
    """The TDCONFIG.JSN body: the same settings the Pi flow writes as files
    (flash_config.render_config), as one JSON object. None when there's nothing
    to write."""
    from .. import flash_config

    cfg = flash_config.render_config(hostname, networks, authorized_keys)
    if not cfg:
        return None
    return (json.dumps(cfg, indent=1, sort_keys=True) + "\n").encode("utf-8")


# ----------------------------------------------------------------- block overlay
class Overlay:
    """Reads the image through a set of modified, block-aligned copies."""

    def __init__(self, f: BinaryIO, size: int, block: int = BLOCK):
        self.f, self.size, self.block = f, size, block
        self.blocks: dict[int, bytearray] = {}
        self.dirty: set[int] = set()

    def _blk(self, b: int) -> bytearray:
        if b not in self.blocks:
            self.f.seek(b)
            data = self.f.read(self.block)
            self.blocks[b] = bytearray(data.ljust(self.block, b"\0"))
        return self.blocks[b]

    def read(self, off: int, n: int) -> bytes:
        if off < 0 or off + n > self.size:
            raise ValueError(f"read {off}+{n} outside the image ({self.size} bytes)")
        out = bytearray()
        while n:
            b = off - off % self.block
            blk = self._blk(b)
            i = off - b
            k = min(n, self.block - i)
            out += blk[i : i + k]
            off, n = off + k, n - k
        return bytes(out)

    def write(self, off: int, data: bytes) -> None:
        if off < 0 or off + len(data) > self.size:
            raise ValueError(f"write {off}+{len(data)} outside the image ({self.size} bytes)")
        mv = memoryview(bytes(data))
        while mv:
            b = off - off % self.block
            blk = self._blk(b)
            i = off - b
            k = min(len(mv), self.block - i)
            if blk[i : i + k] != mv[:k]:
                blk[i : i + k] = mv[:k]
                self.dirty.add(b)
            off, mv = off + k, mv[k:]

    def patches(self) -> List[Patch]:
        """Only the blocks whose bytes actually changed."""
        out = []
        for b in sorted(self.dirty):
            data = bytes(self.blocks[b][: max(0, min(self.block, self.size - b))])
            out.append((b, data))
        return out


# ----------------------------------------------------------------- partitions
def _is_fat(bs: bytes) -> bool:
    if len(bs) < 512 or bs[510:512] != b"\x55\xaa":
        return False
    bps = struct.unpack_from("<H", bs, 11)[0]
    spc = bs[13]
    return (
        bps in (512, 1024, 2048, 4096)
        and spc in (1, 2, 4, 8, 16, 32, 64, 128)
        and bs[16] >= 1
        and (bs[54:57] == b"FAT" or bs[82:87] == b"FAT32")
    )


def _label(bs: bytes) -> str:
    raw = bs[71:82] if bs[82:87] == b"FAT32" else bs[43:54]
    return raw.decode("ascii", "replace").strip()


def partitions(ov: Overlay) -> List[Tuple[int, int]]:
    """(byte offset, byte length) of every partition in the GPT and the MBR."""
    out = []
    try:
        hdr = ov.read(512, 92)
    except ValueError:
        hdr = b""
    if hdr[:8] == b"EFI PART":
        ent_lba, n, esz = struct.unpack_from("<QII", hdr, 72)
        for i in range(min(n, 256)):
            try:
                e = ov.read(ent_lba * 512 + i * esz, 48)
            except ValueError:
                break
            if e[:16] == b"\0" * 16:
                continue
            first, last = struct.unpack_from("<QQ", e, 32)
            out.append((first * 512, (last - first + 1) * 512))
    mbr = ov.read(0, 512)
    if mbr[510:512] == b"\x55\xaa":
        for i in range(4):
            e = mbr[446 + 16 * i : 462 + 16 * i]
            ptype = e[4]
            start, count = struct.unpack_from("<II", e, 8)
            if ptype not in (0, 0xEE) and count:
                out.append((start * 512, count * 512))
    seen, uniq = set(), []
    for p in out:
        if p[0] not in seen:
            seen.add(p[0])
            uniq.append(p)
    return uniq


def find_efi_fat(ov: Overlay) -> Optional[int]:
    """Byte offset of the image's EFI FAT filesystem (label EFIBOOT preferred)."""
    fats = []
    for off, length in partitions(ov):
        if off + 512 > ov.size:
            continue
        bs = ov.read(off, 512)
        if _is_fat(bs):
            fats.append((off, _label(bs)))
    for off, label in fats:
        if label.upper() == EFI_LABEL:
            return off
    return fats[0][0] if fats else None


# ----------------------------------------------------------------- FAT
def _name83(name: str) -> bytes:
    stem, _, ext = name.upper().partition(".")
    if not (1 <= len(stem) <= 8 and len(ext) <= 3):
        raise ValueError(f"{name!r} is not an 8.3 name")
    return stem.ljust(8).encode("ascii") + ext.ljust(3).encode("ascii")


class Fat:
    def __init__(self, ov: Overlay, base: int):
        self.ov, self.base = ov, base
        bs = ov.read(base, 512)
        if not _is_fat(bs):
            raise ValueError(f"no FAT filesystem at offset {base}")
        self.bps, self.spc = struct.unpack_from("<H", bs, 11)[0], bs[13]
        self.rsvd = struct.unpack_from("<H", bs, 14)[0]
        self.nfats = bs[16]
        self.root_ent = struct.unpack_from("<H", bs, 17)[0]
        tot16, fatsz16 = struct.unpack_from("<H", bs, 19)[0], struct.unpack_from("<H", bs, 22)[0]
        tot32, fatsz32 = struct.unpack_from("<I", bs, 32)[0], struct.unpack_from("<I", bs, 36)[0]
        self.fatsz = fatsz16 or fatsz32
        tot = tot16 or tot32
        root_secs = (self.root_ent * 32 + self.bps - 1) // self.bps
        self.root_sec = self.rsvd + self.nfats * self.fatsz
        self.first_data = self.root_sec + root_secs
        self.nclusters = (tot - self.first_data) // self.spc
        self.bits = 12 if self.nclusters < 4085 else 16 if self.nclusters < 65525 else 32
        self.root_clus = struct.unpack_from("<I", bs, 44)[0] if self.bits == 32 else 0
        self.fsinfo = struct.unpack_from("<H", bs, 48)[0] if self.bits == 32 else 0
        self.cbytes = self.spc * self.bps
        self.eoc_min = {12: 0xFF8, 16: 0xFFF8, 32: 0x0FFFFFF8}[self.bits]
        self.eoc = {12: 0xFFF, 16: 0xFFFF, 32: 0x0FFFFFFF}[self.bits]

    # FAT entries --------------------------------------------------------
    def _ent_off(self, n: int, copy: int = 0) -> int:
        fat = self.base + (self.rsvd + copy * self.fatsz) * self.bps
        return fat + {12: n + n // 2, 16: n * 2, 32: n * 4}[self.bits]

    def get(self, n: int) -> int:
        off = self._ent_off(n)
        if self.bits == 12:
            v = struct.unpack("<H", self.ov.read(off, 2))[0]
            return v >> 4 if n & 1 else v & 0xFFF
        if self.bits == 16:
            return struct.unpack("<H", self.ov.read(off, 2))[0]
        return struct.unpack("<I", self.ov.read(off, 4))[0] & 0x0FFFFFFF

    def set(self, n: int, v: int) -> None:
        for copy in range(self.nfats):
            off = self._ent_off(n, copy)
            if self.bits == 12:
                cur = struct.unpack("<H", self.ov.read(off, 2))[0]
                new = (cur & 0x000F) | (v << 4) if n & 1 else (cur & 0xF000) | (v & 0xFFF)
                self.ov.write(off, struct.pack("<H", new & 0xFFFF))
            elif self.bits == 16:
                self.ov.write(off, struct.pack("<H", v & 0xFFFF))
            else:
                cur = struct.unpack("<I", self.ov.read(off, 4))[0]
                self.ov.write(off, struct.pack("<I", (cur & 0xF0000000) | (v & 0x0FFFFFFF)))

    def chain(self, c: int) -> List[int]:
        out = []
        while 2 <= c < self.eoc_min and len(out) <= self.nclusters:
            out.append(c)
            c = self.get(c)
        return out

    def cluster_off(self, c: int) -> int:
        return self.base + (self.first_data + (c - 2) * self.spc) * self.bps

    def _fsinfo_adjust(self, delta: int, next_free: Optional[int] = None) -> None:
        """Keep a FAT32 FSInfo free-cluster count right (when it's being kept)."""
        if self.bits != 32 or not self.fsinfo:
            return
        fs = self.base + self.fsinfo * self.bps
        if self.ov.read(fs, 4) != b"RRaA":
            return
        free, nxt = struct.unpack("<II", self.ov.read(fs + 488, 8))
        if free != 0xFFFFFFFF:
            free = max(0, min(self.nclusters, free + delta))
        if next_free is not None:
            nxt = next_free
        self.ov.write(fs + 488, struct.pack("<II", free, nxt))

    def alloc(self, n: int) -> List[int]:
        free = []
        for c in range(2, self.nclusters + 2):
            if self.get(c) == 0:
                free.append(c)
                if len(free) == n:
                    break
        if len(free) < n:
            raise OSError("the installer's EFI partition has no room for the config file")
        for a, b in zip(free, free[1:]):
            self.set(a, b)
        self.set(free[-1], self.eoc)
        self._fsinfo_adjust(-n, next_free=free[-1] + 1)
        return free

    # root directory -------------------------------------------------------
    def root_slots(self):
        if self.bits == 32:
            for c in self.chain(self.root_clus):
                o = self.cluster_off(c)
                for i in range(self.cbytes // 32):
                    yield o + 32 * i
        else:
            o = self.base + self.root_sec * self.bps
            for i in range(self.root_ent):
                yield o + 32 * i

    def write_file(self, name: str, data: bytes) -> None:
        n83 = _name83(name)
        slot = existing = None
        for off in self.root_slots():
            e = self.ov.read(off, 32)
            if e[0] == 0x00:
                slot = slot if slot is not None else off
                break
            if e[0] == 0xE5:
                slot = slot if slot is not None else off
                continue
            if e[11] == 0x0F or e[11] & 0x08:  # LFN piece / volume label
                continue
            if e[:11] == n83:
                existing = off
                break
        if existing is not None:
            e = self.ov.read(existing, 32)
            first = struct.unpack_from("<H", e, 26)[0] | (struct.unpack_from("<H", e, 20)[0] << 16)
            old = self.chain(first)
            for c in old:
                self.set(c, 0)
            self._fsinfo_adjust(len(old))
            slot = existing
        if slot is None:
            raise OSError("the installer's EFI partition root directory is full")
        first = 0
        if data:
            clusters = self.alloc(math.ceil(len(data) / self.cbytes))
            first = clusters[0]
            for i, c in enumerate(clusters):
                chunk = data[i * self.cbytes : (i + 1) * self.cbytes]
                self.ov.write(self.cluster_off(c), chunk.ljust(self.cbytes, b"\0"))
        t = time.localtime()
        fdate = ((max(t.tm_year, 1980) - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
        ftime = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
        entry = n83 + struct.pack(
            "<BBBHHHHHHHI",
            0x20,  # archive
            0,
            0,
            ftime,
            fdate,
            fdate,
            (first >> 16) & 0xFFFF,
            ftime,
            fdate,
            first & 0xFFFF,
            len(data),
        )
        self.ov.write(slot, entry)

    def read_file(self, name: str) -> Optional[bytes]:
        """Read a root-directory file back (tests, verification)."""
        n83 = _name83(name)
        for off in self.root_slots():
            e = self.ov.read(off, 32)
            if e[0] == 0x00:
                return None
            if e[0] == 0xE5 or e[11] == 0x0F or e[:11] != n83:
                continue
            first = struct.unpack_from("<H", e, 26)[0] | (struct.unpack_from("<H", e, 20)[0] << 16)
            size = struct.unpack_from("<I", e, 28)[0]
            buf = b"".join(
                self.ov.read(self.cluster_off(c), self.cbytes) for c in self.chain(first)
            )
            return buf[:size]
        return None


# ----------------------------------------------------------------- API
def config_patches(
    image: str, payload: bytes, name: str = CONFIG_NAME, block: int = BLOCK
) -> List[Patch]:
    """The block patches that put `payload` at /<name> in `image`'s EFI FAT."""
    import os

    size = os.path.getsize(image)
    with open(image, "rb") as f:
        ov = Overlay(f, size, block)
        off = find_efi_fat(ov)
        if off is None:
            raise ValueError("no EFI FAT partition found in the installer image")
        Fat(ov, off).write_file(name, payload)
        return ov.patches()


def write_patch_file(path: str, patches: Iterable[Patch]) -> None:
    with open(path, "wb") as fh:
        fh.write(PATCH_MAGIC)
        for off, data in patches:
            fh.write(struct.pack("<QI", off, len(data)))
            fh.write(data)
