"""
Parameter-expression transpiler: a TD/Python parameter expression -> an MLIR
function in the arith/math/func dialects (all f64), for AOT compilation to native
code (LLVM/NEON) on the Pi. Expressions that use unsupported constructs return
None so the caller can fall back to the Python interpreter (per the design).

Supported: numeric literals, + - * / and ** (pow), unary minus, `pi`,
math funcs (sin/cos/tan/sqrt/floor/ceil/abs), `absTime.seconds|frame|step`, and
`op('name')['chan']` (a live CHOP channel). Inputs become f64 function arguments
in a stable order (returned alongside the MLIR) that the runtime supplies.
"""
from __future__ import annotations
import ast
import math

_BINOPS = {ast.Add: "arith.addf", ast.Sub: "arith.subf",
           ast.Mult: "arith.mulf", ast.Div: "arith.divf"}
_FUNCS = {"sin": "math.sin", "cos": "math.cos", "tan": "math.tan",
          "sqrt": "math.sqrt", "floor": "math.floor", "ceil": "math.ceil",
          "abs": "math.absf"}


class Unsupported(Exception):
    pass


class _Gen:
    def __init__(self):
        self.lines: list[str] = []
        self.n = 0
        self.inputs: list[str] = []   # ordered arg names (t, frame, chop_<op>_<ch>)

    def _fresh(self) -> str:
        v = f"%v{self.n}"; self.n += 1; return v

    def _arg(self, name: str) -> str:
        if name not in self.inputs:
            self.inputs.append(name)
        return f"%arg_{name}"

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
            a = self.emit(node.operand); v = self._fresh()
            self.lines.append(f"{v} = arith.negf {a} : f64"); return v
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Pow):
                a = self.emit(node.left); b = self.emit(node.right); v = self._fresh()
                self.lines.append(f"{v} = math.powf {a}, {b} : f64"); return v
            op = _BINOPS.get(type(node.op))
            if not op:
                raise Unsupported(f"binop {type(node.op).__name__}")
            a = self.emit(node.left); b = self.emit(node.right); v = self._fresh()
            self.lines.append(f"{v} = {op} {a}, {b} : f64"); return v
        if isinstance(node, ast.Name):
            if node.id == "pi":
                return self._const(math.pi)
            raise Unsupported(f"name {node.id!r}")
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "absTime":
                if node.attr == "seconds":
                    return self._arg("t")
                if node.attr in ("frame", "step"):
                    return self._arg("frame")
            raise Unsupported("attribute")
        if isinstance(node, ast.Call):
            fn = node.func.id if isinstance(node.func, ast.Name) else None
            if fn in _FUNCS and len(node.args) == 1:
                a = self.emit(node.args[0]); v = self._fresh()
                self.lines.append(f"{v} = {_FUNCS[fn]} {a} : f64"); return v
            raise Unsupported(f"call {fn!r}")
        if isinstance(node, ast.Subscript):
            base = node.value
            if (isinstance(base, ast.Call) and isinstance(base.func, ast.Name)
                    and base.func.id == "op" and base.args
                    and isinstance(base.args[0], ast.Constant)):
                sl = node.slice
                if isinstance(sl, ast.Index):        # py<3.9
                    sl = sl.value
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                    return self._arg(f"chop_{base.args[0].value}_{sl.value}")
            raise Unsupported("subscript")
        raise Unsupported(type(node).__name__)


def transpile(expr, fname: str = "expr"):
    """Return (mlir_text, input_names) or (None, reason) if unsupported."""
    try:
        tree = ast.parse(str(expr).strip(), mode="eval")
        g = _Gen()
        ret = g.emit(tree.body)
        args = ", ".join(f"%arg_{a}: f64" for a in g.inputs)
        body = "\n    ".join(g.lines)
        mlir = (f"func.func @{fname}({args}) -> f64 {{\n"
                f"    {body}\n"
                f"    return {ret} : f64\n"
                f"}}\n")
        return mlir, list(g.inputs)
    except (Unsupported, SyntaxError) as e:
        return None, str(e)
