# toxc worklog

Newest first. See `docs/design/tox-to-pi.md` for the full design.

## 2026-09-15 — Pi image size reduction (IN PROGRESS, branch tbd)

**Goal (user):** get the Pi SD image as small as possible — both the raw `.img`
(fast decompress + flash) and the published `.zst` (fast download). Agreed
approach: **measure first**, then trim.

**Ground truth** (from the `build-image` CI "Image size breakdown" step, run
35027659025 on `main`, Pi 3 image): raw `.img` **4.1 GB**; system closure **2439
MB / 763 paths**. Top offenders:

| MB | path | notes |
|----|------|-------|
| 507.5 | llvm-19.1.7-lib | **#1** — pulled in only by Mesa's llvmpipe SW rasterizer |
| 206.5 | mesa-25.0.7 | |
| 185.6 | source | UNIDENTIFIED — likely the RPi kernel/firmware `src`; needs `nix why-depends` |
| 152.2 | linux_rpi kernel | |
| 116.8 | python3-3.12.12 | pulled by ? (systemd/udev/activation); needs why-depends |
| 78.5 | raspberrypi-firmware | bootloader/GPU fw (unavoidable) |
| 61.4 | perl | likely `environment.defaultPackages` — dropped by `lean` |
| 56.3 | systemd | |
| 55.7 | git (+15.2 git-doc) | needed on-device? deploy_live uses nix, maybe not git |
| 45.2 | gtk+3 | why on a headless image? needs why-depends (NM plugins already dropped upstream) |
| 43.1 | vim | RPi sd-image rescue toolkit — dropped by `lean` |
| 41.6 | glibc / 40.2 icu4c / 36.5 nix / 25.7 nix-doc / 25.4 nixos-manual-html / 15.0 texinfo | docs dropped by `lean`; nix needed on-device for deploy_live |

**Key findings this session (resumed after container restart):**

- **The `lean` seam is UNUSED.** sbc-deploy `sbc_application` has `lean=False`
  default; setting `lean=True` exports `$SBC_LEAN=1`, and at our LOCKED rev
  (`4d88be7`) `mkSbcSystem` then: disables `profiles/base.nix` (rescue toolkit —
  **vim** + testdisk/ddrescue/sshfs/tcpdump), forces `documentation.*=false`
  (**nixos-manual-html, man, texinfo, doc/info**), and `environment.defaultPackages
  = []` (**perl/rsync/strace**). **First win: add `lean = True` to BOTH
  `sbc_application` calls in `deploy/BUILD.bazel`.** Safe, purpose-built, ~100–150+
  MB. Does NOT touch LLVM/mesa.
- **LLVM 507 MB is the whale** and is Mesa's llvmpipe dependency, NOT our MLIR
  toolchain (apps.nix uses `llvmPackages_18` mlir/clang only as build-time
  `nativeBuildInputs` to compile exprs.mlir→libexprs.so; that's LLVM 18 and stays
  out of the runtime closure — the closure's LLVM is 19, mesa's). To drop it we
  must build Mesa without llvmpipe (`mesa.override { galliumDrivers = [ "vc4"
  "v3d" "kmsro" ]; vulkanDrivers = []; ... }` — VC4/V3D gallium don't need LLVM).
  **Tradeoff: no software-GL fallback.** Needs user sign-off + verifying mesa
  builds w/o llvmpipe and VC4 still works.
- **INCONSISTENCY to resolve first:** `deploy/BUILD.bazel` lines 32–34 comment
  says "We run llvmpipe, so desktop_gl", but `deploy/nix/apps.nix` sets
  `softwareGL = false` (VC4 hardware, gles2). WORKLOG 2026-09-10(2) confirms the
  move to VC4 hardware. If we truly run VC4 hw, llvmpipe is only a fallback and is
  droppable. Confirm on-device which renderer is actually used
  (`journalctl -u sbc-tdplayer | grep '\[gl\]'`) before removing llvmpipe.

**Measurement loop:** `build-image.yml` (native arm64 runner) → step "Image size
breakdown" (continue-on-error) prints the closure top-N via
`.github/scripts/closure_top.py`. Trigger via `workflow_dispatch` (needs a `tag`
input; the final release-attach step will fail w/o a real release but the size
step runs before it). Read logs via the GitHub API — NB the job-logs endpoint
302-redirects to a signed blob, so STRIP the Authorization header on redirect.

**Decisions (user, this session):**
- Remove llvmpipe/LLVM — **VC4/V3D hardware is confirmed** by the user (safe to
  lose the SW-GL fallback).
- **Build on the MAC HOST** via the hostdeploy tooling (Mac has the aarch64
  builder VM), NOT via CI. `/workspace` is a **virtiofs bind mount from the Mac
  (Lima)**, so edits here ARE what the Mac builds — **no git push needed**.
- **Keep local nix** (the overlay) — useful for `why-depends`/dev-shell/tool
  realizations in the container, even though the image build runs on the Mac.

**Container nix overlay ADDED + WORKING** (`.claude-container-overlay/Dockerfile`):
Determinate Nix (`--init none`, no sudo, flakes, `build-users-group=`,
`sandbox=false`, `max-jobs=auto`), the **nixos-raspberrypi cachix** substituter
(key `…4iMO9LXa8BqhU+Rpg6LQKiGa2lsNh/j2oiYLNOQ5sPI=`), nix on PATH. Post-restart
`nix 3.22.3` evaluates + realizes as uid 501, rpi cachix + cache.nixos.org active.
BUG hit + FIXED: the skill's `rm -rf profiles/per-user` ORPHANED Determinate's
default profile (`default -> per-user/root/profile`) → nix vanished from PATH.
Overlay now repoints `default` at the concrete determinate-nix store pkg BEFORE
deleting per-user. See memory determinate-nix-overlay-per-user-profile.

**Flake gotcha:** `deploy/nix/mesa-lean.nix` is git-STAGED (not committed) so
git-based flake eval sees it (untracked files are invisible to flakes).

**Edits APPLIED this session (uncommitted, on the shared mount):**
- `deploy/BUILD.bazel`: `lean = True` on both `sbc_application` (drops
  vim/docs/perl/rsync/strace). Also fixed the stale "we run llvmpipe" comment.
- `deploy/nix/mesa-lean.nix` (NEW) + wired into `flake.nix` systemModules: global
  `nixpkgs.overlays` building Mesa with `galliumDrivers=["vc4" "v3d"]`,
  `vulkanDrivers=["broadcom"]`, PLUS `overrideAttrs` appending mesonFlags to
  disable `gallium-rusticl` (OpenCL — the OTHER llvm puller, hardcoded true in
  nixpkgs), `gallium-vdpau`/`va`/`xa` (video/X state trackers that REQUIRE a
  desktop gallium driver we dropped → meson errors without this), and `teflon`.
  Dropping galliumDrivers ALONE is insufficient: (a) rusticl still links libLLVM,
  (b) `-Dauto_features=enabled` + vc4/v3d-only makes vdpau's meson check fail
  ("VDPAU requires r600/radeonsi/nouveau/d3d12"). Binary-cache MISS → Mesa builds
  from source on the aarch64 VM (kernel/firmware stay cache hits).
- `deploy/measure_image_size.sh` (NEW): mirrors the CI size step (resolve
  toplevel → `nix path-info -S` + `closure_top.py`), plus `--why PKG` to run
  `nix why-depends` for the mystery whales. Run it on the machine holding the
  built image (the Mac): `deploy/measure_image_size.sh build.log 30 --why source --why python3`.

**How to build+measure on the Mac (bind-mounted, so it builds THESE edits):**
Two ways, same result (`deploy/build_and_measure.sh <board> <top_n>` under the
hood → builds `…image_sd --no-write`, then `measure_image_size.sh`):
- HTTP-driven via the hostbridge (NEW `/build` endpoint, token-gated, fixed prog,
  board enum only — mirrors `/deploy`): `POST /build {board:"pi3"}` then poll
  `GET /build?id=build-N&from=<next>`. Advertised in `/health`.
- By hand on the Mac: `deploy/build_and_measure.sh pi3 30`
  (or `bazel run //deploy:tdplayer_pi3.image_sd -- --no-write | tee build.log`
  then `deploy/measure_image_size.sh build.log 30 --why source --why python3`).

Baseline to beat: raw 4.1 GB, closure 2439 MB. Expected wins: −507 MB (LLVM,
mesa-lean) + ~100-150 MB (lean seam). The `/build` hostbridge additions
(`build_start`/`build_poll` + `_BUILD_SCRIPT`) are py_compile'd + smoke-tested.
Bridge now binds LOOPBACK by default (`--host 127.0.0.1`) — reachable from the
container via Docker's host gateway (`host.docker.internal:8770`), NOT the LAN.

**SBC_CROSS gotcha (fixed):** the first host build cross-compiled on the Mac
(building the aarch64 cross-toolchain + would rebuild mesa+kernel from source)
instead of using `SBC_BUILDER_DISK`. Cause: `sbc_deploy.sh choose_backend` takes
the cross path whenever `$SBC_CROSS` is set in the env, and the bridge inherited a
stray `SBC_CROSS`. macOS DEFAULT (no --cross/--builder) already auto-manages the
sized builder VM honoring `SBC_BUILDER_DISK`. Fix: `build_and_measure.sh` now
`unset SBC_CROSS SBC_BUILD_PLATFORM` so it always takes the auto-managed-builder
path. Confirmed: build-2 logs "Using auto-managed aarch64-linux builder VM" +
`--lean` active. (Also fixed macOS mktemp: trailing X's only.) No bridge restart
needed for script fixes — `/build` re-reads the script each call.
Future nicety: expose `--keep-builder` via /build for warm iteration.

**MEASURED (host builds via /build, Pi 3):**
- Baseline (main): raw 4.1 GB, closure 2439 MB.
- build-4 (lean + mesa driver/rusticl trim, but MISSING -Dllvm): raw **3.5 GB**,
  closure **2088 MB** (−351). mesa 206→**30.7 MB** ✓; vim/nixos-manual/nix-doc/
  texinfo gone ✓. BUT llvm-19 **still 507 MB** — `why-depends` = `mesa → llvm`:
  nixpkgs doesn't set `-Dllvm`, so `auto_features=enabled` links libLLVM into
  libgallium regardless of drivers/rusticl. Fix = `-Dllvm=disabled` (build-5,
  in flight). Expected closure after: ~1580 MB.
- Build iteration gotchas hit + fixed in mesa-lean.nix: (1) meson vdpau error →
  disable vdpau/va/xa; (2) `spirv2dxil` output "failed to produce" (d3d12 gone) →
  `mkdir -p "$spirv2dxil"` in postInstall; (3) the real llvm puller = `-Dllvm`.

**Next whales (from build-4 why-depends) — the follow-on cuts:**
- `source` 186 MB ← `etc → nix/registry.json → source`: the nix flake registry
  pins the FULL nixpkgs source into the image. Kill via `nix.registry = lib.mkForce
  {};` + clear `nix.nixPath`/flake-registry, if on-device flake ref isn't needed.
- `python3` 117 MB ← `system-path → git → python3`: GIT drags python3. If git
  isn't needed on-device (deploy_live uses nix copy, not git), drop git from
  systemPath → likely drops python3 + git-doc 15 + git 56 too.
- `gtk+3` 45 MB ← `networkmanager → openconnect → stoken → gtk3`: a VPN dep NM
  still pulls despite `networkmanager.plugins=[]`. Trim NM's openconnect/VPN
  runtime dep (or a leaner NM) → drops gtk3 + openconnect + stoken.
- `perl` 61 MB: still present (pulled outside defaultPackages, likely systemd/
  activation). Harder; revisit last.

- build-5 (`-Dllvm=disabled` added): **WIN.** raw **3.0 GB**, closure **1580 MB**
  (−859 / −35% vs baseline). `why-depends llvm` = "no closure path matching 'llvm'"
  — the 507 MB whale is fully gone; mesa 30 MB, vc4/v3d hardware GL intact. This is
  the committed state of mesa-lean.nix.

**Remaining top whales @ 1580 MB:** source 186, kernel 152, python3 117, rpi-fw 78,
perl 61, systemd 56, git 56, gtk3 45, glibc 42, icu4c 40, nix 36, mesa 30.

- build-6 (`deploy/nix/lean-extra.nix`: `nix.registry`/`nix.nixPath` mkForce empty
  + `stoken.override{withGTK3=false}`): raw **2.7 GB**, closure **1235 MB**
  (−345 from build-5). `why-depends` = source GONE, gtk+3 GONE (gtk3 cascaded its
  pango/cairo/gdk-pixbuf subtree too). User confirmed deploy = `nix copy` + switch,
  so on-device flake registry not needed. **Cumulative: 2439 → 1235 MB (−49%).**

**Committed-state cuts (all validated on real Pi3 host builds):**
`lean=True` (BUILD.bazel) · `mesa-lean.nix` (−507 llvm, mesa 206→30) ·
`lean-extra.nix` (−source 186, −gtk3 subtree). Files: deploy/BUILD.bazel,
deploy/nix/{mesa-lean,lean-extra}.nix + flake.nix wiring.

**LAST big cut — git+python3 (~188 MB), needs a sbc-deploy change:**
`ssh-deploy.nix:64` = `environment.systemPackages = [ git rsync ]` ("toolchain the
remote nixos-rebuild switch needs"). git → python3 (117) + git-doc (15) + git (56).
NixOS can't subtract a package another module adds, and overlaying git to a stub
breaks fetchers, so the fix is upstream: gate git behind SBC_LEAN in ssh-deploy.nix
(`[ rsync ] ++ lib.optionals (getEnv "SBC_LEAN" != "1") [ git ]`). Two-repo change:
commit to sbc-deploy build-data + bump deploy/nix/flake.lock + MODULE.bazel
git_override. Validating locally via a path: input override first.

**Remaining floor @ 1235 MB:** kernel 152, python3 117 (git), rpi-fw 78, perl 61,
systemd 56, git 56, glibc 42, nix 36, mesa 30, systemd-min 26, NM 21, modemmgr 15.
After git: ~1047 MB. Perl (61) is systemd/activation-pulled (hard). Floor ~1.0 GB.

**git cut — sbc-deploy PR #20 (`chore/lean-drop-git`, commit 37c9067):** gates git
behind SBC_LEAN in ssh-deploy.nix. PINS BUMPED to that branch commit for
validation: `deploy/nix/flake.nix` url → `chore/lean-drop-git`, flake.lock re-locked,
MODULE.bazel git_override → 37c9067. **COORDINATION TODO:** once the user merges
PR #20 into build-data, repoint flake.nix url → `build-data` + `nix flake update
sbc-deploy` + MODULE.bazel commit → the merged build-data rev (TODO in MODULE.bazel).

- build-7 (git cut): raw **2.5 GB**, closure **1150 MB** (−85). git + git-doc GONE.
  BUT python3 (117) STAYED — its real referrer is **mesa → python3** (mesa installs
  a python-shebang script; `patchShebangs $out/bin/*` in mesa postFixup pulls the
  whole interpreter). So git removal saved only git+doc, not python3.
  **CUMULATIVE: 2439 → 1150 MB (−53%); raw 4.1 → 2.5 GB.** llvm/source/gtk3/git all
  verified GONE via why-depends.

- build-8 (`why-depends --precise`): the python3 referrer is exactly
  `mesa/bin/mesa-overlay-control.py` (a python-shebang debug helper for the Vulkan
  overlay-HUD layer — NOT a runtime GL/GLES component).
- build-9 (mesa-lean.nix postInstall `rm -f $out/bin/mesa-overlay-control.py`):
  **python3 GONE.** raw **2.4 GB**, closure **1033 MB**. why-depends confirms
  llvm/source/python3/gtk3 all "no closure path".

**RESULT (build-10): closure 2439 → 1033 MB (−58%); raw 4.1 → 2.4 GB; .img.zst
(zstd -19, the download) 621 → 348 MB (−44%).** measure_image_size.sh takes `--zst`.

**More cuts (build-12/13), all in lean-extra.nix, clean NixOS levers:**
- `system.disableInstallerTools = true` → drops nixos-option/rebuild/generate-config/
  install → drops man-db + groff (nixos-option baked them into PATH). Safe:
  deploy_live runs switch-to-configuration on the board, never nixos-rebuild.
- `networking.modemmanager.enable = false` (service side) + NM overrideAttrs
  `mesonFlags += -Dmodem_manager=false` (the REAL fix — NM's `libnm-wwan.so`
  embedded a ref to modemmanager regardless of the service). Drops modemmanager
  15 + libqmi 7.5. NM rebuilds from source (~5 min). WiFi/ethernet unaffected.
- **build-13: closure 984 MB (−60% cumulative), .img.zst 341 MB, raw 2.3 GB.**
  modemmanager + groff verified gone.

**Raw-image zero padding explained (make-ext4-fs.nix):** ext4 sized at
`2×8KB×numFiles + 1.2×content`; `resize2fs -M` shrinks the FS but the image file
is NOT truncated back (nixpkgs #125121 caveats), leaving ~1.4 GB sparse zeros.
Compresses away (doesn't hit the 341 MB download); only costs flash-write time.
Fixable by truncating the .img to the shrunk-FS size — not done (user chose the
content cuts instead).

**Tailscale added (build-14):** `deploy/nix/tailscale.nix` (systemModule, modeled on
splanc pi/hitl) — `services.tailscale` + `authKeyFile=/var/lib/tailscale/authkey`
(`--ssh --hostname=<host>`) + a `tailscale-hostname` pin service + trustedInterfaces.
Auth seeded OUT OF BAND via `deploy/seed_tailscale.sh <host> <tskey-…>` (run from a
box on the board's LAN; writes the key + restarts tailscaled-autoconnect over the
deploy SSH). Reason: `tdplayer2.local` mDNS only works same-L2; the board deploys to
a different LAN. Cost: tailscale ~55 MB → closure 984→**1040 MB**, .zst 341→**356 MB**.
For /inspect over the tailnet, the hostbridge's Mac must also be on the tailnet;
then target `tdplayer2` (tailnet name), not `.local`.

**ON-HARDWARE VALIDATION (tdplayer2, Pi 3, the −60% lean image + tailscale):**
Booted clean; `sbc-tdplayer` active; **rendering 60 fps on the VC4 GPU in HARDWARE**
(/stats: frame=16.75ms, present:flip=14.75ms DRM page-flip; renderD128 + vc4 module
loaded; 80k frames). **Confirms the no-LLVM mesa cut works on real hardware — no
llvmpipe.** WiFi (brcmfmac), tailscale (tun), HDMI audio, SD all up. Reached it via
the tailnet (`tdplayer2`) through the hostbridge `/inspect` endpoint.

**Kernel trim = DECLINED (user: stop).** Data collected via /inspect: module tree
113 MB, 81 modules loaded — BUT the loaded set misses HOTPLUG modules (the Twister
MIDI wasn't plugged in → snd-usb-audio/usbhid absent), so blind removal would break
MIDI on plug-in. Options were (A) minimal config or (B) CONFIG_MODULE_COMPRESS_ZSTD
(safer, keeps all). Both recompile the RPi kernel (lose cachix, 30-60 min builds) +
need flash-and-boot verify, for ~25 MB off the download. Judged not worth it — the
image is validated at 356 MB download / 1040 MB closure. If ever revisited: prefer
(B) module compression; for (A), re-inspect WITH the Twister plugged to capture its
modules first.

**Raw-image zero padding = the 1 GB FIRMWARE partition, NOT rootfs slack.** A
diagnostic trim (reading the MBR + ext4 superblock) showed the root ext4 already
FILLS its partition (~1.46 GB of genuine inode/metadata for the ~1 GB closure —
not truncatable), and the padding is the FAT firmware partition: nixos-raspberrypi
defaults `sdImage.firmwareSize=1024` MiB but /boot/firmware only uses ~26 MB
(vendor fw + u-boot + config.txt + dtbs + extlinux; kernel/initrd live in the ext4
rootfs). Fix: `image.nix` sets `sdImage.firmwareSize = lib.mkForce 128` (~5x usage)
→ **raw .img 2.4 → 1.5 GB (~38% faster SD write)**; .img.zst unchanged (356 MB, zeros
already compressed away). (Scrapped the earlier shrink-sd-image.nix post-process —
it targeted the wrong partition.)

**FINAL: closure 2439 → 1040 MB (−57%), raw 4.1 → 1.5 GB (−63%), .img.zst 621 →
356 MB (−43%), + tailnet reachability. Validated on a real Pi 3.**
Everything left is the OS floor: kernel 152, rpi-fw 78, perl 61 (systemd/activation),
systemd 56, glibc 42, nix 36, mesa 30, systemd-min 26, NM 21, modemmgr 15, …
Further nibbles (diminishing): perl (activation-bound, hard), modemmanager 15 (drop
if no cellular), sd-image free-space padding (raw .img has ~1.4 GB empty above the
~1 GB closure; expandOnBoot fills the card anyway — compresses to ~nothing in .zst
but affects decompress/write time).

**COORDINATION / HANDOFF:**
- sbc-deploy PR #20 (chore/lean-drop-git) → user reviews/merges → then repoint
  td-deploy pins to build-data (flake.nix url + `nix flake update sbc-deploy` +
  MODULE.bazel commit; TODO marker in MODULE.bazel).
- td-deploy changes are UNCOMMITTED (BUILD.bazel, flake.nix, flake.lock, MODULE.bazel,
  mesa-lean.nix, lean-extra.nix, hostbridge/td_host_server.py, build_and_measure.sh,
  measure_image_size.sh, .claude-container-overlay/, WORKLOG.md) — user to commit.
- PR #10 (dev live-reload) still open + mergeable — user's call.

**State:** PR #10 (`feat/dev-live-reload`, interactive dev target + live-reload)
is OPEN + mergeable, awaiting the user's merge call — not mine to merge. All the
above edits are UNCOMMITTED on this branch (working tree survives restart).


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
