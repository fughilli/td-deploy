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
{ ... }:
{
  sdImage.compressImage = false;
}
