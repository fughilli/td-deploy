# Fused CHOP+TOP optimization across the CPU/GPU boundary (investigation)

**Status:** Investigation / design (2026-09-10). Extends
[`compiler-backend.md`](compiler-backend.md) (the `tox` dialect + lowering) with a
concrete plan for optimizing *across* the CHOP↔TOP (CPU↔GPU) boundary, and ties
it to the on-target performance counters (`/stats`).

## 1. The problem

TouchDesigner networks mix two rates/locations:

- **CHOPs** — control-rate, small (scalars/channels), naturally **CPU**.
- **TOPs** — image-rate, large (2D textures), naturally **GPU**.

Data crosses the boundary in **both directions**, often repeatedly in one graph:

- **CHOP → TOP** — a CHOP value drives a TOP parameter (our banana:
  `midiin1 → constant1 → speed1 → transform1.uRotate`). Today that's a shader
  *uniform* written each frame.
- **TOP → CHOP** — an *analysis* CHOP reads a reduced TOP (average luminance,
  histogram, a picked pixel) → feeds it back into control logic.
- **CHOP ↔ TOP round-trips** — the two above chained (analyze a TOP → compute →
  drive another TOP), sometimes several hops.

**Today these are two disjoint engines** glued by uniform writes and (for
analysis) CPU readbacks:

- CHOPs: evaluated at runtime by **fasteval** (`runtime_rs/src/expr.rs`) + the
  hand-rolled per-frame CHOP eval (`Renderer::eval_chops`).
- TOPs: **GLSL** shaders run by the GL runtime.

Because fasteval is a **runtime interpreter**, the CHOP computation is *opaque to
the compiler*. Nothing can be optimized across the boundary: a constant CHOP
can't be folded into a shader, a CHOP→TOP→CHOP round-trip can't be collapsed, and
adjacent TOP passes can't be fused with the control logic that feeds them. **This
is the ceiling the current architecture hits, and why we want one IR.**

## 2. Why MLIR

Put **both CHOPs and TOPs in one IR** — the existing `tox` dialect — so the
compiler sees the whole computation and can transform across the boundary
*before* deciding what runs where. MLIR is built for exactly this: multiple
dialects (control/CPU: `arith`/`math`/`linalg`/`vector`; image/GPU:
`gpu`/`spirv`/`linalg-on-tensors`) coexisting and being progressively lowered,
with a `gpu` dialect that models the **host/device split explicitly**
(`gpu.launch`, `gpu.module`, device buffers, host↔device copies). IREE is the
existence proof that a single graph can be compiled to a **fused CPU+GPU**
program with the transfers optimized. LLVM underneath gives us NEON codegen for
the CPU side and mature scalar optimization for the fused control math.

The `tox` dialect already has the shape we need (see `compiler/include/Tox`):
`!tox.top<WxH,fmt>`, planned `!tox.chop<N,rate>`, source/level/glsl/transform
ops. The move is to **also represent the CHOP DAG as `tox` ops** (not fasteval),
then run cross-boundary passes.

## 3. The optimizations this unlocks

1. **CHOP→TOP uniform specialization / constant folding.** A CHOP subgraph
   feeding a TOP uniform: if it folds to a constant (or a cheap function of
   `absTime`), *inline it into the shader* (specialize the SPIR-V), removing a
   uniform write and enabling downstream shader constant-folding. If it's
   genuinely dynamic (MIDI-driven), still **fuse the whole CHOP chain into one
   compiled CPU function** (constant→speed→math → a single `arith`/`math` kernel)
   instead of per-node interpretation.
2. **TOP→TOP pass fusion.** Our ascii chain is 4 passes
   (`crop→transform→glsl2→glsl3`), each its own framebuffer. Producer/consumer
   fusion (linalg-style on the image ops, or shader concatenation where the
   sampling pattern is point-wise) cuts render targets, bandwidth, and
   intermediate allocations.
3. **TOP→CHOP reduction lowering.** An analysis CHOP that averages/reduces a TOP
   is, today, a **full CPU readback + CPU reduce**. Lower it to a **GPU reduction**
   (mip chain / compute) fused into the producing pass, so only the scalar
   crosses back — turning a whole-frame transfer into a few bytes.
4. **CHOP↔TOP transfer minimization.** Model every boundary crossing as an
   explicit copy op and run copy-elimination + scheduling (bufferization) to
   remove redundant round-trips and keep data on the side that needs it next.
5. **Control-math fusion + NEON.** The fused CHOP kernels compile through LLVM →
   NEON, and constant subexpressions across the (formerly per-node) CHOP graph
   fold once.

## 4. Architecture

```
 .tox → import → tox IR (CHOPs + TOPs, one graph)
        │
        ├─ canonicalize / fold / DCE / format-infer         (whole graph)
        ├─ COST-MODELLED PLACEMENT  (CPU vs GPU per op)  ◄── /stats profile (§5)
        ├─ CROSS-BOUNDARY FUSION:
        │    • CHOP→TOP uniform inline / shader specialize
        │    • TOP→TOP producer/consumer fusion
        │    • TOP→CHOP reduction lowering (readback → GPU reduce)
        │    • transfer (copy) elimination / scheduling
        │
        ├─ PARTITION + LOWER:
        │    • CPU side  (CHOP/expr/reduce) → arith/math/linalg/vector → LLVM → .o (NEON)
        │    • GPU side  (TOP)              → gpu/spirv → SPIR-V → (SPIRV-Cross) GLSL-ES
        │
        └─ emit fused schedule + kernels.o + shaders + explicit copies
                         │
              Rust runtime executes the schedule (no interpreter)
```

Key idea: **placement + fusion happen on the unified graph, before the CPU/GPU
split.** The split is an *output* of the cost model, not a fixed rule ("CHOP =
CPU, TOP = GPU"). On a real GPU (Pi 5 V3D) the boundary is physical (bus
transfers matter); on **llvmpipe (Pi 3)** GPU==CPU, so the boundary is mostly
*dispatch + readback overhead* — fusion still wins (fewer passes, fewer
readbacks, one compiled kernel), it just weights the cost model differently.

## 5. Profile-guided optimization (why the perf counters exist)

The placement/fusion decisions need a **cost model**, and the best cost data is
*measured on the target*. The per-node counters just added
(`runtime_rs/src/main.rs`, `GET /stats`) give exactly that: `chop:eval`,
`top:<node>` (per pass, `TOXC_PROFILE=1` for glFinish-accurate GPU time),
`readback`, `encode`, `present`, `frame`.

The PGO loop:

```
compile (cold cost model) → deploy → collect /stats on the real board
   → re-run PLACEMENT + FUSION with measured per-node costs
   → recompile → redeploy   (repeat until the frame budget is met)
```

Concrete uses of the profile:
- A `top:<node>` that dominates → candidate for fusion with its neighbour or a
  cheaper lowering (e.g. separable blur, mip reduction).
- A large `readback` relative to `frame` → a TOP→CHOP analysis that should be a
  GPU reduction (opt #3), not a full transfer.
- `chop:eval` non-trivial → fuse the CHOP DAG into one compiled kernel (opt #1).
- The measured costs become edge/node weights the placement pass minimizes
  (min-cut style: partition the graph to minimize `Σ compute + Σ boundary
  transfer`).

## 6. Relationship to fasteval

fasteval is **not** the long-term CHOP engine for the fused path — it's a runtime
interpreter, opaque to the compiler. The plan **keeps fasteval as the dev/fallback
interpreter** (fast to iterate, handles exprs the transpiler can't lower) but, for
release builds, **lowers the CHOP DAG into the `tox`/MLIR graph** so it's
optimized and compiled alongside the TOPs. The M1 param-expr transpiler
(`compiler/expr_transpile.py`, expr → `arith`/`math` → LLVM) is the seed; extend
it from single exprs to the whole CHOP DAG (constant/speed/math/… as `tox` ops).

## 7. Phased plan

- **P0 — one graph. ✅ landed (2026-09-10).** The `tox` dialect now carries the
  CHOP DAG next to the TOPs: a parametric `!tox.chop<N>` type and the ops
  `tox.chop_source` (live MIDI/OSC in), `tox.chop_constant` (literal),
  `tox.chop_expr` (channels = TD expressions over input CHOPs — the general
  control-math node, carried verbatim for parity), `tox.chop_speed` (integrator),
  `tox.chop_select` (Null/Select passthrough) and `tox.chop_sample` (the
  CHOP→uniform scalar edge). `compiler/chop_to_tox.py` emits the DAG from the
  importer's `chops` JSON; it round-trips through `toxc-opt`
  (`compiler/nix/shell.sh compiler/build/tools/toxc-opt/toxc-opt <(python3
  compiler/chop_to_tox.py deploy/prebuilt/ascii/schedule.json)`). Hermetic test:
  `//compiler:test_chop_to_tox`. No optimization yet — structural parity with the
  runtime CHOP eval.
- **P1 — CHOP fusion + compile.** Fold/DCE the CHOP subgraph; lower to one
  `arith`/`math`/`linalg` CPU kernel per uniform (via the M1 transpiler path),
  replacing per-node interpretation. Verify bit-parity vs the fasteval path.
- **P2 — CHOP→TOP specialization.** Constant-fold CHOP→uniform into shaders;
  keep dynamic ones as fused-kernel-computed uniforms.
- **P3 — TOP fusion + placement (profile-guided).** Producer/consumer TOP
  fusion; cost-modelled placement seeded by `/stats`; the PGO loop of §5.
- **P4 — TOP→CHOP reductions + transfer elimination.** Lower analysis CHOPs to
  GPU reductions; bufferize + eliminate redundant CHOP↔TOP copies.

## 8. Risks / reality check

- **Scope.** Full IREE-style whole-graph CPU+GPU compilation is a large
  undertaking. The plan is **incremental**: each Pn is independently useful
  (P1 alone removes the interpreter from the CHOP path; P2 specializes shaders;
  P3 fuses passes). Don't gate early wins on the whole thing.
- **llvmpipe vs V3D.** On the Pi 3 (llvmpipe) the CPU/GPU split is logical, not
  physical — fusion still helps (dispatch/readback/allocation), and the cost
  model naturally reflects it. The physical-transfer wins (opt #3/#4) matter most
  on a real GPU (Pi 5 V3D, or desktop).
- **SPIR-V → GLSL-ES.** VC4 is GLES2-only; the SPIR-V→GLSL-ES path already exists
  (`compiler/translate_gles.py`, glslang + SPIRV-Cross). The fused GPU side plugs
  into it.
- **Feedback/state.** The `tox.feedback` loop-carried model (`@cook(%state) ->
  (%out, %state')`) already covers integrators like the Speed CHOP; the CHOP
  integrator (our `speed1`) is the CPU-side instance of the same pattern.
- **Correctness gate.** The Python reference runtime stays the conformance oracle
  at every phase (bit-diff the fused output vs the reference), same as M2.
