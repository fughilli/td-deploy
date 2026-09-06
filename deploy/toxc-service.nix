# NixOS module for deploying the toxc runtime on an sbc-deploy Pi image.
# Drop into an sbc-deploy Pi3 config: `imports = [ ./toxc-service.nix ];` then
#   services.toxc = { enable = true; artifact = ./my_artifact; };
#
# Pi3 note: the VideoCore IV GPU is GLES 2.0 only, so the GLES-3.x / desktop-3.3
# shaders can't run on its HARDWARE. softwareGL forces Mesa llvmpipe (CPU) — the
# same desktop-GL-3.3 path verified bit-exact in-container. HW acceleration would
# need a Pi4/Pi5 (V3D/GLES 3.1) + desktop-GLSL->GLES shader translation.
{ config, lib, pkgs, ... }:
let
  cfg = config.services.toxc;
  toxc-runtime = pkgs.callPackage ../runtime_rs/default.nix { };
in
{
  options.services.toxc = {
    enable = lib.mkEnableOption "toxc native media-graph runtime";
    artifact = lib.mkOption {
      type = lib.types.path;
      description = "Compiled artifact directory (schedule.json + shaders + assets + exprs).";
    };
    port = lib.mkOption { type = lib.types.port; default = 8788; };
    fps = lib.mkOption { type = lib.types.int; default = 30; };
    softwareGL = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Force Mesa llvmpipe (required on Pi3/VC4 which lacks GLES3/desktop GL3.3).";
    };
    openFirewall = lib.mkOption { type = lib.types.bool; default = true; };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ toxc-runtime pkgs.mesa ];
    systemd.services.toxc = {
      description = "toxc native media-graph runtime";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      serviceConfig = {
        ExecStart = "${toxc-runtime}/bin/toxc-runtime stream ${cfg.artifact} "
          + "${toString cfg.port} ${toString cfg.fps}";
        Restart = "always";
        RestartSec = 2;
        DynamicUser = true;
        Environment = [
          "EGL_PLATFORM=surfaceless"
          "LD_LIBRARY_PATH=${lib.makeLibraryPath [ pkgs.mesa pkgs.libglvnd ]}"
          "__EGL_VENDOR_LIBRARY_DIRS=${pkgs.mesa}/share/glvnd/egl_vendor.d"
        ] ++ lib.optional cfg.softwareGL "LIBGL_ALWAYS_SOFTWARE=1";
      };
    };
    networking.firewall.allowedTCPPorts = lib.optional cfg.openFirewall cfg.port;
  };
}
