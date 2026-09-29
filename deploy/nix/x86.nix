# x86_64 mini-PC player hardware (Intel/AMD iGPU), layered on sbc-deploy's
# generic amd64 target (nix/modules/x86-target.nix: UEFI + systemd-boot +
# by-label root/ESP).
#
# The player drives the projector itself: toxc-runtime renders with desktop GL
# 3.3 on the iGPU and scans out through GBM/KMS (sink.rs / scanout.rs). That
# needs the GPU's KMS driver (i915/xe/amdgpu) bound — which the framework's
# default `nomodeset` (kept on the EFI framebuffer so a headless box's console
# never garbles) prevents: no /dev/dri/card*, no render node, llvmpipe at best.
{ config, lib, pkgs, ... }:
{
  # Undo the framework's nomodeset. kernelParams is a merged list with no way to
  # remove one element, so restate the whole list (x86-target.nix's nomodeset was
  # its only contribution; kernel.nix's loglevel is kept here).
  boot.kernelParams = lib.mkForce [
    "loglevel=${toString config.boot.consoleLogLevel}"
    "consoleblank=0" # a show box never blanks its output
  ];
  # Mesa (iris for Intel Gen8+, radeonsi for AMD) system-wide under
  # /run/opengl-driver, plus Intel's OpenCL runtime: OpenVINO's GPU plugin (the
  # trickster matte network) runs on it, falling back to CPU on AMD.
  hardware.graphics = {
    enable = true;
    extraPackages = with pkgs; [ intel-compute-runtime intel-media-driver ];
  };

  # The iGPU's firmware (amdgpu, i915 DMC/GuC/HuC) and the NIC/Wi-Fi blobs
  # (r8169, rtw88, ...) come from linux-firmware. NixOS leaves it out unless
  # enableRedistributableFirmware is on (default false; only nixos-generate-config
  # and the installer ISO turn it on), and sbc-base doesn't set it for x86 — so
  # without this amdgpu fails ("Failed to get gpu_info firmware
  # picasso_gpu_info.bin") and the runtime falls back to llvmpipe on simpledrm.
  hardware.enableRedistributableFirmware = true;

  # A show box never sleeps.
  systemd.targets.sleep.enable = false;
  systemd.targets.suspend.enable = false;
  systemd.targets.hibernate.enable = false;
  systemd.targets.hybrid-sleep.enable = false;
  services.logind.lidSwitch = "ignore";
  console.earlySetup = true;

  # USB webcams (UVC) for camera-driven pieces; the runtime user is in `video`.
  environment.systemPackages = [ pkgs.v4l-utils ];

  # Optional, per box: AMD APU power limits. Laptop-class APUs in mini PCs ship
  # with conservative limits (a Ryzen 5 3500U box idled its Vega GPU at ~300 MHz
  # and its cores at ~1.6 GHz under a camera piece's load); raising them trades
  # heat for clocks. Nothing is changed unless /var/lib/tdplayer/power-limits
  # exists — it holds ryzenadj arguments, e.g.
  #   --stapm-limit=25000 --fast-limit=30000 --slow-limit=25000 --tctl-temp=90
  # (milliwatts / °C). Keep to the power limits: raising the VRM current limits
  # (--vrm-current / --vrmmax-current) hung that box. The SMU forgets the
  # settings at every boot (and some firmware resets them), so they are applied
  # at boot and re-applied every 10 minutes.
  systemd.services.td-power-limits = {
    description = "Apply per-box AMD APU power limits (ryzenadj)";
    wantedBy = [ "multi-user.target" ];
    unitConfig.ConditionPathExists = "/var/lib/tdplayer/power-limits";
    serviceConfig.Type = "oneshot";
    path = [ pkgs.gnugrep pkgs.coreutils ];
    script = ''
      grep -q AuthenticAMD /proc/cpuinfo || exit 0
      # shellcheck disable=SC2046
      ${pkgs.ryzenadj}/bin/ryzenadj $(grep -v '^#' /var/lib/tdplayer/power-limits) 2>&1 \
        | grep -iv 'pm_table\|/dev/mem\|memory access\|ryzen_smu\|monitoring' || true
    '';
  };
  systemd.timers.td-power-limits = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnUnitActiveSec = "10min";
      OnBootSec = "2min";
    };
  };
}
