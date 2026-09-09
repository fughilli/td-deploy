# td-deploy — Raspberry Pi deployment (Bazel + sbc-deploy)

End-to-end: a TouchDesigner `.tox` → compiled artifact → native Rust runtime on
a Pi, imaged onto an SD card and live-deployable. **Bazel is the driver** — the
image and live-deploy targets come from the [sbc-deploy](https://github.com/fughilli/sbc-deploy)
framework via the `sbc_application` macro (`//deploy:BUILD.bazel`).

## The targets

```sh
# 1. (optional) snapshot a .toe to a committed IR .json — needs the Mac TD bridge:
bazel run //:expand -- project.toe graphs/project.json

# 2. compile the IR into the portable artifact (hermetic, no bridge):
bazel build //deploy:toxc_artifact          # or point a new toxc_artifact() at your .json

# 3. image an SD card (bundles the artifact + Rust runtime):
bazel run //deploy:tdplayer_pi3.image_sd -- --device /dev/sdX      # Raspberry Pi 3
bazel run //deploy:tdplayer.image_sd     -- --device /dev/sdX      # Raspberry Pi 5

# base image only (networking, no app):
bazel run //deploy:tdplayer_pi3.image_sd_base -- --device /dev/sdX

# 4. iterate on a running board without re-flashing:
bazel run //deploy:tdplayer_pi3.deploy_live -- tdplayer.local
bazel run //deploy:tdplayer_pi3.ssh         -- tdplayer.local
bazel run //deploy:tdplayer_pi3.keys        -- init
```

Boot the Pi and view the live render at `http://<pi>:8788/` (MJPEG). On macOS,
start the aarch64 builder first: `bazel run @sbc_deploy//:linux_builder`.

## How it fits together

`sbc_application` (Pi 5 `tdplayer`, Pi 3 `tdplayer_pi3`) bundles two Bazel
outputs as `build_data` and hands them to the Nix flake (`deploy/nix`) through
sbc-deploy's `sbcBuildData` (keyed by basename):

| Bazel target | key | role |
|---|---|---|
| `//deploy:toxc_artifact` | `toxc_artifact` | portable artifact: `schedule.json` + `shaders/` + `assets/` + `exprs.mlir` + `services.json` (arch-independent) |
| `//runtime_rs:toxc_runtime` | `toxc_runtime` | the dynamic aarch64 Rust runtime |

`deploy/nix/apps.nix` then, at image-build time:
1. **finishes the artifact for aarch64** — compiles `exprs.mlir → exprs/libexprs.so`
   with the image's own LLVM 18 + clang (the Bazel-emitted artifact is
   arch-independent on purpose; the native `.so` must match the Pi).
2. **autoPatchelfs the runtime** onto NixOS and wraps it with the headless-EGL
   env (Mesa on `LD_LIBRARY_PATH`, `EGL_PLATFORM=surfaceless`) + the
   `stream <artifact> <port> <fps>` args.
3. exposes it as a `services.sbcApps.tdplayer` systemd unit on port 8788.

The sbc-deploy version is pinned twice, in parallel — `git_override` in
`//MODULE.bazel` (Bazel side) and `deploy/nix/flake.lock` (Nix side). Bump both.

## GPU reality on the Pi (important)

The image forces **Mesa llvmpipe** (`softwareGL = true` in `apps.nix`): CPU GL,
functional on any board and **required on the Pi 3** (VideoCore IV is GLES 2.0
only and can't run these desktop/GLES-3 shaders in hardware). It's CPU-bound
(small res / modest fps on a Pi 3). For hardware acceleration, build a `gles2`
artifact + translate the shaders (`bazel run //compiler:build_shaders_gles`) and
set `softwareGL = false` on a Pi 5 (V3D). `//deploy:toxc_artifact` already
lowers to `gles2` so a Pi 3 can render it under llvmpipe.

## `services.toxc` (legacy, manual import)

`deploy/toxc-service.nix` is the older hand-imported NixOS module (`imports = [
./toxc-service.nix ]` in an external sbc-deploy config). The `sbc_application`
targets above supersede it; it's kept for reference.

## Status / not-yet-verified on hardware

Verified in-container: the full Bazel graph builds (`bazel build //...`), the
hermetic pipeline test passes, `//:toxc` renders (CPU) + emits the artifact, the
Rust runtime builds (aarch64), and the `*.image_sd` targets materialize with all
runfiles. **Pending hardware:** the actual `nix` image realization + flashing,
booting a Pi 3/5, on-device GL, and the `.toe`-bridge expansion (needs a Mac
running TouchDesigner). The runtime is DYNAMIC (it `dlopen`s libEGL) — the image
autoPatchelfs it; a fully-static musl build is impossible here (`-ldl`) and
pointless (a loader is needed for `dlopen` regardless).
