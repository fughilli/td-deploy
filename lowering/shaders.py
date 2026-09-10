"""
GLSL generation + the desktop-GL <-> GLES targeting shim (docs §5 "lowering").

The Pi runs GLES 3.1 (`#version 310 es`, explicit precision); the host reference
runtime runs desktop core GL (`#version 330 core`). Same shader body, different
header — this module is where that translation lives. Real custom-GLSL TOPs will
later route TD desktop-GLSL through glslang->SPIR-V->SPIRV-Cross; the built-in
kernels below are authored portably so they need only header swapping.
"""
from __future__ import annotations

# "gles2" emits desktop GLSL (330) as the *input* to the ES1.00 translator
# (compiler/translate_gles.py); the schedule target tells the runtime to use ES2.
TARGETS = {"desktop_gl", "gles", "gles2"}


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


def crop_top(target: str) -> str:
    """Crop TOP: sample a sub-rectangle of the input (normalized left/right/
    bottom/top) and rescale it to fill the output. uCropRect = (l, r, b, t)."""
    return _header(target, "fragment") + """
in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
uniform vec4 uCropRect;   // (left, right, bottom, top) in input UV
void main() {
    vec2 uv = vec2(mix(uCropRect.x, uCropRect.y, vUV.x),
                   mix(uCropRect.z, uCropRect.w, vUV.y));
    fragColor = texture(tex0, uv);
}
"""


def crop_transform_top(target: str) -> str:
    """Fused Crop->Transform: transform the screen UV, apply the crop remap, then
    sample the SOURCE once. Exact composition of crop_top feeding transform_top —
    one pass and one FBO instead of two (producer/consumer fusion of two
    single-tap coordinate-remap ops)."""
    return _header(target, "fragment") + """
in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;      // the source (crop's input)
uniform vec4 uCropRect;      // (left, right, bottom, top) in source UV
uniform float uRotate;       // radians
uniform float uTranslateX;
uniform float uTranslateY;
uniform float uScaleX;
uniform float uScaleY;
void main() {
    // transform: screen UV -> crop-output UV
    vec2 c = vec2(0.5);
    vec2 p = vUV - c - vec2(uTranslateX, uTranslateY);
    float s = sin(-uRotate), co = cos(-uRotate);
    p = mat2(co, -s, s, co) * p;
    p /= vec2(uScaleX, uScaleY);
    p = p + c;
    // crop: crop-output UV -> source UV
    vec2 uv = vec2(mix(uCropRect.x, uCropRect.y, p.x),
                   mix(uCropRect.z, uCropRect.w, p.y));
    fragColor = texture(tex0, uv);
}
"""


def transform_top(target: str) -> str:
    """Transform TOP: translate/rotate/scale about a pivot. Samples the input at
    the inverse-transformed UV (clamp-to-edge outside)."""
    # Per-component scalar uniforms (not vec2) so each can be a per-frame
    # expression (TD parameters like scale = op('midiin1')[1]/64). One decl per
    # line for the GLES translator's per-uniform layout injection.
    return _header(target, "fragment") + """
in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
uniform float uRotate;       // radians
uniform float uTranslateX;
uniform float uTranslateY;
uniform float uScaleX;
uniform float uScaleY;
void main() {
    vec2 c = vec2(0.5);
    vec2 p = vUV - c - vec2(uTranslateX, uTranslateY);
    float s = sin(-uRotate), co = cos(-uRotate);
    p = mat2(co, -s, s, co) * p;
    p /= vec2(uScaleX, uScaleY);
    fragColor = texture(tex0, p + c);
}
"""


# Trivial passthrough (used by the sink to sample its input into the readback FBO).
def passthrough(target: str) -> str:
    return _header(target, "fragment") + """
in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
void main() { fragColor = texture(tex0, vUV); }
"""
