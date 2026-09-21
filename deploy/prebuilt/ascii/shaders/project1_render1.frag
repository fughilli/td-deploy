#version 330 core

in vec3 vNrm;
in vec2 vUV;
out vec4 fragColor;
uniform sampler2D tex0;
uniform float uLightX;
uniform float uLightY;
uniform float uLightZ;
uniform float uDimmer;      // TD light "dimmer" — illumination strength
void main() {
    vec3 n = normalize(vNrm);
    vec3 l = normalize(vec3(uLightX, uLightY, uLightZ));
    // Half-Lambert keeps the unlit side readable instead of crushing it to black,
    // which matters when the result is quantised into ASCII cells.
    float d = max(dot(n, l), 0.0) * 0.5 + 0.5;
    fragColor = vec4(texture(tex0, vUV).rgb * d * uDimmer, 1.0);
}
