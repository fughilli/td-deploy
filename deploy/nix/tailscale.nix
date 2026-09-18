# Tailscale membership for the tdplayer board — so it's reachable over the tailnet
# even when it's on a different LAN than the operator (mDNS `<host>.local` only
# works on the same L2). Modeled on splanc's pi/hitl tailscale setup.
#
# The auth key is NEVER baked into the image / nix store (per sbc-base's secrets
# policy). It's pre-seeded OUT OF BAND to authKeyFile; tailscaled-autoconnect then
# runs `tailscale up` from it on a fresh state dir (a no-op once logged in). Seed
# it from a machine on the board's LAN with `deploy/seed_tailscale.sh <host>
# <tskey-…>` (writes the key + restarts autoconnect over the deploy SSH). tailscaled
# persists its node key under /var/lib/tailscale, so membership survives reboots
# and redeploys without re-seeding.
#
# Cost: tailscale adds ~30 MB to the closure — an intentional trade for
# cross-LAN reachability on top of the lean image.
{ config, lib, ... }:
{
  services.tailscale = {
    enable = true;
    # Device-side path; seed the key here out of band (see seed_tailscale.sh).
    authKeyFile = "/var/lib/tailscale/authkey";
    # --ssh: reach the board over the tailnet with tailnet identity. The tailnet
    # hostname IS the system hostname (the board identity from identity.nix), so
    # it appears as e.g. `tdplayer2`.
    extraUpFlags = [ "--ssh" "--hostname=${config.networking.hostName}" ];
  };

  # Reach the board's services (SSH, the :8788 stream) over the tailnet without
  # opening them to the LAN.
  networking.firewall.trustedInterfaces = [ "tailscale0" ];

  # `tailscale up` only sets --hostname on a FRESH login; pin it on every boot with
  # `tailscale set` (a no-op once correct) so the tailnet name tracks the system
  # hostname across redeploys.
  systemd.services.tailscale-hostname = {
    description = "Pin the tailscale device hostname to the system hostname";
    after = [ "tailscaled.service" "tailscaled-autoconnect.service" ];
    wants = [ "tailscaled.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      ts=${config.services.tailscale.package}/bin/tailscale
      for _ in $(seq 1 30); do "$ts" status >/dev/null 2>&1 && break; sleep 2; done
      # Use the LIVE hostname, not the eval-time config value: flash-config.nix may
      # have overridden it per-card from /boot/firmware/td-hostname, and the tailnet
      # name should track that.
      "$ts" set --hostname="$(cat /proc/sys/kernel/hostname)" || true
    '';
  };
}
