"""
Translate an artifact's shaders to GLSL ES 1.00 (OpenGL ES 2.0) for the Pi3's
VideoCore-IV hardware, which is GLES2-only. Pipeline per fragment shader:

  desktop GLSL -> inject layout(location/binding) -> glslang (-G) -> SPIR-V
  -> spirv-cross --es --version 100 -> legalize integer % (ES100 lacks it)

Fragment shaders come from TD/our lowering (they only need the location injection +
% legalization). Vertex shaders are ours; ES2 has no gl_VertexID, so we emit an
attribute-based fullscreen-quad vertex shader matching each fragment's vUV arity.

Writes <artifact>/shaders_gles/<id>.{vert,frag}. Run in a shell with glslang +
spirv-cross + python3 (see build_shaders_gles.sh).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys


def inject_locations(src: str) -> str:
    """version->450 and add layout(location=N)/binding=M to user in/out + default
    uniforms so glslang can emit OpenGL SPIR-V."""
    out, loc, binding = [], 0, 0
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("#version"):
            out.append("#version 450")
            continue
        m = re.match(r"^(in|out)\s+(vec\d)\s+(\w+);", s)
        if m:
            out.append(f"layout(location=0) {s}")
            continue
        m = re.match(r"^uniform\s+sampler\w+\s+(\w+)(\[\d+\])?;", s)
        if m:
            out.append(f"layout(binding={binding}) {s}")
            binding += 1
            continue
        m = re.match(r"^uniform\s+\w+\s+(\w+)(\[\d+\])?;", s)
        if m:
            out.append(f"layout(location={loc}) {s}")
            loc += 1
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def _match_operand_left(t: str, i: int) -> int:
    """Return start index of the operand ending at i (just before ' %')."""
    j = i
    while j > 0 and t[j - 1] == " ":
        j -= 1
    if j > 0 and t[j - 1] == ")":
        depth = 0
        while j > 0:
            j -= 1
            if t[j] == ")":
                depth += 1
            elif t[j] == "(":
                depth -= 1
                if depth == 0:
                    break
        # include a preceding identifier (e.g. int(...) )
        k = j
        while k > 0 and (t[k - 1].isalnum() or t[k - 1] == "_"):
            k -= 1
        return k
    while j > 0 and (t[j - 1].isalnum() or t[j - 1] in "_."):
        j -= 1
    return j


def _match_operand_right(t: str, i: int) -> int:
    j = i
    while j < len(t) and t[j] == " ":
        j += 1
    if j < len(t) and t[j] == "(":
        depth = 0
        while j < len(t):
            if t[j] == "(":
                depth += 1
            elif t[j] == ")":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        return j
    while j < len(t) and (t[j].isalnum() or t[j] in "_."):
        j += 1
    return j


def legalize_modulo(src: str) -> str:
    """Replace integer `a % b` with imod(a,b) (ES100 has no %)."""
    if " % " not in src:
        return src
    t = src
    while " % " in t:
        i = t.index(" % ")
        ls = _match_operand_left(t, i)
        re_ = _match_operand_right(t, i + 3)
        left, right = t[ls:i], t[i + 3 : re_]
        t = t[:ls] + f"imod({left.strip()}, {right.strip()})" + t[re_:]
    helper = "int imod(int a, int b) { return a - (a / b) * b; }\n"
    # insert helper after the precision lines
    lines = t.splitlines(keepends=True)
    ins = 0
    for k, ln in enumerate(lines):
        if ln.startswith("precision") or ln.startswith("#version"):
            ins = k + 1
    lines.insert(ins, helper)
    return "".join(lines)


def es2_vertex(vec3: bool) -> str:
    ty = "vec3" if vec3 else "vec2"
    uv = "vec3(aPos * 0.5 + 0.5, 0.0)" if vec3 else "(aPos * 0.5 + 0.5)"
    return (
        "#version 100\n"
        "attribute vec2 aPos;\n"
        f"varying {ty} vUV;\n"
        "void main() {\n"
        f"    vUV = {uv};\n"
        "    gl_Position = vec4(aPos, 0.0, 1.0);\n"
        "}\n"
    )


def translate_frag(path: str) -> str:
    src = inject_locations(open(path).read())
    tmp = "/tmp/_gles_in.frag"
    open(tmp, "w").write(src)
    subprocess.run(
        ["glslangValidator", "-G", tmp, "-o", "/tmp/_gles.spv"], check=True, capture_output=True
    )
    es = subprocess.run(
        ["spirv-cross", "/tmp/_gles.spv", "--es", "--version", "100"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return legalize_modulo(es)


def main(art=None):
    art = art or sys.argv[1]
    sched = json.load(open(f"{art}/schedule.json"))
    outdir = f"{art}/shaders_gles"
    os.makedirs(outdir, exist_ok=True)
    n = 0
    for st in sched["steps"]:
        if st["kind"] != "shader":
            continue
        sid = st["id"].replace("/", "_")
        vsrc = open(f"{art}/{st['vert']}").read()
        vec3 = "vec3 vUV" in vsrc
        open(f"{outdir}/{sid}.vert", "w").write(es2_vertex(vec3))
        open(f"{outdir}/{sid}.frag", "w").write(translate_frag(f"{art}/{st['frag']}"))
        n += 1
    print(f"[gles] translated {n} shaders -> {outdir}", file=sys.stderr)


if __name__ == "__main__":
    main()
