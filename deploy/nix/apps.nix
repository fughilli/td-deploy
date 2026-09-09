# toxc runtime application unit for the sbc-deploy Pi image.
#
# Consumes two Bazel-built inputs via sbc-deploy's `build_data` (keyed by
# basename in `sbcBuildData`):
#   * "toxc_runtime"  — the dynamic aarch64 Rust runtime (//runtime_rs).
#   * "toxc_artifact" — the PORTABLE, arch-independent compiled artifact
#                       (//deploy:toxc_artifact): schedule.json + shaders/ +
#                       assets/ + exprs.mlir + services.json.
#
# This module finishes the artifact for THIS image's arch (compiles exprs.mlir
# -> exprs/libexprs.so with the image's own LLVM 18), autoPatchelfs the runtime
# onto NixOS, and wires it up as a `services.sbcApps` unit.
{ pkgs, lib, sbcBuildData ? { }, ... }:
let
  runtimeBin = sbcBuildData."toxc_runtime" or (throw
    "toxc_runtime binary missing from build_data — add //runtime_rs:toxc_runtime "
    + "to sbc_application(build_data=…).");
  artifactSrc = sbcBuildData."toxc_artifact" or (throw
    "toxc_artifact missing from build_data — add //deploy:toxc_artifact "
    + "to sbc_application(build_data=…).");

  # Baked output config for this image.
  port = 8788;
  fps = 30;
  # Force Mesa llvmpipe (CPU GL): required on the Pi 3 (VC4 = GLES2-only, can't
  # run these desktop/GLES3 shaders in HARDWARE) and safe on the Pi 5. Flip to
  # false on a Pi 5 with a gles2 artifact to use the V3D GPU.
  softwareGL = true;

  glLibs = [ pkgs.mesa pkgs.libglvnd pkgs.libdrm ];
  mlir = pkgs.llvmPackages_18;

  # Finish the portable artifact for aarch64: exprs.mlir -> exprs/libexprs.so,
  # using the image's own LLVM 18 + clang (correct target arch, not the host
  # that emitted the arch-independent artifact under Bazel).
  artifact = pkgs.runCommand "toxc-artifact"
    { nativeBuildInputs = [ mlir.mlir mlir.clang ]; } ''
    cp -r ${artifactSrc} "$out"
    chmod -R u+w "$out"
    if [ -s "$out/exprs.mlir" ]; then
      mkdir -p "$out/exprs"
      mlir-opt "$out/exprs.mlir" --convert-math-to-llvm --convert-arith-to-llvm \
        --convert-func-to-llvm --reconcile-unrealized-casts -o "$out/exprs/low.mlir"
      mlir-translate "$out/exprs/low.mlir" --mlir-to-llvmir -o "$out/exprs/exprs.ll"
      clang -O2 -shared -fPIC "$out/exprs/exprs.ll" -o "$out/exprs/libexprs.so" -lm
    fi
  '';

  # The runtime: a dynamic aarch64 binary from Bazel. autoPatchelf fixes the ELF
  # interpreter + rpath for NixOS; makeWrapper adds the dlopen'd Mesa EGL/GL libs
  # + the headless-EGL env, and bakes the `stream <artifact> <port> <fps>` args.
  toxcPkg = pkgs.stdenv.mkDerivation {
    name = "toxc-runtime";
    dontUnpack = true;
    nativeBuildInputs = [ pkgs.autoPatchelfHook pkgs.makeWrapper ];
    buildInputs = [ pkgs.stdenv.cc.cc.lib ] ++ glLibs;
    installPhase = ''
      install -Dm755 ${runtimeBin} "$out/libexec/toxc-runtime"
    '';
    postFixup = ''
      makeWrapper "$out/libexec/toxc-runtime" "$out/bin/toxc-runtime" \
        --prefix LD_LIBRARY_PATH : ${lib.makeLibraryPath glLibs} \
        --set EGL_PLATFORM surfaceless \
        --set __EGL_VENDOR_LIBRARY_DIRS ${pkgs.mesa}/share/glvnd/egl_vendor.d \
        --set MESA_SHADER_CACHE_DISABLE true \
        ${lib.optionalString softwareGL "--set LIBGL_ALWAYS_SOFTWARE 1"} \
        --add-flags stream \
        --add-flags ${artifact} \
        --add-flags ${toString port} \
        --add-flags ${toString fps}
    '';
  };
in
{
  services.sbcApps.tdplayer = {
    description = "toxc native media-graph runtime (MJPEG stream)";
    package = toxcPkg;
    exec = "bin/toxc-runtime";
    user = "tdplayer";
    # `audio` grants read access to /dev/snd/midiC*D* — the raw ALSA MIDI device
    # the runtime reads for a MIDI In CHOP (e.g. a Midi Fighter Twister on USB).
    # `video`/`render` grant /dev/dri access; llvmpipe (softwareGL) doesn't need
    # it, but it silences the EGL "failed to open /dev/dri/card0" probe warning
    # and is required for the VC4-hardware gles2 path (softwareGL=false).
    extraGroups = [ "audio" "video" "render" ];
    ports = [ port ];
    after = [ "network.target" ];
    # CAP_SYS_ADMIN lets the runtime become DRM master (drmSetMaster) to modeset
    # the HDMI output (sink.rs). Needed because it's a system service, not a
    # logind session. Harmless if the display becomes an implicit master on open.
    extraServiceConfig = {
      AmbientCapabilities = [ "CAP_SYS_ADMIN" ];
      CapabilityBoundingSet = [ "CAP_SYS_ADMIN" ];
    };
  };
}
