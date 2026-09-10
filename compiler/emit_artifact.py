"""
Emit a compiled artifact from a lowered plan — the ABI the Rust runtime loads.

Layout (a directory):
  schedule.json      ordered steps (source/shader/passthrough), refs to shaders,
                     assets, per-frame expr functions, output node, services
  shaders/<id>.vert  fullscreen vertex shader per shader step
  shaders/<id>.frag  fragment shader (GLSL; SPIR-V is a later refinement)
  assets/<name>.png  source images (movies -> first frame for now)
  exprs.mlir         transpiled param-expression functions (compile with
                     compiler/build_exprs.sh -> exprs/libexprs.so)
  services.json      OSC/MIDI manifest

This runs in the Python compiler env (PIL/PyAV for assets; expr transpile is pure
Python and only emits MLIR text — the .so is built separately in the MLIR shell).
"""
from __future__ import annotations
import json
import os

from expr_transpile import transpile


def _sid(node_id: str) -> str:
    return node_id.replace("/", "_")


def emit(plan, graph, outdir: str) -> dict:
    os.makedirs(os.path.join(outdir, "shaders"), exist_ok=True)
    os.makedirs(os.path.join(outdir, "assets"), exist_ok=True)

    expr_funcs: list[str] = []
    expr_cache: dict[str, tuple] = {}   # expr string -> (fn name, input names) (dedup)
    steps_json = []
    coverage = {"interpreted_exprs": []}

    def add_expr(expr: str):
        if expr in expr_cache:
            return expr_cache[expr]           # (fn, inputs) — keep the input list!
        mlir, info = transpile(expr, fname=f"expr{len(expr_funcs)}")
        if mlir is None:
            return None, info                 # unsupported -> Rust/py fallback
        fn = f"expr{len(expr_funcs)}"
        expr_funcs.append(mlir)
        expr_cache[expr] = (fn, info)
        return fn, info

    for st in plan.steps:
        sid = _sid(st.node_id)
        j = {"id": st.node_id, "op": st.op, "kind": st.kind,
             "w": st.target["w"], "h": st.target["h"], "inputs": list(st.inputs)}

        if st.kind == "source":
            path = st.params.get("path")
            dst = os.path.join(outdir, "assets", f"{sid}.png")
            if path and os.path.isfile(path):
                from runtime.video import is_video
                if is_video(path):
                    from runtime.video import VideoSource
                    from PIL import Image
                    fr = VideoSource(path).frame_at(0.0)
                    Image.fromarray(fr, "RGBA").save(dst)   # first frame (video: TODO stream)
                else:
                    from PIL import Image
                    Image.open(path).convert("RGBA").save(dst)
                j["source"] = {"type": "image", "path": f"assets/{sid}.png"}
            else:
                j["source"] = {"type": "testcard", "w": st.target["w"], "h": st.target["h"]}

        elif st.kind == "shader":
            with open(os.path.join(outdir, "shaders", f"{sid}.vert"), "w") as f:
                f.write(st.vertex or "")
            with open(os.path.join(outdir, "shaders", f"{sid}.frag"), "w") as f:
                f.write(st.fragment or "")
            j["vert"] = f"shaders/{sid}.vert"
            j["frag"] = f"shaders/{sid}.frag"
            j["sampler_array"] = st.sampler_array
            j["uniforms"] = {n: {"type": t, "value": v} for n, (t, v) in st.uniforms.items()}
            tus = {}
            for n, spec in st.time_uniforms.items():
                expr, mul = spec["expr"], spec.get("mul", 1.0)
                fn, info = add_expr(str(expr))
                if fn:
                    tus[n] = {"fn": fn, "inputs": info, "mul": mul}
                else:
                    tus[n] = {"interpreted": str(expr), "mul": mul}
                    coverage["interpreted_exprs"].append(str(expr))
            j["time_uniforms"] = tus

        steps_json.append(j)

    with open(os.path.join(outdir, "exprs.mlir"), "w") as f:
        f.write("\n".join(expr_funcs) + ("\n" if expr_funcs else ""))
    with open(os.path.join(outdir, "services.json"), "w") as f:
        json.dump(getattr(graph, "services", []), f, indent=2)

    chops = list(getattr(graph, "chops", []))
    # Fuse + lower the whole CHOP DAG to one native kernel (P1). Emit chops.mlir
    # (compiled to chops/libchops.so in-image, arch-correct); the runtime calls
    # `chops_v` instead of the fasteval loop. Falls back to fasteval (keeps the
    # `chops` list) if any node is unlowerable.
    chops_lib = None
    chops_abi = None
    if chops:
        try:
            from chop_lower import lower as _chop_lower
            mlir, abi = _chop_lower(chops)
            with open(os.path.join(outdir, "chops.mlir"), "w") as f:
                f.write("module {\n" + mlir + "}\n")
            chops_lib = "chops/libchops.so"
            chops_abi = abi
        except Exception as e:                       # noqa: BLE001 (parity fallback)
            coverage["chop_lower_fallback"] = str(e)

    schedule = {
        "output": plan.output_id,
        "target": plan.target,
        "steps": steps_json,
        "exprs_lib": "exprs/libexprs.so" if expr_funcs else None,
        "services": "services.json",
        "chops": chops,
        "chops_lib": chops_lib,
        "chops_abi": chops_abi,
    }
    with open(os.path.join(outdir, "schedule.json"), "w") as f:
        json.dump(schedule, f, indent=2)

    return {"steps": len(steps_json), "exprs": len(expr_funcs), "chops": len(chops), **coverage}
