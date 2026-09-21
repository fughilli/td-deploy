#version 330 core

in vec3 vUV;
uniform sampler2D sTD2DInputs[1];
struct TDInfo { vec4 res; };
uniform TDInfo uTD2DInfos[1];
vec4 TDOutputSwizzle(vec4 c) { return c; }


// Example Pixel Shader

// uniform float exampleUniform;

out vec4 fragColor;
void main()
{
	vec4 color = texture(sTD2DInputs[0], vUV.st).rgba * vec4(1.0, 1.0, 0.0, 1.0);
	fragColor = TDOutputSwizzle(color);
}
