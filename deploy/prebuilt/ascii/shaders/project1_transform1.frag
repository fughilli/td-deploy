#version 330 core

in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;      // the source (crop's input)
uniform vec4 uCropRect;      // (left, right, bottom, top) in source UV
uniform float uRotate;       // radians
uniform vec2 uTranslate;     // in uv units
uniform vec2 uScale;
void main() {
    // transform: screen UV -> crop-output UV
    vec2 c = vec2(0.5);
    vec2 p = vUV - c - uTranslate;
    float s = sin(-uRotate), co = cos(-uRotate);
    p = mat2(co, -s, s, co) * p;
    p /= uScale;
    p = p + c;
    // crop: crop-output UV -> source UV
    vec2 uv = vec2(mix(uCropRect.x, uCropRect.y, p.x),
                   mix(uCropRect.z, uCropRect.w, p.y));
    fragColor = texture(tex0, uv);
}
