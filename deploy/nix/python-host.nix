# Python for Python-host artifacts (x86_64 players).
#
# A project that runs Python TouchDesigner can't compile away (Execute DATs,
# Script TOPs/CHOPs/SOPs, a pose-estimation sidecar...) deploys as a "toxc-host/1"
# artifact: the runtime spawns `python -m tdhost.serve` (pyhost/) from the
# project's own venv, `project/.venv`, and renders what it binds.
#
# The venv is built ON THE BOX at deploy time by `tdplayer-prepare <staging>`,
# which the desktop app runs over ssh before swapping the new artifact live
# (deploy_engine.push pre_restart). It installs the artifact's requirements —
# from the wheelhouse the app shipped in project/wheels when there is one
# (offline), else from PyPI — into /var/lib/tdplayer/venvs/<hash>, reused while
# the requirements are unchanged.
#
# Those are manylinux wheels (mediapipe, opencv, onnxruntime-openvino...): they
# bundle most of their native deps but expect the usual system libraries
# (libstdc++, zlib, glib, libGL, a few X libs for opencv's GUI half) on the
# loader path, which NixOS doesn't provide globally — so the runtime service
# gets them on LD_LIBRARY_PATH, inherited by its Python child and anything that
# child spawns.
{ config, lib, pkgs, ... }:
let
  python = pkgs.python312;

  wheelLibs = with pkgs; [
    stdenv.cc.cc.lib # libstdc++, libgcc_s
    zlib
    glib # libgthread / libglib (opencv)
    libGL
    libglvnd
    xorg.libX11
    xorg.libXext
    xorg.libSM
    xorg.libICE
    xorg.libxcb
    libxkbcommon
    fontconfig
    freetype
    dbus
    ocl-icd # libOpenCL.so.1 (OpenVINO GPU plugin)
  ];

  prepare = pkgs.writeScriptBin "tdplayer-prepare" (
    "#!${python}/bin/python3\n" + builtins.readFile ./tdplayer-prepare.py
  );
in
{
  environment.systemPackages = [ python prepare ];

  # The runtime service (services.sbcApps.tdplayer -> sbc-tdplayer).
  systemd.services.sbc-tdplayer = {
    # host.rs falls back to `python3` for an artifact without a venv.
    path = [ python ];
    environment = {
      LD_LIBRARY_PATH = lib.makeLibraryPath wheelLibs;
      # Intel's OpenCL ICD (hardware.graphics.extraPackages in x86.nix).
      OCL_ICD_VENDORS = "/run/opengl-driver/etc/OpenCL/vendors";
      HOME = "/var/lib/tdplayer";
      PYTHONUNBUFFERED = "1";
    };
  };
}
