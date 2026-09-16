#!/usr/bin/env bash
# Launch td-deploy Studio in dev mode with live-reload.
#
#   app/dev.sh                 # or: bazel run //app:dev
#
# electronmon reloads the Electron side (renderer on save, restart on main.js /
# preload.js edits); TOXC_DEV makes main.js hot-restart the Python sidecar when
# sidecar.py / deploy_engine/ change. So you edit UI *or* engine with the window
# open and see it live. TOXC_PYTHON points the sidecar at the repo's Nix dev env
# (Pillow/PyAV/zstandard/certifi) — override it to use a different interpreter.
set -euo pipefail

# Works both as `bash app/dev.sh` and under `bazel run` (which copies srcs into
# runfiles, so $0's dir is not the source tree — use the workspace dir instead).
if [ -n "${BUILD_WORKSPACE_DIRECTORY:-}" ]; then
  repo="$BUILD_WORKSPACE_DIRECTORY"
else
  repo="$(cd "$(dirname "$0")/.." && pwd)"
fi

cd "$repo/app/electron"
# Install dev deps (incl. electronmon) only when missing.
[ -x node_modules/.bin/electronmon ] || npm install

elapp="node_modules/electron/dist/Electron.app"
elbin="$elapp/Contents/MacOS/Electron"
# The Electron binary can be MISSING even when the rest of node_modules is there:
# a failed postinstall download, or — on macOS — a prior Gatekeeper "malware"
# verdict that outright removed the binary. Re-fetch so electronmon can spawn it.
if [ ! -x "$elbin" ]; then
  echo "app/dev: Electron binary missing — reinstalling electron…" >&2
  rm -rf node_modules/electron && npm install
fi

# macOS Gatekeeper quarantines npm's prebuilt (unsigned) Electron.app, so it
# refuses to launch in dev with "…contains malware" (and can delete the binary).
# Strip the quarantine attr BEFORE launch — no quarantine means no Gatekeeper
# scan, so the fresh binary survives. (The *packaged* app is separately
# signed/notarized; this only touches the dev binary.) Idempotent + non-fatal.
if [ "$(uname -s)" = "Darwin" ] && [ -e "$elapp" ]; then
  xattr -dr com.apple.quarantine "$elapp" 2>/dev/null || true
fi

export TOXC_DEV=1
export TOXC_PYTHON="${TOXC_PYTHON:-$repo/nix/dev.sh}"
exec npm run dev
