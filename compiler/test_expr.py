"""
Verify the expr transpiler: transpile -> lower (mlir-opt) -> LLVM IR
(mlir-translate) -> native .so (clang), then compare the compiled function against
the Python reference evaluator (runtime.expr.eval_expr) over sample inputs.
Run inside compiler/nix/shell.sh (needs mlir-opt/translate/clang + python3).
"""

import ctypes
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from expr_transpile import transpile  # noqa: E402

from runtime.expr import eval_expr  # noqa: E402
from runtime.services import ChopStore  # noqa: E402

LOWER = [
    "--convert-math-to-llvm",
    "--convert-arith-to-llvm",
    "--convert-func-to-llvm",
    "--reconcile-unrealized-casts",
]


def build(mlir: str, fname: str) -> str:
    d = tempfile.mkdtemp(prefix="toxc_expr_")
    src, low, ll, so = (os.path.join(d, n) for n in ("e.mlir", "low.mlir", "e.ll", "e.so"))
    open(src, "w").write(mlir)
    subprocess.run(["mlir-opt", src, *LOWER, "-o", low], check=True)
    subprocess.run(["mlir-translate", low, "--mlir-to-llvmir", "-o", ll], check=True)
    subprocess.run(["clang", "-O2", "-shared", "-fPIC", ll, "-o", so, "-lm"], check=True)
    return so


def ref(expr, inputs, values):
    """Reference value via the Python evaluator, mapping input names -> values."""
    t = frame = 0.0
    store = ChopStore()
    for name, val in zip(inputs, values):
        if name == "t":
            t = val
        elif name == "frame":
            frame = val
        elif name.startswith("chop_"):
            _, op, ch = name.split("_", 2)
            store.set(op, ch, val)
    return eval_expr(expr, t, int(frame), chops=store)


EXPRS = [
    "absTime.seconds * 10",
    "op('osc1')['rot'] * 2 + 1",
    "(absTime.seconds + op('osc1')['x']) / 2 - 3",
    "sin(absTime.seconds) * op('lfo')['amp']",
    "me.time.frame",  # unsupported -> fallback
]
SAMPLES = [(0.5,), (1.25,), (3.0,), (0.7, 0.4), (2.0,)]

ok = True
for i, expr in enumerate(EXPRS):
    mlir, info = transpile(expr, fname=f"expr{i}")
    if mlir is None:
        print(f"[{i}] FALLBACK (python eval): {expr!r}  reason={info}")
        continue
    inputs = info
    so = build(mlir, f"expr{i}")
    lib = ctypes.CDLL(so)
    fn = getattr(lib, f"expr{i}")
    fn.restype = ctypes.c_double
    fn.argtypes = [ctypes.c_double] * len(inputs)
    # sample values: cycle a couple of test vectors per input arity
    import itertools

    passed = True
    for vec in itertools.islice(itertools.product([0.5, 1.25, 3.0, 0.7], repeat=len(inputs)), 8):
        native = fn(*[ctypes.c_double(v) for v in vec])
        expect = ref(expr, inputs, vec)
        if abs(native - expect) > 1e-6:
            print(
                f"[{i}] MISMATCH {expr!r} inputs={inputs} vec={vec}: "
                f"native={native} ref={expect}"
            )
            passed = False
            ok = False
    print(f"[{i}] {'OK  ' if passed else 'FAIL'} {expr!r}  inputs={inputs}")

print("\nALL PASS" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
