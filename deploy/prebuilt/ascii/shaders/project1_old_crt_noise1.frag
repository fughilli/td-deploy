#version 330 core

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
uniform float uSpread;      // frequency multiplier per octave
uniform float uRough;       // amplitude multiplier per octave
uniform float uAspect;      // output w/h, so features stay round

// Trig-free hash -> a gradient vector per lattice cell. Three fract/multiply
// mixing rounds; the point is even distribution, not cryptographic quality.
vec3 cellGradient(vec3 cell) {
    vec3 p = fract(cell * vec3(0.1031, 0.1030, 0.0973));
    p += dot(p, p.yxz + 19.19);
    p = fract((p.xxy + p.yzz) * p.zyx) * 2.0 - 1.0;
    // Unit-length so every cell contributes the same amplitude (uneven gradient
    // magnitudes read as blotchy contrast). inversesqrt is one ALU op.
    return p * inversesqrt(max(dot(p, p), 1e-6));
}

float gradNoise(vec3 p) {
    vec3 i = floor(p);
    vec3 f = p - i;
    vec3 w = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);   // quintic smoothstep
    // Wrap the lattice so the hash input stays small and well conditioned.
    i = mod(i, 289.0);
    vec3 i1 = mod(i + 1.0, 289.0);
    float n000 = dot(cellGradient(vec3(i.x,  i.y,  i.z )), f - vec3(0.0, 0.0, 0.0));
    float n100 = dot(cellGradient(vec3(i1.x, i.y,  i.z )), f - vec3(1.0, 0.0, 0.0));
    float n010 = dot(cellGradient(vec3(i.x,  i1.y, i.z )), f - vec3(0.0, 1.0, 0.0));
    float n110 = dot(cellGradient(vec3(i1.x, i1.y, i.z )), f - vec3(1.0, 1.0, 0.0));
    float n001 = dot(cellGradient(vec3(i.x,  i.y,  i1.z)), f - vec3(0.0, 0.0, 1.0));
    float n101 = dot(cellGradient(vec3(i1.x, i.y,  i1.z)), f - vec3(1.0, 0.0, 1.0));
    float n011 = dot(cellGradient(vec3(i.x,  i1.y, i1.z)), f - vec3(0.0, 1.0, 1.0));
    float n111 = dot(cellGradient(vec3(i1.x, i1.y, i1.z)), f - vec3(1.0, 1.0, 1.0));
    return mix(mix(mix(n000, n100, w.x), mix(n010, n110, w.x), w.y),
               mix(mix(n001, n101, w.x), mix(n011, n111, w.x), w.y), w.z);
}

float channel(float seed) {
    vec2 uv = vUV - 0.5;
    uv.x *= uAspect;                       // keep features round on a wide frame
    vec3 p = vec3(uv, 0.0) / max(uPeriod, 1e-4);
    p = p * uScale.xyz + uTranslate.xyz + seed;
    p.z += uT;
    float sum = 0.0, amp = 1.0, norm = 0.0;
    vec3 q = p;
    sum += amp * gradNoise(q); norm += amp;
    q *= max(uSpread, 1.0); amp *= clamp(uRough, 0.0, 1.0);
    sum += amp * gradNoise(q); norm += amp;
    q *= max(uSpread, 1.0); amp *= clamp(uRough, 0.0, 1.0);
    sum += amp * gradNoise(q); norm += amp;
    float n = norm > 0.0 ? sum / norm : 0.0;   // roughly [-1, 1]
    return uOffset + uAmp * n;
}

void main() {
    float r = channel(uSeed);
    fragColor = vec4(vec3(r), 1.0);
}
