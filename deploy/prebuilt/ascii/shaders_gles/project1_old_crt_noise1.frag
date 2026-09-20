#version 100
precision mediump float;
precision highp int;

uniform highp float uHarmonics;
uniform highp float uSpread;
uniform highp float uRough;
uniform highp float uAspect;
uniform highp float uPeriod;
uniform highp vec4 uScale;
uniform highp vec4 uTranslate;
uniform highp float uT;
uniform highp float uExp;
uniform highp float uOffset;
uniform highp float uAmp;
uniform highp float uSeed;
uniform highp float uMono;

varying highp vec2 vUV;

highp float perm289(highp float x)
{
    return mod(((x * 34.0) + 1.0) * x, 289.0);
}

highp float cellHash(highp vec3 cell, highp float salt)
{
    highp float param = mod(cell.x, 289.0) + salt;
    highp float h = perm289(param);
    highp float param_1 = h + mod(cell.y, 289.0);
    h = perm289(param_1);
    highp float param_2 = h + mod(cell.z, 289.0);
    h = perm289(param_2);
    return h;
}

highp vec3 cellGradient(highp vec3 cell, highp float salt)
{
    highp vec3 param = cell;
    highp float param_1 = salt;
    highp float h = cellHash(param, param_1);
    highp float a = h * 0.0217411257326602935791015625;
    highp float param_2 = h + 7.0;
    highp float b = perm289(param_2) * 0.0217411257326602935791015625;
    return vec3(cos(a) * sin(b), sin(a) * sin(b), cos(b));
}

highp float gradNoise(highp vec3 p, highp float salt)
{
    highp vec3 i = floor(p);
    highp vec3 f = p - i;
    highp vec3 w = ((f * f) * f) * ((f * ((f * 6.0) - vec3(15.0))) + vec3(10.0));
    highp float n = 0.0;
    for (int cz = 0; cz <= 1; cz++)
    {
        for (int cy = 0; cy <= 1; cy++)
        {
            for (int cx = 0; cx <= 1; cx++)
            {
                highp vec3 c = vec3(float(cx), float(cy), float(cz));
                highp vec3 d = f - c;
                highp vec3 param = i + c;
                highp float param_1 = salt;
                highp float g = dot(cellGradient(param, param_1), d);
                highp vec3 bl = mix(vec3(1.0) - w, w, c);
                n += (((g * bl.x) * bl.y) * bl.z);
            }
        }
    }
    return n;
}

highp float fbm(highp vec3 p, highp float salt)
{
    highp float sum = 0.0;
    highp float amp = 1.0;
    highp float norm = 0.0;
    highp float freq = 1.0;
    int oct = int(clamp(uHarmonics, 0.0, 6.0)) + 1;
    for (int o = 0; o < 7; o++)
    {
        if (o >= oct)
        {
            break;
        }
        highp vec3 param = p * freq;
        highp float param_1 = salt;
        sum += (amp * gradNoise(param, param_1));
        norm += amp;
        freq *= max(uSpread, 1.0);
        amp *= clamp(uRough, 0.0, 1.0);
    }
    highp float _267;
    if (norm > 0.0)
    {
        _267 = sum / norm;
    }
    else
    {
        _267 = 0.0;
    }
    return _267;
}

highp float channel(highp float salt)
{
    highp vec2 uv = vUV - vec2(0.5);
    uv.x *= uAspect;
    highp vec3 p = vec3(uv, 0.0) / vec3(max(uPeriod, 9.9999997473787516355514526367188e-05));
    p = (p * uScale.xyz) + uTranslate.xyz;
    p.z += uT;
    highp vec3 param = p;
    highp float param_1 = salt;
    highp float n = fbm(param, param_1);
    if (uExp != 1.0)
    {
        n = sign(n) * pow(abs(n), max(uExp, 9.9999997473787516355514526367188e-05));
    }
    return uOffset + (uAmp * n);
}

void main()
{
    highp float param = uSeed;
    highp float r = channel(param);
    highp vec3 _357;
    if (uMono > 0.5)
    {
        _357 = vec3(r);
    }
    else
    {
        highp float param_1 = uSeed + 31.0;
        highp float param_2 = uSeed + 67.0;
        _357 = vec3(r, channel(param_1), channel(param_2));
    }
    highp vec3 rgb = _357;
    gl_FragData[0] = vec4(rgb, 1.0);
}

