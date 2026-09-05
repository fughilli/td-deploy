# toxc host dev/runtime environment.
#   Python (numpy, pillow, pyopengl) + (Linux only) Mesa for headless
#   EGL-surfaceless software GL (llvmpipe). This is the HOST reference-runtime
#   context; the Pi uses real EGL/GLES on the V3D with the same GL calls.
#
# The GL render path is Linux-only (EGL surfaceless + Mesa). On macOS the env
# still builds (for import/expand and non-GL work), but rendering/streaming must
# run in the Linux container (which also mirrors the Pi target). See README.
#
# Enter with:  nix/dev.sh <cmd...>     (see dev.sh)
let
  pkgs = (builtins.getFlake "nixpkgs").legacyPackages.${builtins.currentSystem};
  lib = pkgs.lib;
  isLinux = pkgs.stdenv.isLinux;
  py = pkgs.python3.withPackages (ps: with ps; [ numpy pillow pyopengl ]);
  glLibs = lib.optionals isLinux [ pkgs.libGL pkgs.mesa pkgs.libdrm ];
in
pkgs.mkShell {
  packages = [ py ] ++ glLibs;
  shellHook = lib.optionalString isLinux ''
    export PYOPENGL_PLATFORM=egl
    export LIBGL_ALWAYS_SOFTWARE=1
    export EGL_PLATFORM=surfaceless
    export __EGL_VENDOR_LIBRARY_DIRS=${pkgs.mesa}/share/glvnd/egl_vendor.d
    export LD_LIBRARY_PATH=${lib.makeLibraryPath glLibs}:$LD_LIBRARY_PATH
  '';
}
