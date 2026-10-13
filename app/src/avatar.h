#pragma once
// The morphable head: loads avatar.bin (tools/convert_assets.py) and its
// textures on a worker thread, uploads them a little per frame, and applies
// blendshapes on the CPU from the sparse quantised deltas into a per-frame
// position buffer (the Python app used a GPU delta texture instead).

#include "renderer.h"
#include "textures.h"

#include <glm/glm.hpp>

#include <atomic>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <thread>
#include <vector>

struct AvatarMaterial {
    std::string name;
    int textures[5] = {-1, -1, -1, -1, -1};  // base, normal, metallic, ao, emission
    glm::vec4 diffuse{1.0f};
    bool transparent = false;
    MaterialParams params;
    std::vector<glm::vec3> positions;  // neutral pose
    std::vector<glm::vec3> normals;
    std::vector<glm::vec2> uvs;
    std::vector<uint32_t> indices;
    struct Morph {
        uint32_t shape;
        float scale;  // metres per 32767
        std::vector<uint32_t> vertex;
        std::vector<int16_t> delta;  // xyz per entry
    };
    std::vector<Morph> morphs;
};

struct AvatarData {
    std::vector<std::string> shapes;
    std::vector<std::string> texture_paths;
    std::vector<AvatarMaterial> materials;
    std::vector<TextureData> textures;
};

// Parses <asset_dir>/avatar.bin and decodes its textures (no GPU work)
AvatarData load_avatar_data(const std::string& asset_dir);

struct DialogPlacement {
    glm::vec3 position;     // dialog origin (button-row centre)
    float label_top;        // world y the label hangs from
    std::optional<float> cover_center;
};

class Avatar {
public:
    ~Avatar();
    void start_loading(const std::string& asset_dir);
    // One GPU upload step per call; true once everything is ready
    bool pump(VkContext& vk, Renderer& renderer);
    bool ready() const { return ready_; }
    const std::string& error() const { return error_; }
    const std::vector<std::string>& shape_names() const { return data_.shapes; }

    // Weights use the user's sides (MouthSmileLeft = the user's left). The
    // avatar faces the user, so it shows them mirrored: a ...Left weight drives
    // its ...Right shape, and the reverse, like the user's reflection.
    void set_weights(const std::map<std::string, float>& weights);
    void update(VkContext& vk, int slot);  // morph into this frame slot's buffers
    void draw(Renderer& renderer, int slot, const glm::mat4& model, Renderer::Pass pass);
    // Port of dialog_position_below_chin(): placement from the JawOpen/Eye* regions
    std::optional<DialogPlacement> dialog_placement(const glm::mat4& model, float chin_clearance,
                                                    float head_distance, float cover_drop,
                                                    float big_pane_height) const;
    void destroy(VkContext& vk, Renderer& renderer);

private:
    struct GpuMaterial {
        GpuMesh mesh;
        std::array<Buffer, FRAMES_IN_FLIGHT> morphed;  // per-slot positions (morphing materials)
        std::array<uint64_t, FRAMES_IN_FLIGHT> version{};
        VkDescriptorSet set = VK_NULL_HANDLE;
        Image solid;  // diffuse colour when there is no base texture
    };
    std::thread loader_;
    std::atomic<bool> loaded_{false};
    std::string load_error_;
    AvatarData data_;
    std::vector<Image> images_;
    std::vector<GpuMaterial> gpu_;
    size_t next_texture_ = 0;
    bool ready_ = false;
    std::string error_;
    std::vector<std::string> mirror_names_;  // per shape: the weight name it shows
    std::vector<std::pair<uint32_t, float>> active_;  // (shape, weight)
    uint64_t weights_version_ = 1;
    std::vector<glm::vec3> scratch_;
};
