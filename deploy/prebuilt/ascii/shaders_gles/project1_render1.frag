#version 100
precision mediump float;
precision highp int;

uniform highp float uLightX;
uniform highp float uLightY;
uniform highp float uLightZ;
uniform highp sampler2D tex0;
uniform highp float uDimmer;

varying highp vec3 vNrm;
varying highp vec2 vUV;

void main()
{
    highp vec3 n = normalize(vNrm);
    highp vec3 l = normalize(vec3(uLightX, uLightY, uLightZ));
    highp float d = (max(dot(n, l), 0.0) * 0.5) + 0.5;
    gl_FragData[0] = vec4((texture2D(tex0, vUV).xyz * d) * uDimmer, 1.0);
}

