"""Hermetic (no MLIR toolchain) tests for the P1 CHOP pieces: the reference
evaluator's semantics and the lowering's ABI/structure. The full compile +
bit-parity gate is compiler/test_chop_lower.py (needs the MLIR nix shell).
"""

import unittest

from chop_lower import Unsupported, lower
from chop_ref import ChopEval

ASCII = [
    {
        "name": "constant1",
        "type": "constant",
        "inputs": [],
        "channels": ["op('midiin1')[0][0]/127 - 0.5"],
    },
    {"name": "speed1", "type": "speed", "inputs": ["constant1"], "channels": []},
]


class ChopRefTest(unittest.TestCase):
    def test_constant_expr_and_double_index(self):
        ev = ChopEval(ASCII)
        st = ev.step(0.0, {"midiin1": {"0": 127.0}})
        # 127/127 - 0.5 == 0.5 ; the trailing [0] sample index is dropped.
        self.assertAlmostEqual(st.get("constant1", "0"), 0.5)

    def test_speed_integrates_with_clamped_dt(self):
        ev = ChopEval(ASCII)
        # dt clamps to 1.0 on the first step (t jumps from last_t=0 to 5).
        st = ev.step(5.0, {"midiin1": {"0": 127.0}})
        self.assertAlmostEqual(st.get("speed1", "0"), 0.5)  # 0.5 * dt(=1.0)
        st = ev.step(5.5, {"midiin1": {"0": 127.0}})
        self.assertAlmostEqual(st.get("speed1", "0"), 0.75)  # + 0.5 * 0.5

    def test_nonfinite_expr_is_zero(self):
        ev = ChopEval([{"name": "c", "type": "constant", "inputs": [], "channels": ["1.0/0.0"]}])
        self.assertEqual(ev.step(0.0, {}).get("c", "0"), 0.0)

    def test_null_passthrough(self):
        chops = [
            {"name": "c", "type": "constant", "inputs": [], "channels": ["2.0"]},
            {"name": "n", "type": "null", "inputs": ["c"], "channels": []},
        ]
        self.assertAlmostEqual(ChopEval(chops).step(0.0, {}).get("n", "0"), 2.0)


class ChopLowerAbiTest(unittest.TestCase):
    def test_ascii_abi(self):
        mlir, abi = lower(ASCII)
        self.assertEqual(abi["sources"], [("midiin1", "0")])
        self.assertEqual(abi["states"], [("speed1", "0")])
        self.assertEqual(abi["outputs"], [("constant1", "0"), ("speed1", "0")])
        # The MIDI source and the carried Speed state are function args.
        self.assertIn("%src_midiin1_0: f64", mlir)
        self.assertIn("%st_speed1_0: f64", mlir)
        self.assertIn("-> (f64, f64)", mlir)
        # The integrator is state + input*dt.
        self.assertIn("arith.mulf", mlir)
        self.assertIn("arith.addf %st_speed1_0", mlir)

    def test_literal_constant_no_sources(self):
        _, abi = lower(
            [{"name": "c", "type": "constant", "inputs": [], "channels": ["1.0", "2.0"]}]
        )
        self.assertEqual(abi["sources"], [])
        self.assertEqual(abi["outputs"], [("c", "0"), ("c", "1")])


if __name__ == "__main__":
    unittest.main()


class MultiChannelTest(unittest.TestCase):
    """A Speed fed a multi-channel rate must integrate EVERY channel, and a
    passthrough must carry them all. Both used to handle only channel 0, which
    left a three-axis spin moving on one axis with nothing reporting it."""

    DAG = [
        {
            "name": "rate1",
            "type": "constant",
            "inputs": [],
            "channels": ["1.0", "2.0", "-4.0"],
        },
        {"name": "spin1", "type": "speed", "inputs": ["rate1"], "channels": []},
        {"name": "out1", "type": "null", "inputs": ["spin1"], "channels": []},
    ]

    def test_reference_integrates_every_channel(self):
        ev = ChopEval(self.DAG)
        ev.step(0.0, {})
        st = ev.step(1.0, {})  # one second of integration
        self.assertAlmostEqual(st.get("spin1", 0), 1.0, places=6)
        self.assertAlmostEqual(st.get("spin1", 1), 2.0, places=6)
        self.assertAlmostEqual(st.get("spin1", 2), -4.0, places=6)

    def test_passthrough_carries_every_channel(self):
        ev = ChopEval(self.DAG)
        ev.step(0.0, {})
        st = ev.step(1.0, {})
        for i in range(3):
            self.assertAlmostEqual(st.get("out1", i), st.get("spin1", i), places=9)

    def test_lowering_carries_one_accumulator_per_channel(self):
        _mlir, abi = lower(self.DAG)
        self.assertEqual(
            [tuple(x) for x in abi["states"]],
            [("spin1", "0"), ("spin1", "1"), ("spin1", "2")],
        )
        outs = [tuple(x) for x in abi["outputs"]]
        for i in ("0", "1", "2"):
            self.assertIn(("out1", i), outs)

    def test_width_propagates_through_a_chain(self):
        _mlir, abi = lower(self.DAG)
        # Every CHOP in the chain is as wide as the Constant that originated it.
        for nm in ("rate1", "spin1", "out1"):
            self.assertEqual(sum(1 for n, _c in (tuple(x) for x in abi["outputs"]) if n == nm), 3)


class LiveSourceInputTest(unittest.TestCase):
    """A CHOP wired straight off a MIDI/OSC service.

    The importer keeps the service in the consumer's `inputs` but leaves it out
    of the DAG, so `inputs[0]` names something the DAG never defines. The width
    of a live service is not knowable at compile time, and its channels are
    NAMED rather than indexed — so the lowering declines and the interpreted
    path, which can see the real channel set, carries it.
    """

    SRC_FED = [
        {"name": "null1", "type": "null", "inputs": ["midiin1"], "channels": []},
    ]
    CHAINED = SRC_FED + [
        {"name": "null2", "type": "null", "inputs": ["null1"], "channels": []},
    ]
    SPEED_FED = [
        {"name": "spin1", "type": "speed", "inputs": ["midiin1"], "channels": []},
    ]
    MIDI = {"midiin1": {"ch1ctrl1": 0.25, "ch1ctrl2": 0.75}}

    def test_lowering_declines_with_a_diagnosis(self):
        # It used to die with `TypeError: sequence item 0: expected str
        # instance, NoneType found`, which emit_artifact then filed in the
        # coverage log as if it were an ordinary unsupported operator.
        with self.assertRaises(Unsupported) as cm:
            lower(self.SRC_FED)
        msg = str(cm.exception)
        self.assertIn("null1", msg)
        self.assertIn("midiin1", msg)

    def test_lowering_declines_a_speed_too(self):
        # The speed branch was worse than a crash: it substituted 0.0 for the
        # unresolved input and integrated nothing, forever, reporting success.
        with self.assertRaises(Unsupported):
            lower(self.SPEED_FED)

    def test_reference_carries_named_channels(self):
        st = ChopEval(self.SRC_FED).step(0.0, self.MIDI)
        self.assertEqual(st.channels("null1"), ["ch1ctrl1", "ch1ctrl2"])
        self.assertAlmostEqual(st.get("null1", "ch1ctrl1"), 0.25)
        self.assertAlmostEqual(st.get("null1", "ch1ctrl2"), 0.75)

    def test_reference_does_not_invent_channel_zero(self):
        # The old failure mode: width defaulted to 1, so it read index "0",
        # which a name-keyed source never sets, and published a silent 0.0.
        st = ChopEval(self.SRC_FED).step(0.0, self.MIDI)
        self.assertNotIn("0", st.channels("null1"))

    def test_the_property_is_contagious(self):
        ev = ChopEval(self.CHAINED)
        st = ev.step(0.0, self.MIDI)
        self.assertEqual(ev.dynamic, {"null1", "null2"})
        self.assertEqual(st.channels("null2"), ["ch1ctrl1", "ch1ctrl2"])

    def test_reference_integrates_each_named_channel(self):
        ev = ChopEval(self.SPEED_FED)
        ev.step(0.0, self.MIDI)
        st = ev.step(1.0, self.MIDI)
        self.assertAlmostEqual(st.get("spin1", "ch1ctrl1"), 0.25, places=6)
        self.assertAlmostEqual(st.get("spin1", "ch1ctrl2"), 0.75, places=6)

    def test_numeric_channels_sort_numerically(self):
        # Ordering has to be stable and not lexicographic, or channel 10 lands
        # between 1 and 2 and the ABI silently permutes.
        st = ChopEval(self.SRC_FED).step(
            0.0, {"midiin1": {str(i): float(i) for i in (0, 1, 2, 10, 11)}}
        )
        self.assertEqual(st.channels("null1"), ["0", "1", "2", "10", "11"])
