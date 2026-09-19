# Apply per-card configuration chosen at flash time (hostname + WiFi).
#
# The image is a fixed NixOS build, so the hostname and WiFi creds can't be baked
# per-card. Instead the desktop app's flasher, right after the raw image write,
# drops ready-to-use artifacts onto the FAT `/boot/firmware` partition:
#
#   /boot/firmware/td-hostname                      one line: the chosen hostname
#   /boot/firmware/system-connections/*.nmconnection  NetworkManager keyfiles,
#                                                   one per WiFi network (rendered
#                                                   host-side from SSID/PSK)
#   /boot/firmware/authorized_keys                  SSH public key(s) the app's
#                                                   generated deploy key(s) use
#
# This oneshot installs them on boot: it sets the live hostname, copies the WiFi
# keyfiles into NetworkManager's store with the perms NM requires (0600 root),
# and merges the deploy pubkey(s) into root's authorized_keys so the app can ssh
# in (a fresh flash otherwise only trusts the CI-baked key the user doesn't hold).
# It runs on EVERY boot (idempotent) so the choice survives reboots without any
# on-device state, and it runs BEFORE NetworkManager/Tailscale so they come up
# with the right name and networks. Absent files → no-op, so a default flash (no
# customization) behaves exactly as before. Every step is guarded so a malformed
# drop-in can never fail the boot.
{ config, lib, pkgs, ... }:
{
  systemd.services.td-flash-config = {
    description = "Apply flash-time hostname + WiFi + deploy key from /boot/firmware";
    wantedBy = [ "multi-user.target" ];
    # The boot FAT partition must be mounted; come up before the network stack so
    # the hostname/networks are in place when NetworkManager and Tailscale start.
    unitConfig.RequiresMountsFor = [ "/boot/firmware" ];
    before = [ "NetworkManager.service" "tailscaled-autoconnect.service" "tailscale-hostname.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    path = [ pkgs.coreutils pkgs.gnugrep pkgs.inetutils ]; # grep, `hostname`
    script = ''
      set -u
      firmware=/boot/firmware

      # --- hostname ---------------------------------------------------------
      hn="$firmware/td-hostname"
      if [ -r "$hn" ]; then
        name="$(tr -d '[:space:]' < "$hn" | tr '[:upper:]' '[:lower:]')"
        # RFC1123 label: 1..63 chars, [a-z0-9-], no leading/trailing hyphen.
        if printf '%s' "$name" | grep -Eq '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'; then
          cur="$(cat /proc/sys/kernel/hostname 2>/dev/null || true)"
          if [ "$name" != "$cur" ]; then
            echo "td-flash-config: hostname '$cur' -> '$name'"
            hostname "$name" || true
          fi
        elif [ -n "$name" ]; then
          echo "td-flash-config: ignoring invalid hostname '$name'"
        fi
      fi

      # --- WiFi (NetworkManager keyfiles) -----------------------------------
      src="$firmware/system-connections"
      dst=/etc/NetworkManager/system-connections
      if [ -d "$src" ]; then
        mkdir -p "$dst"
        for f in "$src"/*.nmconnection; do
          [ -e "$f" ] || continue
          base="$(basename "$f")"
          # Don't clobber a profile already customized on-device (e.g. seed_wifi).
          if [ -e "$dst/$base" ]; then
            echo "td-flash-config: keeping existing $base"
            continue
          fi
          echo "td-flash-config: installing WiFi profile $base"
          install -m 0600 -o root -g root "$f" "$dst/$base" || true
        done
      fi

      # --- SSH deploy key (root authorized_keys) ----------------------------
      # The app generates a deploy keypair and drops the PUBLIC half here. Merge
      # it into root's authorized_keys (append lines not already present) so a
      # freshly flashed card trusts the operator's app. Never clobbers the
      # CI-baked deploy key (which lives in authorized_keys.d) or keys added
      # on-device; sshd reads ~/.ssh/authorized_keys by default. Only well-formed
      # public-key lines are accepted.
      ak="$firmware/authorized_keys"
      if [ -r "$ak" ]; then
        install -d -m 0700 -o root -g root /root/.ssh
        target=/root/.ssh/authorized_keys
        touch "$target"; chmod 0600 "$target"; chown root:root "$target"
        while IFS= read -r line; do
          case "$line" in
            "ssh-ed25519 "*|"ssh-rsa "*|"ecdsa-sha2-"*|"sk-ssh-ed25519@openssh.com "*)
              if ! grep -qxF "$line" "$target"; then
                echo "td-flash-config: adding deploy key"
                printf '%s\n' "$line" >> "$target"
              fi
              ;;
          esac
        done < "$ak"
      fi
      exit 0
    '';
  };
}
