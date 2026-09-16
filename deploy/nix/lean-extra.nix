# Extra image-size trims beyond the `lean` seam + mesa-lean.nix, driven by the
# `nix why-depends` breakdown of the built image. Baked into BOTH images.
{ lib, ... }:
{
  # `source` (~186 MB, the full nixpkgs source) is pulled ONLY via the on-device
  # flake registry / nixPath (`etc → nix/registry.json → source`). This appliance
  # deploys by `nix copy` of a prebuilt closure + switch — it never resolves
  # nixpkgs by flake ref on-device — so empty both. (Both options always exist,
  # so this can't error on a missing option; `nix` itself stays for the
  # store/switch path.)
  nix.registry = lib.mkForce { };
  nix.nixPath = lib.mkForce [ ];

  # No cellular modem on this appliance. NetworkManager enables ModemManager by
  # default (`networking.modemmanager.enable = mkDefault true`); turn it off. WiFi
  # (NetworkManager + wpa_supplicant) is unaffected. Drops modemmanager (~15 MB).
  networking.modemmanager.enable = false;

  # Drop the on-device installer tools — nixos-rebuild / nixos-option /
  # nixos-generate-config / nixos-install. `deploy_live` builds on the operator,
  # `nix copy`s the closure to the board, and runs `switch-to-configuration` there
  # (which stays); the board never runs nixos-rebuild itself. Removing these also
  # drops man-db + groff, which `nixos-option` baked into its PATH.
  system.disableInstallerTools = true;

  nixpkgs.overlays = [
    (final: prev: {
      # gtk+3 (45 MB) is pulled by NetworkManager's built-in openconnect VPN helper
      # (the NM package embeds a store ref to openconnect via fix-paths.patch) ->
      # openconnect -> stoken -> gtk3. stoken only needs GTK for its GUI; build the
      # CLI-only SecurID lib so gtk3 leaves the closure (NM/openconnect unaffected).
      stoken = prev.stoken.override { withGTK3 = false; };

      # ModemManager (15 MB) + libqmi/libmbim are pulled by NM's WWAN cellular
      # plugin (`libnm-wwan.so`), which nixpkgs builds unconditionally
      # (`-Dmodem_manager=true`, hardcoded). This appliance has no cellular modem,
      # so build NM without it — meson takes the last -D, so appending false wins.
      # WiFi/ethernet are unaffected. (Also see networking.modemmanager.enable=false
      # above, which drops the service side.)
      networkmanager = prev.networkmanager.overrideAttrs (old: {
        mesonFlags = (old.mesonFlags or [ ]) ++ [ "-Dmodem_manager=false" ];
      });
    })
  ];
}
