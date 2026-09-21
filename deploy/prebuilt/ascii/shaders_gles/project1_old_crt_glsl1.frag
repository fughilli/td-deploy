#version 100
precision mediump float;
precision highp int;

struct TDInfo
{
    highp vec4 res;
};

uniform highp sampler2D sTD2DInputs[2];
uniform TDInfo uTD2DInfos[2];
uniform highp float time;

varying highp vec3 vUV;
highp vec2 fishEye;
highp float crtOutIntensity;
highp float crtInIntensity;
highp float scanIntensity;
highp float aberrationIntensity;
int monochromeAberrations;
highp float grainIntensity;
highp float haloRadius;
highp float blurIntensity;
highp float scratchesIntensity;

highp vec2 fisheye(inout highp vec2 uv)
{
    uv = (uv * 1.7999999523162841796875) - vec2(0.89999997615814208984375);
    uv *= vec2(1.0 + ((uv.y * uv.y) * fishEye.x), 1.0 + ((uv.x * uv.x) * fishEye.y));
    return (uv * 0.5) + vec2(0.5);
}

highp vec3 surface(highp vec2 uv, highp sampler2D tex)
{
    return texture2D(tex, uv).xyz;
}

highp vec3 aberration(highp vec2 uv, highp sampler2D tex, highp vec2 iResolution)
{
    highp float o = sin((uv.y * iResolution.x) * 3.1415927410125732421875);
    o *= (aberrationIntensity / iResolution.x);
    highp vec2 param = vec2(uv.x + o, uv.y + o);
    highp vec2 param_1 = vec2(uv.x, uv.y + o);
    highp vec2 param_2 = vec2(uv.x + o, uv.y);
    highp vec3 newVec = vec3(surface(param, tex).x, surface(param_1, tex).y, surface(param_2, tex).z);
    if (monochromeAberrations > 0)
    {
        highp vec2 param_3 = vec2(uv.x, uv.y + o);
        highp vec2 param_4 = vec2(uv.x + o, uv.y + o);
        highp vec2 param_5 = vec2(uv.x + o, uv.y);
        highp vec2 param_6 = vec2(uv.x + o, uv.y);
        highp vec2 param_7 = vec2(uv.x + o, uv.y);
        highp vec2 param_8 = vec2(uv.x + o, uv.y + o);
        newVec = ((newVec / vec3(3.0)) + (vec3(surface(param_3, tex).x, surface(param_4, tex).y, surface(param_5, tex).z) / vec3(3.0))) + (vec3(surface(param_6, tex).x, surface(param_7, tex).y, surface(param_8, tex).z) / vec3(3.0));
    }
    return newVec;
}

highp vec3 screenshit(highp vec2 uv, highp sampler2D tex)
{
    highp float c = ((0.5 * texture2D(tex, uv).x) + (0.300000011920928955078125 * texture2D(tex, uv * 5.0).x)) + (0.20000000298023223876953125 * texture2D(tex, uv / vec2(2.0)).x);
    c = (max(c, 0.7799999713897705078125) - 0.7799999713897705078125) * scratchesIntensity;
    return vec3(smoothstep(0.0, 1.0, c));
}

highp vec3 blur(highp vec2 uv, highp sampler2D tex)
{
    highp vec3 col = vec3(0.0);
    highp vec2 d = (vec2(0.5) - uv) / vec2(32.0);
    highp float w = 1.0;
    highp vec2 s = uv;
    for (int i = 0; i < 32; i++)
    {
        highp vec2 param = vec2(s.x, s.y);
        highp vec3 res = surface(param, tex);
        col += (smoothstep(vec3(0.0), vec3(1.0), res) * w);
        w *= 0.98500001430511474609375;
        s += d;
    }
    col = (col * 4.5) / vec3(32.0);
    highp vec2 param_1 = uv;
    return ((col * 0.20000000298023223876953125) + (surface(param_1, tex) * 0.800000011920928955078125)) * blurIntensity;
}

highp float scanLines(highp vec2 uv, highp vec2 fakeRes)
{
    highp float dy = uv.y * fakeRes.y;
    dy = fract(dy) - 0.5;
    return exp2(((-dy) * dy) * scanIntensity);
}

highp vec3 crt(inout highp vec2 xy)
{
    xy = floor(xy * vec2(1.0, 0.5));
    xy.x += (xy.y * 3.0);
    highp vec3 c = vec3(crtOutIntensity);
    xy.x = fract(xy.x / 6.0);
    if (xy.x < 0.333000004291534423828125)
    {
        c.x = crtInIntensity;
    }
    else
    {
        if (xy.x < 0.66600000858306884765625)
        {
            c.y = crtInIntensity;
        }
        else
        {
            c.z = crtInIntensity;
        }
    }
    return c;
}

highp float rand(highp vec2 p, highp float t)
{
    return fract(sin(dot(p + vec2(mod(t, 1.0)), vec2(12.98980045318603515625, 78.23329925537109375))) * 43758.546875);
}

highp float grain(highp vec2 uv, highp float t)
{
    highp vec2 param = uv;
    highp float param_1 = t;
    return (1.0 - grainIntensity) + (grainIntensity * rand(param, param_1));
}

highp float halo(highp vec2 uv)
{
    return (haloRadius - distance(uv, vec2(0.20000000298023223876953125, 0.5))) - distance(uv, vec2(0.800000011920928955078125, 0.5));
}

void mainImage(inout highp vec4 fragColor, highp vec2 fragCoord, highp sampler2D tex, highp sampler2D _noise, highp vec2 iResolution, highp float t)
{
    highp vec2 fakeRes = iResolution / vec2(12.0);
    highp vec2 param = fragCoord;
    highp vec2 _485 = fisheye(param);
    highp vec2 uv = _485;
    highp vec2 param_1 = uv;
    highp vec2 param_2 = iResolution;
    highp vec2 param_3 = uv;
    highp vec2 param_4 = uv;
    highp vec3 _498 = (aberration(param_1, tex, param_2) + screenshit(param_3, _noise)) + blur(param_4, tex);
    fragColor.x = _498.x;
    fragColor.y = _498.y;
    fragColor.z = _498.z;
    highp vec2 param_5 = uv;
    highp vec2 param_6 = fakeRes;
    highp vec2 param_7 = fragCoord;
    highp vec3 _512 = crt(param_7);
    highp vec2 param_8 = uv;
    highp float param_9 = t;
    highp vec2 param_10 = uv;
    highp vec4 _526 = fragColor;
    highp vec3 _528 = _526.xyz * ((((_512 * scanLines(param_5, param_6)) * grain(param_8, param_9)) * halo(param_10)) * 0.60000002384185791015625);
    fragColor.x = _528.x;
    fragColor.y = _528.y;
    fragColor.z = _528.z;
    fragColor.w = 1.0;
}

highp vec4 TDOutputSwizzle(highp vec4 c)
{
    return c;
}

void main()
{
    fishEye = vec2(0.0500000007450580596923828125, 0.070000000298023223876953125);
    crtOutIntensity = 1.10000002384185791015625;
    crtInIntensity = 0.89999997615814208984375;
    scanIntensity = 1.10000002384185791015625;
    aberrationIntensity = 1.5;
    monochromeAberrations = 0;
    grainIntensity = 0.300000011920928955078125;
    haloRadius = 1.7999999523162841796875;
    blurIntensity = 0.4000000059604644775390625;
    scratchesIntensity = 3.0;
    highp vec4 color = vec4(1.0);
    highp vec2 param_1 = vUV.xy;
    highp vec2 param_2 = uTD2DInfos[0].res.zw;
    highp float param_3 = time;
    highp vec4 param;
    mainImage(param, param_1, sTD2DInputs[0], sTD2DInputs[1], param_2, param_3);
    color = param;
    highp vec4 param_4 = color;
    gl_FragData[0] = TDOutputSwizzle(param_4);
}

