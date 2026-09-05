"""
toxc optimization passes (see docs/design/tox-to-pi.md §5).

M1 subset, all operating on the plain `ir.Graph`:
  * dead_node_elim   — drop nodes not reachable from the output sink.
  * infer_format     — propagate TOP resolution/format from sources downstream.
  * constant_fold    — fold static params into compile-time constants
                       (e.g. gaussian sigma -> radius + normalized 1D weights,
                       which then get *baked into the GLSL* at lowering; this is
                       the "static application" premise — no per-frame recompute).

Each pass mutates the graph and appends human-readable notes to `report`.
The op-specific knowledge lives in small handlers keyed by `node.op`, mirroring
the kernel-registry design so coverage grows by adding entries, not editing passes.
"""
from __future__ import annotations

import math
from ir.graph import Graph


def dead_node_elim(g: Graph, report: list[str]) -> None:
    keep = g.reachable_from_output()
    dropped = [nid for nid in g.nodes if nid not in keep]
    for nid in dropped:
        del g.nodes[nid]
    if dropped:
        report.append(f"dead_node_elim: dropped {sorted(dropped)}")
    else:
        report.append("dead_node_elim: nothing to drop")


def infer_format(g: Graph, report: list[str], out_res: int = 256) -> None:
    """Assign node.out_type = {w,h,fmt}. image_in keeps its native size; crop
    rescales to the project output resolution `out_res`; other ops inherit input 0."""
    for nid in g.topo_order():
        n = g.nodes[nid]
        if n.op == "image_in":
            w = int(n.params.get("w", out_res))
            h = int(n.params.get("h", out_res))
            n.out_type = {"w": w, "h": h, "fmt": n.params.get("fmt", "rgba8")}
        elif n.op == "crop":
            fmt = g.nodes[n.inputs[0].node].out_type["fmt"] if n.inputs else "rgba8"
            n.out_type = {"w": out_res, "h": out_res, "fmt": fmt}
        elif n.inputs:
            src = g.nodes[n.inputs[0].node]
            if src.out_type is None:
                raise ValueError(f"{nid}: input {src.id} has no inferred type")
            n.out_type = dict(src.out_type)
        else:
            raise ValueError(f"{nid}: op {n.op!r} has no inputs and declares no format")
    report.append("infer_format: " + ", ".join(
        f"{nid.split('/')[-1]}={n.out_type['w']}x{n.out_type['h']}"
        for nid, n in g.nodes.items()))


def _gaussian_weights(sigma: float, cap: int = 20) -> tuple[int, list[float]]:
    radius = max(1, min(cap, math.ceil(3.0 * sigma)))
    xs = range(-radius, radius + 1)
    raw = [math.exp(-(x * x) / (2.0 * sigma * sigma)) for x in xs]
    s = sum(raw)
    return radius, [w / s for w in raw]


def constant_fold(g: Graph, report: list[str]) -> None:
    for nid, n in g.nodes.items():
        if n.op == "gaussian_blur":
            sigma = float(n.params.get("sigma", 4.0))
            radius, weights = _gaussian_weights(sigma)
            n.params["_radius"] = radius
            n.params["_weights"] = weights
            report.append(
                f"constant_fold: {nid} sigma={sigma} -> radius={radius}, "
                f"{2*radius+1} weights (baked)")


def optimize(g: Graph, out_res: int = 256) -> list[str]:
    report: list[str] = []
    dead_node_elim(g, report)
    infer_format(g, report, out_res=out_res)
    constant_fold(g, report)
    return report
