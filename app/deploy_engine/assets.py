"""Locate an image_in's file on disk, with configurable roots + explicit substitutions.

Kept dependency-free (stdlib only) so it's unit-testable in isolation and carries no
import-time cost. `resolve_local_asset` returns both the resolved path (or None) and the
list of directories it searched, so a "not found" can be made actionable in the UI.
"""

from __future__ import annotations

import os


def resolve_local_asset(p, toe_path, roots=(), asset_map=None):
    """Return ``(resolved_abs_path | None, searched_dirs)`` for asset path ``p``.

    An explicit substitution wins first (``asset_map`` keyed by the exact path OR the
    basename). Otherwise — because TouchDesigner stores movie paths relative to the
    project, not the app's CWD — search CWD, the .toe's own dir and its parent, and any
    configured extra ``roots``, trying both the full relative path and the bare basename
    in each. ``searched_dirs`` lists the directories tried (for a "not found" message)."""
    asset_map = asset_map or {}
    for key in (p, os.path.basename(p)):
        repl = asset_map.get(key)
        if repl and os.path.isfile(repl):
            return os.path.abspath(repl), []

    if os.path.isfile(p):
        return os.path.abspath(p), []

    toedir = os.path.dirname(os.path.abspath(toe_path)) if toe_path else os.getcwd()
    bases = [os.getcwd(), toedir, os.path.dirname(toedir)]
    bases += [os.path.abspath(r) for r in roots]
    searched, seen = [], set()
    for b in bases:
        if b not in searched:
            searched.append(b)
        for cand in (os.path.join(b, p), os.path.join(b, os.path.basename(p))):
            cand = os.path.abspath(cand)
            if cand not in seen and os.path.isfile(cand):
                return cand, searched
            seen.add(cand)
    return None, searched
