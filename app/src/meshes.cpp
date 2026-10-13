#include "meshes.h"

#include <algorithm>
#include <cmath>

namespace {
constexpr float PI = 3.14159265358979f;
float radians(float degrees) { return degrees * PI / 180.0f; }
}  // namespace

MeshData cone_mesh(float length, float radius, int segments) {
    MeshData m;
    const glm::vec3 apex(0.0f, 0.0f, -length);
    const float slant = std::hypot(length, radius);
    auto side_normal = [&](float angle) {
        return glm::vec3(length * std::cos(angle) / slant, length * std::sin(angle) / slant, -radius / slant);
    };
    const glm::vec2 uv(0.0f);
    for (int i = 0; i < segments; ++i) {
        float a0 = 2.0f * PI * i / segments, a1 = 2.0f * PI * (i + 1) / segments;
        glm::vec3 p0(radius * std::cos(a0), radius * std::sin(a0), 0.0f);
        glm::vec3 p1(radius * std::cos(a1), radius * std::sin(a1), 0.0f);
        m.add(apex, side_normal((a0 + a1) / 2), uv);  // smooth side
        m.add(p0, side_normal(a0), uv);
        m.add(p1, side_normal(a1), uv);
        const glm::vec3 back(0.0f, 0.0f, 1.0f);       // flat base cap
        m.add(glm::vec3(0.0f), back, uv);
        m.add(p1, back, uv);
        m.add(p0, back, uv);
    }
    return m;
}

MeshData box_mesh() {
    MeshData m;
    auto quad = [&](glm::vec3 a, glm::vec3 b, glm::vec3 c, glm::vec3 d, glm::vec3 n) {
        for (const glm::vec3& p : {a, b, c, a, c, d}) m.add(p, n, glm::vec2(0.0f));
    };
    const float s = 0.5f;
    quad({-s, -s, s}, {s, -s, s}, {s, s, s}, {-s, s, s}, {0, 0, 1});
    quad({s, -s, -s}, {-s, -s, -s}, {-s, s, -s}, {s, s, -s}, {0, 0, -1});
    quad({-s, -s, -s}, {-s, -s, s}, {-s, s, s}, {-s, s, -s}, {-1, 0, 0});
    quad({s, -s, s}, {s, -s, -s}, {s, s, -s}, {s, s, s}, {1, 0, 0});
    quad({-s, s, s}, {s, s, s}, {s, s, -s}, {-s, s, -s}, {0, 1, 0});
    quad({-s, -s, -s}, {s, -s, -s}, {s, -s, s}, {-s, -s, s}, {0, -1, 0});
    return m;
}

namespace {
struct OutlinePoint { glm::vec2 p, n; };

// CCW rounded-rectangle outline in the XY plane with outward normals
std::vector<OutlinePoint> rounded_rect_outline(float width, float height, float radius, int segments) {
    radius = std::min({radius, width / 2, height / 2});
    float cx = width / 2 - radius, cy = height / 2 - radius;
    const glm::vec2 centers[4] = {{cx, cy}, {-cx, cy}, {-cx, -cy}, {cx, -cy}};
    const float starts[4] = {0.0f, 90.0f, 180.0f, 270.0f};
    std::vector<OutlinePoint> outline;
    for (int c = 0; c < 4; ++c) {
        for (int k = 0; k <= segments; ++k) {
            float angle = radians(starts[c] + 90.0f * k / segments);
            glm::vec2 n(std::cos(angle), std::sin(angle));
            outline.push_back({centers[c] + radius * n, n});
        }
    }
    return outline;
}
}  // namespace

MeshData rounded_box_mesh(float width, float height, float depth, float radius, int segments,
                          int profile_segments) {
    // Minkowski sum of a sphere with an inset rounded-rect slab: exact
    // analytic normals everywhere (see create_rounded_box_mesh in Python)
    const float edge = std::min(depth / 2, radius);
    const auto inner = rounded_rect_outline(width - 2 * edge, height - 2 * edge, radius - edge, segments);
    const float wall_half = depth / 2 - edge;
    auto front_uv = [&](float x, float y) { return glm::vec2(x / width + 0.5f, 0.5f - y / height); };

    struct RingVertex { glm::vec3 p, n; glm::vec2 uv; };
    auto ring = [&](float alpha, float z_base) {
        float ca = std::cos(alpha), sa = std::sin(alpha);
        std::vector<RingVertex> points;
        for (const auto& o : inner) {
            glm::vec2 p = o.p + edge * ca * o.n;
            glm::vec2 uv = alpha >= 0.0f ? front_uv(p.x, p.y) : glm::vec2(0.0f);
            points.push_back({glm::vec3(p, z_base + edge * sa), glm::vec3(ca * o.n, sa), uv});
        }
        return points;
    };
    std::vector<std::vector<RingVertex>> rings;
    for (int k = 0; k <= profile_segments; ++k)
        rings.push_back(ring(radians(-90.0f + 90.0f * k / profile_segments), -wall_half));
    if (wall_half > 1e-9f) rings.push_back(ring(0.0f, wall_half));
    for (int k = 1; k <= profile_segments; ++k)
        rings.push_back(ring(radians(90.0f * k / profile_segments), wall_half));

    MeshData m;
    auto emit = [&](const RingVertex& v) { m.add(v.p, v.n, v.uv); };
    const size_t count = inner.size();
    for (size_t r = 0; r + 1 < rings.size(); ++r) {
        for (size_t i = 0; i < count; ++i) {
            size_t j = (i + 1) % count;
            emit(rings[r][i]); emit(rings[r][j]); emit(rings[r + 1][j]);
            emit(rings[r][i]); emit(rings[r + 1][j]); emit(rings[r + 1][i]);
        }
    }
    // Flat caps over the inner outline; the pole rings end exactly on them
    const float hz = depth / 2;
    for (size_t i = 0; i < count; ++i) {
        glm::vec2 a = inner[i].p, b = inner[(i + 1) % count].p;
        m.add({0, 0, hz}, {0, 0, 1}, front_uv(0, 0));
        m.add({a, hz}, {0, 0, 1}, front_uv(a.x, a.y));
        m.add({b, hz}, {0, 0, 1}, front_uv(b.x, b.y));
        m.add({0, 0, -hz}, {0, 0, -1}, glm::vec2(0.0f));
        m.add({b, -hz}, {0, 0, -1}, glm::vec2(0.0f));
        m.add({a, -hz}, {0, 0, -1}, glm::vec2(0.0f));
    }
    return m;
}
