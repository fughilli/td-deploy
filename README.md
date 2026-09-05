# toxc — TouchDesigner `.tox` → Raspberry Pi compiler

Compile a TouchDesigner project into a native real-time media graph that runs on a
Raspberry Pi, with TouchDesigner removed from the deployment path. See the full
design in [`docs/design/tox-to-pi.md`](docs/design/tox-to-pi.md).

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

```sh
bazel run //:toxc -- /workspace/ascii_project.toe --stream --port 8788 --fps 30
# or:  nix/dev.sh python3 -m cli <project.tox> --stream
```
Serves an MJPEG stream on a wall-clock timebase (animated params like a Transform's
`rotate = absTime.seconds*10` move live). In claude-container it's exposed as a named
service (`.claude-container-overlay/overlay.json` -> `{"services":{"toxc":8788}}`); open
`http://toxc.$CLAUDE_SERVICE_INSTANCE.claude.localhost/` on the host. Routes: `/` viewer,
`/stream` MJPEG, `/frame.jpg`, `/stats`. Use `--res 256` to trade resolution for fps.

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
