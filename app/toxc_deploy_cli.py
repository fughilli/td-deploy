#!/usr/bin/env python3
"""CLI harness for the td-deploy engine (Phase-1 verification).

    python app/toxc_deploy_cli.py project.toe --pi tdplayer.local --target gles2 \
        [--set-file project1/moviefilein1=/path/Banana.tif] [--bridge host.docker.internal:8770]

Compiles the .toe, finishes it for aarch64 on THIS host, and live-updates the Pi.
In the dev container (no local toeexpand) pass --bridge to expand via the Mac host
bridge; on a machine with TouchDesigner it's found automatically.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from deploy_engine import cli_progress, deploy  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Compile a .toe and live-deploy it to the Pi")
    ap.add_argument("toe", help="project .toe/.tox (or a toxc IR .json)")
    ap.add_argument("--pi", required=True, help="Pi host (e.g. tdplayer.local)")
    ap.add_argument("--target", choices=["desktop_gl", "gles", "gles2"], default="gles2")
    ap.add_argument("--res", type=int, default=256)
    ap.add_argument("--set-file", action="append", default=[], metavar="NODE=PATH")
    ap.add_argument(
        "--bridge",
        default=os.environ.get("TOXC_HOST"),
        help="host bridge for toeexpand when TD isn't local (dev)",
    )
    ap.add_argument("--user", default="root")
    ap.add_argument(
        "--key", default=None, help="ssh deploy key (default deploy/secrets/deploy_key)"
    )
    ap.add_argument("--service", default="sbc-tdplayer")
    ap.add_argument("--keep-artifact", default=None, help="write the artifact here (keep it)")
    args = ap.parse_args()

    res = deploy(
        args.toe,
        args.pi,
        target=args.target,
        res=args.res,
        set_file=args.set_file,
        bridge=args.bridge,
        user=args.user,
        key=args.key,
        service=args.service,
        artifact_dir=args.keep_artifact,
        progress=cli_progress(),
    )
    print(f"[done] live on {args.pi}: {res['staging']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
