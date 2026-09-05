# toxc compiler toolchain — pinned nixos-24.11 LLVM/MLIR 18 (cached, out-of-tree
# ready). The flakehub-weekly nixpkgs ships a broken MLIR patch, so pin here.
let
  pkgs = (builtins.getFlake "github:NixOS/nixpkgs/nixos-24.11").legacyPackages.${builtins.currentSystem};
  llvm = pkgs.llvmPackages_18;
in
pkgs.mkShell {
  packages = [
    pkgs.cmake pkgs.ninja pkgs.lld
    llvm.clang llvm.mlir llvm.llvm.dev
  ];
  shellHook = ''
    export MLIR_DIR=${llvm.mlir.dev}/lib/cmake/mlir
    export LLVM_DIR=${llvm.llvm.dev}/lib/cmake/llvm
    export PATH=${llvm.mlir}/bin:$PATH
    export LD_LIBRARY_PATH=${pkgs.stdenv.cc.cc.lib}/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
    echo "[toxc-mlir] MLIR_DIR=$MLIR_DIR"
  '';
}
