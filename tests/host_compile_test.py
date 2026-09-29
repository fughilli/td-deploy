"""compiler/host_compile.py + pyhost/tdhost/serve.py — the Python-host path end to end
without TouchDesigner or a GPU.

A small network (Window COMP -> Fit -> GLSL TOP with an expression-driven uniform,
a Script TOP only Python reads, an Execute DAT, an unsupported TOP) is compiled;
the schedule's shape is checked, then the emitted artifact's host co-process is
driven over its framed stdin/stdout protocol exactly as runtime_rs/src/host.rs does.
"""

import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "compiler"))
sys.path.insert(0, os.path.join(ROOT, "pyhost"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import host_compile as HC  # noqa: E402
from toetree import ToeTree  # noqa: E402

FLOAT = 772804865


def _project(tmp):
    proj = os.path.join(tmp, "proj")
    os.makedirs(os.path.join(proj, "scripts"))
    with open(os.path.join(proj, "scripts", "core.py"), "w") as fh:
        fh.write("GAIN = 3.0\n")
    with open(os.path.join(proj, "scripts", "ignored.txt"), "w") as fh:
        fh.write("not shipped\n")
    with open(os.path.join(proj, "requirements.txt"), "w") as fh:
        fh.write("numpy\n")
    with open(os.path.join(proj, "td-deploy.json"), "w") as fh:
        json.dump({"files": ["scripts/*.py"], "python": {"requirements": "requirements.txt"}}, fh)

    t = ToeTree(tmp)
    t.op(
        "project1/rig",
        "COMP:base",
        params={"Gain": "1"},
        custom=[f'{FLOAT} Gain Gain 1 1 0 0 1 1 10 2 1 "" "" Main 0'],
    )
    t.text(
        "project1/rig/exec",
        "def onFrameStart(frame):\n"
        "    op('/project1/rig').par.Gain = frame * 0.5\n"
        "    import core\n"
        "    op('/project1/rig').store('gain', core.GAIN)\n"
        "def onFrameEnd(frame):\n"
        "    op('/project1/matte').numpyArray(delayed=True)\n",
        kind="DAT:execute",
        params={"framestart": "on", "frameend": "on"},
    )
    t.text(
        "project1/pix",
        "out vec4 fragColor;\nuniform vec4 uGain;\nvoid main(){ fragColor = uGain; }\n",
    )
    t.op(
        "project1/glsl1",
        "TOP:glsl",
        params={
            "pixeldat": "pix",
            "resolutionw": "64",
            "resolutionh": "32",
            "vec0name": "uGain",
            "vec0valuex": (17, "0", "op('rig').par.Gain * 2"),
            "vec0valuey": "0.25",
        },
    )
    t.op("project1/weird1", "TOP:kinect", inputs=["glsl1"])
    t.op("project1/fit1", "TOP:fit", inputs=["weird1"], params={"fit": "fitbest"})
    t.op("project1/matte", "TOP:script", params={"callbacks": "pix"})
    t.op("project1/window1", "COMP:window", params={"winop": "fit1", "borders": "off"})
    return t.dir, proj


def _msg(head, blobs=()):
    head = dict(head, blobs=[len(b) for b in blobs])
    hb = json.dumps(head).encode()
    return struct.pack("<I", len(hb)) + hb + b"".join(blobs)


def _read(stream):
    (n,) = struct.unpack("<I", stream.read(4))
    head = json.loads(stream.read(n))
    blobs = [stream.read(k) for k in head.get("blobs", [])]
    return head, blobs


class HostCompileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.dirroot, cls.proj = _project(cls.tmp)
        cls.out = os.path.join(cls.tmp, "art")
        cls.res = HC.compile_host(
            cls.dirroot, cls.out, project_dir=cls.proj, project_name="test", log=lambda *_: None
        )
        with open(os.path.join(cls.out, "schedule.json")) as fh:
            cls.sched = json.load(fh)

    def test_needs_python_host(self):
        self.assertTrue(HC.needs_python_host(self.dirroot))
        t = ToeTree(tempfile.mkdtemp())
        t.op("project1/noise1", "TOP:noise", flags="display on")
        self.assertFalse(HC.needs_python_host(t.dir))

    def test_schedule_graph(self):
        s = self.sched
        self.assertEqual(s["format"], "toxc-host/1")
        self.assertEqual(s["output"], "/project1/fit1")  # the Window COMP's operator
        self.assertFalse(s["window"]["borders"])
        nodes = {n["id"]: n for n in s["nodes"]}
        self.assertEqual(nodes["/project1/fit1"]["kind"], "fit")
        # unsupported ops pass their input through, and are reported
        self.assertEqual(
            nodes["/project1/weird1"],
            {**nodes["/project1/weird1"], "kind": "alias", "of": "/project1/glsl1"},
        )
        self.assertIn("TOP:kinect", self.res["unsupported"])
        # a Script TOP only Python reads (numpyArray) is cooked on demand
        self.assertTrue(nodes["/project1/matte"].get("on_demand"))
        # dependencies come before their consumers
        ids = [n["id"] for n in s["nodes"]]
        self.assertLess(ids.index("/project1/glsl1"), ids.index("/project1/fit1"))

    def test_glsl_uniform_bindings(self):
        g = next(n for n in self.sched["nodes"] if n["id"] == "/project1/glsl1")
        # every authored parameter is a per-frame binding: Python may set any of
        # them at runtime; unauthored components keep their default
        x, y, z, w = g["uniforms"]["uGain"]
        self.assertEqual(x[0], "b")
        self.assertEqual(y[0], "b")
        self.assertEqual((z, w), (0.0, 0.0))
        with open(os.path.join(self.out, g["frag"])) as fh:
            frag = fh.read()
        self.assertIn("uniform vec4 uGain", frag)
        self.assertIn("sTD2DInputs", frag)  # TD's GLSL TOP prelude

    def test_project_files_and_host_package(self):
        self.assertTrue(os.path.isfile(os.path.join(self.out, "project", "scripts", "core.py")))
        self.assertFalse(
            os.path.exists(os.path.join(self.out, "project", "scripts", "ignored.txt"))
        )
        self.assertTrue(os.path.isfile(os.path.join(self.out, "project", "requirements.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "host", "tdhost", "serve.py")))
        self.assertEqual(self.sched["host"]["python"], {"requirements": "requirements.txt"})

    def test_serve_protocol_round_trip(self):
        # the artifact's own host package first, then this interpreter's paths (numpy
        # — under Bazel those live on sys.path, not in the environment)
        env = dict(
            os.environ, PYTHONPATH=os.pathsep.join([os.path.join(self.out, "host")] + sys.path)
        )
        p = subprocess.Popen(
            [sys.executable, "-m", "tdhost.serve", self.out],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            p.stdin.write(
                _msg(
                    {"cmd": "init", "init": {"monitors": [], "sizes": {"/project1/matte": [8, 4]}}}
                )
            )
            p.stdin.flush()
            try:
                head, _ = _read(p.stdout)
            except struct.error:
                self.fail("host co-process died:\n" + p.stderr.read().decode())
            self.assertTrue(head.get("ok"), head)
            n_floats = head["floats"]

            g = next(n for n in self.sched["nodes"] if n["id"] == "/project1/glsl1")
            bx = g["uniforms"]["uGain"][0][1]
            for frame in (1, 4):
                p.stdin.write(
                    _msg({"cmd": "frame", "t": frame / 60.0, "frame": frame, "readbacks": []})
                )
                p.stdin.flush()
                head, blobs = _read(p.stdout)
                self.assertNotIn("error", head)
                vals = np.frombuffer(blobs[head["floats"]], np.float64)
                self.assertEqual(len(vals), n_floats)
                # onFrameStart set Gain = frame * 0.5; the uniform is Gain * 2
                self.assertAlmostEqual(vals[bx], frame * 1.0)
                p.stdin.write(_msg({"cmd": "frame_end"}))
                p.stdin.flush()
                head, _ = _read(p.stdout)
                self.assertEqual(head["readback"], ["/project1/matte"])

            p.stdin.write(_msg({"cmd": "exit"}))
            p.stdin.flush()
            head, _ = _read(p.stdout)
            self.assertTrue(head.get("ok"))
        finally:
            p.stdin.close()
            p.wait(timeout=10)
            p.stdout.close()
            err = p.stderr.read().decode()
            p.stderr.close()
        self.assertEqual(p.returncode, 0, err)


if __name__ == "__main__":
    unittest.main()
