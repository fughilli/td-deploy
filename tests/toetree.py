"""Build tiny toeexpand-style `*.dir` trees for tests, without TouchDesigner.

    t = ToeTree(tmp)
    t.op("project1/rig", "COMP:base", params={"Solve": (67108928, "1")},
         custom=['772804867 Solve Solve 1 1 0 0 1 1 1 2 1 "" "" Main 0'])
    t.op("project1/glsl1", "TOP:glsl", inputs=["noise1"], flags="display on")
    t.text("project1/core", "def f():\\n    return 1\\n")

Writes <path>.n (family:type, flags, inputs block), <path>.parm (`name mode value
[expr]`), <path>.cparm and <path>.text / .table payloads in toeexpand's formats.
"""

from __future__ import annotations

import os
import struct


def _q(v: str) -> str:
    return f'"{v}"' if (" " in v or v == "") else v


class ToeTree:
    def __init__(self, root: str, name: str = "test.toe"):
        self.dir = os.path.join(root, name + ".dir")
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, rel):
        p = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def op(self, rel, kind, *, inputs=(), flags="", params=None, custom=None, pages=None):
        lines = [kind, "tile 0 0 130 90", f"flags =  {flags} parlanguage 0".replace("  ", " ", 1)]
        if inputs:
            lines += ["inputs", "{"] + [f"{i} \t{n}" for i, n in enumerate(inputs)] + ["}"]
        lines += ["color 0.67 0.67 0.67 ", "end"]
        with open(self._path(rel + ".n"), "w") as fh:
            fh.write("\n".join(lines) + "\n")
        if params:
            out = ["?"]
            for k, v in params.items():
                if isinstance(v, tuple):
                    mode, val, *expr = v
                    tail = f' "{expr[0]}"' if expr else ""
                    out.append(f"{k} {mode} {_q(str(val))}{tail}")
                else:
                    out.append(f"{k} 0 {_q(str(v))}")
            out.append("?")
            with open(self._path(rel + ".parm"), "w") as fh:
                fh.write("\n".join(out) + "\n")
        if custom:
            pg = pages or sorted(
                {ln.split()[-2] if ln.split()[-1].isdigit() else "Custom" for ln in custom}
            )
            body = ["?", f"pages {len(pg)} " + " ".join(_q(p) for p in pg)] + list(custom) + ["?"]
            with open(self._path(rel + ".cparm"), "w") as fh:
                fh.write("\n".join(body) + "\n")
        return self

    def text(self, rel, src, kind="DAT:text", params=None):
        self.op(rel, kind, params=params)
        body = src.encode()
        with open(self._path(rel + ".text"), "wb") as fh:
            fh.write(
                b"2\n*" + struct.pack(">IIII", 1, 1, 1, 1) + struct.pack(">I", len(body)) + body
            )
        return self

    def table(self, rel, rows):
        self.op(rel, "DAT:table")
        cols = max(len(r) for r in rows)
        buf = b"1\n*" + struct.pack(">IIII", 1, len(rows), cols, 0)
        for r in rows:
            for c in range(cols):
                cell = (r[c] if c < len(r) else "").encode()
                buf += struct.pack(">II", 2, len(cell)) + cell
        with open(self._path(rel + ".table"), "wb") as fh:
            fh.write(buf)
        return self
