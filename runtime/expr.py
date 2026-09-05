"""
Minimal TouchDesigner parameter-expression evaluator (the per-frame dynamic path).

TD parameters can be Python expressions (e.g. rotate = "absTime.seconds * 10").
Full TD Python is out of scope; this evaluates simple arithmetic over a small,
safe namespace (absTime, math) with builtins disabled. Unsupported/failing
expressions fall back to a default so a frame still renders.
"""
from __future__ import annotations
import math


class _AbsTime:
    def __init__(self, t: float, frame: int):
        self.seconds = t
        self.frame = frame
        self.step = frame


_SAFE = {
    "math": math, "pi": math.pi, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "abs": abs, "min": min, "max": max, "pow": pow, "sqrt": math.sqrt,
    "radians": math.radians, "degrees": math.degrees, "floor": math.floor,
    "ceil": math.ceil, "mod": math.fmod,
}


class _ChopAccessor:
    """Backs `op('name')['chan']` in parameter expressions, reading live values
    from a ChopStore (OSC/MIDI). Returns 0.0 when unset / no store."""
    def __init__(self, store, name):
        self._store, self._name = store, name

    def __getitem__(self, chan):
        if self._store is None:
            return 0.0
        return self._store.get_chan(self._name, str(chan), 0.0)


def eval_expr(expr, t: float, frame: int, default: float = 0.0, chops=None) -> float:
    if isinstance(expr, (int, float)):
        return float(expr)
    s = str(expr).strip().strip('"').strip("'")
    if s == "":
        return default
    ns = {**_SAFE, "absTime": _AbsTime(t, frame),
          "op": lambda name: _ChopAccessor(chops, name)}
    try:
        return float(eval(s, {"__builtins__": {}}, ns))
    except Exception:
        try:
            return float(s)
        except Exception:
            return default


def value_or_expr(raw: str, default: float) -> object:
    """Interpret a `.parm` value remainder. If it carries a quoted expression,
    return that expression string; otherwise the leading numeric literal; else
    the default."""
    if raw is None:
        return default
    s = str(raw).strip()
    q = s.find('"')
    if q != -1:
        end = s.find('"', q + 1)
        if end != -1:
            return s[q + 1:end]
    toks = s.split()
    tok = toks[0] if toks else ""
    try:
        return float(tok)
    except Exception:
        # not numeric and not TD-quoted -> treat the whole string as an expression
        return s if s else default
