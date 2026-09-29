"""TouchDesigner Python API emulation over the imported network.

A deployed project keeps its Python — Execute DAT callbacks, text-DAT modules,
Script TOP/CHOP/SOP callbacks and Python parameter expressions — and runs it
unchanged against this emulation of the parts of TouchDesigner's API that
scripts actually use: `op()`, `.par`, storage, object transforms, `absTime`,
`run()`, `tdu`, `project`, `monitors`...

The renderer (runtime_rs) drives it through `Host` (see host.py): once per frame
it runs the frame-start callbacks, cooks the Script operators the render graph
needs, evaluates the parameters the renderer is bound to, and after the GPU has
cooked, runs the frame-end callbacks.

Fidelity rules of thumb:
  * values: numeric -> float (int for Int/Toggle-ish), toggles -> bool, menus and
    strings -> str; parameters behave like their value in arithmetic and
    comparisons, as in TD;
  * assigning `op.par.X = v` sets a constant (leaving expression mode), as in TD;
  * object transforms follow TD's Xform page (xord/rord/pivot/uniform scale) and
    nest through object parents (input wire) and containing object COMPs.
"""

from __future__ import annotations

import fnmatch

import numpy as np

from . import tdu as _tdu

# toeexpand writes TouchDesigner's internal short type names; scripts see the
# Python names (op.type / op.OPType). Anything not listed is identical.
TYPE_ALIASES = {
    ("COMP", "geo"): "geometry",
    ("COMP", "cam"): "camera",
    ("COMP", "ambient"): "ambientlight",
    ("COMP", "envlight"): "environmentlight",
    ("DAT", "parexec"): "parameterexecute",
    ("DAT", "chopexec"): "chopexecute",
    ("DAT", "datexec"): "datexecute",
    ("DAT", "opexec"): "opexecute",
    ("DAT", "panelexec"): "panelexecute",
    ("TOP", "comp"): "composite",
    ("TOP", "res"): "resolution",
}

OBJECT_TYPES = {
    "geometry",
    "null",
    "camera",
    "light",
    "ambientlight",
    "environmentlight",
    "fbx",
    "bone",
    "handle",
    "blend",
    "sharedmem",
}

# TD's own defaults for builtin parameters scripts commonly read. TD writes only
# non-default values to disk, so anything absent falls back here (then to 0).
BUILTIN_DEFAULTS = {
    "*": {
        "tx": 0.0,
        "ty": 0.0,
        "tz": 0.0,
        "rx": 0.0,
        "ry": 0.0,
        "rz": 0.0,
        "sx": 1.0,
        "sy": 1.0,
        "sz": 1.0,
        "px": 0.0,
        "py": 0.0,
        "pz": 0.0,
        "scale": 1.0,
        "xord": "srt",
        "rord": "xyz",
        "ptx": 0.0,
        "pty": 0.0,
        "ptz": 0.0,
        "prx": 0.0,
        "pry": 0.0,
        "prz": 0.0,
        "psx": 1.0,
        "psy": 1.0,
        "psz": 1.0,
        "pscale": 1.0,
        "render": True,
        "display": True,
        "active": True,
        "bypass": False,
        "resolutionw": 256,
        "resolutionh": 256,
    },
    "camera": {"fov": 45.0, "near": 0.1, "far": 1000.0, "projection": "perspective"},
    "light": {
        "dimmer": 1.0,
        "cr": 1.0,
        "cg": 1.0,
        "cb": 1.0,
        "lighttype": "point",
        "attenuated": False,
        "attenuationstart": 0.0,
        "attenuationend": 10.0,
        "attenuationexp": 2.0,
        "coneangle": 20.0,
        "conedelta": 10.0,
        "conerolloff": 1.0,
    },
    "ambientlight": {"dimmer": 1.0, "cr": 0.05, "cg": 0.05, "cb": 0.05},
    "phong": {
        "diffr": 1.0,
        "diffg": 1.0,
        "diffb": 1.0,
        "ambr": 1.0,
        "ambg": 1.0,
        "ambb": 1.0,
        "specr": 1.0,
        "specg": 1.0,
        "specb": 1.0,
        "emitr": 0.0,
        "emitg": 0.0,
        "emitb": 0.0,
        "constantr": 0.0,
        "constantg": 0.0,
        "constantb": 0.0,
        "shininess": 51.2,
        "alphafront": 1.0,
        "alphaside": 1.0,
        "rolloff": 1.0,
        "bumpscale": 1.0,
        "alphathreshold": 0.0,
        "ambdiff": False,
        "alphatest": False,
        "postmultalpha": False,
    },
    "blur": {"size": 1.0, "offset": 0.0},
    "level": {"opacity": 1.0, "brightness1": 1.0, "gamma1": 1.0, "contrast": 1.0},
    "flip": {"flipx": False, "flipy": False, "flop": "noflop"},
    "fit": {"fit": "fitbest"},
}


class TDError(Exception):
    pass


# ================================================================== parameters


def _coerce(tok, style=None):
    """String token from disk -> Python value (TD semantics)."""
    if style in (
        "Str",
        "Menu",
        "StrMenu",
        "File",
        "Folder",
        "OP",
        "COMP",
        "TOP",
        "CHOP",
        "DAT",
        "SOP",
    ):
        return tok
    if isinstance(tok, (int, float, bool)):
        return tok
    if tok is None:
        return 0.0
    s = str(tok)
    if style == "Toggle":
        return s in ("on", "1", "True", "true")
    if s in ("on", "off"):
        return s == "on"
    try:
        if style in ("Int", "Pulse"):
            return int(float(s))
        f = float(s)
        return f
    except ValueError:
        return s


class ParMode:
    CONSTANT = 0
    EXPRESSION = 1
    EXPORT = 2
    BIND = 3


class Par:
    """One parameter. Behaves like its evaluated value in expressions."""

    __slots__ = (
        "owner",
        "name",
        "_val",
        "_expr",
        "_mode",
        "style",
        "default",
        "menuNames",
        "menuLabels",
        "label",
        "page",
        "min",
        "max",
        "_code",
        "_evaluating",
        "isCustom",
        "tuplet",
        "enableExpr",
    )

    def __init__(self, owner, name, val, expr=None, mode=0, style=None, **kw):
        self.owner = owner
        self.name = name
        self.style = style
        self._val = val
        self._expr = expr
        self._mode = ParMode.EXPRESSION if (mode & 1 and expr) else ParMode.CONSTANT
        self.default = kw.get("default", val)
        self.menuNames = kw.get("menuNames", [])
        self.menuLabels = kw.get("menuLabels", [])
        self.label = kw.get("label", name)
        self.page = kw.get("page", None)
        self.min = kw.get("min", 0.0)
        self.max = kw.get("max", 1.0)
        self.isCustom = kw.get("isCustom", False)
        self.enableExpr = kw.get("enableExpr", None)
        self.tuplet = None
        self._code = None
        self._evaluating = False

    # -- value access ---------------------------------------------------------
    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, m):
        self._mode = ParMode.EXPRESSION if m in (ParMode.EXPRESSION, 1) else ParMode.CONSTANT

    @property
    def expr(self):
        return self._expr or ""

    @expr.setter
    def expr(self, text):
        self._expr = text
        self._code = None
        self._mode = ParMode.EXPRESSION if text else ParMode.CONSTANT
        self.owner._host._par_changed(self)

    @property
    def val(self):
        return self._val

    @val.setter
    def val(self, v):
        prev = self.eval()
        self._val = self._normalize(v)
        self._mode = ParMode.CONSTANT
        self.owner._host._par_changed(self, prev)

    def _normalize(self, v):
        if isinstance(v, Par):
            v = v.eval()
        if self.style == "Toggle":
            return bool(v)
        if self.style in ("Int", "Pulse") and not isinstance(v, str):
            return int(v)
        if self.style == "Float" and not isinstance(v, str):
            return float(v)
        if self.style in ("Menu", "Str") and not isinstance(v, str):
            if self.style == "Menu" and isinstance(v, (int, float)) and self.menuNames:
                return self.menuNames[int(v)]
            return str(v)
        return v

    def eval(self):
        if self._mode == ParMode.EXPRESSION and self._expr:
            if self._evaluating:
                return self._val
            host = self.owner._host
            self._evaluating = True
            try:
                if self._code is None:
                    self._code = host._compile(self._expr, f"{self.owner.path}:{self.name}")
                v = eval(self._code, host._expr_globals, host._expr_locals(self.owner))
                if isinstance(v, Par):
                    v = v.eval()
                elif isinstance(v, OP) and self.style not in (
                    "OP",
                    "COMP",
                    "TOP",
                    "CHOP",
                    "SOP",
                    "DAT",
                ):
                    v = v.path
                v = self._normalize(v)
            except Exception as e:  # noqa: BLE001 — TD shows the error and keeps going
                host._expr_error(self, e)
                v = self._val
            finally:
                self._evaluating = False
            host._note_dep(self, v)
            return v
        self.owner._host._note_dep(self, self._val)
        return self._val

    def pulse(self, *a, **k):
        self.owner._host._pulse(self)

    @property
    def valid(self):
        return bool(getattr(self.owner, "valid", True))

    @property
    def isPulse(self):
        return self.style in ("Pulse", "Momentary")

    @property
    def isMenu(self):
        return self.style in ("Menu", "StrMenu")

    @property
    def isToggle(self):
        return self.style == "Toggle"

    @property
    def isString(self):
        return self.style in ("Str", "StrMenu")

    @property
    def menuIndex(self):
        v = self.eval()
        try:
            return self.menuNames.index(v)
        except ValueError:
            return 0

    @menuIndex.setter
    def menuIndex(self, i):
        self.val = self.menuNames[int(i)]

    def reset(self):
        self.val = self.default

    @property
    def enable(self):
        return True

    @property
    def readOnly(self):
        return False

    # -- value protocol --------------------------------------------------------
    def __float__(self):
        v = self.eval()
        return float(v) if not isinstance(v, str) else float(v or 0)

    def __int__(self):
        return int(float(self))

    __index__ = __int__

    def __bool__(self):
        return bool(self.eval())

    def __str__(self):
        v = self.eval()
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, float) and v.is_integer() and self.style in ("Int", None):
            return str(int(v))
        return str(v)

    def __format__(self, spec):
        return format(self.eval(), spec)

    def __repr__(self):
        return f"type:Par name:{self.name} owner:{self.owner.path} value:{self.eval()!r}"

    def __hash__(self):
        return id(self)

    def __eq__(self, o):
        return self.eval() == (o.eval() if isinstance(o, Par) else o)

    def __ne__(self, o):
        return not self.__eq__(o)

    def _bin(op_):
        def f(self, o):
            return op_(self.eval(), o.eval() if isinstance(o, Par) else o)

        def r(self, o):
            return op_(o.eval() if isinstance(o, Par) else o, self.eval())

        return f, r

    import operator as _o

    __add__, __radd__ = _bin(_o.add)
    __sub__, __rsub__ = _bin(_o.sub)
    __mul__, __rmul__ = _bin(_o.mul)
    __truediv__, __rtruediv__ = _bin(_o.truediv)
    __floordiv__, __rfloordiv__ = _bin(_o.floordiv)
    __mod__, __rmod__ = _bin(_o.mod)
    __pow__, __rpow__ = _bin(_o.pow)
    __lt__, _ = _bin(_o.lt)
    __le__, _ = _bin(_o.le)
    __gt__, _ = _bin(_o.gt)
    __ge__, _ = _bin(_o.ge)
    del _bin, _o, _

    def __neg__(self):
        return -self.eval()

    def __pos__(self):
        return +self.eval()

    def __abs__(self):
        return abs(self.eval())

    def __round__(self, n=None):
        return round(self.eval(), n)

    def __contains__(self, x):
        return x in self.eval()


class ParCollection:
    """`op.par` — attribute access by parameter name."""

    def __init__(self, owner):
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_pars", {})

    def _get(self, name, create=True):
        pars = object.__getattribute__(self, "_pars")
        p = pars.get(name)
        if p is None and create:
            owner = object.__getattribute__(self, "_owner")
            dv = owner._builtin_default(name)
            if dv is None:
                return None
            p = Par(owner, name, dv)
            pars[name] = p
        return p

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        p = self._get(name)
        if p is None:
            raise AttributeError(f"'td.ParCollection' object has no attribute '{name}'")
        return p

    def __setattr__(self, name, value):
        p = self._get(name)
        if p is None:
            raise AttributeError(name)
        p.val = value

    def __getitem__(self, name):
        return self.__getattr__(name)

    def __setitem__(self, name, value):
        self.__setattr__(name, value)

    def __iter__(self):
        return iter(object.__getattribute__(self, "_pars").values())

    def __contains__(self, name):
        return name in object.__getattribute__(self, "_pars")

    def __dir__(self):
        return list(object.__getattribute__(self, "_pars"))


class ParGroup:
    """`op.parGroup.Keydir` — a tuple of pars (read-mostly)."""

    def __init__(self, pars):
        self.pars = pars

    def eval(self):
        return tuple(p.eval() for p in self.pars)

    def __iter__(self):
        return iter(self.pars)

    def __getitem__(self, i):
        return self.pars[i]


# ================================================================== operators


class Cell:
    def __init__(self, dat, r, c):
        self._dat, self.row, self.col = dat, r, c

    @property
    def val(self):
        return self._dat._table[self.row][self.col]

    @val.setter
    def val(self, v):
        self._dat._table[self.row][self.col] = str(v)

    def __str__(self):
        return self.val

    def __float__(self):
        return float(self.val)

    def __int__(self):
        return int(float(self.val))

    def __eq__(self, o):
        return self.val == (o.val if isinstance(o, Cell) else o)

    def __hash__(self):
        return hash((id(self._dat), self.row, self.col))


class OP:
    family = "OP"
    isCOMP = isTOP = isCHOP = isSOP = isDAT = isMAT = isPOP = isObject = False

    def __init__(self, host, path, rec):
        self._host = host
        self.path = path
        self.name = path.rsplit("/", 1)[-1] if path != "/" else ""
        raw_type = rec.get("type", "")
        self.type = TYPE_ALIASES.get((rec.get("family", self.family), raw_type), raw_type)
        self._rec = rec
        self.par = ParCollection(self)
        self.storage = {}
        self._flags = rec.get("flags", {})
        self._input_paths = list(rec.get("inputs", []))
        self.id = host._next_id()
        self.valid = True
        self._build_pars()

    # -- params ------------------------------------------------------------------
    def _build_pars(self):
        rec = self._rec
        pars = object.__getattribute__(self.par, "_pars")
        custom_names = {}
        for d in rec.get("custom", []):
            for k, nm in enumerate(d["names"]):
                dflt = d["default"]
                if isinstance(dflt, list):
                    dflt = dflt[k] if k < len(dflt) else dflt[-1]
                style = d["style"]
                comp_style = "Float" if style in ("RGB", "RGBA", "XYZ", "XY", "UV", "WH") else style
                p = Par(
                    self,
                    nm,
                    _coerce(dflt, comp_style),
                    style=comp_style,
                    default=_coerce(dflt, comp_style),
                    menuNames=d.get("menuNames", []),
                    menuLabels=d.get("menuLabels", []),
                    label=d.get("label", nm),
                    page=d.get("page"),
                    min=d.get("min", 0.0),
                    max=d.get("max", 1.0),
                    isCustom=True,
                    enableExpr=d.get("enableExpr"),
                )
                pars[nm] = p
                custom_names[nm] = d
        for nm, spec in rec.get("params", {}).items():
            if nm in pars:
                p = pars[nm]
                p._val = _coerce(spec["val"], p.style)
                p._expr = spec.get("expr")
                p.mode = 1 if (spec.get("mode", 0) & 1 and spec.get("expr")) else 0
            else:
                p = Par(self, nm, _coerce(spec["val"]), spec.get("expr"), spec.get("mode", 0))
                pars[nm] = p
        self.customPars = [pars[n] for n in custom_names]
        self.customPages = [
            _Page(n, [p for p in self.customPars if p.page == n]) for n in rec.get("pages", [])
        ]

    def _builtin_default(self, name):
        d = BUILTIN_DEFAULTS.get(self.type, {})
        if name in d:
            return d[name]
        return BUILTIN_DEFAULTS["*"].get(name)

    @property
    def parGroup(self):
        groups = {}
        for d in self._rec.get("custom", []):
            groups[d["name"]] = ParGroup([getattr(self.par, n) for n in d["names"]])

        class _G:
            def __getattr__(_s, n):
                if n in groups:
                    return groups[n]
                return ParGroup([getattr(self.par, n)])

        return _G()

    # -- identity ------------------------------------------------------------------
    @property
    def OPType(self):
        return self.type + self.family

    @property
    def opType(self):
        return self.OPType

    @property
    def label(self):
        return self.name

    @property
    def digits(self):
        import re

        m = re.search(r"(\d+)$", self.name)
        return int(m.group(1)) if m else None

    @property
    def base(self):
        import re

        return re.sub(r"\d+$", "", self.name)

    def __repr__(self):
        return f"type:{self.OPType} path:{self.path}"

    def __eq__(self, o):
        return isinstance(o, OP) and o.path == self.path

    def __hash__(self):
        return hash(self.path)

    # -- network -------------------------------------------------------------------
    def parent(self, n=1):
        p = self
        for _ in range(int(n)):
            if p.path == "/":
                return None
            pp = p.path.rsplit("/", 1)[0] or "/"
            p = self._host.ops.get(pp)
            if p is None:
                return None
        return p

    @property
    def _context(self):
        """The network relative paths resolve against: a COMP's own contents for
        COMP.op(), the containing network for everything else."""
        return self.path.rsplit("/", 1)[0] or "/"

    def op(self, path):
        return self._host.resolve(path, self._context)

    def ops(self, *patterns):
        out = []
        for pat in patterns:
            out.extend(self._host.resolve_many(pat, self._context))
        return out

    @property
    def inputs(self):
        return [self._host.ops.get(p) for p in self._input_paths if p in self._host.ops]

    @property
    def outputs(self):
        return [o for o in self._host.ops.values() if self.path in o._input_paths]

    @property
    def inputConnectors(self):
        return []

    @property
    def outputConnectors(self):
        return []

    # -- storage -------------------------------------------------------------------
    def store(self, key, value):
        self.storage[key] = value
        return value

    def fetch(self, key, *args, search=True, storeDefault=False):
        o = self
        while o is not None:
            if key in o.storage:
                return o.storage[key]
            if not search:
                break
            o = o.parent()
        if args:
            if storeDefault:
                self.storage[key] = args[0]
            return args[0]
        raise KeyError(key)

    def unstore(self, keys="*", *a):
        for k in list(self.storage):
            if fnmatch.fnmatchcase(str(k), keys):
                del self.storage[k]

    def storeStartupValue(self, key, value):
        self.storage.setdefault(key, value)

    def unstoreStartupValue(self, *a):
        pass

    # -- misc -----------------------------------------------------------------------
    def cook(self, force=False, recurse=False, includeUtility=False):
        self._host._cook(self, force=force)

    def errors(self, recurse=False):
        return ""

    def warnings(self, recurse=False):
        return ""

    def scriptErrors(self, recurse=False):
        return ""

    def addError(self, msg):
        self._host.log(f"{self.path}: error: {msg}")

    def addWarning(self, msg):
        self._host.log(f"{self.path}: warning: {msg}")

    def addScriptError(self, msg):
        self.addError(msg)

    def clearScriptErrors(self, *a, **k):
        pass

    @property
    def cookTime(self):
        return 0.0

    cpuCookTime = gpuCookTime = cookTime

    @property
    def cookFrame(self):
        return self._host.absTime.frame

    cookAbsFrame = cookFrame

    @property
    def time(self):
        return self._host.absTime

    def destroy(self):
        self._host._destroy(self)

    def dependenciesTo(self, *a):
        return []

    @property
    def viewer(self):
        return False

    @property
    def display(self):
        return bool(self._flags.get("display", False))

    @property
    def render(self):
        return bool(self._flags.get("render", False))

    @property
    def bypass(self):
        return bool(self._flags.get("bypass", False))

    @property
    def lock(self):
        return bool(self._flags.get("lock", False))


class _Page:
    def __init__(self, name, pars):
        self.name = name
        self.pars = pars

    def __repr__(self):
        return f"type:Page name:{self.name}"


class COMP(OP):
    family = "COMP"
    isCOMP = True

    @property
    def _context(self):
        return self.path

    @property
    def children(self):
        pre = self.path.rstrip("/") + "/"
        return [
            o for p, o in self._host.ops.items() if p.startswith(pre) and "/" not in p[len(pre) :]
        ]

    def findChildren(
        self,
        type=None,
        name=None,
        path=None,
        depth=None,
        maxDepth=None,
        tags=None,
        allTags=None,
        parValue=None,
        parExpr=None,
        parName=None,
        key=None,
        includeUtility=False,
    ):
        pre = self.path.rstrip("/") + "/"
        out = []
        for p, o in self._host.ops.items():
            if not p.startswith(pre):
                continue
            d = p[len(pre) :].count("/") + 1
            if depth is not None and d != depth:
                continue
            if maxDepth is not None and d > maxDepth:
                continue
            if type is not None and not _is_type(o, type):
                continue
            if name is not None and not fnmatch.fnmatchcase(o.name, name):
                continue
            if path is not None and not fnmatch.fnmatchcase(o.path, path):
                continue
            if key is not None and not key(o):
                continue
            out.append(o)
        return out

    def create(self, optype, name=None, initialize=True):
        return self._host._create(self, optype, name)

    def copy(self, o, name=None, includeDocked=True):
        return self._host._copy(self, o, name)

    # -- object transforms ------------------------------------------------------------
    @property
    def isObject(self):
        return self.type in OBJECT_TYPES

    def transform(self):
        return _tdu.Matrix(self._host.local_matrix(self))

    @property
    def localTransform(self):
        return self.transform()

    @property
    def worldTransform(self):
        return _tdu.Matrix(self._host.world_matrix(self))

    def setTransform(self, matrix):
        m = matrix.m if isinstance(matrix, _tdu.Matrix) else np.asarray(matrix, dtype=float)
        self._host.set_local_matrix(self, m)

    def preTransform(self):
        pre = self._host._pre_matrix(self)
        return _tdu.Matrix(pre if pre is not None else np.eye(4))

    def relativeTransform(self, target):
        return _tdu.Matrix(
            np.linalg.inv(self._host.world_matrix(target)) @ self._host.world_matrix(self)
        )

    # -- window COMP ----------------------------------------------------------------------
    @property
    def isOpen(self):
        return self._host._window_open.get(self.path, False) if self.type == "window" else False

    # -- storage on panels etc. -----------------------------------------------------------
    @property
    def extensions(self):
        return []


class TOP(OP):
    family = "TOP"
    isTOP = True

    @property
    def width(self):
        return self._host.top_size(self)[0]

    @property
    def height(self):
        return self._host.top_size(self)[1]

    @property
    def aspect(self):
        w, h = self._host.top_size(self)
        return w / h if h else 1.0

    def numpyArray(self, delayed=False, writable=False, neverNone=False):
        arr = self._host._readback(self, delayed)
        if arr is None and neverNone:
            w, h = self._host.top_size(self)
            arr = np.zeros((h, w, 4), np.float32)
        return arr

    def save(self, path, *a, **k):
        self._host.log(f"{self.path}.save({path!r}) ignored on device")
        return path

    def sample(self, x=None, y=None, u=None, v=None):
        arr = self._host._readback(self, True)
        if arr is None:
            return (0.0, 0.0, 0.0, 0.0)
        h, w = arr.shape[:2]
        if u is not None:
            x, y = int(u * (w - 1)), int(v * (h - 1))
        return tuple(float(c) for c in arr[int(y), int(x)])

    # Script TOP
    def copyNumpyArray(self, arr, *a, **k):
        self._host._script_top_data(self, arr)

    def loadByteArray(self, *a, **k):
        pass


class Channel:
    def __init__(self, owner, name, vals=None):
        self.owner = owner
        self.name = name
        self._vals = np.zeros(max(1, owner._num_samples), np.float32) if vals is None else vals

    @property
    def vals(self):
        return [float(v) for v in self._vals]

    @vals.setter
    def vals(self, v):
        self._vals = np.asarray(v, dtype=np.float32).reshape(-1)

    def copyNumpyArray(self, arr):
        self._vals = np.asarray(arr, dtype=np.float32).reshape(-1).copy()

    def numpyArray(self):
        return self._vals

    def eval(self, index=0):
        return float(self._vals[int(index)]) if len(self._vals) else 0.0

    def __getitem__(self, i):
        return float(self._vals[i])

    def __setitem__(self, i, v):
        self._vals[i] = v

    def __len__(self):
        return len(self._vals)

    def __float__(self):
        return self.eval()

    def _bin(op_):
        def f(self, o):
            return op_(self.eval(), float(o))

        def r(self, o):
            return op_(float(o), self.eval())

        return f, r

    import operator as _o

    __add__, __radd__ = _bin(_o.add)
    __sub__, __rsub__ = _bin(_o.sub)
    __mul__, __rmul__ = _bin(_o.mul)
    __truediv__, __rtruediv__ = _bin(_o.truediv)
    del _bin, _o


class CHOP(OP):
    family = "CHOP"
    isCHOP = True

    def __init__(self, host, path, rec):
        super().__init__(host, path, rec)
        self._chans: dict[str, Channel] = {}
        self._num_samples = 1
        self.rate = 60.0

    # Script CHOP API
    def clear(self):
        self._chans = {}

    @property
    def numSamples(self):
        return self._num_samples

    @numSamples.setter
    def numSamples(self, n):
        self._num_samples = int(n)

    @property
    def numChans(self):
        return len(self._chans)

    def appendChan(self, name):
        ch = Channel(self, name)
        self._chans[name] = ch
        return ch

    def chans(self, *patterns):
        if not patterns:
            return list(self._chans.values())
        return [
            c for n, c in self._chans.items() if any(fnmatch.fnmatchcase(n, p) for p in patterns)
        ]

    def chan(self, name):
        if isinstance(name, int):
            vals = list(self._chans.values())
            return vals[name] if 0 <= name < len(vals) else None
        return self._chans.get(name)

    def __getitem__(self, key):
        self._host._ensure_chop(self)
        c = self.chan(key)
        if c is None:
            raise IndexError(key)
        return c

    def numpyArray(self):
        return (
            np.stack([c._vals for c in self._chans.values()]) if self._chans else np.zeros((0, 0))
        )


class _Attrib:
    def __init__(self, name, default):
        self.name, self.default = name, default


class _Attribs:
    def __init__(self):
        self._a = {}

    def create(self, name, default=None):
        self._a[name] = _Attrib(name, default)
        return self._a[name]

    def __getitem__(self, n):
        return self._a[n]

    def __contains__(self, n):
        return n in self._a

    def __iter__(self):
        return iter(self._a.values())


class Point:
    __slots__ = ("index", "P", "N", "Cd", "uv", "_extra")

    def __init__(self, index):
        self.index = index
        self.P = (0.0, 0.0, 0.0)
        self.N = (0.0, 0.0, 1.0)
        self.Cd = (1.0, 1.0, 1.0, 1.0)
        self.uv = (0.0, 0.0, 0.0)
        self._extra = None

    @property
    def x(self):
        return self.P[0]

    @property
    def y(self):
        return self.P[1]

    @property
    def z(self):
        return self.P[2]


class _Vertex:
    __slots__ = ("point",)

    def __init__(self):
        self.point = None


class Poly:
    def __init__(self, n, closed):
        self.vertices = [_Vertex() for _ in range(n)]
        self.closed = closed

    def __getitem__(self, i):
        return self.vertices[i]

    def __len__(self):
        return len(self.vertices)


class SOP(OP):
    family = "SOP"
    isSOP = True

    def __init__(self, host, path, rec):
        super().__init__(host, path, rec)
        self.points = []
        self.prims = []
        self.pointAttribs = _Attribs()
        self.primAttribs = _Attribs()
        self.vertexAttribs = _Attribs()
        self._geo_version = 0

    def clear(self):
        self.points = []
        self.prims = []

    @property
    def numPoints(self):
        return len(self.points)

    @property
    def numPrims(self):
        return len(self.prims)

    def appendPoint(self):
        p = Point(len(self.points))
        self.points.append(p)
        return p

    def appendPoly(self, numVertices, closed=True, addPoints=True):
        poly = Poly(int(numVertices), closed)
        if addPoints:
            for v in poly.vertices:
                v.point = self.appendPoint()
        self.prims.append(poly)
        return poly

    def copy(self, sop):
        self.points = list(sop.points)
        self.prims = list(sop.prims)


class DAT(OP):
    family = "DAT"
    isDAT = True

    def __init__(self, host, path, rec):
        super().__init__(host, path, rec)
        self._text = rec.get("text", "") or ""
        self._table = [list(r) for r in rec.get("table", [])]
        self._module = None
        self._module_src = None

    @property
    def text(self):
        if self._table and not self._text:
            return "\n".join("\t".join(r) for r in self._table)
        return self._text

    @text.setter
    def text(self, t):
        self._text = str(t)
        self._module = None

    @property
    def module(self):
        if self._module is None or self._module_src != self._text:
            self._module = self._host._make_module(self)
            self._module_src = self._text
        return self._module

    def run(self, *args, delayFrames=0, delayMilliSeconds=0, **kw):
        self._host.run(
            self._text,
            *args,
            delayFrames=delayFrames,
            delayMilliSeconds=delayMilliSeconds,
            fromOP=self,
        )

    # table API
    @property
    def numRows(self):
        return len(self._table)

    @property
    def numCols(self):
        return max((len(r) for r in self._table), default=0)

    def __getitem__(self, idx):
        r, c = idx
        if isinstance(r, str):
            r = next((i for i, row in enumerate(self._table) if row and row[0] == r), None)
        if isinstance(c, str):
            c = self._table[0].index(c) if self._table and c in self._table[0] else None
        if r is None or c is None or r >= len(self._table) or c >= len(self._table[r]):
            return None
        return Cell(self, r, c)

    def row(self, r):
        if isinstance(r, str):
            r = next((i for i, row in enumerate(self._table) if row and row[0] == r), None)
        return None if r is None else [Cell(self, r, c) for c in range(len(self._table[r]))]

    def col(self, c):
        if isinstance(c, str):
            c = self._table[0].index(c) if self._table and c in self._table[0] else None
        return None if c is None else [Cell(self, r, c) for r in range(len(self._table))]

    def rows(self, *a):
        return [self.row(i) for i in range(len(self._table))]

    def cols(self, *a):
        return [self.col(i) for i in range(self.numCols)]

    def clear(self, keepFirstRow=False, keepFirstCol=False):
        self._table = self._table[:1] if keepFirstRow else []
        self._text = ""

    def appendRow(self, vals=(), *a):
        self._table.append([str(v) for v in vals])

    def write(self, *args):
        self._text += "".join(str(a) for a in args)


class MAT(OP):
    family = "MAT"
    isMAT = True


class POP(OP):
    family = "POP"
    isPOP = True


FAMILY_CLASS = {
    "COMP": COMP,
    "TOP": TOP,
    "CHOP": CHOP,
    "SOP": SOP,
    "DAT": DAT,
    "MAT": MAT,
    "POP": POP,
}


def _is_type(o: OP, t) -> bool:
    if isinstance(t, type):
        return isinstance(o, t)
    if isinstance(t, str):
        return o.OPType == t or o.type == t
    tname = getattr(t, "__name__", None)
    if tname:
        if tname in ("COMP", "TOP", "CHOP", "SOP", "DAT", "MAT", "POP"):
            return o.family == tname
        return o.OPType == tname
    return False


# ================================================================== globals


class _AbsTime:
    def __init__(self):
        self.seconds = 0.0
        self.frame = 1
        self.rate = 60.0
        self.stepSeconds = 1.0 / 60.0
        self.step = 1


class Monitor:
    def __init__(
        self, index, width, height, left=0, top=0, primary=False, name="display", refresh=60.0
    ):
        self.index = index
        self.width = int(width)
        self.height = int(height)
        self.left = int(left)
        self.top = int(top)
        self.right = self.left + self.width
        self.bottom = self.top + self.height
        self.isPrimary = primary
        self.displayName = name
        self.description = name
        self.refreshRate = refresh
        self.dpiScale = 1.0
        self.scaledWidth = self.width
        self.scaledHeight = self.height
        self.isAffinity = False

    def __repr__(self):
        return f"type:Monitor index:{self.index} {self.width}x{self.height}"


class _Monitors(list):
    def refresh(self):
        pass

    @property
    def primary(self):
        return next((m for m in self if m.isPrimary), self[0] if self else None)


class _Project:
    def __init__(self, host, folder, name, cookrate, realtime):
        self._host = host
        self.folder = folder
        self.name = name
        self.cookRate = cookrate
        self.realTime = realtime
        self.paths = {}
        self.performOnStart = True
        self.windowOnTop = False

    def save(self, *a, **k):
        self._host.log("project.save() ignored on device")
        return ""

    def quit(self, force=False, crash=False):
        self._host.quit_requested = True

    def load(self, *a, **k):
        pass


class _UI:
    performMode = True
    showPaletteBrowser = False

    def __getattr__(self, n):
        return None

    def openTextport(self, *a):
        pass

    def status(self, *a):
        pass


class _App:
    samplesFolder = "/usr/share/touchdesigner/Samples"
    installFolder = "/usr/share/touchdesigner"
    userPaletteFolder = ""
    preferencesFolder = ""
    configFolder = ""
    version = "2025.33070"
    build = "33070"
    osName = "Linux"
    osVersion = ""
    product = "TouchDesigner"
    architecture = "64"
    launchTime = 0.0
    power = True
    enableOptimizedExprs = True


class _Parent:
    """`parent` — callable (parent(n)) with parent-shortcut attributes
    (parent.FBX finds the nearest ancestor whose Parent Shortcut is FBX)."""

    def __init__(self, host, me):
        self._host, self._me = host, me

    def __call__(self, n=1):
        return self._me.parent(n)

    def __getattr__(self, shortcut):
        o = self._me.parent()
        while o is not None:
            p = object.__getattribute__(o.par, "_pars").get("parentshortcut")
            if p is not None and str(p.eval()) == shortcut:
                return o
            o = o.parent()
        raise AttributeError(shortcut)


class _OpFinder:
    """`op` inside a module/expression: resolves relative to the owner's network,
    and exposes global OP shortcuts (op.Name)."""

    def __init__(self, host, context):
        self._host, self._context = host, context

    def __call__(self, *paths):
        if len(paths) == 1:
            return self._host.resolve(paths[0], self._context)
        return [self._host.resolve(p, self._context) for p in paths]

    def __getattr__(self, shortcut):
        for o in self._host.ops.values():
            p = object.__getattribute__(o.par, "_pars").get("opshortcut")
            if p is not None and str(p.eval()) == shortcut:
                return o
        raise AttributeError(shortcut)


class _ModFinder:
    def __init__(self, host, context):
        self._host, self._context = host, context

    def __call__(self, path):
        d = self._host.resolve(path, self._context)
        return d.module if d is not None else None

    def __getattr__(self, name):
        d = self._host.resolve(name, self._context)
        if d is None:
            # search upward like TD's mod shortcut
            ctx = self._context
            while ctx and d is None:
                ctx = ctx.rsplit("/", 1)[0]
                d = self._host.resolve(name, ctx or "/")
        if d is None:
            raise AttributeError(name)
        return d.module


def _var(name, *a):
    return {
        "SYS_GFX_GLSL_MAX_UNIFORMS": "1024",
        "SYS_GFX_VENDOR": "Mesa",
    }.get(name, "")


def _debug(*args):
    print("[debug]", *args, flush=True)
