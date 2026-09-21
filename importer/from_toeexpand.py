"""
Importer: a toeexpand `.dir` tree  ->  toxc IR (ir.Graph).

toeexpand emits, per operator, a `<name>.n` file:
    TOP:crop                     # line 1 = Family:type
    tile <x> <y> <w> <h>
    flags =  ... display on ...  # 'display on' marks the shown/output TOP
    inputs
    {
    0 \t <srcname>               # wiring: input index -> source op (rel path)
    1 \t <srcname>
    }
    ...
    end
Plus a sibling `<name>.parm` (parameters) and type payloads (e.g. a GLSL TOP's
`pixeldat` points at a text DAT whose `<name>.text` holds the shader source).
COMPs carry a `<name>.network` with a `compinputs` block mapping external inputs
to internal In operators.

This importer:
  * parses the tree into fully-pathed operators,
  * resolves wiring (including COMP In/Out boundaries) into a flat TOP DAG,
  * maps TD op types to toxc kernels (coverage-reported; unknown ops degrade to
    passthrough with a warning),
  * traces back from the display sink to emit only the render-relevant subgraph.

Scope (M1): the TOP render path. Non-TOP inputs to GLSL (CHOP/DAT uniforms) are
noted in the coverage report and skipped.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field

from ir.graph import Graph, Node, Port

# --- TD op type -> toxc kernel -------------------------------------------------
# value None = structural passthrough (identity on input 0).
OP_MAP = {
    ("TOP", "moviefilein"): "image_in",
    ("TOP", "glsl"): "glsl_top",
    ("TOP", "crop"): "crop",
    ("TOP", "transform"): "transform",
    ("TOP", "level"): "level",
    ("TOP", "add"): "add",
    ("TOP", "math"): "math",
    ("TOP", "noise"): "noise",
    ("TOP", "feedback"): "feedback",
    ("TOP", "render"): "render3d",
    ("TOP", "in"): None,  # COMP input  -> passthrough after flattening
    ("TOP", "out"): None,  # COMP output -> passthrough
    ("TOP", "null"): None,  # null        -> passthrough (often the display node)
}


# --- raw file parsing ----------------------------------------------------------
@dataclass
class RawOp:
    path: str  # full path id, e.g. "project1/ascii/glsl2"
    family: str  # TOP / CHOP / SOP / DAT / COMP...
    optype: str  # crop / glsl / moviefilein / ...
    inputs: list[tuple[int, str]] = field(default_factory=list)  # (index, relname)
    flags: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)  # name -> value token(s)
    text: str | None = None  # payload text (for text DATs)


def _parse_n(text: str) -> tuple[str, str, list[tuple[int, str]], dict]:
    lines = text.splitlines()
    family, _, optype = lines[0].partition(":")
    inputs: list[tuple[int, str]] = []
    flags: dict = {}
    i = 1
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("flags"):
            toks = ln.split("=", 1)[1].split() if "=" in ln else []
            j = 0
            while j < len(toks):
                t = toks[j]
                if j + 1 < len(toks) and toks[j + 1] in ("on", "off"):
                    flags[t] = toks[j + 1] == "on"
                    j += 2
                elif j + 1 < len(toks) and toks[j + 1].lstrip("-").isdigit():
                    flags[t] = int(toks[j + 1])
                    j += 2
                else:
                    flags[t] = True
                    j += 1
        elif ln == "inputs":
            i += 2  # skip 'inputs' and '{'
            while i < len(lines) and lines[i].strip() != "}":
                parts = lines[i].split("\t")
                if len(parts) >= 2 and parts[0].strip().isdigit():
                    inputs.append((int(parts[0].strip()), parts[1].strip()))
                i += 1
        i += 1
    return family, optype, inputs, flags


def _parse_parm(text: str) -> dict:
    """`.parm` lines look like:  name <mode> <value...>   between '?' markers.
    We keep name -> the remaining tokens (best-effort; full param semantics are
    per-op and grow with coverage)."""
    params: dict = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln == "?":
            continue
        toks = ln.split()
        if len(toks) >= 3:
            params[toks[0]] = " ".join(toks[2:])
        elif len(toks) == 2:
            params[toks[0]] = toks[1]
    return params


def _strip_dat_text(raw: bytes) -> str:
    """A text DAT `.text` is `2\\n*` + a small big-endian int header + content.
    Find the 4-byte length field whose value runs exactly to EOF, return content."""
    star = raw.find(b"*")
    if star != -1:
        for p in range(star + 1, min(star + 64, len(raw) - 4)):
            (L,) = struct.unpack(">I", raw[p : p + 4])
            if p + 4 + L == len(raw):
                return raw[p + 4 :].decode("utf-8", "replace")
    # fallback: drop the first line and a leading '*'
    body = raw.split(b"\n", 1)[-1]
    return body.lstrip(b"*").decode("utf-8", "replace")


# --- tree loading --------------------------------------------------------------
def _load_tree(dirroot: str) -> dict[str, RawOp]:
    ops: dict[str, RawOp] = {}
    for dp, _dn, fnames in os.walk(dirroot):
        for fn in fnames:
            if not fn.endswith(".n"):
                continue
            base = fn[:-2]
            rel = os.path.relpath(os.path.join(dp, base), dirroot)
            rel = rel.replace(os.sep, "/")
            with open(os.path.join(dp, fn)) as fh:
                family, optype, inputs, flags = _parse_n(fh.read())
            op = RawOp(rel, family, optype, inputs, flags)
            pf = os.path.join(dp, base + ".parm")
            if os.path.isfile(pf):
                with open(pf) as fh:
                    op.params = _parse_parm(fh.read())
            tf = os.path.join(dp, base + ".text")
            if os.path.isfile(tf):
                with open(tf, "rb") as fh:
                    op.text = _strip_dat_text(fh.read())
            ops[rel] = op
    return ops


def _resolve(parent_dir: str, name: str) -> str:
    """Resolve an input/reference name relative to the referring op's parent dir."""
    joined = name if parent_dir == "" else parent_dir + "/" + name
    parts: list[str] = []
    for seg in joined.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)


def _parse_compinputs(dirroot: str, comp_path: str) -> dict[str, str]:
    """For a COMP, map internal-In-op-name -> external source (resolved full path).
    Reads `<comp>.network` compinputs. Returns {} if none."""
    nf = os.path.join(dirroot, comp_path + ".network")
    if not os.path.isfile(nf):
        return {}
    comp_parent = comp_path.rsplit("/", 1)[0] if "/" in comp_path else ""
    out: dict[str, str] = {}
    with open(nf) as fh:
        lines = [ln.rstrip("\n") for ln in fh]
    i = 0
    while i < len(lines):
        if lines[i].strip() == "compinputs":
            i += 2  # skip 'compinputs' and '{'
            while i < len(lines) and lines[i].strip() != "}":
                # block: "<idx>\t<external>" , "\t<internal>" , "\t<TYPE>"
                ext = lines[i].split("\t")[-1].strip()
                internal = lines[i + 1].strip() if i + 1 < len(lines) else ""
                out[internal] = _resolve(comp_parent, ext)
                i += 3
            break
        i += 1
    return out


# --- import --------------------------------------------------------------------
class ImportResult:
    def __init__(self, graph: Graph, coverage: list[str], sink: str, unsupported=None):
        self.graph = graph
        self.coverage = coverage
        self.sink = sink
        # `FAMILY:optype` strings for render-path ops with no OP_MAP entry (degraded to
        # passthrough). Surfaced so the deploy path can turn them into a real error.
        self.unsupported = sorted(unsupported or [])


def _effective_inputs(ops: dict[str, RawOp], compinputs: dict, path: str) -> list[str]:
    """Resolved full-path TOP inputs of an op, threading COMP In-op boundaries."""
    op = ops[path]
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    if op.family == "TOP" and op.optype == "in":
        # A COMP's In TOP: its source is the COMP's external input.
        ext = compinputs.get((parent, path.rsplit("/", 1)[-1]))
        return [ext] if ext else []
    resolved = []
    for _idx, name in sorted(op.inputs):
        full = _resolve(parent, name)
        resolved.append(full)
    return resolved


_OPREF = re.compile(r"op\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _param_expr(raw) -> str:
    """A Constant CHOP value param stores `<default> "<expr>"`. Return the quoted
    expression if present, else the leading numeric literal (as a string the
    interpreter evaluates as a constant)."""
    if raw is None:
        return "0"
    s = str(raw).strip()
    for q in ('"', "'"):
        a = s.find(q)
        if a != -1:
            b = s.find(q, a + 1)
            if b != -1:
                return s[a + 1 : b]
    toks = s.split()
    return toks[0] if toks else "0"


def _collect_chops(ops: dict, compinputs: dict | None = None) -> list:
    """Import the CHOP DAG feeding any op('X') reference in a param expr, in
    dependency (topo) order. Services (oscin/midiin) are excluded — the runtime
    reads them live. constant: per-channel exprs; speed/math/null: passthrough."""

    def resolve(name):
        for p, op in ops.items():
            if op.family == "CHOP" and (p == name or p.endswith("/" + name)):
                return op
        return None

    # Seed from op() refs in ANY op's params (TOP exprs + CHOP exprs).
    stack = []
    for op in ops.values():
        for v in (op.params or {}).values():
            stack += _OPREF.findall(str(v))

    defs: dict = {}  # name -> def dict, or None for services / unresolved
    while stack:
        name = stack.pop()
        if name in defs:
            continue
        op = resolve(name)
        if op is None or op.optype in ("oscin", "midiin"):
            defs[name] = None  # service or external — read live, don't emit
            continue
        inputs = [rel for (_i, rel) in sorted(op.inputs)]
        if op.optype == "in" and not inputs and compinputs:
            # An In CHOP has no internal input: its data arrives over the COMP's
            # external wire. That is the same boundary _effective_inputs threads
            # for TOPs, so thread it here too — otherwise a COMP that takes its
            # control signal as a CHOP input evaluates to a dead 0 on device.
            parent, _, leaf = op.path.rpartition("/")
            ext = compinputs.get((parent, leaf))
            if ext:
                inputs = [ext]
        channels = []
        if op.optype == "constant":
            i = 0
            while f"const{i}value" in op.params:
                channels.append(_param_expr(op.params[f"const{i}value"]))
                i += 1
        defs[name] = {"name": name, "type": op.optype, "inputs": inputs, "channels": channels}
        stack += inputs
        for v in op.params.values():
            stack += _OPREF.findall(str(v))

    # Topo order: inputs before dependents.
    real = {n: d for n, d in defs.items() if d is not None}
    order, seen = [], set()

    def visit(n):
        if n in seen or n not in real:
            return
        seen.add(n)
        for inp in real[n]["inputs"]:
            visit(inp)
        order.append(real[n])

    for n in list(real):
        visit(n)
    return order


def _ref_param(op_params: dict, name: str, parent: str) -> str | None:
    """A COMP-path parameter (`camera`, `geometry`, `lights`, `file`) resolved to a
    tree path. TD writes these absolute or relative to the referring op's parent."""
    raw = (op_params.get(name) or "").split()
    if not raw:
        return None
    tok = raw[0].strip('"')
    if not tok:
        return None
    return _resolve("" if tok.startswith("/") else parent, tok)


# Object-transform parameters lifted off the geometry COMP. They are ordinary TD
# parameters, so any of them may be an expression (the knob rig drives rx/ry/rz
# and the scale from CHOPs) — they are passed through verbatim for lowering to
# turn into per-frame uniforms.
_XFORM = ("tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz", "px", "py", "pz", "scale")
# Camera intrinsics we honor. TD only writes non-defaults, so lowering supplies
# TD's own defaults (perspective, horizontal fov 45, near 0.1, far 1000).
_CAM = ("fov", "near", "far", "projection", "viewanglemethod", "orthowidth")


def _render_scene(ops: dict, path: str, op: "RawOp", coverage: list[str]) -> dict:
    """Flatten a Render TOP's camera / geometry / lights into plain params."""
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    out: dict = {}

    cam = _ref_param(op.params, "camera", parent)
    if cam and cam in ops:
        for k in _XFORM + _CAM:
            v = ops[cam].params.get(k)
            if v is not None:
                out[f"_cam_{k}"] = v
    else:
        coverage.append(f"{path}: Render TOP camera {cam!r} unresolved — using a default view")

    lit = _ref_param(op.params, "lights", parent)
    if lit and lit in ops:
        for k in ("tx", "ty", "tz"):
            v = ops[lit].params.get(k)
            if v is not None:
                out[f"_light_{k}"] = v

    geo = _ref_param(op.params, "geometry", parent)
    if not geo or geo not in ops:
        coverage.append(f"{path}: Render TOP geometry {geo!r} unresolved — nothing to draw")
        return out
    for k in _XFORM:
        v = ops[geo].params.get(k)
        if v is not None:
            out[f"_geo_{k}"] = v

    # The mesh itself. TouchDesigner does not expand procedural SOPs — a Torus SOP
    # is just `torus1.n` plus parameters, with no vertices on disk — so the only
    # geometry we can actually read is a SOP that points at a file.
    # TouchDesigner 2025 renders geometry from POPs (Point Operators); older
    # projects use SOPs. Accept either — the file-backed case looks the same.
    sops = [
        (p2, o2)
        for p2, o2 in ops.items()
        if o2.family in ("SOP", "POP") and p2.startswith(geo + "/")
    ]
    if not sops:
        coverage.append(f"{path}: geometry {geo} contains no SOP/POP to draw")
        return out
    for p2, o2 in sops:
        if o2.optype in ("filein", "file"):
            # A filesystem path, not a node path — take the raw token.
            raw = (o2.params.get("file") or "").split()
            out["_mesh_path"] = raw[0].strip('"') if raw else None
            out["_mesh_sop"] = p2
            return out
    kinds = ", ".join(sorted({f"{o2.family}:{o2.optype}" for _, o2 in sops}))
    coverage.append(
        f"{path}: geometry {geo} is procedural ({kinds}) — TouchDesigner does not "
        f"expand SOP geometry, so the mesh must come from a File In SOP (.obj)"
    )
    return out


def import_dir(dirroot: str) -> ImportResult:
    ops = _load_tree(dirroot)

    # compinputs keyed by (comp_path, internal_in_name)
    compinputs: dict[tuple[str, str], str] = {}
    for p, op in ops.items():
        for internal, ext in _parse_compinputs(dirroot, p).items():
            compinputs[(p, internal)] = ext

    # display sink: a TOP flagged 'display on' (prefer), else the last 'null'.
    sink = None
    for p, op in ops.items():
        if op.family == "TOP" and op.flags.get("display"):
            sink = p
            break
    if sink is None:
        raise ValueError("no display TOP found (no 'display on' flag)")

    coverage: list[str] = []
    unsupported: set[str] = set()
    nodes: dict[str, Node] = {}

    # BFS back from the sink over TOP inputs, flattening COMP boundaries.
    stack = [sink]
    visited: set[str] = set()
    while stack:
        path = stack.pop()
        if path in visited or path not in ops:
            continue
        visited.add(path)
        op = ops[path]
        if op.family != "TOP":
            coverage.append(f"skip non-TOP input {path} ({op.family}:{op.optype})")
            continue

        ins = _effective_inputs(ops, compinputs, path)
        # keep only TOP inputs in the render DAG
        top_ins = []
        for src in ins:
            if src in ops and ops[src].family == "TOP":
                top_ins.append(src)
                stack.append(src)
            elif src:
                coverage.append(f"{path}: non-TOP/unknown input {src!r} dropped")

        key = (op.family, op.optype)
        kernel = OP_MAP.get(key, "PASSTHROUGH?")
        if kernel == "PASSTHROUGH?":
            unsupported.add(f"{op.family}:{op.optype}")
            # Degrade so the graph still renders if the deploy path chooses to continue
            # (lenient mode): identity on input 0 when there's something to pass through,
            # else a blank testcard `image_in` source (a "null" placeholder — the runtime
            # already synths a testcard for a pathless image_in).
            kernel = None if top_ins else "image_in"
        params = dict(op.params)
        if kernel == "glsl_top":
            # resolve pixeldat -> its .text shader
            pd = op.params.get("pixeldat")
            shader = None
            if pd:
                pd_path = _resolve(path.rsplit("/", 1)[0] if "/" in path else "", pd)
                if pd_path in ops and ops[pd_path].text is not None:
                    shader = ops[pd_path].text
            params["_shader"] = shader
            if shader is None:
                coverage.append(f"{path}: glsl_top has no resolvable pixel shader")
        if kernel == "image_in":
            # `.parm` file value may carry a trailing default expr; take the path token.
            fv = (op.params.get("file") or "").split()
            path_val = fv[0] if fv else None
            params["path"] = path_val
            if path_val and not os.path.isfile(path_val):
                # Only a note, not a verdict: TouchDesigner stores movie paths relative
                # to the project, so this almost always misses here. The deploy path
                # resolves against the .toe dir, its parent and any configured asset
                # roots, and reports the real outcome (with the dirs it searched).
                coverage.append(
                    f"{path}: asset {path_val!r} is not relative to the CWD — "
                    f"resolving it against the project dir and asset roots"
                )

        ports = [Port(node=s) for s in top_ins]
        if kernel == "render3d":
            # A Render TOP references its scene through PARAMETERS (camera /
            # geometry / lights), not wires, so nothing arrives as a TOP input.
            # Those COMPs and the SOP inside the geometry are pulled in here and
            # flattened onto this node — the render is a leaf in the TOP graph.
            params.update(_render_scene(ops, path, op, coverage))

        if kernel == "feedback":
            # A Feedback TOP names its source in the `top` parameter, not as a wire:
            # it emits that TOP's PREVIOUS frame. Model it as a delay=1 edge so the
            # loop is legal (Graph.topo_order cuts delayed edges) and so the target
            # stays reachable for dead-node elimination. Input 0 seeds the buffer.
            raw = (params.get("top") or "").split()
            tgt = raw[0].strip('"') if raw else ""
            tgt_path = None
            if tgt:
                parent = path.rsplit("/", 1)[0] if "/" in path else ""
                # `top` may be absolute (/project1/add1) or relative to the parent.
                tgt_path = _resolve("" if tgt.startswith("/") else parent, tgt)
            if tgt_path and ops.get(tgt_path) is not None and ops[tgt_path].family == "TOP":
                params["_feedback_target"] = tgt_path
                ports.append(Port(node=tgt_path, delay=1))
                stack.append(tgt_path)  # pull the target in even if nothing else uses it
            else:
                coverage.append(
                    f"{path}: Feedback TOP target {tgt!r} unresolved — "
                    f"buffer will just echo input 0"
                )

        nodes[path] = Node(
            id=path,
            op=(kernel or "passthrough"),
            family="TOP",
            params=params,
            inputs=ports,
        )

    # Collect I/O CHOP services (OSC/MIDI In) — they live outside the TOP render
    # DAG but feed parameter expressions like op('oscin1')['ch'].
    services = []
    for p, op in ops.items():
        if op.family == "CHOP" and op.optype in ("oscin", "midiin"):
            name = p.split("/")[-1]
            if op.optype == "oscin":
                services.append(
                    {
                        "type": "oscin",
                        "name": name,
                        "port": (
                            int(float(op.params.get("port", "7000").split()[0]))
                            if op.params.get("port")
                            else 7000
                        ),
                    }
                )
            else:
                services.append({"type": "midiin", "name": name, "device": op.params.get("device")})
            coverage.append(f"service: {op.optype} {name} -> {services[-1]}")

    # Control-rate CHOP DAG feeding parameter exprs (op('speed1')[0] etc.). The
    # render path is TOP-only, but exprs read CHOPs; import that little DAG so the
    # runtime can evaluate it per-frame. Services (oscin/midiin) are excluded —
    # the runtime reads them live.
    chops = _collect_chops(ops, compinputs)
    for c in chops:
        coverage.append(f"chop: {c['type']} {c['name']} <- {c['inputs']}")

    g = Graph(output=sink, nodes=nodes, services=services, chops=chops)
    g.validate()

    covline = (
        f"nodes in render path: {len(nodes)}; "
        f"unsupported op types (degraded to passthrough): "
        f"{sorted(unsupported) or 'none'}"
    )
    coverage.insert(0, covline)
    return ImportResult(g, coverage, sink, unsupported=unsupported)
