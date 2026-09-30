"""Compile a project's numeric Python to native code on the player (Numba).

TouchDesigner projects do their per-frame math in plain Python + numpy on tiny
arrays (3-vectors, 4x4 matrices, a few hundred rope points), where the time goes
to the interpreter and numpy's per-call overhead rather than to arithmetic. On
the player the host compiles that math with Numba (Python + numpy -> LLVM ->
native code for this CPU), with no annotations in the project and no change to
how it runs inside TouchDesigner:

1. When a DAT module loads, every top-level function that only does numeric
   work is found by static analysis (no TouchDesigner API, no mutable module
   state, only math/numpy/constants/other such functions) and replaced by a
   `Kernel`: a callable that runs the Python original until a native version
   for the argument types it is called with exists.
2. For the first seconds the host records the argument types each kernel is
   called with, then hands them to a separate, low-priority process
   (`python -m tdhost.jit <request>`) that compiles them into Numba's on-disk
   cache. A separate process because Numba's compiler holds the GIL: compiling
   in the host would stall frames for seconds.
3. When it finishes, the host loads the compiled code from the cache and the
   kernels switch over between two frames. The recorded types are kept, so the
   next start (or a redeploy of unchanged code) loads the native kernels
   straight from the cache before the first frame.

Anything that doesn't compile (a construct Numba lacks, a list of mixed types,
an argument that is a TouchDesigner object) stays Python, silently; a call with
argument types that weren't compiled runs the Python version and is recorded
for the next compile. Numba freezes module constants into the compiled code,
so only globals that are never rebound or modified qualify; the compile
process checks the constants it sees against the host's before compiling.

Environment:
  TOXC_JIT=0                disable
  TOXC_JIT_CHECK=1          run the Python original alongside each native call
                            and log any result that differs (slow; debugging)
  NUMBA_CACHE_DIR           where compiled kernels live (the player image sets
                            /var/lib/tdplayer/numba-cache)
Project opt-outs (td-deploy.json): {"python": {"jit": false}} or
{"python": {"jit": {"exclude": ["module.function", ...]}}}.
"""

from __future__ import annotations

import ast
import base64
import builtins
import hashlib
import json
import os
import pickle
import subprocess
import sys
import tempfile
import time
import types

RECORD_FRAMES = 90  # frames of argument-type recording before a compile
_TD_CALLBACK = __import__("re").compile(r"^on[A-Z]")  # onCook, onFrameStart, ...
_TD_NAMES = {
    "op",
    "ops",
    "me",
    "parent",
    "mod",
    "run",
    "iop",
    "ipar",
    "absTime",
    "project",
    "tdu",
    "td",
    "ui",
    "app",
    "var",
    "debug",
    "root",
    "monitors",
    "Par",
    "OP",
    "COMP",
    "TOP",
    "CHOP",
    "SOP",
    "DAT",
    "MAT",
}
_OK_BUILTINS = {
    "abs",
    "min",
    "max",
    "len",
    "range",
    "enumerate",
    "zip",
    "float",
    "int",
    "bool",
    "round",
    "sum",
    "pow",
    "divmod",
    "complex",
    "tuple",
    "True",
    "False",
    "None",
    "ValueError",
    "ZeroDivisionError",
    "IndexError",
    "RuntimeError",
    "AssertionError",
}
_OK_MODULES = ("math", "cmath", "numpy")
_MUTATING_METHODS = {
    "fill",
    "sort",
    "resize",
    "put",
    "itemset",
    "setfield",
    "partition",
    "append",
    "extend",
    "insert",
    "pop",
    "remove",
    "clear",
    "update",
    "setdefault",
    "popitem",
    "add",
    "discard",
}
_BAD_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.Yield,
    ast.YieldFrom,
    ast.Await,
    ast.Try,
    ast.With,
    ast.AsyncWith,
    ast.Global,
    ast.Nonlocal,
    ast.Delete,
    ast.JoinedStr,
    ast.Starred,
    ast.Dict,
    ast.DictComp,
    ast.Set,
    ast.SetComp,
    ast.GeneratorExp,
    ast.Import,
    ast.ImportFrom,
)
if hasattr(ast, "TryStar"):
    _BAD_NODES = _BAD_NODES + (ast.TryStar,)
if hasattr(ast, "Match"):
    _BAD_NODES = _BAD_NODES + (ast.Match,)


# ----------------------------------------------------------------- analysis
def mutable_globals(tree: ast.Module) -> set:
    """Module-level names that may change after import: bound more than once at
    top level, declared `global` anywhere, or modified in place (an item or
    attribute store, an augmented assignment, a mutating method call) — at top
    level or in a function where the name isn't local."""
    bound: dict[str, int] = {}
    imported: set = set()
    out: set = set()

    def targets(t):
        if isinstance(t, ast.Name):
            yield t.id
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                yield from targets(e)

    for st in tree.body:
        names = []
        if isinstance(st, ast.Assign):
            for t in st.targets:
                names += list(targets(t))
        elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
            names += list(targets(st.target))
            if isinstance(st, ast.AugAssign):
                out.update(names)
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            names += list(targets(st.target))
            out.update(names)
        elif isinstance(st, (ast.Import, ast.ImportFrom)):
            got = [(a.asname or a.name).split(".")[0] for a in st.names]
            names += got
            imported.update(got)
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(st.name)
        elif not isinstance(st, ast.Expr):
            # conditional / looped definitions: whatever they bind can vary
            for n in ast.walk(st):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    out.add(n.id)
        for n in names:
            bound[n] = bound.get(n, 0) + 1
    out.update(n for n, c in bound.items() if c > 1)

    def base_name(node):
        while isinstance(node, (ast.Subscript, ast.Attribute)):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None

    def scan(body_nodes, local):
        for n in body_nodes:
            name = None
            if isinstance(n, ast.Global):
                out.update(n.names)
                continue
            if isinstance(n, (ast.Subscript, ast.Attribute)) and isinstance(
                n.ctx, (ast.Store, ast.Del)
            ):
                name = base_name(n.value)
            elif isinstance(n, ast.AugAssign) and isinstance(
                n.target, (ast.Subscript, ast.Attribute)
            ):
                name = base_name(n.target.value)
            elif (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in _MUTATING_METHODS
                and isinstance(n.func.value, ast.Name)
            ):
                name = n.func.value.id
                if name in imported:  # np.add(...) is a module function
                    name = None
            if name and name not in local and name in bound:
                out.add(name)

    # top level (outside functions)
    top_nodes = []
    for st in tree.body:
        if not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_nodes += list(ast.walk(st))
    scan(top_nodes, set())
    # each function (and method), with its own locals
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            local = {a.arg for a in fn.args.args + fn.args.posonlyargs + fn.args.kwonlyargs}
            if fn.args.vararg:
                local.add(fn.args.vararg.arg)
            if fn.args.kwarg:
                local.add(fn.args.kwarg.arg)
            declared_global = set()
            for n in ast.walk(fn):
                if isinstance(n, ast.Global):
                    declared_global.update(n.names)
                elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                    local.add(n.id)
            scan(list(ast.walk(fn)), local - declared_global)
    return out


def _free_names(fn: ast.FunctionDef) -> set:
    """Names a function reads that aren't its parameters or locals."""
    local = {a.arg for a in fn.args.args + fn.args.posonlyargs}
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            local.add(n.id)
        elif isinstance(n, ast.comprehension):
            for t in ast.walk(n.target):
                if isinstance(t, ast.Name):
                    local.add(t.id)
    return {
        n.id
        for n in ast.walk(fn)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in local
    }


def _is_const(v, depth=0) -> bool:
    import numpy as np

    if v is None or isinstance(v, (bool, int, float, complex, str, np.number, np.bool_)):
        return True
    if isinstance(v, np.ndarray):
        return v.dtype.kind in "biufc" and v.size <= 1 << 20
    if isinstance(v, tuple) and depth < 3:
        return all(_is_const(x, depth + 1) for x in v)
    return False


def _module_ok(v) -> bool:
    if isinstance(v, types.ModuleType):
        return v.__name__.split(".")[0] in _OK_MODULES
    mod = getattr(v, "__module__", None) or ""
    if type(v).__name__ in (
        "ufunc",
        "builtin_function_or_method",
        "function",
        "_ArrayFunctionDispatcher",
    ):
        return mod.split(".")[0] in _OK_MODULES or (mod == "" and type(v).__name__ == "ufunc")
    return False


def candidates(source: str, glb: dict, exclude=(), why: dict | None = None) -> dict:
    """Top-level functions of a module that Numba can plausibly compile, by name,
    with the module globals each one reads (constants to fingerprint)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    mutable = mutable_globals(tree)
    funcs = {
        st.name: st
        for st in tree.body
        if isinstance(st, ast.FunctionDef) and st.name not in exclude
    }
    why = why if why is not None else {}
    ok: dict[str, set] = {}
    deps: dict[str, set] = {}
    for name, fn in funcs.items():
        a = fn.args
        if _TD_CALLBACK.match(name):
            why[name] = "a TouchDesigner callback"
            continue
        if fn.decorator_list or a.vararg or a.kwarg or a.kwonlyargs:
            why[name] = "decorators / *args / **kwargs / keyword-only arguments"
            continue
        bad_node = next(
            (n for n in ast.walk(fn) if n is not fn and isinstance(n, _BAD_NODES)), None
        )
        if bad_node is not None:
            why[name] = f"uses {type(bad_node).__name__} (line {getattr(bad_node, 'lineno', '?')})"
            continue
        consts, calls, bad = set(), set(), None
        for g in sorted(_free_names(fn)):
            if g in _TD_NAMES:
                bad = f"TouchDesigner API ({g})"
            elif g in mutable:
                bad = f"module state that changes ({g})"
            elif g in funcs:
                calls.add(g)
            elif g in glb:
                v = glb[g]
                if _module_ok(v):
                    continue
                if _is_const(v):
                    consts.add(g)
                else:
                    bad = f"global {g} ({type(v).__name__})"
            elif g in _OK_BUILTINS and hasattr(builtins, g):
                continue
            else:
                bad = f"name {g}"
            if bad:
                break
        if bad:
            why[name] = bad
        else:
            ok[name] = consts
            deps[name] = calls
    changed = True
    while changed:  # a kernel may only call kernels
        changed = False
        for name in list(ok):
            if not deps[name] <= ok.keys():
                why[name] = f"calls {', '.join(sorted(deps[name] - ok.keys()))}"
                del ok[name]
                changed = True
    return {n: (ok[n], deps[n]) for n in ok}


def fingerprint(v) -> str:
    import numpy as np

    h = hashlib.sha1()
    if isinstance(v, np.ndarray):
        h.update(f"{v.dtype}{v.shape}".encode())
        h.update(np.ascontiguousarray(v).tobytes())
    elif isinstance(v, tuple):
        for x in v:
            h.update(fingerprint(x).encode())
    else:
        h.update(repr((type(v).__name__, v)).encode())
    return h.hexdigest()[:16]


# ----------------------------------------------------------------- numba glue
def numba_available() -> bool:
    if os.environ.get("TOXC_JIT", "1") in ("0", "off", "false"):
        return False
    try:
        import numba  # noqa: F401
    except Exception:  # noqa: BLE001 - not installed / broken: stay Python
        return False
    return True


def build_dispatchers(glb: dict, cands: dict, modname: str, cache: bool = True) -> dict:
    """numba.njit dispatchers for a module's kernels. They are compiled from copies
    of the functions living in a stand-in module registered as `modname` (the same
    name in the host and the compile process: Numba's cache refers to compiled
    code's module by name), whose globals are the original module's, with the
    other kernels mapped to their dispatchers (compiled code can only call
    compiled code). Constants are frozen at compile time."""
    import numba

    jm = types.ModuleType(modname)
    jg = jm.__dict__
    jg.update({k: v for k, v in glb.items() if k not in ("__name__", "__file__", "__spec__")})
    sys.modules[modname] = jm
    disp = {}
    for name in cands:
        py = glb[name]
        py = getattr(py, "py", py)  # a Kernel wrapper -> its original
        f = types.FunctionType(py.__code__, jg, py.__name__, py.__defaults__, py.__closure__)
        f.__qualname__ = py.__qualname__
        f.__module__ = modname
        disp[name] = numba.njit(cache=cache, nogil=True)(f)
    jg.update(disp)
    return disp


def _jit_modname(key: str) -> str:
    return "tdjit_" + "".join(c if c.isalnum() else "_" for c in key)


def _typeof(args):
    from numba import typeof

    return tuple(typeof(a) for a in args)


def _enc(obj) -> str:
    return base64.b64encode(pickle.dumps(obj)).decode()


def _dec(s):
    return pickle.loads(base64.b64decode(s))


class Kernel:
    """Stands in for a numeric function: native once compiled for the argument
    types, the Python original otherwise."""

    __slots__ = ("py", "name", "disp", "sigs", "off", "_jit", "__wrapped__")

    def __init__(self, jit, name, py):
        self._jit, self.name, self.py = jit, name, py
        self.__wrapped__ = py
        self.disp = None
        self.sigs = set()
        self.off = False

    def __call__(self, *args, **kw):
        d = self.disp
        if d is not None:
            try:
                out = d(*args, **kw)
            except TypeError as e:
                if not str(e).startswith("No matching definition"):
                    raise
            else:
                if self._jit.check:
                    self._jit.compare(self, args, kw, out)
                return out
        if self._jit.recording and not self.off and not kw:
            try:
                self.sigs.add(_typeof(args))
            except Exception:  # noqa: BLE001 - untypeable argument: never compiles
                self.off = True
        return self.py(*args, **kw)

    def __repr__(self):
        return f"<kernel {self.name} {'native' if self.disp is not None else 'python'}>"


# ----------------------------------------------------------------- per host
class Jit:
    def __init__(self, log, workdir: str, exclude=()):
        self.log = log
        self.enabled = numba_available()
        self.check = os.environ.get("TOXC_JIT_CHECK") == "1"
        self.exclude = set(exclude)
        self.workdir = workdir
        self.modules: dict[str, dict] = {}  # key -> {"file", "glb", "cands", "kernels", "fp"}
        self.frames = 0
        self.recording = False
        self.proc = None
        self.req_file = None
        self._mismatch = set()
        self._requested: set = set()  # (module key, kernel, sig) already sent to a compile

    # -- module loading ---------------------------------------------------
    def source_file(self, dat_path: str, text: str) -> str | None:
        """A real file holding a DAT module's text, named by content, so Numba's
        cache can key compiled code to it (and find it again after a restart or an
        unchanged redeploy)."""
        if not self.enabled:
            return None
        h = hashlib.sha1(text.encode()).hexdigest()[:12]
        stem = dat_path.strip("/").replace("/", "_") or "module"
        d = os.path.join(self.workdir, "src")
        os.makedirs(d, exist_ok=True)
        fn = os.path.join(d, f"{stem}_{h}.py")
        if not os.path.exists(fn):  # never rewritten: the cache checks its mtime
            tmp = fn + f".{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, fn)
        return fn

    def wrap_module(self, dat_path: str, text: str, glb: dict, filename: str) -> None:
        if not self.enabled:
            return
        modname = dat_path.strip("/").rsplit("/", 1)[-1]
        excl = {e.split(".", 1)[1] for e in self.exclude if e.split(".", 1)[0] == modname}
        why: dict = {}
        cands = candidates(text, glb, exclude=excl, why=why)
        if os.environ.get("TOXC_JIT_VERBOSE") == "1":
            for n, r in sorted(why.items()):
                self.log(f"jit: {modname}.{n} stays Python: {r}")
        if not cands:
            return
        key = os.path.basename(filename)[:-3]
        kernels = {}
        for name in cands:
            if not isinstance(glb.get(name), types.FunctionType):
                continue
            k = Kernel(self, f"{modname}.{name}", glb[name])
            glb[name] = k
            kernels[name] = k
        cands = {n: c for n, c in cands.items() if n in kernels}
        fp = {n: {g: fingerprint(glb[g]) for g in consts} for n, (consts, _) in cands.items()}
        m = {
            "file": filename,
            "glb": glb,
            "cands": cands,
            "kernels": kernels,
            "fp": fp,
            "disp": None,
        }
        self.modules[key] = m
        known = self._load_sigs(key)
        if known:
            self._install(key, known)
        self.recording = True
        self.frames = 0
        self.log(
            f"jit: {modname}: {len(kernels)} numeric function(s) — {', '.join(sorted(kernels))}"
        )

    # -- per frame --------------------------------------------------------
    def tick(self) -> None:
        if not self.modules:
            return
        if self.proc is not None:
            if self.proc.poll() is not None:
                self._finish()
            return
        if self.recording:
            self.frames += 1
            if self.frames >= RECORD_FRAMES:
                self.recording = False
                self._request()

    # -- compile request / result ------------------------------------------
    def _pending(self):
        req = {}
        for key, m in self.modules.items():
            for name, k in m["kernels"].items():
                if k.off:
                    continue
                have = set(k.disp.signatures) if k.disp is not None else set()
                sigs = [
                    s for s in k.sigs if s not in have and (key, name, s) not in self._requested
                ]
                if sigs:
                    req.setdefault(key, {})[name] = sigs
        return req

    def _request(self) -> None:
        pending = self._pending()
        if not pending:
            return
        for key, ks in pending.items():
            for name, sigs in ks.items():
                self._requested.update((key, name, s) for s in sigs)
        body = {
            "modules": {
                key: {
                    "file": self.modules[key]["file"],
                    "kernels": {n: [_enc(s) for s in sigs] for n, sigs in ks.items()},
                    "cands": sorted(self.modules[key]["cands"]),
                    "fp": self.modules[key]["fp"],
                }
                for key, ks in pending.items()
            }
        }
        fd, self.req_file = tempfile.mkstemp(prefix="jit-", suffix=".json", dir=self.workdir)
        with os.fdopen(fd, "w") as fh:
            json.dump(body, fh)
        n = sum(len(s) for ks in pending.values() for s in ks.values())
        self.log(f"jit: compiling {n} kernel signature(s) in the background")
        env = dict(os.environ)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "tdhost.jit", self.req_file],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=None,
            preexec_fn=(lambda: os.nice(10)) if hasattr(os, "nice") else None,
        )
        self._t0 = time.monotonic()

    def _finish(self) -> None:
        rc = self.proc.returncode
        self.proc = None
        res_file = self.req_file[:-5] + ".out.json"
        try:
            with open(res_file) as fh:
                res = json.load(fh)
        except (OSError, ValueError):
            self.log(f"jit: background compile failed (exit {rc}); staying in Python")
            return
        finally:
            for f in (self.req_file, res_file):
                try:
                    os.unlink(f)
                except OSError:
                    pass
        done, failed = [], []
        for key, ks in res.get("modules", {}).items():
            ok = {n: [_dec(s) for s in r["ok"]] for n, r in ks.items() if r["ok"]}
            for n, r in ks.items():
                if r.get("error") and not r["ok"]:
                    failed.append(f"{n} ({r['error']})")
                    m = self.modules.get(key)
                    if m and n in m["kernels"]:
                        m["kernels"][n].off = True
            if ok:
                self._save_sigs(key, ok)
                done += self._install(key, ok)
        dt = time.monotonic() - self._t0
        if done:
            self.log(f"jit: native in {dt:.1f}s: {', '.join(sorted(done))}")
        if failed:
            self.log(f"jit: stays Python: {'; '.join(sorted(failed))}")
        # new argument types seen meanwhile get their own compile later
        self.recording = True
        self.frames = 0

    def _install(self, key, sigs_by_kernel) -> list:
        """Load compiled kernels from Numba's cache (compiling here only if the cache
        lost them) and switch the Kernel objects over."""
        m = self.modules[key]
        if m["disp"] is None:
            m["disp"] = build_dispatchers(m["glb"], m["cands"], _jit_modname(key))
        names = []
        for name, sigs in sigs_by_kernel.items():
            d = m["disp"].get(name)
            k = m["kernels"].get(name)
            if d is None or k is None:
                continue
            ok = False
            for sig in sigs:
                try:
                    d.compile(sig)
                    ok = True
                except Exception as e:  # noqa: BLE001
                    self.log(f"jit: {k.name}{sig}: {type(e).__name__}: {str(e)[:200]}")
            if ok:
                d.disable_compile()
                k.disp = d
                names.append(k.name)
        return names

    # -- persisted signatures ---------------------------------------------
    def _sig_file(self, key):
        return os.path.join(self.workdir, "sigs", key + ".json")

    def _load_sigs(self, key):
        try:
            with open(self._sig_file(key)) as fh:
                data = json.load(fh)
            return {n: [_dec(s) for s in sigs] for n, sigs in data.items()}
        except (OSError, ValueError, pickle.UnpicklingError, EOFError):
            return {}
        except Exception:  # noqa: BLE001 - a numba upgrade can break old pickles
            return {}

    def _save_sigs(self, key, new):
        old = self._load_sigs(key)
        for n, sigs in new.items():
            have = old.setdefault(n, [])
            have += [s for s in sigs if s not in have]
        os.makedirs(os.path.dirname(self._sig_file(key)), exist_ok=True)
        tmp = self._sig_file(key) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({n: [_enc(s) for s in sigs] for n, sigs in old.items()}, fh)
        os.replace(tmp, self._sig_file(key))

    # -- TOXC_JIT_CHECK -------------------------------------------------------
    def compare(self, k, args, kw, out):
        import copy

        import numpy as np

        try:
            ref = k.py(*copy.deepcopy(args), **kw)
        except Exception as e:  # noqa: BLE001
            ref = e
        try:
            same = _close(out, ref, np)
        except Exception:  # noqa: BLE001
            same = False
        if not same and k.name not in self._mismatch:
            self._mismatch.add(k.name)
            self.log(f"jit check: {k.name} differs: native {out!r:.200} vs python {ref!r:.200}")

    def exit(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()


def _close(a, b, np) -> bool:
    if isinstance(a, tuple) or isinstance(b, tuple) or isinstance(a, list):
        return len(a) == len(b) and all(_close(x, y, np) for x, y in zip(a, b))
    if isinstance(b, Exception):
        return False
    return bool(
        np.allclose(
            np.asarray(a, dtype=float),
            np.asarray(b, dtype=float),
            rtol=1e-9,
            atol=1e-9,
            equal_nan=True,
        )
    )


# ----------------------------------------------------------------- compile process
class _Stub:
    """Stands in for TouchDesigner objects while a module's top level runs in the
    compile process (kernels never touch them: they were excluded)."""

    def __getattr__(self, name):
        return _Stub()

    def __call__(self, *a, **k):
        return _Stub()

    def __getitem__(self, k):
        return _Stub()

    def __iter__(self):
        return iter(())

    def __float__(self):
        return 0.0

    def __int__(self):
        return 0

    def __bool__(self):
        return False


def _compile_main(req_file: str) -> int:
    import warnings

    warnings.filterwarnings("ignore")
    with open(req_file) as fh:
        req = json.load(fh)
    out = {"modules": {}}
    for key, spec in req["modules"].items():
        res = out["modules"].setdefault(key, {})
        with open(spec["file"], encoding="utf-8") as fh:
            text = fh.read()
        glb = {"__name__": key, "__builtins__": builtins}
        glb.update({n: _Stub() for n in _TD_NAMES})
        for m in ("td", "tdu"):  # `import tdu` at a module's top level
            sys.modules.setdefault(m, _Stub())
        import math

        glb["math"] = math
        try:
            exec(compile(text, spec["file"], "exec"), glb)
        except Exception as e:  # noqa: BLE001
            for n in spec["kernels"]:
                res[n] = {"ok": [], "error": f"module top level: {type(e).__name__}: {e}"}
            continue
        cands = {n: c for n, c in candidates(text, glb).items() if n in spec["cands"]}
        disp = build_dispatchers(glb, cands, _jit_modname(key))
        for name, sigs in spec["kernels"].items():
            r = res.setdefault(name, {"ok": [], "error": None})
            if name not in disp:
                r["error"] = "not a kernel in the compile process"
                continue
            consts, _ = cands[name]
            fp = spec["fp"].get(name, {})
            diff = [g for g in consts if fingerprint(glb[g]) != fp.get(g)]
            if diff:
                r["error"] = f"module constants differ from the player's: {', '.join(diff)}"
                continue
            for s in sigs:
                sig = _dec(s)
                try:
                    disp[name].compile(sig)
                    r["ok"].append(s)
                except Exception as e:  # noqa: BLE001
                    msg = str(e).strip().splitlines()
                    r["error"] = f"{type(e).__name__}: {msg[0][:160] if msg else ''}"
    with open(req_file[:-5] + ".out.json", "w") as fh:
        json.dump(out, fh)
    return 0


if __name__ == "__main__":
    sys.exit(_compile_main(sys.argv[1]))
