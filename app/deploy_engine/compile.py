"""Compile a .toe/.tox to an OPTIMIZED artifact directory (schedule.json + shaders
+ assets + exprs.mlir + chops.mlir), reusing the existing toxc pipeline. Mirrors
cli.py's `--emit-artifact` path exactly; the arch-specific codegen of the .mlir ->
.so and the GLES translation happen later in finish.py.
"""
from __future__ import annotations

import os
import tempfile
import urllib.parse

from . import _paths
from .expand import expand, http_get_bytes
from .progress import Progress

_paths.ensure_on_path()


def _fetch_host_assets(g, bridge: str | None, assetdir: str, progress: Progress) -> None:
    """Ensure each image_in has a locally-readable file + native size. Local files
    (production) are used as-is; missing ones are pulled from the dev bridge."""
    os.makedirs(assetdir, exist_ok=True)
    from PIL import Image
    for n in g.nodes.values():
        p = n.params.get("path") if n.op == "image_in" else None
        if not p:
            continue
        local = p
        if not os.path.isfile(p) and bridge:
            try:
                url = f"http://{bridge}/readfile?path={urllib.parse.quote(p)}"
                data = http_get_bytes(url)
                local = os.path.join(assetdir, os.path.basename(p))
                with open(local, "wb") as fh:
                    fh.write(data)
                n.params["path"] = local
                progress.log(f"asset {os.path.basename(p)} ({len(data)}B)")
            except Exception as e:  # noqa: BLE001
                progress.log(f"asset {p} unavailable ({e}); testcard substitute")
                continue
        try:
            from runtime import video
            w, h = (video.probe_size(local) if video.is_video(local)
                    else Image.open(local).size)
            n.params["w"], n.params["h"] = int(w), int(h)
        except Exception:  # noqa: BLE001
            pass


def compile_toe(toe_path: str, outdir: str, *, target: str = "gles2", res: int = 256,
                set_file: list[str] | None = None, bridge: str | None = None,
                keep_expanded: str | None = None, progress: Progress = Progress()) -> dict:
    """Expand + import + optimize + lower + emit into `outdir`. Returns emit info."""
    from ir.graph import Graph
    from passes.optimize import optimize
    from lowering.lower import lower

    os.makedirs(outdir, exist_ok=True)
    workdir = keep_expanded or tempfile.mkdtemp(prefix="toxc_import_")

    # expand + import
    if toe_path.endswith(".json"):
        progress.phase("expand", 1.0, "IR json (no expand)")
        g = Graph.load(toe_path)
    else:
        progress.phase("expand", 0.0, os.path.basename(toe_path))
        dirroot = expand(toe_path, workdir, bridge=bridge, progress=progress)
        progress.phase("import", 0.0, os.path.basename(dirroot))
        from importer.from_toeexpand import import_dir
        g = import_dir(dirroot).graph

    # --set-file overrides (match by full id or trailing name)
    for spec in (set_file or []):
        node, _, fpath = spec.partition("=")
        matches = [nid for nid in g.nodes if nid == node or nid.endswith("/" + node)]
        for nid in matches:
            g.nodes[nid].op = "image_in"
            g.nodes[nid].params["path"] = fpath
            progress.log(f"set-file {nid} <- {fpath}")

    _fetch_host_assets(g, bridge, os.path.join(workdir, "assets"), progress)

    progress.phase("optimize", 0.0, f"{len(g.nodes)} nodes")
    for line in optimize(g, out_res=res):
        progress.log(line)

    progress.phase("lower", 0.0, target)
    plan = lower(g, target=target)
    progress.log(f"plan: {len(plan.steps)} steps, target={plan.target}")

    progress.phase("emit", 0.0, outdir)
    from emit_artifact import emit  # from compiler/ (on path)
    info = emit(plan, g, outdir)
    progress.log(f"artifact: {info}")
    return info
