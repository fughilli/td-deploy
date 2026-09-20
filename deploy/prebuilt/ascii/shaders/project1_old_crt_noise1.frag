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
