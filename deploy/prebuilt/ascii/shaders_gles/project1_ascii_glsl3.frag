#version 100
precision mediump float;
precision highp int;
int imod(int a, int b) { return a - (a / b) * b; }

struct TDInfo
{
    highp vec4 res;
};

uniform TDInfo uTD2DInfos[3];
uniform highp sampler2D sTD2DInputs[3];

varying highp vec3 vUV;

int lookup_shading_index(highp float intensity)
{
    return int(clamp(9.0 * intensity, 0.0, 8.0));
}

highp vec4 lookup_sprite(highp sampler2D spriteSheet, highp vec2 charUv, int sprite_index)
{
    int col_index = imod(sprite_index, 4);
    int row_index = 3 - (sprite_index / 4);
    highp vec2 spriteSheetUv = (charUv + vec2(float(col_index), float(row_index))) / vec2(4.0);
    return texture2D(spriteSheet, spriteSheetUv);
}

highp vec4 lookup_shading_sprite(highp sampler2D spriteSheet, highp vec2 charUv, highp float shading)
{
    highp float param = shading;
    int char_index = 4 + lookup_shading_index(param);
    highp vec2 param_1 = charUv;
    int param_2 = char_index;
    return lookup_sprite(spriteSheet, param_1, param_2);
}

int compute_directional_index(highp float angle)
{
    highp float angleRadians = (angle * 2.0) * 3.141592502593994140625;
    highp float angleStep = 0.785398185253143310546875;
    highp float angleOffset = angleRadians - 0.0;
    int sprite_index = imod(int((angleOffset / angleStep) + 0.5), 4);
    return sprite_index;
}

highp vec4 lookup_directional_sprite(highp sampler2D spriteSheet, highp vec2 charUv, highp float angle)
{
    highp float param = angle;
    int sprite_index = compute_directional_index(param);
    highp vec2 param_1 = charUv;
    int param_2 = sprite_index;
    return lookup_sprite(spriteSheet, param_1, param_2);
}

highp vec4 lookup_sprite_sheet(highp sampler2D spriteSheet, highp vec2 charUv, highp float angle, highp float intensity, highp float shading)
{
    if (intensity < 0.20000000298023223876953125)
    {
        highp vec2 param = charUv;
        highp float param_1 = shading;
        return lookup_shading_sprite(spriteSheet, param, param_1);
    }
    highp vec2 param_2 = charUv;
    highp float param_3 = angle;
    return lookup_directional_sprite(spriteSheet, param_2, param_3);
}

highp vec4 TDOutputSwizzle(highp vec4 c)
{
    return c;
}

void main()
{
    highp vec2 iResolution = uTD2DInfos[0].res.zw;
    highp vec2 pix = vUV.xy * iResolution;
    highp vec2 c_pix = (floor(pix / vec2(12.0, 16.5)) + vec2(0.5)) * vec2(12.0, 16.5);
    highp vec2 ul_pix = floor(pix / vec2(12.0, 16.5)) * vec2(12.0, 16.5);
    highp vec2 charUv = (pix - ul_pix) / vec2(12.0, 16.5);
    highp vec4 sobel = texture2D(sTD2DInputs[0], c_pix / iResolution);
    highp vec4 color = texture2D(sTD2DInputs[2], c_pix / iResolution);
    highp float gray = ((color.x + color.y) + color.z) / 3.0;
    highp vec2 param = charUv;
    highp float param_1 = sobel.x;
    highp float param_2 = sobel.y;
    highp float param_3 = gray;
    highp vec4 result = lookup_sprite_sheet(sTD2DInputs[1], param, param_1, param_2, param_3);
    highp vec4 param_4 = result;
    gl_FragData[0] = TDOutputSwizzle(param_4);
}

