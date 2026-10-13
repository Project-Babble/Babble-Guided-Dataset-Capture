#include "dialog.h"

#include "textures.h"

#include <glm/gtc/matrix_transform.hpp>

#include <algorithm>
#include <cmath>
#include <limits>

namespace {
const Rgb UI_TEXT_COLOR{235, 235, 240};
const Rgb UI_LABEL_BG{24, 26, 34};
const Rgb UI_BUTTON_BG{45, 48, 62};
const Rgb UI_BUTTON_BG_HOVER{70, 110, 190};
const size_t LABEL_CACHE_SIZE = 96;  // countdown labels repeat every second

// Slab-method ray vs AABB: entry distance (0 inside), nullopt on a miss
std::optional<float> intersect_ray_aabb(const Ray& ray, glm::vec3 center, glm::vec3 half) {
    float t_near = 0.0f, t_far = std::numeric_limits<float>::infinity();
    for (int axis = 0; axis < 3; ++axis) {
        if (std::abs(ray.direction[axis]) < 1e-9f) {
            if (std::abs(ray.origin[axis] - center[axis]) > half[axis]) return std::nullopt;
            continue;
        }
        float t1 = (center[axis] - half[axis] - ray.origin[axis]) / ray.direction[axis];
        float t2 = (center[axis] + half[axis] - ray.origin[axis]) / ray.direction[axis];
        if (t1 > t2) std::swap(t1, t2);
        t_near = std::max(t_near, t1);
        t_far = std::min(t_far, t2);
        if (t_near > t_far) return std::nullopt;
    }
    return t_near;
}

// Texture-pixel margins keeping text on the element's flat front face
std::pair<int, int> text_margins(glm::vec3 size, float radius, int tex_width, int tex_height) {
    float edge = std::min(size.z / 2, radius);
    return {int(tex_width * 2 * edge / size.x) + 24, int(tex_height * 2 * edge / size.y) + 24};
}
}  // namespace

void Dialog::init(VkContext& vk, Renderer& renderer, const TextRasterizer& text, glm::vec3 pos, float cover_y) {
    vk_ = &vk;
    renderer_ = &renderer;
    text_ = &text;
    position = pos;
    cover_center = cover_y;
    set_label(" ");
}

Dialog::Texture Dialog::make_texture(const std::string& text, int width, int height, Rgb bg, bool italic,
                                     glm::vec3 size, float radius) {
    auto [mx, my] = text_margins(size, radius, width, height);
    Texture t;
    t.image = upload(*vk_, rgba_texture(uint32_t(width), uint32_t(height),
                                        text_->render(text, width, height, bg, UI_TEXT_COLOR, italic, mx, my), true));
    t.set = renderer_->material_set(t.image.view, VK_NULL_HANDLE, VK_NULL_HANDLE, VK_NULL_HANDLE, VK_NULL_HANDLE,
                                    true);
    return t;
}

void Dialog::release(Texture& texture) {
    if (texture.set) renderer_->free_material_set(texture.set);
    Image image = texture.image;
    VkContext* vk = vk_;
    vk_->defer([vk, image]() mutable { vk->destroy_image(image); });
    texture = Texture{};
}

const GpuMesh& Dialog::mesh_for(glm::vec3 size, float radius) {
    std::array<float, 4> key{size.x, size.y, size.z, radius};
    auto it = meshes_.find(key);
    if (it == meshes_.end()) {
        it = meshes_.emplace(key, renderer_->create_mesh(rounded_box_mesh(size.x, size.y, size.z, radius))).first;
    }
    return it->second;
}

glm::vec3 Dialog::effective_position() const {
    glm::vec3 p = position;
    if (cover_ && cover_center) {
        // Centre the label+buttons cluster (buttons bottom to label top)
        float cluster_center = (LABEL_GAP + label_size_.y - BUTTON_SIZE.y / 2) / 2;
        p.y = *cover_center - cluster_center;
    } else if (anchor_top) {
        p.y = *anchor_top - (LABEL_GAP + label_size_.y);
    }
    return p;
}

void Dialog::set_label(const std::string& text, bool big, bool cover) {
    label_size_ = big ? BIG_LABEL_SIZE : LABEL_SIZE;
    cover_ = cover;
    auto key = std::make_pair(text, big);
    for (auto& entry : label_cache_) {
        if (entry.first == key) {
            label_ = &entry.second;
            return;
        }
    }
    if (label_cache_.size() >= LABEL_CACHE_SIZE) {
        // Evict the oldest entry that isn't on screen
        for (auto it = label_cache_.begin(); it != label_cache_.end(); ++it) {
            if (&it->second != label_) {
                release(it->second);
                label_cache_.erase(it);
                break;
            }
        }
    }
    int tex_height = int(1024 * label_size_.y / label_size_.x);
    label_cache_.emplace_back(key, make_texture(text, 1024, tex_height, UI_LABEL_BG, true, label_size_,
                                                LABEL_CORNER_RADIUS));
    label_ = &label_cache_.back().second;
}

void Dialog::set_buttons(std::vector<std::pair<std::string, Callback>> definitions) {
    for (auto& b : buttons_) {
        release(b.normal);
        release(b.hover);
    }
    buttons_.clear();
    const glm::vec3 offsets[2] = {{-0.105f, 0.0f, 0.0f}, {0.105f, 0.0f, 0.0f}};
    for (size_t i = 0; i < definitions.size() && i < 2; ++i) {
        Button b;
        b.callback = std::move(definitions[i].second);
        b.offset = offsets[i];
        b.normal = make_texture(definitions[i].first, 512, 224, UI_BUTTON_BG, false, BUTTON_SIZE, BUTTON_CORNER_RADIUS);
        b.hover = make_texture(definitions[i].first, 512, 224, UI_BUTTON_BG_HOVER, false, BUTTON_SIZE,
                               BUTTON_CORNER_RADIUS);
        buttons_.push_back(std::move(b));
    }
}

void Dialog::update(const std::array<std::vector<glm::vec3>, 2>& probes, const std::array<std::optional<Ray>, 2>& rays,
                    const std::array<bool, 2>& pressed) {
    ray_hit_distances = {std::nullopt, std::nullopt};
    const glm::vec3 origin = effective_position();
    const glm::vec3 half = BUTTON_SIZE / 2.0f + HOVER_MARGIN;
    Callback fire;
    for (auto& button : buttons_) {
        glm::vec3 center = origin + button.offset;
        bool hovered = false, press = false;
        for (int hand = 0; hand < 2; ++hand) {
            bool cone_hit = std::any_of(probes[hand].begin(), probes[hand].end(), [&](const glm::vec3& p) {
                glm::vec3 d = glm::abs(p - center);
                return d.x <= half.x && d.y <= half.y && d.z <= half.z;
            });
            std::optional<float> ray_hit = rays[hand] ? intersect_ray_aabb(*rays[hand], center, half) : std::nullopt;
            if (ray_hit) {
                auto& best = ray_hit_distances[size_t(hand)];
                best = best ? std::min(*best, *ray_hit) : *ray_hit;
            }
            if (cone_hit || ray_hit) {
                hovered = true;
                press = press || pressed[size_t(hand)];
            }
        }
        button.hovered = hovered;
        if (press && button.callback && !fire) fire = button.callback;
    }
    // Fire after the loop: the callback may replace the buttons
    if (fire) fire();
}

void Dialog::draw(Renderer& renderer) {
    MaterialParams params;
    params.smoothness = 0.4f;  // the cones' lightly glossy neutral material
    const glm::vec3 origin = effective_position();
    if (label_) {
        glm::mat4 model = glm::translate(glm::mat4(1.0f), origin + glm::vec3(0.0f, LABEL_GAP + label_size_.y / 2, 0.0f));
        renderer.draw(mesh_for(label_size_, LABEL_CORNER_RADIUS), label_->set, model, params);
    }
    const GpuMesh& button_mesh = mesh_for(BUTTON_SIZE, BUTTON_CORNER_RADIUS);
    for (const auto& b : buttons_) {
        glm::mat4 model = glm::translate(glm::mat4(1.0f), origin + b.offset);
        if (b.hovered) model = glm::scale(model, glm::vec3(1.08f));
        renderer.draw(button_mesh, (b.hovered ? b.hover : b.normal).set, model, params);
    }
}

void Dialog::destroy() {
    if (!renderer_) return;
    for (auto& b : buttons_) {
        release(b.normal);
        release(b.hover);
    }
    buttons_.clear();
    for (auto& entry : label_cache_) release(entry.second);
    label_cache_.clear();
    label_ = nullptr;
    for (auto& [key, mesh] : meshes_) renderer_->destroy_mesh(mesh);
    meshes_.clear();
}
