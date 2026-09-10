#version 100
attribute vec2 aPos;
varying vec3 vUV;
void main() {
    vUV = vec3(aPos * 0.5 + 0.5, 0.0);
    gl_Position = vec4(aPos, 0.0, 1.0);
}
