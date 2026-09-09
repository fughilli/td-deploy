#version 330 core

in vec3 vUV;
uniform sampler2D sTD2DInputs[3];
struct TDInfo { vec4 res; };
uniform TDInfo uTD2DInfos[3];
vec4 TDOutputSwizzle(vec4 c) { return c; }

out vec4 fragColor;

//const float blockSize = 8.;
const vec2 blockSize = vec2(24, 33) / 2.;
#define M_PI 3.1415926

// Function to compute the minimum angular difference between two angles
float angularDifference(float angle1, float angle2) {
    float diff = abs(angle1 - angle2);
    return min(diff, 2.0 * 3.14159265359 - diff);
}

// Function to check if two angles are within a certain distance of each other
bool areAnglesClose(float angle1, float angle2, float maxDistance) {
    float diff = angularDifference(angle1, angle2);
    return diff <= maxDistance;
}

const float kMinEdgeIntensity = 0.2;
const int kSpriteSheetCols = 4;
const int kSpriteSheetRows = 4;
const int kSpriteSheetNumDirectional = 4;
const int kSpriteSheetNumShading = 9;

// The angle of the 0th directional sprite in radians
const float kSpriteSheetDirectionalMinRadians =
    //
    0;
// The angle of the last directional sprite in radians
const float kSpriteSheetDirectionalMaxRadians =
    //
    2.356194490192345;

// The max intensity fraction of each shading sprite. Intensities are ordered
// from low to high.
const float kSpriteSheetShadingIntensities[9] = 
    float[9](
    //
    0.0, 0.039141414141414144, 0.07702020202020202, 0.19823232323232323, 0.23106060606060605, 0.23232323232323232, 0.34595959595959597, 0.4027777777777778, 0.4696969696969697
    );

int lookup_shading_index(float intensity) {
  return int(clamp(kSpriteSheetNumShading * intensity,
                                             0, kSpriteSheetNumShading - 1));
  // Find the index of the shading sprite that is closest to the given intensity
  // The shading sprites are ordered from low to high intensity
  for (int i = 0; i < kSpriteSheetNumShading; i++) {
    if (intensity < kSpriteSheetShadingIntensities[i]) {
      return i;
    }
  }

  return kSpriteSheetNumShading - 1;
}

vec4 lookup_sprite(sampler2D spriteSheet, vec2 charUv, int sprite_index) {
  // charUv is the uv coordinate within the character sprite
  // Compute the uv coordinate within the sprite sheet by scaling charUv by the
  // sprite sheet row and column count and then offset it by the uv coordinate
  // of the top-left corner of the character sprite
  int col_index = sprite_index % kSpriteSheetCols;
  int row_index = kSpriteSheetRows - 1 - (sprite_index / kSpriteSheetCols);
  vec2 spriteSheetUv = (charUv + vec2(col_index, row_index)) /
                       vec2(kSpriteSheetCols, kSpriteSheetRows);

  return texture(spriteSheet, spriteSheetUv);
}
vec4 lookup_shading_sprite(sampler2D spriteSheet, vec2 charUv, float shading) {
  int char_index =
      int(kSpriteSheetNumDirectional + lookup_shading_index(shading));

  return lookup_sprite(spriteSheet, charUv, char_index);
}

int compute_directional_index(float angle) {
  // The angle is represented as a value between 0 and 1 where 0 is 0 radians
  // and 1 is 2 * M_PI radians Convert the angle to radians
  float angleRadians = angle * 2.0 * M_PI;

  // Assume that the directional sprites are symmetric by pi radians rotation
  // (the sprite for angle 0 is the same as the sprite for angle pi) Thus we can
  // map the angle into the range [kSpriteSheetDirectionalMinRadians,
  // kSpriteSheetDirectionalMaxRadians] and use that to compute the sprite index
  // We need to consider that the most applicable sprite is the one that is
  // closest to the angle (could be negative or positive delta)

  // We subtract one from the number of directional sprites because
  // kSpriteSheetDirectionalMaxRadians is inclusive (it is the angle of the last
  // sprite, not the angle after the last sprite)
  float angleStep =
      (kSpriteSheetDirectionalMaxRadians - kSpriteSheetDirectionalMinRadians) /
      (kSpriteSheetNumDirectional - 1);
  float angleOffset = angleRadians - kSpriteSheetDirectionalMinRadians;
  int sprite_index =
      int(angleOffset / angleStep + 0.5) % kSpriteSheetNumDirectional;

  return sprite_index;
}

vec4 lookup_directional_sprite(sampler2D spriteSheet, vec2 charUv,
                               float angle) {
  int sprite_index = compute_directional_index(angle);

  return lookup_sprite(spriteSheet, charUv, sprite_index);
}

vec4 lookup_sprite_sheet(sampler2D spriteSheet, vec2 charUv, float angle,
                         float intensity, float shading) {
  if (intensity < kMinEdgeIntensity) {
    // The edge is not strong enough to render it as a directional sprite
    return lookup_shading_sprite(spriteSheet, charUv, shading);
  }

  return lookup_directional_sprite(spriteSheet, charUv, angle);
}

void main()
{
  vec2 iResolution = uTD2DInfos[0].res.zw;
  vec2 pix = vUV.st * iResolution;
  vec2 c_pix = (floor(pix / blockSize) + vec2(0.5)) * blockSize;
  vec2 ul_pix = floor(pix / blockSize) * blockSize;
  vec2 charUv = (pix - ul_pix) / blockSize;

  vec4 sobel = texture(sTD2DInputs[0], c_pix / iResolution);
  
  vec4 color = texture(sTD2DInputs[2], c_pix / iResolution);
  float gray = (color.r + color.g + color.b) / 3.;

  /*float orientation = sobel.x * M_PI * 2;
  float intensity = sobel.y;
  
  vec2 dir_in_cell = pix - c_pix;
  float angle = atan(dir_in_cell.y, dir_in_cell.x);
  
  vec4 result = vec4(vec3(areAnglesClose(orientation, angle, M_PI/2 ) && (intensity > 0.1) ? 1. : 0.), 1.);*/
  
  vec4 result = lookup_sprite_sheet(sTD2DInputs[1], charUv, sobel.x, sobel.y, gray);

  fragColor = TDOutputSwizzle(result);
}