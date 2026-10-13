#pragma once
// CPU mesh data and the procedural meshes ported from test_model_renderer.py:
// controller cone, laser box and the rounded "pill" boxes of the dialog.
// UVs use Vulkan's top-left origin (the Python versions are flipped in v).

#include <glm/glm.hpp>

#include <cstdint>
#include <vector>

struct MeshData {
    std::vector<glm::vec3> positions;
    std::vector<glm::vec3> normals;
    std::vector<glm::vec2> uvs;
    std::vector<uint32_t> indices;  // empty: non-indexed triangle list

    void add(const glm::vec3& p, const glm::vec3& n, const glm::vec2& uv) {
        positions.push_back(p);
        normals.push_back(n);
        uvs.push_back(uv);
    }
};

// Cone pointing down -Z (OpenXR controller forward), apex at (0, 0, -length)
MeshData cone_mesh(float length = 0.15f, float radius = 0.02f, int segments = 32);
// Unit cube centered at the origin
MeshData box_mesh();
// Pill box rounded in all three dimensions; the front half carries the texture
MeshData rounded_box_mesh(float width, float height, float depth, float radius,
                          int segments = 10, int profile_segments = 6);
