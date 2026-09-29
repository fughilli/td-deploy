"""Load a toeexpand `*.dir` tree into a plain, JSON-serializable network model.

This is the whole network the Python host needs to emulate TouchDesigner's
Python API at runtime — every operator, not just the TOP render path the
importer lowers:

    {
      "ops": {
        "/project1/rig": {
          "family": "COMP", "type": "base", "flags": {...},
          "inputs": ["/project1/..."],            # wired inputs, resolved
          "params": {"tx": {"mode": 0, "val": "0", "expr": null}, ...},
          "custom": [ {name, label, style, page, size, default, menu...}, ...],
          "pages": ["Trickster", ...],
          "text": "...",                          # text/script DAT payload
          "table": [["bone","tx",...], ...],      # table DAT payload
        }, ...
      },
      "start": {"cookrate": 60, "realtime": true},
    }

Formats (as written by TouchDesigner 2025 toeexpand):
  <op>.n       `FAMILY:type`, tile, `flags = ...`, an `inputs { i\\tname }` block.
  <op>.parm    `?`-delimited lines: `name <mode> <value...> ["<expr>"]`. Bit 0 of
               mode = the parameter is in expression mode; bit 4 = Python.
  <op>.cparm   custom parameters: `pages N ...` then one line per parameter:
               `<style> Name "Label" ... <default fields> ... Page <index>`.
  <op>.text    text DAT payload (`2\\n*` + header + big-endian length + bytes).
  <op>.table   table DAT payload (`1\\n*` + u32 ver, rows, cols, 0, then per cell
               u32 kind + u32 length + bytes).
"""

from __future__ import annotations

import os
import shlex
import struct

# --- .n --------------------------------------------------------------------------


def parse_n(text: str) -> tuple[str, str, list[tuple[int, str]], dict]:
    lines = text.splitlines()
    family, _, optype = lines[0].partition(":")
    inputs: list[tuple[int, str]] = []
    flags: dict = {}
    i = 1
    while i < len(lines):
        ln = lines[i].strip()
        if ln.startswith("flags"):
            toks = ln.split("=", 1)[1].split() if "=" in ln else []
            j = 0
            while j < len(toks):
                t = toks[j]
                if j + 1 < len(toks) and toks[j + 1] in ("on", "off"):
                    flags[t] = toks[j + 1] == "on"
                    j += 2
                elif j + 1 < len(toks) and toks[j + 1].lstrip("-").isdigit():
                    flags[t] = int(toks[j + 1])
                    j += 2
                else:
                    flags[t] = True
                    j += 1
        elif ln == "inputs":
            i += 2
            while i < len(lines) and lines[i].strip() != "}":
                parts = lines[i].split("\t")
                if len(parts) >= 2 and parts[0].strip().isdigit():
                    inputs.append((int(parts[0].strip()), parts[1].strip()))
                i += 1
        i += 1
    return family.strip(), optype.strip(), inputs, flags


# --- .parm -----------------------------------------------------------------------


def _split(line: str) -> list[str]:
    """shlex-split a parameter line, tolerating TD's odd quoting (a BOM before a
    quoted label, unbalanced quotes inside Python expressions)."""
    line = line.replace("﻿", "")
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        return line.split()


def parse_parm(text: str) -> dict:
    """name -> {"mode": int, "val": str, "expr": str|None}.

    TD writes a numeric parameter as `name <mode> <value> ["<expr>"]` and keeps
    the expression text around even when the parameter was switched back to
    constant — `mode & 1` says which one is live."""
    params: dict = {}
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln == "?":
            continue
        name, _, rest = ln.partition(" ")
        rest = rest.strip()
        mode_tok, _, rest = rest.partition(" ")
        try:
            mode = int(mode_tok)
        except ValueError:
            # a bare `name value` line (no mode)
            params[name] = {"mode": 0, "val": mode_tok, "expr": None}
            continue
        rest = rest.strip()
        val, expr = rest, None
        toks = _split(rest)
        if len(toks) >= 2:
            # `<value> <expr>`: the value is the first token; the remainder (quoted
            # or not) is the expression text.
            first = rest.split(" ", 1)
            v0 = toks[0]
            tail = first[1].strip() if len(first) > 1 else ""
            if tail.startswith('"') and tail.endswith('"') and len(tail) >= 2:
                tail = tail[1:-1].replace('\\"', '"')
            if rest.startswith('"'):
                # a quoted string value (e.g. `"char/output_unwrapped handleR"`)
                # optionally followed by an expression
                v0 = toks[0]
                qend = _quoted_end(rest)
                tail = rest[qend:].strip()
                if tail.startswith('"') and tail.endswith('"') and len(tail) >= 2:
                    tail = tail[1:-1].replace('\\"', '"')
            val, expr = v0, (tail or None)
        elif len(toks) == 1:
            val = toks[0]
        else:
            val = ""
        # TD quotes any value containing spaces, so an unquoted value is always a
        # single token and whatever follows it is the (possibly stale) expression.
        params[name] = {"mode": mode, "val": val, "expr": expr}
    return params


def _quoted_end(s: str) -> int:
    """Index just past the closing quote of the leading quoted token of `s`."""
    i = 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == '"':
            return i + 1
        i += 1
    return len(s)


def _is_path_or_text(val: str, rest: str) -> bool:
    # constant-mode, unquoted, multi-token: a menu/str value whose trailing tokens
    # are a stale expression is always numeric-first in TD's writer; a non-numeric
    # first token followed by more words is a plain spaced string.
    try:
        float(val)
        return False
    except ValueError:
        return not rest.startswith('"')


# --- .cparm (custom parameters) ---------------------------------------------------

# Low 5 bits of the style word (0x2E1000xx) -> TD parameter style.
_STYLE_BY_LOW = {
    1: "Float",
    2: "Int",
    3: "Toggle",
    4: "Str",
    5: "Pulse",
    15: "Menu",
}
# Tuple styles are identified by the second byte's high nibble (0x23xx RGB, 0x33xx XYZ).
_TUPLE_SUFFIX = {"RGB": "rgb", "RGBA": "rgba", "XYZ": "xyz", "XY": "xy", "UV": "uv", "WH": "wh"}


def _style_of(word: int, nvals: int) -> str:
    low = word & 0x1F
    mid = (word >> 8) & 0xFF
    if nvals > 1:
        if mid & 0xF0 == 0x20:
            return "RGBA" if nvals == 4 else "RGB"
        if mid & 0xF0 == 0x30:
            return "XYZ" if nvals >= 3 else "XY"
        return "Float"
    return _STYLE_BY_LOW.get(low, "Float")


def parse_cparm(text: str) -> tuple[list[str], list[dict]]:
    """Custom parameter definitions. Returns (pages, defs); each def is
    {name, label, style, page, order, size, names, default, menuNames,
    menuLabels, min, max, enableExpr}."""
    pages: list[str] = []
    defs: list[dict] = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln == "?":
            continue
        toks = _split(ln)
        if not toks:
            continue
        if toks[0] == "pages":
            pages = toks[2:]
            continue
        try:
            word = int(toks[0])
        except ValueError:
            continue
        name, label = toks[1], toks[2]
        rest = toks[3:]
        d = _parse_cparm_rest(word, name, label, rest)
        if d:
            defs.append(d)
    return pages, defs


def _num(t: str, default=0.0):
    try:
        return float(t)
    except (TypeError, ValueError):
        return default


def _parse_cparm_rest(word: int, name: str, label: str, rest: list[str]) -> dict | None:
    # Layout observed: `<n1> <n2> <clampmin> <min> <n3> <n4> <max>` per component
    # group, then per component `2 <default> <exprA> <exprB>` (numeric styles) or
    # `2 0 <default> <x>` (string/menu styles), then `Page [menu block] <order>
    # [enableExpr]`. Find the value blocks by their leading `2` marker.
    low = word & 0x1F
    vals: list[str] = []
    i = 0
    rng = []
    # range header: everything up to the first `2` that starts a value block
    while i < len(rest) and not (rest[i] == "2" and i >= 7):
        rng.append(rest[i])
        i += 1
    string_like = low in (4, 15)
    while i < len(rest) and rest[i] == "2":
        if string_like:
            vals.append(rest[i + 2] if i + 2 < len(rest) else "")
        else:
            vals.append(rest[i + 1] if i + 1 < len(rest) else "0")
        i += 4
    page = rest[i] if i < len(rest) else ""
    i += 1
    menu_names, menu_labels = [], []
    if low == 15 and i < len(rest) and rest[i] == "4097":
        n = int(rest[i + 1])
        i += 2
        for k in range(n):
            menu_names.append(rest[i + 2 * k])
            menu_labels.append(rest[i + 2 * k + 1])
        i += 2 * n
    order = int(_num(rest[i], 0)) if i < len(rest) else 0
    enable_expr = " ".join(rest[i + 1 :]) or None
    size = max(1, len(vals))
    style = _style_of(word, size)
    if size > 1:
        suffix = _TUPLE_SUFFIX.get(style, "1234")
        if len(suffix) < size:
            suffix = "1234567890"[:size]
        names = [name + suffix[k] for k in range(size)]
    else:
        names = [name]
    mn = _num(rng[3], 0.0) if len(rng) > 3 else 0.0
    mx = _num(rng[6], 1.0) if len(rng) > 6 else 1.0
    if string_like:
        default = vals[0] if vals else ""
    elif style in ("Int", "Toggle", "Pulse"):
        default = [int(_num(v)) for v in vals] or [0]
    else:
        default = [_num(v) for v in vals] or [0.0]
    return {
        "name": name,
        "label": label,
        "style": style,
        "page": page,
        "order": order,
        "size": size,
        "names": names,
        "default": default,
        "menuNames": menu_names,
        "menuLabels": menu_labels,
        "min": mn,
        "max": mx,
        "enableExpr": enable_expr,
    }


# --- DAT payloads ------------------------------------------------------------------


def strip_dat_text(raw: bytes) -> str:
    star = raw.find(b"*")
    if star != -1:
        for p in range(star + 1, min(star + 64, len(raw) - 4)):
            (n,) = struct.unpack(">I", raw[p : p + 4])
            if p + 4 + n == len(raw):
                return raw[p + 4 :].decode("utf-8", "replace")
    body = raw.split(b"\n", 1)[-1]
    return body.lstrip(b"*").decode("utf-8", "replace")


def parse_table(raw: bytes) -> list[list[str]]:
    star = raw.find(b"*")
    if star == -1:
        return []
    p = star + 1
    try:
        _ver, rows, cols, _z = struct.unpack(">IIII", raw[p : p + 16])
    except struct.error:
        return []
    p += 16
    cells: list[str] = []
    while p + 8 <= len(raw) and len(cells) < rows * cols:
        _kind, n = struct.unpack(">II", raw[p : p + 8])
        p += 8
        cells.append(raw[p : p + n].decode("utf-8", "replace"))
        p += n
    return [cells[r * cols : (r + 1) * cols] for r in range(rows)]


# --- tree ----------------------------------------------------------------------------


def _resolve(parent_dir: str, name: str) -> str:
    joined = name if (parent_dir == "" or name.startswith("/")) else parent_dir + "/" + name
    parts: list[str] = []
    for seg in joined.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
        else:
            parts.append(seg)
    return "/" + "/".join(parts)


def _parse_start(dirroot: str) -> dict:
    out = {"cookrate": 60.0, "realtime": True}
    fn = os.path.join(dirroot, ".start")
    if os.path.isfile(fn):
        with open(fn) as fh:
            for ln in fh:
                t = ln.split()
                if len(t) >= 2 and t[0] == "cookrate":
                    out["cookrate"] = _num(t[1], 60.0)
                elif len(t) >= 2 and t[0] == "realtime":
                    out["realtime"] = t[1] == "on"
    return out


def load_network(dirroot: str) -> dict:
    """Parse the whole expanded tree into the host's network model."""
    ops: dict[str, dict] = {}
    for dp, _dn, fnames in os.walk(dirroot):
        for fn in fnames:
            if not fn.endswith(".n"):
                continue
            base = fn[:-2]
            rel = os.path.relpath(os.path.join(dp, base), dirroot).replace(os.sep, "/")
            if rel.startswith("."):
                continue
            path = "/" + rel
            with open(os.path.join(dp, fn), encoding="utf-8", errors="replace") as fh:
                family, optype, inputs, flags = parse_n(fh.read())
            parent = path.rsplit("/", 1)[0] or "/"
            rec = {
                "family": family,
                "type": optype,
                "flags": flags,
                "inputs": [
                    _resolve(parent if parent != "/" else "", nm) for _i, nm in sorted(inputs)
                ],
                "params": {},
                "custom": [],
                "pages": [],
            }
            stem = os.path.join(dp, base)
            if os.path.isfile(stem + ".parm"):
                with open(stem + ".parm", encoding="utf-8", errors="replace") as fh:
                    rec["params"] = parse_parm(fh.read())
            if os.path.isfile(stem + ".cparm"):
                with open(stem + ".cparm", encoding="utf-8", errors="replace") as fh:
                    rec["pages"], rec["custom"] = parse_cparm(fh.read())
            if os.path.isfile(stem + ".text"):
                with open(stem + ".text", "rb") as fh:
                    rec["text"] = strip_dat_text(fh.read())
            if os.path.isfile(stem + ".table"):
                with open(stem + ".table", "rb") as fh:
                    rec["table"] = parse_table(fh.read())
            ops[path] = rec
    return {"ops": ops, "start": _parse_start(dirroot)}
