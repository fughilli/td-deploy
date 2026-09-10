# toxc compiler backend — native compilation (no Python in the hot path)

**Status:** Draft v0.1 (2026-09-05). Supersedes the "Python reference runtime is the
product" stance. See also `tox-to-pi.md`.

## Decisions (2026-09-05)

- **Native Pi runtime in Rust** — mirrors `player_rs` (static aarch64-linux-musl through
  the Bazel/Nix graph, EGL/GLES via FFI). No Python in the frame loop.
- **MLIR-first full compile** — the `tox` dialect + lowerings are the backend; the compiled
  artifact is what ships, not an interpreted plan.
- **Parameter expressions are transpiled** (expr → `arith`/`math` → LLVM) with a **Python-eval
  fallback** only for expressions the transpiler can't handle.
- **Toolchain gate CLEARED:** nixpkgs `nixos-24.11` `llvmPackages_18.{mlir,llvm}` is cached and
  out-of-tree-ready (mlir-tblgen, headers, `MLIRConfig.cmake` in `.dev`). The flakehub-weekly
  nixpkgs ships a broken MLIR patch (`mlir-tablegen-imported-target.patch` hunk#1) — pin 24.11
  for the compiler toolchain.

## The split (the core change)

Today one Python process both _builds_ and _executes_ the plan (an interpreter). Split into:

- **Compiler (host / build-time, Python + MLIR/LLVM C++):** `.tox` → import → `tox` IR →
  passes → **emit a serializable artifact**: op schedule, SPIR-V shader blobs, compiled CPU
  kernels (LLVM/NEON objects), compiled param-expr functions, I/O service manifest.
- **Runtime (Pi, native Rust):** loads the artifact and runs the per-frame loop with zero
  interpreter — native EGL/GLES, native video (ffmpeg/GStreamer), native OSC/MIDI, native
  scheduler, native/compiled param exprs.

The JSON IR stays the contract; the current Python renderer becomes the **host reference /
conformance oracle**, never shipped.

## Two wins, kept distinct

1. **Native runtime engine** → removes Python from the hot path (native dispatch + GL + IO).
2. **MLIR compilation of kernels/exprs** → removes dispatch overhead, enables fusion, targets
   NEON. TOP→`gpu`/`spirv`→SPIR-V; CPU (CHOP/SOP)→`linalg`/`vector`/`arith`→LLVM→NEON;
   exprs→`arith`/`math`→LLVM.

## The `tox` dialect (MLIR)

- Types: `!tox.top<WxH,fmt>` (texture), `!tox.chop<N,rate>`, `!tox.sop`, `!tox.dat`.
- Ops: sources (`tox.movie_in`, `tox.image_in`), pixel ops (`tox.gaussian`, `tox.transform`,
  `tox.crop`, `tox.glsl` carrying shader), `tox.feedback` (loop-carried state = the state
  vector), I/O (`tox.osc_in`), sinks (`tox.out_hdmi`).
- Feedback modeled as a `@cook(%state...) -> (%out, %state'...)` function (loop-carried args).
- Lowering pipeline: `tox` → fold/DCE/format-infer/place/fuse → {`spirv` for TOPs; `llvm` for
  CPU/exprs} → SPIR-V blobs + a native object + a schedule the Rust runtime executes.

## The compiled-artifact ABI (Rust runtime ⇄ compiler)

A directory/flatbuffer bundle:

- `schedule` — ordered steps: {kind, program/kernel ref, input/output buffer bindings,
  uniform specs, param-expr fn refs}.
- `shaders/*.spv` — SPIR-V (SPIRV-Cross → GLSL ES on the Pi if needed).
- `kernels.o`/`libkernels.a` — LLVM-compiled CPU kernels (NEON) + param-expr functions.
- `assets/` — LUTs, sprite sheets, still frames; movies referenced by path/manifest.
- `services.json` — OSC/MIDI manifest (ports/devices → CHOP names).

## Param-expression transpiler (+ fallback)

Build-time: parse each TD/Python expr → small AST → emit an `arith`/`math` MLIR function
(inputs: time, frame, CHOP channel reads). Unsupported constructs → mark the expr as
"interpreted" and the runtime evaluates it via an embedded CPython (or the host reference at
dev time). Goal: zero interpreted exprs for the common cases (`absTime.*`, `op('x')['ch']`,
arithmetic).

## Phased plan

- **M0 ✅ toolchain gate** — working MLIR/LLVM 18 (nixos-24.11) + rustc 1.82.
- **M0.5 ✅ dialect spine** (DONE) — out-of-tree `tox` dialect (TableGen + C++) → `toxc-opt` round-trips a
  `.mlir` with `tox` ops. _(in progress)_
- **M1 ✅ expr transpile** (DONE) — expr AST → `arith`/`math` → LLVM `.o`; Rust runtime calls it; Python
  fallback path. Verify against the reference evaluator.
- **M2 TOP lowering** — `tox` TOP ops → SPIR-V; Rust runtime (EGL/GLES) runs the ascii graph
  natively, diffed against the Python reference (conformance).
- **M3 CPU kernels** — CHOP/SOP → `linalg`/`vector` → LLVM/NEON.
- **M4 package + deploy** — static aarch64-musl Rust runtime + artifact via sbc-deploy; on-Pi.

## What stays Python

The compiler front/mid (importer, passes, codegen orchestration) and the reference runtime
(host preview + conformance). Never the Pi hot path.
