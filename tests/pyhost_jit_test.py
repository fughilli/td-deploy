"""pyhost/tdhost/jit.py — native kernels for a project's numeric Python.

The static analysis (which functions qualify, which globals may change) runs
everywhere; the compile path (record argument types -> background compile process
-> load from Numba's cache -> switch; next start loads straight from the cache)
runs when Numba and SciPy are installed.
"""

import ast
import importlib.util
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pyhost"))

from tdhost import jit  # noqa: E402

SRC = textwrap.dedent(
    """
    import math
    import numpy as np

    AXES = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    S = {}
    COUNT = 0
    SCALE = 2.0
    CACHE = np.zeros(3)

    def unit(v):
        n = math.sqrt(float(np.sum(v * v)))
        if n > 1e-9:
            return v / n
        return v * 1.0

    def scaled(v, k):
        return unit(v) * k * SCALE + AXES[0]

    def loop_sum(n):
        t = 0.0
        for i in range(n):
            t += math.sin(i * 0.1)
        return t

    def uses_op(x):
        return op('/project1/rig').par.Gain * x

    def uses_state(x):
        return S.get('k', 1.0) * x

    def bumps():
        global COUNT
        COUNT += 1

    def uses_count(x):
        return COUNT * x

    def fills():
        CACHE[0] = 1.0

    def reads_cache(x):
        return CACHE * x

    def calls_bad(x):
        return uses_op(x) + 1

    def onCook(scriptOp):
        return 1.0

    def uses_dict(x):
        return {'a': x}
    """
)


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        self.glb = {"__name__": "m"}
        exec(compile(SRC, "m.py", "exec"), self.glb)

    def test_mutable_globals(self):
        mg = jit.mutable_globals(ast.parse(SRC))
        self.assertIn("COUNT", mg)  # `global COUNT; COUNT += 1`
        self.assertIn("CACHE", mg)  # CACHE[0] = ... in a function
        self.assertNotIn("AXES", mg)
        self.assertNotIn("SCALE", mg)
        self.assertNotIn("np", mg)

    def test_candidates(self):
        why = {}
        c = jit.candidates(SRC, self.glb, why=why)
        self.assertEqual(set(c), {"unit", "scaled", "loop_sum"})
        self.assertEqual(c["scaled"][0], {"SCALE", "AXES"})  # constants it freezes
        self.assertEqual(c["scaled"][1], {"unit"})  # kernels it calls
        self.assertIn("TouchDesigner API", why["uses_op"])
        self.assertIn("uses_op", why["calls_bad"])
        self.assertIn("changes", why["uses_count"])
        self.assertIn("changes", why["reads_cache"])
        self.assertIn("callback", why["onCook"])
        self.assertIn("Dict", why["uses_dict"])
        self.assertIn("uses_state", why)

    def test_fingerprint_tracks_values(self):
        a = np.arange(3.0)
        self.assertEqual(jit.fingerprint(a), jit.fingerprint(a.copy()))
        self.assertNotEqual(jit.fingerprint(a), jit.fingerprint(a + 1))
        self.assertNotEqual(jit.fingerprint(2.0), jit.fingerprint(2))


def _have_numba():
    return all(importlib.util.find_spec(m) for m in ("numba", "scipy"))


@unittest.skipUnless(_have_numba(), "numba/scipy not installed")
class CompileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = {
            "NUMBA_CACHE_DIR": os.path.join(self.tmp, "cache"),
            "PYTHONPATH": os.pathsep.join([os.path.join(ROOT, "pyhost")] + sys.path),
        }
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.logs = []

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self):
        j = jit.Jit(self.logs.append, os.path.join(self.tmp, "work"))
        fn = j.source_file("/project1/m", SRC)
        glb = {"__name__": "m", "op": None}
        exec(compile(SRC, fn, "exec"), glb)
        j.wrap_module("/project1/m", SRC, glb, fn)
        return j, glb

    def _settle(self, j, calls, limit=180.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < limit:
            calls()
            j.tick()
            if j.proc is None and not j.recording:
                return
            if j.proc is not None:
                time.sleep(0.05)
        self.fail("jit never settled: " + "\n".join(self.logs))

    def test_record_compile_switch_and_reload(self):
        j, glb = self._load()
        v = np.array([3.0, 4.0, 0.0])
        ref_scaled = glb["scaled"].py(v, 0.5)
        self.assertIsNone(glb["scaled"].disp)

        def calls():
            glb["scaled"](v, 0.5)
            glb["loop_sum"](10)

        self._settle(j, calls)
        self.assertIsNotNone(glb["scaled"].disp, self.logs)
        self.assertIsNotNone(glb["loop_sum"].disp, self.logs)
        np.testing.assert_allclose(glb["scaled"](v, 0.5), ref_scaled)
        self.assertAlmostEqual(glb["loop_sum"](10), glb["loop_sum"].py(10))
        # argument types it wasn't compiled for: runs Python, no compile in-process
        self.assertEqual(glb["loop_sum"](np.int8(3)), glb["loop_sum"].py(3))
        self.assertTrue(any("native" in m for m in self.logs), self.logs)

        # a restart (or an unchanged redeploy) loads the kernels before the first call
        self.logs.clear()
        j2, glb2 = self._load()
        self.assertIsNotNone(glb2["scaled"].disp, self.logs)
        np.testing.assert_allclose(glb2["scaled"](v, 0.5), ref_scaled)

    def test_untypeable_arguments_stay_python(self):
        j, glb = self._load()

        class Thing:
            pass

        t = Thing()
        with self.assertRaises(TypeError):
            glb["unit"](t)  # the Python original's own error
        self.assertTrue(glb["unit"].off)


if __name__ == "__main__":
    unittest.main()
