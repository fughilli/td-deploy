# toxc — Compiling TouchDesigner `.tox` projects to Raspberry Pi

**Status:** Draft v0.1 (2026-09-05)
**Goal:** Take a TouchDesigner `.tox`, compile away TouchDesigner entirely, and run the
resulting real-time media graph natively on a Raspberry Pi — deployed and iterated via
[`sbc-deploy`](https://github.com/fughilli/sbc-deploy).

---

## 1. Thesis

TouchDesigner (TD) is a proprietary, GPU-heavy, x86, licensed runtime. Running it on a Pi is a
non-starter. Instead we **extract the computational graph and re-implement the operator
semantics natively.** TD is removed from the *deployment* path and kept only as:

1. a one-shot **import tool** (`toeexpand`, a bundled CLI file-transform — not the GUI/GPU
   runtime), and
2. a **test oracle** for conformance (render in TD, render on Pi, diff).

The importer is ~20% of the work. The other 80% is re-implementing a *subset* of TD's operator
runtime. This is only tractable if operator coverage is **scoped to a concrete target project**
and grown outward, guided by a per-import coverage report.

## 2. Key decisions (2026-09-05)

| Decision | Choice | Notes |
|---|---|---|
| First output sink | **HDMI display** (DRM/KMS) | Simplest to validate visually; most general. LED/NDI sinks later as pluggable output ops. |
| Operator coverage | **Target-project-driven** | Implement exactly what one real `.tox` needs; grow from there. |
| IR substrate | **MLIR from the start** | Custom `tox` dialect. Bigger upfront cost; cleaner long-term optimization/lowering. See §7 risk. |
| Import path | **`toeexpand`**, *not* TDXN | TDXN requires a live TD runtime via MCP and drops GLSL/feedback detail. `toeexpand` is a standalone file transform. |

### Rejected: TDXN for import
TDXN is a YAML projection of a network, but conversion runs through **MCP tools against a live
TouchDesigner runtime** (`read_tdn`/`export_network`/`import_network`), stores only non-default
params, and doesn't detail GLSL or feedback. It's for AI-editing a running network, not headless
extraction. `toeexpand` expands `.toe`/`.tox` into a tree of ASCII files (node types, params,
wiring, embedded GLSL/DAT text) and `toecollapse` reverses it — a pure file transform, no
GUI/GPU.

## 3. Pipeline

```
.tox ──toeexpand──▶ expanded tree ──[Importer]──▶ tox IR (MLIR)
                                                      │
                              ┌───────────────────────┴──────────────────────┐
                              │  topology: typed DAG                          │
                              │  feedback: delay edges → loop-carried state   │
                              │  ops: type + params + embedded code           │
                              └───────────────────────┬──────────────────────┘
                                                      ▼
                                          [Optimization passes]
                        fold static params · DCE · format/res inference ·
                        CPU⇄GPU placement · adjacent-TOP shader fusion
                                                      ▼
                                          [Lowering]
                    TOPs → gpu/spirv → SPIR-V (SPIRV-Cross → GLSL ES)
                    CHOP/SOP → linalg/vector → LLVM → NEON (AOT .so)
                                                      ▼
                            ┌──────── Pi Runtime (native C++) ────────┐
                            │ per-frame scheduler + state buffers     │
                            │ TOP kernels → EGL/GLES 3.1 on V3D       │
                            │ CHOP/SOP kernels → CPU/NEON             │
                            │ I/O services (OSC/MIDI/NDI/…)           │
                            │ output sinks (HDMI now; LED/NDI later)  │
                            └───────────────────┬─────────────────────┘
                                                ▼
                                    sbc-deploy live-deploy push
```

## 4. The `tox` IR (MLIR dialect)

The IR model is the foundation — get it right first.

### Types (one per operator family)
- `!tox.top<WxH, format>` — 2D texture (raster). Carries resolution + pixel format.
- `!tox.chop<Nchan, Nsamp, rate>` — channel/sample streams.
- `!tox.sop` — geometry (opaque handle initially; structured later).
- `!tox.dat` — table/text.

Cross-family conversions (e.g. TOP→CHOP) are **explicit ops**, never implicit edges. Edges are
type-checked.

### Feedback = loop-carried state (the "state vector")
The per-frame network is a pure DAG plus a set of **delay edges** (Feedback TOP, CHOP feedback,
1-frame cook cycles). Model the whole frame as a function:

```mlir
func.func @cook(%state0: !tox.top<...>, ...) -> (%out, %state0': !tox.top<...>, ...)
```

Delay edges become **loop-carried values**: this frame reads `%state`, produces `%state'` for
next frame. The buffers on those edges *are* the state vector — allocated once, ping-ponged each
frame. This is the synchronous-dataflow-with-delays model (cf. Faust); it makes feedback
first-class and analyzable rather than a runtime hack.

### Representative ops
- Concrete kernels: `tox.noise`, `tox.transform`, `tox.level`, `tox.composite`, …
- Escape hatch: `tox.glsl_top` carrying custom shader source as an attribute.
- Delay: `tox.feedback` (lowers to loop-carried arg).
- I/O (phase 2): `tox.osc_in`, `tox.osc_out`, `tox.midi_in`, `tox.ndi_out`, …
- Sinks: `tox.out_hdmi` (M1), `tox.out_led`, `tox.out_ndi`.

## 5. Passes

**Optimization** (on `tox`): `tox-fold-params` (static params → constants), DCE,
`tox-infer-format` (resolution/format propagation), `tox-place` (CPU/GPU assignment minimizing
cross-bus copies), `tox-fuse` (chain of pixel TOPs → single shader).

**Lowering:**
- TOPs → `gpu` + `spirv` → SPIR-V. Runtime consumes SPIR-V, or SPIRV-Cross → GLSL ES 3.1.
- Custom GLSL TOPs: TD desktop-GLSL → **glslang → SPIR-V → SPIRV-Cross → GLSL ES**, plus shims
  for TD's built-in uniforms (`sTD*`).
- CHOP/SOP → `linalg`/`vector`/`arith` → LLVM → NEON, AOT-compiled to a `.so` the runtime loads.
- Host schedule emitted as a runtime graph the C++ scheduler drives.

## 6. Pi runtime

- **GL context:** EGL surfaceless + GLES 3.1 on Mesa/V3D (Pi 4/5), offscreen FBOs for TOP chains.
- **Scheduler:** per frame — read delay-edge state buffers, evaluate DAG in topo order, write
  outputs, swap state.
- **Sinks are pluggable output ops.** M1 = HDMI via DRM/KMS. Later: sample-to-LED (reuses the
  LED-mapper / WS281x / player_rs stack), NDI out.
- **I/O as graph-declared services (phase 2):** an I/O operator's presence *declares* a runtime
  service. OSC In → UDP listener on its port; MIDI In/Out → ALSA/USB-MIDI matched by device
  name; NDI → NDI SDK (ARM builds); WebRTC → GStreamer/libwebrtc. The importer emits a **service
  manifest** (ports/devices); the runtime's service manager instantiates and wires them to the
  graph's channel ports. Each compiled project is self-describing about its external interface.

## 7. Conformance harness (the quality mechanism)

For each op: render a parameter sweep in real TD, render the same on the Pi kernel, compare
within tolerance. This is the *only* safe way to grow operator coverage, and the legitimate
reason TD stays "in the loop" — as an oracle, not a runtime dependency. Build it at M2.

## 8. Deployment (sbc-deploy)

Compiled artifact = native runtime binary + IR/lowered blob + assets (SPIR-V shaders, CPU-kernel
`.so`, LUTs, geometry) + service manifest. Image the SD once as an `sbc_application` variant;
iterate via the **live-deploy** flow. Fits the existing Bazel+Nix graph.

## 9. Roadmap

- **M0 — Verify-first (gating).** (a) `toeexpand` runs headless on Linux w/o license/GPU;
  document actual output layout. (b) EGL+GLES 3.1 offscreen render works on the target Pi.
  *Nothing downstream is designed until both are green.*
- **M0.5 — MLIR skeleton.** Out-of-tree `tox` dialect (TableGen types+ops) building against
  Nix-provided LLVM/MLIR; one trivial op lowering end-to-end (`tox.level` → SPIR-V). Proves the
  toolchain before the full slice. *Fallback if this drags: a thin interpreter behind the same
  IR so first-pixels isn't blocked on MLIR.*
- **M1 — Vertical slice.** `Noise → Transform → Level → Out(HDMI)`, no feedback, no Python. Full
  spine: importer → IR → ~4 TOP kernels → lower → runtime → sbc-deploy live push. Goal: pixels
  on the Pi from a `.tox`.
- **M2 — Feedback + conformance.** Delay edges / loop-carried state / ping-pong buffers +
  Feedback TOP. Stand up the differential test harness vs TD.
- **M3 — Widen TOPs + optimizer v1.** More TOP kernels, custom-GLSL translation, fold/DCE/
  format-inference/fusion. Coverage driven by one real target project.
- **M4 — CHOPs + I/O services.** CHOP kernels, then OSC + MIDI as declared services ("plug the
  controller into the Pi and it just works"). NDI/WebRTC after.
- **M5 — SOP/3D.** Geometry + Render path. Gated on real need.

## 10. Risks / open questions

1. **`toeexpand` licensing/headless** on Linux CI + Pi build host (M0 blocker).
2. **MLIR-from-start cost** — out-of-tree dialect + Bazel/Nix LLVM wiring delays first-pixels;
   M0.5 + interpreter fallback mitigate.
3. **Embedded binary data** — do stored TOP contents / movie caches / presets survive expansion
   as usable data, or only references?
4. **Operator long tail** — no "support everything"; per-project scoping + coverage report only.
5. **GLSL desktop→ES gaps** — compute-shader limits on older Pis; TD built-in uniforms.
6. **Python DATs** — arbitrary Python is out of scope. Decide the declarative subset (param
   expressions, CHOP references) to evaluate natively.
7. **Pi perf** — targets projects that *fit* the budget; TOP-heavy 2D is the sweet spot.

## 11. Proposed repo layout

```
toxc/
  docs/design/tox-to-pi.md   # this doc
  WORKLOG.md
  importer/                  # toeexpand wrapper -> tox IR
  ir/                        # tox MLIR dialect (TableGen + C++)
  passes/                    # optimization + lowering passes
  runtime/                   # Pi C++ runtime (EGL/GLES + CPU kernels)
  conformance/               # differential test harness vs TD
  deploy/                    # sbc-deploy integration
  MODULE.bazel
```
