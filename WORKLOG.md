# toxc worklog

Newest first. See `docs/design/tox-to-pi.md` for the full design.

## 2026-09-10 (3) — Zero-copy GPU→HDMI (GBM scanout) + present 124ms→0.65ms

The dumb-buffer HDMI sink read the GPU's finished frame back to the CPU and copied
it into ~uncached scanout memory — ~111ms/frame (~4-7fps), while all GPU compute
was ~1.5ms. Replaced it with **zero-copy GBM scanout**: the VC4 renders straight
into a gbm surface's buffer objects, page-flipped on the CRTC — no readback, no
CPU copy.

- `runtime_rs/src/scanout.rs` — GBM: dlopen'd libgbm (no build-time link, like
  libEGL), `gbm_device` on card0 + a SCANOUT|RENDERING surface, EGL
  `EGL_PLATFORM_GBM` window surface, `flip()` = lock front bo → `add_framebuffer`
  (bo wrapped for the `drm` crate) → page-flip → vblank wait → release prev bo.
  Hotplug-aware: comes up headless at 1280x720 if no display and scans out when
  one appears. Linux-only + a stub elsewhere.
- `main.rs` — `render()` split into `cook()` (graph passes) + `readback()`; new
  `present_scanout()` GPU-blits the final texture into the surface (aspect-fit).
  `stream()` builds its own context (GBM scanout, else surfaceless + dumb-buffer
  sink); readback only when the sink or a web client needs pixels.
- Bring-up (deploy+profile on the Pi): `GBM_BACKENDS_PATH`/`LIBGL_DRIVERS_PATH` →
  mesa (the split `mesa-libgbm` loader can't find the vc4 backend otherwise);
  fixed the XR24 fourcc (0x34325258) + pick the EGL config by `NATIVE_VISUAL_ID`
  (else BAD_MATCH). apps.nix adds libgbm to `LD_LIBRARY_PATH`.
- **Measured on-device:** present 238→124 (dumb)→**0.65ms** (GBM); frame **2.3ms**
  headless (~431fps unthrottled) — with a live display it's vblank-locked ~60fps.
  Graph renders correctly through the GBM VC4 context (banana verified). **User to
  confirm the HDMI picture** when the display is on (it was off during bring-up;
  hotplug handles it) — incl. vertical orientation of the scanout blit.

**HDMI hotplug (no reboot).** Scanout owns its EGL state and reconfigures live:
`poll_hotplug()` force-probes while headless and, on connect, recreates the gbm +
EGL surface at the display's mode (`recreate_surface`) then modesets; while
connected a cheap state check detects disconnect → headless (keeps rendering).
Verified: plug HDMI in after boot and it comes up on its own.

**Transform TOP scale, now dynamic + correct.** `sx`/`sy` in `ascii_project.toe`
are TD expressions (`op('midiin1')[1]/64`) — but the lowering read scale as a
static float (and under the wrong names), so it never applied. Now every
transform component (rotate/translate/scale) lowers to a per-frame time-uniform
(per-component scalar shader uniforms `uScaleX/uScaleY/…`), evaluated from the
CHOP store each frame. Scale tracks knob 1 live (0 → scaled to nothing). Also
fixed an `emit_artifact` dedup bug that wrote `"inputs": null` for a repeated expr
(crashed the runtime's schedule parse). Confirmed on-device: banana rotates AND
scales from the Midi Fighter Twister.

**TOP fusion started (P3).** "Why 4 GPU passes — does the optimizer fuse them?" It
didn't. `lowering/_fuse_coord_remaps` now composes a Crop feeding only a Transform
into ONE coordinate-remap pass (source → crop-UV → transform-UV → one sample),
dropping the crop FBO — ascii went **4 shader passes → 3**, verified bit-faithful
on-device (banana identical) + `//:fusion_test`. The general `glsl2→glsl3`
(Sobel-into-ASCII) fusion needs shader-body inlining (documented, not done — it's
GPU headroom, not fps, since the frame is vblank-locked). Also: `cli.py` now sends
the host bridge's `X-Auth-Token` (the bridge requires a token now).



## 2026-09-10 (2) — VC4 GPU acceleration + fused-graph P0 (branch bazel-top-level)

**VC4 hardware GL.** The GLESv2 transpiler backend is now stood up in the deploy,
so the Pi 3 renders the graph on its VideoCore-IV GPU instead of llvmpipe:
- The ascii artifact is compiled `target=gles2` and its shaders are translated to
  GLSL ES 1.00 (`compiler/translate_gles.py`: desktop GLSL → glslang → SPIR-V →
  spirv-cross `--es --version 100` → `%` legalization) into `shaders_gles/`, baked
  into the prebuilt. All 8 shaders validated compiling under a real GLES2 context
  (`check_gles2`).
- `deploy/nix/apps.nix`: `softwareGL = false`. Surfaceless EGL binds the GPU's DRM
  render node (renderD128 = vc4) unless `LIBGL_ALWAYS_SOFTWARE=1` forces llvmpipe;
  the `video`/`render` groups were already granted.
- `make_gl` logs `[gl] target=… renderer=… (hardware|SOFTWARE)` so the journal
  shows VC4 vs llvmpipe at a glance. **User to verify on-device** (deploy, then
  `journalctl -u sbc-tdplayer | grep '\[gl\]'` + `/stats` for the fps lift). If it
  shows llvmpipe, the fallback is a GBM-platform EGL path on renderD128.

**Fused CHOP+TOP MLIR — P0 (one graph).** The `tox` dialect now represents the
CHOP DAG next to the TOPs (`docs/design/fused-chop-top-mlir.md` §7):
- New `!tox.chop<N>` type + ops `chop_source`/`chop_constant`/`chop_expr`/
  `chop_speed`/`chop_select`/`chop_sample` (`compiler/include/Tox/*.td`).
- `compiler/chop_to_tox.py` emits the DAG from the importer's `chops` JSON; it
  round-trips through `toxc-opt` (built via the pinned MLIR-18 nix shell).
- Structural parity with the runtime CHOP eval — no fusion yet. Hermetic test
  `//compiler:test_chop_to_tox`.

**Fused CHOP+TOP MLIR — P1 (fuse + compile).** `compiler/chop_lower.py` fuses the
whole CHOP DAG into ONE `arith`/`math` function `@chops(t, dt, frame, <sources>,
<speed-states-in>) -> (<outputs>)` — every channel is SSA, so only live sources,
the loop-carried Speed accumulators, and the outputs cross the ABI. It compiles
to a native `.so` (mlir-opt → mlir-translate → clang) and is **bit-parity
(|Δ|≤1e-9)** with `compiler/chop_ref.py` (a Python mirror of runtime_rs
`eval_chops`) over a multi-frame sequence with carried state — replacing per-node
fasteval interpretation. Gates: `//compiler:test_chop_lower` (compile+parity,
manual/network), `//compiler:test_chop_ref` (hermetic).

**And the runtime runs it.** A stable pointer ABI `void chops_v(const double* in,
double* out)` is emitted next to the scalar `@chops`, compiled in-image to
`chops/libchops.so` (`apps.nix`, same pipeline as `libexprs.so`) and `dlopen`ed;
`Renderer::eval_chops` builds `[t,dt,frame,<sources>,<states>]`, calls the kernel,
writes outputs to the store + carries the Speed accumulators. The fasteval loop is
now the fallback (unlowerable DAGs). `emit_artifact` emits `chops.mlir`+`chops_abi`
into the artifact. Verified in-container (aarch64): the emitted `chops.mlir`
compiles via the exact in-image pipeline and `chops_v` returns `[0.5,0.5]` for the
banana; runtime cross-builds; `bazel test //...` green. **User to verify on-device**
(banana still spins; `chop:eval` in `/stats` should drop toward ~0).

## 2026-09-10 — On-Pi runtime: DRM/KMS HDMI, CHOP engine, perf counters (branch bazel-top-level)

The whole td-deploy stack runs on a Pi 3 (tdplayer.local, deploy from the Mac via
`bazel run //deploy:tdplayer_pi3.deploy_live -- tdplayer.local`; the runtime is
cross-compiled to aarch64-linux-gnu via a nixpkgs cross-clang cc toolchain —
`//runtime_rs/cross`, NOT the host, else a macOS deploy ships a Mach-O).

Shipped this session (all on branch `bazel-top-level`, PR #1):
- **Native DRM/KMS HDMI sink** (`runtime_rs/src/sink.rs`) — double-buffered +
  vblank page-flip (tear-free), aspect-fit blit to the display, no video
  encoding. `drm` crate (Linux-only dep + no-op stub for other hosts). Needs
  `CAP_SYS_ADMIN` + `video`/`render` groups (set in `deploy/nix/apps.nix`).
- **Lazy MJPEG** — encode only while a web client is on `:8788`; DRM scans out
  every frame regardless.
- **CHOP-eval engine + expr interpreter** — the Twister→`constant1`→`speed1`
  (integrate)→`transform1.uRotate` chain. `expr.rs` (fasteval + a hand-rolled
  TD-syntax preprocessor for `op('N')[i]`/`absTime`, pre-compiled per-frame);
  `Renderer::eval_chops` runs the CHOP DAG (topo) into the store before the
  shader uniforms read it. MIDI stored RAW 0-127 + indexed keys
  (`op('midiin1')[i]`→i-th CC). Importer `_collect_chops` walks the CHOP DAG
  feeding any `op('X')` and emits it (`schedule.json["chops"]`, via `graph.chops`).
- **Per-node perf counters** (`GET /stats` JSON + ~5s journal log; `TOXC_PROFILE=1`
  for glFinish-accurate per-step GPU) — labels chop:eval / top:<node> / readback /
  encode / present / frame.
- **Design doc** `docs/design/fused-chop-top-mlir.md` — investigation of unifying
  CHOP+TOP in the tox MLIR dialect + cross-CPU/GPU fusion, profile-guided by /stats.

State / caveats: the banana renders + HDMI is tear-free on-device; the ascii
artifact is baked from `deploy/prebuilt/ascii` (bridge-compiled with
`--set-file project1/moviefilein1=…/Banana.tif`). **UNVERIFIED on hardware:** the
Twister actually spinning the banana (needs the physical MFT + encoders in
ABSOLUTE 0-127 mode) and the perf counters live — user to test. sbc-deploy vmnet
design = PR #17 (separate repo). MLIR CHOP+TOP fusion is a follow-up (design only).

## 2026-09-09 — Bazel is the top-level driver + SD-image targets (repo → td-deploy)

Made Bazel drive the whole project and added Raspberry Pi SD-image / live-deploy
targets. Repo/project renamed to **td-deploy** (pushed to `fughilli/td-deploy`);
the `toxc` compiler name is unchanged (module, `//:toxc`, MLIR dialect).

- **MODULE.bazel** rewritten: rules_python (hermetic 3.11 + `requirements.lock`),
  rules_rust (1.88; crate_universe from `runtime_rs/Cargo.lock`), rules_nixpkgs
  (pinned nixpkgs 25.05 for Mesa), and a `git_override` on `@sbc_deploy`. Needed
  `.bazelrc: common --experimental_isolated_extension_usages` (sbc_deploy uses it).
- **Python pipeline** as a library graph in the root BUILD (`//ir //importer
  //passes //lowering //runtime //runtime_expr`, all `imports=["."]` for the flat
  layout), `//:toxc` py_binary, `//:expand` (.toe→IR), `//compiler:emit` + the
  `build_exprs`/`build_shaders_gles` nix tools, and a hermetic `//:pipeline_test`.
- **Rust runtime** `//runtime_rs:toxc_runtime` (rust_binary). NB: it `dlopen`s
  libEGL/libexprs.so, so static-musl is impossible (`-ldl`) + pointless — it's a
  DYNAMIC aarch64 binary; the image autoPatchelfs it. (Bumped rustc 1.85→1.88 for
  the `image` crate's `as_chunks_mut`.)
- **Deploy**: `//deploy:toxc_artifact` (a rule running `//:toxc --emit-artifact`
  from a committed IR — hermetic) + `sbc_application` `tdplayer` (Pi 5) /
  `tdplayer_pi3` (Pi 3) → `.image_sd/.image_sd_base/.deploy_live/.ssh/.keys/.update`.
  `deploy/nix/{flake.nix,apps.nix,flake.lock}` consume the artifact + runtime via
  sbc-deploy `build_data`; apps.nix finishes the aarch64 `libexprs.so` in-image.
- **Verified in-container**: `bazel build //...` + `bazel test //...` green;
  `//:toxc` CPU render + emit-artifact run; Rust runtime builds (aarch64);
  `//deploy:*.image_sd` materialize with runfiles; flake locks/fetches.
- **NOT verified (needs hardware / Mac)**: full `nix` image realization + flashing
  a real SD, booting a Pi 3/5, on-device GL, the `.toe`-bridge expansion (TD), and
  the dynamic-runtime dlopen(libEGL) on NixOS (autoPatchelf path). See deploy/README.

## 2026-09-05 (3) — Realtime stream to a browser window + time/transform

**Live video path.** `bazel run //:toxc -- <project.tox> --stream` renders on a
wall-clock timebase and serves MJPEG; exposed to the Mac as a claude-container named
service (`overlay.json {"services":{"toxc":8788}}`) →
`http://toxc.$CLAUDE_SERVICE_INSTANCE.claude.localhost/` (instance = td-deploy). Verified
both halves (local curl + mux `OK 8788`); ~30 fps at 512×512 on llvmpipe.

**Dynamic path landed** (the per-frame state the design flagged): `runtime/expr.py` (safe
`absTime.seconds` evaluator), `transform` op implemented (rotate/translate/scale about
pivot) with `Step.time_uniforms` evaluated each frame → the ascii output visibly rotates
(`rotate = absTime.seconds*10`). `runtime/renderer.py` = persistent GL renderer (compile/
upload once, redraw per frame); `backend_gl.run` now delegates to it (one GL code path).
`runtime/stream_server.py` = dedicated render thread (owns GL context) + ThreadingHTTP
serving latest JPEG (multi-viewer safe).

Still stub: `crop` (passthrough). Non-animated sources uploaded once (no video decode yet).

## 2026-09-05 (2) — Importer + full `.tox`→pixels + `bazel run`

**Real TouchDesigner project runs natively, no TD in the loop.** `bazel run //:toxc
-- /workspace/ascii_project.toe` does the whole shebang: expand (host bridge
`toeexpand`) → import → optimize → lower → GL render → PNG. Both custom GLSL TOPs
(Sobel `glsl2`, ASCII sprite-lookup `glsl3`) execute on Mesa GL via a TD-compat shim.

**toeexpand format (reverse-engineered, see importer/samples/ascii_project):**
- `<op>.n`: line1 `Family:type`; `inputs { idx \t src }` = wiring; `flags … display on`
  marks the output TOP; `<op>.parm` = `name mode value…`.
- GLSL TOP `.parm` has `pixeldat <datname>`; the DAT's `<name>.text` holds the shader,
  framed as `2\n*` + big-endian int header + body (strip via the length field that runs
  to EOF).
- COMPs: `<comp>.network` `compinputs` block maps external input → internal In op.
  Importer flattens this (verified: `ascii/in1` → `project1/transform1`).
- toeexpand exits rc=1 on SUCCESS (prints "expanded into …" on stderr) — not an error.

**Built:** `importer/from_toeexpand.py` (tree→IR + COMP flatten + coverage report),
`cli.py` (end-to-end orchestrator incl. bridge expand + `/readfile` asset fetch),
`glsl_top` lowering/runtime (TD shim: `sTD2DInputs[]`, `uTD2DInfos[].res`, vec3 `vUV`,
`TDOutputSwizzle`), passthrough aliasing + output-readback runtime model. Bazel/Nix:
`.bazelversion` 8.8.0, `.bazelrc` (SVE fix), `MODULE.bazel` (rules_shell), `//:toxc`
sh_binary → `nix/dev.sh python3 -m cli`. Host bridge gained `/expand_local` + `/readfile`.

**Known gaps (next):** crop/transform are passthrough stubs (transform rotate is an
animated expr `absTime.seconds*10` → needs a time uniform, the per-frame dynamic path);
sprite-sheet asset needs the user to sync the updated bridge (has `/readfile`) for a
faithful `glsl3`; CPU reference has no glsl_top kernel (GL is the oracle for GLSL TOPs).
Toolchain is Nix-hermetic but wrapped by an sh_binary (not rules_nixpkgs build-hermetic).

## 2026-09-05 — Host bridge + downstream pipeline validated end-to-end

**Built:**
- `hostbridge/td_host_server.py` — zero-dep Mac HTTP bridge to `toeexpand`/`toecollapse`
  (+ experimental TD render). Endpoints `/health`, `/expand`, `/collapse`, `/render`.
  Robust to unknown toeexpand output layout (captures whatever it produces). Smoke-tested
  in-container (health/routing/auth/501-when-missing all green). **User runs it on the Mac.**
- Full **downstream pipeline** (everything after import), on the `image → GLSL gaussian blur
  → display` slice: `ir/graph.py` (typed IR + delay-edge/topo model) → `passes/optimize.py`
  (dead-node-elim, infer_format, constant_fold: sigma→radius+weights baked into GLSL) →
  `lowering/` (GLSL codegen + desktop-GL⇄GLES `#version` shim) → `runtime/` two backends.
- **Two runtime backends cross-validate**: real offscreen OpenGL (EGL-surfaceless / Mesa
  llvmpipe, GL 4.6 core) and a numpy reference. On blur_demo they agree to **max 1 LSB,
  99.35% pixels exact** — pure 8-bit rounding. Visual montage confirms the blur.

**Env solved (rootless, no sudo in this container):** GL via **Nix** (`nix/dev.sh` →
`nix/dev.nix`). Key finding: moderngl/glcontext EGL path fails ("0 devices" — llvmpipe not
enumerated as an EGL device); the working headless route is **PyOpenGL + EGL
`EGL_PLATFORM_SURFACELESS_MESA`** (no device/display needed). `osmesa` attr is gone in mesa 26.

**Next:** wire the importer once the host bridge is up on the Mac (expand a real `.tox`, map
nodes→IR, emit coverage report); then feedback/state; then MLIR compile backend; then the
Pi/HDMI sink + sbc-deploy. `--target gles` shaders are emitted but not yet run on a device.

## 2026-09-05 — Project kickoff, design doc drafted

**What:** Wrote the initial design for compiling TouchDesigner `.tox` projects to run natively on
a Raspberry Pi (TD removed from the deployment path).

**Key findings this session:**
- **TDXN is the wrong import path** — it needs a live TouchDesigner runtime via MCP tools and
  drops GLSL/feedback detail. Verified against the Embody/tdxn spec.
- **`toeexpand`** (TD-bundled CLI) is the real import path: a standalone file transform
  `.tox` → ASCII tree (node types, params, wiring, embedded GLSL/DAT text). `toecollapse`
  reverses it.
- The real cost center is **re-implementing a subset of TD's operator runtime**, not the
  importer. Only tractable via per-project coverage scoping + TD-as-oracle conformance testing.

**Decisions locked:** HDMI first sink · target-project-driven coverage · **MLIR from the start**
(custom `tox` dialect) · import via `toeexpand`.

**Next (M0, gating — do before designing further):**
1. Confirm `toeexpand` runs headless on Linux without a license/GPU; document its real output
   directory/file layout (couldn't find this in public docs).
2. Confirm EGL + GLES 3.1 offscreen render works on the target Pi.

Then M0.5: stand up the out-of-tree `tox` MLIR dialect skeleton against Nix-provided LLVM/MLIR,
one trivial op (`tox.level`) lowering to SPIR-V. Fallback if MLIR wiring drags: thin interpreter
behind the same IR so first-pixels isn't blocked.

**Not yet started:** no code, no Bazel/Nix wiring, not a git repo yet.
