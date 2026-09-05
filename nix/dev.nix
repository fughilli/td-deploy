# toxc host dev/runtime environment.
#   Python (numpy, pillow, pyopengl) + Mesa configured for headless EGL-surfaceless
#   software GL (llvmpipe). This is the HOST reference-runtime context; the Pi uses
#   real EGL/GLES on the V3D with the same GL calls.
#
# Enter with:  nix/dev.sh <cmd...>     (see dev.sh)
let
  pkgs = (builtins.getFlake "nixpkgs").legacyPackages.${builtins.currentSystem};
  py = pkgs.python3.withPackages (ps: with ps; [ numpy pillow pyopengl ]);
in
pkgs.mkShell {
  packages = [ py ];
  shellHook = ''
    export PYOPENGL_PLATFORM=egl
    export LIBGL_ALWAYS_SOFTWARE=1
    export EGL_PLATFORM=surfaceless
    export __EGL_VENDOR_LIBRARY_DIRS=${pkgs.mesa}/share/glvnd/egl_vendor.d
    export LD_LIBRARY_PATH=${pkgs.lib.makeLibraryPath [ pkgs.libGL pkgs.mesa pkgs.libdrm ]}:$LD_LIBRARY_PATH
  '';
}
