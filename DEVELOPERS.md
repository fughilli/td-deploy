# Developer guide

Technical documentation for **td-deploy** — building from source, the compiler
internals, and the release pipeline. For the end-user app, see the
[README](README.md).

[![CI](https://github.com/fughilli/td-deploy/actions/workflows/test.yml/badge.svg)](https://github.com/fughilli/td-deploy/actions/workflows/test.yml)
[![build-app](https://github.com/fughilli/td-deploy/actions/workflows/build-app.yml/badge.svg)](https://github.com/fughilli/td-deploy/actions/workflows/build-app.yml)
[![build-image](https://github.com/fughilli/td-deploy/actions/workflows/build-image.yml/badge.svg)](https://github.com/fughilli/td-deploy/actions/workflows/build-image.yml)
[![build-toolchain](https://github.com/fughilli/td-deploy/actions/workflows/build-toolchain.yml/badge.svg)](https://github.com/fughilli/td-deploy/actions/workflows/build-toolchain.yml)

## What it is

td-deploy compiles a TouchDesigner `.toe`/`.tox` project into a native real-time media
graph that runs on a Raspberry Pi, with **TouchDesigner removed from the deployment
path**. The compiler is **`toxc`** (the TOX Compiler — the `//:toxc` Bazel target and
the MLIR dialect keep that name); the repo/project is `td-deploy`. The full design is
in [`docs/design/tox-to-pi.md`](docs/design/tox-to-pi.md).

## Pipeline

```text
.toe ──toeexpand──▶ IR ──passes──▶ lowering ──▶ artifact ──push──▶ Raspberry Pi
      (Mac bridge)  (typed   (fold / infer_   (GLSL + baked   (rsync + symlink
                     graph)   format / DCE)    params, ES)     swap + restart)
```

- **Import** — `toeexpand` (TD-bundled CLI) turns a `.tox` into an ASCII node tree; the
  `hostbridge/` server exposes it over HTTP so the pipeline can run off-Mac. The
  `importer/` maps that tree to the typed IR.
- **Optimize** — `passes/` runs dead-node elimination, format inference, and constant
  folding (e.g. a gaussian σ folds to a radius + normalized weights baked into the
  GLSL — no per-frame recompute).
- **Lower** — `lowering/` emits GLSL and shims desktop-GL ⇄ GLES `#version` for the
  Pi's V3D.
- **Codegen** — expression/CHOP `.mlir` is compiled to a native `aarch64-linux` `.so`
  on the host (far faster than on the Pi3); the Pi just loads the finished artifact.
- **Runtime** — `runtime_rs/` (Rust) plays the artifact on the Pi. Two reference
  backends in `runtime/` cross-validate: real offscreen **OpenGL** (EGL-surfaceless /
  Mesa) and a **numpy** oracle — they agree to 1 LSB on the blur demo.

## Repo layout

```text
app/                 td-deploy Studio (Electron UI + Python deploy sidecar) — see app/README.md
hostbridge/          Mac-side HTTP bridge to toeexpand/TouchDesigner
importer/            toeexpand tree -> typed IR
ir/                  typed operator-graph IR (JSON)
passes/              fold / infer_format / dead-node-elim
lowering/            GLSL codegen + desktop-GL⇄GLES shim; IR -> RuntimePlan
compiler/            native codegen (.mlir -> .so), GLES translation
runtime/             host reference backends (GL + numpy) and conformance driver
runtime_rs/          the Rust runtime that plays the artifact on the Pi
deploy/              Pi SD-image + live-deploy targets (Nix) — see deploy/README.md
graphs/              sample IR graphs (blur_demo.json)
nix/                 rootless Nix dev env (python + mesa) for the host runtime
docs/design/         design docs
```

## Prerequisites

Bazel drives everything (the Python compiler, the Rust runtime, and the Pi
image/live-deploy targets). Toolchains are hermetic — a pinned nixpkgs via
`rules_nixpkgs` for Mesa/LLVM, plus `rules_python` and `rules_rust` — so **`nix` is the
only system requirement** (use `bazelisk` for the pinned Bazel).

## Build & test

```sh
bazel build //...       # build the whole graph
bazel test  //...       # hermetic tests (pipeline + lock)
bazel query //...       # list every target
```

Key targets: `//ir` `//passes` `//lowering` `//runtime` (pipeline libs), `//:toxc`
(CLI), `//:expand` (`.toe`→IR, needs the Mac bridge), `//compiler:emit` +
`//compiler:build_exprs` (native lowering), `//runtime_rs:toxc_runtime` (Rust runtime),
`//deploy:toxc_artifact` + `//deploy:tdplayer{,_pi3}.*` (SD-image / live-deploy).

### Compile & render locally (no hardware)

```sh
bazel run //:toxc -- graphs/blur_demo.json --backend cpu --out /tmp/out
bazel run //:toxc -- graphs/blur_demo.json --emit-artifact /tmp/art --target gles2

# GL-vs-CPU conformance render (1-LSB check):
cd toxc && nix/dev.sh python3 -m runtime.run graphs/blur_demo.json --backend both --out out
# -> out/gl.png, out/cpu.png, out/diff8x.png + a GL-vs-CPU LSB report
```

The host GL path is desktop core 3.3 via llvmpipe (surfaceless EGL); `--target gles`
emits `#version 310 es` for the Pi's V3D. `runtime/egl_context.py` is the only file
that differs between host and Pi.

## The desktop app

**td-deploy Studio** (`app/`) wraps the pipeline in an Electron UI over a Python deploy
sidecar, so end users need no Nix/Bazel/toolchain install. Full architecture, live-reload
dev loop, packaging, and macOS signing/notarization are documented in
**[`app/README.md`](app/README.md)**.

```sh
bazel run //app:dev      # interactive GUI with live-reload (renderer + main + sidecar)
```

## Deploy targets (Pi)

```sh
bazel run //deploy:tdplayer_pi3.image_sd -- --device /dev/sdX   # write a Pi 3 SD card
bazel run //deploy:tdplayer.image_sd     -- --device /dev/sdX   # Pi 5
bazel run //deploy:tdplayer_pi3.deploy_live -- <host>           # push to a running Pi
```

The player image is a lean, headless NixOS build (VC4/V3D hardware GL, no LLVM/llvmpipe)
that boots straight into the runtime, with optional tailnet membership. Sizing,
mesa-lean flags, and the on-device diagnostics live in
**[`deploy/README.md`](deploy/README.md)**.

## The host bridge

Run on the Mac where TouchDesigner is installed; exposes `toeexpand`/`toecollapse` (and
experimentally TD headless render) to the pipeline.

```sh
python3 hostbridge/td_host_server.py --port 8770
curl -s http://<mac-ip>:8770/health | python3 -m json.tool
```

It also serves the app's loopback-only, token-gated `/deploy`, `/build`, and `/inspect`
endpoints (fixed commands, no arbitrary exec).

## CI & releases

Four GitHub Actions workflows (`.github/workflows/`):

| Workflow          | What it does                                          |
| ----------------- | ----------------------------------------------------- |
| `test`            | Presubmit: `prek` lints + the Bazel test suite        |
| `build-image`     | Builds the Pi `.img.zst` and attaches it to a Release |
| `build-toolchain` | Builds the per-OS cross toolchain the app bundles     |
| `build-app`       | Builds the signed `.dmg` / `.exe` and attaches them   |

**One release tag drives all three build workflows.** Push a `vX.Y.Z` tag; the image,
toolchain, and app builds attach their artifacts to that Release. macOS signing +
notarization uses the `release-signing` GitHub Environment (see
[`app/README.md`](app/README.md#macos-signing--notarization)).

Presubmit lints run via `prek` against the pinned hooks in `.pre-commit-config.yaml`
(black, isort, flake8, shellcheck, buildifier, nixpkgs-fmt, prettier, markdownlint).
Run locally with `prek run --all-files`.

## Regenerating the app screenshots

The README screenshots (`docs/img/studio-*.png`) are rendered from the real
`app/electron/renderer/` markup + `style.css` with realistic state applied, via headless
Chromium. To refresh them after a UI change, re-render the renderer at 2× device scale
(`chromium --headless=new --disable-crashpad --disable-breakpad --force-device-scale-factor=2`)
and keep each PNG under the 600 KB pre-commit limit.
