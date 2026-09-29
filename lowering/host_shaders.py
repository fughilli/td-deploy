"""GLSL for the Python-host schedule (desktop GL 3.3 core; Mesa/Intel/AMD/NVIDIA).

Two families:
  * full-screen TOP passes (TouchDesigner GLSL TOPs wrapped in TD's compat
    prelude, plus the built-in TOPs this path implements: Blur, Fit, Flip,
    Resolution, Composite);
  * the scene shader behind Render TOPs: TouchDesigner's Phong MAT lighting
    (point lights with TD's attenuation curve, ambient lights, diffuse / normal /
    colour / alpha maps, alpha test, point colour), with optional GPU skinning
    and CHOP instancing, specialised per draw with #defines.
"""

from __future__ import annotations

HEADER = "#version 330 core\n"

FULLSCREEN_VERT = (
    HEADER
    + """
// One oversized triangle covering the viewport; no vertex buffer needed.
out vec3 vUV;
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    vUV = vec3(p, 0.0);
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""
)


def td_glsl_top(user_src: str, n_inputs: int) -> str:
    """TouchDesigner GLSL TOP compatibility prelude + the user's pixel shader.

    Covers the TD builtins projects lean on: sTD2DInputs[] / uTD2DInfos[] (res =
    1/w, 1/h, w, h), uTDOutputInfo, vUV, TDOutputSwizzle, TDDither and TD's
    colour helpers. The user source declares its own `out` variables (with
    `layout(location = N)` for multiple colour buffers) and main()."""
    n = max(1, n_inputs)
    body = _strip_version(user_src)
    return (
        HEADER
        + f"""
#define TD_NUM_2D_INPUTS {n_inputs}
in vec3 vUV;
uniform sampler2D sTD2DInputs[{n}];
struct TDTexInfo {{ vec4 res; vec4 depth; }};
uniform TDTexInfo uTD2DInfos[{n}];
uniform TDTexInfo uTDOutputInfo;
uniform float uTDCurrentDepth;
vec4 TDOutputSwizzle(vec4 c) {{ return c; }}
uvec4 TDOutputSwizzle(uvec4 c) {{ return c; }}
vec4 TDDither(vec4 c) {{ return c; }}
float TDLuminance(vec3 c) {{ return dot(c, vec3(0.2126, 0.7152, 0.0722)); }}
vec3 TDHSVToRGB(vec3 c) {{
    vec4 K = vec4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}}
vec3 TDRGBToHSV(vec3 c) {{
    vec4 K = vec4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    float e = 1.0e-10;
    return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}}
#line 1
"""
        + body
    )


def _strip_version(src: str) -> str:
    out = []
    for ln in src.splitlines():
        if ln.strip().startswith("#version"):
            out.append("// " + ln)
        else:
            out.append(ln)
    return "\n".join(out) + "\n"


def blur_top() -> str:
    """Separable-ish Gaussian: one 2D pass whose tap count follows the radius at
    runtime (uRadius in output pixels; sigma = radius / 2)."""
    return (
        HEADER
        + """
in vec3 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
uniform vec2 uTexel;      // 1/input size
uniform float uRadius;    // blur size in INPUT pixels
void main() {
    float r = max(uRadius, 0.0);
    if (r < 0.5) { fragColor = texture(tex0, vUV.st); return; }
    float sigma = max(r * 0.5, 0.5);
    // sample on a grid of at most 21x21 taps spanning +-r
    int n = int(min(ceil(r), 10.0));
    float step_ = r / float(max(n, 1));
    vec4 acc = vec4(0.0);
    float wsum = 0.0;
    for (int j = -10; j <= 10; ++j) {
        if (abs(j) > n) continue;
        for (int i = -10; i <= 10; ++i) {
            if (abs(i) > n) continue;
            vec2 o = vec2(float(i), float(j)) * step_;
            float w = exp(-dot(o, o) / (2.0 * sigma * sigma));
            acc += w * texture(tex0, vUV.st + o * uTexel);
            wsum += w;
        }
    }
    fragColor = acc / wsum;
}
"""
    )


def fit_top() -> str:
    """Fit TOP: place the input in the output keeping (or not) its aspect.
    uMode: 0 fill (stretch), 1 fit horizontal, 2 fit vertical, 3 fit best,
    4 fit outside, 5 native resolution."""
    return (
        HEADER
        + """
in vec3 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
uniform vec2 uIn;    // input size (px)
uniform vec2 uOut;   // output size (px)
uniform int uMode;
uniform vec4 uBg;
void main() {
    vec2 s;  // input size in output pixels
    float ia = uIn.x / uIn.y, oa = uOut.x / uOut.y;
    if (uMode == 0) s = uOut;
    else if (uMode == 1) s = vec2(uOut.x, uOut.x / ia);
    else if (uMode == 2) s = vec2(uOut.y * ia, uOut.y);
    else if (uMode == 3) s = (ia > oa) ? vec2(uOut.x, uOut.x / ia) : vec2(uOut.y * ia, uOut.y);
    else if (uMode == 4) s = (ia < oa) ? vec2(uOut.x, uOut.x / ia) : vec2(uOut.y * ia, uOut.y);
    else s = uIn;
    vec2 p = vUV.st * uOut;
    vec2 uv = (p - 0.5 * (uOut - s)) / s;
    if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0) fragColor = uBg;
    else fragColor = texture(tex0, uv);
}
"""
    )


def flip_top() -> str:
    return (
        HEADER
        + """
in vec3 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
uniform vec3 uFlip;  // x: flip x, y: flip y, z: flop (swap x/y)
void main() {
    vec2 uv = vUV.st;
    // In sample space TD transposes first (flop), then flips — checked against
    // TD: flop+flipx turns the image 90 degrees clockwise.
    if (uFlip.z > 0.5) uv = uv.yx;
    if (uFlip.x > 0.5) uv.x = 1.0 - uv.x;
    if (uFlip.y > 0.5) uv.y = 1.0 - uv.y;
    fragColor = texture(tex0, uv);
}
"""
    )


def resolution_top() -> str:
    """Resolution TOP: resample; a box filter over the input pixels each output
    pixel covers (TD's 'mipmap' input filter for large downscales)."""
    return (
        HEADER
        + """
in vec3 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
uniform vec2 uIn;
uniform vec2 uOut;
void main() {
    vec2 k = max(uIn / uOut, vec2(1.0));
    int nx = int(min(k.x, 16.0)), ny = int(min(k.y, 16.0));
    vec2 base = vUV.st - 0.5 / uOut;  // lower-left of this output pixel, in uv
    vec4 acc = vec4(0.0);
    for (int j = 0; j < 16; ++j) {
        if (j >= ny) break;
        for (int i = 0; i < 16; ++i) {
            if (i >= nx) break;
            vec2 uv = base + (vec2(float(i), float(j)) + 0.5) / vec2(float(nx), float(ny)) / uOut;
            acc += texture(tex0, uv);
        }
    }
    fragColor = acc / float(nx * ny);
}
"""
    )


def composite_top(n_inputs: int, operand: str) -> str:
    """Composite TOP with premultiplied 'over' / 'under' / 'add' / 'multiply' / 'maximum'.
    TD composites the inputs pairwise in order: ((in0 op in1) op in2) ..."""
    n = max(1, n_inputs)
    ops = {
        "over": "a + (1.0 - a.a) * b",
        "under": "b + (1.0 - b.a) * a",
        "add": "a + b",
        "multiply": "a * b",
        "maximum": "max(a, b)",
        "minimum": "min(a, b)",
        "subtract": "a - b",
        "difference": "abs(a - b)",
        "inside": "a * b.a",
        "outside": "a * (1.0 - b.a)",
    }
    expr = ops.get(operand, ops["over"])
    lines = "\n".join(
        f"    {{ vec4 a = c; vec4 b = texture(tex[{i}], vUV.st); c = {expr}; }}"
        for i in range(1, n)
    )
    return (
        HEADER
        + f"""
in vec3 vUV;
out vec4 fragColor;
uniform sampler2D tex[{n}];
void main() {{
    vec4 c = texture(tex[0], vUV.st);
{lines}
    fragColor = c;
}}
"""
    )


def level_top() -> str:
    return (
        HEADER
        + """
in vec3 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
uniform vec4 uLevel;  // x: brightness, y: gamma, z: contrast, w: opacity
void main() {
    vec4 c = texture(tex0, vUV.st);
    vec3 x = c.rgb * uLevel.x;
    x = (x - 0.5) * uLevel.z + 0.5;
    x = pow(max(x, 0.0), vec3(1.0 / max(uLevel.y, 1e-4)));
    fragColor = vec4(x, c.a) * uLevel.w;
}
"""
    )


# ---------------------------------------------------------------- scene

SCENE_VERT = (
    HEADER
    + """
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNrm;
layout(location = 2) in vec2 aUV;
layout(location = 3) in vec4 aTan;
layout(location = 4) in vec4 aBoneIdx;
layout(location = 5) in vec4 aBoneWt;
layout(location = 6) in vec4 aCol;
// instancing: model matrix columns + colour (divisor 1)
layout(location = 7) in vec4 aI0;
layout(location = 8) in vec4 aI1;
layout(location = 9) in vec4 aI2;
layout(location = 10) in vec4 aI3;
layout(location = 11) in vec4 aICol;

uniform mat4 uModel;     // geometry world (non-skinned)
uniform mat4 uView;
uniform mat4 uProj;
#ifdef SKINNED
uniform mat4 uBones[MAX_BONES];   // full bone->world matrices (geometry-local in)
#endif

out vec3 vPosW;
out vec3 vNrmW;
out vec2 vUV;
out vec4 vTanW;
out vec4 vCol;
out float vDepth;

void main() {
#ifdef SKINNED
    mat4 m = aBoneWt.x * uBones[int(aBoneIdx.x)] + aBoneWt.y * uBones[int(aBoneIdx.y)]
           + aBoneWt.z * uBones[int(aBoneIdx.z)] + aBoneWt.w * uBones[int(aBoneIdx.w)];
#else
    mat4 m = uModel;
#endif
#ifdef INSTANCED
    m = m * mat4(aI0, aI1, aI2, aI3);
    vCol = aCol * aICol;
#else
    vCol = aCol;
#endif
    vec4 pw = m * vec4(aPos, 1.0);
    mat3 nm = mat3(m);
    vPosW = pw.xyz;
    vNrmW = nm * aNrm;
    vTanW = vec4(nm * aTan.xyz, aTan.w);
    vUV = aUV;
    vec4 pv = uView * pw;
    vDepth = -pv.z;
    gl_Position = uProj * pv;
}
"""
)

SCENE_FRAG = (
    HEADER
    + """
in vec3 vPosW;
in vec3 vNrmW;
in vec2 vUV;
in vec4 vTanW;
in vec4 vCol;
in float vDepth;

layout(location = 0) out vec4 fragColor;
#ifdef DEPTH_OUT
layout(location = 1) out vec4 depthOut;
#endif

uniform vec3 uCamPos;
uniform int uNumLights;
uniform vec3 uLightPos[MAX_LIGHTS];
uniform vec3 uLightCol[MAX_LIGHTS];     // colour * dimmer
uniform vec4 uLightAtten[MAX_LIGHTS];   // x: on, y: start, z: end, w: exponent
uniform vec3 uAmbient;                  // sum of ambient lights (colour * dimmer)

uniform vec3 uDiff;
uniform vec3 uAmb;
uniform vec3 uSpec;
uniform vec3 uEmit;
uniform vec3 uConst;
uniform float uShininess;
uniform float uAlphaFront;
uniform float uAlphaThreshold;
uniform float uBumpScale;

uniform sampler2D sDiffuse;
uniform sampler2D sNormal;
uniform sampler2D sColor;
uniform sampler2D sAlpha;

// TouchDesigner's light attenuation: a quarter-sine ramp from End (0) to Start
// (1), raised to the Attenuation Exponent.
float tdAtten(vec4 a, float d) {
    if (a.x < 0.5) return 1.0;
    float t = clamp((a.z - d) / max(a.z - a.y, 1e-5), 0.0, 1.0);
    return pow(sin(t * 1.5707963), a.w);
}

void main() {
    vec2 uv = vUV;
    float alpha = uAlphaFront;
#ifdef ALPHA_MAP
    alpha *= texture(sAlpha, uv).a;
#endif
#ifdef ALPHA_TEST
    if (!(alpha > uAlphaThreshold)) discard;
#endif
    vec3 n = normalize(vNrmW);
    if (!gl_FrontFacing) n = -n;
#ifdef NORMAL_MAP
    {
        vec3 t = normalize(vTanW.xyz - n * dot(n, vTanW.xyz));
        vec3 b = cross(n, t) * (vTanW.w < 0.0 ? -1.0 : 1.0);
        vec3 tn = texture(sNormal, uv).xyz * 2.0 - 1.0;
        tn.xy *= uBumpScale;
        n = normalize(mat3(t, b, n) * tn);
    }
#endif
    vec3 base = vec3(1.0);
    float baseA = 1.0;
#ifdef DIFFUSE_MAP
    vec4 dm = texture(sDiffuse, uv);
    base = dm.rgb;
    baseA = dm.a;
#endif
#ifdef POINT_COLOR
    base *= vCol.rgb;
#endif
    vec3 v = normalize(uCamPos - vPosW);
    vec3 diff = vec3(0.0), spec = vec3(0.0);
    for (int i = 0; i < MAX_LIGHTS; ++i) {
        if (i >= uNumLights) break;
        vec3 ld = uLightPos[i] - vPosW;
        float dist = length(ld);
        vec3 l = ld / max(dist, 1e-6);
        vec3 lc = uLightCol[i] * tdAtten(uLightAtten[i], dist);
        float nl = max(dot(n, l), 0.0);
        diff += lc * nl;
        if (nl > 0.0) {
            vec3 h = normalize(l + v);
            spec += lc * pow(max(dot(n, h), 0.0), max(uShininess, 1e-3));
        }
    }
    vec3 c = diff * uDiff * base + uAmbient * uAmb * base + spec * uSpec + uEmit + uConst;
#ifdef COLOR_MAP
    c *= texture(sColor, uv).rgb;
#endif
    float a = alpha * baseA;
    fragColor = vec4(c * a, a);
#ifdef DEPTH_OUT
    depthOut = vec4(vDepth, 0.0, 0.0, 1.0);
#endif
}
"""
)


def scene_program(defines: list[str], max_lights: int = 8, max_bones: int = 64) -> tuple[str, str]:
    d = f"#define MAX_LIGHTS {max_lights}\n#define MAX_BONES {max_bones}\n" + "".join(
        f"#define {x}\n" for x in defines
    )
    return (
        SCENE_VERT.replace(HEADER, HEADER + d, 1),
        SCENE_FRAG.replace(HEADER, HEADER + d, 1),
    )
