#!/usr/bin/env bash
# Compile an artifact's exprs.mlir -> exprs/libexprs.so (native param-expr fns).
# Usage: compiler/build_exprs.sh <artifact_dir>
set -euo pipefail
ART="$(cd "$1" && pwd)"
here="$(cd "$(dirname "$0")" && pwd)"
if [ ! -s "$ART/exprs.mlir" ]; then
  echo "[build_exprs] no transpiled exprs; nothing to build"; exit 0
fi
mkdir -p "$ART/exprs"
exec "$here/nix/shell.sh" bash -c "
  set -e
  mlir-opt '$ART/exprs.mlir' --convert-math-to-llvm --convert-arith-to-llvm \
    --convert-func-to-llvm --reconcile-unrealized-casts -o '$ART/exprs/low.mlir'
  mlir-translate '$ART/exprs/low.mlir' --mlir-to-llvmir -o '$ART/exprs/exprs.ll'
  clang -O2 -shared -fPIC '$ART/exprs/exprs.ll' -o '$ART/exprs/libexprs.so' -lm
  echo '[build_exprs] wrote $ART/exprs/libexprs.so'
"
