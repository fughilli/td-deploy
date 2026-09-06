#!/usr/bin/env bash
# Translate an artifact's shaders to GLSL ES 1.00 (Pi3 / VideoCore-IV GLES2 HW).
# Produces <artifact>/shaders_gles/. Usage: compiler/build_shaders_gles.sh <artifact_dir>
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ART="$(cd "$1" && pwd)"
export PATH=/nix/var/nix/profiles/default/bin:$PATH
exec nix shell nixpkgs#glslang nixpkgs#spirv-cross nixpkgs#python3 \
  --command python3 "$here/translate_gles.py" "$ART"
