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

export TOXC_DEV=1
export TOXC_PYTHON="${TOXC_PYTHON:-$repo/nix/dev.sh}"
exec npm run dev
