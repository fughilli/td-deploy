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
      # Baked into BOTH images (full + base):
      #   image.nix    — skip zstd compression for a much faster image build.
      #   mesa-lean.nix — build Mesa without the LLVM software renderers (VC4/V3D
      #                   hardware only), dropping the ~507 MB llvm-*-lib closure.
      #   lean-extra.nix — drop the on-device flake registry/nixPath (186 MB
      #                   nixpkgs source) + gtk3 (stoken CLI-only, 45 MB).
      #   tailscale.nix — join the tailnet (reachable across LANs); authkey seeded
      #                   out of band via deploy/seed_tailscale.sh (adds ~30 MB).
      systemModules = [ ./image.nix ./mesa-lean.nix ./lean-extra.nix ./tailscale.nix ];
    };
}
