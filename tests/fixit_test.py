"""Unit tests for the 'fix and file' prompt builder (app/deploy_engine/fixit.py).

Loaded by file path so the test stays hermetic — importing the `deploy_engine` package
would pull in the whole deploy toolchain. No bridge / GL / nix needed.
"""

import importlib.util
import os
import unittest


def _load_fixit():
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.getcwd(), here, os.path.dirname(here)):
        p = os.path.join(base, "app", "deploy_engine", "fixit.py")
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("fixit", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError("app/deploy_engine/fixit.py")


fixit = _load_fixit()


class UnsupportedOperatorErrorTest(unittest.TestCase):
    def test_sorts_and_dedupes(self):
        e = fixit.UnsupportedOperatorError(["TOP:blur", "TOP:blur", "TOP:cache"])
        self.assertEqual(e.operators, ["TOP:blur", "TOP:cache"])

    def test_message_names_the_operators(self):
        e = fixit.UnsupportedOperatorError(["TOP:blur"])
        self.assertIn("TOP:blur", str(e))


class BuildFixPromptTest(unittest.TestCase):
    def test_includes_error_trace_and_repo(self):
        p = fixit.build_fix_prompt("boom", "Traceback...\nValueError: boom", toe="x.toe")
        self.assertIn("boom", p)
        self.assertIn("Traceback...", p)
        self.assertIn(fixit.REPO_URL, p)
        self.assertIn("x.toe", p)
        self.assertIn("pull request", p.lower())

    def test_unsupported_operator_guidance(self):
        p = fixit.build_fix_prompt("nope", "trace", unsupported=["TOP:blur", "TOP:cache"])
        self.assertIn("TOP:blur", p)
        self.assertIn("TOP:cache", p)
        self.assertIn("OP_MAP", p)  # points the agent at the dispatch table
        self.assertIn("gen_supported_ops.py", p)

    def test_generic_guidance_has_no_op_map(self):
        p = fixit.build_fix_prompt("kaboom", "trace")
        self.assertNotIn("OP_MAP", p)
        self.assertIn("regression test", p)


if __name__ == "__main__":
    unittest.main()
