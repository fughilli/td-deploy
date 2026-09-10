"""Reference CHOP-DAG evaluator — the conformance oracle for the compiled kernel.

Replicates runtime_rs `Renderer::eval_chops` (runtime_rs/src/main.rs) exactly, so
the P1 compiled kernel (chop_lower.py -> MLIR -> .so) can be bit-diffed against it:

  dt    = clamp(t - last_t, 0, 1)
  frame = floor(t * 60)
  constant: each channel i = eval(expr) read/written through the store
  speed:    acc[(name,0)] += input_ch0 * dt ; write acc
  else:     passthrough — copy input channel 0

Within a frame, a CHOP reads the *current* frame's upstream values (the store is
updated in DAG order), matching the runtime. Expression semantics match the
runtime's fasteval + preprocess: `op('X')[c]` reads channel c; a trailing
`[sample]` is dropped; `absTime.seconds->t`, `.frame/.step->frame`; non-finite->0.
"""
from __future__ import annotations

import math


class _Sample(float):
    """A CHOP channel value that also swallows a trailing sample subscript, so
    `op('X')[c][s]` == `op('X')[c]` (the runtime drops the sample index)."""
    def __getitem__(self, _k):
        return self


class _AbsTime:
    def __init__(self, t: float, frame: float):
        self.seconds = t
        self.frame = frame
        self.step = frame


_SAFE = {
    "pi": math.pi, "e": math.e, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "abs": abs, "min": min, "max": max, "pow": pow, "sqrt": math.sqrt,
    "floor": math.floor, "ceil": math.ceil, "radians": math.radians,
    "degrees": math.degrees, "mod": math.fmod, "math": math,
}


class Store:
    def __init__(self):
        self.d: dict[tuple[str, str], float] = {}

    def get(self, name: str, chan) -> float:
        return self.d.get((name, str(chan)), 0.0)

    def set(self, name: str, chan, v: float) -> None:
        self.d[(name, str(chan))] = float(v)


def eval_expr(expr, t: float, frame: float, store: Store) -> float:
    s = str(expr).strip().strip('"').strip("'")
    if s == "":
        return 0.0

    class _Acc:
        def __init__(self, name):
            self.name = name

        def __getitem__(self, chan):
            return _Sample(store.get(self.name, chan))

    ns = {**_SAFE, "absTime": _AbsTime(t, frame), "op": lambda n: _Acc(n)}
    try:
        v = float(eval(s, {"__builtins__": {}}, ns))  # noqa: S307 (sandboxed ns)
        return v if math.isfinite(v) else 0.0
    except Exception:
        try:
            v = float(s)
            return v if math.isfinite(v) else 0.0
        except Exception:
            return 0.0


class ChopEval:
    """Stateful DAG evaluator. `step(t, sources)` returns the post-frame Store."""
    def __init__(self, chops: list[dict]):
        self.chops = chops
        self.speed: dict[tuple[str, int], float] = {}
        self.last_t = 0.0

    def step(self, t: float, sources: dict[str, dict]) -> Store:
        store = Store()
        for nm, chans in sources.items():
            for c, v in chans.items():
                store.set(nm, c, v)
        dt = min(max(t - self.last_t, 0.0), 1.0)
        self.last_t = t
        frame = math.floor(t * 60.0)
        for c in self.chops:
            name, typ = c["name"], c.get("type", "")
            if typ == "constant":
                for i, e in enumerate(c.get("channels", [])):
                    store.set(name, i, eval_expr(e, t, frame, store))
            elif typ == "speed":
                inps = c.get("inputs") or []
                if inps:
                    iv = store.get(inps[0], "0")
                    key = (name, 0)
                    self.speed[key] = self.speed.get(key, 0.0) + iv * dt
                    store.set(name, "0", self.speed[key])
            else:
                inps = c.get("inputs") or []
                if inps:
                    store.set(name, "0", store.get(inps[0], "0"))
        return store
