#!/usr/bin/env bash
# Enter the toxc Rust runtime env (rustc/cargo + Mesa GL) and run a command.
set -euo pipefail
export PATH=/nix/var/nix/profiles/default/bin:$PATH
here="$(cd "$(dirname "$0")" && pwd)"
expr="$(cat "$here/rust.nix")"
if [ "$#" -eq 0 ]; then
  exec nix develop --impure --expr "$expr"
fi
exec nix develop --impure --expr "$expr" --command "$@"
