"""Emit the CHOP DAG as `tox` dialect MLIR — P0 of the fused CHOP+TOP plan
(docs/design/fused-chop-top-mlir.md).

Reads the `chops` list the importer produces (schedule.json / graph.chops) and
emits one `tox` function that represents the DAG in the same IR as the TOPs, so
later passes can optimize across the CHOP<->TOP (CPU<->GPU) boundary instead of
leaving CHOPs to the runtime fasteval interpreter:

  external op('X') refs      -> tox.chop_source "X"
  constant, literal channels -> tox.chop_constant [v...]
  constant/math, expr chans  -> tox.chop_expr(inputs) ["expr"...]
  speed                      -> tox.chop_speed        (time integrator)
  null/select/out            -> tox.chop_select       (passthrough)

P0 is structural parity with the runtime CHOP eval — the channel expressions are
carried verbatim (no lowering yet). P1 lowers chop_expr -> arith/math and fuses
the DAG into one compiled kernel; see the design doc.

Pure Python (no MLIR needed to emit); round-trips through toxc-opt.
"""
from __future__ import annotations

import re

_OPREF = re.compile(r"op\(\s*['\"]([^'\"]+)['\"]\s*\)")
_FLOAT = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")

# Types that are pure passthrough of their single input (Null/Select/Out CHOPs).
_PASSTHROUGH = {"null", "select", "out", "output"}


def _ssa(name: str) -> str:
    return "%" + re.sub(r"[^A-Za-z0-9_]", "_", name)


def _oprefs(exprs) -> list[str]:
    """External op('X') references across a channel-expr list, first-seen order."""
    seen: list[str] = []
    for e in exprs:
        for m in _OPREF.finditer(str(e)):
            if m.group(1) not in seen:
                seen.append(m.group(1))
    return seen


def emit(chops: list[dict], *, source_width: int = 16, func_name: str = "chops") -> str:
    """Return a `tox` MLIR module string for the CHOP DAG."""
    defined = {c["name"] for c in chops}
    width: dict[str, int] = {}   # ssa name (sans %) -> channel count
    lines: list[str] = []

    # External sources: any op('X') referenced by a channel expr that isn't a
    # defined CHOP (e.g. the MIDI In feeding the Constant). Emit once, up front.
    external: list[str] = []
    for c in chops:
        for ref in _oprefs(c.get("channels", [])):
            if ref not in defined and ref not in external:
                external.append(ref)
    for src in external:
        lines.append(f'  {_ssa(src)} = tox.chop_source "{src}" : !tox.chop<{source_width}>')
        width[src[1:] if src.startswith("%") else src] = source_width
        width[re.sub(r"[^A-Za-z0-9_]", "_", src)] = source_width

    def w(name: str) -> int:
        return width.get(re.sub(r"[^A-Za-z0-9_]", "_", name), 1)

    # The importer emits chops in dependency order; honor it as-is.
    for c in chops:
        name, typ = c["name"], c.get("type", "")
        chans = [str(x) for x in c.get("channels", [])]
        inputs = list(c.get("inputs", []))
        res = _ssa(name)
        rkey = re.sub(r"[^A-Za-z0-9_]", "_", name)

        if typ == "constant":
            n = len(chans) or 1
            if chans and all(_FLOAT.match(x.strip()) for x in chans):
                vals = ", ".join(f"{float(x):g}" for x in chans)
                lines.append(f"  {res} = tox.chop_constant [{vals}] : !tox.chop<{n}>")
            else:
                refs = _oprefs(chans)
                operands = ", ".join(_ssa(r) for r in refs)
                intypes = ", ".join(f"!tox.chop<{w(r)}>" for r in refs)
                chstr = ", ".join(_mlir_str(x) for x in chans)
                lines.append(
                    f"  {res} = tox.chop_expr({operands}) [{chstr}] : "
                    f"({intypes}) -> !tox.chop<{n}>")
            width[rkey] = n

        elif typ == "speed":
            src = inputs[0] if inputs else name
            n = w(src)
            lines.append(
                f"  {res} = tox.chop_speed {_ssa(src)} : "
                f"(!tox.chop<{w(src)}>) -> !tox.chop<{n}>")
            width[rkey] = n

        elif typ in _PASSTHROUGH and inputs:
            src = inputs[0]
            n = w(src)
            lines.append(
                f"  {res} = tox.chop_select {_ssa(src)} : "
                f"(!tox.chop<{w(src)}>) -> !tox.chop<{n}>")
            width[rkey] = n

        else:
            # Unknown CHOP type: represent as an expr node over its inputs so no
            # structure is silently dropped (parity-preserving fallback).
            operands = ", ".join(_ssa(i) for i in inputs)
            intypes = ", ".join(f"!tox.chop<{w(i)}>" for i in inputs)
            n = len(chans) or (w(inputs[0]) if inputs else 1)
            chstr = ", ".join(_mlir_str(x) for x in chans)
            lines.append(
                f"  {res} = tox.chop_expr({operands}) [{chstr}] : "
                f"({intypes}) -> !tox.chop<{n}>")
            width[rkey] = n

    body = "\n".join(lines)
    return (f"module {{\n  func.func @{func_name}() {{\n{body}\n"
            f"    return\n  }}\n}}\n")


def _mlir_str(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


if __name__ == "__main__":
    import json
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "/dev/stdin"
    obj = json.load(open(src))
    chops = obj.get("chops", obj) if isinstance(obj, dict) else obj
    sys.stdout.write(emit(chops))
