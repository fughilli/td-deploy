"""The privileged raw-write worker — the ONLY code that runs as root/admin.

It is deliberately tiny and dependency-free: open the image, open the whole
device, copy in large chunks, and append `<done> <total>` progress lines to a
plain text file that the (unprivileged) parent tails. Keeping the elevated code
this small is the security posture — the parent handles all disk selection and
user confirmation; this only ever writes the bytes it is told to.

Optionally it overlays a few block patches onto the stream as it writes (the
x86 installer USB's flash-time config, computed unprivileged by isoconfig.py):
a patch file of `TDPATCH1` + (u64 offset, u32 length, bytes)* records. It still
only ever writes within the image's own byte range.

Entry points:
  * `python -m deploy_engine.flasher.rawwrite <image> <device> <progress_file> [patch_file]`
  * re-invoked through the frozen sidecar via its `--raw-write` flag (so the
    elevated process is the same signed binary, no external python needed).
"""

from __future__ import annotations

import errno
import os
import struct
import sys

CHUNK = 8 << 20  # 8 MiB
PATCH_MAGIC = b"TDPATCH1"


def read_patch_file(path: str) -> list:
    out = []
    with open(path, "rb") as fh:
        if fh.read(len(PATCH_MAGIC)) != PATCH_MAGIC:
            raise ValueError(f"{path}: not a patch file")
        while True:
            head = fh.read(12)
            if not head:
                break
            off, n = struct.unpack("<QI", head)
            data = fh.read(n)
            if len(data) != n:
                raise ValueError(f"{path}: truncated patch file")
            out.append((off, data))
    return out


def apply_patches(chunk: bytes, chunk_off: int, patches: list) -> bytes:
    """Overlay every patch that intersects [chunk_off, chunk_off + len(chunk))."""
    end = chunk_off + len(chunk)
    buf = None
    for off, data in patches:
        lo, hi = max(off, chunk_off), min(off + len(data), end)
        if lo >= hi:
            continue
        if buf is None:
            buf = bytearray(chunk)
        buf[lo - chunk_off : hi - chunk_off] = data[lo - off : hi - off]
    return bytes(buf) if buf is not None else chunk


def _open_help(device: str, e: OSError) -> str:
    """Turn an EPERM/EACCES on the raw device into an actionable message.

    On macOS Sonoma/Sequoia, raw access to removable media is gated by TCC (Full
    Disk Access) even for a root process — the elevated write is attributed to the
    GUI app, so opening /dev/rdiskN returns EPERM ("Operation not permitted") until
    the app is granted access. No code can bypass this; the user must grant it."""
    if sys.platform == "darwin" and e.errno in (errno.EPERM, errno.EACCES):
        return (
            f"macOS blocked raw disk access to {device}. Grant 'td-deploy Studio' "
            "Full Disk Access in System Settings > Privacy & Security > Full Disk "
            "Access (toggle it on, then relaunch the app) and flash again. "
            f"[{e.strerror}]"
        )
    return f"cannot open {device} for writing: {e.strerror}"


def raw_write(image: str, device: str, progress_file: str, patch_file: str | None = None) -> int:
    total = os.path.getsize(image)
    patches = read_patch_file(patch_file) if patch_file else []
    done = 0
    # Unbuffered write to the whole device; O_SYNC keeps the SD honest.
    flags = os.O_WRONLY
    flags |= getattr(os, "O_SYNC", 0)
    try:
        dst = os.open(device, flags)
    except OSError as e:
        raise OSError(_open_help(device, e)) from e
    try:
        with open(image, "rb") as src, open(progress_file, "w") as pf:
            pf.write(f"0 {total}\n")
            pf.flush()
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                if patches:
                    chunk = apply_patches(chunk, done, patches)
                off = 0
                while off < len(chunk):
                    off += os.write(dst, chunk[off:])
                done += len(chunk)
                pf.write(f"{done} {total}\n")
                pf.flush()
        os.fsync(dst)
    finally:
        os.close(dst)
    with open(progress_file, "a") as pf:
        pf.write(f"DONE {done} {total}\n")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        sys.stderr.write("usage: rawwrite <image> <device> <progress_file> [patch_file]\n")
        return 2
    try:
        return raw_write(*argv)
    except Exception as e:  # noqa: BLE001 - report to progress file for the parent
        try:
            with open(argv[2], "a") as pf:
                pf.write(f"ERROR {e}\n")
        except OSError:
            pass
        sys.stderr.write(f"rawwrite failed: {e}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
