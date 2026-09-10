# td-deploy Studio

A standalone desktop app (macOS + Windows) that drives the whole td-deploy
pipeline with no Nix, Bazel, or toolchain install on the user's machine:

- **Pick a `.toe`** and, with **Watch** on, every save auto-compiles + live-deploys
  the updated graph to the Pi with a **progress bar**.
- **Flash an SD** with the Pi base image (pulled from GitHub Releases).

The full **MLIR/LLVM optimization + aarch64 codegen runs on the host** (which is far
faster than the Pi3); the Pi just runs the finished artifact.

## How it fits together

```text
Electron UI  ──stdin/stdout JSON──▶  Python sidecar (frozen)
(renderer)                             │
                                       ├─ deploy_engine.compile   (.toe → optimized artifact + MLIR)
                                       ├─ deploy_engine.finish     (host aarch64 codegen: mlir-opt→translate→clang; GLSL→ES)
                                       ├─ deploy_engine.push       (rsync → /var/lib/tdplayer/staging → symlink swap → restart)
                                       └─ deploy_engine.flasher    (removable-disk SD flash, elevated raw write)
```

- `deploy_engine/` — the pipeline, reusing the repo's compiler (`ir/ passes/
lowering/ importer/ compiler/`) exactly as the dev CLI does. stdlib only; the
  toolchain is pluggable (`NixToolchain` in dev, `BundledToolchain` in the app).
- `sidecar.py` — the long-lived process Electron talks to (JSON-lines). Also the
  elevated raw-write worker via `--raw-write` (the frozen binary is its own
  privileged helper, no external interpreter).
- `electron/` — `main.js` (spawns the sidecar, native file dialogs), `renderer/`
  (pick · watch · deploy · progress · Flash SD), `preload.js`.
- `packaging/` — PyInstaller spec + per-OS build scripts + toolchain assembly.

## Develop (no packaging)

```bash
# from the repo root, with the dev Nix toolchain available:
python app/toxc_deploy_cli.py /path/project.toe --pi tdplayer.local --target gles2
# run the GUI against the repo's python sidecar:
cd app/electron && npm install && TOXC_PYTHON=../../nix/dev.sh npm start
```

## Build the app

```bash
# CI populates app/toolchain/ first (see .github/workflows/build-toolchain.yml)
BASE_IMAGE_TAG=<release-tag> app/packaging/build_app.sh      # macOS/Linux
powershell -File app\packaging\build_app.ps1                 # Windows
```

CI (`.github/workflows/`): `build-image` (Pi `.img` → Release), `build-toolchain`
(per-OS cross toolchain), `build-app` (dmg/exe → Release). One release tag drives
all three.
