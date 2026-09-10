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
    ("TOP", "in"): None,       # COMP input  -> passthrough after flattening
    ("TOP", "out"): None,      # COMP output -> passthrough
    ("TOP", "null"): None,     # null        -> passthrough (often the display node)
}


# --- raw file parsing ----------------------------------------------------------
@dataclass
class RawOp:
    path: str                       # full path id, e.g. "project1/ascii/glsl2"
    family: str                     # TOP / CHOP / SOP / DAT / COMP...
    optype: str                     # crop / glsl / moviefilein / ...
    inputs: list[tuple[int, str]] = field(default_factory=list)  # (index, relname)
    flags: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)   # name -> value token(s)
    text: str | None = None         # payload text (for text DATs)


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
                    flags[t] = toks[j + 1] == "on"; j += 2
                elif j + 1 < len(toks) and toks[j + 1].lstrip("-").isdigit():
                    flags[t] = int(toks[j + 1]); j += 2
                else:
                    flags[t] = True; j += 1
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
            (L,) = struct.unpack(">I", raw[p:p + 4])
            if p + 4 + L == len(raw):
                return raw[p + 4:].decode("utf-8", "replace")
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
    def __init__(self, graph: Graph, coverage: list[str], sink: str):
        self.graph = graph
        self.coverage = coverage
        self.sink = sink


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
                return s[a + 1:b]
    toks = s.split()
    return toks[0] if toks else "0"


def _collect_chops(ops: dict) -> list:
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
            sink = p; break
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
            kernel = None  # degrade to passthrough
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
                coverage.append(f"{path}: asset {path_val!r} not local -> testcard "
                                f"substitute (add a bridge /readfile to fetch host assets)")

        nodes[path] = Node(
            id=path, op=(kernel or "passthrough"), family="TOP",
            params=params, inputs=[Port(node=s) for s in top_ins])

    # Collect I/O CHOP services (OSC/MIDI In) — they live outside the TOP render
    # DAG but feed parameter expressions like op('oscin1')['ch'].
    services = []
    for p, op in ops.items():
        if op.family == "CHOP" and op.optype in ("oscin", "midiin"):
            name = p.split("/")[-1]
            if op.optype == "oscin":
                services.append({"type": "oscin", "name": name,
                                 "port": int(float(op.params.get("port", "7000").split()[0]))
                                 if op.params.get("port") else 7000})
            else:
                services.append({"type": "midiin", "name": name,
                                 "device": op.params.get("device")})
            coverage.append(f"service: {op.optype} {name} -> {services[-1]}")

    # Control-rate CHOP DAG feeding parameter exprs (op('speed1')[0] etc.). The
    # render path is TOP-only, but exprs read CHOPs; import that little DAG so the
    # runtime can evaluate it per-frame. Services (oscin/midiin) are excluded —
    # the runtime reads them live.
    chops = _collect_chops(ops)
    for c in chops:
        coverage.append(f"chop: {c['type']} {c['name']} <- {c['inputs']}")

    g = Graph(output=sink, nodes=nodes, services=services, chops=chops)
    g.validate()

    supported = sum(1 for n in nodes.values() if n.op != "passthrough" or True)
    covline = (f"nodes in render path: {len(nodes)}; "
               f"unsupported op types (degraded to passthrough): "
               f"{sorted(unsupported) or 'none'}")
    coverage.insert(0, covline)
    return ImportResult(g, coverage, sink)
