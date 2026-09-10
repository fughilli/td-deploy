#version 100
precision mediump float;
precision highp int;

uniform highp float uTranslateX;
uniform highp float uTranslateY;
uniform highp float uRotate;
uniform highp float uScaleX;
uniform highp float uScaleY;
uniform highp vec4 uCropRect;
uniform highp sampler2D tex0;

varying highp vec2 vUV;

void main()
{
    highp vec2 c = vec2(0.5);
    highp vec2 p = (vUV - c) - vec2(uTranslateX, uTranslateY);
    highp float s = sin(-uRotate);
    highp float co = cos(-uRotate);
    p = mat2(vec2(co, -s), vec2(s, co)) * p;
    p /= vec2(uScaleX, uScaleY);
    p += c;
    highp vec2 uv = vec2(mix(uCropRect.x, uCropRect.y, p.x), mix(uCropRect.z, uCropRect.w, p.y));
    gl_FragData[0] = texture2D(tex0, uv);
}

