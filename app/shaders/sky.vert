#version 450
// Fullscreen triangle from gl_VertexIndex; no vertex buffers
layout(location = 0) out vec2 ndc;
void main() {
    vec2 pos = vec2(float((gl_VertexIndex << 1) & 2), float(gl_VertexIndex & 2)) * 2.0 - 1.0;
    ndc = pos;
    gl_Position = vec4(pos, 0.999, 1.0);
}
