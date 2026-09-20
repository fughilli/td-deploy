#version 330 core

in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
uniform float uPreOff;
uniform float uGain;
uniform float uPostOff;
void main() {
    vec4 acc = texture(tex0, vUV);
    fragColor = (acc + uPreOff) * uGain + uPostOff;
}
