#version 330 core

in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
uniform sampler2D tex1;
void main() {
    fragColor = texture(tex0, vUV) + texture(tex1, vUV);
}
