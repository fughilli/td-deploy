{
  # td-deploy — Raspberry Pi image + live-deploy, as an sbc-deploy consumer.
  #
  # `mkSbcProject` builds the base + full SD images and the live-switch config;
  # `services.sbcApps` (from sbc-deploy) models the long-lived Pi process — the
  # toxc native runtime streaming a compiled artifact. The application inputs
  # (the Rust runtime binary + the portable artifact) come from Bazel via
  # sbc-deploy's `build_data` (-> sbcBuildData in apps.nix); nothing is vendored.
  #
  # Built via `//deploy:tdplayer.*` / `//deploy:tdplayer_pi3.*`. The sbc-deploy
  # version is pinned here (Nix side) by flake.lock in parallel with the
  # git_override in //MODULE.bazel — keep the two revs in sync.

  description = "td-deploy — Raspberry Pi image + live-deploy (sbc-deploy consumer)";

  inputs.sbc-deploy.url = "github:fughilli/sbc-deploy/build-data?dir=nix";

  outputs = { self, sbc-deploy, ... }:
    sbc-deploy.lib.mkSbcProject {
      hostName = "tdplayer";
      # Default board; the Bazel sbc_application `board` attr overrides this via
      # $SBC_BOARD (so //deploy:tdplayer_pi3 targets a Pi 3 off the same flake).
      board = "raspberry-pi-5";
      appModules = [ ./apps.nix ];
      # Baked into BOTH images (full + base): skip zstd compression for a much
      # faster image build (see image.nix). Uncompressed .img, flashed as-is.
      systemModules = [ ./image.nix ];
    };
}
