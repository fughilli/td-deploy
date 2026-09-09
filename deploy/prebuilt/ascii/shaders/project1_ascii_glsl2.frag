#version 330 core

in vec3 vUV;
uniform sampler2D sTD2DInputs[1];
struct TDInfo { vec4 res; };
uniform TDInfo uTD2DInfos[1];
vec4 TDOutputSwizzle(vec4 c) { return c; }

// Example Pixel Shader

// uniform float exampleUniform;

#define M_PI 3.1415926

out vec4 fragColor;

float normalizeAngleToUnitRange(float angle) {

    float normalizedAngle = mod(angle, 2 * M_PI);
    if (normalizedAngle < 0.0) {
        normalizedAngle += 2 * M_PI;
    }

    // Map the angle to the range [0, 1)
    return normalizedAngle / (2 * M_PI);
}

void main()
{
	vec2 offsetx = vec2(uTD2DInfos[0].res.x, 0.);
	vec2 offsety = vec2(0., uTD2DInfos[0].res.y);
	
    vec4 left = texture(sTD2DInputs[0], vUV.st - offsetx);
    vec4 right = texture(sTD2DInputs[0], vUV.st + offsetx);
    vec4 top = texture(sTD2DInputs[0], vUV.st - offsety);
    vec4 bottom = texture(sTD2DInputs[0], vUV.st + offsety);
    
    vec4 sobelx = -left + right;
    vec4 sobely = -top + bottom;
    
    float x = sobelx.x + sobelx.y + sobelx.z;
    float y = sobely.x + sobely.y + sobely.z;
    
    float mag = abs(x) + abs(y) / 6.;
    
    float dir = normalizeAngleToUnitRange(atan(y, x) + M_PI / 2);

	vec4 color = vec4(vec3(dir, mag, 0.), 1.);
	fragColor = TDOutputSwizzle(color);
}
