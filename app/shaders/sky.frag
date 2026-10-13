#version 450
#extension GL_GOOGLE_include_directive : require
// Port of test_model_renderer.py BACKGROUND_FRAGMENT_SHADER: night sky, aurora
// and stars; alpha fades the lower world into passthrough (ALPHA_BLEND).
#include "frame.glsl"

layout(location = 0) in vec2 ndc;
layout(location = 0) out vec4 FragColor;

float hash21(vec2 p) {
    return fract(sin(dot(p, vec2(41.31, 289.13))) * 43758.5453);
}

float aurora_wave(float phi, float t) {
    return sin(phi * 2.0 + t * 0.100)
         + 0.55 * sin(phi * 5.0 - t * 0.063 + 1.7)
         + 0.30 * sin(phi * 9.0 + t * 0.151 + 4.2);
}

float curtain(float phi, float elev, float t, float seed) {
    float w = aurora_wave(phi + seed * 2.39, t + seed * 13.0);
    float center = 0.40 + 0.07 * seed + 0.11 * w;
    float width = 0.14 + 0.05 * sin(phi * 3.0 - t * 0.047 + seed * 5.0);
    float band = exp(-pow((elev - center) / width, 2.0));
    float horiz = sqrt(max(1.0 - elev * elev, 0.0));
    float rays = 0.78 + 0.22 * sin(phi * 36.0 + w * 4.0 + seed * 9.0)
                        * smoothstep(0.35, 0.70, horiz);
    float zenith_fade = 1.0 - smoothstep(0.62, 0.85, elev);
    return band * rays * zenith_fade * smoothstep(0.06, 0.24, elev);
}

vec3 stars(vec3 dir, float t) {
    float horizon_fade = smoothstep(0.12, 0.35, dir.y);
    if (horizon_fade <= 0.0) return vec3(0.0);
    vec3 a = abs(dir);
    vec2 uv;
    float face;
    if (a.y >= a.x && a.y >= a.z) {
        uv = dir.xz / a.y; face = 0.0;
    } else if (a.x >= a.z) {
        uv = dir.yz / a.x; face = dir.x > 0.0 ? 1.0 : 2.0;
    } else {
        uv = dir.xy / a.z; face = dir.z > 0.0 ? 3.0 : 4.0;
    }
    vec2 grid = (uv * 0.5 + 0.5) * 22.0;
    vec2 cell = floor(grid) + face * 91.7;
    float h = hash21(cell);
    if (h < 0.78) return vec3(0.0);
    vec2 star_pos = vec2(hash21(cell + 7.13), hash21(cell + 3.71)) * 0.6 + 0.2;
    float mag = hash21(cell + 11.9);
    float radius = mix(0.05, 0.16, mag * mag);
    float bright = mix(0.22, 1.0, mag * mag * mag);
    float d = length(fract(grid) - star_pos);
    float twinkle = 1.0 - (0.5 - 0.3 * mag) * (0.5 + 0.5 * sin(t * (0.8 + 2.2 * h) + h * 41.0));
    vec3 tint = mix(vec3(1.00, 0.84, 0.66), vec3(0.72, 0.84, 1.00), hash21(cell + 5.3));
    return (1.0 - smoothstep(0.0, radius, d)) * bright * twinkle * tint * horizon_fade;
}

void main() {
    float time = frame.sky_a.x;
    float fade_low = frame.sky_a.y;
    float fade_high = frame.sky_a.z;
    float passthrough_on = frame.sky_a.w;
    float sky_detail = frame.sky_b.x;
    float aurora_strength = frame.sky_b.y;
    float aurora_speed = frame.sky_b.z;
    float star_strength = frame.sky_b.w;

    vec4 v = frame.inv_proj * vec4(ndc, 1.0, 1.0);
    vec3 dir = normalize(mat3(frame.inv_view) * v.xyz);
    float elev = dir.y;
    float phi = atan(dir.x, -dir.z);

    vec3 sky = mix(frame.horizon_color.rgb, frame.top_color.rgb,
                   smoothstep(0.0, 0.85, max(elev, 0.0)));
    if (sky_detail > 0.5) {
        float at = time * aurora_speed;
        sky += aurora_strength * curtain(phi, elev, at, 0.0) * frame.aurora_primary.rgb;
        if (sky_detail > 1.5) {
            sky += aurora_strength * 0.7 * curtain(phi, elev, at, 1.0) * frame.aurora_secondary.rgb;
        }
        sky += star_strength * stars(dir, time);
    }

    float fade = smoothstep(fade_low, fade_high, elev);
    if (passthrough_on > 0.5) {
        FragColor = vec4(sky, fade);
    } else {
        FragColor = vec4(mix(frame.floor_color.rgb, sky, fade), 1.0);
    }
}
