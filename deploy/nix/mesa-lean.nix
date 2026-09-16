# Drop LLVM from the image by building Mesa WITHOUT the software renderers OR the
# OpenCL frontend.
#
# Two things pull the ~507 MB `llvm-*-lib` into the image closure, and BOTH must
# go — dropping the gallium software drivers alone is NOT enough:
#   1. llvmpipe/swrast — the LLVM-backed software rasterizers (galliumDrivers).
#   2. rusticl — Mesa's Rust OpenCL frontend (`-Dgallium-rusticl=true` in nixpkgs),
#      which links clang/libLLVM. This is hardcoded in the derivation's mesonFlags,
#      not an override arg, so it needs overrideAttrs.
#
# The Raspberry Pi renders on its Broadcom GPU in HARDWARE (VC4 on Pi 0-3, V3D on
# Pi 4/5); those gallium drivers use neither LLVM nor OpenCL. So: restrict Mesa to
# the Pi drivers AND disable rusticl + the video/X state-trackers (vdpau/va/xa)
# that require desktop gallium drivers (r600/radeonsi/nouveau/…) we no longer
# build — with only vc4/v3d + `-Dauto_features=enabled`, meson otherwise errors
# ("VDPAU state tracker requires at least one of r600, radeonsi, nouveau, d3d12").
#
# TRADE-OFF: no llvmpipe software-GL FALLBACK. Safe here — the on-device renderer
# is the confirmed VC4/V3D hardware path (apps.nix `softwareGL = false`). A
# zero-LLVM safety net is available if wanted: add "softpipe" to galliumDrivers.
#
# GLOBAL overlay (not just the app's package) so EVERY mesa reference in the
# system closure uses the LLVM-free build. Cost: a custom Mesa is a binary-cache
# MISS, so it builds from source on the aarch64 builder (kernel/firmware stay
# cache hits).
{ lib, ... }:
{
  nixpkgs.overlays = [
    (final: prev: {
      mesa = (prev.mesa.override {
        # Broadcom Pi GPUs only. vc4 = Pi 0-3, v3d = Pi 4/5. No llvmpipe/softpipe.
        galliumDrivers = [ "vc4" "v3d" ];
        # V3D's Vulkan (Pi 4/5); drop swrast/Lavapipe (the LLVM one). The runtime
        # uses GLES/EGL, not Vulkan, so this is only belt-and-suspenders.
        vulkanDrivers = [ "broadcom" ];
      }).overrideAttrs (old: {
        # Appended AFTER the derivation's own mesonFlags; meson takes the last
        # value for a repeated -D option, so these override the hardcoded defaults.
        mesonFlags = (old.mesonFlags or [ ]) ++ [
          # THE load-bearing one: nixpkgs doesn't set -Dllvm, so `auto_features=
          # enabled` turns it ON and libgallium links libLLVM (the 507 MB whale)
          # even with no llvmpipe/rusticl. VC4/V3D are pure-hardware and need no
          # LLVM, so force it off — this is what actually drops llvm-*-lib.
          (lib.mesonEnable "llvm" false)
          (lib.mesonBool "gallium-rusticl" false) # OpenCL -> drops clang/libLLVM
          (lib.mesonEnable "gallium-vdpau" false) # video state trackers need a
          (lib.mesonEnable "gallium-va" false)    # desktop gallium driver we
          (lib.mesonEnable "gallium-xa" false)    # no longer build (headless too)
          (lib.mesonBool "teflon" false)          # TensorFlow NPU frontend, unused
        ];
        # With d3d12/rusticl gone, the `spirv2dxil` output gets no files, so its
        # dir is never created and nix fails ("failed to produce output path").
        # The derivation declares it as a separate output unconditionally, so just
        # ensure the dir exists. (opencl + cross_tools are already mkdir'd/filled
        # by the stock postInstall, so they don't need this.)
        #
        # Also drop `bin/mesa-overlay-control.py` — a python-shebang helper for the
        # Vulkan overlay-HUD layer (debug utility, NOT a runtime GL/GLES component).
        # It's the sole thing referencing python3 in mesa's $out, so removing it
        # drops the whole 117 MB python3 interpreter from the image closure.
        postInstall = (old.postInstall or "") + ''
          mkdir -p "$spirv2dxil"
          rm -f "$out/bin/mesa-overlay-control.py"
        '';
      });
    })
  ];
}
