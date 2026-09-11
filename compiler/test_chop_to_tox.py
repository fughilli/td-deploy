"""Unit tests for the CHOP-DAG -> tox MLIR emitter (P0).

Pure Python (no MLIR toolchain), so it runs in `bazel test //...`. The parse/
verify round-trip through toxc-opt is exercised manually (it needs the pinned
nixpkgs MLIR shell); see the module docstring in chop_to_tox.py.
"""

import unittest

from chop_to_tox import emit


class ChopToToxTest(unittest.TestCase):
    def test_ascii_dag(self):
        # midiin1 (source) -> constant1 (expr over it) -> speed1 (integrate).
        chops = [
            {
                "name": "constant1",
                "type": "constant",
                "inputs": [],
                "channels": ["op('midiin1')[0][0]/127 - 0.5"],
            },
            {"name": "speed1", "type": "speed", "inputs": ["constant1"], "channels": []},
        ]
        ir = emit(chops)
        self.assertIn("func.func @chops()", ir)
        # The external MIDI ref becomes a source, emitted before its user.
        self.assertIn('%midiin1 = tox.chop_source "midiin1" : !tox.chop<16>', ir)
        self.assertLess(ir.index("chop_source"), ir.index("chop_expr"))
        # The expr-valued Constant is a chop_expr over the source (verbatim expr).
        self.assertIn(
            "%constant1 = tox.chop_expr(%midiin1) "
            "[\"op('midiin1')[0][0]/127 - 0.5\"] : "
            "(!tox.chop<16>) -> !tox.chop<1>",
            ir,
        )
        # Speed integrates its input, same width.
        self.assertIn("%speed1 = tox.chop_speed %constant1 : " "(!tox.chop<1>) -> !tox.chop<1>", ir)

    def test_literal_constant(self):
        ir = emit(
            [{"name": "c", "type": "constant", "inputs": [], "channels": ["0.5", "-1", "2.0"]}]
        )
        self.assertIn("%c = tox.chop_constant [0.5, -1, 2] : !tox.chop<3>", ir)
        self.assertNotIn("chop_expr", ir)  # all-literal -> not an expr node
        self.assertNotIn("chop_source", ir)  # no op() refs

    def test_null_is_passthrough(self):
        ir = emit(
            [
                {"name": "c", "type": "constant", "inputs": [], "channels": ["1.0"]},
                {"name": "n", "type": "null", "inputs": ["c"], "channels": []},
            ]
        )
        self.assertIn("%n = tox.chop_select %c : " "(!tox.chop<1>) -> !tox.chop<1>", ir)

    def test_unknown_type_preserved_as_expr(self):
        # An unmodeled CHOP type must not silently vanish — parity-preserving.
        ir = emit(
            [
                {"name": "c", "type": "constant", "inputs": [], "channels": ["1.0"]},
                {"name": "lag1", "type": "lag", "inputs": ["c"], "channels": []},
            ]
        )
        self.assertIn("%lag1 = tox.chop_expr(%c)", ir)


if __name__ == "__main__":
    unittest.main()
