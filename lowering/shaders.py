"""
GLSL generation + the desktop-GL <-> GLES targeting shim (docs §5 "lowering").

The Pi runs GLES 3.1 (`#version 310 es`, explicit precision); the host reference
runtime runs desktop core GL (`#version 330 core`). Same shader body, different
header — this module is where that translation lives. Real custom-GLSL TOPs will
later route TD desktop-GLSL through glslang->SPIR-V->SPIRV-Cross; the built-in
kernels below are authored portably so they need only header swapping.
"""
from __future__ import annotations

TARGETS = {"desktop_gl", "gles"}


def _header(target: str, stage: str) -> str:
    if target == "gles":
        h = "#version 310 es\n"
        if stage == "fragment":
            h += "precision highp float;\nprecision highp sampler2D;\n"
        return h
    return "#version 330 core\n"


# Fullscreen-triangle vertex shader (no vertex buffers; positions from gl_VertexID).
def vertex(target: str) -> str:
    return _header(target, "vertex") + """
out vec2 vUV;
void main() {
    vec2 uv = vec2((gl_VertexID == 1) ? 2.0 : 0.0,
                   (gl_VertexID == 2) ? 2.0 : 0.0);
    vUV = uv;
    gl_Position = vec4(uv * 2.0 - 1.0, 0.0, 1.0);
}
"""


def vertex_td(target: str) -> str:
    """TD GLSL TOPs expect `vec3 vUV` (they use vUV.st)."""
    return _header(target, "vertex") + """
out vec3 vUV;
void main() {
    vec2 uv = vec2((gl_VertexID == 1) ? 2.0 : 0.0,
                   (gl_VertexID == 2) ? 2.0 : 0.0);
    vUV = vec3(uv, 0.0);
    gl_Position = vec4(uv * 2.0 - 1.0, 0.0, 1.0);
}
"""


def td_glsl_top(target: str, n_inputs: int, user_src: str) -> str:
    """Wrap a TouchDesigner GLSL-TOP pixel shader with the minimal TD compat
    prelude so it runs on plain GL/GLES. TD builtins covered here (grow as
    coverage requires): sTD2DInputs[], uTD2DInfos[].res = vec4(1/w,1/h,w,h),
    vUV (vec3), TDOutputSwizzle(). The user shader supplies `out vec4 fragColor`
    and main()."""
    n = max(1, n_inputs)
    prelude = f"""
in vec3 vUV;
uniform sampler2D sTD2DInputs[{n}];
struct TDInfo {{ vec4 res; }};
uniform TDInfo uTD2DInfos[{n}];
vec4 TDOutputSwizzle(vec4 c) {{ return c; }}
"""
    return _header(target, "fragment") + prelude + "\n" + user_src


def gaussian_blur(target: str, radius: int, weights: list[float]) -> str:
    """2D gaussian as a separable-weighted (2R+1)^2 kernel; weights baked as a
    compile-time const array (the folded sigma). Clamp-to-edge is set on the
    sampler so out-of-range taps replicate the border."""
    wlit = ", ".join(f"{w:.9g}" for w in weights)
    n = 2 * radius + 1
    return _header(target, "fragment") + f"""
in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
uniform vec2 uResolution;
const int R = {radius};
const float W[{n}] = float[]({wlit});
void main() {{
    vec2 texel = 1.0 / uResolution;
    vec4 acc = vec4(0.0);
    for (int j = -R; j <= R; ++j) {{
        for (int i = -R; i <= R; ++i) {{
            float w = W[i + R] * W[j + R];
            acc += w * texture(tex0, vUV + vec2(float(i), float(j)) * texel);
        }}
    }}
    fragColor = acc;
}}
"""


# Trivial passthrough (used by the sink to sample its input into the readback FBO).
def passthrough(target: str) -> str:
    return _header(target, "fragment") + """
in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
void main() { fragColor = texture(tex0, vUV); }
"""
