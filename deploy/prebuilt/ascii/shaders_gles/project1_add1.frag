#version 100
precision mediump float;
precision highp int;

uniform highp sampler2D tex0;
uniform highp sampler2D tex1;

varying highp vec2 vUV;

void main()
{
    gl_FragData[0] = texture2D(tex0, vUV) + texture2D(tex1, vUV);
}

