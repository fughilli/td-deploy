"""Put the toxc repo on sys.path so the deploy engine can reuse the existing
compiler pipeline (ir/importer/passes/lowering/compiler) exactly as cli.py does.

Works both from the repo checkout and, later, from a PyInstaller bundle where the
compiler modules are collected next to the engine (REPO_ROOT resolves to the
bundle root via sys._MEIPASS in that case).
"""

from __future__ import annotations

import os
import sys

# app/deploy_engine/_paths.py -> repo root is two dirs up.
REPO_ROOT = os.environ.get("TOXC_REPO_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

# When frozen, PyInstaller unpacks the collected repo modules under _MEIPASS.
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    REPO_ROOT = sys._MEIPASS  # type: ignore[attr-defined]


def ensure_on_path() -> str:
    """Idempotently add the repo root + compiler/ to sys.path; return REPO_ROOT."""
    for p in (REPO_ROOT, os.path.join(REPO_ROOT, "compiler")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return REPO_ROOT
