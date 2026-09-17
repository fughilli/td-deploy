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


def _resolve_local_asset(p: str, toe_path: str) -> str | None:
    """Find an image_in file on disk. TouchDesigner stores movie paths RELATIVE to
    the project dir — usually the .toe's own dir or its parent — NOT the app's CWD,
    so a param like `toxc/Banana.tif` won't resolve against CWD. Try those bases
    (and the bare basename in each); return the first that exists, else None."""
    if os.path.isfile(p):
        return os.path.abspath(p)
    toedir = os.path.dirname(os.path.abspath(toe_path)) if toe_path else os.getcwd()
    bases = [os.getcwd(), toedir, os.path.dirname(toedir)]
    seen = set()
    for b in bases:
        for cand in (os.path.join(b, p), os.path.join(b, os.path.basename(p))):
            cand = os.path.abspath(cand)
            if cand not in seen and os.path.isfile(cand):
                return cand
            seen.add(cand)
    return None


def _fetch_host_assets(
    g, bridge: str | None, assetdir: str, toe_path: str, progress: Progress
) -> None:
    """Ensure each image_in has a locally-readable file + native size. Resolve the
    path locally first (relative to the .toe dir, not CWD); only pull from the dev
    bridge if it can't be found on this machine."""
    os.makedirs(assetdir, exist_ok=True)
    from PIL import Image

    for n in g.nodes.values():
        p = n.params.get("path") if n.op == "image_in" else None
        if not p:
            continue
        local = _resolve_local_asset(p, toe_path)
        if local:
            n.params["path"] = local  # normalize so emit reads the resolved file
        elif bridge:
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
        else:
            progress.log(
                f"asset {p} not found (tried CWD + .toe dir + parent) and no bridge;"
                " testcard substitute"
            )
            continue
        try:
            from runtime import video

            w, h = video.probe_size(local) if video.is_video(local) else Image.open(local).size
            n.params["w"], n.params["h"] = int(w), int(h)
        except Exception:  # noqa: BLE001
            pass


def compile_toe(
    toe_path: str,
    outdir: str,
    *,
    target: str = "gles2",
    res: int = 256,
    set_file: list[str] | None = None,
    bridge: str | None = None,
    keep_expanded: str | None = None,
    strict_unsupported: bool = True,
    magic_chop: bool = False,
    progress: Progress = Progress(),
) -> dict:
    """Expand + import + optimize + lower + emit into `outdir`. Returns emit info,
    including what the engine couldn't handle natively:
      `unsupported`       — unsupported render-path TOP operators;
      `unsupported_chops` — unsupported CHOP types feeding parameter expressions;
      `magic_chops`       — CHOPs replaced with sinusoids (when `magic_chop`).

    When the render path uses an unsupported TOP operator, `strict_unsupported` (default)
    raises `UnsupportedOperatorError`; set it False for lenient "warn but continue" — the
    operator degrades to a placeholder (identity passthrough, or a blank testcard source).
    Unsupported CHOPs never fail the build: they resolve to 0, or — with `magic_chop` —
    are driven by random sinusoids so the piece still animates. Any of these are surfaced
    (logged + returned) so the UI can offer a 'fix and file' prompt."""
    from ir.graph import Graph
    from lowering.lower import lower
    from passes import magic_chop as magic
    from passes.optimize import optimize

    os.makedirs(outdir, exist_ok=True)
    workdir = keep_expanded or tempfile.mkdtemp(prefix="toxc_import_")

    unsupported: list[str] = []

    # expand + import
    if toe_path.endswith(".json"):
        progress.phase("expand", 1.0, "IR json (no expand)")
        g = Graph.load(toe_path)
    else:
        progress.phase("expand", 0.0, os.path.basename(toe_path))
        dirroot = expand(toe_path, workdir, bridge=bridge, progress=progress)
        progress.phase("import", 0.0, os.path.basename(dirroot))
        from importer.from_toeexpand import import_dir

        result = import_dir(dirroot)
        g = result.graph
        unsupported = list(result.unsupported)
        for line in result.coverage:
            progress.log(line)

    # Unsupported CHOPs (LFO/noise/audio/... driving parameter exprs): never fatal — they
    # resolve to a dead 0, or get random sinusoids in magic-chop mode. Detect BEFORE the
    # magic rewrite (which inlines + drops them). Reported for the fix-it prompt either way.
    unsupported_chop_defs = magic.unsupported_chops(g)
    chop_types = sorted({f"CHOP:{c.get('type', '?')}" for c in unsupported_chop_defs})
    magic_chops: list[dict] = []
    if unsupported_chop_defs:
        if magic_chop:
            magic_chops = magic.apply(g)
            for r in magic_chops:
                progress.log(
                    f"magic-chop: unsupported CHOP {r['name']} ({r['type']}) driven by "
                    f"random sinusoids on channel(s) {', '.join(r['channels'])}"
                )
        else:
            names = ", ".join(f"{c['name']} ({c.get('type', '?')})" for c in unsupported_chop_defs)
            progress.log(
                f"WARNING: unsupported CHOP(s) {names} resolve to 0 — enable Magic Chop to "
                "animate them, or add support (fix-it)"
            )

    if unsupported:
        # An unmapped render-path operator degrades to a placeholder, which produces
        # wrong output. By default fail loudly so the UI can offer a fix-it prompt; in
        # lenient mode, warn and ship the placeholder version.
        if strict_unsupported:
            from .fixit import UnsupportedOperatorError

            raise UnsupportedOperatorError(unsupported)
        progress.log(
            "WARNING: unsupported operators replaced with placeholders "
            f"(deploying anyway): {', '.join(unsupported)}"
        )

    # --set-file overrides (match by full id or trailing name)
    for spec in set_file or []:
        node, _, fpath = spec.partition("=")
        matches = [nid for nid in g.nodes if nid == node or nid.endswith("/" + node)]
        for nid in matches:
            g.nodes[nid].op = "image_in"
            g.nodes[nid].params["path"] = fpath
            progress.log(f"set-file {nid} <- {fpath}")

    _fetch_host_assets(g, bridge, os.path.join(workdir, "assets"), toe_path, progress)

    progress.phase("optimize", 0.0, f"{len(g.nodes)} nodes")
    for line in optimize(g, out_res=res):
        progress.log(line)

    progress.phase("lower", 0.0, target)
    plan = lower(g, target=target)
    progress.log(f"plan: {len(plan.steps)} steps, target={plan.target}")

    progress.phase("emit", 0.0, outdir)
    from emit_artifact import emit  # from compiler/ (on path)

    info = emit(plan, g, outdir)
    if isinstance(info, dict):
        info["unsupported"] = unsupported
        info["unsupported_chops"] = chop_types
        info["magic_chops"] = magic_chops
    progress.log(f"artifact: {info}")
    return info
