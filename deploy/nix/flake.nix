{
  # td-deploy — player images + live-deploy, as an sbc-deploy consumer.
  #
  # One flake, two board families (sbc-deploy selects the builder from the
  # sbc_application `board` attr via $SBC_BOARD_FAMILY):
  #   * raspberrypi — an SD image (images.sdImage), Pi 3/5.
  #   * x86_64      — a bootable install USB (images.installerIso) that installs
  #                   the player onto a mini PC's internal disk (UEFI/systemd-boot).
  # Both expose nixosConfigurations.tdplayer for deploy_live.
  #
  # `services.sbcApps` (from sbc-deploy) models the long-lived process — the toxc
  # native runtime streaming a compiled artifact. The application inputs (the Rust
  # runtime binary for the image's arch + the portable artifact) come from Bazel
  # via sbc-deploy's `build_data` (-> sbcBuildData in apps.nix); nothing is vendored.
  #
  # Built via `//deploy:tdplayer.*` (Pi 5), `//deploy:tdplayer_pi3.*` (Pi 3) and
  # `//deploy:tdplayer_amd64.*` (x86_64 mini PC). The sbc-deploy version is pinned
  # here (Nix side) by flake.lock in parallel with the git_override in
  # //MODULE.bazel — keep the two revs in sync.

  description = "td-deploy — Raspberry Pi + x86_64 player images and live-deploy (sbc-deploy consumer)";

  inputs.sbc-deploy.url = "github:fughilli/sbc-deploy?dir=nix";

  outputs = { self, sbc-deploy, ... }:
    let
      # Same seam sbc-deploy resolves its builder from (exported by launch.sh from
      # the board definition; read under the --impure build). Empty => Pi.
      family = builtins.getEnv "SBC_BOARD_FAMILY";
      isX86 = family == "x86_64";
    in
    sbc-deploy.lib.mkSbcProject {
      hostName = "tdplayer";
      # Default board; the Bazel sbc_application `board` attr overrides this via
      # $SBC_BOARD / $SBC_BOARD_FAMILY (so //deploy:tdplayer_pi3 targets a Pi 3 and
      # //deploy:tdplayer_amd64 an x86_64 mini PC off the same flake).
      board = "raspberry-pi-5";
      appModules = [ ./apps.nix ];
      # Baked into BOTH images (full + base) of either family:
      #   lean-extra.nix  — drop the on-device flake registry/nixPath (186 MB
      #                     nixpkgs source) + gtk3 (stoken CLI-only, 45 MB).
      #   tailscale.nix   — join the tailnet (reachable across LANs); authkey seeded
      #                     out of band via deploy/seed_tailscale.sh (adds ~30 MB).
      #   flash-config.nix — apply a per-card/per-USB hostname + WiFi + deploy key
      #                     that the desktop app's flasher dropped at flash time.
      # Raspberry Pi only:
      #   image.nix       — skip zstd compression + shrink the oversized firmware
      #                     partition (the raw-image "zero padding"). sdImage.* only
      #                     exists on the Pi (sd-image module).
      #   mesa-lean.nix   — Mesa without the LLVM software renderers (VC4/V3D
      #                     hardware only), dropping the ~507 MB llvm-*-lib closure.
      # x86_64 only:
      #   x86.nix         — KMS on the iGPU (undo the framework's nomodeset), Mesa
      #                     iris/radeonsi + Intel OpenCL (OpenVINO GPU inference).
      #   python-host.nix — Python for Python-host artifacts: the interpreter, the
      #                     venv `tdplayer-prepare` step a deploy runs, and the
      #                     libraries manylinux wheels expect.
      systemModules = [ ./lean-extra.nix ./tailscale.nix ./flash-config.nix ]
        ++ (if isX86
      then [ ./x86.nix ./python-host.nix ]
      else [ ./image.nix ./mesa-lean.nix ]);
    };
}
