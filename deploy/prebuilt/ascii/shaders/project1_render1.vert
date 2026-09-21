#version 330 core

in vec3 aPos;
in vec3 aNrm;
in vec2 aUV;
out vec3 vNrm;
out vec2 vUV;
uniform float uRotX;
uniform float uRotY;
uniform float uRotZ;
uniform float uSclX;
uniform float uSclY;
uniform float uSclZ;
uniform float uTrnX;
uniform float uTrnY;
uniform float uTrnZ;
uniform float uCamX;
uniform float uCamY;
uniform float uCamZ;
uniform float uFov;         // degrees, horizontal
uniform float uNear;
uniform float uFar;
uniform float uAspect;      // w/h

mat3 rotXYZ(vec3 r) {
    vec3 s = sin(r), c = cos(r);
    mat3 rx = mat3(1.0, 0.0, 0.0,  0.0, c.x, s.x,  0.0, -s.x, c.x);
    mat3 ry = mat3(c.y, 0.0, -s.y, 0.0, 1.0, 0.0,  s.y, 0.0,  c.y);
    mat3 rz = mat3(c.z, s.z, 0.0, -s.z, c.z, 0.0,  0.0, 0.0,  1.0);
    return rz * ry * rx;                       // TD's default XYZ order
}

void main() {
    vec3 rad = radians(vec3(uRotX, uRotY, uRotZ));
    mat3 rot = rotXYZ(rad);
    vec3 world = rot * (aPos * vec3(uSclX, uSclY, uSclZ)) + vec3(uTrnX, uTrnY, uTrnZ);
    // Camera is translation-only here; TD looks down -Z.
    vec3 eye = world - vec3(uCamX, uCamY, uCamZ);
    // Perspective from a HORIZONTAL fov, matching TD's default viewanglemethod.
    float f = 1.0 / tan(radians(uFov) * 0.5);
    float z = eye.z;
    gl_Position = vec4(
        eye.x * f,
        eye.y * f * uAspect,
        -z * (uFar + uNear) / (uFar - uNear) - 2.0 * uFar * uNear / (uFar - uNear),
        -z);
    vNrm = rot * aNrm;                         // uniform scale assumed
    vUV = aUV;
}
