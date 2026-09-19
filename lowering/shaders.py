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
    return (
        _header(target, "vertex")
        + """
out vec2 vUV;
void main() {
    vec2 uv = vec2((gl_VertexID == 1) ? 2.0 : 0.0,
                   (gl_VertexID == 2) ? 2.0 : 0.0);
    vUV = uv;
    gl_Position = vec4(uv * 2.0 - 1.0, 0.0, 1.0);
}
"""
    )


def vertex_td(target: str) -> str:
    """TD GLSL TOPs expect `vec3 vUV` (they use vUV.st)."""
    return (
        _header(target, "vertex")
        + """
out vec3 vUV;
void main() {
    vec2 uv = vec2((gl_VertexID == 1) ? 2.0 : 0.0,
                   (gl_VertexID == 2) ? 2.0 : 0.0);
    vUV = vec3(uv, 0.0);
    gl_Position = vec4(uv * 2.0 - 1.0, 0.0, 1.0);
}
"""
    )


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
    return (
        _header(target, "fragment")
        + f"""
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
    )


def crop_top(target: str) -> str:
    """Crop TOP: sample a sub-rectangle of the input (normalized left/right/
    bottom/top) and rescale it to fill the output. uCropRect = (l, r, b, t)."""
    return (
        _header(target, "fragment")
        + """
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
    )


def crop_transform_top(target: str) -> str:
    """Fused Crop->Transform: transform the screen UV, apply the crop remap, then
    sample the SOURCE once. Exact composition of crop_top feeding transform_top —
    one pass and one FBO instead of two (producer/consumer fusion of two
    single-tap coordinate-remap ops)."""
    return (
        _header(target, "fragment")
        + """
in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;      // the source (crop's input)
uniform vec4 uCropRect;      // (left, right, bottom, top) in source UV
uniform float uRotate;       // radians
uniform float uTranslateX;
uniform float uTranslateY;
uniform float uScaleX;
uniform float uScaleY;
uniform float uAspect;       // output w/h — rotate in square space so it doesn't stretch
void main() {
    // transform: screen UV -> crop-output UV
    vec2 c = vec2(0.5);
    vec2 p = vUV - c - vec2(uTranslateX, uTranslateY);
    float s = sin(-uRotate), co = cos(-uRotate);
    p.x *= uAspect;
    p = mat2(co, -s, s, co) * p;
    p.x /= uAspect;
    p /= vec2(uScaleX, uScaleY);
    p = p + c;
    // crop: crop-output UV -> source UV
    vec2 uv = vec2(mix(uCropRect.x, uCropRect.y, p.x),
                   mix(uCropRect.z, uCropRect.w, p.y));
    fragColor = texture(tex0, uv);
}
"""
    )


def transform_top(target: str) -> str:
    """Transform TOP: translate/rotate/scale about a pivot. Samples the input at
    the inverse-transformed UV (clamp-to-edge outside)."""
    # Per-component scalar uniforms (not vec2) so each can be a per-frame
    # expression (TD parameters like scale = op('midiin1')[1]/64). One decl per
    # line for the GLES translator's per-uniform layout injection.
    return (
        _header(target, "fragment")
        + """
in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
uniform float uRotate;       // radians
uniform float uTranslateX;
uniform float uTranslateY;
uniform float uScaleX;
uniform float uScaleY;
uniform float uAspect;       // output w/h — rotate in square space so it doesn't stretch
void main() {
    vec2 c = vec2(0.5);
    vec2 p = vUV - c - vec2(uTranslateX, uTranslateY);
    float s = sin(-uRotate), co = cos(-uRotate);
    p.x *= uAspect;
    p = mat2(co, -s, s, co) * p;
    p.x /= uAspect;
    p /= vec2(uScaleX, uScaleY);
    fragColor = texture(tex0, p + c);
}
"""
    )


# Trivial passthrough (used by the sink to sample its input into the readback FBO).
def passthrough(target: str) -> str:
    return (
        _header(target, "fragment")
        + """
in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
void main() { fragColor = texture(tex0, vUV); }
"""
    )


def add_top(target: str, n_inputs: int) -> str:
    """Add TOP: sum the bound inputs. TD's Add TOP composites its inputs by
    addition (per-input transform params are identity in the common case and are
    not modelled here)."""
    n = max(1, n_inputs)
    decls = "\n".join(f"uniform sampler2D tex{i};" for i in range(n))
    acc = " + ".join(f"texture(tex{i}, vUV)" for i in range(n))
    return (
        _header(target, "fragment")
        + f"""
in vec2 vUV;
out vec4 fragColor;
{decls}
void main() {{
    fragColor = {acc};
}}
"""
    )


# Math TOP multi-input combine -> the GLSL expression that folds input i into acc.
_MATH_COMBINE = {
    "add": "acc += s;",
    "sub": "acc -= s;",
    "subtract": "acc -= s;",
    "mult": "acc *= s;",
    "multiply": "acc *= s;",
    "div": "acc /= max(s, vec4(1e-6));",
    "divide": "acc /= max(s, vec4(1e-6));",
    "max": "acc = max(acc, s);",
    "maximum": "acc = max(acc, s);",
    "min": "acc = min(acc, s);",
    "minimum": "acc = min(acc, s);",
    "diff": "acc = abs(acc - s);",
    "difference": "acc = abs(acc - s);",
    "average": "acc += s;",  # divided by the input count below
}


def math_top(target: str, n_inputs: int, combine: str = "add") -> str:
    """Math TOP: combine the inputs, then `(v + preOff) * gain + postOff`.

    That is TD's Pre-Offset -> Gain -> Post-Offset order on the Value page. The
    combine operator folds inputs 1..n into input 0; with a single input it is a
    no-op, which is the `no_op` case."""
    n = max(1, n_inputs)
    decls = "\n".join(f"uniform sampler2D tex{i};" for i in range(n))
    key = (combine or "add").strip().lower()
    fold_stmt = _MATH_COMBINE.get(key, "acc += s;")
    body = ""
    for i in range(1, n):
        body += f"    {{ vec4 s = texture(tex{i}, vUV); {fold_stmt} }}\n"
    if key == "average" and n > 1:
        body += f"    acc /= {float(n)};\n"
    return (
        _header(target, "fragment")
        + f"""
in vec2 vUV;
out vec4 fragColor;
{decls}
uniform float uPreOff;
uniform float uGain;
uniform float uPostOff;
void main() {{
    vec4 acc = texture(tex0, vUV);
{body}    fragColor = (acc + uPreOff) * uGain + uPostOff;
}}
"""
    )


def noise_top(target: str) -> str:
    """Noise TOP: 3D gradient (Perlin-style) noise, `offset + amp * fBm(p)`.

    NOT bit-exact with TouchDesigner's own simplex/sparse generators — those are
    proprietary — so this is a visually equivalent stand-in that responds to the
    same parameters (period, amplitude, offset, transform, harmonics, seed, and
    the 4D time offset for animation).

    The hash is a float permutation polynomial rather than integer/bit mixing so
    the shader survives translation down to GLSL ES 1.00 for the `gles2` target,
    which has no integer operations."""
    return (
        _header(target, "fragment")
        + """
in vec2 vUV;
out vec4 fragColor;
uniform float uPeriod;      // TD "Period": larger = bigger features
uniform float uAmp;
uniform float uOffset;
uniform float uSeed;
uniform float uT;           // TD "Translate 4D" — animates the field
uniform vec4  uTranslate;   // xyz used (runtime has no vec3)
uniform vec4  uScale;       // xyz used
uniform float uExp;
uniform float uHarmonics;   // extra octaves beyond the base
uniform float uSpread;      // frequency multiplier per octave
uniform float uRough;       // amplitude multiplier per octave
uniform float uMono;        // 1 = same value on r,g,b
uniform float uAspect;      // output w/h, so features stay round

// Permutation polynomial on a 289 ring: float-only so it survives the ES 1.00
// translation, and exactly reproducible for a given lattice cell.
float perm289(float x) { return mod(((x * 34.0) + 1.0) * x, 289.0); }

float cellHash(vec3 cell, float salt) {
    float h = perm289(mod(cell.x, 289.0) + salt);
    h = perm289(h + mod(cell.y, 289.0));
    h = perm289(h + mod(cell.z, 289.0));
    return h;
}

// Pseudo-random unit-ish gradient for a lattice cell.
vec3 cellGradient(vec3 cell, float salt) {
    float h = cellHash(cell, salt);
    float a = h * (6.2831853 / 289.0);
    float b = perm289(h + 7.0) * (6.2831853 / 289.0);
    return vec3(cos(a) * sin(b), sin(a) * sin(b), cos(b));
}

float gradNoise(vec3 p, float salt) {
    vec3 i = floor(p);
    vec3 f = p - i;
    vec3 w = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);   // quintic smoothstep
    float n = 0.0;
    for (int cz = 0; cz <= 1; ++cz) {
        for (int cy = 0; cy <= 1; ++cy) {
            for (int cx = 0; cx <= 1; ++cx) {
                vec3 c = vec3(float(cx), float(cy), float(cz));
                vec3 d = f - c;
                float g = dot(cellGradient(i + c, salt), d);
                vec3 bl = mix(1.0 - w, w, c);
                n += g * bl.x * bl.y * bl.z;
            }
        }
    }
    return n;
}

float fbm(vec3 p, float salt) {
    float sum = 0.0, amp = 1.0, norm = 0.0, freq = 1.0;
    // +1 for the base octave; clamped so a large Harmonics can't unroll forever.
    int oct = int(clamp(uHarmonics, 0.0, 6.0)) + 1;
    for (int o = 0; o < 7; ++o) {
        if (o >= oct) break;
        sum += amp * gradNoise(p * freq, salt);
        norm += amp;
        freq *= max(uSpread, 1.0);
        amp *= clamp(uRough, 0.0, 1.0);
    }
    return norm > 0.0 ? sum / norm : 0.0;
}

float channel(float salt) {
    vec2 uv = vUV - 0.5;
    uv.x *= uAspect;                       // keep features round on a wide frame
    vec3 p = vec3(uv, 0.0) / max(uPeriod, 1e-4);
    p = p * uScale.xyz + uTranslate.xyz;
    p.z += uT;
    float n = fbm(p, salt);                // roughly [-1, 1]
    if (uExp != 1.0) n = sign(n) * pow(abs(n), max(uExp, 1e-4));
    return uOffset + uAmp * n;
}

void main() {
    float r = channel(uSeed);
    vec3 rgb = (uMono > 0.5) ? vec3(r)
                             : vec3(r, channel(uSeed + 31.0), channel(uSeed + 67.0));
    fragColor = vec4(rgb, 1.0);
}
"""
    )
