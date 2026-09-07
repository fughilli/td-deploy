"""
toxc end-to-end CLI — the whole shebang from a .tox/.toe (or a toxc IR .json).

    python3 -m cli <project.toe|project.tox|graph.json> [opts]

Flow for .tox/.toe:
    expand (via the Mac host bridge)  ->  import (toeexpand tree -> IR)
    ->  optimize  ->  lower  ->  run (GL)  ->  PNG + coverage report

The .tox lives on the Mac with TouchDesigner; `toeexpand` runs there behind the
host bridge (default http://host.docker.internal:8770). A local file is uploaded
to /expand; a path that only exists on the host uses /expand_local.
"""
from __future__ import annotations
import argparse
import io
import os
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir.graph import Graph            # noqa: E402
from passes.optimize import optimize  # noqa: E402
from lowering.lower import lower       # noqa: E402


def _http_get_bytes(url: str, data: bytes | None = None, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _expand_via_bridge(path: str, host: str, workdir: str) -> str:
    """Return the path to the expanded `*.dir` directory."""
    name = os.path.basename(path)
    base = f"http://{host}"
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            body = fh.read()
        url = f"{base}/expand?name={urllib.parse.quote(name)}&format=tar"
        tgz = _http_get_bytes(url, data=body)
    else:
        # not present locally -> assume it's a host-side path
        url = f"{base}/expand_local?path={urllib.parse.quote(path)}&format=tar"
        tgz = _http_get_bytes(url)
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tf:
        tf.extractall(workdir)
    for dp, dns, _fn in os.walk(workdir):
        for d in dns:
            if d.endswith(".dir"):
                return os.path.join(dp, d)
    raise RuntimeError(f"no *.dir found in expansion of {name}; "
                       f"bridge output: {sorted(os.listdir(workdir))}")


def _fetch_host_assets(g: Graph, host: str, assetdir: str) -> None:
    """Pull image_in assets that live on the host (Mac) into a local dir via the
    bridge /readfile, rewriting node paths. Silent fallback (testcard) on any
    failure — e.g. a bridge that predates /readfile."""
    os.makedirs(assetdir, exist_ok=True)
    from PIL import Image
    for n in g.nodes.values():
        p = n.params.get("path") if n.op == "image_in" else None
        if not p:
            continue
        local = p
        if not os.path.isfile(p):
            try:
                url = f"http://{host}/readfile?path={urllib.parse.quote(p)}"
                data = _http_get_bytes(url)
                local = os.path.join(assetdir, os.path.basename(p))
                with open(local, "wb") as fh:
                    fh.write(data)
                n.params["path"] = local
                print(f"[asset] fetched {p} -> {local} ({len(data)}B)")
            except Exception as e:  # noqa: BLE001
                print(f"[asset] could not fetch {p} ({e}); using testcard substitute")
                continue
        # record native size so the source keeps its real resolution (crop needs it)
        try:
            from runtime import video
            if video.is_video(local):
                w, h = video.probe_size(local)
            else:
                w, h = Image.open(local).size
            n.params["w"], n.params["h"] = int(w), int(h)
        except Exception:  # noqa: BLE001
            pass


def _load_graph(path: str, host: str, keep: str | None) -> tuple[Graph, list[str]]:
    if path.endswith(".json"):
        return Graph.load(path), []
    if path.endswith((".tox", ".toe")):
        from importer.from_toeexpand import import_dir
        workdir = keep or tempfile.mkdtemp(prefix="toxc_import_")
        print(f"[expand] {path} via bridge {host} -> {workdir}")
        dirroot = _expand_via_bridge(path, host, workdir)
        print(f"[import] {dirroot}")
        res = import_dir(dirroot)
        print("[coverage]")
        for c in res.coverage:
            print("  " + c)
        return res.graph, res.coverage
    raise ValueError(f"unsupported input {path!r} (want .tox/.toe/.json)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help=".tox/.toe project or toxc IR .json")
    ap.add_argument("--host", default=os.environ.get("TOXC_HOST", "host.docker.internal:8770"))
    ap.add_argument("--backend", choices=["both", "gl", "cpu"], default="both")
    ap.add_argument("--target", choices=["desktop_gl", "gles", "gles2"], default="desktop_gl")
    ap.add_argument("--out", default="out")
    ap.add_argument("--cwd", default=None, help="resolve a relative input against this dir")
    ap.add_argument("--keep-expanded", default=None, help="keep expansion in this dir")
    ap.add_argument("--stream", action="store_true", help="serve a live MJPEG stream")
    ap.add_argument("--port", type=int, default=8788, help="stream port")
    ap.add_argument("--fps", type=float, default=30.0, help="stream fps cap")
    ap.add_argument("--res", type=int, default=256, help="project output resolution (square)")
    ap.add_argument("--set-file", action="append", default=[], metavar="NODE=PATH",
                    help="override an image_in file (e.g. content that the .tox left empty); "
                         "PATH may be a host path fetched via the bridge")
    ap.add_argument("--emit-artifact", default=None, metavar="DIR",
                    help="compile to a native-runtime artifact directory (schedule.json + "
                         "shaders + assets + exprs.mlir) instead of rendering")
    args = ap.parse_args()

    inp = args.input
    if not os.path.isabs(inp) and args.cwd:
        cand = os.path.join(args.cwd, inp)
        if os.path.exists(cand) or not os.path.exists(inp):
            inp = cand
    os.makedirs(args.out, exist_ok=True)

    g, _cov = _load_graph(inp, args.host, args.keep_expanded)

    # apply --set-file overrides (match by full id or trailing name), then fetch assets
    for spec in args.set_file:
        node, _, fpath = spec.partition("=")
        matches = [nid for nid in g.nodes if nid == node or nid.endswith("/" + node)]
        if not matches:
            print(f"[set-file] no node matches {node!r}; nodes: {list(g.nodes)}")
        for nid in matches:
            g.nodes[nid].op = "image_in"
            g.nodes[nid].params["path"] = fpath
            print(f"[set-file] {nid} <- {fpath}")
    _fetch_host_assets(g, args.host,
                       (args.keep_expanded or tempfile.gettempdir()) + "/toxc_assets")
    print(f"[ir] {len(g.nodes)} nodes, output={g.output!r}")

    print("[passes]")
    for line in optimize(g, out_res=args.res):
        print("  " + line)

    plan = lower(g, target=args.target)
    print(f"[plan] target={plan.target}, {len(plan.steps)} steps"
          + (" (contains GL-only glsl_top)" if plan.has_gl_only_ops() else ""))

    # I/O services (OSC/MIDI in) -> live values for op('..')['..'] param exprs
    from runtime.services import ChopStore, ServiceManager, collect_services
    store = ChopStore()
    specs = collect_services(g)
    if specs:
        print(f"[services] {specs}")
        ServiceManager(specs, store).start()

    if args.emit_artifact:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "compiler"))
        from emit_artifact import emit
        info = emit(plan, g, args.emit_artifact)
        print(f"[artifact] wrote {args.emit_artifact}: {info}")
        print(f"[artifact] next: compiler/build_exprs.sh {args.emit_artifact}  (compiles exprs.mlir)")
        return 0

    if args.stream:
        from runtime.stream_server import serve
        print(f"[stream] starting realtime render loop for {inp}")
        serve(plan, port=args.port, fps=args.fps, chops=store)
        return 0

    import numpy as np
    from PIL import Image
    results = {}
    want_cpu = args.backend in ("cpu", "both") and not plan.has_gl_only_ops()
    if args.backend in ("cpu", "both") and plan.has_gl_only_ops():
        print("[cpu] skipped — GL-only plan; GL is the reference")
    if want_cpu:
        from runtime import backend_cpu
        results["cpu"] = backend_cpu.run(plan)
        Image.fromarray(results["cpu"], "RGBA").save(f"{args.out}/cpu.png")
        print(f"[cpu] wrote {args.out}/cpu.png")
    if args.backend in ("gl", "both"):
        from runtime import backend_gl
        results["gl"] = backend_gl.run(plan)
        Image.fromarray(results["gl"], "RGBA").save(f"{args.out}/gl.png")
        print(f"[gl]  wrote {args.out}/gl.png")

    if "gl" in results and "cpu" in results:
        diff = np.abs(results["gl"].astype(np.int16) - results["cpu"].astype(np.int16))
        print(f"[validate] GL vs CPU: max|Δ|={int(diff.max())} LSB, "
              f"{100.0*(diff==0).mean():.2f}% exact")
    print("[done]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
