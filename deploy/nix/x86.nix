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

  # The iGPU's firmware (i915 DMC/GuC/HuC, amdgpu) comes from linux-firmware,
  # which sbc-base keeps on x86 (its leanFirmware trim is aarch64-only).

  # A show box never sleeps.
  systemd.targets.sleep.enable = false;
  systemd.targets.suspend.enable = false;
  systemd.targets.hibernate.enable = false;
  systemd.targets.hybrid-sleep.enable = false;
  services.logind.lidSwitch = "ignore";
  console.earlySetup = true;

  # USB webcams (UVC) for camera-driven pieces; the runtime user is in `video`.
  environment.systemPackages = [ pkgs.v4l-utils ];
}
