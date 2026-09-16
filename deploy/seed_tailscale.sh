#!/usr/bin/env bash
# Seed a Tailscale auth key onto a BOOTED tdplayer board FROM A MACHINE ON ITS LAN
# (so <host>.local resolves via mDNS). Writes the key to /var/lib/tailscale/authkey
# out of band — never in git or the nix store — then kicks tailscaled-autoconnect,
# which runs `tailscale up` from it. After this the board is on the tailnet and
# reachable by its hostname from anywhere on the tailnet, regardless of LAN.
#
#   deploy/seed_tailscale.sh <host>                 # reads deploy/secrets/tailscale-authkey
#   deploy/seed_tailscale.sh <host> <tskey-...>     # explicit key
#   TS_AUTHKEY=tskey-... deploy/seed_tailscale.sh <host>
#
# Get a reusable/ephemeral auth key from the tailnet admin console (Settings →
# Keys). Runs over the sbc-deploy deploy SSH key (the .ssh target), same as a deploy.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
host="${1:?usage: seed_tailscale.sh <host> [authkey]   (else deploy/secrets/tailscale-authkey or \$TS_AUTHKEY)}"
# Key resolution, most explicit first. The default is the gitignored secrets file,
# so the key never lands on a command line, in shell history, or in git.
key="${2:-${TS_AUTHKEY:-}}"
keyfile="$here/secrets/tailscale-authkey"
if [ -z "$key" ] && [ -f "$keyfile" ]; then
  key="$(tr -d '[:space:]' < "$keyfile")"
  echo "== using auth key from $keyfile =="
fi
[ -n "$key" ] || { echo "no auth key: pass it as arg 2, set \$TS_AUTHKEY, or put it in $keyfile" >&2; exit 2; }
case "$key" in tskey-*) ;; *) echo "warning: key doesn't look like a tskey-… key" >&2 ;; esac

bazel="${TOXC_BAZEL:-bazel}"
unset SBC_CROSS SBC_BUILD_PLATFORM   # ssh only; keep the operator env clean

# base64 the key, then the whole remote script, so both survive the
# bazel-run -> ssh -> remote-sh hops without quoting/interpolation surprises.
kb64="$(printf '%s' "$key" | base64 | tr -d '\n')"
remote="$(cat <<EOS
set -e
umask 077
mkdir -p /var/lib/tailscale
echo $kb64 | base64 -d > /var/lib/tailscale/authkey
chmod 600 /var/lib/tailscale/authkey
systemctl restart tailscaled-autoconnect.service
sleep 3
echo '== tailscale status =='; tailscale status 2>/dev/null | head -n 20 || true
echo '== tailscale IPv4 =='; tailscale ip -4 2>/dev/null || true
EOS
)"
rb64="$(printf '%s' "$remote" | base64 | tr -d '\n')"

echo "== seeding tailscale on $host (key -> /var/lib/tailscale/authkey, then autoconnect) =="
# Pass the pipeline as ONE arg (no `sh -c` wrapper): the .ssh target forwards args
# straight to `ssh`, which re-joins them with spaces so the REMOTE login shell
# parses the command — a `sh -c "…"` wrapper gets mangled (the string becomes $0),
# but a single pipeline string is run correctly by the remote shell's own -c.
exec "$bazel" run //deploy:tdplayer_pi3.ssh -- "$host" -- "echo $rb64 | base64 -d | sh"
