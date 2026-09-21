#!/usr/bin/env python3
"""Generate the "supported operators" list in DEVELOPERS.md from the translator source.

The single source of truth is the importer's dispatch table (`OP_MAP` in
`importer/from_toeexpand.py`) plus the I/O CHOP services it reads live. We parse that
file with `ast` (no import, no deps) so the doc can never drift from the code: add an
`OP_MAP` entry and re-run this, and the table updates.

    python3 tools/gen_supported_ops.py            # rewrite the section in DEVELOPERS.md
    python3 tools/gen_supported_ops.py --check     # exit 1 if the section is stale (CI)

The section is delimited in DEVELOPERS.md by:
    <!-- BEGIN GENERATED: supported-operators -->
    <!-- END GENERATED: supported-operators -->
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
import textwrap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMPORTER = os.path.join(REPO, "importer", "from_toeexpand.py")
DOC = os.path.join(REPO, "DEVELOPERS.md")
BEGIN = "<!-- BEGIN GENERATED: supported-operators -->"
END = "<!-- END GENERATED: supported-operators -->"

# Editorial one-liners keyed by TD op type. The SET of operators is mechanical (from
# OP_MAP); these are just human descriptions. Missing keys render with a blank note, so
# a newly-mapped operator still appears in the table — it just needs a description added.
DESCRIPTIONS = {
    "moviefilein": "Load an image or movie file as a texture.",
    "glsl": "Run a custom GLSL pixel shader.",
    "crop": "Crop / resize the image.",
    "transform": "Translate, rotate, and scale.",
    "level": "Brightness / contrast / gamma / opacity adjustments.",
    "add": "Sum the input images.",
    "math": "Combine inputs, then pre-offset / gain / post-offset.",
    "noise": "Generate gradient noise (approximates TD's noise generators).",
    "feedback": "Echo the previous frame of its Target TOP (feedback loops).",
    "render": "Rasterize a 3D scene (camera + geometry + lights) into a texture.",
    "file": "Read a mesh from disk (`.obj`), baked into the artifact at compile time.",
    "filein": "Read a mesh from disk (`.obj`), baked into the artifact at compile time.",
    "in": "COMP input — structural passthrough after flattening.",
    "out": "COMP output — structural passthrough.",
    "null": "Null / terminator — structural passthrough (often the display node).",
    "oscin": "OSC input, read live and exposed to parameter expressions.",
    "midiin": "MIDI input (notes + control changes), read live for expressions.",
}


def _literal(node: ast.AST):
    """ast.literal_eval a node, tolerating tuple keys and None values."""
    return ast.literal_eval(node)


def parse_op_map(tree: ast.Module) -> list[tuple[str, str, str | None]]:
    """Return [(family, optype, kernel-or-None)] from the OP_MAP assignment."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "OP_MAP" for t in node.targets
        ):
            out = []
            for k, v in zip(node.value.keys, node.value.values):
                family, optype = _literal(k)
                kernel = _literal(v)
                out.append((family, optype, kernel))
            return out
    raise SystemExit("OP_MAP not found in importer/from_toeexpand.py")


def parse_live_services(tree: ast.Module) -> list[str]:
    """Collect optypes compared as live services, e.g. `op.optype in ("oscin","midiin")`."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.In):
            left = node.left
            if (
                isinstance(left, ast.Attribute)
                and left.attr == "optype"
                and isinstance(node.comparators[0], (ast.Tuple, ast.List, ast.Set))
            ):
                for el in node.comparators[0].elts:
                    if isinstance(el, ast.Constant) and isinstance(el.value, str):
                        found.add(el.value)
    return sorted(found)


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Render a GitHub markdown table, column-aligned to match prettier's output (so a
    prettier pass is a no-op and `--check` stays byte-stable)."""
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]
    widths = [max(3, w) for w in widths]

    def row(cells):
        return "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    out = [row(headers), "| " + " | ".join("-" * w for w in widths) + " |"]
    out += [row(r) for r in rows]
    return out


def _para(text: str) -> list[str]:
    """Hard-wrap a paragraph to <=100 cols (markdownlint MD013). prettier's proseWrap is
    'preserve', so these stay put and round-trip cleanly."""
    return textwrap.wrap(" ".join(text.split()), width=100)


def render(op_map, services) -> str:
    def kernel_cell(kernel):
        return f"`{kernel}`" if kernel else "_(passthrough)_"

    lines = [BEGIN, ""]
    note = _para(
        "Generated by tools/gen_supported_ops.py from importer/from_toeexpand.py."
        " Do not edit by hand; run the script to refresh."
    )
    note[0] = "_" + note[0]
    note[-1] = note[-1] + "_"
    lines += note + [""]
    lines += _para(
        "TouchDesigner ships hundreds of operators. td-deploy currently understands the"
        " subset below; any other operator in a project is skipped (degraded to a"
        " passthrough) and reported in the import coverage log."
    )

    lines += ["", "### TOP (image) operators", ""]
    tops = [(o, k) for (f, o, k) in op_map if f == "TOP"]
    lines += _table(
        ["TouchDesigner TOP", "toxc kernel", "Notes"],
        [[f"`{o}`", kernel_cell(k), DESCRIPTIONS.get(o, "")] for o, k in tops],
    )

    # Any non-TOP families that appear in OP_MAP (future-proofing).
    for fam in sorted({f for (f, _o, _k) in op_map if f != "TOP"}):
        rows = [
            [f"`{o}`", kernel_cell(k), DESCRIPTIONS.get(o, "")] for (f, o, k) in op_map if f == fam
        ]
        lines += ["", f"### {fam} operators", ""]
        lines += _table(["TouchDesigner op", "toxc kernel", "Notes"], rows)

    if services:
        lines += ["", "### Live input CHOPs", ""]
        lines += _para("Read at runtime and exposed to parameter expressions.") + [""]
        for svc in services:
            note = DESCRIPTIONS.get(svc, "")
            lines.append(f"- `{svc}` — {note}" if note else f"- `{svc}`")
        lines += [""]
        lines += _para(
            "Parameter expressions on any operator may also reference other CHOPs (e.g."
            " `constant`, math/`absTime` expressions); these are transpiled rather than"
            " mapped as named operators."
        )

    lines += ["", END]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 if DEVELOPERS.md is stale")
    args = ap.parse_args()

    tree = ast.parse(open(IMPORTER, encoding="utf-8").read())
    section = render(parse_op_map(tree), parse_live_services(tree))

    doc = open(DOC, encoding="utf-8").read()
    if BEGIN not in doc or END not in doc:
        raise SystemExit(f"markers not found in {DOC}; add {BEGIN} ... {END}")
    pre, _, rest = doc.partition(BEGIN)
    _, _, post = rest.partition(END)
    new = pre + section + post

    if args.check:
        if new != doc:
            print(
                "DEVELOPERS.md supported-operators section is stale; run "
                "tools/gen_supported_ops.py",
                file=sys.stderr,
            )
            return 1
        print("supported-operators section up to date")
        return 0

    if new != doc:
        open(DOC, "w", encoding="utf-8").write(new)
        print(f"updated supported-operators section in {DOC}")
    else:
        print("supported-operators section already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
