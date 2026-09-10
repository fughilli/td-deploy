"""Bit-parity gate for the P1 CHOP-DAG compiler: lower the DAG to MLIR, compile it
to a native .so (mlir-opt -> mlir-translate -> clang), then drive it frame-by-frame
alongside the reference evaluator (chop_ref.ChopEval, which mirrors runtime_rs
eval_chops) — feeding the same sources and carrying the Speed state across frames —
and assert every output channel matches.

Run inside compiler/nix/shell.sh (needs mlir-opt/translate/clang + python3); it's
tagged manual/local/requires-network in BUILD.
"""
import ctypes
import math
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chop_lower import lower                       # noqa: E402
from chop_ref import ChopEval                      # noqa: E402

LOWER = ["--convert-math-to-llvm", "--convert-arith-to-llvm",
         "--convert-func-to-llvm", "--reconcile-unrealized-casts"]


def build(mlir: str) -> str:
    d = tempfile.mkdtemp(prefix="toxc_chop_")
    src, low, ll, so = (os.path.join(d, n) for n in ("c.mlir", "low.mlir", "c.ll", "c.so"))
    open(src, "w").write("module {\n" + mlir + "}\n")
    subprocess.run(["mlir-opt", src, *LOWER, "-o", low], check=True)
    subprocess.run(["mlir-translate", low, "--mlir-to-llvmir", "-o", ll], check=True)
    subprocess.run(["clang", "-O2", "-shared", "-fPIC", ll, "-o", so, "-lm"], check=True)
    return so


def _ret_type(n: int):
    return type(f"Ret{n}", (ctypes.Structure,),
               {"_fields_": [(f"f{i}", ctypes.c_double) for i in range(n)]})


def run_parity(name: str, chops: list, frames: list) -> bool:
    """frames: list of (t, {chop_name: {chan: value}}) live source inputs."""
    mlir, abi = lower(chops)
    so = build(mlir)
    lib = ctypes.CDLL(so)
    n_in = 3 + len(abi["sources"]) + len(abi["states"])
    n_out = len(abi["outputs"])
    # scalar entry (struct return) — used to cross-check the pointer wrapper.
    fn = lib.chops
    fn.argtypes = [ctypes.c_double] * n_in
    fn.restype = _ret_type(n_out)
    # pointer entry `void chops_v(const double* in, double* out)` — the runtime path.
    fnv = lib.chops_v
    fnv.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)]
    fnv.restype = None
    out_index = {o: i for i, o in enumerate(abi["outputs"])}

    ref = ChopEval(chops)
    state = {s: 0.0 for s in abi["states"]}         # carried Speed accumulators
    last_t = 0.0
    ok = True
    for (t, sources) in frames:
        dt = min(max(t - last_t, 0.0), 1.0)
        frame = math.floor(t * 60.0)
        last_t = t
        args = [t, dt, frame]
        args += [sources.get(n, {}).get(c, 0.0) for (n, c) in abi["sources"]]
        args += [state[s] for s in abi["states"]]
        # pointer ABI (runtime path)
        in_buf = (ctypes.c_double * n_in)(*args)
        out_buf = (ctypes.c_double * n_out)()
        fnv(in_buf, out_buf)
        native = list(out_buf)
        # scalar ABI must agree with the pointer wrapper
        sres = fn(*[ctypes.c_double(a) for a in args])
        for i in range(n_out):
            if abs(getattr(sres, f"f{i}") - native[i]) > 0:
                print(f"  ABI MISMATCH {name} t={t} out{i}: "
                      f"scalar={getattr(sres, f'f{i}')} ptr={native[i]}")
                ok = False
        # feed each Speed output back as its next-frame state
        for s in abi["states"]:
            state[s] = native[out_index[s]]

        store = ref.step(t, sources)
        for o in abi["outputs"]:
            exp = store.get(o[0], o[1])
            got = native[out_index[o]]
            if abs(got - exp) > 1e-9:
                print(f"  MISMATCH {name} t={t} {o}: native={got} ref={exp}")
                ok = False
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}  "
          f"sources={abi['sources']} states={abi['states']} outputs={abi['outputs']}")
    return ok


# The ascii banana: MIDI -> Constant(expr) -> Speed(integrate).
ASCII = [
    {"name": "constant1", "type": "constant", "inputs": [],
     "channels": ["op('midiin1')[0][0]/127 - 0.5"]},
    {"name": "speed1", "type": "speed", "inputs": ["constant1"], "channels": []},
]
# A knob sweep over ~a second of frames (60fps), then held.
ASCII_FRAMES = [(i / 60.0, {"midiin1": {"0": v}})
                for i, v in enumerate([0, 32, 64, 96, 127, 127, 100, 64, 0, 0, 0])]

# A second DAG exercising literals, absTime, a Null passthrough, funcs.
MIX = [
    {"name": "c1", "type": "constant", "inputs": [],
     "channels": ["sin(absTime.seconds) + op('osc1')[0] * 2", "3.5"]},
    {"name": "sp", "type": "speed", "inputs": ["c1"], "channels": []},
    {"name": "n1", "type": "null", "inputs": ["sp"], "channels": []},
]
MIX_FRAMES = [(i * 0.25, {"osc1": {"0": 0.1 * i}}) for i in range(8)]


if __name__ == "__main__":
    ok = True
    ok &= run_parity("ascii", ASCII, ASCII_FRAMES)
    ok &= run_parity("mix", MIX, MIX_FRAMES)
    print("\nALL PASS" if ok else "\nFAILURES")
    sys.exit(0 if ok else 1)
