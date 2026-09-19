"""Regression: the Transform TOP primitive wraps its rotation angle.

GLES uniforms are 32-bit floats. An unbounded rotation angle (a Speed CHOP or
absTime feeding `rotate`) loses its per-frame increment to the f32 ULP once the
value grows large — after days of uptime the rotation visibly cogs. The fix
models an fmod into the primitive: the Transform TOP emits its `uRotate`
time-uniform with a periodic wrap (mod = 2*pi radians), which the runtime applies
in f64 before the f32 upload. Non-periodic components (translate/scale) get no
wrap.
"""

import math
import unittest

from ir.graph import Graph
from lowering.lower import lower


def _transform(params, ot=None):
    ot = ot or {"w": 256, "h": 256, "fmt": "rgba8"}
    return Graph.from_json(
        {
            "output": "t",
            "nodes": [
                {"id": "src", "op": "image_in", "family": "TOP", "inputs": [], "out_type": ot},
                {
                    "id": "t",
                    "op": "transform",
                    "family": "TOP",
                    "inputs": ["src"],
                    "params": params,
                    "out_type": ot,
                },
            ],
        }
    )


class TransformRotateWrapTest(unittest.TestCase):
    def _transform_step(self, params):
        plan = lower(_transform(params), "desktop_gl")
        step = next(s for s in plan.steps if s.node_id == "t")
        return step.time_uniforms

    def test_rotate_carries_2pi_wrap(self):
        tus = self._transform_step({"rotate": "absTime.seconds * 45"})
        self.assertIn("uRotate", tus)
        self.assertAlmostEqual(tus["uRotate"]["mod"], 2.0 * math.pi)
        # deg -> rad conversion is still folded into `mul`.
        self.assertAlmostEqual(tus["uRotate"]["mul"], math.pi / 180.0)

    def test_wrap_present_even_for_static_rotation(self):
        # Modelled into the primitive: the wrap is unconditional, not expr-dependent.
        tus = self._transform_step({"rotate": 30.0})
        self.assertAlmostEqual(tus["uRotate"]["mod"], 2.0 * math.pi)

    def test_rotation_is_aspect_corrected(self):
        # On a non-square frame, rotation must not stretch: the step carries the
        # output aspect (w/h) and the shader rotates in aspect-corrected space.
        plan = lower(
            _transform({"rotate": "30"}, {"w": 1280, "h": 720, "fmt": "rgba8"}), "desktop_gl"
        )
        step = next(s for s in plan.steps if s.node_id == "t")
        self.assertIn("uAspect", step.uniforms)
        kind, val = step.uniforms["uAspect"]
        self.assertEqual(kind, "float")
        self.assertAlmostEqual(val, 1280.0 / 720.0)
        self.assertIn("uAspect", step.fragment)
        self.assertIn("p.x *= uAspect", step.fragment)
        self.assertIn("p.x /= uAspect", step.fragment)

    def test_translate_and_scale_are_not_wrapped(self):
        tus = self._transform_step({"tx": "absTime.seconds", "sx": "2"})
        for name in ("uTranslateX", "uTranslateY", "uScaleX", "uScaleY"):
            self.assertNotIn("mod", tus[name], f"{name} must not be wrapped")


if __name__ == "__main__":
    unittest.main()
