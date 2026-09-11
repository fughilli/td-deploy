#!/usr/bin/env bash
# Assemble the host cross-toolchain bundle the app ships (app/toolchain/):
#
#   toolchain/
#     bin/{mlir-opt,mlir-translate,clang,clang++,ld.lld,glslangValidator,spirv-cross}
#
# No sysroot: finish cross-links the kernels with `-nostdlib` and leaves libm
# undefined (resolved on the Pi at dlopen) — see deploy_engine/toolchain.py.
#
# This is the OS-scriptable part (macOS/Linux). It expects the individual pieces
# to have been fetched/built into $STAGE by the caller (CI) and just lays them out
# with the right names + a sanity check. Windows uses assemble_toolchain.ps1.
#
#   STAGE=<dir with the fetched pieces> OUT=app/toolchain assemble_toolchain.sh
set -euo pipefail
STAGE="${STAGE:?set STAGE to the dir holding mlir/clang/glslang pieces}"
OUT="${OUT:?set OUT to the toolchain output dir (e.g. app/toolchain)}"

mkdir -p "$OUT/bin"

copy() {  # copy <src> <dst-name>   (resolves symlinks; keeps exec bit)
  local src="$1" dst="$2"
  [ -e "$src" ] || { echo "MISSING: $src" >&2; return 1; }
  cp -L "$src" "$OUT/bin/$dst"
  chmod +x "$OUT/bin/$dst"
}

# MLIR tools (built from compiler/nix on macOS/Linux; from source on Windows).
copy "$STAGE/mlir/mlir-opt"        mlir-opt
copy "$STAGE/mlir/mlir-translate"  mlir-translate

# clang + lld (from the official LLVM release for this OS).
copy "$STAGE/llvm/clang"    clang
copy "$STAGE/llvm/clang++"  clang++ || true
copy "$STAGE/llvm/ld.lld"   ld.lld

# GLES shader translators. translate_gles.py invokes `glslangValidator` +
# `spirv-cross`, so the bundled names must match exactly.
copy "$STAGE/glslang/glslangValidator" glslangValidator
copy "$STAGE/spirv-cross/spirv-cross"  spirv-cross

echo "==> toolchain assembled at $OUT"
ls -l "$OUT/bin"
