#!/usr/bin/env bash
# Build the Pi SD image (NO flash) and print its size breakdown. This is the ONE
# fixed program the hostbridge /build endpoint runs on the Mac host — a size
# measurement loop for the image trim work (no device, no write). Also runnable
# by hand from the repo root.
#
#   deploy/build_and_measure.sh [pi3|pi5] [TOP_N]
#
# board selects the target (pi3 -> //deploy:tdplayer_pi3, pi5 -> //deploy:tdplayer).
# Honors $TOXC_BAZEL for the bazel binary (the hostbridge passes its resolved
# path). The heavy lifting (image realization on the aarch64 nix builder) happens
# inside `bazel run …image_sd --no-write`.
set -uo pipefail

board="${1:-pi3}"
top_n="${2:-30}"
case "$board" in
  pi3) target="//deploy:tdplayer_pi3.image_sd" ;;
  pi5) target="//deploy:tdplayer.image_sd" ;;
  *) echo "board must be 'pi3' or 'pi5', got: $board" >&2; exit 2 ;;
esac

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bazel="${TOXC_BAZEL:-bazel}"
# Trailing X's only (BSD/macOS mktemp won't fill X's followed by a suffix).
log="$(mktemp "${TMPDIR:-/tmp}/toxc-image-build.XXXXXX")"

# Force the sbc-deploy macOS DEFAULT backend: an auto-managed aarch64-linux
# builder VM that honors $SBC_BUILDER_DISK (native, binary-cached). A stray
# SBC_CROSS in the bridge's environment would otherwise send choose_backend down
# the cross-compile path — no aarch64-linux cache hits, rebuilding mesa AND the
# RPi kernel from source. We measure the image, so we want the fast cached VM.
unset SBC_CROSS SBC_BUILD_PLATFORM

echo "== building $target (--no-write) -> $log =="
"$bazel" run "$target" -- --no-write 2>&1 | tee "$log"
rc="${PIPESTATUS[0]}"
[ "$rc" -eq 0 ] || { echo "image build FAILED (rc=$rc)" >&2; exit "$rc"; }

echo
# Size breakdown + why-depends for the whales we're chasing + the compressed size.
"$here/measure_image_size.sh" "$log" "$top_n" --zst \
  --why perl --why nix-2 --why modemmanager --why networkmanager --why systemd-257 \
  --why linux_rpi --why openssh --why iptables --why groff
