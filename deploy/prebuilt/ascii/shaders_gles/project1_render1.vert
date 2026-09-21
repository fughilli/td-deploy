#version 100

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
uniform float uFov;
uniform float uAspect;
uniform float uFar;
uniform float uNear;

attribute vec3 aPos;
varying vec3 vNrm;
attribute vec3 aNrm;
varying vec2 vUV;
attribute vec2 aUV;

mat3 rotXYZ(vec3 r)
{
    vec3 s = sin(r);
    vec3 c = cos(r);
    mat3 rx = mat3(vec3(1.0, 0.0, 0.0), vec3(0.0, c.x, s.x), vec3(0.0, -s.x, c.x));
    mat3 ry = mat3(vec3(c.y, 0.0, -s.y), vec3(0.0, 1.0, 0.0), vec3(s.y, 0.0, c.y));
    mat3 rz = mat3(vec3(c.z, s.z, 0.0), vec3(-s.z, c.z, 0.0), vec3(0.0, 0.0, 1.0));
    return (rz * ry) * rx;
}

void main()
{
    vec3 rad = radians(vec3(uRotX, uRotY, uRotZ));
    vec3 param = rad;
    mat3 rot = rotXYZ(param);
    vec3 world = (rot * (aPos * vec3(uSclX, uSclY, uSclZ))) + vec3(uTrnX, uTrnY, uTrnZ);
    vec3 eye = world - vec3(uCamX, uCamY, uCamZ);
    float f = 1.0 / tan(radians(uFov) * 0.5);
    float z = eye.z;
    gl_Position = vec4(eye.x * f, (eye.y * f) * uAspect, (((-z) * (uFar + uNear)) / (uFar - uNear)) - (((2.0 * uFar) * uNear) / (uFar - uNear)), -z);
    vNrm = rot * aNrm;
    vUV = aUV;
}

