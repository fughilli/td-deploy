#!/usr/bin/env bash
# Build the td-deploy Studio desktop app end-to-end on the host (macOS/Linux).
# Windows uses build_app.ps1. CI calls this after populating app/toolchain/.
#
#   app/packaging/build_app.sh
#
# Steps: freeze the sidecar (PyInstaller) -> stage into app/dist -> electron-builder.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
app="$(cd "$here/.." && pwd)"
repo="$(cd "$app/.." && pwd)"

echo "==> stamp base image tag"
printf '{"base_image_tag": "%s"}\n' "${BASE_IMAGE_TAG:-latest}" > "$app/version.json"

echo "==> python deps"
python3 -m pip install -r "$here/requirements.txt"

echo "==> freeze sidecar (PyInstaller)"
cd "$repo"
python3 -m PyInstaller --noconfirm --distpath "$app/dist" --workpath "$app/build" \
  "$here/sidecar.spec"

if [ ! -d "$app/toolchain" ]; then
  echo "WARNING: app/toolchain/ missing — the frozen app will fall back to Nix." >&2
  echo "         CI's build-toolchain job should populate it before packaging." >&2
fi

echo "==> electron-builder"
cd "$app/electron"
npm install
npm run dist

echo "==> artifacts in $app/electron/dist"
ls -1 "$app/electron/dist" || true
