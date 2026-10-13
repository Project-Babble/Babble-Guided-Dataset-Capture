// Per-eye frame data (renderer.h: FrameUniforms), set 0 binding 0
layout(set = 0, binding = 0) uniform Frame {
    mat4 view_proj;
    mat4 inv_proj;
    mat4 inv_view;
    vec4 camera_pos;      // xyz
    vec4 sky_a;           // time, fade_low, fade_high, passthrough_on
    vec4 sky_b;           // sky_detail, aurora_strength, aurora_speed, star_strength
    vec4 top_color;
    vec4 horizon_color;
    vec4 floor_color;
    vec4 aurora_primary;
    vec4 aurora_secondary;
} frame;
