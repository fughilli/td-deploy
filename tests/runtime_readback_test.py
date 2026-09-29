"""toxc-runtime + the Python host end to end: numpyArray(delayed=True) readbacks.

A GLSL TOP renders a colour a Python parameter drives (it changes every frame);
an Execute DAT requests its pixels at frame end and logs what arrives at the next
frame start. With TOXC_SYNC_READBACK=1 that is exactly the previous frame's
pixels. The asynchronous (pixel-pack buffer + fence) readback never waits for
the GPU unless reads pile up, so it delivers a frame rendered one to three
frames earlier — never garbage, never a frame from the future.

Needs a built runtime ($TOXC_RUNTIME, else runtime_rs/target/release) and an
EGL/GL driver (Mesa llvmpipe is fine); skipped otherwise.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "compiler"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import host_compile as HC  # noqa: E402
from toetree import ToeTree  # noqa: E402

FLOAT = 772804865


def _runtime():
    cand = os.environ.get("TOXC_RUNTIME") or os.path.join(
        ROOT, "runtime_rs", "target", "release", "toxc-runtime"
    )
    return cand if os.path.exists(cand) else None


def _network(tmp):
    t = ToeTree(tmp)
    t.op(
        "project1/rig",
        "COMP:base",
        params={"Gain": "0"},
        custom=[f'{FLOAT} Gain Gain 1 1 0 0 1 1 10 2 1 "" "" Main 0'],
    )
    t.text(
        "project1/rig/exec",
        "import sys\n"
        "def onFrameStart(frame):\n"
        "    a = op('/project1/glsl1').numpyArray(delayed=True)\n"
        "    if a is not None:\n"
        "        print('RB frame=%d red=%.4f' % (frame, float(a[0, 0, 0])), file=sys.stderr)\n"
        "    op('/project1/rig').par.Gain = (frame % 10) * 0.1\n"
        "def onFrameEnd(frame):\n"
        "    op('/project1/glsl1').numpyArray(delayed=True)\n",
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
            "format": "rgba32float",
            "resolutionw": "16",
            "resolutionh": "8",
            "vec0name": "uGain",
            "vec0valuex": (17, "0", "op('rig').par.Gain"),
            "vec0valuew": "1",
        },
    )
    t.op("project1/window1", "COMP:window", params={"winop": "glsl1", "borders": "off"})
    return t.dir


@unittest.skipUnless(_runtime(), "toxc-runtime not built")
class RuntimeReadbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.out = os.path.join(cls.tmp, "art")
        proj = os.path.join(cls.tmp, "proj")
        os.makedirs(proj)
        HC.compile_host(
            _network(cls.tmp), cls.out, project_dir=proj, project_name="rb", log=lambda *_: None
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run(self, sync):
        env = dict(os.environ, TOXC_PYTHON=sys.executable)
        env["PYTHONPATH"] = os.pathsep.join(sys.path)
        env.pop("TOXC_SYNC_READBACK", None)
        if sync:
            env["TOXC_SYNC_READBACK"] = "1"
        p = subprocess.run(
            [_runtime(), "runseq", self.out, os.path.join(self.tmp, "f"), "8", "30"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if "EGL" in p.stderr and p.returncode != 0:
            self.skipTest("no EGL/GL driver: " + p.stderr[-300:])
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        return [
            (int(m.group(1)), float(m.group(2)))
            for m in re.finditer(r"RB frame=(\d+) red=([-\d.]+)", p.stderr)
        ]

    @staticmethod
    def _red(rendered_at):
        return (rendered_at % 10) * 0.1  # Gain set at that frame's start

    def test_sync_is_exactly_one_frame_late(self):
        s = self._run(True)
        self.assertGreaterEqual(len(s), 5, s)
        for frame, red in s:
            self.assertAlmostEqual(red, self._red(frame - 1), places=4)

    def test_async_delivers_recent_frames_in_order(self):
        a = self._run(False)
        self.assertGreaterEqual(len(a), 4, a)
        lags = []
        for frame, red in a:
            lag = next((k for k in (1, 2, 3) if abs(red - self._red(frame - k)) < 1e-4), None)
            self.assertIsNotNone(lag, (frame, red, a))
            lags.append(lag)
        # delivery never goes back in time
        rendered = [f - k for (f, _), k in zip(a, lags)]
        self.assertEqual(rendered, sorted(rendered))


if __name__ == "__main__":
    unittest.main()
