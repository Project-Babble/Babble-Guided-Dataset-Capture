#version 450
#extension GL_GOOGLE_include_directive : require
// Port of test_model_renderer.py VERTEX_SHADER. Blendshapes are applied on
// the CPU (avatar.cpp), so positions arrive already morphed.
#include "frame.glsl"

layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec2 aTexCoord;

layout(push_constant) uniform Push {
    mat4 model;
    vec4 material;   // bump_scale, metallic_factor, smoothness, occlusion_strength
    vec4 emission;   // rgb
} pc;

layout(location = 0) out vec2 TexCoord;
layout(location = 1) out vec3 WorldPos;
layout(location = 2) out vec3 WorldNormal;

void main() {
    vec4 world = pc.model * vec4(aPos, 1.0);
    WorldPos = world.xyz;
    WorldNormal = mat3(pc.model) * aNormal;  // model has no non-uniform scale
    TexCoord = aTexCoord;
    gl_Position = frame.view_proj * world;
}
