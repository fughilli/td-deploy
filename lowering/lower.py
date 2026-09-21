"""
Lowering: ir.Graph -> RuntimePlan (docs §5).

One Step per node, in cook order. The runtime reads back the output node's
texture at the end (no explicit sink node needed — any op can be the output).

Step kinds:
  source      — produce a texture (load image / synth testcard).
  shader      — full-screen fragment pass over bound inputs -> new texture.
  passthrough — alias input 0's texture (identity ops: in/out/null/out_display,
                and — for now — crop/transform until real kernels land).
  feedback    — a persistent buffer holding the PREVIOUS frame of another step
                (Feedback TOP); seeded from input 0 and re-filled after each cook.

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


def _first_token(v, default):
    """First whitespace token of a `.parm` value (they can carry a trailing
    default/expr), falling back to `default` when absent or unparseable."""
    if v is None:
        return default
    tok = str(v).split()
    if not tok:
        return default
    t = tok[0].strip('"')
    if isinstance(default, str):
        return t
    try:
        return float(t)
    except ValueError:
        return default


def _truthy(v, default: bool) -> bool:
    if v is None:
        return default
    t = str(v).split()[0].strip('"').lower() if str(v).split() else ""
    if t in ("on", "1", "true", "yes"):
        return True
    if t in ("off", "0", "false", "no"):
        return False
    return default


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
    # kind == "feedback": the step whose PREVIOUS frame this buffer echoes.
    feedback_from: str | None = None
    # kind == "render3d": the .obj to draw and the texture to modulate it with.
    mesh_path: str | None = None
    texture_path: str | None = None
    # vec4 per-frame uniforms: name -> [x, y, z, w], each a literal or expression.
    # (A TD GLSL TOP's "Vectors" page; its "Constants" page lands in time_uniforms.)
    vec_uniforms: dict = field(default_factory=dict)


@dataclass
class RuntimePlan:
    steps: list[Step]
    output_id: str
    target: str

    def has_gl_only_ops(self) -> bool:
        # ops with no CPU reference kernel (GL is the oracle for these)
        return any(
            s.op in ("glsl_top", "crop", "transform", "noise", "feedback", "render3d")
            for s in self.steps
        )


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
        # User uniforms declared on the TOP's parameter pages. TouchDesigner
        # exposes scalars on "Constants" and vec4s on "Vectors"; either may be
        # driven by an expression, which is the whole point of them (a knob
        # feeding a shader), so both lower as per-frame values.
        user_scalars, user_vecs = {}, {}
        i = 0
        while f"const{i}name" in n.params:
            nm = str(_first_token(n.params.get(f"const{i}name"), ""))
            if nm:
                user_scalars[nm] = {
                    "expr": value_or_expr(n.params.get(f"const{i}value"), 0.0),
                    "mul": 1.0,
                }
            i += 1
        i = 0
        while f"vec{i}name" in n.params:
            nm = str(_first_token(n.params.get(f"vec{i}name"), ""))
            if nm:
                user_vecs[nm] = [
                    value_or_expr(n.params.get(f"vec{i}value{c}"), 0.0)
                    for c in ("x", "y", "z", "w")
                ]
            i += 1
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
            time_uniforms=user_scalars,
            vec_uniforms=user_vecs,
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
                # Rotation is periodic, so wrap it to [-2pi, 2pi) (fmod) before it
                # reaches the shader. GLES uniforms are 32-bit floats; an unbounded
                # angle (e.g. a Speed CHOP / absTime feeding `rotate`) loses its
                # per-frame increment to the f32 ULP after the value grows large
                # (days of uptime), and the rotation visibly cogs. Modelled into the
                # primitive so every Transform TOP is bounded by construction.
                "uRotate": {"expr": rot, "mul": math.pi / 180.0, "mod": 2.0 * math.pi},
                "uTranslateX": {"expr": _p(["tx", "translatex"], 0.0), "mul": 1.0},
                "uTranslateY": {"expr": _p(["ty", "translatey"], 0.0), "mul": 1.0},
                "uScaleX": {"expr": _p(["sx", "scalex"], 1.0), "mul": usc},
                "uScaleY": {"expr": _p(["sy", "scaley"], 1.0), "mul": usc},
            },
            # Output aspect (w/h): the shader rotates in aspect-corrected space so a
            # non-square frame doesn't stretch under rotation.
            uniforms={"uAspect": ("float", float(ot["w"]) / float(ot["h"]))},
            params=dict(n.params),
        )

    if n.op == "add":
        return Step(
            nid,
            n.op,
            "shader",
            ot,
            inputs=inputs,
            vertex=shaders.vertex(target),
            fragment=shaders.add_top(target, len(inputs)),
            params=dict(n.params),
        )

    if n.op == "math":
        # TD writes `no_op` when no multi-input combine is selected; a single
        # input then just passes through to the Pre-Offset/Gain/Post-Offset chain.
        combine = str(_first_token(n.params.get("op"), "add"))
        if combine in ("no_op", "off", ""):
            combine = "add"
        return Step(
            nid,
            n.op,
            "shader",
            ot,
            inputs=inputs,
            vertex=shaders.vertex(target),
            fragment=shaders.math_top(target, len(inputs), combine),
            # Gain/offsets are ordinary TD parameters, so they may be driven by an
            # expression (e.g. a CHOP) — lower them as per-frame uniforms.
            time_uniforms={
                "uPreOff": {"expr": value_or_expr(n.params.get("preoff", 0.0), 0.0), "mul": 1.0},
                "uGain": {"expr": value_or_expr(n.params.get("gain", 1.0), 1.0), "mul": 1.0},
                "uPostOff": {"expr": value_or_expr(n.params.get("postoff", 0.0), 0.0), "mul": 1.0},
            },
            params=dict(n.params),
        )

    if n.op == "noise":

        def _f(name, default):
            return float(_first_token(n.params.get(name), default))

        # Defaults below mirror TouchDesigner's own Noise TOP defaults, because a
        # TD node that has never been touched writes no `.parm` entry at all — so
        # every one of these applies verbatim to the common case. Notably amp 0.5 /
        # offset 0.5 map the signed noise into [0,1]; amp 1 / offset 0 would clip
        # half the field to black.
        return Step(
            nid,
            n.op,
            "shader",
            ot,
            inputs=[],  # a generator: TD's Noise TOP input only modulates, unsupported
            vertex=shaders.vertex(target),
            # harmon/mono/exp are constant TD parameters, so bake them into the
            # shader: the octave loop unrolls and the branches vanish, which is
            # most of the win on a VideoCore GPU.
            fragment=shaders.noise_top(
                target,
                octaves=int(_f("harmon", 2.0)) + 1,
                mono=_truthy(n.params.get("mono"), True),
                apply_exp=abs(_f("exp", 1.0) - 1.0) > 1e-6,
            ),
            uniforms={
                "uSeed": ("float", _f("seed", 1.0)),
                "uExp": ("float", _f("exp", 1.0)),
                "uSpread": ("float", _f("spread", 2.0)),
                "uRough": ("float", _f("rough", 0.5)),
                "uAspect": ("float", float(ot["w"]) / float(ot["h"])),
                "uTranslate": ("vec4", [_f("tx", 0.0), _f("ty", 0.0), _f("tz", 0.0), 0.0]),
                "uScale": ("vec4", [_f("sx", 1.0), _f("sy", 1.0), _f("sz", 1.0), 0.0]),
            },
            time_uniforms={
                "uPeriod": {"expr": value_or_expr(n.params.get("period", 1.0), 1.0), "mul": 1.0},
                "uAmp": {"expr": value_or_expr(n.params.get("amp", 0.5), 0.5), "mul": 1.0},
                "uOffset": {"expr": value_or_expr(n.params.get("offset", 0.5), 0.5), "mul": 1.0},
                "uT": {"expr": value_or_expr(n.params.get("t4d", 0.0), 0.0), "mul": 1.0},
            },
            params=dict(n.params),
        )

    if n.op == "feedback":
        # The Feedback TOP echoes the PREVIOUS frame of its Target TOP. The target
        # arrives as a delay=1 edge so it never constrains cook order (ir.Graph
        # cuts delayed edges in topo_order); delay-0 input 0 seeds the buffer.
        seed = [p.node for p in n.inputs if p.delay == 0]
        target_id = next((p.node for p in n.inputs if p.delay > 0), None)
        return Step(
            nid,
            n.op,
            "feedback",
            ot,
            inputs=seed,
            feedback_from=target_id,
            params=dict(n.params),
        )

    if n.op == "render3d":
        # A Render TOP draws a scene rather than filtering an input, so it has no
        # bound textures from the graph — its inputs are a mesh file and a
        # material texture, both baked into the artifact.
        def _g(name, default):
            return value_or_expr(n.params.get(f"_geo_{name}"), default)

        def _c(name, default):
            return float(_first_token(n.params.get(f"_cam_{name}"), default))

        def _l(name, default):
            return float(_first_token(n.params.get(f"_light_{name}"), default))

        tex = n.params.get("_texture_path")
        return Step(
            nid,
            n.op,
            "render3d",
            ot,
            inputs=[],
            vertex=shaders.mesh_vertex(target),
            fragment=shaders.mesh_fragment(target, textured=bool(tex)),
            mesh_path=n.params.get("_mesh_path"),
            texture_path=tex,
            # The object transform stays expression-driven: this is where a knob
            # rig feeding rx/ry/rz through a Speed CHOP actually lands.
            time_uniforms={
                "uRotX": {"expr": _g("rx", 0.0), "mul": 1.0, "mod": 360.0},
                "uRotY": {"expr": _g("ry", 0.0), "mul": 1.0, "mod": 360.0},
                "uRotZ": {"expr": _g("rz", 0.0), "mul": 1.0, "mod": 360.0},
                "uSclX": {"expr": _g("sx", 1.0), "mul": 1.0},
                "uSclY": {"expr": _g("sy", 1.0), "mul": 1.0},
                "uSclZ": {"expr": _g("sz", 1.0), "mul": 1.0},
                "uTrnX": {"expr": _g("tx", 0.0), "mul": 1.0},
                "uTrnY": {"expr": _g("ty", 0.0), "mul": 1.0},
                "uTrnZ": {"expr": _g("tz", 0.0), "mul": 1.0},
                "uDimmer": {"expr": value_or_expr(n.params.get("_light_dimmer"), 1.0), "mul": 1.0},
            },
            # TD writes only non-default camera/light parameters, so these
            # defaults are TouchDesigner's own.
            uniforms={
                "uCamX": ("float", _c("tx", 0.0)),
                "uCamY": ("float", _c("ty", 0.0)),
                "uCamZ": ("float", _c("tz", 0.0)),
                "uFov": ("float", _c("fov", 45.0)),
                "uNear": ("float", _c("near", 0.1)),
                "uFar": ("float", _c("far", 1000.0)),
                "uAspect": ("float", float(ot["w"]) / float(ot["h"])),
                "uLightX": ("float", _l("tx", 0.0)),
                "uLightY": ("float", _l("ty", 0.0)),
                "uLightZ": ("float", _l("tz", 1.0)),
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
