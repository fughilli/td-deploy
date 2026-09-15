#!/usr/bin/env python3
"""Print the N largest store paths by self (NAR) size from `nix path-info --json`.

Nix's human `-h` sizes ("845.7 MiB") don't sort with `sort -h`, and `--all`
includes build tooling; this reads the JSON, sorts numerically, and prints a clean
top-N so we can see what actually dominates the image closure.

Usage:  nix path-info -r --json <toplevel> | closure_top.py [N]
"""

import json
import sys


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    data = json.load(sys.stdin)
    # `nix path-info --json` is a dict{path: info} on newer Nix, a list on older.
    items = data.items() if isinstance(data, dict) else [(x["path"], x) for x in data]
    rows = sorted(((int(v.get("narSize") or 0), k) for k, v in items), reverse=True)
    total = sum(sz for sz, _ in rows)
    print(f"total closure self-size: {total / 1e6:.0f} MB across {len(rows)} paths")
    for sz, path in rows[:n]:
        print(f"{sz / 1e6:9.1f} MB  {path.split('/')[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
