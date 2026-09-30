"""`python -m tdhost.serve <artifact>` — the host co-process the runtime spawns.

Speaks the framed protocol of runtime_rs/src/host.rs over stdin/stdout:
    message = u32 LE header length | header JSON | blobs (header["blobs"] = sizes)

Commands (header["cmd"]):
    init       {"init": {"monitors": [...], "sizes": {top: [w, h]}}}   -> {"ok": true}
    frame      {"t", "frame", "sizes": {...}, "readbacks": [{"path","w","h"}...]}
               + one float32 RGBA blob per readback                     -> values
    frame_end  {}                                                       -> {"readback": [paths]}
    exit       {}                                                       -> {"ok": true}

The project's own print()/debug() output goes to stderr (the runtime's log);
stdout carries only protocol frames.

TOXC_HOST_PROFILE=<seconds> profiles the per-frame Python (cProfile over the
frame + frame_end callbacks and the reply packing) and logs the hottest
functions every <seconds> — where host:frame's time goes on a device.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
import traceback

import numpy as np  # BLAS threads: see tdhost/__init__.py


def _out_stream():
    # Take the real stdout for the protocol, point sys.stdout at stderr.
    fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(fd, "wb", buffering=0)


def _bytes_view(b):
    if isinstance(b, np.ndarray):
        return memoryview(np.ascontiguousarray(b).reshape(-1).view(np.uint8))
    return memoryview(b).cast("B")


class Wire:
    def __init__(self):
        self.out = _out_stream()
        self.inp = sys.stdin.buffer

    def _read_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.inp.read(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return bytes(buf)

    def read(self):
        (n,) = struct.unpack("<I", self._read_exact(4))
        head = json.loads(self._read_exact(n))
        blobs = [self._read_exact(int(k)) for k in head.get("blobs", [])]
        return head, blobs

    def write(self, head, blobs=()):
        blobs = [_bytes_view(b) for b in blobs]
        head["blobs"] = [b.nbytes for b in blobs]
        hb = json.dumps(head).encode()
        # header + small blobs in one write; big blobs (camera frames, ~MBs)
        # straight from their buffers rather than copied into one joined bytes
        small = [struct.pack("<I", len(hb)), hb]
        big = []
        for b in blobs:
            (big if big or b.nbytes > 65536 else small).append(b)
        self._write_all(memoryview(b"".join(small)))
        for b in big:
            self._write_all(b)

    def _write_all(self, mv):
        while mv.nbytes:
            n = self.out.write(mv)
            mv = mv[n:]


class _Profile:
    """cProfile the per-frame work; print a summary every `every` seconds."""

    def __init__(self, every):
        import cProfile

        self.every = max(1.0, every)
        self.prof = cProfile.Profile()
        self._reset()

    def _reset(self):
        self.prof.clear()
        self.t0, self.frames = time.monotonic(), 0
        self.spent = {"frame": 0.0, "pack": 0.0, "frame_end": 0.0}

    def run(self, part, fn, *args):
        t = time.perf_counter()
        self.prof.enable()
        try:
            return fn(*args)
        finally:
            self.prof.disable()
            self.spent[part] += time.perf_counter() - t

    def tick(self):
        self.frames += 1
        now = time.monotonic()
        if now - self.t0 < self.every:
            return
        import io
        import pstats

        n = max(self.frames, 1)
        parts = " ".join(f"{k}={1000 * v / n:.1f}ms" for k, v in self.spent.items())
        out = io.StringIO()
        st = pstats.Stats(self.prof, stream=out)
        st.sort_stats("tottime").print_stats(25)
        st.sort_stats("cumulative").print_stats(25)
        print(
            f"[host-profile] {n} frames in {now - self.t0:.1f}s | per frame: {parts}\n"
            + out.getvalue(),
            file=sys.stderr,
            flush=True,
        )
        self._reset()


class _NoProfile:
    def run(self, part, fn, *args):
        return fn(*args)

    def tick(self):
        pass


def main(argv):
    artifact = os.path.abspath(argv[1])
    with open(os.path.join(artifact, "schedule.json")) as fh:
        sched = json.load(fh)
    hc = sched["host"]
    with open(os.path.join(artifact, hc["network"])) as fh:
        net = json.load(fh)
    from .host import Host

    project = os.path.join(artifact, hc.get("project_folder", "project"))
    os.makedirs(project, exist_ok=True)
    path_map = {src: project for src in hc.get("path_map", {})}
    wire = Wire()
    host = None
    spec = os.environ.get("TOXC_HOST_PROFILE", "")
    prof = _Profile(float(spec)) if spec else _NoProfile()
    while True:
        try:
            head, blobs = wire.read()
        except EOFError:
            break
        cmd = head.get("cmd")
        try:
            if cmd == "init":
                init = head.get("init", {})
                os.chdir(project)
                sys.path.insert(0, project)
                host = Host(
                    net,
                    project,
                    project_name=hc.get("project_name", "project"),
                    monitors=init.get("monitors"),
                    path_map=path_map,
                    jit=(hc.get("python") or {}).get("jit"),
                )
                host.native_xf = "xf" in (init.get("caps") or [])
                _rewrite_paths(host, path_map)
                host.bind(sched["bindings"])
                host.set_top_sizes(init.get("sizes", {}))
                _apply_overrides(host, os.environ.get("TOXC_SET", ""))
                host.start()
                wire.write(
                    {
                        "ok": True,
                        "floats": len(sched["bindings"]["pars"]) + len(sched["bindings"]["flags"]),
                    }
                )
            elif cmd == "frame":
                if head.get("sizes"):
                    host.set_top_sizes(head["sizes"])
                rb = {}
                for spec, blob in zip(head.get("readbacks", []), blobs):
                    a = np.frombuffer(blob, np.float32).reshape(int(spec["h"]), int(spec["w"]), 4)
                    rb[spec["path"]] = a
                out = prof.run(
                    "frame", host.frame, float(head["t"]), rb, int(head.get("frame", 0)) or None
                )
                reply, oblobs = prof.run("pack", _pack, out)
                reply["quit"] = bool(host.quit_requested)
                wire.write(reply, oblobs)
            elif cmd == "frame_end":
                wire.write({"readback": prof.run("frame_end", host.frame_end)})
                prof.tick()
            elif cmd == "exit":
                if host is not None:
                    host.exit()
                wire.write({"ok": True})
                break
            else:
                wire.write({"error": f"unknown command {cmd!r}"})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            wire.write({"error": f"{type(e).__name__}: {e}", "ok": False})


def _apply_overrides(host, spec):
    """TOXC_SET="/project1/rig:Showmode=in;/project1/light1:dimmer=2" — pin
    parameters at startup (tests, on-site overrides without re-deploying)."""
    for item in filter(None, (x.strip() for x in spec.split(";"))):
        try:
            target, _, assign = item.partition(":")
            name, _, value = assign.partition("=")
            o = host.ops.get(target)
            p = o.par._get(name)
            try:
                v = float(value)
            except ValueError:
                v = value
            p.val = v
            host.log(f"override {target}:{name} = {v!r}")
        except Exception as e:  # noqa: BLE001
            host.log(f"bad TOXC_SET entry {item!r}: {e}")


def _rewrite_paths(host, path_map):
    """Parameters baked with the authoring machine's paths (file-synced DATs,
    movie files...) are rewritten to the deployed project folder."""
    if not path_map:
        return
    for o in host.ops.values():
        for p in object.__getattribute__(o.par, "_pars").values():
            if isinstance(p._val, str):
                for src, dst in path_map.items():
                    if p._val.startswith(src):
                        p._val = dst + p._val[len(src) :]


def _pack(out):
    blobs = []

    def add(arr):
        blobs.append(np.ascontiguousarray(arr))  # sent from its buffer, no copy
        return len(blobs) - 1

    reply = {
        "floats": add(np.asarray(out["floats"], np.float64)),
        "mats": add(np.asarray(out["mats"], np.float64)),
        "tops": {},
        "chops": {},
        "sops": {},
    }
    xf = out.get("xf")
    if xf is not None:
        reply["xf"] = {"idx": add(xf["idx"]), "m": add(xf["m"])}
        for k in ("parent", "mat_nodes"):
            if k in xf:
                reply["xf"][k] = xf[k]
    for path, a in out["tops"].items():
        dt = "u8" if a.dtype == np.uint8 else "f32"
        if dt == "f32" and a.dtype != np.float32:
            a = a.astype(np.float32)
        reply["tops"][path] = {
            "w": int(a.shape[1]),
            "h": int(a.shape[0]),
            "dtype": dt,
            "blob": add(a),
        }
    for path, chans in out["chops"].items():
        n = int(chans.pop("__n__", 1))
        reply["chops"][path] = {
            "n": n,
            "chans": {c: add(np.asarray(v, np.float32)) for c, v in chans.items()},
        }
    for path, m in out["sops"].items():
        reply["sops"][path] = {
            "n": int(len(m["pos"])),
            "count": int(len(m["idx"])),
            "pos": add(m["pos"]),
            "nrm": add(m["nrm"]),
            "col": add(m["col"]),
            "uv": add(m["uv"]),
            "idx": add(m["idx"]),
        }
    return reply, blobs


if __name__ == "__main__":
    main(sys.argv)
