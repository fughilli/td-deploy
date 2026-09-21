"""Lower the CHOP DAG to ONE MLIR function (arith/math/func, all f64) — P1 of the
fused CHOP+TOP plan (docs/design/fused-chop-top-mlir.md).

Instead of interpreting the DAG per node at runtime (fasteval), compile the whole
thing to native code. Within the kernel every CHOP channel is an SSA value — the
store only crosses the ABI boundary for three things, which become the function's
args/results:

  sources  live inputs (MIDI/OSC channels `op('X')[c]` where X isn't a CHOP) -> args
  states   Speed CHOP accumulators (loop-carried across frames)  -> args AND results
  outputs  every channel the DAG writes                          -> results

  func.func private @chops(%t, %dt, %frame, <sources...>, <states-in...>)
      -> (<outputs...>)          // a Speed output IS its next-frame state

Channel semantics match runtime_rs eval_chops / the fasteval preprocess:
  constant channel i = eval(expr)      (op('X')[c][s] drops the sample s)
  speed    ch0       = state_in + input_ch0 * dt
  else               = input_ch0       (Null/Select passthrough)

Unsupported constant exprs raise Unsupported so the caller can keep that CHOP on
the fasteval path (partial lowering) — parity is preserved either way.
"""

from __future__ import annotations

import ast
import math

_BINOPS = {
    ast.Add: "arith.addf",
    ast.Sub: "arith.subf",
    ast.Mult: "arith.mulf",
    ast.Div: "arith.divf",
}
_FUNCS = {
    "sin": "math.sin",
    "cos": "math.cos",
    "tan": "math.tan",
    "sqrt": "math.sqrt",
    "floor": "math.floor",
    "ceil": "math.ceil",
    "abs": "math.absf",
}
_PASSTHROUGH = {"null", "select", "in", "out", "output"}


class Unsupported(Exception):
    pass


def _san(s: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def _is_op_call(n) -> bool:
    return (
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "op"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and isinstance(n.args[0].value, str)
    )


def _index_str(sl) -> str:
    if isinstance(sl, ast.Index):  # py<3.9
        sl = sl.value
    if isinstance(sl, ast.Constant):
        v = sl.value
        return str(int(v)) if isinstance(v, (int, float)) else str(v)
    if (
        isinstance(sl, ast.UnaryOp)
        and isinstance(sl.op, ast.USub)
        and isinstance(sl.operand, ast.Constant)
    ):
        return str(-int(sl.operand.value))
    raise Unsupported("non-constant channel index")


def _op_ref(node):
    """(chop_name, channel_str) for op('X')[c] or op('X')[c][s] (sample dropped)."""
    if not isinstance(node, ast.Subscript):
        return None
    inner = node.value
    if _is_op_call(inner):  # op('X')[c]
        return inner.args[0].value, _index_str(node.slice)
    if isinstance(inner, ast.Subscript) and _is_op_call(inner.value):
        return inner.value.args[0].value, _index_str(inner.slice)  # op('X')[c][s]
    return None


class _Fn:
    """Emits SSA into a shared function body; resolves op()/absTime to SSA."""

    def __init__(self, env: dict, t_ssa: str, frame_ssa: str):
        self.lines: list[str] = []
        self.n = 0
        self.env = env  # (name, chan) -> ssa ; sources+states pre-bound
        self.t, self.frame = t_ssa, frame_ssa

    def _fresh(self) -> str:
        v = f"%v{self.n}"
        self.n += 1
        return v

    def _const(self, val: float) -> str:
        v = self._fresh()
        self.lines.append(f"{v} = arith.constant {float(val)!r} : f64")
        return v

    def emit(self, node) -> str:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise Unsupported(f"constant {node.value!r}")
            return self._const(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            a = self.emit(node.operand)
            v = self._fresh()
            self.lines.append(f"{v} = arith.negf {a} : f64")
            return v
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Pow):
                a = self.emit(node.left)
                b = self.emit(node.right)
                v = self._fresh()
                self.lines.append(f"{v} = math.powf {a}, {b} : f64")
                return v
            op = _BINOPS.get(type(node.op))
            if not op:
                raise Unsupported(f"binop {type(node.op).__name__}")
            a = self.emit(node.left)
            b = self.emit(node.right)
            v = self._fresh()
            self.lines.append(f"{v} = {op} {a}, {b} : f64")
            return v
        if isinstance(node, ast.Name):
            if node.id == "pi":
                return self._const(math.pi)
            if node.id == "e":
                return self._const(math.e)
            raise Unsupported(f"name {node.id!r}")
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "absTime":
                if node.attr == "seconds":
                    return self.t
                if node.attr in ("frame", "step"):
                    return self.frame
            raise Unsupported("attribute")
        if isinstance(node, ast.Call):
            fn = node.func.id if isinstance(node.func, ast.Name) else None
            if fn in _FUNCS and len(node.args) == 1:
                a = self.emit(node.args[0])
                v = self._fresh()
                self.lines.append(f"{v} = {_FUNCS[fn]} {a} : f64")
                return v
            raise Unsupported(f"call {fn!r}")
        if isinstance(node, ast.Subscript):
            ref = _op_ref(node)
            if ref is None:
                raise Unsupported("subscript")
            if ref not in self.env:
                raise Unsupported(f"unresolved op ref {ref}")
            return self.env[ref]
        raise Unsupported(type(node).__name__)


def _expr_refs(exprs) -> list[tuple[str, str]]:
    """(name, chan) op-refs across a channel-expr list, first-seen order."""
    seen: list[tuple[str, str]] = []
    for e in exprs:
        try:
            tree = ast.parse(str(e).strip().strip('"').strip("'"), mode="eval")
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            r = _op_ref(node)
            if r and r not in seen:
                seen.append(r)
    return seen


def _widths(chops: list[dict], defined: set[str]) -> dict[str, int]:
    """Channel count per CHOP, propagated in dependency order.

    Only a Constant states its own width; everything else is as wide as what it
    carries. Deriving it once here is what lets every op emit per-channel, rather
    than each kind having its own (and, historically, its own single-channel
    assumption). `chops` arrives topologically ordered, so a producer is always
    measured before its consumer."""
    w: dict[str, int] = {}
    for c in chops:
        name = c["name"]
        if c.get("type") == "constant":
            w[name] = max(1, len(c.get("channels") or []))
            continue
        inps = c.get("inputs") or []
        if not inps:
            w[name] = 1
            continue
        src = inps[0]
        if src not in defined:
            # A live MIDI/OSC service. The importer keeps it in `inputs` but
            # leaves it out of the DAG (it has no expression to fuse), and how
            # many channels it will publish — and under which names — is only
            # known once it is running. Decline instead of guessing one channel:
            # the interpreted path can see the real channel set, this cannot.
            raise Unsupported(
                f"CHOP {name!r} carries live source {src!r}; the fused kernel "
                f"needs a channel count known at compile time"
            )
        if src not in w:
            raise Unsupported(f"CHOP {name!r} reads {src!r} before it is defined")
        w[name] = w[src]
    return w


def _input_ssa(env, fn, name: str, inps: list, i: int, verb: str) -> str:
    """The SSA value for channel `i` of `name`'s input.

    A CHOP with no input contributes zero — that is a real shape. A CHOP that
    HAS an input whose channel never got bound is not: it used to silently
    become 0.0, so a Speed fed something unresolved integrated nothing forever
    and reported success. Refuse, and name what is missing."""
    if not inps:
        return fn._const(0.0)
    ssa = env.get((inps[0], str(i)))
    if ssa is None:
        raise Unsupported(f"CHOP {name!r} {verb} {inps[0]!r} channel {i}, which is never bound")
    return ssa


def lower(chops: list[dict], func_name: str = "chops"):
    """Return (mlir_text, abi). abi = {'sources':[(n,c)], 'states':[(n,'0')],
    'outputs':[(n,c)]} giving the arg/result order after (t, dt, frame)."""
    defined = {c["name"] for c in chops}

    # Sources: op-refs to non-CHOPs (live MIDI/OSC channels).
    sources: list[tuple[str, str]] = []
    for c in chops:
        if c.get("type") == "constant":
            for ref in _expr_refs(c.get("channels", [])):
                if ref[0] not in defined and ref not in sources:
                    sources.append(ref)
    widths = _widths(chops, defined)
    # A Speed integrates each channel independently, so it carries one
    # accumulator PER CHANNEL.
    states = [
        (c["name"], str(i))
        for c in chops
        if c.get("type") == "speed"
        for i in range(widths[c["name"]])
    ]

    env: dict[tuple[str, str], str] = {}
    args = ["%t: f64", "%dt: f64", "%frame: f64"]
    for n, ch in sources:
        a = f"%src_{_san(n)}_{_san(ch)}"
        env[(n, ch)] = a
        args.append(f"{a}: f64")
    for n, ch in states:
        a = f"%st_{_san(n)}_{_san(ch)}"
        env[("__state__", n, ch)] = a  # carried-in accumulator, per channel
        args.append(f"{a}: f64")

    fn = _Fn(env, "%t", "%frame")
    outputs: list[tuple[str, str]] = []

    def bind(n, ch, ssa):
        env[(n, str(ch))] = ssa
        outputs.append((n, str(ch)))

    for c in chops:
        name, typ = c["name"], c.get("type", "")
        if typ == "constant":
            for i, e in enumerate(c.get("channels", [])):
                tree = ast.parse(str(e).strip().strip('"').strip("'"), mode="eval")
                bind(name, i, fn.emit(tree.body))
        elif typ == "speed":
            inps = c.get("inputs") or []
            for i in range(widths[name]):
                iv = _input_ssa(env, fn, name, inps, i, "integrates")
                m = fn._fresh()
                fn.lines.append(f"{m} = arith.mulf {iv}, %dt : f64")
                acc = fn._fresh()
                fn.lines.append(f"{acc} = arith.addf {env[('__state__', name, str(i))]}, {m} : f64")
                bind(name, i, acc)
        else:
            # null / select / in / out: alias every channel of the input.
            inps = c.get("inputs") or []
            for i in range(widths[name]):
                bind(name, i, _input_ssa(env, fn, name, inps, i, "carries"))

    if not outputs:
        raise Unsupported("empty CHOP DAG")
    ret_ssa = ", ".join(env[o] for o in outputs)
    ret_ty = ", ".join("f64" for _ in outputs)
    ret_ty = ret_ty if len(outputs) == 1 else f"({ret_ty})"
    body = "\n    ".join(fn.lines)
    mlir = (
        f"func.func private @{func_name}({', '.join(args)}) -> {ret_ty}\n"
        f"    attributes {{llvm.linkage = #llvm.linkage<internal>}} {{\n"
        f"    {body}\n"
        f"    return {ret_ssa} : {', '.join('f64' for _ in outputs)}\n"
        f"}}\n"
    )
    mlir += _wrapper(abi_of(sources, states, outputs), func_name)
    return mlir, abi_of(sources, states, outputs)


def abi_of(sources, states, outputs) -> dict:
    return {"sources": sources, "states": states, "outputs": outputs}


def _wrapper(abi: dict, func_name: str) -> str:
    """A stable C ABI over the (arity-varying) scalar @chops:

        void <name>_v(const double* in, double* out)

    in  = [t, dt, frame, <sources…>, <states-in…>]   (scalar-arg order)
    out = [<outputs…>]

    so the runtime dlopens one symbol and calls it with two f64 buffers, no
    per-graph signature. Emitted in the llvm dialect; the standard
    convert-*-to-llvm passes turn the func.call into an llvm.call.

    This wrapper is the ONLY exported entry. @chops itself is private, because
    its multi-result signature lowers to a literal struct return that is not
    AArch64 C-ABI: LLVM hands back 5..8 doubles in d0..d7, while AAPCS says an
    HFA stops at 4 members and anything larger returns indirectly via x8. A C
    caller in that range reads uninitialized memory and gets no diagnostic.
    (<=4 is a real HFA and >8 falls back to indirect, so both happen to agree,
    which is exactly what made the gap easy to miss.) Keeping @chops internal
    means the broken signature is never something a caller can bind to.
    """
    n_in = 3 + len(abi["sources"]) + len(abi["states"])
    n_out = len(abi["outputs"])
    lines = [f"llvm.func @{func_name}_v(%in: !llvm.ptr, %out: !llvm.ptr) {{"]
    argv = []
    for i in range(n_in):
        lines.append(f"  %pi{i} = llvm.getelementptr %in[{i}] : " f"(!llvm.ptr) -> !llvm.ptr, f64")
        lines.append(f"  %ai{i} = llvm.load %pi{i} : !llvm.ptr -> f64")
        argv.append(f"%ai{i}")
    intys = ", ".join("f64" for _ in range(n_in))
    outtys = ", ".join("f64" for _ in range(n_out))
    if n_out == 1:
        lines.append(f"  %r = func.call @{func_name}({', '.join(argv)}) : " f"({intys}) -> f64")
        res = ["%r"]
    else:
        lines.append(
            f"  %r:{n_out} = func.call @{func_name}({', '.join(argv)}) : "
            f"({intys}) -> ({outtys})"
        )
        res = [f"%r#{i}" for i in range(n_out)]
    for i in range(n_out):
        lines.append(f"  %po{i} = llvm.getelementptr %out[{i}] : " f"(!llvm.ptr) -> !llvm.ptr, f64")
        lines.append(f"  llvm.store {res[i]}, %po{i} : f64, !llvm.ptr")
    lines.append("  llvm.return")
    lines.append("}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    import json
    import sys

    obj = json.load(open(sys.argv[1]))
    chops = obj.get("chops", obj) if isinstance(obj, dict) else obj
    mlir, abi = lower(chops)
    sys.stderr.write(f"// abi: {json.dumps(abi)}\n")
    sys.stdout.write(mlir)
