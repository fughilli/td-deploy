# Nix package for the toxc native runtime — the deployable form for a NixOS /
# sbc-deploy Pi image. Built for the host arch (aarch64-linux here == the Pi3),
# nix manages the closure (incl. Mesa at runtime). The binary dlopens libEGL
# (the Pi's V3D Mesa) + the artifact's libexprs.so, so nothing GL is linked here.
{ pkgs ? import <nixpkgs> { } }:
pkgs.rustPlatform.buildRustPackage {
  pname = "toxc-runtime";
  version = "0.1.0";
  src = ./.;
  cargoLock.lockFile = ./Cargo.lock;
  doCheck = false;
  meta.description = "toxc native media-graph runtime (EGL/GLES + OSC + native exprs)";
}
