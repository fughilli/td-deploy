#version 100
precision mediump float;
precision highp int;

struct TDInfo
{
    highp vec4 res;
};

uniform TDInfo uTD2DInfos[1];
uniform highp sampler2D sTD2DInputs[1];

varying highp vec3 vUV;

highp float normalizeAngleToUnitRange(highp float angle)
{
    highp float normalizedAngle = mod(angle, 6.28318500518798828125);
    if (normalizedAngle < 0.0)
    {
        normalizedAngle += 6.28318500518798828125;
    }
    return normalizedAngle / 6.28318500518798828125;
}

highp vec4 TDOutputSwizzle(highp vec4 c)
{
    return c;
}

void main()
{
    highp vec2 offsetx = vec2(uTD2DInfos[0].res.x, 0.0);
    highp vec2 offsety = vec2(0.0, uTD2DInfos[0].res.y);
    highp vec4 left = texture2D(sTD2DInputs[0], vUV.xy - offsetx);
    highp vec4 right = texture2D(sTD2DInputs[0], vUV.xy + offsetx);
    highp vec4 top = texture2D(sTD2DInputs[0], vUV.xy - offsety);
    highp vec4 bottom = texture2D(sTD2DInputs[0], vUV.xy + offsety);
    highp vec4 sobelx = (-left) + right;
    highp vec4 sobely = (-top) + bottom;
    highp float x = (sobelx.x + sobelx.y) + sobelx.z;
    highp float y = (sobely.x + sobely.y) + sobely.z;
    highp float mag = abs(x) + (abs(y) / 6.0);
    highp float param = atan(y, x) + 1.5707962512969970703125;
    highp float dir = normalizeAngleToUnitRange(param);
    highp vec4 color = vec4(vec3(dir, mag, 0.0), 1.0);
    highp vec4 param_1 = color;
    gl_FragData[0] = TDOutputSwizzle(param_1);
}

