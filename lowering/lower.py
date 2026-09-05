"""
Lowering: ir.Graph -> RuntimePlan (docs §5).

One Step per node, in cook order. The runtime reads back the output node's
texture at the end (no explicit sink node needed — any op can be the output).

Step kinds:
  source      — produce a texture (load image / synth testcard).
  shader      — full-screen fragment pass over bound inputs -> new texture.
  passthrough — alias input 0's texture (identity ops: in/out/null/out_display,
                and — for now — crop/transform until real kernels land).

Both backends read the same plan: the GL backend uses `vertex`/`fragment`, the
CPU reference uses `op`+`params`. GL-only ops (glsl_top) have no CPU kernel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from ir.graph import Graph
from lowering import shaders

# ops that are identity on input 0 (M1: crop/transform are stubs pending kernels)
PASSTHROUGH_OPS = {"passthrough", "in", "out", "null", "out_display", "crop", "transform"}


@dataclass
class Step:
    node_id: str
    op: str
    kind: str                       # "source" | "shader" | "passthrough"
    target: dict                    # {"w","h","fmt"}
    inputs: list[str] = field(default_factory=list)
    vertex: str | None = None
    fragment: str | None = None
    sampler_array: str | None = None    # e.g. "sTD2DInputs" (else bind tex0,tex1,..)
    uniforms: dict = field(default_factory=dict)   # exact-GLSL-name -> (type, value)
    params: dict = field(default_factory=dict)


@dataclass
class RuntimePlan:
    steps: list[Step]
    output_id: str
    target: str

    def has_gl_only_ops(self) -> bool:
        return any(s.op == "glsl_top" for s in self.steps)


def _lower_node(g: Graph, nid: str, target: str) -> Step:
    n = g.nodes[nid]
    ot = n.out_type
    inputs = [p.node for p in n.inputs]

    if n.op == "image_in":
        return Step(nid, n.op, "source", ot, params=dict(n.params))

    if n.op == "gaussian_blur":
        radius, weights = n.params["_radius"], n.params["_weights"]
        return Step(nid, n.op, "shader", ot, inputs=inputs,
                    vertex=shaders.vertex(target),
                    fragment=shaders.gaussian_blur(target, radius, weights),
                    uniforms={"uResolution": ("vec2", [float(ot["w"]), float(ot["h"])])},
                    params={"_radius": radius, "_weights": weights})

    if n.op == "glsl_top":
        src = n.params.get("_shader") or shaders.passthrough(target)
        uniforms = {}
        for i, src_id in enumerate(inputs):
            it = g.nodes[src_id].out_type
            w, h = float(it["w"]), float(it["h"])
            uniforms[f"uTD2DInfos[{i}].res"] = ("vec4", [1.0 / w, 1.0 / h, w, h])
        return Step(nid, n.op, "shader", ot, inputs=inputs,
                    vertex=shaders.vertex_td(target),
                    fragment=shaders.td_glsl_top(target, len(inputs), src),
                    sampler_array="sTD2DInputs", uniforms=uniforms,
                    params=dict(n.params))

    if n.op in PASSTHROUGH_OPS:
        return Step(nid, n.op, "passthrough", ot, inputs=inputs, params=dict(n.params))

    # Unknown op: degrade to passthrough so the pipeline still runs.
    return Step(nid, n.op, "passthrough", ot, inputs=inputs, params=dict(n.params))


def lower(g: Graph, target: str = "desktop_gl") -> RuntimePlan:
    if target not in shaders.TARGETS:
        raise ValueError(f"unknown target {target!r}; pick one of {sorted(shaders.TARGETS)}")
    steps = [_lower_node(g, nid, target) for nid in g.topo_order()]
    return RuntimePlan(steps=steps, output_id=g.output, target=target)
