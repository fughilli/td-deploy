#!/usr/bin/env bash
# Enter the toxc MLIR/LLVM compiler toolchain (pinned nixos-24.11) and run a command.
#   compiler/nix/shell.sh <cmd...>   (no args = interactive shell)
set -euo pipefail
export PATH=/nix/var/nix/profiles/default/bin:$PATH
here="$(cd "$(dirname "$0")" && pwd)"
expr="$(cat "$here/mlir.nix")"
if [ "$#" -eq 0 ]; then
  exec nix develop --impure --expr "$expr"
fi
exec nix develop --impure --expr "$expr" --command "$@"
