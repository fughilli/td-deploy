#version 100
precision mediump float;
precision highp int;

uniform highp vec2 uTranslate;
uniform highp float uRotate;
uniform highp vec2 uScale;
uniform highp sampler2D tex0;

varying highp vec2 vUV;

void main()
{
    highp vec2 c = vec2(0.5);
    highp vec2 p = (vUV - c) - uTranslate;
    highp float s = sin(-uRotate);
    highp float co = cos(-uRotate);
    p = mat2(vec2(co, -s), vec2(s, co)) * p;
    p /= uScale;
    gl_FragData[0] = texture2D(tex0, p + c);
}

