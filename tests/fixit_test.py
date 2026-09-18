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

    def test_chop_guidance(self):
        p = fixit.build_fix_prompt("nope", "trace", chops=["CHOP:lfo", "CHOP:noise"])
        self.assertIn("CHOP:lfo", p)
        self.assertIn("CHOP:noise", p)
        self.assertIn("eval_chops", p)  # points the agent at the runtime CHOP eval

    def test_combined_top_and_chop_guidance(self):
        p = fixit.build_fix_prompt(
            "nope", "trace", unsupported=["TOP:feedback"], chops=["CHOP:lfo"]
        )
        self.assertIn("OP_MAP", p)  # TOP guidance
        self.assertIn("eval_chops", p)  # CHOP guidance
        self.assertIn("TOP:feedback", p)
        self.assertIn("CHOP:lfo", p)

    def test_prefers_existing_clone(self):
        # If the agent is already sitting in a clone of the repo, the prompt should tell
        # it to detect that, branch off latest main, and push to that clone's own remote —
        # not unconditionally fork+clone.
        p = fixit.build_fix_prompt("boom", "trace")
        low = p.lower()
        self.assertIn("--show-toplevel", p)  # detect it is inside a clone
        self.assertIn("origin/main", p)  # branch off latest main
        self.assertIn("git fetch origin", p)  # fetch first
        self.assertTrue(
            "already a clone" in low or "already a git clone" in low,
            "prompt should call out the already-in-a-clone case",
        )
        self.assertIn("this clone's own remote", low)  # push to that clone's remote

    def test_permission_failure_fallback(self):
        # When pushing to the existing clone's remote fails on permissions, the prompt
        # should offer BOTH: supply a push credential, OR fork and push to the fork.
        p = fixit.build_fix_prompt("boom", "trace")
        low = p.lower()
        self.assertIn("permissions issue", low)  # the failure mode
        self.assertIn("push credential", low)  # option 1
        self.assertIn("fork and push to the fork", low)  # option 2

    def test_still_has_fork_clone_fallback(self):
        # The original fork+clone flow must survive as the fallback for when the CWD is
        # NOT already a clone.
        p = fixit.build_fix_prompt("boom", "trace")
        self.assertIn("gh repo fork fughilli/td-deploy --clone", p)
        self.assertIn("NOT already a clone", p)


if __name__ == "__main__":
    unittest.main()
