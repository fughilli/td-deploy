#version 330 core

in vec3 vUV;
uniform sampler2D sTD2DInputs[2];
struct TDInfo { vec4 res; };
uniform TDInfo uTD2DInfos[2];
vec4 TDOutputSwizzle(vec4 c) { return c; }

// Example Pixel Shader

// uniform float exampleUniform;

uniform float time;

out vec4 fragColor;

// Post-processing shader aiming to emulate old CRT screens
// The goal wasn't to be as realistic as possible but to
// simply have a good-looking shader which is optimised for SPEED

// MIT License (c) do whatever you want with this shit

#define PI 3.14159265359

// Emulated CRT resolution
#define FAKE_RES (iResolution.xy/6.0)

// ------ PARAMETERS ------
vec2 fishEye = vec2(0.05,0.07); // Fish-eye warp factor
float crtOutIntensity = 1.1; // intensity of crt cell outline
float crtInIntensity = 0.9; // intensity of crt cell inside
float scanIntensity = 1.1; // intensity of scanlines
float aberrationIntensity = 1.5; // Intensity of chromatic aberration
int monochromeAberrations = 0;
float grainIntensity = 0.3; // Intensity of film grain
float haloRadius = 1.8; // Radius of the ellipsis halo
float blurIntensity = 0.4; // Intensity of the radial blur
float scratchesIntensity = 3.; // Intensity of screen scratches
// ------------------------


vec3 surface(vec2 uv, sampler2D tex) {
	return texture(tex, uv).rgb;
}

// Fish-eye effect
vec2 fisheye(vec2 uv){
  uv = uv*1.8 - 0.9;    
  uv *= vec2(1.0+(uv.y*uv.y)*fishEye.x,1.0+(uv.x*uv.x)*fishEye.y);
  return uv*0.5 + 0.5;
}

// Scanlines chromatic aberration
vec3 aberration(vec2 uv, sampler2D tex, vec2 iResolution) {
    float o = sin(uv.y * iResolution.x * PI);
    o *= aberrationIntensity / iResolution.x;
    vec3 newVec = vec3(surface(vec2( uv.x+o, uv.y+o ), tex).x, surface(vec2( uv.x, uv.y+o ), tex).y, surface(vec2( uv.x+o, uv.y ), tex).z);
    if (monochromeAberrations > 0) {
        newVec = newVec / 3.0
            + vec3(surface(vec2( uv.x, uv.y+o ), tex).x, surface(vec2( uv.x+o, uv.y+o ), tex).y, surface(vec2( uv.x+o, uv.y ), tex).z) / 3.0
            + vec3(surface(vec2( uv.x+o, uv.y ), tex).x, surface(vec2( uv.x+o, uv.y ), tex).y, surface(vec2( uv.x+o, uv.y+o ), tex).z) / 3.0;
    }
    return newVec;
}

// Draw smoothed scanlines
float scanLines(vec2 uv, vec2 fakeRes){
  float dy = uv.y * fakeRes.y;
  dy = fract(dy) - 0.5;
  return exp2(-dy*dy*scanIntensity);
}

// CRT cells
vec3 crt(vec2 xy){
  xy=floor(xy*vec2(1.0,0.5));
  xy.x += xy.y*3.0;
  vec3 c = vec3(crtOutIntensity,crtOutIntensity,crtOutIntensity);
  xy.x = fract(xy.x/6.0);
    
  if(xy.x < 0.333)
      c.r=crtInIntensity;
  else if(xy.x < 0.666)
      c.g=crtInIntensity;
  else 
      c.b=crtInIntensity;
  return c;
}    

// from rez in Glenz vector form Hell
float rand(in vec2 p,in float t) {
	return fract(sin(dot(p+mod(t,1.0),vec2(12.9898,78.2333)))*43758.5453);
}

// Film grain
float grain(vec2 uv, float t) {
    return 1.0-grainIntensity+grainIntensity*rand(uv,t);
}

// Halo
float halo(vec2 uv) {    
    return haloRadius-distance(uv,vec2(0.2,0.5))-distance(uv,vec2(0.8,0.5));
}

// Screen scratches
vec3 screenshit(vec2 uv, sampler2D tex) {
	float c = 0.5*texture(tex,uv).r + 0.3*texture(tex,uv*5.0).r + 0.2*texture(tex,uv/2.0).r;
    c = (max(c, 0.78)-0.78)*scratchesIntensity;
    return vec3(smoothstep(0.,1.,c));
}

// Radial blur
vec3 blur(vec2 uv, sampler2D tex) {
    vec3 col = vec3(0.0,0.0,0.0);
    vec2 d = (vec2(0.5,0.5)-uv)/32.;
    float w = 1.0;
    vec2 s = uv;
    for( int i=0; i<32; i++ )
    {
        vec3 res = surface(vec2(s.x,s.y), tex);
        col += w*smoothstep( 0.0, 1.0, res );
        w *= .985;
        s += d;
    }
    col = col * 4.5 / 32.;
	return blurIntensity*vec3( 0.2*col + 0.8*surface(uv, tex));
}

void mainImage( out vec4 fragColor, in vec2 fragCoord , in sampler2D tex, in sampler2D noise, in vec2 iResolution, in float t){
	vec2 fakeRes = iResolution / 12.0;
    vec2 uv = fisheye(fragCoord);

    fragColor.rgb = aberration(uv, tex, iResolution) + screenshit(uv, noise) + blur(uv, tex);
    fragColor.rgb *= scanLines(uv, fakeRes) * crt(fragCoord) * grain(uv, t) * halo(uv) * 0.6;
    fragColor.a = 1.;
}



void main()
{
	
	// vec4 color = texture(sTD2DInputs[0], vUV.st);
	vec4 color = vec4(1.0);
	mainImage(color, vUV.st, sTD2DInputs[0], sTD2DInputs[1], uTD2DInfos[0].res.zw, time);
	fragColor = TDOutputSwizzle(color);
}
