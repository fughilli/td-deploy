"""The privileged raw-write worker — the ONLY code that runs as root/admin.

It is deliberately tiny and dependency-free: open the image, open the whole
device, copy in large chunks, and append `<done> <total>` progress lines to a
plain text file that the (unprivileged) parent tails. Keeping the elevated code
this small is the security posture — the parent handles all disk selection and
user confirmation; this only ever writes the bytes it is told to.

Entry points:
  * `python -m deploy_engine.flasher.rawwrite <image> <device> <progress_file>`
  * re-invoked through the frozen sidecar via its `--raw-write` flag (so the
    elevated process is the same signed binary, no external python needed).
"""
from __future__ import annotations

import os
import sys

CHUNK = 8 << 20  # 8 MiB


def raw_write(image: str, device: str, progress_file: str) -> int:
    total = os.path.getsize(image)
    done = 0
    # Unbuffered write to the whole device; O_SYNC keeps the SD honest.
    flags = os.O_WRONLY
    flags |= getattr(os, "O_SYNC", 0)
    dst = os.open(device, flags)
    try:
        with open(image, "rb") as src, open(progress_file, "w") as pf:
            pf.write(f"0 {total}\n"); pf.flush()
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                off = 0
                while off < len(chunk):
                    off += os.write(dst, chunk[off:])
                done += len(chunk)
                pf.write(f"{done} {total}\n"); pf.flush()
        os.fsync(dst)
    finally:
        os.close(dst)
    with open(progress_file, "a") as pf:
        pf.write(f"DONE {done} {total}\n")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write("usage: rawwrite <image> <device> <progress_file>\n")
        return 2
    try:
        return raw_write(argv[0], argv[1], argv[2])
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
