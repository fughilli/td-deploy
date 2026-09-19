#!/usr/bin/env bash
# Assemble the host cross-toolchain bundle the app ships (app/toolchain/):
#
#   toolchain/
#     bin/{mlir-opt,mlir-translate,clang,clang++,ld.lld,glslangValidator,spirv-cross}
#     lib/*                         bundled shared libs (see below)
#
# No sysroot: finish cross-links the kernels with `-nostdlib` and leaves libm
# undefined (resolved on the Pi at dlopen) — see deploy_engine/toolchain.py.
#
# The MLIR tools + glslang/spirv-cross are built from nixpkgs and link
# DYNAMICALLY against LLVM/MLIR and the C++ runtime in /nix/store. Copying just
# the executable ships a binary whose dylibs are missing on the user's machine,
# so it aborts at launch (SIGABRT) — exactly the failure this fixes. So we bundle
# each tool's non-system shared-library closure into lib/ and repoint the load
# paths to be relative to the binary (@loader_path on macOS, $ORIGIN on Linux),
# mirroring how the Windows bundle ships its DLLs. A final check fails the build
# if anything still references /nix/store. clang/clang++/ld.lld come from the
# official LLVM release and link only system libs, so the closure walk leaves
# them untouched.
#
# This is the OS-scriptable part (macOS/Linux). It expects the individual pieces
# to have been fetched/built into $STAGE by the caller (CI) and lays them out
# with the right names, bundles their libs, and sanity-checks. Windows uses
# assemble_toolchain.ps1.
#
#   STAGE=<dir with the fetched pieces> OUT=app/toolchain assemble_toolchain.sh
set -euo pipefail
STAGE="${STAGE:?set STAGE to the dir holding mlir/clang/glslang pieces}"
OUT="${OUT:?set OUT to the toolchain output dir (e.g. app/toolchain)}"

mkdir -p "$OUT/bin"

copy() {  # copy <src> <dst-name>   (resolves symlinks; keeps exec bit)
  local src="$1" dst="$2"
  [ -e "$src" ] || { echo "MISSING: $src" >&2; return 1; }
  cp -L "$src" "$OUT/bin/$dst"
  chmod +x "$OUT/bin/$dst"
}

# MLIR tools (built from compiler/nix on macOS/Linux; from source on Windows).
copy "$STAGE/mlir/mlir-opt"        mlir-opt
copy "$STAGE/mlir/mlir-translate"  mlir-translate

# clang + lld (from the official LLVM release for this OS).
copy "$STAGE/llvm/clang"    clang
copy "$STAGE/llvm/clang++"  clang++ || true
copy "$STAGE/llvm/ld.lld"   ld.lld

# GLES shader translators. translate_gles.py invokes `glslangValidator` +
# `spirv-cross`, so the bundled names must match exactly.
copy "$STAGE/glslang/glslangValidator" glslangValidator
copy "$STAGE/spirv-cross/spirv-cross"  spirv-cross

# --- bundle the /nix/store dylib closure so the tools run standalone ----------

bundle_macos() {
  local libdir="$OUT/lib"
  mkdir -p "$libdir"
  local -a work=()
  local b
  for b in "$OUT"/bin/*; do work+=("$b"); done

  # BFS the closure: copy every /nix/store dylib a scanned Mach-O loads, then
  # scan the copies too (their own deps).
  local i=0 f dep base
  while [ "$i" -lt "${#work[@]}" ]; do
    f="${work[$i]}"
    i=$((i + 1))
    while IFS= read -r dep; do
      case "$dep" in
        /nix/store/*)
          base="$(basename "$dep")"
          if [ ! -e "$libdir/$base" ]; then
            cp "$dep" "$libdir/$base"
            chmod u+w "$libdir/$base"
            work+=("$libdir/$base")
          fi
          ;;
      esac
    done < <(otool -L "$f" | awk 'NR>1 {print $1}')
  done

  # Repoint every Mach-O (bin/ + lib/) at the bundled copies.
  local rel
  for f in "$OUT"/bin/* "$libdir"/*; do
    [ -f "$f" ] || continue
    case "$f" in
      "$libdir"/*) install_name_tool -id "@loader_path/$(basename "$f")" "$f" ;;
    esac
    while IFS= read -r dep; do
      case "$dep" in
        /nix/store/*)
          base="$(basename "$dep")"
          case "$f" in
            "$OUT"/bin/*) rel="@loader_path/../lib/$base" ;;
            *) rel="@loader_path/$base" ;;
          esac
          install_name_tool -change "$dep" "$rel" "$f"
          ;;
      esac
    done < <(otool -L "$f" | awk 'NR>1 {print $1}')
  done
}

bundle_linux() {
  if ! command -v patchelf >/dev/null 2>&1; then
    echo "WARN: patchelf not found; skipping Linux dylib bundling (dev only)" >&2
    return 0
  fi
  local libdir="$OUT/lib"
  mkdir -p "$libdir"
  local -a work=()
  local b
  for b in "$OUT"/bin/*; do work+=("$b"); done

  local i=0 f so base
  while [ "$i" -lt "${#work[@]}" ]; do
    f="${work[$i]}"
    i=$((i + 1))
    while IFS= read -r so; do
      case "$so" in
        /nix/store/*)
          base="$(basename "$so")"
          if [ ! -e "$libdir/$base" ]; then
            cp "$so" "$libdir/$base"
            chmod u+w "$libdir/$base"
            work+=("$libdir/$base")
          fi
          ;;
      esac
    done < <(ldd "$f" 2>/dev/null | awk '{print $3}' | grep -E '^/nix/store/' || true)
  done

  for f in "$OUT"/bin/*; do patchelf --set-rpath '$ORIGIN/../lib' "$f" || true; done
  for f in "$libdir"/*; do
    [ -f "$f" ] || continue
    patchelf --set-rpath '$ORIGIN' "$f" || true
  done
}

# Fail the build if any bundled binary/lib still loads from /nix/store — this is
# the guard that would have caught the SIGABRT this script fixes.
check_self_contained() {
  local f bad=0
  case "$(uname)" in
    Darwin)
      for f in "$OUT"/bin/* "$OUT"/lib/*; do
        [ -f "$f" ] || continue
        if otool -L "$f" | awk 'NR>1 {print $1}' | grep -q '^/nix/store/'; then
          echo "NOT self-contained: $f still references /nix/store:" >&2
          otool -L "$f" | grep '/nix/store/' >&2 || true
          bad=1
        fi
      done
      ;;
    Linux)
      command -v ldd >/dev/null 2>&1 || return 0
      for f in "$OUT"/bin/* "$OUT"/lib/*; do
        [ -f "$f" ] || continue
        if ldd "$f" 2>/dev/null | grep -q '/nix/store/'; then
          echo "NOT self-contained: $f still references /nix/store:" >&2
          ldd "$f" | grep '/nix/store/' >&2 || true
          bad=1
        fi
      done
      ;;
  esac
  [ "$bad" -eq 0 ] || { echo "toolchain bundle is not self-contained" >&2; exit 1; }
}

case "$(uname)" in
  Darwin) bundle_macos ;;
  Linux) bundle_linux ;;
  *) echo "WARN: unknown OS $(uname); skipping dylib bundling" >&2 ;;
esac
check_self_contained

echo "==> toolchain assembled at $OUT"
ls -l "$OUT/bin"
if [ -d "$OUT/lib" ]; then ls -l "$OUT/lib"; fi
