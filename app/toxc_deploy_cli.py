#!/usr/bin/env python3
"""CLI harness for the td-deploy engine (Phase-1 verification).

    python app/toxc_deploy_cli.py project.toe --pi tdplayer.local [--target auto] \
        [--set-file project1/moviefilein1=/path/Banana.tif] [--bridge host.docker.internal:8770] \
        [--skip-unsupported] [--magic-chop] \
        [--asset-root /media/library] [--asset-map Banana.tif=/path/other.png]

    python app/toxc_deploy_cli.py project.toe --pi showbox.local   # an x86_64 player, same flags

Asks the player what it is (ssh `uname -m`), compiles the .toe for it — GLSL ES +
aarch64 native code for a Pi; desktop GL + x86_64 native code for a mini PC, or a
Python-host artifact when the project runs Python — and live-updates it.
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
    ap = argparse.ArgumentParser(description="Compile a .toe and live-deploy it to a player")
    ap.add_argument("toe", help="project .toe/.tox (or a toxc IR .json)")
    ap.add_argument(
        "--pi", required=True, help="player host (e.g. tdplayer.local): a Pi or an x86_64 box"
    )
    ap.add_argument(
        "--target",
        choices=["auto", "desktop_gl", "gles", "gles2"],
        default="auto",
        help="GL target (auto: the detected player's own)",
    )
    ap.add_argument(
        "--arch",
        choices=["aarch64", "x86_64"],
        help="the player's architecture, skipping the ssh probe",
    )
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
    ap.add_argument(
        "--skip-unsupported",
        action="store_true",
        help="lenient 'deploy anyway': unsupported TOPs become placeholders + a warning, "
        "instead of failing the build",
    )
    ap.add_argument(
        "--magic-chop",
        action="store_true",
        help="drive unsupported CHOPs with random sinusoids so the piece still animates",
    )
    ap.add_argument(
        "--asset-root",
        action="append",
        default=[],
        metavar="DIR",
        help="extra folder to search for movie/image assets (repeatable)",
    )
    ap.add_argument(
        "--asset-map",
        action="append",
        default=[],
        metavar="ASSET=PATH",
        help="substitute a missing asset, keyed by its full path or bare basename " "(repeatable)",
    )
    args = ap.parse_args()

    asset_map = {}
    for spec in args.asset_map:
        key, sep, repl = spec.partition("=")
        if not sep:
            ap.error(f"--asset-map expects ASSET=PATH, got {spec!r}")
        asset_map[key] = repl

    res = deploy(
        args.toe,
        args.pi,
        target=args.target,
        arch=args.arch,
        res=args.res,
        set_file=args.set_file,
        bridge=args.bridge,
        user=args.user,
        key=args.key,
        service=args.service,
        artifact_dir=args.keep_artifact,
        strict_unsupported=not args.skip_unsupported,
        magic_chop=args.magic_chop,
        asset_roots=args.asset_root,
        asset_map=asset_map,
        progress=cli_progress(),
    )
    print(f"[done] live on {args.pi}: {res['staging']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
