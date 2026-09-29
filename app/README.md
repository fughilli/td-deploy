# td-deploy Studio

A standalone desktop app (macOS + Windows) that drives the whole td-deploy
pipeline with no Nix, Bazel, or toolchain install on the user's machine:

- **Pick a `.toe`** and, with **Watch** on, every save auto-compiles + live-deploys
  the updated graph to the player with a **progress bar**. Each deploy asks the
  player what it is (`uname -m`) and builds for it — a Raspberry Pi or an x86_64
  mini PC (see `deploy_engine/players.py`).
- **Flash a player** with its base image (pulled from GitHub Releases): the Pi SD
  image, or the x86_64 installer USB.

The full **MLIR/LLVM optimization + native codegen runs on the host** (which is far
faster than a Pi3); the player just runs the finished artifact.

## How it fits together

```text
Electron UI  ──stdin/stdout JSON──▶  Python sidecar (frozen)
(renderer)                             │
                                       ├─ deploy_engine.compile   (.toe → optimized artifact + MLIR)
                                       ├─ deploy_engine.detect     (ssh `uname -m` → which player → target + triple)
                                       ├─ deploy_engine.finish     (host codegen for that arch: mlir-opt→translate→clang; GLSL→ES)
                                       ├─ deploy_engine.push       (rsync → /var/lib/tdplayer/staging → [prepare] → symlink swap → restart)
                                       └─ deploy_engine.flasher    (removable-disk flash, elevated raw write; the x86
                                                                    installer's flash-time config as in-stream patches)
```

- `deploy_engine/` — the pipeline, reusing the repo's compiler (`ir/ passes/
lowering/ importer/ compiler/`) exactly as the dev CLI does. stdlib only; the
  toolchain is pluggable (`NixToolchain` in dev, `BundledToolchain` in the app).
- `sidecar.py` — the long-lived process Electron talks to (JSON-lines). Also the
  elevated raw-write worker via `--raw-write` (the frozen binary is its own
  privileged helper, no external interpreter).
- `electron/` — `main.js` (spawns the sidecar, native file dialogs), `renderer/`
  (pick · watch · deploy · progress · Flash a player), `preload.js`.
- `packaging/` — PyInstaller spec + per-OS build scripts + toolchain assembly.

## Develop (no packaging)

```bash
# from the repo root, with the dev Nix toolchain available:
python app/toxc_deploy_cli.py /path/project.toe --pi tdplayer.local   # detects Pi vs x86_64
```

### Interactive GUI dev (live-reload)

```bash
bazel run //app:dev      # or: app/dev.sh
```

Launches the GUI with the window kept open across edits:

- **Renderer** (`renderer/**`) reloads on save.
- **Main / preload** (`main.js`, `preload.js`) restart the app automatically
  (via `electronmon`).
- **Python sidecar** (`sidecar.py`, `deploy_engine/**`) hot-restarts in place —
  `main.js` watches them when `TOXC_DEV` is set.

The sidecar runs under the repo's Nix dev env by default
(`TOXC_PYTHON=nix/dev.sh`, so Pillow/PyAV/zstandard/certifi are present); export
`TOXC_PYTHON` to point at a different interpreter. For a one-off without
live-reload: `cd app/electron && npm install && TOXC_PYTHON=../../nix/dev.sh npm start`.

## Build the app

```bash
# CI populates app/toolchain/ first (see .github/workflows/build-toolchain.yml)
BASE_IMAGE_TAG=<release-tag> app/packaging/build_app.sh      # macOS/Linux
powershell -File app\packaging\build_app.ps1                 # Windows
```

CI (`.github/workflows/`): `build-image` (Pi `.img` → Release), `build-toolchain`
(per-OS cross toolchain), `build-app` (dmg/exe → Release). One release tag drives
all three.

### macOS signing + notarization

The `.dmg` must be Developer ID signed **and** notarized, otherwise macOS
quarantines the downloaded app and Gatekeeper rejects it ("… was not opened
because it contains malware"). `build-app` does this using credentials from the
**`release-signing` GitHub Environment** (Settings → Environments → New
environment → add these as environment secrets):

| Secret                        | What it is                                                                          |
| ----------------------------- | ----------------------------------------------------------------------------------- |
| `CSC_LINK`                    | base64 of your **Developer ID Application** `.p12` (`base64 -i cert.p12 \| pbcopy`) |
| `CSC_KEY_PASSWORD`            | password for that `.p12`                                                            |
| `APPLE_ID`                    | Apple ID email used with `notarytool`                                               |
| `APPLE_APP_SPECIFIC_PASSWORD` | app-specific password from appleid.apple.com                                        |
| `APPLE_TEAM_ID`               | 10-character Apple Developer Team ID                                                |

Using an environment (rather than plain repo secrets) lets you gate the creds
behind required reviewers and restrict them to release tags via the
environment's deployment branch/tag rules.

Requires a paid Apple Developer account. `mac.notarize` is enabled in
`electron/package.json`, so with the secrets unset the mac job **fails at the
notarize step** rather than silently shipping a Gatekeeper-rejected app.
