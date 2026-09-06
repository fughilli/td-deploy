# toxc deployment (sbc-deploy → Raspberry Pi)

End-to-end: a TouchDesigner `.tox` → compiled artifact → native Rust runtime on a Pi.

## Build the artifact (host / container)
```sh
# expand + import + optimize + lower + emit the artifact (assets fetched via the bridge)
nix/dev.sh python3 -m cli <project.tox> --res 256 --emit-artifact ./my_artifact \
    [--set-file NODE=/host/path/asset]
# compile the transpiled parameter expressions to native code
compiler/build_exprs.sh ./my_artifact
```
Artifact = `schedule.json` + `shaders/` + `assets/` + `exprs/libexprs.so` + `services.json`.

## Deploy (sbc-deploy Pi3)
`sbc-deploy` is a separate repo (github.com/fughilli/sbc-deploy). In the Pi3
`sbc_application` NixOS config:
```nix
imports = [ /path/to/toxc/deploy/toxc-service.nix ];
services.toxc = {
  enable = true;
  artifact = ./my_artifact;   # copied into the Nix store / image
  port = 8788;
  fps = 30;
  # softwareGL = true;  # default; required on Pi3 (see below)
};
```
Then image once and use sbc-deploy's live-deploy flow to update. `runtime_rs/default.nix`
is a `buildRustPackage`, so nix builds the aarch64 closure (incl. Mesa) — no musl needed.
View the live output at `http://<pi>:8788/`.

## GPU reality on the Pi3 (important)
The **Pi3 (VideoCore IV) GPU is GLES 2.0 only** — it cannot run the GLES-3.x / desktop
GL-3.3 shaders these graphs use (`texture()`, sampler arrays, `gl_VertexID`, `out`).
So `softwareGL = true` forces **Mesa llvmpipe** (CPU): the exact desktop-GL-3.3 path
verified bit-exact in-container. It's CPU-bound (small res / modest fps on the Pi3), but
functional and needs no shader translation.

Hardware acceleration would require either a **Pi4/Pi5** (V3D, GLES 3.1) plus a
desktop-GLSL→GLES translation pass (glslang → SPIR-V → SPIRV-Cross; the `--target gles`
lowering emits ES headers but raw TD shaders still need type-legalization), or rewriting
to GLES 2.0 for the Pi3's V3D. Tracked as future work.

## Output sinks
- **Network (now):** `stream` mode serves MJPEG over HTTP (this module). Works headless;
  view from any browser on the Pi's network.
- **HDMI (planned):** a DRM/KMS sink for direct HDMI scanout — to be written and tested
  on the device (needs the Pi + display).

## Status
Verified in-container: importer → IR → optimize → lower → artifact → native Rust runtime
(GL bit-exact vs the Python reference; native transpiled exprs; native OSC input; native
MJPEG stream). Pending on hardware: the actual Pi3 run (llvmpipe perf) + HDMI/DRM sink.
