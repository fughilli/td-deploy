"""The Python host: owns the emulated network and runs the project's Python.

Frame protocol with the renderer (runtime_rs/src/pyhost.rs), once per frame:

    out = host.frame(t, readbacks)   # onFrameStart callbacks, run() queue, Script
                                     # op cooks, bound parameter/matrix values
    ... GPU cooks the TOP graph ...
    req = host.frame_end()           # onFrameEnd callbacks; TOPs they read back

`readbacks` maps a TOP path to the float32 RGBA pixels (row 0 = bottom, as TD's
numpyArray) the renderer downloaded after the previous cook, for every TOP the
scripts asked for — which is exactly TD's `numpyArray(delayed=True)` contract.

Bindings are declared once (`host.bind(spec)`) by the renderer: the parameters,
object world matrices, Script TOP/CHOP/SOP outputs and render flags the compiled
schedule depends on. `frame()` returns them packed:

    {"floats": float64[n_scalars], "mats": float64[n_mats * 16],
     "tops": {path: uint8/float32 HxWx4 (only when newly cooked)},
     "chops": {path: {chan: float32[n]}}, "sops": {path: mesh dict (when changed)}}
"""

from __future__ import annotations

import math
import os
import sys
import traceback
import types

import numpy as np

from . import network as N
from . import tdu as _tdu

# Names every TD Python context sees (expressions, DAT modules, run() scripts).
_TD_NAMES = ("tdu",)


class Host:
    def __init__(
        self,
        net: dict,
        project_folder: str | None = None,
        *,
        project_name: str = "project",
        monitors: list[dict] | None = None,
        path_map: dict[str, str] | None = None,
        log=None,
    ):
        self._id = 0
        self.log = log or (lambda msg: print(f"[tdhost] {msg}", flush=True))
        self.absTime = N._AbsTime()
        start = net.get("start", {})
        self.absTime.rate = float(start.get("cookrate", 60.0))
        self.absTime.stepSeconds = 1.0 / self.absTime.rate
        # Host paths baked into the project (the author's Mac) -> device paths.
        self.path_map = dict(path_map or {})
        self.project = N._Project(
            self,
            project_folder or os.getcwd(),
            project_name,
            self.absTime.rate,
            start.get("realtime", True),
        )
        self.monitors = N._Monitors(
            N.Monitor(
                i,
                m["width"],
                m["height"],
                m.get("left", 0),
                m.get("top", 0),
                i == 0,
                m.get("name", "display"),
            )
            for i, m in enumerate(monitors or [{"width": 1920, "height": 1080}])
        )
        self.quit_requested = False
        self._window_open: dict[str, bool] = {}
        self._run_queue: list = []
        self._expr_errors: dict = {}
        self._deps = None  # active dependency recorder during a Script SOP cook
        self._top_sizes: dict[str, tuple[int, int]] = {}
        self._readbacks: dict[str, np.ndarray] = {}
        self._readback_requests: set[str] = set()
        self._script_top_arrays: dict[str, np.ndarray] = {}
        self._script_top_dirty: set[str] = set()
        self._sop_state: dict[str, tuple] = {}
        self._sop_meshes: dict[str, dict] = {}
        self._sop_dirty: set[str] = set()
        self._chop_cooked_frame: dict[str, int] = {}
        self._cooking: set[str] = set()
        self._bindings: dict | None = None
        self._frame_callbacks_started = False

        self.ops: dict[str, N.OP] = {}
        self.ops["/"] = N.COMP(self, "/", {"type": "container", "flags": {}})
        for path in sorted(net["ops"]):
            rec = net["ops"][path]
            cls = N.FAMILY_CLASS.get(rec.get("family"), N.OP)
            self.ops[path] = cls(self, path, rec)
        self._expr_globals = self._base_globals()
        self._install_td_module()

    # ------------------------------------------------------------------ ids / misc
    def _next_id(self):
        self._id += 1
        return self._id

    def map_path(self, p: str) -> str:
        """Rewrite a path baked on the authoring machine to its place on device."""
        for src, dst in self.path_map.items():
            if p.startswith(src):
                return dst + p[len(src) :]
        return p

    # ------------------------------------------------------------------ resolution
    def resolve(self, path, context="/"):
        if isinstance(path, N.OP):
            return path
        if path is None:
            return None
        path = str(path).strip()
        if not path:
            return None
        if path.startswith("/"):
            full = path
        else:
            full = (context.rstrip("/") + "/" + path) if context != "/" else "/" + path
        parts: list[str] = []
        for seg in full.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(seg)
        return self.ops.get("/" + "/".join(parts))

    def resolve_many(self, pattern, context="/"):
        import fnmatch

        if not any(ch in pattern for ch in "*?["):
            o = self.resolve(pattern, context)
            return [o] if o else []
        full = pattern if pattern.startswith("/") else (context.rstrip("/") + "/" + pattern)
        return [o for p, o in self.ops.items() if fnmatch.fnmatchcase(p, full)]

    # ------------------------------------------------------------------ Python contexts
    def _base_globals(self) -> dict:
        g = {
            "__builtins__": __builtins__,
            "math": math,
            "absTime": self.absTime,
            "tdu": _tdu,
            "project": self.project,
            "monitors": self.monitors,
            "ui": N._UI(),
            "app": N._App(),
            "var": N._var,
            "debug": N._debug,
            "Par": N.Par,
            "ParMode": N.ParMode,
            "OP": N.OP,
            "COMP": N.COMP,
            "TOP": N.TOP,
            "CHOP": N.CHOP,
            "SOP": N.SOP,
            "DAT": N.DAT,
            "MAT": N.MAT,
            "root": self.ops["/"],
        }
        for t in (
            "baseCOMP",
            "containerCOMP",
            "geometryCOMP",
            "nullCOMP",
            "cameraCOMP",
            "lightCOMP",
            "windowCOMP",
            "textDAT",
            "tableDAT",
            "executeDAT",
            "glslTOP",
            "renderTOP",
            "nullTOP",
            "scriptTOP",
            "scriptCHOP",
            "scriptSOP",
            "rampTOP",
            "phongMAT",
        ):
            g[t] = type(t, (), {"__name__": t})
        return g

    def _context_names(self, me: N.OP) -> dict:
        ctx = me.path if isinstance(me, N.COMP) and False else (me.path.rsplit("/", 1)[0] or "/")
        return {
            "me": me,
            "op": N._OpFinder(self, ctx),
            "ops": lambda *pats: [o for p in pats for o in self.resolve_many(p, ctx)],
            "parent": N._Parent(self, me),
            "mod": N._ModFinder(self, ctx),
            "run": lambda script, *a, **k: self.run(script, *a, fromOP=k.pop("fromOP", me), **k),
            "iop": _Shortcuts(self, me, "iopshortcut"),
            "ipar": _Shortcuts(self, me, "iparshortcut", pars=True),
        }

    def _expr_locals(self, owner: N.OP) -> dict:
        cache = getattr(owner, "_expr_ctx", None)
        if cache is None:
            cache = self._context_names(owner)
            owner._expr_ctx = cache
        return cache

    def _compile(self, src: str, where: str):
        return compile(src.strip(), where, "eval")

    def _expr_error(self, par, e):
        key = (par.owner.path, par.name)
        msg = f"{type(e).__name__}: {e}"
        if self._expr_errors.get(key) != msg:
            self._expr_errors[key] = msg
            self.log(f"expression error {par.owner.path}:{par.name} = {par.expr!r}: {msg}")

    def _install_td_module(self):
        """`import td` inside project code sees the same names."""
        m = types.ModuleType("td")
        for k, v in self._expr_globals.items():
            if not k.startswith("__"):
                setattr(m, k, v)
        m.op = N._OpFinder(self, "/")
        m.run = lambda script, *a, **k: self.run(script, *a, **k)
        m.absTime = self.absTime
        sys.modules["td"] = m
        sys.modules["tdu"] = _tdu

    def _make_module(self, dat: N.DAT) -> types.ModuleType:
        name = dat.name
        mod = types.ModuleType(name)
        mod.__file__ = dat.path
        g = mod.__dict__
        g.update({k: v for k, v in self._expr_globals.items() if k != "__builtins__"})
        g.update(self._context_names(dat))
        try:
            code = compile(dat.text, dat.path, "exec")
            exec(code, g)
        except Exception:  # noqa: BLE001
            self.log(f"error loading module {dat.path}:\n{traceback.format_exc()}")
        return mod

    # ------------------------------------------------------------------ run() queue
    def run(
        self,
        script,
        *args,
        delayFrames=0,
        delayMilliSeconds=0,
        delayRef=None,
        fromOP=None,
        endFrame=False,
        group=None,
        asParameter=False,
    ):
        due_frame = self.absTime.frame + max(int(delayFrames), 0)
        due_time = self.absTime.seconds + max(float(delayMilliSeconds), 0.0) / 1000.0
        self._run_queue.append((due_frame, due_time, script, args, fromOP))

        class _Run:
            active = True

            def kill(_s):
                _s.active = False

        return _Run()

    def _run_due(self):
        if not self._run_queue:
            return
        now_f, now_t = self.absTime.frame, self.absTime.seconds
        due = [r for r in self._run_queue if r[0] <= now_f and r[1] <= now_t]
        if not due:
            return
        self._run_queue = [r for r in self._run_queue if not (r[0] <= now_f and r[1] <= now_t)]
        for _f, _t, script, args, from_op in due:
            me = from_op or self.ops["/"]
            g = dict(self._expr_globals)
            g.update(self._context_names(me))
            g["args"] = args
            try:
                if callable(script):
                    script(*args)
                else:
                    exec(compile(str(script), f"run:{me.path}", "exec"), g)
            except Exception:  # noqa: BLE001
                self.log(f"run() error from {me.path}:\n{traceback.format_exc()}")

    # ------------------------------------------------------------------ callbacks
    def _callback_module(self, o: N.OP) -> types.ModuleType | None:
        """The module an op's callbacks live in: its own text for Execute-family
        DATs, the DAT named by its `callbacks` parameter for Script ops."""
        if o.type in (
            "execute",
            "parameterexecute",
            "chopexecute",
            "datexecute",
            "opexecute",
            "panelexecute",
        ):
            return o.module
        cb = object.__getattribute__(o.par, "_pars").get("callbacks")
        if cb is None:
            return None
        d = self.resolve(str(cb.eval()), o.path.rsplit("/", 1)[0] or "/")
        return d.module if isinstance(d, N.DAT) else None

    def _call(self, o: N.OP, fn: str, *args):
        mod = self._callback_module(o)
        f = getattr(mod, fn, None) if mod is not None else None
        if f is None:
            return None
        try:
            return f(*args)
        except Exception:  # noqa: BLE001
            self.log(f"{o.path}.{fn} raised:\n{traceback.format_exc()}")
            return None

    def _executes(self, flag: str):
        for o in self.ops.values():
            if o.family == "DAT" and o.type == "execute":
                p = object.__getattribute__(o.par, "_pars")
                if bool(p["active"].eval()) if "active" in p else True:
                    if flag in p and bool(p[flag].eval()):
                        yield o

    def start(self):
        for o in self._executes("start"):
            self._call(o, "onStart")
        self._window_autostart()

    def exit(self):
        for o in self._executes("exit"):
            self._call(o, "onExit")

    def _window_autostart(self):
        for o in self.ops.values():
            if o.family == "COMP" and o.type == "window":
                p = object.__getattribute__(o.par, "_pars")
                if "winopen" in p:
                    pass
                self._window_open.setdefault(o.path, False)

    def _pulse(self, par):
        o = par.owner
        if o.family == "COMP" and o.type == "window":
            if par.name == "winopen":
                self._window_open[o.path] = True
            elif par.name == "winclose":
                self._window_open[o.path] = False
            return
        if o.family == "COMP" and o.type == "window":
            return
        # Parameter Execute DATs watching this op
        for pe in self.ops.values():
            if pe.family == "DAT" and pe.type == "parameterexecute":
                if self._parexec_watches(pe, par):
                    self._call(pe, "onPulse", par)

    def _par_changed(self, par, prev=None):
        for pe in self.ops.values():
            if pe.family == "DAT" and pe.type == "parameterexecute":
                if self._parexec_watches(pe, par):
                    p = object.__getattribute__(pe.par, "_pars")
                    if "valuechange" in p and not bool(p["valuechange"].eval()):
                        continue
                    self._call(pe, "onValueChange", par, prev)

    def _parexec_watches(self, pe, par) -> bool:
        import fnmatch

        p = object.__getattribute__(pe.par, "_pars")
        if "active" in p and not bool(p["active"].eval()):
            return False
        target = self.resolve(str(p["op"].eval()), pe.path.rsplit("/", 1)[0]) if "op" in p else None
        if target is None or target is not par.owner:
            return False
        pats = str(p["pars"].eval()).split() if "pars" in p else ["*"]
        return any(fnmatch.fnmatchcase(par.name, pat) for pat in pats)

    # ------------------------------------------------------------------ dependencies
    def _note_dep(self, par, value):
        if self._deps is not None:
            self._deps[(par.owner.path, par.name)] = _hashable(value)

    # ------------------------------------------------------------------ cooking
    def _cook(self, o: N.OP, force=False):
        if o.path in self._cooking:
            return
        t = o.type
        if o.family == "TOP" and t == "script":
            self._cook_script_top(o)
        elif o.family == "CHOP" and t == "script":
            self._cook_script_chop(o, force=True)
        elif o.family == "SOP" and t == "script":
            self._cook_script_sop(o, force=force)

    def _cook_script_top(self, o):
        self._cooking.add(o.path)
        try:
            self._call(o, "onCook", o)
        finally:
            self._cooking.discard(o.path)

    def _script_top_data(self, o, arr):
        a = np.asarray(arr)
        if a.ndim == 2:
            a = a[:, :, None]
        if a.shape[2] == 1:
            a = np.repeat(a, 4, axis=2)
            a[..., 3] = 1 if a.dtype != np.uint8 else 255
        elif a.shape[2] == 3:
            alpha = np.full(a.shape[:2] + (1,), 255 if a.dtype == np.uint8 else 1.0, a.dtype)
            a = np.concatenate([a, alpha], axis=2)
        self._script_top_arrays[o.path] = np.ascontiguousarray(a)
        self._script_top_dirty.add(o.path)
        self._top_sizes[o.path] = (a.shape[1], a.shape[0])

    def _cook_script_chop(self, o, force=False):
        f = self.absTime.frame
        if not force and self._chop_cooked_frame.get(o.path) == f:
            return
        self._chop_cooked_frame[o.path] = f
        self._cooking.add(o.path)
        try:
            self._call(o, "onCook", o)
        finally:
            self._cooking.discard(o.path)

    def _ensure_chop(self, o):
        if o.type == "script" and o.path not in self._cooking:
            self._cook_script_chop(o)

    def _cook_script_sop(self, o, force=False):
        """Cook when forced, when never cooked, or when any parameter it read last
        time now evaluates differently (TD's dependency-driven recook)."""
        prev = self._sop_state.get(o.path)
        if not force and prev is not None:
            changed = False
            for (path, name), v in prev.items():
                owner = self.ops.get(path)
                if owner is None:
                    continue
                p = owner.par._get(name, create=False)
                if p is not None and _hashable(p.eval()) != v:
                    changed = True
                    break
            if not changed:
                return
        saved = self._deps
        self._deps = {}
        self._cooking.add(o.path)
        try:
            self._call(o, "onCook", o)
        finally:
            deps = self._deps
            self._deps = saved
            self._cooking.discard(o.path)
        self._sop_state[o.path] = deps
        self._sop_meshes[o.path] = sop_to_mesh(o)
        self._sop_dirty.add(o.path)

    # ------------------------------------------------------------------ TOP helpers
    def top_size(self, o) -> tuple[int, int]:
        s = self._top_sizes.get(o.path)
        if s:
            return s
        p = object.__getattribute__(o.par, "_pars")
        try:
            w = int(float(p["resolutionw"].eval())) if "resolutionw" in p else 256
            h = int(float(p["resolutionh"].eval())) if "resolutionh" in p else 256
        except (TypeError, ValueError):
            w, h = 256, 256
        return (w, h)

    def set_top_sizes(self, sizes: dict):
        for k, v in sizes.items():
            self._top_sizes[k] = (int(v[0]), int(v[1]))

    def _readback(self, o, delayed):
        self._readback_requests.add(o.path)
        return self._readbacks.get(o.path)

    # ------------------------------------------------------------------ transforms
    def _xform(self, o, prefix=""):
        p = o.par

        def f(n):
            return float(getattr(p, prefix + n).eval())

        t = np.array([f("tx"), f("ty"), f("tz")])
        r = (f("rx"), f("ry"), f("rz"))
        s = np.array([f("sx"), f("sy"), f("sz")]) * f("scale")
        piv = np.array([f("px"), f("py"), f("pz")]) if not prefix else np.zeros(3)
        xord = str(getattr(p, prefix + "xord").eval()) if not prefix else "srt"
        rord = str(getattr(p, prefix + "rord").eval()) if not prefix else "xyz"
        return t, r, s, piv, xord, rord

    @staticmethod
    def _compose(t, r, s, piv, xord, rord) -> np.ndarray:
        T = np.eye(4)
        T[:3, 3] = t
        R = _tdu.euler_matrix(r[0], r[1], r[2], rord)
        S = np.diag([s[0], s[1], s[2], 1.0])
        P = np.eye(4)
        P[:3, 3] = piv
        Pi = np.eye(4)
        Pi[:3, 3] = -piv
        mats = {"t": T, "r": P @ R @ Pi, "s": P @ S @ Pi}
        m = np.eye(4)
        for k in xord:  # applied in order: first letter first
            m = mats[k] @ m
        return m

    def _pre_matrix(self, o) -> np.ndarray:
        pars = object.__getattribute__(o.par, "_pars")
        if not any(
            k in pars
            for k in ("ptx", "pty", "ptz", "prx", "pry", "prz", "psx", "psy", "psz", "pscale")
        ):
            return np.eye(4)
        t, r, s, piv, xord, rord = self._xform(o, prefix="p")
        return self._compose(t, r, s, piv, xord, rord)

    def local_matrix(self, o) -> np.ndarray:
        t, r, s, piv, xord, rord = self._xform(o)
        return self._compose(t, r, s, piv, xord, rord) @ self._pre_matrix(o)

    def _object_parent(self, o):
        """Object parent: an object COMP wired into input 0, else the containing
        object COMP (objects inside a Geometry/FBX COMP ride along with it)."""
        for ip in o._input_paths:
            po = self.ops.get(ip)
            if po is not None and po.family == "COMP" and po.type in N.OBJECT_TYPES:
                return po
        pp = o.parent()
        if pp is not None and pp.family == "COMP" and pp.type in N.OBJECT_TYPES:
            return pp
        return None

    def world_matrix(self, o) -> np.ndarray:
        m = self.local_matrix(o)
        p = self._object_parent(o)
        guard = 0
        while p is not None and guard < 256:
            m = self.local_matrix(p) @ m
            p = self._object_parent(p)
            guard += 1
        return m

    def set_local_matrix(self, o, m: np.ndarray):
        """setTransform(): TD sets the Xform parameters so transform() == m
        (pivot 0, the op's own xord/rord; the uniform scale is folded to 1)."""
        rord = str(o.par.rord.eval())
        pre = self._pre_matrix(o)
        if not np.allclose(pre, np.eye(4)):
            m = m @ np.linalg.inv(pre)
        s, r, t = _tdu.decompose(m, rord)
        pars = object.__getattribute__(o.par, "_pars")
        for nm, v in zip(("tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz"), (*t, *r, *s)):
            p = pars.get(nm) or o.par._get(nm)
            p._val = float(v)
            p._mode = N.ParMode.CONSTANT
        for nm, v in (("px", 0.0), ("py", 0.0), ("pz", 0.0), ("scale", 1.0)):
            if nm in pars:
                pars[nm]._val = v
                pars[nm]._mode = N.ParMode.CONSTANT

    # ------------------------------------------------------------------ create/copy
    def _create(self, parent, optype, name):
        raise N.TDError("creating operators at runtime is not supported on device")

    def _copy(self, parent, o, name):
        raise N.TDError("copying operators at runtime is not supported on device")

    def _destroy(self, o):
        self.ops.pop(o.path, None)

    # ------------------------------------------------------------------ renderer API
    def bind(self, spec: dict):
        """spec = {"pars": [[op_path, par_name], ...], "mats": [op_path, ...],
        "tops": [script TOP paths], "chops": [[chop_path, [chans]], ...],
        "sops": [script SOP paths], "flags": [[op_path, flag], ...]}"""
        pars = []
        for opath, pname in spec.get("pars", []):
            o = self.ops.get(opath)
            pars.append(o.par._get(pname) if o is not None else None)
        mats = [self.ops.get(p) for p in spec.get("mats", [])]
        flags = []
        for opath, flag in spec.get("flags", []):
            flags.append((self.ops.get(opath), flag))
        self._bindings = {
            "pars": pars,
            "mats": mats,
            "tops": [self.ops.get(p) for p in spec.get("tops", [])],
            "chops": [(self.ops.get(p), list(ch)) for p, ch in spec.get("chops", [])],
            "sops": [self.ops.get(p) for p in spec.get("sops", [])],
            "flags": flags,
        }

    def frame(self, t: float, readbacks: dict | None = None, frame: int | None = None) -> dict:
        at = self.absTime
        prev = at.seconds
        at.seconds = float(t)
        at.frame = int(frame) if frame is not None else at.frame + 1
        at.stepSeconds = max(at.seconds - prev, 1e-6) if at.frame > 1 else 1.0 / at.rate
        if readbacks:
            for path, arr in readbacks.items():
                self._readbacks[path] = arr
        self._run_due()
        for o in self._executes("framestart"):
            self._call(o, "onFrameStart", at.frame)
        return self._snapshot()

    def frame_end(self) -> list:
        for o in self._executes("frameend"):
            self._call(o, "onFrameEnd", self.absTime.frame)
        req = sorted(self._readback_requests)
        self._readback_requests = set()
        return req

    def _snapshot(self) -> dict:
        b = self._bindings or {
            "pars": [],
            "mats": [],
            "tops": [],
            "chops": [],
            "sops": [],
            "flags": [],
        }
        floats = np.zeros(len(b["pars"]) + len(b["flags"]), np.float64)
        for i, p in enumerate(b["pars"]):
            if p is None:
                continue
            v = p.eval()
            floats[i] = _to_float(v)
        k = len(b["pars"])
        for j, (o, flag) in enumerate(b["flags"]):
            floats[k + j] = 1.0 if (o is not None and self.flag(o, flag)) else 0.0
        mats = np.zeros((len(b["mats"]), 16), np.float64)
        for i, o in enumerate(b["mats"]):
            if o is not None:
                # column-major, ready for glUniformMatrix4fv(transpose=false)
                mats[i] = self.world_matrix(o).T.reshape(-1)
        for o in b["sops"]:
            if o is not None:
                self._cook_script_sop(o)
        tops = {}
        for o in b["tops"]:
            if o is None:
                continue
            self._cook_script_top(o)
            if o.path in self._script_top_dirty:
                tops[o.path] = self._script_top_arrays[o.path]
        self._script_top_dirty.clear()
        chops = {}
        for o, chans in b["chops"]:
            if o is None:
                continue
            if o.type == "script":
                self._cook_script_chop(o)
            d = {}
            for c in chans:
                ch = o._chans.get(c) if isinstance(o, N.CHOP) else None
                d[c] = (
                    ch._vals
                    if ch is not None
                    else np.zeros(o._num_samples if isinstance(o, N.CHOP) else 1, np.float32)
                )
            d["__n__"] = o._num_samples if isinstance(o, N.CHOP) else 1
            chops[o.path] = d
        sops = {p: self._sop_meshes[p] for p in self._sop_dirty if p in self._sop_meshes}
        self._sop_dirty.clear()
        return {
            "floats": floats,
            "mats": mats.reshape(-1),
            "tops": tops,
            "chops": chops,
            "sops": sops,
        }

    def flag(self, o, flag: str) -> bool:
        """An op's flag, honouring a parameter of the same name when present (TD
        lets the Render flag be driven by the `render` parameter)."""
        pars = object.__getattribute__(o.par, "_pars")
        base = bool(o._flags.get(flag, True if flag == "render" else False))
        if flag in pars:
            return base and bool(pars[flag].eval())
        return base

    def eval_par(self, opath: str, pname: str):
        o = self.ops.get(opath)
        if o is None:
            return None
        return o.par._get(pname).eval()


class _Shortcuts:
    def __init__(self, host, me, parname, pars=False):
        self._host, self._me, self._parname, self._pars = host, me, parname, pars

    def __getattr__(self, name):
        o = self._me.parent()
        while o is not None:
            for c in o.children if isinstance(o, N.COMP) else []:
                p = object.__getattribute__(c.par, "_pars").get(self._parname)
                if p is not None and str(p.eval()) == name:
                    return c.par if self._pars else c
            o = o.parent()
        raise AttributeError(name)


def _to_float(v) -> float:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _hashable(v):
    if isinstance(v, np.ndarray):
        return v.tobytes()
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _hashable(x)) for k, x in v.items()))
    return v


def sop_to_mesh(o) -> dict:
    """Script SOP geometry -> triangle mesh arrays (pos3, nrm3, color4, uv2, u32
    indices). Polygons fan-triangulate."""
    n = len(o.points)
    pos = np.zeros((n, 3), np.float32)
    nrm = np.zeros((n, 3), np.float32)
    col = np.ones((n, 4), np.float32)
    uv = np.zeros((n, 2), np.float32)
    for i, p in enumerate(o.points):
        pos[i] = p.P
        nrm[i] = p.N
        c = tuple(p.Cd) + (1.0,) * (4 - len(tuple(p.Cd)))
        col[i] = c[:4]
        uv[i] = tuple(p.uv)[:2]
    idx = []
    for poly in o.prims:
        vs = [v.point.index for v in poly.vertices if v.point is not None]
        for k in range(1, len(vs) - 1):
            idx += [vs[0], vs[k], vs[k + 1]]
    return {"pos": pos, "nrm": nrm, "col": col, "uv": uv, "idx": np.asarray(idx, np.uint32)}
