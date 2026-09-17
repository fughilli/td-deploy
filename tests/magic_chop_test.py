"""Unit tests for the magic-chop pass (passes/magic_chop.py).

Pure IR rewrite — no bridge/GL/nix. Builds a tiny graph whose param references an
unsupported CHOP and asserts the reference becomes an animated sinusoid.
"""

import unittest

from ir.graph import Graph, Node
from passes import magic_chop


def _graph_with(chop_type, expr):
    n = Node(id="transform1", op="transform", family="TOP", params={"rotate": expr})
    g = Graph(output="transform1", nodes={"transform1": n})
    g.chops = [{"name": "lfo1", "type": chop_type, "inputs": [], "channels": []}]
    g.services = [{"type": "midiin", "name": "midiin1"}]
    return g


class MagicChopTest(unittest.TestCase):
    def test_detects_unsupported_only(self):
        g = _graph_with("lfo", "op('lfo1')['tx']")
        g.chops.append({"name": "c1", "type": "constant", "inputs": [], "channels": ["0.5"]})
        names = {c["name"] for c in magic_chop.unsupported_chops(g)}
        self.assertEqual(names, {"lfo1"})  # constant is supported, lfo is not

    def test_rewrites_reference_to_sinusoid(self):
        g = _graph_with("lfo", "op('lfo1')['tx'] * 90")
        recs = magic_chop.apply(g)
        rot = g.nodes["transform1"].params["rotate"]
        self.assertIn("sin(absTime.seconds", rot)
        self.assertNotIn("op('lfo1')", rot)
        self.assertIn("* 90", rot)  # surrounding expression is preserved
        # the unsupported chop is inlined + dropped from the DAG
        self.assertEqual(g.chops, [])
        self.assertEqual(recs[0]["name"], "lfo1")
        self.assertEqual(recs[0]["channels"], ["tx"])

    def test_supported_chops_untouched(self):
        g = _graph_with("constant", "op('lfo1')['tx']")  # 'lfo1' is a constant here
        recs = magic_chop.apply(g)
        self.assertEqual(recs, [])
        self.assertEqual(g.nodes["transform1"].params["rotate"], "op('lfo1')['tx']")

    def test_deterministic(self):
        a = _graph_with("noise", "op('lfo1')['tx']")
        b = _graph_with("noise", "op('lfo1')['tx']")
        magic_chop.apply(a)
        magic_chop.apply(b)
        self.assertEqual(a.nodes["transform1"].params, b.nodes["transform1"].params)

    def test_distinct_channels_distinct_periods(self):
        n = Node(
            id="t",
            op="transform",
            family="TOP",
            params={"tx": "op('lfo1')['x']", "ty": "op('lfo1')['y']"},
        )
        g = Graph(output="t", nodes={"t": n})
        g.chops = [{"name": "lfo1", "type": "lfo", "inputs": [], "channels": []}]
        magic_chop.apply(g)
        self.assertNotEqual(n.params["tx"], n.params["ty"])  # different random periods
        for v in n.params.values():
            self.assertRegex(v, r"sin\(absTime\.seconds \* [0-9.]+ \+ [0-9.]+\)")


if __name__ == "__main__":
    unittest.main()
