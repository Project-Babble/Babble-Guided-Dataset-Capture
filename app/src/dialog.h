#pragma once
// 3D label-plus-two-buttons dialog operated with the controller cones and
// lasers: port of ConfirmationBox in test_model_renderer.py. Buttons
// highlight (and grow) while a cone overlaps them or a laser points at them,
// and fire on a trigger press during hover. Callbacks may call set_label /
// set_buttons to drive a multi-step flow.

#include "renderer.h"
#include "text.h"

#include <glm/glm.hpp>

#include <array>
#include <functional>
#include <list>
#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

struct Ray {
    glm::vec3 origin, direction;
};

class Dialog {
public:
    using Callback = std::function<void()>;
    static constexpr glm::vec3 LABEL_SIZE{0.44f, 0.09f, 0.02f};
    static constexpr glm::vec3 BIG_LABEL_SIZE{0.44f, 0.26f, 0.02f};  // multi-line panes
    static constexpr glm::vec3 BUTTON_SIZE{0.16f, 0.07f, 0.035f};
    static constexpr float BUTTON_CORNER_RADIUS = 0.035f;  // half the height: a full pill
    static constexpr float LABEL_CORNER_RADIUS = 0.028f;
    static constexpr float HOVER_MARGIN = 0.012f;
    static constexpr float LABEL_GAP = 0.065f;             // button row centre -> label bottom

    void init(VkContext& vk, Renderer& renderer, const TextRasterizer& text, glm::vec3 position, float cover_y);
    void destroy();

    void set_label(const std::string& text, bool big = false, bool cover = false);
    void set_buttons(std::vector<std::pair<std::string, Callback>> buttons = {});
    // probes: per hand, world points along the cone; rays: per hand laser
    void update(const std::array<std::vector<glm::vec3>, 2>& probes, const std::array<std::optional<Ray>, 2>& rays,
                const std::array<bool, 2>& pressed);
    void draw(Renderer& renderer);

    glm::vec3 effective_position() const;

    glm::vec3 position{0.0f};
    std::optional<float> anchor_top;     // world y the label hangs from
    std::optional<float> cover_center;   // world y cover-mode panes centre on
    std::array<std::optional<float>, 2> ray_hit_distances;

private:
    struct Texture {
        Image image;
        VkDescriptorSet set = VK_NULL_HANDLE;
    };
    struct Button {
        Callback callback;
        glm::vec3 offset;
        bool hovered = false;
        Texture normal, hover;
    };
    Texture make_texture(const std::string& text, int width, int height, Rgb bg, bool italic, glm::vec3 size,
                         float radius);
    void release(Texture& texture);
    const GpuMesh& mesh_for(glm::vec3 size, float radius);

    VkContext* vk_ = nullptr;
    Renderer* renderer_ = nullptr;
    const TextRasterizer* text_ = nullptr;
    std::map<std::array<float, 4>, GpuMesh> meshes_;
    std::list<std::pair<std::pair<std::string, bool>, Texture>> label_cache_;  // oldest first
    const Texture* label_ = nullptr;
    glm::vec3 label_size_ = LABEL_SIZE;
    bool cover_ = false;
    std::vector<Button> buttons_;
};
