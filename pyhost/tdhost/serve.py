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
"""

from __future__ import annotations

import json
import os
import struct
import sys
import traceback

import numpy as np


def _out_stream():
    # Take the real stdout for the protocol, point sys.stdout at stderr.
    fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(fd, "wb", buffering=0)


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
        blobs = [b if isinstance(b, (bytes, bytearray, memoryview)) else bytes(b) for b in blobs]
        head["blobs"] = [len(b) for b in blobs]
        hb = json.dumps(head).encode()
        parts = [struct.pack("<I", len(hb)), hb, *blobs]
        self.out.write(b"".join(bytes(p) for p in parts))


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
                )
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
                out = host.frame(float(head["t"]), rb, int(head.get("frame", 0)) or None)
                reply, oblobs = _pack(out)
                reply["quit"] = bool(host.quit_requested)
                wire.write(reply, oblobs)
            elif cmd == "frame_end":
                wire.write({"readback": host.frame_end()})
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
        blobs.append(np.ascontiguousarray(arr).tobytes())
        return len(blobs) - 1

    reply = {
        "floats": add(np.asarray(out["floats"], np.float64)),
        "mats": add(np.asarray(out["mats"], np.float64)),
        "tops": {},
        "chops": {},
        "sops": {},
    }
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
