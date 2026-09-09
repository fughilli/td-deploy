# toxc worklog

Newest first. See `docs/design/tox-to-pi.md` for the full design.

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
