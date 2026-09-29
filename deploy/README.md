# td-deploy — player deployment (Bazel + sbc-deploy)

End-to-end: a TouchDesigner `.tox` → compiled artifact → native Rust runtime on
a player — a Raspberry Pi (imaged onto an SD card) or an x86_64 mini PC
(installed from a USB stick) — and live-deployable. **Bazel is the driver** — the
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

# ... or build an x86_64 install USB (Intel/AMD mini PC):
bazel run //deploy:tdplayer_amd64.image_installer -- --device /dev/sdX

# base image only (networking, no app):
bazel run //deploy:tdplayer_pi3.image_sd_base -- --device /dev/sdX

# 4. iterate on a running board without re-flashing:
bazel run //deploy:tdplayer_pi3.deploy_live -- tdplayer.local
bazel run //deploy:tdplayer_pi3.ssh         -- tdplayer.local
bazel run //deploy:tdplayer_pi3.keys        -- init

# 5. seed WiFi onto a running board (persistent; survives redeploys):
cp deploy/wifi.yaml.example deploy/secrets/wifi.yaml   # then edit in your SSID/PSK
bazel run //deploy:tdplayer_pi3.seed_wifi   -- tdplayer.local --wifi-file deploy/secrets/wifi.yaml
bazel run //deploy:tdplayer_pi3.seed_wifi   -- tdplayer.local --list
```

## WiFi

`.seed_wifi` pushes the networks in a YAML file onto a **running** board as a
persistent NetworkManager layer (`nmcli`, profiles named `seed-<ssid>` in
`/etc/NetworkManager/system-connections`). It is **not** baked into the image —
secret PSKs stay off the nix store — and it survives `deploy_live`. See
[`wifi.yaml.example`](wifi.yaml.example) for the schema (`{ssid, psk?, priority?,
hidden?}`). Put real creds in `deploy/secrets/wifi.yaml` (gitignored) and pass it
with `--wifi-file`; `--list` / `--remove <ssid>` manage seeded profiles. To bake
networks into the image instead (reproducible across a reflash, PSK in the store),
set `wifi_config_file = "wifi.yaml"` on the `sbc_application` in `BUILD.bazel`.

Boot the Pi and view the live render at `http://<pi>:8788/` (MJPEG). On macOS,
start the aarch64 builder first: `bazel run @sbc_deploy//:linux_builder`.

## x86_64 players (mini PC + installer USB)

`tdplayer_amd64` builds the same flake under sbc-deploy's **amd64 family**
(`board = @sbc_deploy//deploy/boards:amd64-generic`): instead of an SD image,
`image_installer` produces a bootable install USB carrying the whole system.
Boot the mini PC from it (UEFI boot menu), pick the internal disk, confirm the
erase, and it installs offline (UEFI + systemd-boot) and reboots into the player.
CI publishes the same thing as `tdplayer-amd64.iso.zst`, which the desktop app
downloads and writes to a USB stick ("Flash a player… → x86_64 mini PC").

What the x86 image adds (`deploy/nix/x86.nix`, `python-host.nix`):

- **KMS on the iGPU.** sbc-deploy's x86 target boots with `nomodeset` (a garbled
  console on some AMD boxes); the player needs the GPU's KMS driver for GBM
  scanout and hardware GL, so `x86.nix` restates the kernel params without it.
- **Mesa iris/radeonsi** system-wide (`hardware.graphics`) + Intel's OpenCL
  runtime, which OpenVINO's GPU plugin uses for on-box inference.
- **Python for Python-host artifacts:** `python3`, the libraries manylinux wheels
  expect (on the runtime unit's `LD_LIBRARY_PATH`), and `tdplayer-prepare`, which
  the app runs before a new artifact goes live to build its venv under
  `/var/lib/tdplayer/venvs` (see DEVELOPERS.md).
- **Flash-time config from the USB.** The app writes the hostname / Wi-Fi / deploy
  keys chosen at flash time as `TDCONFIG.JSN` into the installer's EFI partition
  (`EFIBOOT`). `nixos-install` runs the new system's activation with the stick
  attached, which imports it into `/var/lib/td-flash-config`; the same
  `td-flash-config` oneshot as the Pi's then applies it every boot
  (`flash-config.nix`). Reflash the stick and boot the box with it plugged in to
  change them.

- **A virtual camera for testing** (`virtual-camera.nix`): `/dev/video10`
  (v4l2loopback) looks like an ordinary webcam to anything on the box. Stream this
  laptop's camera, a recorded clip or a URL into it with
  `app/toxc_camstream.py <player> [--source 0|clip.mp4|rtsp://…]`. The stream
  travels as MJPEG over an SSH tunnel (the deploy key) to a feeder listening only on
  the box's loopback, so no port is exposed on either machine. A project reads it as
  camera index 10.

The runtime is cross-built for x86_64 (`//runtime_rs/cross:toxc_runtime_linux_x86_64`,
linked by nixpkgs' `pkgsCross.gnu64` clang) and runs at 60 fps. On macOS,
sbc-deploy manages an x86_64 builder VM the same way it does the aarch64 one.

## Which player is at the host?

Pis and mini PCs both come up as `tdplayer.local`, and a box can be reflashed from
one to the other, so the desktop app (and `app/toxc_deploy_cli.py`) asks on every
deploy — `uname -m` over the deploy ssh — and builds for what answers:

| `uname -m` | Player       | Artifact                                                     |
| ---------- | ------------ | ------------------------------------------------------------ |
| `aarch64`  | Raspberry Pi | `gles2` (or `gles`) + aarch64 native code                    |
| `x86_64`   | mini PC      | `desktop_gl` + x86_64 native code, or a Python-host artifact |

The table lives in `app/deploy_engine/players.py`; `--arch` on the CLI skips the probe.

## How it fits together

`sbc_application` (Pi 5 `tdplayer`, Pi 3 `tdplayer_pi3`, x86_64 `tdplayer_amd64`)
bundles two Bazel outputs as `build_data` and hands them to the Nix flake (`deploy/nix`) through
sbc-deploy's `sbcBuildData` (keyed by basename):

| Bazel target                                     | key             | role                                                                                                            |
| ------------------------------------------------ | --------------- | --------------------------------------------------------------------------------------------------------------- |
| `//deploy:toxc_artifact`                         | `toxc_artifact` | portable artifact: `schedule.json` + `shaders/` + `assets/` + `exprs.mlir` + `services.json` (arch-independent) |
| `//runtime_rs/cross:toxc_runtime_linux[_x86_64]` | `toxc_runtime`  | the dynamic Rust runtime, cross-built for the image's arch                                                      |

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
