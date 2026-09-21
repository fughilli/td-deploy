"""
Emit a compiled artifact from a lowered plan — the ABI the Rust runtime loads.

Layout (a directory):
  schedule.json      ordered steps (source/shader/passthrough/feedback), refs to
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
    expr_cache: dict[str, tuple] = {}  # expr string -> (fn name, input names) (dedup)
    steps_json = []
    coverage = {"interpreted_exprs": []}

    def add_expr(expr: str):
        if expr in expr_cache:
            return expr_cache[expr]  # (fn, inputs) — keep the input list!
        mlir, info = transpile(expr, fname=f"expr{len(expr_funcs)}")
        if mlir is None:
            return None, info  # unsupported -> Rust/py fallback
        fn = f"expr{len(expr_funcs)}"
        expr_funcs.append(mlir)
        expr_cache[expr] = (fn, info)
        return fn, info

    def _time_uniforms(st) -> dict:
        """Per-frame uniforms: compiled to a native expr fn where possible, else
        carried as an interpreted string for the runtime's fasteval path."""
        tus = {}
        for n, spec in st.time_uniforms.items():
            expr, mul = spec["expr"], spec.get("mul", 1.0)
            fn, info = add_expr(str(expr))
            if fn:
                entry = {"fn": fn, "inputs": info, "mul": mul}
            else:
                entry = {"interpreted": str(expr), "mul": mul}
                coverage["interpreted_exprs"].append(str(expr))
            # Optional periodic wrap (radians, for rotation) applied in f64 by the
            # runtime before the f32 uniform upload — see lowering/lower.py.
            mod = spec.get("mod")
            if mod is not None:
                entry["mod"] = mod
            tus[n] = entry
        return tus

    for st in plan.steps:
        sid = _sid(st.node_id)
        j = {
            "id": st.node_id,
            "op": st.op,
            "kind": st.kind,
            "w": st.target["w"],
            "h": st.target["h"],
            "inputs": list(st.inputs),
        }

        if st.kind == "source":
            path = st.params.get("path")
            dst = os.path.join(outdir, "assets", f"{sid}.png")
            if path and os.path.isfile(path):
                from runtime.video import is_video

                if is_video(path):
                    from PIL import Image

                    from runtime.video import VideoSource

                    fr = VideoSource(path).frame_at(0.0)
                    Image.fromarray(fr, "RGBA").save(dst)  # first frame (video: TODO stream)
                else:
                    from PIL import Image

                    Image.open(path).convert("RGBA").save(dst)
                j["source"] = {"type": "image", "path": f"assets/{sid}.png"}
            else:
                j["source"] = {"type": "testcard", "w": st.target["w"], "h": st.target["h"]}

        elif st.kind == "render3d":
            # Geometry is baked like any other asset: interleaved vertices and a
            # u32 index buffer, both little-endian, so the runtime can upload them
            # straight into a VBO/EBO with no parsing on device.
            import struct

            from importer.wavefront import parse_obj

            os.makedirs(os.path.join(outdir, "meshes"), exist_ok=True)
            mp = st.mesh_path
            if mp and os.path.isfile(mp):
                with open(mp) as fh:
                    mesh = parse_obj(fh.read())
            else:
                mesh = None
            if mesh is None or not mesh.indices:
                j["mesh"] = None
                info_note = f"{st.node_id}: mesh {mp!r} unreadable — nothing to draw"
                coverage.setdefault("mesh_warnings", []).append(info_note)
            else:
                vtx = bytearray()
                for i in range(mesh.vertex_count):
                    px, py, pz = mesh.positions[i]
                    nx, ny, nz = mesh.normals[i]
                    u, v = mesh.uvs[i]
                    vtx += struct.pack("<8f", px, py, pz, nx, ny, nz, u, v)
                # GLES 2.0 only guarantees 16-bit indices — UNSIGNED_INT needs
                # OES_element_index_uint, which is not universal on VC4-class
                # hardware. Use u16 whenever the mesh fits.
                if mesh.vertex_count <= 0xFFFF:
                    idx = struct.pack(f"<{len(mesh.indices)}H", *mesh.indices)
                    itype = "u16"
                else:
                    idx = struct.pack(f"<{len(mesh.indices)}I", *mesh.indices)
                    itype = "u32"
                with open(os.path.join(outdir, "meshes", f"{sid}.vtx"), "wb") as fh:
                    fh.write(vtx)
                with open(os.path.join(outdir, "meshes", f"{sid}.idx"), "wb") as fh:
                    fh.write(idx)
                j["mesh"] = {
                    "vtx": f"meshes/{sid}.vtx",
                    "idx": f"meshes/{sid}.idx",
                    "vertices": mesh.vertex_count,
                    "indices": len(mesh.indices),
                    "stride": 32,
                    "index_type": itype,
                }
            # Material texture, converted like an image source.
            tp = st.texture_path
            if tp and os.path.isfile(tp):
                from PIL import Image

                dst = os.path.join(outdir, "assets", f"{sid}_tex.png")
                Image.open(tp).convert("RGBA").save(dst)
                j["texture"] = f"assets/{sid}_tex.png"
            else:
                j["texture"] = None
            with open(os.path.join(outdir, "shaders", f"{sid}.vert"), "w") as f:
                f.write(st.vertex or "")
            with open(os.path.join(outdir, "shaders", f"{sid}.frag"), "w") as f:
                f.write(st.fragment or "")
            j["vert"] = f"shaders/{sid}.vert"
            j["frag"] = f"shaders/{sid}.frag"
            j["uniforms"] = {n: {"type": t, "value": v} for n, (t, v) in st.uniforms.items()}
            j["time_uniforms"] = _time_uniforms(st)

        elif st.kind == "feedback":
            # A persistent buffer: no shader of its own. `feedback_from` names the
            # step whose previous frame it serves; `inputs` (if any) seed it.
            j["feedback_from"] = st.feedback_from

        elif st.kind == "shader":
            with open(os.path.join(outdir, "shaders", f"{sid}.vert"), "w") as f:
                f.write(st.vertex or "")
            with open(os.path.join(outdir, "shaders", f"{sid}.frag"), "w") as f:
                f.write(st.fragment or "")
            j["vert"] = f"shaders/{sid}.vert"
            j["frag"] = f"shaders/{sid}.frag"
            j["sampler_array"] = st.sampler_array
            j["uniforms"] = {n: {"type": t, "value": v} for n, (t, v) in st.uniforms.items()}
            j["time_uniforms"] = _time_uniforms(st)

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
        except Exception as e:  # noqa: BLE001 (parity fallback)
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
