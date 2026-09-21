"""Reference CHOP-DAG evaluator — the conformance oracle for the compiled kernel.

Replicates runtime_rs `Renderer::eval_chops` (runtime_rs/src/main.rs) exactly, so
the P1 compiled kernel (chop_lower.py -> MLIR -> .so) can be bit-diffed against it:

  dt    = clamp(t - last_t, 0, 1)
  frame = floor(t * 60)
  constant: each channel i = eval(expr) read/written through the store
  speed:    acc[(name,i)] += input_ch_i * dt ; write acc, for every channel i
  else:     passthrough — copy every channel of the input

A CHOP's channel count comes from the Constant that originates it and is carried
by everything downstream, so each op evaluates ALL of its channels rather than
just the first.

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
    "pi": math.pi,
    "e": math.e,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "abs": abs,
    "min": min,
    "max": max,
    "pow": pow,
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "radians": math.radians,
    "degrees": math.degrees,
    "mod": math.fmod,
    "math": math,
}


class Store:
    def __init__(self):
        self.d: dict[tuple[str, str], float] = {}

    def get(self, name: str, chan) -> float:
        return self.d.get((name, str(chan)), 0.0)

    def set(self, name: str, chan, v: float) -> None:
        self.d[(name, str(chan))] = float(v)

    def channels(self, name: str) -> list[str]:
        """Channel keys `name` actually published, numeric ones in numeric order
        (so channel 10 does not sort ahead of channel 2)."""
        ks = [c for (n, c) in self.d if n == name]
        return sorted(ks, key=lambda k: (0, int(k), "") if k.isdigit() else (1, 0, k))


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
        self.speed: dict[tuple[str, str], float] = {}
        self.last_t = 0.0
        self.defined = {c["name"] for c in chops}
        # Channel count per CHOP. Only a Constant declares its own; everything
        # else is as wide as what it carries. Derived independently of the
        # lowering's copy on purpose — if the two ever disagree, the bit-parity
        # test sees it instead of both being quietly wrong together.
        #
        # `dynamic` is the exception: a CHOP carrying a live MIDI/OSC service has
        # no width until that service publishes, and its channels are NAMED, not
        # indexed. Those read their channel set from the store each frame, and
        # the property is contagious — a Null downstream of one is just as
        # unindexable. The lowering declines this shape outright; the runtime
        # (which this mirrors) evaluates it, so it has to be modelled here.
        self.width: dict[str, int] = {}
        self.dynamic: set[str] = set()
        for c in self.chops:
            name = c["name"]
            if c.get("type") == "constant":
                self.width[name] = max(1, len(c.get("channels") or []))
                continue
            ins = c.get("inputs") or []
            if ins and (ins[0] not in self.defined or ins[0] in self.dynamic):
                self.dynamic.add(name)
                continue
            self.width[name] = self.width.get(ins[0], 1) if ins else 1

    def _in_channels(self, store: "Store", name: str, src: str) -> list[str]:
        """Channel keys to carry from `src` into `name`."""
        if name in self.dynamic:
            return store.channels(src)
        return [str(i) for i in range(self.width.get(name, 1))]

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
                    for ch in self._in_channels(store, name, inps[0]):
                        iv = store.get(inps[0], ch)
                        key = (name, ch)
                        self.speed[key] = self.speed.get(key, 0.0) + iv * dt
                        store.set(name, ch, self.speed[key])
            else:
                inps = c.get("inputs") or []
                if inps:
                    for ch in self._in_channels(store, name, inps[0]):
                        store.set(name, ch, store.get(inps[0], ch))
        return store
