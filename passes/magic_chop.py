"""'Magic chop' pass: drive unsupported CHOPs with sinusoids so a project still animates.

td-deploy only evaluates a few CHOP types (constant, speed, and passthrough-style
null/select/in/out). A project that drives parameters from an unsupported generator —
an LFO, noise, pattern, audio-reactive CHOP, etc. — would otherwise see those channels
resolve to a dead 0. In "magic chop" mode we replace every downstream reference to such a
CHOP's channel with an inline sinusoid of `absTime.seconds` at a random (but stable)
period and phase, so the piece moves while real support is added.

This is a pure IR rewrite: it edits the expression strings the runtime already evaluates
(`op('lfo1')['tx']` -> `(sin(absTime.seconds * 0.83 + 1.7))`), so it needs no runtime,
emitter, or shader changes. `sin` + `absTime.seconds` are supported by both the compiled
and interpreted expression paths.
"""

from __future__ import annotations

import hashlib
import math
import re

from ir.graph import Graph

# CHOP types td-deploy actually evaluates (mirror of the runtime's chop match arms):
# constant (expr channels), speed (integrator), and passthrough-copy of input 0.
SUPPORTED_CHOP_TYPES = {"constant", "speed", "null", "select", "in", "out"}

# op('name')[sub][sub]... — capture the referenced op name and any subscript chain.
_REF = re.compile(r"op\(\s*['\"]([^'\"]+)['\"]\s*\)((?:\[[^\]]*\])*)")


def unsupported_chops(g: Graph) -> list[dict]:
    """CHOP defs whose type td-deploy can't evaluate (candidates for magic/fix-it)."""
    return [c for c in getattr(g, "chops", []) if c.get("type") not in SUPPORTED_CHOP_TYPES]


def _first_channel(subscripts: str) -> str:
    """The channel key from a subscript chain: `['tx']` -> tx, `[0][0]` -> 0, `` -> 0."""
    m = re.search(r"\[\s*['\"]?([^'\"\]]*)['\"]?\s*\]", subscripts)
    tok = (m.group(1).strip() if m else "") or "0"
    return tok


def _sinusoid(name: str, chan: str) -> str:
    """A stable sinusoid expr for one channel: period 1.5–8 s, random phase, range [-1,1].
    Deterministic in the (chop, channel) name so recompiles are reproducible."""
    h = int(hashlib.md5(f"{name}:{chan}".encode()).hexdigest(), 16)
    period = 1.5 + (h % 1000) / 1000.0 * 6.5  # 1.5 .. 8.0 seconds
    phase = ((h >> 10) % 1000) / 1000.0 * 2.0 * math.pi  # 0 .. 2π
    w = 2.0 * math.pi / period
    return f"(sin(absTime.seconds * {w:.4f} + {phase:.4f}))"


def apply(g: Graph) -> list[dict]:
    """Rewrite references to unsupported CHOPs into sinusoids across all node params and
    remaining chop channel expressions. Returns one record per substituted CHOP:
    {"name","type","channels":[keys...]} — for logging and the fix-it prompt. The
    substituted CHOP defs are dropped from the graph (now inlined into expressions)."""
    targets = {c["name"]: c.get("type", "?") for c in unsupported_chops(g)}
    if not targets:
        return []

    hit: dict[str, set] = {name: set() for name in targets}

    def rewrite(text):
        if not isinstance(text, str) or "op(" not in text:
            return text

        def repl(m):
            name, subs = m.group(1), m.group(2)
            if name not in targets:
                return m.group(0)
            chan = _first_channel(subs)
            hit[name].add(chan)
            return _sinusoid(name, chan)

        return _REF.sub(repl, text)

    for node in g.nodes.values():
        node.params = {k: rewrite(v) for k, v in node.params.items()}
    for c in getattr(g, "chops", []):
        if "channels" in c:
            c["channels"] = [rewrite(ch) for ch in c["channels"]]

    # Drop the now-inlined unsupported CHOP defs from the DAG.
    g.chops = [c for c in getattr(g, "chops", []) if c["name"] not in targets]

    return [
        {"name": name, "type": targets[name], "channels": sorted(hit[name])}
        for name in sorted(targets)
        if hit[name]  # only report the ones actually referenced downstream
    ]
