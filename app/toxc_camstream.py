#!/usr/bin/env python3
"""Stream this machine's camera (or a video file / URL) into a player's virtual
camera, /dev/video10 — test camera-driven pieces without a webcam at the box.

    python app/toxc_camstream.py tricksterplayer                 # camera 0
    python app/toxc_camstream.py tricksterplayer --source clip.mp4
    python app/toxc_camstream.py tricksterplayer --source rtsp://cam.local/stream

The player's project then reads camera index 10 (/dev/video10) like a webcam.
Needs the x86 player image with deploy/nix/virtual-camera.nix. Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from deploy_engine import camstream, cli_progress  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("host", help="player host (e.g. tdplayer.local)")
    ap.add_argument("--source", default="0", help="camera index, video file, or URL (default 0)")
    ap.add_argument("--size", default="1280x720", help="WxH sent to the player (default 1280x720)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--quality", type=int, default=5, help="JPEG qscale, 2 (best) .. 31")
    ap.add_argument("--user", default="root")
    ap.add_argument(
        "--key", default=None, help="ssh deploy key (default deploy/secrets/deploy_key)"
    )
    a = ap.parse_args()
    w, h = (int(v) for v in a.size.lower().split("x"))
    try:
        camstream.run(
            a.host,
            source=a.source,
            width=w,
            height=h,
            fps=a.fps,
            quality=a.quality,
            user=a.user,
            key=a.key,
            progress=cli_progress(),
        )
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
