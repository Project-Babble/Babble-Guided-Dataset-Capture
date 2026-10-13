#version 450
#extension GL_GOOGLE_include_directive : require
// Port of test_model_renderer.py FRAGMENT_SHADER (Blinn-Phong, derivative TBN)
#include "frame.glsl"

layout(location = 0) in vec2 TexCoord;
layout(location = 1) in vec3 WorldPos;
layout(location = 2) in vec3 WorldNormal;
layout(location = 0) out vec4 FragColor;

layout(set = 1, binding = 0) uniform sampler2D base_map;     // albedo (alpha = cutout/blend)
layout(set = 1, binding = 1) uniform sampler2D normal_map;   // tangent-space normal
layout(set = 1, binding = 2) uniform sampler2D metallic_map; // metallic in R
layout(set = 1, binding = 3) uniform sampler2D ao_map;       // ambient occlusion in R
layout(set = 1, binding = 4) uniform sampler2D emission_map; // emissive color

layout(push_constant) uniform Push {
    mat4 model;
    vec4 material;   // bump_scale, metallic_factor, smoothness, occlusion_strength
    vec4 emission;   // rgb
} pc;

const vec3 LIGHT_DIR = vec3(0.302, 0.805, 0.503); // pre-normalized, world space
const vec3 LIGHT_COLOR = vec3(0.9);
const vec3 AMBIENT = vec3(0.4);

vec3 perturb_normal() {
    vec3 n_tex = texture(normal_map, TexCoord).xyz * 2.0 - 1.0;
    n_tex.xy *= pc.material.x;
    vec3 N = normalize(WorldNormal);
    vec3 dp1 = dFdx(WorldPos);
    vec3 dp2 = dFdy(WorldPos);
    vec2 duv1 = dFdx(TexCoord);
    vec2 duv2 = dFdy(TexCoord);
    vec3 dp2perp = cross(dp2, N);
    vec3 dp1perp = cross(N, dp1);
    vec3 T = dp2perp * duv1.x + dp1perp * duv2.x;
    vec3 B = dp2perp * duv1.y + dp1perp * duv2.y;
    float invmax = inversesqrt(max(max(dot(T, T), dot(B, B)), 1e-12));
    return normalize(mat3(T * invmax, B * invmax, N) * normalize(n_tex));
}

void main() {
    vec4 base = texture(base_map, TexCoord);
    if (base.a < 0.1) discard;  // alpha cutout for card geometry (eyelashes)

    vec3 N = perturb_normal();
    float smoothness = pc.material.z;
    float metallic = texture(metallic_map, TexCoord).r * pc.material.y;
    float ao = mix(1.0, texture(ao_map, TexCoord).r, pc.material.w);

    vec3 V = normalize(frame.camera_pos.xyz - WorldPos);
    vec3 H = normalize(LIGHT_DIR + V);
    float ndl = max(dot(N, LIGHT_DIR), 0.0);
    float ndh = max(dot(N, H), 0.0);

    float spec_power = exp2(smoothness * 10.0 + 1.0);
    float spec = pow(ndh, spec_power) * smoothness * ndl;

    vec3 diffuse_color = base.rgb * (1.0 - metallic);
    vec3 spec_color = mix(vec3(0.04), base.rgb, metallic);
    vec3 color = diffuse_color * (AMBIENT * ao + LIGHT_COLOR * ndl)
               + spec_color * LIGHT_COLOR * spec
               + texture(emission_map, TexCoord).rgb * pc.emission.rgb;
    FragColor = vec4(color, base.a);
}
