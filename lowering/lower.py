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

import math
from dataclasses import dataclass, field

from ir.graph import Graph
from lowering import shaders
from runtime.expr import value_or_expr

# ops that are identity on input 0
PASSTHROUGH_OPS = {"passthrough", "in", "out", "null", "out_display"}


def _crop_edge(params, name, unit_name, size, default):
    raw = params.get(name)
    if raw is None:
        return default
    v = float(value_or_expr(raw, default))
    unit = str(params.get(unit_name, "pixels"))
    return v / size if unit.startswith("pix") else v


@dataclass
class Step:
    node_id: str
    op: str
    kind: str  # "source" | "shader" | "passthrough"
    target: dict  # {"w","h","fmt"}
    inputs: list[str] = field(default_factory=list)
    vertex: str | None = None
    fragment: str | None = None
    sampler_array: str | None = None  # e.g. "sTD2DInputs" (else bind tex0,tex1,..)
    uniforms: dict = field(default_factory=dict)  # exact-GLSL-name -> (type, value)
    # per-frame uniforms: name -> {"expr": <literal|expr str>, "mul": float}
    time_uniforms: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)


@dataclass
class RuntimePlan:
    steps: list[Step]
    output_id: str
    target: str

    def has_gl_only_ops(self) -> bool:
        # ops with no CPU reference kernel (GL is the oracle for these)
        return any(s.op in ("glsl_top", "crop", "transform") for s in self.steps)


def _lower_node(g: Graph, nid: str, target: str) -> Step:
    n = g.nodes[nid]
    ot = n.out_type
    inputs = [p.node for p in n.inputs]

    if n.op == "image_in":
        return Step(nid, n.op, "source", ot, params=dict(n.params))

    if n.op == "gaussian_blur":
        radius, weights = n.params["_radius"], n.params["_weights"]
        return Step(
            nid,
            n.op,
            "shader",
            ot,
            inputs=inputs,
            vertex=shaders.vertex(target),
            fragment=shaders.gaussian_blur(target, radius, weights),
            uniforms={"uResolution": ("vec2", [float(ot["w"]), float(ot["h"])])},
            params={"_radius": radius, "_weights": weights},
        )

    if n.op == "glsl_top":
        src = n.params.get("_shader") or shaders.passthrough(target)
        uniforms = {}
        for i, src_id in enumerate(inputs):
            it = g.nodes[src_id].out_type
            w, h = float(it["w"]), float(it["h"])
            uniforms[f"uTD2DInfos[{i}].res"] = ("vec4", [1.0 / w, 1.0 / h, w, h])
        return Step(
            nid,
            n.op,
            "shader",
            ot,
            inputs=inputs,
            vertex=shaders.vertex_td(target),
            fragment=shaders.td_glsl_top(target, len(inputs), src),
            sampler_array="sTD2DInputs",
            uniforms=uniforms,
            params=dict(n.params),
        )

    if n.op == "crop":
        it = g.nodes[inputs[0]].out_type if inputs else {"w": 1, "h": 1}
        iw, ih = float(it["w"]), float(it["h"])
        left = _crop_edge(n.params, "cropleft", "cropleftunit", iw, 0.0)
        right = _crop_edge(n.params, "cropright", "croprightunit", iw, 1.0)
        bottom = _crop_edge(n.params, "cropbottom", "cropbottomunit", ih, 0.0)
        top = _crop_edge(n.params, "croptop", "croptopunit", ih, 1.0)
        return Step(
            nid,
            n.op,
            "shader",
            ot,
            inputs=inputs,
            vertex=shaders.vertex(target),
            fragment=shaders.crop_top(target),
            uniforms={"uCropRect": ("vec4", [left, right, bottom, top])},
            params=dict(n.params),
        )

    if n.op == "transform":

        def _p(names, default):
            for nm in names:
                if nm in n.params:
                    return value_or_expr(n.params[nm], default)  # float OR expr string
            return default

        # Every transform component can be a per-frame TD expression (e.g. scale =
        # op('midiin1')[1]/64), so lower them ALL as time-uniforms (evaluated each
        # frame via the compiled/interpreted expr path), like rotate. TD names:
        # translate tx/ty, rotate, per-axis Scale sx/sy, "Uniform Scale" scale.
        rot = _p(["rotate", "r"], 0.0)  # degrees
        uscale = _p(["scale"], 1.0)  # uniform-scale multiplier
        usc = float(uscale) if isinstance(uscale, (int, float)) else 1.0
        return Step(
            nid,
            n.op,
            "shader",
            ot,
            inputs=inputs,
            vertex=shaders.vertex(target),
            fragment=shaders.transform_top(target),
            time_uniforms={
                "uRotate": {"expr": rot, "mul": math.pi / 180.0},
                "uTranslateX": {"expr": _p(["tx", "translatex"], 0.0), "mul": 1.0},
                "uTranslateY": {"expr": _p(["ty", "translatey"], 0.0), "mul": 1.0},
                "uScaleX": {"expr": _p(["sx", "scalex"], 1.0), "mul": usc},
                "uScaleY": {"expr": _p(["sy", "scaley"], 1.0), "mul": usc},
            },
            params=dict(n.params),
        )

    if n.op in PASSTHROUGH_OPS:
        return Step(nid, n.op, "passthrough", ot, inputs=inputs, params=dict(n.params))

    # Unknown op: degrade to passthrough so the pipeline still runs.
    return Step(nid, n.op, "passthrough", ot, inputs=inputs, params=dict(n.params))


def _fuse_coord_remaps(steps: list[Step], target: str) -> list[Step]:
    """TOP producer/consumer fusion: a crop feeding ONLY a transform is two
    single-tap coordinate remaps, so compose them into one pass (source ->
    crop-UV -> transform-UV -> one sample), dropping the crop's FBO. The transform
    keeps its id, so downstream inputs are unchanged; it now samples the source.
    (glsl2->glsl3 Sobel-into-ASCII fusion is the next step — see the design doc.)"""
    consumers: dict[str, list[str]] = {}
    for s in steps:
        for inp in s.inputs:
            consumers.setdefault(inp, []).append(s.node_id)
    by_id = {s.node_id: s for s in steps}
    drop: set[str] = set()
    for t in steps:
        if t.op != "transform" or len(t.inputs) != 1:
            continue
        c = by_id.get(t.inputs[0])
        # Fuse only when the crop's output goes nowhere else (else it's shared).
        if c is None or c.op != "crop" or c.node_id in drop:
            continue
        if consumers.get(c.node_id) != [t.node_id] or len(c.inputs) != 1:
            continue
        t.fragment = shaders.crop_transform_top(target)
        t.uniforms = {**c.uniforms, **t.uniforms}  # uCropRect + uTranslate/uScale
        t.inputs = list(c.inputs)  # sample the source directly
        t.params = {**c.params, **t.params, "_fused_from": c.node_id}
        drop.add(c.node_id)
    return [s for s in steps if s.node_id not in drop]


def lower(g: Graph, target: str = "desktop_gl") -> RuntimePlan:
    if target not in shaders.TARGETS:
        raise ValueError(f"unknown target {target!r}; pick one of {sorted(shaders.TARGETS)}")
    steps = [_lower_node(g, nid, target) for nid in g.topo_order()]
    steps = _fuse_coord_remaps(steps, target)
    return RuntimePlan(steps=steps, output_id=g.output, target=target)
