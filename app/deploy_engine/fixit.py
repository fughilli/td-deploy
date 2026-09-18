"""The "fix and file" canned agent prompt.

When a deploy (or flash) fails — most notably when a project uses a TouchDesigner
operator td-deploy doesn't support yet — the app offers a one-click **Copy fix-it
prompt** button. It copies the text built here: a self-contained instruction set plus the
real error trace and context, ready to paste into an AI coding agent that will diagnose
the bug, implement a fix, and open a pull request against the upstream repo.

`build_fix_prompt()` is pure (string in, string out) so it is trivially unit-testable and
carries no import-time dependencies beyond the stdlib.
"""

from __future__ import annotations

REPO_URL = "https://github.com/fughilli/td-deploy"


class UnsupportedOperatorError(Exception):
    """Raised when a project's render path uses TouchDesigner operators that td-deploy
    does not map yet. Carries the offending `FAMILY:optype` strings so the UI can build a
    targeted fix-it prompt."""

    def __init__(self, operators):
        self.operators = sorted(set(operators))
        joined = ", ".join(self.operators)
        super().__init__(
            f"Unsupported TouchDesigner operator(s): {joined}. "
            "td-deploy doesn't know how to compile these yet."
        )


_TOP_GUIDANCE = """\
The project uses **unsupported TouchDesigner TOP operator(s)**: {ops}. td-deploy only
understands a subset of TouchDesigner's operators — see the "Supported operators" table
in `DEVELOPERS.md`. To add support:

1. Add the operator to `OP_MAP` in `importer/from_toeexpand.py`, mapping
   `("<FAMILY>", "<optype>")` to a new IR kernel name (or `None` if it is a purely
   structural passthrough).
2. Map any TouchDesigner parameters you need in the importer, then implement the kernel
   in `lowering/lower.py` — model it on the existing `level` / `transform` / `crop`
   handlers, or emit a custom shader like `glsl_top`. Use the numpy reference backend in
   `runtime/` as the conformance oracle (it should agree with the GL backend to ~1 LSB).
3. Add a one-line description for the operator to `DESCRIPTIONS` in
   `tools/gen_supported_ops.py`, then run `python3 tools/gen_supported_ops.py` to refresh
   the supported-operators list in `DEVELOPERS.md`.
4. Add a test under `tests/` that imports and lowers a small graph using the operator."""

_CHOP_GUIDANCE = """\
The project drives parameters from **unsupported CHOP(s)**: {chops}. td-deploy evaluates
only a few CHOP types (constant, speed, and passthrough-style null/select); others resolve
to 0 (or to a placeholder sinusoid in "magic chop" mode). To add real support:

1. Build the CHOP's def (its `inputs`/`channels`) in `importer/from_toeexpand.py`
   (`_collect_chops`), the way `constant` is handled.
2. Evaluate it per frame in the runtime: add a match arm in `runtime_rs/src/main.rs`
   `eval_chops` alongside the `constant` / `speed` arms (and/or lower it natively via
   `compiler/chop_lower.py`). `absTime.seconds`, `absTime.frame`, and a per-frame `dt`
   are available to expressions.
3. Add a test under `tests/` that exercises the CHOP feeding a parameter expression."""

_GENERIC_GUIDANCE = """\
Diagnose the failure from the trace above. Find the root cause in the pipeline
(`importer/` -> `passes/` -> `lowering/` -> `runtime_rs/`, or the app's
`app/deploy_engine/`), implement a fix, and add a regression test under `tests/` that
fails before your change and passes after."""

_TEMPLATE = """\
You are an expert software engineer. Help me fix a bug in **td-deploy**, an open-source
tool that compiles TouchDesigner projects to run natively on a Raspberry Pi, and open a
pull request with the fix.

## The error

While {action}, td-deploy failed with:

    {error_summary}

Full trace:

```
{traceback}
```

Context:

- Repository: {repo}
- Project: {toe}
- Target: {target}
- td-deploy version: {version}

## What to do

{guidance}

## Verify your fix

- `bazel test //...` passes.
- `prek run --all-files` is clean.
- If you changed which operators are supported, you ran `python3 tools/gen_supported_ops.py`
  so `DEVELOPERS.md` matches (CI enforces this).

Orient yourself with `DEVELOPERS.md`, which documents the pipeline, the repo layout, and
how to build and test.

## File the pull request

First figure out where you are. **If your current working directory is already a clone of
this repo** — `git rev-parse --show-toplevel` succeeds and `git remote -v` points at
{repo} (any of its `.git`/SSH/HTTPS forms) — work in that existing clone rather than
cloning anew: fetch origin, branch off the latest `main`, make the fix, and push to that
clone's own remote:

```
git rev-parse --show-toplevel      # confirms this CWD is a git clone
git remote -v                      # confirms origin points at fughilli/td-deploy
git fetch origin
git checkout -b fix/<short-description> origin/main   # branch off latest main
# ...make the change + add a test...
git commit -am "<summary of the fix>"
git push -u origin fix/<short-description>             # push to this clone's own remote
gh pr create --repo fughilli/td-deploy --base main \\
  --title "<concise title>" \\
  --body "<what was broken, what you changed, and how you verified it; paste the error above>"
```

**If that push fails due to a permissions issue** (you don't have write access to this
clone's remote), don't give up — tell me and offer both options:

1. **Provide a push credential for this remote**: I can supply a credential (e.g. a
   GitHub token / SSH key) authorized to push to this clone's remote, and you retry the
   push above; or
2. **Fork and push to the fork instead**: fork {repo} to my account, add the fork as a
   new remote, and push the branch there, then open the PR from the fork:

```
gh repo fork fughilli/td-deploy --remote --remote-name fork   # or fork in the GitHub UI
git push -u fork fix/<short-description>
gh pr create --repo fughilli/td-deploy --base main --head <my-account>:fix/<short-description> \\
  --title "<concise title>" \\
  --body "<what was broken, what you changed, and how you verified it>"
```

**If your current working directory is NOT already a clone of this repo**, fall back to the
fork-and-clone flow: fork the repo, clone your fork, then branch off `main` and open the PR:

```
gh repo fork fughilli/td-deploy --clone   # or fork in the GitHub UI, then clone your fork
cd td-deploy
git checkout -b fix/<short-description>
# ...make the change + add a test...
git commit -am "<summary of the fix>"
git push -u origin fix/<short-description>
gh pr create --repo fughilli/td-deploy --base main \\
  --title "<concise title>" \\
  --body "<what was broken, what you changed, and how you verified it; paste the error above>"
```

If I don't have a GitHub credential set up, stop and walk me through it first:

1. If I don't have a GitHub account, help me create a free one at https://github.com/signup.
2. Fork {repo} to my account (the **Fork** button, or `gh repo fork`).
3. Authenticate git: either run `gh auth login` (GitHub CLI, easiest), or create a
   **Fine-grained Personal Access Token** at GitHub → Settings → Developer settings →
   Fine-grained tokens, scoped to my fork with **Contents: Read and write** and
   **Pull requests: Read and write**, and use that token as the password when git prompts
   on push.

Confirm the diagnosis and the diff with me before you push or open the PR.
"""


def build_fix_prompt(
    error_message: str,
    traceback_text: str,
    *,
    action: str = "deploying your project",
    toe: str | None = None,
    target: str | None = None,
    version: str | None = None,
    unsupported=None,
    chops=None,
) -> str:
    """Build the copy-paste agent prompt for a failure or degradation. `unsupported` is
    the list of `FAMILY:optype` TOP operators; `chops` the list of `CHOP:type` control
    operators — either steers the guidance toward adding operator support."""
    parts = []
    if unsupported:
        parts.append(_TOP_GUIDANCE.format(ops=", ".join(f"`{o}`" for o in unsupported)))
    if chops:
        parts.append(_CHOP_GUIDANCE.format(chops=", ".join(f"`{c}`" for c in chops)))
    guidance = "\n\n".join(parts) if parts else _GENERIC_GUIDANCE
    return _TEMPLATE.format(
        action=action,
        error_summary=(error_message or "(no message)").strip().replace("\n", "\n    "),
        traceback=(traceback_text or error_message or "").strip() or "(no traceback captured)",
        repo=REPO_URL,
        toe=toe or "(unknown)",
        target=target or "(unknown)",
        version=version or "(unknown)",
        guidance=guidance,
    )
