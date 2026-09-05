# toxc native Rust runtime dev env — rustc/cargo + Mesa for headless
# EGL-surfaceless GL (llvmpipe in-container; the Pi swaps to V3D/GLES). Mirrors
# the Python runtime's GL setup so the two can be conformance-diffed.
let
  pkgs = (builtins.getFlake "nixpkgs").legacyPackages.${builtins.currentSystem};
  lib = pkgs.lib;
in
pkgs.mkShell {
  packages = [
    pkgs.rustc pkgs.cargo pkgs.pkg-config pkgs.gcc
    pkgs.libGL pkgs.mesa pkgs.libdrm
  ];
  shellHook = ''
    export LIBGL_ALWAYS_SOFTWARE=1
    export EGL_PLATFORM=surfaceless
    export __EGL_VENDOR_LIBRARY_DIRS=${pkgs.mesa}/share/glvnd/egl_vendor.d
    export LD_LIBRARY_PATH=${lib.makeLibraryPath [ pkgs.libGL pkgs.mesa pkgs.libdrm ]}:$LD_LIBRARY_PATH
  '';
}
