"""Compile a TouchDesigner project that runs Python into a Python-host artifact.

The classic path (importer -> passes -> lowering -> emit_artifact) bakes a TOP
chain whose parameters are constants or simple expressions. Projects that drive
their visuals from Python — Execute DAT callbacks, Script TOP/CHOP/SOP, Python
parameter expressions reading storage — need that Python at runtime. This path
ships the whole network plus the `tdhost` package, and compiles the render graph
into a schedule whose every dynamic value is a *binding* the host evaluates each
frame (a parameter, an object's world matrix, a Script op's output, a flag).

Artifact layout:

    schedule.json        format "toxc-host/1": nodes (topo order), bindings,
                         meshes, textures, host config, output, window
    shaders/*.vert|frag  one program per full-screen pass / per scene draw
    meshes/*.bin|*.idx   baked geometry (interleaved f32 + u32 indices)
    assets/*.png         textures (Movie File In, FBX texture imports)
    host/network.json    the whole network (tdhost.tree.load_network)
    host/tdhost/         the host package (run by the runtime's Python co-process)
    project/             project files the Python needs at runtime (scripts,
                         models...) per the project's td-deploy.json manifest

Coverage (TOP render graph):
    GLSL (any number of inputs incl. the `tops` parameter, multiple colour
    buffers), Render (multiple geometry COMPs, FBX meshes incl. skinning, Script
    SOP meshes, CHOP instancing, Phong MAT with maps and alpha test, point and
    ambient lights), Depth, Render Select, Select, Script, Movie File In, Import
    Select (FBX textures), Blur, Fit, Flip, Resolution, Composite, Level, Null,
    In/Out.
"""

from __future__ import annotations

import fnmatch
import glob
import json
import os
import shutil
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, os.path.join(_ROOT, "pyhost"), os.path.join(_ROOT, "importer")):
    if p not in sys.path:
        sys.path.insert(0, p)

import fbx as FBX  # noqa: E402  (importer/fbx.py)
from tdhost.tree import load_network  # noqa: E402

from lowering import host_shaders as HS  # noqa: E402

TYPE_ALIASES = {
    ("COMP", "geo"): "geometry",
    ("COMP", "cam"): "camera",
    ("COMP", "ambient"): "ambientlight",
    ("TOP", "comp"): "composite",
    ("TOP", "res"): "resolution",
    ("DAT", "parexec"): "parameterexecute",
}

FORMATS = {
    "rgba8fixed": "rgba8",
    "rgba16fixed": "rgba16f",
    "rgba16float": "rgba16f",
    "rgba32float": "rgba32f",
    "r32float": "r32f",
    "r16float": "r16f",
    "rg16float": "rgba16f",
    "rgb10a2fixed": "rgba8",
    "rgba11float": "rgba16f",
    "mono8fixed": "rgba8",
    "mono16float": "r16f",
    "mono32float": "r32f",
}

FIT_MODES = {"fill": 0, "fithorz": 1, "fitvert": 2, "fitbest": 3, "fitoutside": 4, "nativeres": 5}


class CompileError(Exception):
    pass


# ================================================================== helpers


class Net:
    """Read-only view of the network for compile-time decisions."""

    def __init__(self, net: dict):
        self.net = net
        self.ops = net["ops"]

    def typ(self, path):
        o = self.ops.get(path)
        if o is None:
            return None
        return TYPE_ALIASES.get((o["family"], o["type"]), o["type"])

    def fam(self, path):
        o = self.ops.get(path)
        return o["family"] if o else None

    def raw(self, path, name):
        o = self.ops.get(path)
        return (o or {}).get("params", {}).get(name)

    def const(self, path, name, default=None):
        """A parameter's constant value (string) when it is NOT expression-driven;
        `default` when absent. Expression-driven -> the stored value snapshot."""
        spec = self.raw(path, name)
        if spec is None:
            for d in (self.ops.get(path) or {}).get("custom", []):
                if name in d["names"]:
                    dv = d["default"]
                    return dv[d["names"].index(name)] if isinstance(dv, list) else dv
            return default
        return spec["val"]

    def is_expr(self, path, name):
        spec = self.raw(path, name)
        return bool(spec and spec.get("mode", 0) & 1 and spec.get("expr"))

    def expr(self, path, name):
        spec = self.raw(path, name)
        return spec.get("expr") if spec else None

    def resolve(self, ref: str, owner: str, self_first=False) -> str | None:
        """Resolve an operator-path parameter. TD accepts absolute paths, paths
        relative to the owner's network, and (for COMP parameters) './x' inside."""
        if ref is None:
            return None
        ref = str(ref).strip().strip('"')
        if not ref:
            return None
        cands = []
        if ref.startswith("/"):
            cands.append(ref)
        else:
            parent = owner.rsplit("/", 1)[0] or "/"
            inside = _join(owner, ref)
            beside = _join(parent, ref)
            cands += [inside, beside] if (self_first or ref.startswith("./")) else [beside, inside]
        for c in cands:
            if c in self.ops:
                return c
        return None

    def children(self, comp):
        pre = comp.rstrip("/") + "/"
        return [p for p in self.ops if p.startswith(pre) and "/" not in p[len(pre) :]]

    def flag(self, path, name, default=False):
        return bool((self.ops.get(path) or {}).get("flags", {}).get(name, default))


def _join(base, rel):
    parts = [] if base == "/" else base.strip("/").split("/")
    for seg in rel.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
        else:
            parts.append(seg)
    return "/" + "/".join(parts)


def _sid(path: str) -> str:
    return path.strip("/").replace("/", "_") or "root"


class Bindings:
    """Everything the renderer reads from the host each frame, deduplicated."""

    def __init__(self):
        self.pars: list = []
        self.mats: list = []
        self.flags: list = []
        self.tops: list = []
        self.chops: dict = {}
        self.sops: list = []
        self._pi: dict = {}
        self._mi: dict = {}
        self._fi: dict = {}

    def par(self, op, name) -> list:
        key = (op, name)
        if key not in self._pi:
            self._pi[key] = len(self.pars)
            self.pars.append([op, name])
        return ["b", self._pi[key]]

    def mat(self, op) -> int:
        if op not in self._mi:
            self._mi[op] = len(self.mats)
            self.mats.append(op)
        return self._mi[op]

    def flag(self, op, name) -> list:
        # flags come after the pars in the float vector; index resolved at dump
        key = (op, name)
        if key not in self._fi:
            self._fi[key] = len(self.flags)
            self.flags.append([op, name])
        return ["f", self._fi[key]]

    def top(self, op):
        if op not in self.tops:
            self.tops.append(op)

    def chop(self, op, chans):
        s = self.chops.setdefault(op, [])
        for c in chans:
            if c and c not in s:
                s.append(c)

    def sop(self, op):
        if op not in self.sops:
            self.sops.append(op)

    def to_json(self):
        return {
            "pars": self.pars,
            "mats": self.mats,
            "flags": self.flags,
            "tops": self.tops,
            "chops": [[k, v] for k, v in self.chops.items()],
            "sops": self.sops,
        }


def _finalize_refs(obj, n_pars):
    """["f", i] (flag index) -> ["b", n_pars + i] now that the float layout is known."""
    if isinstance(obj, list):
        if len(obj) == 2 and obj[0] == "f" and isinstance(obj[1], int):
            return ["b", n_pars + obj[1]]
        return [_finalize_refs(x, n_pars) for x in obj]
    if isinstance(obj, dict):
        return {k: _finalize_refs(v, n_pars) for k, v in obj.items()}
    return obj


# ================================================================== compiler


class HostCompiler:
    def __init__(
        self,
        dirroot: str,
        outdir: str,
        *,
        project_dir: str | None = None,
        path_map: dict | None = None,
        asset_roots: list | None = None,
        log=print,
    ):
        self.dirroot = dirroot
        self.outdir = outdir
        self.log = log
        self.netdict = load_network(dirroot)
        self.N = Net(self.netdict)
        self.B = Bindings()
        self.path_map = dict(path_map or {})
        self.asset_roots = list(asset_roots or [])
        self.project_dir = project_dir
        self.nodes: dict[str, dict] = {}
        self.order: list[str] = []
        self.meshes: dict = {}
        self.textures: dict = {}
        self.coverage: list[str] = []
        self.unsupported: set[str] = set()
        self._fbx_cache: dict = {}
        self._render_needs_depth: set[str] = set()
        for sub in ("shaders", "meshes", "assets", "host"):
            os.makedirs(os.path.join(outdir, sub), exist_ok=True)

    # ------------------------------------------------------------ files
    def local_path(self, p: str) -> str | None:
        """A path baked on the authoring machine, found here (path map, then the
        asset roots, then as-is)."""
        if not p:
            return None
        cands = []
        for src, dst in self.path_map.items():
            if p.startswith(src):
                cands.append(dst + p[len(src) :])
        cands.append(p)
        base = os.path.basename(p)
        for r in self.asset_roots:
            cands.append(os.path.join(r, base))
            for src in self.path_map:
                if p.startswith(src):
                    cands.append(os.path.join(r, p[len(src) :].lstrip("/")))
        for c in cands:
            if os.path.isfile(c):
                return c
        return None

    # ------------------------------------------------------------ sink
    def find_sink(self) -> tuple[str, str | None]:
        """The displayed TOP: a Window COMP's operator (what a show outputs), else
        a TOP with its display flag set."""
        N = self.N
        wins = sorted(p for p in N.ops if N.fam(p) == "COMP" and N.typ(p) == "window")
        for w in wins:
            ref = N.resolve(N.const(w, "winop"), w)
            if ref and N.fam(ref) == "TOP":
                return ref, w
        for p in sorted(N.ops):
            if N.fam(p) == "TOP" and N.flag(p, "display"):
                return p, None
        raise CompileError(
            "no output: no Window COMP with a TOP and no TOP with its display flag on"
        )

    # ------------------------------------------------------------ graph walk
    def top_refs(self, path) -> list[tuple[str, int]]:
        """(source TOP, delay) pairs a TOP depends on, wires and parameter refs."""
        N = self.N
        t = N.typ(path)
        out = [(s, 0) for s in N.ops[path]["inputs"] if N.fam(s) == "TOP"]
        if t == "glsl":
            for nm in str(N.const(path, "tops", "") or "").split():
                r = N.resolve(nm, path)
                if r and N.fam(r) == "TOP":
                    out.append((r, 0))
        if t in ("select", "renderselect"):
            r = self._select_source(path)
            if r:
                out.append((r, 0))
        if t == "depth":
            r = N.resolve(N.const(path, "op") or N.const(path, "rendertop"), path)
            if r:
                out.append((r, 0))
        if t == "feedback":
            r = N.resolve(N.const(path, "top"), path)
            if r:
                out.append((r, 1))
        if t == "render":
            for m in self._render_map_tops(path):
                out.append((m, 0))
        return out

    def _select_source(self, path):
        N = self.N
        if N.is_expr(path, "top"):
            ex = N.expr(path, "top")
            # parent.FBX.op('touchTextures/x') — resolve the common shortcut form
            import re

            m = re.match(r"\s*parent\.(\w+)\.op\(\s*['\"]([^'\"]+)['\"]\s*\)\s*$", ex or "")
            if m:
                comp = self._parent_shortcut(path, m.group(1))
                if comp:
                    return N.resolve(m.group(2), comp, self_first=True)
            m = re.match(r"\s*op\(\s*['\"]([^'\"]+)['\"]\s*\)\s*$", ex or "")
            if m:
                return N.resolve(m.group(1), path)
            self.coverage.append(f"{path}: select expression {ex!r} not resolvable at compile time")
            return None
        return N.resolve(N.const(path, "top"), path)

    def _parent_shortcut(self, path, name):
        N = self.N
        p = path.rsplit("/", 1)[0]
        while p and p != "/":
            if str(N.const(p, "parentshortcut", "") or "") == name:
                return p
            p = p.rsplit("/", 1)[0] or "/"
        return None

    def walk(self, sink):
        seen, order, stack = set(), [], []
        temp = set()

        def visit(p):
            if p in seen:
                return
            if p in temp:
                return  # a same-frame cycle; feedback edges are delayed
            temp.add(p)
            for s, delay in self.top_refs(p):
                if delay == 0:
                    visit(s)
            temp.discard(p)
            seen.add(p)
            order.append(p)
            for s, delay in self.top_refs(p):
                if delay:
                    stack.append(s)

        visit(sink)
        while stack:
            visit(stack.pop())
        return order

    # ------------------------------------------------------------ values
    def val(self, op, name, default=None):
        """A bound value: the host evaluates every parameter (constant or
        expression) each frame, so the renderer only ever reads the binding."""
        o = self.N.ops.get(op)
        if o is None:
            return default
        if name not in o.get("params", {}) and not any(
            name in d["names"] for d in o.get("custom", [])
        ):
            if default is not None:
                return default
        return self.B.par(op, name)

    def fmt(self, path, default="rgba8"):
        f = str(self.N.const(path, "format", "") or self.N.const(path, "pixelformat", "") or "")
        if not f or f == "useinput":
            return None if default is None else default
        return FORMATS.get(f, "rgba8")

    def size_rule(self, path, inputs):
        N = self.N
        mode = str(N.const(path, "outputresolution", "useinput") or "useinput")
        if mode == "custom" or (mode == "useinput" and not inputs and N.raw(path, "resolutionw")):
            return {
                "mode": "custom",
                "w": self.val(path, "resolutionw", 256),
                "h": self.val(path, "resolutionh", 256),
            }
        frac = {"eighth": 0.125, "quarter": 0.25, "half": 0.5, "2x": 2.0, "4x": 4.0, "8x": 8.0}.get(
            mode
        )
        if frac:
            return {"mode": "fraction", "f": frac}
        if inputs:
            return {"mode": "input"}
        return {"mode": "custom", "w": 256, "h": 256}

    # ------------------------------------------------------------ nodes
    def script_read_tops(self, exclude):
        """TOPs the project's Python reads back (numpyArray/sample) that the output
        does not depend on. Found by string literals in DAT code naming a TOP
        (by path or by name); they compile as on-demand nodes, cooked only in
        frames where a script asked for their pixels."""
        import re

        lits = set()
        for o in self.N.ops.values():
            if o["family"] == "DAT" and o.get("text"):
                lits.update(re.findall(r"['\"]([A-Za-z0-9_/.]+)['\"]", o["text"]))
        out = []
        for p, o in self.N.ops.items():
            if o["family"] != "TOP" or p in exclude:
                continue
            if p in lits or p.rsplit("/", 1)[-1] in lits:
                out.append(p)
        return sorted(out)

    def compile(self):
        self.sync_dat_files()
        sink, window = self.find_sink()
        self.sink, self.window = sink, window
        order = self.walk(sink)
        main = set(order)
        self.on_demand = set()
        for extra in self.script_read_tops(main):
            for p in self.walk(extra):
                if p not in main:
                    order.append(p)
                    main.add(p)
                    self.on_demand.add(p)
        # Depth TOPs need their Render TOP to write depth
        for p in order:
            if self.N.typ(p) == "depth":
                r = self.N.resolve(self.N.const(p, "op") or self.N.const(p, "rendertop"), p)
                if r:
                    self._render_needs_depth.add(r)
        for p in order:
            node = self.compile_node(p)
            if node is not None:
                if p in self.on_demand:
                    node["on_demand"] = True
                self.nodes[p] = node
                self.order.append(p)
        return self

    def compile_node(self, path):
        N = self.N
        t = N.typ(path)
        wired = [s for s in N.ops[path]["inputs"] if N.fam(s) == "TOP"]
        sid = _sid(path)
        base = {"id": path, "inputs": wired}

        if t in ("null", "out", "in"):
            if not wired and t == "in":
                self.coverage.append(f"{path}: COMP input with no source — blank")
                return {
                    **base,
                    "kind": "blank",
                    "size": {"mode": "custom", "w": 256, "h": 256},
                    "fmt": "rgba8",
                }
            return {**base, "kind": "alias", "of": wired[0] if wired else None}

        if t == "select":
            src = self._select_source(path)
            if src is None:
                return {
                    **base,
                    "kind": "blank",
                    "size": {"mode": "custom", "w": 256, "h": 256},
                    "fmt": "rgba8",
                }
            return {**base, "kind": "alias", "of": src, "inputs": [src]}

        if t == "renderselect":
            src = self._select_source(path)
            k = int(float(N.const(path, "bufferindex", 0) or 0))
            return {
                **base,
                "kind": "renderselect",
                "of": src,
                "buffer": k,
                "inputs": [src] if src else [],
            }

        if t == "depth":
            r = N.resolve(N.const(path, "op") or N.const(path, "rendertop"), path)
            return {**base, "kind": "depth", "of": r, "inputs": [r] if r else []}

        if t == "script":
            self.B.top(path)
            return {**base, "kind": "script", "op": path, "fmt": "rgba8", "inputs": []}

        if t == "moviefilein":
            f = N.const(path, "file")
            tex = self.texture_from_file(f, sid)
            if tex is None:
                self.coverage.append(f"{path}: movie/image {f!r} not found — test card")
            return {**base, "kind": "image", "texture": tex, "inputs": []}

        if t == "importselect":
            tex = self.fbx_texture(path, sid)
            return {**base, "kind": "image", "texture": tex, "inputs": []}

        if t == "glsl":
            return self.compile_glsl(path, base)

        if t == "render":
            return self.compile_render(path, base)

        if t == "blur":
            return self._pass(
                path, base, "blur", HS.blur_top(), {"uRadius": self.val(path, "size", 1.0)}
            )

        if t == "fit":
            mode = FIT_MODES.get(str(N.const(path, "fit", "fitbest") or "fitbest"), 3)
            bg = [self.val(path, "bgcolor" + c, d) for c, d in zip("rgba", (0.0, 0.0, 0.0, 1.0))]
            return self._pass(path, base, "fit", HS.fit_top(), {"uBg": bg}, extra={"mode": mode})

        if t == "flip":
            flip = [
                self.val(path, "flipx", 0.0),
                self.val(path, "flipy", 0.0),
                self.val(path, "flop", 0.0),
            ]
            node = self._pass(path, base, "flip", HS.flip_top(), {"uFlip": flip})
            if node["size"]["mode"] == "input":
                node["size"] = {"mode": "input_flop", "flop": flip[2]}
            return node

        if t == "resolution":
            return self._pass(path, base, "resolution", HS.resolution_top(), {})

        if t == "composite":
            op_ = str(N.const(path, "operand", "over") or "over")
            return self._pass(path, base, "composite", HS.composite_top(len(wired), op_), {})

        if t == "level":
            lv = [
                self.val(path, "brightness1", 1.0),
                self.val(path, "gamma1", 1.0),
                self.val(path, "contrast", 1.0),
                self.val(path, "opacity", 1.0),
            ]
            return self._pass(path, base, "level", HS.level_top(), {"uLevel": lv})

        if t == "feedback":
            tgt = N.resolve(N.const(path, "top"), path)
            return {
                **base,
                "kind": "feedback",
                "of": tgt,
                "size": {"mode": "input"} if wired else {"mode": "of", "node": tgt},
                "fmt": "rgba8",
            }

        self.unsupported.add(f"TOP:{t}")
        self.coverage.append(f"{path}: TOP:{t} unsupported — passing input 0 through")
        if wired:
            return {**base, "kind": "alias", "of": wired[0]}
        return {
            **base,
            "kind": "blank",
            "size": {"mode": "custom", "w": 256, "h": 256},
            "fmt": "rgba8",
        }

    def _write_shader(self, name, src):
        rel = f"shaders/{name}"
        with open(os.path.join(self.outdir, rel), "w") as fh:
            fh.write(src)
        return rel

    def _pass(self, path, base, kind, frag, uniforms, extra=None):
        sid = _sid(path)
        node = {
            **base,
            "kind": kind,
            "size": self.size_rule(path, base["inputs"]),
            "fmt": self.fmt(path, None),
            "frag": self._write_shader(f"{sid}.frag", frag),
            "uniforms": uniforms,
        }
        if extra:
            node.update(extra)
        return node

    def compile_glsl(self, path, base):
        N = self.N
        wired = list(base["inputs"])
        for nm in str(N.const(path, "tops", "") or "").split():
            r = N.resolve(nm, path)
            if r and N.fam(r) == "TOP":
                wired.append(r)
        pd = N.resolve(N.const(path, "pixeldat"), path)
        src = (N.ops.get(pd) or {}).get("text") if pd else None
        if not src:
            self.coverage.append(f"{path}: GLSL TOP has no pixel shader — passthrough")
            src = (
                "out vec4 fragColor;\nvoid main(){ fragColor = texture(sTD2DInputs[0], vUV.st); }\n"
            )
        uniforms = {}
        i = 0
        while N.raw(path, f"vec{i}name") is not None:
            nm = str(N.const(path, f"vec{i}name") or "")
            if nm:
                uniforms[nm] = [self.val(path, f"vec{i}value{c}", 0.0) for c in "xyzw"]
            i += 1
        i = 0
        while N.raw(path, f"const{i}name") is not None:
            nm = str(N.const(path, f"const{i}name") or "")
            if nm:
                uniforms[nm] = self.val(path, f"const{i}value", 0.0)
            i += 1
        nbuf = int(float(N.const(path, "numcolorbufs", 1) or 1))
        sid = _sid(path)
        return {
            **base,
            "inputs": wired,
            "kind": "glsl",
            "size": self.size_rule(path, wired),
            "fmt": self.fmt(path, None),
            "outputs": max(1, nbuf),
            "frag": self._write_shader(f"{sid}.frag", HS.td_glsl_top(src, len(wired))),
            "uniforms": uniforms,
        }

    # ------------------------------------------------------------ textures
    def texture_from_file(self, f, sid):
        lp = self.local_path(str(f or ""))
        if lp is None:
            return None
        return self._add_texture(lp, sid)

    def _add_texture(self, lp, sid):
        from PIL import Image

        rel = f"assets/{sid}.png"
        dst = os.path.join(self.outdir, rel)
        if not os.path.isfile(dst):
            Image.open(lp).convert("RGBA").transpose(Image.FLIP_TOP_BOTTOM).save(dst)
        tex_id = sid
        self.textures[tex_id] = {"file": rel, "origin": "bottom"}
        return tex_id

    def fbx_texture(self, path, sid):
        """Import Select TOP inside an FBX COMP's touchTextures: the named texture
        from the FBX's embedded-media folder (<file>.fbm) or beside the .fbx."""
        N = self.N
        name = str(N.const(path, "texture", "") or "")
        comp = path.rsplit("/", 2)[0]  # .../fbxcomp/touchTextures/x
        f = N.const(comp, "file")
        lp = self.local_path(str(f or ""))
        cands = []
        if lp:
            stem = os.path.splitext(lp)[0]
            cands += [os.path.join(stem + ".fbm", name), os.path.join(os.path.dirname(lp), name)]
        for c in cands:
            if os.path.isfile(c):
                return self._add_texture(c, sid)
        self.coverage.append(f"{path}: FBX texture {name!r} not found near {f!r}")
        return None

    # ------------------------------------------------------------ render
    def _render_list(self, path, par, default):
        v = self.N.const(path, par, None)
        if v is None:
            v = default
        out = []
        for tok in str(v).split():
            if any(ch in tok for ch in "*?"):
                parent = path.rsplit("/", 1)[0] or "/"
                for c in self.N.children(parent):
                    if fnmatch.fnmatchcase(c.rsplit("/", 1)[-1], tok):
                        out.append(c)
            else:
                r = self.N.resolve(tok, path)
                if r:
                    out.append(r)
        return out

    def _render_geos(self, path):
        N = self.N
        return [
            g
            for g in self._render_list(path, "geometry", "*")
            if N.fam(g) == "COMP" and N.typ(g) == "geometry"
        ]

    def _material_of(self, geo):
        m = self.N.const(geo, "material")
        return self.N.resolve(m, geo, self_first=True) if m else None

    def _render_map_tops(self, path):
        out = []
        for g in self._render_geos(path):
            m = self._material_of(g)
            if not m:
                continue
            for key in ("diffusemap", "normalmap", "colormap", "alphamap"):
                r = self.N.resolve(self.N.const(m, key), m)
                if r and self.N.fam(r) == "TOP":
                    out.append(r)
        return out

    def compile_render(self, path, base):
        N = self.N
        cams = self._render_list(path, "camera", "cam1")
        cam = next((c for c in cams if N.typ(c) == "camera"), None)
        camera = None
        if cam:
            camera = {
                "mat": self.B.mat(cam),
                "fov": self.val(cam, "fov", 45.0),
                "near": self.val(cam, "near", 0.1),
                "far": self.val(cam, "far", 1000.0),
            }
        else:
            self.coverage.append(f"{path}: no camera — default view")
        lights = []
        for lp in self._render_list(path, "lights", "*"):
            lt = N.typ(lp)
            if lt == "light":
                ltype = str(N.const(lp, "lighttype", "point") or "point")
                if ltype not in ("point", "cone"):
                    self.coverage.append(f"{lp}: {ltype} light treated as point")
                lights.append(
                    {
                        "kind": "point",
                        "mat": self.B.mat(lp),
                        "color": [self.val(lp, c, 1.0) for c in ("cr", "cg", "cb")],
                        "dimmer": self.val(lp, "dimmer", 1.0),
                        "atten": [
                            1.0 if str(N.const(lp, "attenuated", "off")) in ("on", "1") else 0.0,
                            self.val(lp, "attenuationstart", 0.0),
                            self.val(lp, "attenuationend", 10.0),
                            self.val(lp, "attenuationexp", 2.0),
                        ],
                    }
                )
            elif lt == "ambientlight":
                lights.append(
                    {
                        "kind": "ambient",
                        "color": [
                            self.val(lp, c, d)
                            for c, d in (("cr", 0.05), ("cg", 0.05), ("cb", 0.05))
                        ],
                        "dimmer": self.val(lp, "dimmer", 1.0),
                    }
                )
        geos = []
        for g in self._render_geos(path):
            gd = self.compile_geo(path, g)
            if gd is not None:
                geos.append(gd)
        bg = [self.val(path, "bgcolor" + c, 0.0) for c in "rgba"]
        maps = self._render_map_tops(path)
        return {
            **base,
            "kind": "render",
            "inputs": sorted(set(maps)),
            "size": {
                "mode": "custom",
                "w": self.val(path, "resolutionw", 256),
                "h": self.val(path, "resolutionh", 256),
            },
            "fmt": self.fmt(path, "rgba8"),
            "camera": camera,
            "lights": lights,
            "geos": geos,
            "depth": path in self._render_needs_depth,
            "bg": bg,
            "msaa": 4,
        }

    def compile_geo(self, render, g):
        sid = _sid(g)
        sop = self._render_sop(g)
        if sop is None:
            self.coverage.append(f"{g}: no SOP with the render flag — skipped")
            return None
        recipe = self._sop_recipe(sop)
        if recipe is None:
            return None
        defines = []
        geo = {"op": g, "mat": self.B.mat(g), "render": self.B.flag(g, "render")}
        mat = self._material_of(g)
        material = self.compile_material(mat, defines)
        geo["material"] = material
        if recipe["kind"] == "fbx":
            mesh_id, skinned = self.bake_fbx_mesh(g, recipe, want_skin=material.get("_deform"))
            geo["mesh"] = {"baked": mesh_id}
            if skinned:
                defines.append("SKINNED")
                skin = self.meshes[mesh_id]["skin"]
                geo["skin"] = {
                    "root_mat": self.B.mat(skin["root"]),
                    "bone_mats": [self.B.mat(b) for b in skin["bones"]],
                }
        elif recipe["kind"] == "sop":
            self.B.sop(recipe["sop"])
            geo["mesh"] = {"sop": recipe["sop"], "static": recipe["xform"].T.reshape(-1).tolist()}
            defines.append("POINT_COLOR")
        inst = self.compile_instancing(g)
        if inst:
            geo["inst"] = inst
            defines.append("INSTANCED")
        if render in self._render_needs_depth:
            defines.append("DEPTH_OUT")
        material.pop("_deform", None)
        vert, frag = HS.scene_program(sorted(set(defines + material.pop("_defines", []))))
        key = f"{_sid(render)}__{sid}"
        geo["vert"] = self._write_shader(f"{key}.vert", vert)
        geo["frag"] = self._write_shader(f"{key}.frag", frag)
        return geo

    def _render_sop(self, g):
        N = self.N
        sops = [c for c in N.children(g) if N.fam(c) == "SOP"]
        for c in sops:
            if N.flag(c, "render"):
                return c
        for c in sops:
            if N.flag(c, "display"):
                return c
        return sops[-1] if sops else None

    def _sop_recipe(self, sop, depth=0):
        """Walk a SOP chain back to its source; fold static transforms."""
        N = self.N
        if depth > 32:
            return None
        t = N.typ(sop)
        ins = [s for s in N.ops[sop]["inputs"] if N.fam(s) == "SOP"]
        if t in ("null", "bonegroup", "out", "in") and ins:
            return self._sop_recipe(ins[0], depth + 1)
        if t == "select":
            src = N.resolve(N.const(sop, "sops") or N.const(sop, "sop"), sop)
            return self._sop_recipe(src, depth + 1) if src else None
        if t == "transform":
            inner = self._sop_recipe(ins[0], depth + 1) if ins else None
            if inner is None:
                return None
            if any(
                N.is_expr(sop, k)
                for k in ("tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz", "scale")
            ):
                self.coverage.append(f"{sop}: animated Transform SOP baked at its saved value")
            inner["xform"] = self._static_xform(sop) @ inner["xform"]
            return inner
        if t == "importselect":
            comp = self._fbx_comp_of(sop)
            geopath = str(N.const(sop, "geometry", "") or "")
            return {"kind": "fbx", "comp": comp, "geopath": geopath, "sop": sop, "xform": np.eye(4)}
        if t == "script":
            return {"kind": "sop", "sop": sop, "xform": np.eye(4)}
        self.unsupported.add(f"SOP:{t}")
        self.coverage.append(f"{sop}: SOP:{t} unsupported as render geometry")
        return None

    def _static_xform(self, op):
        from tdhost.host import Host

        def f(n, d):
            try:
                return float(self.N.const(op, n, d))
            except (TypeError, ValueError):
                return d

        t = np.array([f("tx", 0.0), f("ty", 0.0), f("tz", 0.0)])
        r = (f("rx", 0.0), f("ry", 0.0), f("rz", 0.0))
        s = np.array([f("sx", 1.0), f("sy", 1.0), f("sz", 1.0)]) * f("scale", 1.0)
        piv = np.array([f("px", 0.0), f("py", 0.0), f("pz", 0.0)])
        xord = str(self.N.const(op, "xord", "srt") or "srt")
        rord = str(self.N.const(op, "rord", "xyz") or "xyz")
        return Host._compose(t, r, s, piv, xord, rord)

    def _fbx_comp_of(self, path):
        p = path.rsplit("/", 1)[0]
        while p and p != "/":
            if self.N.typ(p) == "fbx":
                return p
            p = p.rsplit("/", 1)[0] or "/"
        return None

    def _fbx_scene(self, comp):
        f = self.N.const(comp, "file")
        lp = self.local_path(str(f or ""))
        if lp is None:
            raise CompileError(f"{comp}: FBX file {f!r} not found (add an asset root or path map)")
        if lp not in self._fbx_cache:
            self.log(f"[fbx] reading {lp}")
            self._fbx_cache[lp] = FBX.Scene(lp)
        return self._fbx_cache[lp]

    def bake_fbx_mesh(self, geo, recipe, want_skin=True):
        comp = recipe["comp"]
        sc = self._fbx_scene(comp)
        mesh = sc.mesh_by_path(recipe["geopath"])
        if mesh is None:
            raise CompileError(f"{recipe['sop']}: FBX geometry {recipe['geopath']!r} not found")
        cp, nrm, uv, tris = FBX.triangulate(mesh)
        pos = mesh.points[cp]
        X = recipe["xform"]
        if not np.allclose(X, np.eye(4)):
            pos = (np.c_[pos, np.ones(len(pos))] @ X.T)[:, :3]
            nm = np.linalg.inv(X[:3, :3]).T
            nrm = nrm @ nm.T
            nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-12
        tan = FBX.tangents(pos, nrm, uv, tris)
        skinned = bool(want_skin and mesh.clusters)
        cols = [
            pos.astype(np.float32),
            nrm.astype(np.float32),
            uv.astype(np.float32),
            tan.astype(np.float32),
        ]
        layout = ["pos3", "nrm3", "uv2", "tan4"]
        skin = None
        if skinned:
            bidx, bwt = FBX.skin_weights(mesh, len(mesh.points))
            cols += [bidx[cp], bwt[cp]]
            layout += ["bidx4", "bwt4"]
            # TouchDesigner's FBX COMP names bone nullCOMPs after the FBX models,
            # inside the FBX COMP; bind = the bone's global at bind (TransformLink)
            bones, inv_bind = [], []
            for cl in mesh.clusters:
                name = sc.models[cl.bone_id].name
                bp = self._find_bone(comp, name)
                if bp is None:
                    self.coverage.append(f"{comp}: bone {name!r} has no COMP — rest pose")
                bones.append(bp or comp)
                inv_bind.append(np.linalg.inv(cl.transform_link).T.reshape(-1).tolist())
            skin = {"root": comp, "bones": bones, "inv_bind": inv_bind}
        data = np.concatenate([c.reshape(len(pos), -1) for c in cols], axis=1).astype(np.float32)
        mesh_id = _sid(geo)
        vrel, irel = f"meshes/{mesh_id}.bin", f"meshes/{mesh_id}.idx"
        data.tofile(os.path.join(self.outdir, vrel))
        tris.astype(np.uint32).tofile(os.path.join(self.outdir, irel))
        self.meshes[mesh_id] = {
            "file": vrel,
            "idx": irel,
            "vertices": int(len(pos)),
            "count": int(tris.size),
            "layout": layout,
            "stride": int(data.shape[1] * 4),
            "skin": skin,
        }
        self.log(
            f"[mesh] {geo}: {len(pos)} vertices, {len(tris)} triangles{' (skinned)' if skinned else ''}"
        )
        return mesh_id, skinned

    def _find_bone(self, comp, name):
        for p in self.N.ops:
            if (
                p.startswith(comp + "/")
                and p.rsplit("/", 1)[-1] == name
                and self.N.fam(p) == "COMP"
            ):
                return p
        return None

    def compile_material(self, mat, defines):
        N = self.N
        out = {"_defines": []}
        if mat is None or N.typ(mat) != "phong":
            if mat is not None:
                self.coverage.append(f"{mat}: MAT:{N.typ(mat)} rendered as default Phong")
            out.update(
                diff=[1.0, 1.0, 1.0],
                amb=[1.0, 1.0, 1.0],
                spec=[0.0, 0.0, 0.0],
                emit=[0.0, 0.0, 0.0],
                const=[0.0, 0.0, 0.0],
                shininess=25.0,
                alpha=1.0,
                alphathreshold=0.0,
                bump=1.0,
                maps={},
                _deform=False,
            )
            return out

        def rgb(prefix, d):
            return [self.val(mat, prefix + c, d) for c in "rgb"]

        out.update(
            diff=rgb("diff", 1.0),
            amb=rgb("amb", 1.0),
            spec=rgb("spec", 1.0),
            emit=rgb("emit", 0.0),
            const=rgb("constant", 0.0),
            shininess=self.val(mat, "shininess", 51.2),
            alpha=self.val(mat, "alphafront", 1.0),
            alphathreshold=self.val(mat, "alphathreshold", 0.0),
            bump=self.val(mat, "bumpscale", 1.0),
        )
        maps = {}
        for key, define, slot in (
            ("diffusemap", "DIFFUSE_MAP", "diffuse"),
            ("normalmap", "NORMAL_MAP", "normal"),
            ("colormap", "COLOR_MAP", "color"),
            ("alphamap", "ALPHA_MAP", "alpha"),
        ):
            r = N.resolve(N.const(mat, key), mat)
            if r and N.fam(r) == "TOP":
                maps[slot] = {
                    "top": r,
                    "filter": str(N.const(mat, key + "filter", "mipmaplinear") or ""),
                }
                out["_defines"].append(define)
        out["maps"] = maps
        if str(N.const(mat, "alphatest", "off")) in ("on", "1"):
            out["_defines"].append("ALPHA_TEST")
        out["_defines"].append("POINT_COLOR")
        out["_deform"] = str(N.const(mat, "dodeform", "off")) in ("on", "1")
        return out

    def compile_instancing(self, g):
        N = self.N
        if str(N.const(g, "instancing", "off")) not in ("on", "1"):
            return None
        chop = N.resolve(N.const(g, "instanceop"), g)
        if chop is None:
            self.coverage.append(f"{g}: instancing CHOP unresolved")
            return None

        def ch(par):
            v = N.const(g, par)
            return str(v) if v not in (None, "") else None

        spec = {
            "chop": chop,
            "t": [ch("instancetx"), ch("instancety"), ch("instancetz")],
            "s": [ch("instancesx"), ch("instancesy"), ch("instancesz")],
            "r": [ch("instancerx"), ch("instancery"), ch("instancerz")],
            "rotto": [ch("instancerottox"), ch("instancerottoy"), ch("instancerottoz")],
            "up": [ch("instancerotupx"), ch("instancerotupy"), ch("instancerotupz")],
            "color": [ch("instancer"), ch("instanceg"), ch("instanceb")],
            "colormode": str(N.const(g, "instancecolormode", "replace") or "replace"),
            "active": ch("instanceactive"),
            "forward": str(N.const(g, "instancerottoforward", "posz") or "posz"),
        }
        chans = [c for key in ("t", "s", "r", "rotto", "up", "color") for c in spec[key] if c] + (
            [spec["active"]] if spec["active"] else []
        )
        self.B.chop(chop, chans)
        return spec

    # ------------------------------------------------------------ output
    def window_spec(self):
        if not self.window:
            return None
        w = self.window
        return {
            "op": w,
            "display": self.val(w, "display", 0.0),
            "size": str(self.N.const(w, "size", "automatic") or "automatic"),
            "borders": str(self.N.const(w, "borders", "on")) in ("on", "1"),
        }

    def sync_dat_files(self):
        """Text DATs with Sync to File on read their file at startup in TD, so the
        file (when present) is the source of truth, not the text snapshot the .toe
        saved. Same here: refresh each synced DAT from its file."""
        n = 0
        for p, o in self.N.ops.items():
            if o["family"] != "DAT":
                continue
            if str(self.N.const(p, "syncfile", "off")) not in ("on", "1"):
                continue
            lp = self.local_path(str(self.N.const(p, "file", "") or ""))
            if lp:
                with open(lp, encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
                if txt != o.get("text"):
                    o["text"] = txt
                    n += 1
        if n:
            self.log(f"[host] refreshed {n} file-synced DAT(s) from disk")

    def emit(self, project_name="project", project_files=None, python=None):
        window = self.window_spec()  # may add bindings: must precede the layout
        n_pars = len(self.B.pars)
        nodes = [_finalize_refs(self.nodes[p], n_pars) for p in self.order]
        # the host package and the network
        pkg_src = os.path.join(_ROOT, "pyhost", "tdhost")
        pkg_dst = os.path.join(self.outdir, "host", "tdhost")
        if os.path.isdir(pkg_dst):
            shutil.rmtree(pkg_dst)
        shutil.copytree(pkg_src, pkg_dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with open(os.path.join(self.outdir, "host", "network.json"), "w") as fh:
            json.dump(self.netdict, fh)
        files = list(project_files or [])
        if python and python.get("requirements") and python["requirements"] not in files:
            files.append(python["requirements"])
        copied = self.copy_project_files(files)
        schedule = {
            "format": "toxc-host/1",
            "target": "desktop_gl",
            "output": self.sink,
            "window": _finalize_refs(window, n_pars),
            "host": {
                "network": "host/network.json",
                "pkg": "host",
                "project_folder": "project",
                "project_name": project_name,
                "path_map": {src: "project" for src in self._project_roots()},
                "python": python,
            },
            "bindings": self.B.to_json(),
            "nodes": nodes,
            "meshes": self.meshes,
            "textures": self.textures,
        }
        with open(os.path.join(self.outdir, "schedule.json"), "w") as fh:
            json.dump(schedule, fh, indent=1)
        with open(os.path.join(self.outdir, "coverage.json"), "w") as fh:
            json.dump(
                {
                    "coverage": self.coverage,
                    "unsupported": sorted(self.unsupported),
                    "project_files": copied,
                },
                fh,
                indent=1,
            )
        return schedule

    def _project_roots(self):
        """Authoring-machine project folders baked into parameters (the path map
        sources), so the host rewrites them to the deployed project folder."""
        return [src for src in self.path_map]

    def copy_project_files(self, patterns):
        if not self.project_dir or not patterns:
            return []
        out = []
        dst_root = os.path.join(self.outdir, "project")
        for pat in patterns:
            for f in glob.glob(os.path.join(self.project_dir, pat), recursive=True):
                if os.path.isdir(f) or "__pycache__" in f:
                    continue
                rel = os.path.relpath(f, self.project_dir)
                d = os.path.join(dst_root, rel)
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy2(f, d)
                out.append(rel)
        return sorted(out)


def needs_python_host(dirroot: str) -> bool:
    """True when the project runs Python the classic path cannot compile away:
    Execute-family DATs, Script operators, or Python parameter expressions that
    read storage or call into modules."""
    net = load_network(dirroot)
    for p, o in net["ops"].items():
        if o["family"] == "DAT" and o["type"] in (
            "execute",
            "parexec",
            "chopexec",
            "datexec",
            "opexec",
        ):
            return True
        if o["type"] == "script" and o["family"] in ("TOP", "CHOP", "SOP", "DAT"):
            return True
        for spec in o.get("params", {}).values():
            ex = spec.get("expr") or ""
            if spec.get("mode", 0) & 1 and (".fetch(" in ex or ".module" in ex or "mod." in ex):
                return True
    return False


def read_manifest(project_dir: str | None) -> dict:
    """Optional `td-deploy.json` beside the .toe:
    {"files": ["scripts/**", "models/*.onnx"],   # shipped into project/
     "python": {"requirements": "requirements-linux.txt"}}"""
    if not project_dir:
        return {}
    fn = os.path.join(project_dir, "td-deploy.json")
    if os.path.isfile(fn):
        with open(fn) as fh:
            return json.load(fh)
    return {}


def compile_host(
    dirroot: str,
    outdir: str,
    *,
    project_dir: str | None = None,
    project_name: str = "project",
    path_map: dict | None = None,
    asset_roots: list | None = None,
    log=print,
) -> dict:
    manifest = read_manifest(project_dir)
    hc = HostCompiler(
        dirroot,
        outdir,
        project_dir=project_dir,
        path_map=path_map,
        asset_roots=asset_roots,
        log=log,
    ).compile()
    sched = hc.emit(
        project_name=project_name,
        project_files=manifest.get("files"),
        python=manifest.get("python"),
    )
    log(
        f"[host] {len(sched['nodes'])} nodes, {len(hc.B.pars)} parameter bindings, "
        f"{len(hc.B.mats)} matrices, {len(sched['meshes'])} meshes, {len(sched['textures'])} textures"
    )
    if hc.unsupported:
        log(f"[host] unsupported: {sorted(hc.unsupported)}")
    for c in hc.coverage:
        log(f"[host] {c}")
    return {"schedule": sched, "coverage": hc.coverage, "unsupported": sorted(hc.unsupported)}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="compile a Python-host TouchDesigner project")
    ap.add_argument("dir", help="toeexpand *.dir tree")
    ap.add_argument("out")
    ap.add_argument("--project-dir")
    ap.add_argument("--project-name", default="project")
    ap.add_argument("--path-map", action="append", default=[], help="SRC=DST")
    ap.add_argument("--asset-root", action="append", default=[])
    a = ap.parse_args()
    pm = dict(x.split("=", 1) for x in a.path_map)
    compile_host(
        a.dir,
        a.out,
        project_dir=a.project_dir,
        project_name=a.project_name,
        path_map=pm,
        asset_roots=a.asset_root,
    )
