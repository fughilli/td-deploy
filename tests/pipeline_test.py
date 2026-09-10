"""Hermetic pipeline smoke test: IR -> passes -> lowering, no bridge/GL/nix.

Loads the committed demo graph, runs the optimizer and the GLSL lowering, and
asserts the plan comes out with real render steps + shader code. This is the
green coverage that `bazel test //...` runs with zero native dependencies.
"""

import os
import unittest

from ir.graph import Graph
from lowering.lower import lower
from passes.optimize import optimize


def _find(rel: str) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.getcwd(), here, os.path.dirname(here)):
        p = os.path.join(base, rel)
        if os.path.exists(p):
            return p
    raise FileNotFoundError(rel)


class PipelineTest(unittest.TestCase):
    def test_blur_demo_lowers_to_shaders(self):
        g = Graph.load(_find("graphs/blur_demo.json"))
        self.assertIn(g.output, g.nodes)

        # optimize() mutates the graph in place and yields human-readable lines.
        lines = list(optimize(g, out_res=256))
        self.assertTrue(lines, "optimizer produced no report lines")

        plan = lower(g, target="desktop_gl")
        self.assertTrue(plan.steps, "lowering produced no steps")
        self.assertEqual(plan.target, "desktop_gl")

        # At least one step must carry generated GLSL (the blur is a shader op).
        shader_steps = [s for s in plan.steps if getattr(s, "fragment", None)]
        self.assertTrue(shader_steps, "no shader step with fragment GLSL")
        self.assertIn("void main", "\n".join(s.fragment for s in shader_steps))


if __name__ == "__main__":
    unittest.main()
