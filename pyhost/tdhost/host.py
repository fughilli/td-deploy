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
        jit=None,
    ):
        self._id = 0
        self.log = log or (lambda msg: print(f"[tdhost] {msg}", flush=True))
        self._jit = _make_jit(self.log, jit)
        self._xf_cache = None  # per-snapshot local/world matrix cache (_snapshot)
        self._resolve_cache: dict = {}  # (path, context) -> normalized path
        self._rec = None  # while evaluating a keepable expression: what it reads
        self.native_xf = False  # the renderer composes world matrices (init caps)
        self._bound_vers: list = []
        self._bound_floats = None
        self._xf = {"nodes": [], "mat_nodes": [], "sent": [], "tree": True}
        self._lm_cache: dict = {}  # op path -> (_pver, local matrix), constant xforms
        self._script_top_src: dict = {}  # Script TOP -> (array given, copy of it sent)
        self._xf_dynamic = False
        self._parexec_list = None
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
        # op('...') runs hundreds of times a frame with the same few paths:
        # memoize the normalized path (the op table itself is looked up fresh)
        key = (path, context) if isinstance(path, str) else None
        full = self._resolve_cache.get(key) if key is not None else None
        if full is None:
            full = self._normalize_path(str(path).strip(), context)
            if key is not None:
                if len(self._resolve_cache) > 8192:
                    self._resolve_cache.clear()
                self._resolve_cache[key] = full
        return self.ops.get(full) if full else None

    @staticmethod
    def _normalize_path(path, context):
        if not path:
            return ""
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
        return "/" + "/".join(parts)

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
        text = dat.text
        # numeric functions get compiled to native code (tdhost/jit.py); that needs
        # the module's code to come from a real file
        fn = self._jit.source_file(dat.path, text) if self._jit is not None else None
        try:
            code = compile(text, fn or dat.path, "exec")
            exec(code, g)
        except Exception:  # noqa: BLE001
            self.log(f"error loading module {dat.path}:\n{traceback.format_exc()}")
            return mod
        if fn:
            try:
                self._jit.wrap_module(dat.path, text, g, fn)
            except Exception:  # noqa: BLE001 - never let the compiler break the project
                self.log(f"jit: {dat.path}: {traceback.format_exc()}")
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
        if self._jit is not None:
            self._jit.exit()

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
        for pe in self._parexecs():
            if self._parexec_watches(pe, par):
                self._call(pe, "onPulse", par)

    def _parexecs(self):
        """The Parameter Execute DATs (the op table only changes by _destroy)."""
        pes = self._parexec_list
        if pes is None:
            pes = self._parexec_list = [
                o for o in self.ops.values() if o.family == "DAT" and o.type == "parameterexecute"
            ]
        return pes

    def _par_changed(self, par, prev=None):
        o = par.owner
        o._pver = getattr(o, "_pver", 0) + 1  # invalidates its cached local matrix
        for pe in self._parexecs():
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
        a = np.ascontiguousarray(a)
        # A Script TOP that re-copies the same unchanged array every cook (a camera
        # frame held until the next one arrives) isn't re-sent: the renderer keeps
        # the texture it has. Compared by content, so an array modified in place is
        # still sent.
        prev = self._script_top_src.get(o.path)
        if (
            prev is not None
            and prev[0] is arr
            and prev[1].shape == a.shape
            and prev[1].dtype == a.dtype
            and _same_bytes(prev[1], a)
        ):
            return
        # keep what was sent, to tell an in-place change from the same pixels next
        # time: a copy, unless the caller's array is read-only (can't change)
        keep = a
        if isinstance(arr, np.ndarray) and arr.flags.writeable and np.shares_memory(a, arr):
            keep = a.copy()
        self._script_top_src[o.path] = (arr, keep)
        self._script_top_arrays[o.path] = a
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
        pars = object.__getattribute__(p, "_pars")
        expr = N.ParMode.EXPRESSION
        tracking = self._deps is not None

        def f(n):
            # Par.eval() without the attribute machinery: constants (almost all
            # transform parameters) are just their value
            par = pars.get(prefix + n)
            if par is None:  # (not `or`: a Par's truth value evaluates it)
                par = p._get(prefix + n)
            if par is None:
                raise AttributeError(prefix + n)
            if tracking or par._mode == expr:
                self._xf_dynamic = True
                return float(par.eval())
            return float(par._val)

        t = np.array([f("tx"), f("ty"), f("tz")])
        r = (f("rx"), f("ry"), f("rz"))
        s = np.array([f("sx"), f("sy"), f("sz")]) * f("scale")
        piv = np.array([f("px"), f("py"), f("pz")]) if not prefix else np.zeros(3)
        xord = str(getattr(p, prefix + "xord").eval()) if not prefix else "srt"
        rord = str(getattr(p, prefix + "rord").eval()) if not prefix else "xyz"
        return t, r, s, piv, xord, rord

    @staticmethod
    def _compose(t, r, s, piv, xord, rord) -> np.ndarray:
        R = _tdu.euler3(r[0], r[1], r[2], rord)
        if xord == "srt" and not (piv[0] or piv[1] or piv[2]):
            # the common case, without the pivot/order bookkeeping: T @ R @ S
            m = np.empty((4, 4))
            m[:3, :3] = R * np.asarray(s, float)  # scales R's columns
            m[:3, 3] = t
            m[3] = (0.0, 0.0, 0.0, 1.0)
            return m
        T = np.eye(4)
        T[:3, 3] = t
        Rm = np.eye(4)
        Rm[:3, :3] = R
        S = np.diag([s[0], s[1], s[2], 1.0])
        P = np.eye(4)
        P[:3, 3] = piv
        Pi = np.eye(4)
        Pi[:3, 3] = -np.asarray(piv, float)
        mats = {"t": T, "r": P @ Rm @ Pi, "s": P @ S @ Pi}
        m = np.eye(4)
        for k in xord:  # applied in order: first letter first
            m = mats[k] @ m
        return m

    _PRE_PARS = ("ptx", "pty", "ptz", "prx", "pry", "prz", "psx", "psy", "psz", "pscale")

    def _pre_matrix(self, o):
        """The Pre-Transform page's matrix, or None when the op has none."""
        pars = object.__getattribute__(o.par, "_pars")
        if not any(k in pars for k in self._PRE_PARS):
            return None
        t, r, s, piv, xord, rord = self._xform(o, prefix="p")
        return self._compose(t, r, s, piv, xord, rord)

    def local_matrix(self, o) -> np.ndarray:
        cache = self._xf_cache
        if cache is not None:
            m = cache.get(("l", o.path))
            if m is not None:
                return m
        # Across frames: an op whose transform parameters are all constants keeps
        # its matrix until one of its parameters is written (_pver). Returned
        # arrays are shared: callers must not modify them in place.
        ver = getattr(o, "_pver", 0)
        kept = self._lm_cache.get(o.path)
        if kept is not None and kept[0] == ver:
            m = kept[1]
        else:
            self._xf_dynamic = False
            t, r, s, piv, xord, rord = self._xform(o)
            m = self._compose(t, r, s, piv, xord, rord)
            pre = self._pre_matrix(o)
            if pre is not None:
                m = m @ pre
            if self._xf_dynamic or self._deps is not None:
                self._lm_cache.pop(o.path, None)
            else:
                m.flags.writeable = False
                self._lm_cache[o.path] = (ver, m)
        if cache is not None:
            cache[("l", o.path)] = m
        return m

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
        cache = self._xf_cache
        if cache is not None:
            m = cache.get(("w", o.path))
            if m is not None:
                return m
        p = self._object_parent(o)
        m = self.local_matrix(o)
        if p is not None:
            # parent's world first: with the snapshot cache a chain of N objects
            # costs N local matrices, not N^2/2
            m = self._world_upto(p, 255) @ m
        if cache is not None:
            cache[("w", o.path)] = m
        return m

    def _world_upto(self, o, depth):
        if depth <= 0:
            return self.local_matrix(o)
        cache = self._xf_cache
        if cache is not None:
            m = cache.get(("w", o.path))
            if m is not None:
                return m
        p = self._object_parent(o)
        m = self.local_matrix(o)
        if p is not None:
            m = self._world_upto(p, depth - 1) @ m
        if cache is not None:
            cache[("w", o.path)] = m
        return m

    def set_local_matrix(self, o, m: np.ndarray):
        """setTransform(): TD sets the Xform parameters so transform() == m
        (pivot 0, the op's own xord/rord; the uniform scale is folded to 1)."""
        rord = str(o.par.rord.eval())
        pre = self._pre_matrix(o)
        if pre is not None and not np.allclose(pre, np.eye(4)):
            m = m @ np.linalg.inv(pre)
        s, r, t = _tdu.decompose(m, rord)
        pars = object.__getattribute__(o.par, "_pars")
        for nm, v in zip(("tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz"), (*t, *r, *s)):
            p = pars.get(nm)
            if p is None:
                p = o.par._get(nm)
            p._val = float(v)
            p._mode = N.ParMode.CONSTANT
            p._ver += 1
        for nm, v in (("px", 0.0), ("py", 0.0), ("pz", 0.0), ("scale", 1.0)):
            if nm in pars:
                pars[nm]._val = v
                pars[nm]._mode = N.ParMode.CONSTANT
                pars[nm]._ver += 1
        o._pver = getattr(o, "_pver", 0) + 1

    # ------------------------------------------------------------------ create/copy
    def _create(self, parent, optype, name):
        raise N.TDError("creating operators at runtime is not supported on device")

    def _copy(self, parent, o, name):
        raise N.TDError("copying operators at runtime is not supported on device")

    def _destroy(self, o):
        self.ops.pop(o.path, None)
        self._parexec_list = None

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
        # the transform hierarchy behind the bound matrices, parents first, for a
        # renderer that composes world matrices itself (caps "xf")
        nodes, index = [], {}

        def node(o, depth=0):
            if o.path in index:
                return index[o.path]
            p = self._object_parent(o) if depth < 255 else None
            pi = node(p, depth + 1) if p is not None else -1
            index[o.path] = len(nodes)
            nodes.append((o, pi))
            return index[o.path]

        mat_nodes = [node(o) if o is not None else -1 for o in mats]
        self._xf = {
            "nodes": nodes,
            "mat_nodes": mat_nodes,
            "sent": [None] * len(nodes),
            "tree": True,
        }
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
        if self._jit is not None:
            self._jit.tick()
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
        # a constant parameter that hasn't been written since last frame keeps its
        # value (no eval); expressions check their kept value (Par.eval)
        seen = self._bound_vers
        if len(seen) != len(b["pars"]):
            seen = self._bound_vers = [None] * len(b["pars"])
        prev = self._bound_floats
        expr = N.ParMode.EXPRESSION
        for i, p in enumerate(b["pars"]):
            if p is None:
                continue
            if p._mode != expr and prev is not None and seen[i] == p._ver:
                floats[i] = prev[i]
                continue
            v = p.eval()
            floats[i] = _to_float(v)
            seen[i] = p._ver if p._mode != expr else None
        self._bound_floats = floats
        k = len(b["pars"])
        for j, (o, flag) in enumerate(b["flags"]):
            floats[k + j] = 1.0 if (o is not None and self.flag(o, flag)) else 0.0
        xf = None
        # parameters can't change while the matrices are gathered: share the
        # local/world matrices of common ancestors across the bound objects
        self._xf_cache = {}
        try:
            if self.native_xf and self._bindings is not None:
                # the renderer composes world matrices: send the local matrices that
                # changed (a kept constant transform is the same array object)
                xs = self._xf
                sent, idx, locs = xs["sent"], [], []
                for i, (o, _) in enumerate(xs["nodes"]):
                    m = self.local_matrix(o)
                    if m is not sent[i]:
                        sent[i] = m
                        idx.append(i)
                        locs.append(m.T.reshape(-1))  # column-major
                xf = {
                    "idx": np.asarray(idx, np.int32),
                    "m": np.asarray(locs, np.float64).reshape(-1),
                }
                if xs["tree"]:
                    xs["tree"] = False
                    xf["parent"] = [pi for _, pi in xs["nodes"]]
                    xf["mat_nodes"] = xs["mat_nodes"]
                mats = np.zeros((0, 16), np.float64)
            else:
                mats = np.zeros((len(b["mats"]), 16), np.float64)
                for i, o in enumerate(b["mats"]):
                    if o is not None:
                        # column-major, ready for glUniformMatrix4fv(transpose=false)
                        mats[i] = self.world_matrix(o).T.reshape(-1)
        finally:
            self._xf_cache = None
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
            "xf": xf,
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


def _make_jit(log, cfg):
    """The native-kernel compiler (tdhost/jit.py) when this is a player (NUMBA_CACHE_DIR
    set by the image, or TOXC_JIT=1) with Numba installed and the project didn't
    opt out ({"python": {"jit": false}} in td-deploy.json)."""
    if cfg is False or (isinstance(cfg, dict) and cfg.get("enabled") is False):
        return None
    flag = os.environ.get("TOXC_JIT", "")
    if flag in ("0", "off", "false") or (not flag and not os.environ.get("NUMBA_CACHE_DIR")):
        return None
    from . import jit as _jit_mod

    if not _jit_mod.numba_available():
        return None
    cache = os.environ.get("NUMBA_CACHE_DIR")
    if not cache:
        import tempfile

        cache = os.environ["NUMBA_CACHE_DIR"] = os.path.join(tempfile.gettempdir(), "tdhost-numba")
    workdir = os.environ.get("TOXC_JIT_DIR") or os.path.join(cache, "tdhost")
    os.makedirs(workdir, exist_ok=True)
    exclude = cfg.get("exclude", ()) if isinstance(cfg, dict) else ()
    return _jit_mod.Jit(log, workdir, exclude=exclude)


def _same_bytes(a, b) -> bool:
    """Equal contents, compared as machine words (8x fewer elements than bytes)."""
    if a.nbytes != b.nbytes:
        return False
    if a.nbytes % 8 == 0:
        return bool(np.array_equal(a.reshape(-1).view(np.uint64), b.reshape(-1).view(np.uint64)))
    return bool(np.array_equal(a, b))


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
