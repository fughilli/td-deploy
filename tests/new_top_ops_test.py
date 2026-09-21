"""Add / Math / Noise / Feedback TOPs: import, type inference, and lowering.

The interesting one is Feedback. A Feedback TOP names its source in the `top`
PARAMETER rather than wiring it as an input, and that source is normally
downstream of the feedback itself — so treating it as an ordinary edge would make
the graph cyclic. It is imported as a `delay=1` edge instead, which
`Graph.topo_order` cuts, and the runtime fills the buffer after each cook.
"""

import os
import shutil
import tempfile
import unittest

from importer.from_toeexpand import import_dir
from ir.graph import Graph
from lowering.lower import lower
from passes.optimize import infer_format, optimize

OT = {"w": 64, "h": 64, "fmt": "rgba8"}


def _graph(nodes, output):
    return Graph.from_json({"output": output, "nodes": nodes})


def _src(nid="src"):
    return {"id": nid, "op": "image_in", "family": "TOP", "inputs": [], "out_type": OT}


class AddTopTest(unittest.TestCase):
    def test_sums_every_bound_input(self):
        g = _graph(
            [
                _src("a"),
                _src("b"),
                {"id": "add", "op": "add", "family": "TOP", "inputs": ["a", "b"], "out_type": OT},
            ],
            "add",
        )
        step = {s.node_id: s for s in lower(g, target="gles").steps}["add"]
        self.assertEqual(step.kind, "shader")
        self.assertEqual(step.inputs, ["a", "b"])
        # Both inputs are sampled and summed.
        self.assertIn("texture(tex0, vUV) + texture(tex1, vUV)", step.fragment)


class MathTopTest(unittest.TestCase):
    def _step(self, params, inputs=("a",)):
        nodes = [_src(i) for i in inputs]
        nodes.append(
            {
                "id": "m",
                "op": "math",
                "family": "TOP",
                "inputs": list(inputs),
                "params": params,
                "out_type": OT,
            }
        )
        return {s.node_id: s for s in lower(_graph(nodes, "m"), target="gles").steps}["m"]

    def test_gain_and_offsets_are_per_frame_uniforms(self):
        # They are ordinary TD parameters, so they must survive as expressions.
        st = self._step({"gain": "0.95", "preoff": "0.1", "postoff": "0.2"})
        self.assertEqual(float(st.time_uniforms["uGain"]["expr"]), 0.95)
        self.assertEqual(float(st.time_uniforms["uPreOff"]["expr"]), 0.1)
        self.assertEqual(float(st.time_uniforms["uPostOff"]["expr"]), 0.2)
        self.assertIn("(acc + uPreOff) * uGain + uPostOff", st.fragment)

    def test_no_op_combine_degrades_to_plain_input(self):
        # TD writes `no_op` when no combine is selected; with one input that must
        # not fold anything in.
        st = self._step({"op": "no_op"})
        self.assertNotIn("acc +=", st.fragment)

    def test_combine_operator_selects_the_fold(self):
        st = self._step({"op": "max"}, inputs=("a", "b"))
        self.assertIn("acc = max(acc, s);", st.fragment)

    def test_defaults_are_identity(self):
        st = self._step({})
        self.assertEqual(float(st.time_uniforms["uGain"]["expr"]), 1.0)
        self.assertEqual(float(st.time_uniforms["uPreOff"]["expr"]), 0.0)
        self.assertEqual(float(st.time_uniforms["uPostOff"]["expr"]), 0.0)


class NoiseTopTest(unittest.TestCase):
    def test_is_a_generator_with_its_own_resolution(self):
        g = _graph(
            [{"id": "n", "op": "noise", "family": "TOP", "inputs": [], "params": {}}],
            "n",
        )
        report: list[str] = []
        infer_format(g, report, out_res=128)
        # A generator has no input to inherit a size from.
        self.assertEqual((g.nodes["n"].out_type["w"], g.nodes["n"].out_type["h"]), (128, 128))
        st = {s.node_id: s for s in lower(g, target="gles").steps}["n"]
        self.assertEqual(st.inputs, [])
        self.assertEqual(st.kind, "shader")

    def test_td_parameters_reach_the_shader(self):
        g = _graph(
            [
                {
                    "id": "n",
                    "op": "noise",
                    "family": "TOP",
                    "inputs": [],
                    "params": {
                        "period": "4",
                        "amp": "0.5",
                        "offset": "0.5",
                        "mono": "off",
                        "seed": "7",
                        "harmon": "2",
                    },
                    "out_type": OT,
                }
            ],
            "n",
        )
        st = {s.node_id: s for s in lower(g, target="gles").steps}["n"]
        self.assertEqual(float(st.time_uniforms["uPeriod"]["expr"]), 4.0)
        self.assertEqual(float(st.time_uniforms["uAmp"]["expr"]), 0.5)
        self.assertEqual(st.uniforms["uSeed"], ("float", 7.0))
        # harmon/mono are BAKED into the shader (unrolled octaves, no branches),
        # so they are not uniforms — assert on the generated source instead.
        self.assertEqual(st.fragment.count("sum += amp * gradNoise(q);"), 3)  # harmon 2 -> 3
        self.assertNotIn("uMono", st.fragment)
        self.assertIn("channel(uSeed + 31.0)", st.fragment)  # mono off -> per-channel

    def test_harmonics_controls_the_unrolled_octave_count(self):
        def octaves(harmon):
            g = _graph(
                [
                    {
                        "id": "n",
                        "op": "noise",
                        "family": "TOP",
                        "inputs": [],
                        "params": {"harmon": harmon},
                        "out_type": OT,
                    }
                ],
                "n",
            )
            frag = {s.node_id: s for s in lower(g, target="gles").steps}["n"].fragment
            return frag.count("sum += amp * gradNoise(q);")

        self.assertEqual(octaves("0"), 1)
        self.assertEqual(octaves("3"), 4)
        # No dynamic loop or branch survives into the shader.
        g = _graph(
            [
                {
                    "id": "n",
                    "op": "noise",
                    "family": "TOP",
                    "inputs": [],
                    "params": {"harmon": "2"},
                    "out_type": OT,
                }
            ],
            "n",
        )
        frag = {s.node_id: s for s in lower(g, target="gles").steps}["n"].fragment
        self.assertNotIn("for (", frag)
        self.assertNotIn("break;", frag)

    def test_no_trig_in_the_hash(self):
        # The gradient hash must stay trig-free: sin/cos per lattice corner was
        # the whole reason this pass was the most expensive one on the Pi.
        g = _graph(
            [
                {
                    "id": "n",
                    "op": "noise",
                    "family": "TOP",
                    "inputs": [],
                    "params": {},
                    "out_type": OT,
                }
            ],
            "n",
        )
        frag = {s.node_id: s for s in lower(g, target="gles").steps}["n"].fragment
        for banned in ("sin(", "cos(", "tan("):
            self.assertNotIn(banned, frag)

    def test_noise_is_deterministic_for_a_given_seed(self):
        # Same params in, byte-identical shader out: recompiles stay reproducible.
        def frag(seed):
            g = _graph(
                [
                    {
                        "id": "n",
                        "op": "noise",
                        "family": "TOP",
                        "inputs": [],
                        "params": {"seed": seed},
                        "out_type": OT,
                    }
                ],
                "n",
            )
            return {s.node_id: s for s in lower(g, target="gles").steps}["n"].fragment

        self.assertEqual(frag("1"), frag("1"))


class FeedbackTopTest(unittest.TestCase):
    def _loop(self):
        """src -> fb -> gain -> add -> out, with fb echoing add's previous frame."""
        return _graph(
            [
                _src("src"),
                {
                    "id": "fb",
                    "op": "feedback",
                    "family": "TOP",
                    # input 0 seeds the buffer; the delayed edge is the target.
                    "inputs": ["src", {"node": "add", "delay": 1}],
                    "params": {"_feedback_target": "add"},
                    "out_type": OT,
                },
                {
                    "id": "gain",
                    "op": "math",
                    "family": "TOP",
                    "inputs": ["fb"],
                    "params": {"gain": "0.95"},
                    "out_type": OT,
                },
                {
                    "id": "add",
                    "op": "add",
                    "family": "TOP",
                    "inputs": ["gain", "src"],
                    "out_type": OT,
                },
            ],
            "add",
        )

    def test_delayed_edge_makes_the_loop_orderable(self):
        order = self._loop().topo_order()
        # The cycle is legal, and the feedback must cook BEFORE its target so it
        # still holds last frame's pixels when the target's consumers read it.
        self.assertLess(order.index("fb"), order.index("add"))

    def test_lowers_to_a_feedback_step_naming_its_target(self):
        st = {s.node_id: s for s in lower(self._loop(), target="gles").steps}["fb"]
        self.assertEqual(st.kind, "feedback")
        self.assertEqual(st.feedback_from, "add")
        # Only the seed is a bound input — the target is not sampled as texture.
        self.assertEqual(st.inputs, ["src"])
        self.assertIsNone(st.fragment)

    def test_target_survives_dead_node_elimination(self):
        g = self._loop()
        optimize(g, out_res=64)  # runs dead_node_elim + infer_format
        self.assertIn("add", g.nodes)
        self.assertIn("fb", g.nodes)

    def test_a_non_delayed_loop_is_still_rejected(self):
        # The delay is what makes it legal; without it this must not silently pass.
        g = _graph(
            [
                _src("src"),
                {"id": "x", "op": "add", "family": "TOP", "inputs": ["src", "y"], "out_type": OT},
                {"id": "y", "op": "add", "family": "TOP", "inputs": ["x"], "out_type": OT},
            ],
            "x",
        )
        with self.assertRaises(ValueError):
            g.topo_order()

    def test_feedback_without_a_seed_input_still_gets_a_buffer(self):
        g = _graph(
            [
                {
                    "id": "fb",
                    "op": "feedback",
                    "family": "TOP",
                    "inputs": [{"node": "a", "delay": 1}],
                },
                {"id": "a", "op": "add", "family": "TOP", "inputs": ["fb"]},
            ],
            "a",
        )
        report: list[str] = []
        infer_format(g, report, out_res=32)
        self.assertEqual(g.nodes["fb"].out_type["w"], 32)


class ImporterFeedbackTargetTest(unittest.TestCase):
    """The `top` parameter is resolved relative to the Feedback TOP's parent."""

    def _expand(self, top_value):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        root = os.path.join(d, "proj.toe.dir")
        os.makedirs(os.path.join(root, "project1"))

        def write(rel, text):
            with open(os.path.join(root, rel), "w") as f:
                f.write(text)

        write(".start", "project1\n")
        write("project1.n", "COMP:base\nend\n")
        write("project1/src.n", "TOP:moviefilein\nend\n")
        write("project1/fb.n", "TOP:feedback\ninputs\n{\n0 \tsrc\n}\nend\n")
        write("project1/fb.parm", f"?\ntop 0 {top_value}\n?\n")
        # The importer walks back from the TOP flagged `display on`.
        write(
            "project1/out.n",
            "TOP:null\nflags =  display on\ninputs\n{\n0 \tfb\n}\nend\n",
        )
        return root

    def _feedback_ports(self, top_value):
        g = import_dir(self._expand(top_value)).graph
        node = g.nodes["project1/fb"]
        return node, [(p.node, p.delay) for p in node.inputs]

    def test_relative_target_resolves_against_the_parent(self):
        node, ports = self._feedback_ports("src")
        self.assertIn(("project1/src", 1), ports)
        self.assertEqual(node.params["_feedback_target"], "project1/src")

    def test_absolute_target_is_not_re_rooted(self):
        _, ports = self._feedback_ports("/project1/src")
        self.assertIn(("project1/src", 1), ports)

    def test_unresolvable_target_degrades_instead_of_raising(self):
        node, ports = self._feedback_ports("nope")
        self.assertEqual([p for p in ports if p[1] > 0], [])
        self.assertNotIn("_feedback_target", node.params)


if __name__ == "__main__":
    unittest.main()
