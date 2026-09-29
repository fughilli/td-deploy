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
HOST_COMPILER = os.path.join(REPO, "compiler", "host_compile.py")
DOC = os.path.join(REPO, "DEVELOPERS.md")
BEGIN = "<!-- BEGIN GENERATED: supported-operators -->"
END = "<!-- END GENERATED: supported-operators -->"
HOST_BEGIN = "<!-- BEGIN GENERATED: python-host-operators -->"
HOST_END = "<!-- END GENERATED: python-host-operators -->"

# Python-host (x86_64 player) notes. The SETS of TOP/SOP types come from the host
# compiler's dispatch (HostCompiler.compile_node / _sop_recipe, parsed with ast);
# the other families are handled structurally (render scene, Python), listed here.
HOST_TOP_NOTES = {
    "null": "Pass through.",
    "out": "Pass through (COMP output).",
    "in": "COMP input (blank when unwired).",
    "select": "Another TOP by reference.",
    "renderselect": "One colour buffer of a Render TOP (MRT).",
    "depth": "A Render TOP's depth.",
    "script": "numpy frames from a Script TOP's onCook, uploaded each frame.",
    "moviefilein": "A still image file, as a texture.",
    "importselect": "A texture referenced by an FBX COMP.",
    "glsl": "Custom GLSL (TD's GLSL TOP prelude, uniforms, MRT, extra inputs).",
    "render": "Scenes: Geometry COMPs, cameras, lights; Phong (skinned, instanced, maps).",
    "blur": "Gaussian blur.",
    "fit": "Fit/fill/stretch into a resolution.",
    "flip": "Flip X/Y, flop.",
    "resolution": "Resample.",
    "composite": "Multi-input composite (over, add, multiply, ...).",
    "level": "Brightness / gamma / contrast / opacity.",
    "feedback": "The previous frame of a TOP.",
}
HOST_SOP_NOTES = {
    "null": "Pass through.",
    "bonegroup": "Pass through (skinning comes from the FBX clusters).",
    "out": "Pass through.",
    "in": "Pass through.",
    "select": "Another SOP by reference.",
    "transform": "Static transform, folded into the mesh.",
    "importselect": "A mesh inside an FBX COMP (skinned: the bones follow their COMPs).",
    "script": "Points/polys from a Script SOP's onCook, re-uploaded when they change.",
}
HOST_OTHER = [
    (
        "COMP",
        "`geometry`, `camera`, `light`, `ambientlight`, `null`, `fbx`, `base`, `window`",
        "Object transforms (incl. parenting), instancing from CHOPs, the Window COMP's output.",
    ),
    ("MAT", "`phong`", "Diffuse/normal/colour/alpha maps, alpha test, point colour."),
    ("CHOP", "`script`", "Script CHOP channels (instancing, expressions)."),
    (
        "DAT",
        "`execute`, `parexec`, `text`, `table`",
        "Execute / Parameter Execute callbacks run as in TD; Text DATs are modules"
        " (`op(...).module`, `mod`), file-synced ones re-read from the project.",
    ),
]

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


def _compared_strings(fn: ast.AST, var: str = "t") -> list[str]:
    """String constants `var` is compared against (==, in) inside `fn`, in source order."""
    found: list[tuple[int, int, str]] = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == var
        ):
            for comp in node.comparators:
                vals = comp.elts if isinstance(comp, (ast.Tuple, ast.List, ast.Set)) else [comp]
                for v in vals:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        found.append((v.lineno, v.col_offset, v.value))
    out: list[str] = []
    for _, _, name in sorted(found):
        if name not in out:
            out.append(name)
    return out


def parse_host_compiler(tree: ast.Module) -> tuple[list[str], list[str]]:
    """(TOP types, SOP types) the Python-host compiler handles."""
    tops: list[str] = []
    sops: list[str] = []
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef) and cls.name == "HostCompiler":
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == "compile_node":
                    tops = _compared_strings(fn)
                if isinstance(fn, ast.FunctionDef) and fn.name == "_sop_recipe":
                    sops = _compared_strings(fn)
    return tops, sops


def render_host(tops: list[str], sops: list[str]) -> str:
    lines = [HOST_BEGIN, ""]
    note = _para(
        "Generated by tools/gen_supported_ops.py from compiler/host_compile.py."
        " Do not edit by hand; run the script to refresh."
    )
    note[0] = "_" + note[0]
    note[-1] = note[-1] + "_"
    lines += note + [""]
    lines += _para(
        "A project that runs Python TouchDesigner can't compile away (Execute DATs,"
        " Script operators, Python expressions over storage/modules) deploys to an"
        " x86_64 player as a Python-host artifact: the project's Python runs against"
        " pyhost's TouchDesigner API emulation and the runtime renders what it binds."
        " Anything else is passed through and reported in the coverage log."
    )
    lines += ["", "### TOPs", ""]
    lines += _table(
        ["TouchDesigner TOP", "Notes"], [[f"`{t}`", HOST_TOP_NOTES.get(t, "")] for t in tops]
    )
    lines += ["", "### SOPs (render geometry)", ""]
    lines += _table(
        ["TouchDesigner SOP", "Notes"], [[f"`{t}`", HOST_SOP_NOTES.get(t, "")] for t in sops]
    )
    lines += ["", "### Other families", ""]
    lines += _table(["Family", "Operators", "Notes"], [list(r) for r in HOST_OTHER])
    lines += ["", HOST_END]
    return "\n".join(lines)


def _splice(doc: str, begin: str, end: str, section: str) -> str:
    if begin not in doc or end not in doc:
        raise SystemExit(f"markers not found in {DOC}; add {begin} ... {end}")
    pre, _, rest = doc.partition(begin)
    _, _, post = rest.partition(end)
    return pre + section + post


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 if DEVELOPERS.md is stale")
    args = ap.parse_args()

    tree = ast.parse(open(IMPORTER, encoding="utf-8").read())
    section = render(parse_op_map(tree), parse_live_services(tree))

    host = render_host(
        *parse_host_compiler(ast.parse(open(HOST_COMPILER, encoding="utf-8").read()))
    )

    doc = open(DOC, encoding="utf-8").read()
    new = _splice(doc, BEGIN, END, section)
    new = _splice(new, HOST_BEGIN, HOST_END, host)

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
