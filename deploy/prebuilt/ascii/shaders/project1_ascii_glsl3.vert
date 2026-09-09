#version 330 core

out vec3 vUV;
void main() {
    vec2 uv = vec2((gl_VertexID == 1) ? 2.0 : 0.0,
                   (gl_VertexID == 2) ? 2.0 : 0.0);
    vUV = vec3(uv, 0.0);
    gl_Position = vec4(uv * 2.0 - 1.0, 0.0, 1.0);
}
