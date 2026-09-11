"""Hermetic (no MLIR toolchain) tests for the P1 CHOP pieces: the reference
evaluator's semantics and the lowering's ABI/structure. The full compile +
bit-parity gate is compiler/test_chop_lower.py (needs the MLIR nix shell).
"""

import unittest

from chop_lower import lower
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
