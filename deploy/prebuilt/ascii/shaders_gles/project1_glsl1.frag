#version 100
precision mediump float;
precision highp int;

struct TDInfo
{
    highp vec4 res;
};

uniform highp sampler2D sTD2DInputs[1];
uniform TDInfo uTD2DInfos[1];

varying highp vec3 vUV;

highp vec4 TDOutputSwizzle(highp vec4 c)
{
    return c;
}

void main()
{
    highp vec4 color = texture2D(sTD2DInputs[0], vUV.xy) * vec4(1.0, 1.0, 0.0, 1.0);
    highp vec4 param = color;
    gl_FragData[0] = TDOutputSwizzle(param);
}

