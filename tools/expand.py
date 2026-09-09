"""`bazel run //:expand -- <project.toe|.tox> <out.json> [--host HOST]`

Snapshot a TouchDesigner project to the toxc IR `.json` so the rest of the
pipeline can compile it HERMETICALLY (no bridge). Expansion itself is NOT
hermetic: it uploads the `.toe`/`.tox` to the Mac TouchDesigner host bridge
(`toeexpand`, default host.docker.internal:8770) and imports the result. Commit
the emitted `.json` and build from it with `bazel run //:toxc -- <that>.json ...`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# The pipeline modules are importable as top-level packages (imports = ["."]).
from cli import _load_graph  # noqa: E402  (reuse the bridge+import path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help=".toe/.tox project (expanded via the host bridge)")
    ap.add_argument("output", help="path to write the toxc IR .json")
    ap.add_argument("--host", default=os.environ.get("TOXC_HOST", "host.docker.internal:8770"))
    ap.add_argument("--cwd", default=os.environ.get("BUILD_WORKING_DIRECTORY"))
    args = ap.parse_args()

    inp = args.input
    if args.cwd and not os.path.isabs(inp):
        inp = os.path.join(args.cwd, inp)
    g, _cov = _load_graph(inp, args.host, None)

    out = args.output
    if args.cwd and not os.path.isabs(out):
        out = os.path.join(args.cwd, out)
    with open(out, "w") as fh:
        json.dump(g.to_dict(), fh, indent=2)
    print(f"[expand] {len(g.nodes)} nodes -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
