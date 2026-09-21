#version 100
precision mediump float;
precision highp int;

uniform highp float uAspect;
uniform highp float uPeriod;
uniform highp vec4 uScale;
uniform highp vec4 uTranslate;
uniform highp float uT;
uniform highp float uSpread;
uniform highp float uRough;
uniform highp float uOffset;
uniform highp float uAmp;
uniform highp float uSeed;
uniform highp float uExp;

varying highp vec2 vUV;

highp vec3 cellGradient(highp vec3 cell)
{
    highp vec3 p = fract(cell * vec3(0.103100001811981201171875, 0.10300000011920928955078125, 0.097300000488758087158203125));
    p += vec3(dot(p, p.yxz + vec3(19.1900005340576171875)));
    p = (fract((p.xxy + p.yzz) * p.zyx) * 2.0) - vec3(1.0);
    return p * inversesqrt(max(dot(p, p), 9.9999999747524270787835121154785e-07));
}

highp float gradNoise(highp vec3 p)
{
    highp vec3 i = floor(p);
    highp vec3 f = p - i;
    highp vec3 w = ((f * f) * f) * ((f * ((f * 6.0) - vec3(15.0))) + vec3(10.0));
    i = mod(i, vec3(289.0));
    highp vec3 i1 = mod(i + vec3(1.0), vec3(289.0));
    highp vec3 param = vec3(i.x, i.y, i.z);
    highp float n000 = dot(cellGradient(param), f - vec3(0.0));
    highp vec3 param_1 = vec3(i1.x, i.y, i.z);
    highp float n100 = dot(cellGradient(param_1), f - vec3(1.0, 0.0, 0.0));
    highp vec3 param_2 = vec3(i.x, i1.y, i.z);
    highp float n010 = dot(cellGradient(param_2), f - vec3(0.0, 1.0, 0.0));
    highp vec3 param_3 = vec3(i1.x, i1.y, i.z);
    highp float n110 = dot(cellGradient(param_3), f - vec3(1.0, 1.0, 0.0));
    highp vec3 param_4 = vec3(i.x, i.y, i1.z);
    highp float n001 = dot(cellGradient(param_4), f - vec3(0.0, 0.0, 1.0));
    highp vec3 param_5 = vec3(i1.x, i.y, i1.z);
    highp float n101 = dot(cellGradient(param_5), f - vec3(1.0, 0.0, 1.0));
    highp vec3 param_6 = vec3(i.x, i1.y, i1.z);
    highp float n011 = dot(cellGradient(param_6), f - vec3(0.0, 1.0, 1.0));
    highp vec3 param_7 = vec3(i1.x, i1.y, i1.z);
    highp float n111 = dot(cellGradient(param_7), f - vec3(1.0));
    return mix(mix(mix(n000, n100, w.x), mix(n010, n110, w.x), w.y), mix(mix(n001, n101, w.x), mix(n011, n111, w.x), w.y), w.z);
}

highp float channel(highp float seed)
{
    highp vec2 uv = vUV - vec2(0.5);
    uv.x *= uAspect;
    highp vec3 p = vec3(uv, 0.0) / vec3(max(uPeriod, 9.9999997473787516355514526367188e-05));
    p = ((p * uScale.xyz) + uTranslate.xyz) + vec3(seed);
    p.z += uT;
    highp float sum = 0.0;
    highp float amp = 1.0;
    highp float norm = 0.0;
    highp vec3 q = p;
    highp vec3 param = q;
    sum += (amp * gradNoise(param));
    norm += amp;
    q *= max(uSpread, 1.0);
    amp *= clamp(uRough, 0.0, 1.0);
    highp vec3 param_1 = q;
    sum += (amp * gradNoise(param_1));
    norm += amp;
    q *= max(uSpread, 1.0);
    amp *= clamp(uRough, 0.0, 1.0);
    highp vec3 param_2 = q;
    sum += (amp * gradNoise(param_2));
    norm += amp;
    highp float _351;
    if (norm > 0.0)
    {
        _351 = sum / norm;
    }
    else
    {
        _351 = 0.0;
    }
    highp float n = _351;
    return uOffset + (uAmp * n);
}

void main()
{
    highp float param = uSeed;
    highp float r = channel(param);
    gl_FragData[0] = vec4(vec3(r), 1.0);
}

