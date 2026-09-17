#!/usr/bin/env bash
# Read-only diagnostics for a BOOTED tdplayer board. Drives the sbc-deploy `.ssh`
# target (which forwards trailing args as the remote command) with a FIXED,
# read-only bundle — no arbitrary exec. Used by the hostbridge /inspect endpoint
# to (a) verify the lean image actually runs (VC4 hardware GL, service up) and
# (b) collect the ACTUALLY-loaded kernel modules so the kernel trim removes only
# genuinely-unused modules instead of guessing at a minimal config.
#
#   deploy/inspect_device.sh [host]     # default: tdplayer.local
set -uo pipefail

host="${1:-tdplayer.local}"
bazel="${TOXC_BAZEL:-bazel}"
unset SBC_CROSS SBC_BUILD_PLATFORM   # ssh only; keep the operator env clean

# The fixed read-only bundle, run ON the board. base64-inlined so quoting/globs
# survive the bazel-run -> ssh -> remote-sh hops intact.
read -r -d '' BUNDLE <<'EOS' || true
set +e
echo "===== uname ====="; uname -a
echo "===== nixos generation ====="; readlink -f /run/current-system
echo "===== gl renderer (want VC4/v3d HARDWARE, not llvmpipe) ====="
journalctl -u sbc-tdplayer --no-pager 2>/dev/null | grep -iE '\[gl\]' | tail -n 8
echo "===== sbc-tdplayer service ====="
systemctl is-active sbc-tdplayer; systemctl status sbc-tdplayer --no-pager -n 6 2>/dev/null | tail -n 10
echo "===== deployed artifact ====="
ls -ld /var/lib/tdplayer/current 2>/dev/null && readlink -f /var/lib/tdplayer/current 2>/dev/null
echo "  shaders_gles present? (gles2 vs desktop_gl):"
ls "$(readlink -f /var/lib/tdplayer/current 2>/dev/null)"/shaders_gles 2>/dev/null | head -3 || echo "  NO shaders_gles/ (desktop_gl artifact)"
echo "===== artifact assets (did the banana bake?) ====="
art="$(readlink -f /var/lib/tdplayer/current 2>/dev/null)"
ls -la "$art/assets" 2>/dev/null
echo "===== schedule: midi/scale/crop exprs + resolutions ====="
grep -oE '"(op|expr|scale|rotate|w|h|res|path)"[[:space:]]*:[^,}]*|midiin1[^"]*|ctrl[0-9]+|uScale[XY][^,}]*|uCropRect[^]]*]' "$art/schedule.json" 2>/dev/null | head -40
echo "===== recent sbc-tdplayer journal (crash?) ====="
journalctl -u sbc-tdplayer --no-pager -n 30 2>/dev/null | tail -n 30
echo "===== runtime /stats ====="; curl -s --max-time 3 http://127.0.0.1:8788/stats 2>/dev/null | head -c 800; echo
echo "===== LOADED MODULES (lsmod) ====="; lsmod
echo "===== module count ====="; grep -c . /proc/modules
echo "===== usb devices (Twister etc.) ====="
lsusb 2>/dev/null || for p in /sys/bus/usb/devices/*/product; do [ -e "$p" ] && cat "$p"; done
echo "===== dri / drm nodes ====="; ls -l /dev/dri 2>/dev/null
echo "===== on-disk module tree size ====="
du -sh /run/current-system/kernel-modules/lib/modules/*/kernel 2>/dev/null \
  || du -sh /lib/modules/*/kernel 2>/dev/null
echo "===== mem / disk ====="; free -h; df -h / /boot/firmware 2>/dev/null
EOS

b64="$(printf '%s' "$BUNDLE" | base64 | tr -d '\n')"
echo "== inspecting $host (read-only) via the deploy ssh key =="
# ONE arg, no `sh -c` wrapper — the .ssh target hands args to `ssh`, which re-joins
# them for the REMOTE login shell to parse; a wrapped `sh -c "…"` gets mangled, but
# a single pipeline string runs correctly (the remote shell supplies the -c).
exec "$bazel" run //deploy:tdplayer_pi3.ssh -- "$host" -- "echo $b64 | base64 -d | sh"
