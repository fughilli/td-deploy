#!/usr/bin/env bash
# Measure the Pi SD image's size the same way CI's build-image "Image size
# breakdown" step does, but runnable by hand on whatever machine holds the built
# image in its Nix store (e.g. the Mac host builder, driven via the hostbridge).
#
# Usage:
#   # after: bazel run //deploy:tdplayer_pi3.image_sd -- --no-write | tee build.log
#   deploy/measure_image_size.sh build.log [TOP_N] [--why PKG]...
#   deploy/measure_image_size.sh /nix/store/…-nixos-image-…/sd-image/…​.img [TOP_N]
#
# Prints: raw .img size, total closure self-size, the TOP_N largest store paths,
# and (for each --why PKG) a `nix why-depends` chain from the system toplevel to
# the first closure path whose name contains PKG — so we can see WHAT drags in
# the mystery whales (llvm, the 186 MB `source`, python3, gtk3, git) before
# cutting them. Diagnostic only; never mutates anything.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
closure_top="$here/../.github/scripts/closure_top.py"

arg="${1:?usage: measure_image_size.sh <build.log|image.img> [TOP_N] [--why PKG]...}"
shift || true
top_n="30"
whys=()
do_zst=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --why) whys+=("${2:?--why needs a PKG}"); shift 2;;
    --zst) do_zst=1; shift;;
    *) top_n="$1"; shift;;
  esac
done

# Resolve the image path: a *.img directly, or scrape it from a build.log line
# ("==> Built image: <path>", printed by sbc_deploy.sh --no-write).
if [ -f "$arg" ] && [[ "$arg" == *.img ]]; then
  IMG="$arg"
else
  IMG="$(grep -oE '==> Built image: .*' "$arg" 2>/dev/null | tail -n1 | sed 's/==> Built image: //')"
fi
[ -n "${IMG:-}" ] || { echo "could not resolve image path from '$arg'" >&2; exit 1; }

echo "== raw image =="; ls -lh "$IMG" || true

# The .img lives at <store-path>/sd-image/<name>.img; the store path is its
# grandparent. Resolve that -> deriver -> the nixos-system toplevel output.
imgdir="$(cd "$(dirname "$IMG")/.." && pwd)"
drv="$(nix-store -q --deriver "$imgdir" 2>/dev/null || true)"
top=""
if [ -n "$drv" ]; then
  topdrv="$(nix-store -q --references "$drv" 2>/dev/null \
    | grep -E 'nixos-system-.*\.drv$' | head -1)"
  [ -n "$topdrv" ] && top="$(nix-store -q --outputs "$topdrv" 2>/dev/null | head -1)"
fi
echo "== toplevel: ${top:-<unresolved>} =="
[ -n "$top" ] && [ -e "$top" ] || { echo "toplevel unresolved; can't size closure" >&2; exit 1; }

echo "== total image closure size =="
nix path-info -S -h "$top" || true
echo "== $top_n largest packages in the image closure =="
nix path-info -r --json "$top" | python3 "$closure_top" "$top_n" || true

for pkg in "${whys[@]}"; do
  echo "== why-depends -> *$pkg* =="
  target="$(nix path-info -r "$top" 2>/dev/null | grep -E "\-$pkg" | head -1)"
  [ -z "$target" ] && target="$(nix path-info -r "$top" 2>/dev/null | grep "$pkg" | head -1)"
  if [ -n "$target" ]; then
    echo "target: $target"
    # --precise shows WHICH FILE in each hop references the next — so we can see
    # the exact script/lib pulling a whale (e.g. a python-shebang in mesa/bin).
    nix why-depends --precise "$top" "$target" 2>/dev/null \
      || nix why-depends "$top" "$target" 2>/dev/null \
      || nix-store -q --tree "$top" 2>/dev/null | grep -m1 "$pkg" || true
  else
    echo "no closure path matching '$pkg'"
  fi
done

# Optional: the COMPRESSED size — what the app actually downloads (build-image
# publishes a `zstd -19` .img.zst). The raw .img carries ~GB of sd-image free
# space that compresses to ~nothing, so the .zst tracks real content, not the
# raw size. zstd from PATH or `nix run nixpkgs#zstd` (CI-parity level 19).
if [ "$do_zst" = 1 ]; then
  echo "== compressed (.img.zst, zstd -19 -T0, what the app downloads) =="
  zst="$(mktemp "${TMPDIR:-/tmp}/toxc-img.XXXXXX.zst")"
  if command -v zstd >/dev/null 2>&1; then
    zstd -19 -T0 -q -f "$IMG" -o "$zst"
  else
    nix run nixpkgs#zstd -- -19 -T0 -q -f "$IMG" -o "$zst"
  fi
  ls -lh "$zst" | awk '{print "  .img.zst =", $5}'
  rm -f "$zst"
fi
