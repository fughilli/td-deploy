# Image-build tuning for the toxc Pi image.
#
# Skip zstd compression of the SD image. The NixOS sd-image build otherwise
# compresses the rootfs to `ext4-fs.img.zst` AND recompresses the whole disk
# image (make-ext4-fs.nix + sd-image.nix, `zstd -T$NIX_BUILD_CORES`) — two slow
# passes over a multi-GB image, with no per-level knob exposed upstream. We have
# plenty of SD/disk headroom, so trade image size for a much faster build.
#
# Safe with the sbc-deploy flash path: sbc_deploy.sh finds `*.img` OR `*.img.zst`
# and its stream_image only runs `zstd -dc` for `.zst`, else streams the raw
# image straight to the card. (expandOnBoot still grows the rootfs to fill the
# card on first boot, so the uncompressed image needn't match the card size.)
{ lib, ... }:
{
  sdImage.compressImage = false;

  # Shrink the FAT firmware partition — the raw image's "zero padding". The Pi
  # image (nixos-raspberrypi) defaults sdImage.firmwareSize = 1024 MiB, but it
  # only holds the RPi vendor firmware + u-boot + config.txt + dtbs + extlinux
  # (~26 MB on the booted board); the kernel/initrd live in the ext4 rootfs, not
  # here. So a 1 GB FAT partition is ~1 GB of zeros between /boot/firmware and the
  # rootfs. 128 MiB is ~5x the real usage. This drops the RAW image ~2.5 → ~1.6 GB
  # (faster SD write); it doesn't change the downloaded .zst (zeros compress away).
  sdImage.firmwareSize = lib.mkForce 128;
}
