"""
toxc downstream driver: IR graph -> optimize -> lower -> run -> output.

    python3 -m runtime.run graphs/blur_demo.json --backend both --out out/

Runs the numpy reference and the GL backend, writes PNGs, and reports the
pixel-diff between them (the cross-validation). On the Pi the same plan runs on
the GL backend with the HDMI sink instead of a PNG.
"""
from __future__ import annotations
import argparse
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir.graph import Graph            # noqa: E402
from passes.optimize import optimize  # noqa: E402
from lowering.lower import lower       # noqa: E402


def _summarize_plan(plan) -> None:
    print("\n[plan] target =", plan.target)
    for st in plan.steps:
        extra = ""
        if st.op == "gaussian_blur":
            extra = f"  radius={st.params['_radius']} taps={(2*st.params['_radius']+1)**2}"
        frag = "" if st.fragment is None else f" glsl={len(st.fragment)}B"
        print(f"  {st.kind:6s} {st.node_id:10s} {st.op:14s} "
              f"{st.target['w']}x{st.target['h']}:{st.target['fmt']}"
              f" inputs={st.inputs}{frag}{extra}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("graph")
    ap.add_argument("--backend", choices=["both", "gl", "cpu"], default="both")
    ap.add_argument("--target", choices=["desktop_gl", "gles"], default="desktop_gl")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    g = Graph.load(args.graph)
    print(f"[ir] loaded {args.graph}: {len(g.nodes)} nodes, output={g.output!r}")

    print("\n[passes]")
    for line in optimize(g):
        print("  " + line)

    plan = lower(g, target=args.target)
    _summarize_plan(plan)

    results = {}
    want_cpu = args.backend in ("cpu", "both")
    if want_cpu and plan.has_gl_only_ops():
        print("\n[cpu] skipped — plan has GL-only ops (glsl_top); GL is the reference here")
        want_cpu = False
    if want_cpu:
        from runtime import backend_cpu
        results["cpu"] = backend_cpu.run(plan)
        Image.fromarray(results["cpu"], "RGBA").save(f"{args.out}/cpu.png")
        print(f"\n[cpu] wrote {args.out}/cpu.png")
    if args.backend in ("gl", "both"):
        from runtime import backend_gl
        results["gl"] = backend_gl.run(plan)
        Image.fromarray(results["gl"], "RGBA").save(f"{args.out}/gl.png")
        print(f"[gl]  wrote {args.out}/gl.png")

    if "gl" in results and "cpu" in results:
        a = results["gl"].astype(np.int16)
        b = results["cpu"].astype(np.int16)
        diff = np.abs(a - b)
        Image.fromarray((np.clip(diff * 8, 0, 255).astype(np.uint8)), "RGBA").save(
            f"{args.out}/diff8x.png")
        print(f"\n[validate] GL vs CPU reference:"
              f" max|Δ|={int(diff.max())} LSB, mean|Δ|={diff.mean():.4f} LSB"
              f"  ({100.0*(diff==0).mean():.2f}% pixels exact)")
        print(f"[validate] wrote {args.out}/diff8x.png (differences x8)")
        if diff.max() <= 2:
            print("[validate] PASS — backends agree within 2 LSB")
        else:
            print("[validate] WARN — divergence >2 LSB; investigate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
