#version 330 core

in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
uniform float uRotate;      // radians
uniform vec2 uTranslate;    // in uv units
uniform vec2 uScale;
void main() {
    vec2 c = vec2(0.5);
    vec2 p = vUV - c - uTranslate;
    float s = sin(-uRotate), co = cos(-uRotate);
    p = mat2(co, -s, s, co) * p;
    p /= uScale;
    fragColor = texture(tex0, p + c);
}
