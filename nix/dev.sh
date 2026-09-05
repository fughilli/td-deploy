#!/usr/bin/env bash
# Enter the toxc dev/runtime env and run a command inside it.
#   nix/dev.sh python3 -m runtime.run graphs/blur_demo.json
# With no args, drops into an interactive shell.
set -euo pipefail
export PATH=/nix/var/nix/profiles/default/bin:$PATH
here="$(cd "$(dirname "$0")" && pwd)"
expr="$(cat "$here/dev.nix")"
if [ "$#" -eq 0 ]; then
  exec nix develop --impure --expr "$expr"
fi
exec nix develop --impure --expr "$expr" --command "$@"
