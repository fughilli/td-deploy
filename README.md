# td-deploy — TouchDesigner `.tox` → Raspberry Pi compiler

> Built on **`toxc`**, the TOX Compiler — the CLI/Bazel target (`//:toxc`) and MLIR dialect keep the `toxc` name.

Compile a TouchDesigner project into a native real-time media graph that runs on a
Raspberry Pi, with TouchDesigner removed from the deployment path. See the full
design in [`docs/design/tox-to-pi.md`](docs/design/tox-to-pi.md).

## Build with Bazel (the top-level driver)

Bazel drives the whole thing — the Python compiler, the Rust runtime, and the Pi
SD-image / live-deploy targets. Toolchains are hermetic (a pinned nixpkgs via
rules_nixpkgs for Mesa/LLVM; rules_python + rules_rust for the rest); `nix` is a
system requirement.

```sh
bazel build //...                        # build the whole graph
bazel test  //...                        # hermetic tests (pipeline + lock)
bazel query //...                        # see every target

# compile + render (in-container, no hardware):
bazel run //:toxc -- graphs/blur_demo.json --backend cpu --out /tmp/out
bazel run //:toxc -- graphs/blur_demo.json --emit-artifact /tmp/art --target gles2

# image an SD card for the Pi and play the compiled graph (see deploy/README.md):
bazel run //deploy:tdplayer_pi3.image_sd -- --device /dev/sdX     # Pi 3
bazel run //deploy:tdplayer.image_sd     -- --device /dev/sdX     # Pi 5
```

Key targets: `//ir` `//passes` `//lowering` `//runtime` (pipeline libs), `//:toxc`
(CLI), `//:expand` (`.toe`→IR, needs the Mac bridge), `//compiler:emit` +
`//compiler:build_exprs` (native lowering), `//runtime_rs:toxc_runtime` (Rust
runtime), `//deploy:toxc_artifact` + `//deploy:tdplayer{,_pi3}.*` (SD-image /
live-deploy). The `toxc` compiler name is unchanged; only the repo/project is
`td-deploy`.

## Status

- **Host bridge** (`hostbridge/`): zero-dependency HTTP server, run on the Mac, exposes
  `toeexpand`/`toecollapse` (and experimentally TD headless render) to the pipeline.
- **Downstream pipeline** (everything after import) is **working and validated** on the
  `image → GLSL gaussian blur → display` slice:
  `IR → passes(fold/infer/DCE) → lowering(GLSL codegen) → runtime`.
  Two runtime backends — real offscreen **OpenGL** (EGL-surfaceless / Mesa llvmpipe) and a
  **numpy reference** — agree to **1 LSB**.
- **Not yet**: the `.tox` importer (needs the host bridge + a real project), feedback/state,
  the MLIR compile backend, and the Pi/HDMI + sbc-deploy target.

## Layout

```
hostbridge/td_host_server.py   Mac-side HTTP bridge to toeexpand/TD
ir/graph.py                    typed operator-graph IR (JSON) — importer target
passes/optimize.py             fold / infer_format / dead-node-elim
lowering/shaders.py            GLSL + desktop-GL⇄GLES targeting shim
lowering/lower.py              IR -> RuntimePlan (ordered steps + baked GLSL)
runtime/egl_context.py         headless EGL surfaceless context (host seam; Pi swaps this)
runtime/backend_gl.py          GL backend (executes the plan's GLSL)
runtime/backend_cpu.py         numpy reference backend (conformance oracle)
runtime/run.py                 driver: load → optimize → lower → run → validate
graphs/blur_demo.json          the M1 sample graph
nix/dev.nix, nix/dev.sh        rootless Nix env (python+mesa) for the runtime
```

## Run the downstream slice (in-container, no hardware)

```sh
cd toxc
nix/dev.sh python3 -m runtime.run graphs/blur_demo.json --backend both --out out
# -> out/gl.png, out/cpu.png, out/diff8x.png  + a GL-vs-CPU LSB report
```

## Live realtime preview (stream to a browser window)

**Model: the container renders, the Mac views.** The GL render path is Linux-only
(EGL-surfaceless + Mesa) — the same path the Pi uses (V3D/GLES), so the container
preview is Pi-faithful. Rendering on macOS is intentionally unsupported (you'll get a
clear error, not a crash); view the stream instead.

Start (or restart) the stream **inside the container**:
```sh
tools/stream.sh /workspace/ascii_project.toe          # convenience: kills old, starts new
# equivalently: nix/dev.sh python3 -m cli <project.tox> --stream --port 8788 [--fps N --res N]
```
View on the **Mac** (nothing to run there):
```
http://toxc.$CLAUDE_SERVICE_INSTANCE.claude.localhost/     (fallback: ...:8484/)
```
It's MJPEG on a wall-clock timebase, so animated params (e.g. a Transform's
`rotate = absTime.seconds*10`) move live. Routes: `/` viewer, `/stream`, `/frame.jpg`,
`/stats`. `--res 256` trades resolution for fps. After you re-export a `.tox`, re-run
`tools/stream.sh <path>` (or just ask the agent to reload) — no reload endpoint yet.

Exposed via a claude-container named service
(`.claude-container-overlay/overlay.json` -> `{"services":{"toxc":8788}}`).

## Run the host bridge (on the Mac, where TouchDesigner is installed)

```sh
python3 hostbridge/td_host_server.py --port 8770
curl -s http://<mac-ip>:8770/health | python3 -m json.tool
curl -s --data-binary @project.tox \
  'http://<mac-ip>:8770/expand?name=project.tox&format=json' > expanded.json
```

## Notes

- Host reference GL is desktop core 3.3 via llvmpipe (surfaceless EGL); the `--target gles`
  lowering emits `#version 310 es` shaders for the Pi's V3D. `egl_context.py` is the only
  file that changes between host and Pi.
- The numpy backend is both a no-hardware validator and the conformance reference the design
  calls for (TD becomes a third oracle once the host bridge is wired).
