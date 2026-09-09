#version 330 core

in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
uniform vec4 uCropRect;   // (left, right, bottom, top) in input UV
void main() {
    vec2 uv = vec2(mix(uCropRect.x, uCropRect.y, vUV.x),
                   mix(uCropRect.z, uCropRect.w, vUV.y));
    fragColor = texture(tex0, uv);
}
