"""Finish an OPTIMIZED artifact into a Pi-ready one, ON THE HOST: compile the
transpiled MLIR to aarch64 shared libs and translate the shaders to GLSL ES.

  exprs.mlir -> exprs/libexprs.so    (compiled param-expr kernels)
  chops.mlir -> chops/libchops.so    (fused CHOP-DAG kernel)
  shaders/*  -> shaders_gles/*       (GLSL ES 1.00, for gles2/VC4)

Same command sequence as compiler/build_exprs.sh + the in-image runCommand, but
driven by a Toolchain (native in dev, aarch64-cross in the shipped app).
"""
from __future__ import annotations

import os

from .progress import Progress
from .toolchain import Toolchain

_MLIR_LOWER = ["--convert-math-to-llvm", "--convert-arith-to-llvm",
               "--convert-func-to-llvm", "--reconcile-unrealized-casts"]


def _build_so(tc: Toolchain, art: str, name: str, progress: Progress) -> bool:
    """`<name>.mlir` -> `<name>/lib<name>.so`. Returns True if built."""
    mlir = os.path.join(art, f"{name}.mlir")
    if not (os.path.isfile(mlir) and os.path.getsize(mlir) > 0):
        return False
    out = os.path.join(art, name)
    os.makedirs(out, exist_ok=True)
    low, ll, so = (os.path.join(out, f) for f in ("low.mlir", f"{name}.ll", f"lib{name}.so"))
    progress.log(f"codegen {name}.mlir -> {name}/lib{name}.so")
    tc.run_pipeline([
        ["mlir-opt", mlir, *_MLIR_LOWER, "-o", low],
        ["mlir-translate", low, "--mlir-to-llvmir", "-o", ll],
        ["clang", *tc.clang_flags, "-O2", "-shared", "-fPIC", ll, "-o", so, "-lm"],
    ])
    return True


def finish(art_dir: str, target: str, tc: Toolchain, progress: Progress = Progress()) -> None:
    """Codegen the .so's + (for gles2) translate the shaders, in place."""
    progress.phase("finish", 0.0, "aarch64 codegen")
    built = []
    if _build_so(tc, art_dir, "exprs", progress):
        built.append("libexprs.so")
    progress.phase("finish", 0.4, "chop kernel")
    if _build_so(tc, art_dir, "chops", progress):
        built.append("libchops.so")
    if target == "gles2":
        progress.phase("finish", 0.7, "GLSL ES translate")
        progress.log("glslang + spirv-cross -> shaders_gles/")
        tc.run_gles(art_dir)
        built.append("shaders_gles/")
    progress.phase("finish", 1.0, "built " + (", ".join(built) or "nothing"))
