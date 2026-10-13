#pragma once
// Vulkan renderer: per-eye MSAA targets resolved into the XR swapchain
// images, the lit mesh pipelines (opaque + blended) and the sky pipeline,
// material descriptor sets and GPU meshes. Mirrors the GL draw order of the
// Python app: sky, opaque meshes, cones/lasers, dialog, transparent meshes.

#include "meshes.h"
#include "vk_context.h"
#include "xr_context.h"

#include <glm/glm.hpp>

#include <array>
#include <vector>

constexpr int FRAMES_IN_FLIGHT = 2;

struct GpuMesh {
    Buffer positions;   // vec3 per vertex (rewritten per frame for morphing meshes)
    Buffer attributes;  // vec3 normal + vec2 uv per vertex
    Buffer indices;     // optional u32 indices
    uint32_t vertex_count = 0;
    uint32_t index_count = 0;
};

struct MaterialParams {
    float bump_scale = 1.0f;
    float metallic = 0.0f;
    float smoothness = 0.3f;
    float occlusion = 0.0f;
    glm::vec3 emission{0.0f};
};

// Matches app/shaders/frame.glsl
struct FrameUniforms {
    glm::mat4 view_proj;
    glm::mat4 inv_proj;
    glm::mat4 inv_view;
    glm::vec4 camera_pos;
    glm::vec4 sky_a;    // time, fade_low, fade_high, passthrough_on
    glm::vec4 sky_b;    // sky_detail, aurora_strength, aurora_speed, star_strength
    glm::vec4 top_color, horizon_color, floor_color, aurora_primary, aurora_secondary;
};

class Renderer {
public:
    enum class Pass { Opaque, Blended };

    void init(VkContext& vk, XrContext& xr, int msaa_samples);
    void destroy();

    // Material: base colour + normal/metallic/AO/emission (null = neutral default)
    VkDescriptorSet material_set(VkImageView base, VkImageView normal = VK_NULL_HANDLE,
                                 VkImageView metallic = VK_NULL_HANDLE, VkImageView ao = VK_NULL_HANDLE,
                                 VkImageView emission = VK_NULL_HANDLE, bool ui = false);
    void free_material_set(VkDescriptorSet set);

    GpuMesh create_mesh(const MeshData& data);
    void destroy_mesh(GpuMesh& mesh);

    // Frame: begin_frame waits for the slot, then per eye: begin_eye (sky
    // drawn), draws, end_eye; end_frame submits
    VkCommandBuffer begin_frame();
    void begin_eye(int eye, uint32_t image_index, const FrameUniforms& uniforms);
    void draw(const GpuMesh& mesh, VkDescriptorSet material, const glm::mat4& model,
              const MaterialParams& params, Pass pass = Pass::Opaque,
              const Buffer* positions_override = nullptr);
    void end_eye();
    void end_frame();

    int frame_slot() const { return slot_; }
    uint64_t frame_index() const { return frame_index_; }

    Image default_normal, default_white, default_black;

private:
    struct EyeTarget {
        Image color_msaa, depth;
        std::vector<VkImageView> swapchain_views;
        std::vector<VkFramebuffer> framebuffers;
    };
    struct Slot {
        VkCommandBuffer cmd = VK_NULL_HANDLE;
        VkFence fence = VK_NULL_HANDLE;
        Buffer uniforms;
        VkDescriptorSet frame_set = VK_NULL_HANDLE;
        uint64_t frame = 0;
        bool submitted = false;
    };

    void create_targets();
    void create_pipelines();
    VkPipeline make_pipeline(VkShaderModule vert, VkShaderModule frag, bool mesh, bool blend, bool depth);

    VkContext* vk_ = nullptr;
    XrContext* xr_ = nullptr;
    VkSampleCountFlagBits samples_ = VK_SAMPLE_COUNT_1_BIT;
    VkFormat depth_format_ = VK_FORMAT_D16_UNORM;
    VkRenderPass render_pass_ = VK_NULL_HANDLE;
    VkDescriptorSetLayout frame_layout_ = VK_NULL_HANDLE, material_layout_ = VK_NULL_HANDLE;
    VkPipelineLayout pipeline_layout_ = VK_NULL_HANDLE;
    VkPipeline opaque_ = VK_NULL_HANDLE, blended_ = VK_NULL_HANDLE, sky_ = VK_NULL_HANDLE;
    VkDescriptorPool pool_ = VK_NULL_HANDLE;
    VkSampler material_sampler_ = VK_NULL_HANDLE, ui_sampler_ = VK_NULL_HANDLE;
    std::array<EyeTarget, 2> targets_;
    std::array<Slot, FRAMES_IN_FLIGHT> slots_;
    VkDeviceSize uniform_stride_ = 0;
    int slot_ = 0;
    uint64_t frame_index_ = 0;
    int current_eye_ = 0;
    VkPipeline bound_pipeline_ = VK_NULL_HANDLE;
};
