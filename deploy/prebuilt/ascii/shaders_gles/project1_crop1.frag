#version 100
precision mediump float;
precision highp int;

uniform highp vec4 uCropRect;
uniform highp sampler2D tex0;

varying highp vec2 vUV;

void main()
{
    highp vec2 uv = vec2(mix(uCropRect.x, uCropRect.y, vUV.x), mix(uCropRect.z, uCropRect.w, vUV.y));
    gl_FragData[0] = texture2D(tex0, uv);
}

