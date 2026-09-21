#version 100
precision mediump float;
precision highp int;

uniform highp sampler2D tex0;
uniform highp float uPreOff;
uniform highp float uGain;
uniform highp float uPostOff;

varying highp vec2 vUV;

void main()
{
    highp vec4 acc = texture2D(tex0, vUV);
    gl_FragData[0] = ((acc + vec4(uPreOff)) * uGain) + vec4(uPostOff);
}

