#include "avatar.h"

#include "common.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <fstream>

namespace {

constexpr size_t MAX_ACTIVE_MORPHS = 24;  // same cap as the Python shader path

class Reader {
public:
    explicit Reader(std::vector<uint8_t> data) : data_(std::move(data)) {}
    template <typename T> T get() {
        need(sizeof(T));
        T value;
        std::memcpy(&value, data_.data() + pos_, sizeof(T));
        pos_ += sizeof(T);
        return value;
    }
    std::string str() {
        uint16_t n = get<uint16_t>();
        need(n);
        std::string s(reinterpret_cast<const char*>(data_.data() + pos_), n);
        pos_ += n;
        align();
        return s;
    }
    template <typename T> void array(std::vector<T>& out, size_t count) {
        need(count * sizeof(T));
        out.resize(count);
        std::memcpy(out.data(), data_.data() + pos_, count * sizeof(T));
        pos_ += count * sizeof(T);
    }
    void align() { pos_ = (pos_ + 3) & ~size_t(3); }
    bool done() const { return pos_ == data_.size(); }

private:
    void need(size_t n) const {
        if (pos_ + n > data_.size()) fail("avatar.bin is truncated");
    }
    std::vector<uint8_t> data_;
    size_t pos_ = 0;
};

}  // namespace

AvatarData load_avatar_data(const std::string& asset_dir) {
    std::string path = asset_dir + "/avatar.bin";
    std::ifstream f(path, std::ios::binary);
    if (!f) fail("cannot open " + path + " (run tools/convert_assets.py)");
    Reader r(std::vector<uint8_t>((std::istreambuf_iterator<char>(f)), {}));
    if (r.get<uint32_t>() != 0x56414242u) fail(path + ": bad magic");  // "BBAV"
    if (uint32_t version = r.get<uint32_t>(); version != 1) fail(path + ": unsupported version " + std::to_string(version));

    AvatarData d;
    d.shapes.resize(r.get<uint32_t>());
    for (auto& s : d.shapes) s = r.str();
    d.texture_paths.resize(r.get<uint32_t>());
    for (auto& t : d.texture_paths) t = r.str();
    d.materials.resize(r.get<uint32_t>());
    for (auto& m : d.materials) {
        m.name = r.str();
        for (int& t : m.textures) t = r.get<int32_t>();
        for (int i = 0; i < 4; ++i) m.diffuse[i] = r.get<float>();
        m.transparent = r.get<uint32_t>() & 1;
        m.params.bump_scale = r.get<float>();
        m.params.metallic = r.get<float>();
        m.params.smoothness = r.get<float>();
        m.params.occlusion = r.get<float>();
        for (int i = 0; i < 3; ++i) m.params.emission[i] = r.get<float>();
        uint32_t vertex_count = r.get<uint32_t>(), index_count = r.get<uint32_t>();
        m.positions.resize(vertex_count);
        m.normals.resize(vertex_count);
        m.uvs.resize(vertex_count);
        for (uint32_t v = 0; v < vertex_count; ++v) {
            for (int i = 0; i < 3; ++i) m.positions[v][i] = r.get<float>();
            int16_t n[4];
            for (auto& c : n) c = r.get<int16_t>();
            m.normals[v] = glm::vec3(n[0], n[1], n[2]) / 32767.0f;
            for (int i = 0; i < 2; ++i) m.uvs[v][i] = r.get<float>();
        }
        r.array(m.indices, index_count);
        m.morphs.resize(r.get<uint32_t>());
        for (auto& morph : m.morphs) {
            morph.shape = r.get<uint32_t>();
            morph.scale = r.get<float>() / 32767.0f;
            uint32_t entries = r.get<uint32_t>();
            r.array(morph.vertex, entries);
            r.array(morph.delta, size_t(entries) * 3);
            r.align();
            for (uint32_t v : morph.vertex) {
                if (v >= vertex_count) fail(path + ": morph vertex out of range");
            }
        }
        for (uint32_t i : m.indices) {
            if (i >= vertex_count) fail(path + ": index out of range");
        }
    }
    if (!r.done()) fail(path + ": trailing data");

    for (const auto& rel : d.texture_paths) {
        d.textures.push_back(decode_texture(asset_dir + "/" + rel));
    }
    return d;
}

Avatar::~Avatar() {
    if (loader_.joinable()) loader_.join();
}

void Avatar::start_loading(const std::string& asset_dir) {
    loader_ = std::thread([this, asset_dir] {
        try {
            auto t0 = monotonic_seconds();
            data_ = load_avatar_data(asset_dir);
            LOG("Avatar data loaded in %.2fs: %zu materials, %zu shapes, %zu textures",
                monotonic_seconds() - t0, data_.materials.size(), data_.shapes.size(), data_.textures.size());
        } catch (const std::exception& e) {
            load_error_ = e.what();
        }
        loaded_ = true;
    });
}

bool Avatar::pump(VkContext& vk, Renderer& renderer) {
    if (ready_ || !error_.empty() || !loaded_) return ready_;
    if (loader_.joinable()) loader_.join();
    if (!load_error_.empty()) {
        error_ = load_error_;
        return false;
    }
    // One texture per frame keeps each frame's upload short
    if (next_texture_ < data_.textures.size()) {
        TextureData& t = data_.textures[next_texture_];
        if (texture_supported(vk, t)) {
            images_.push_back(upload(vk, t));
        } else {
            LOG_ERR("Texture %s: VkFormat %d unsupported on this GPU; using the material colour",
                    data_.texture_paths[next_texture_].c_str(), int(t.format));
            images_.push_back(Image{});
        }
        t.levels.clear();
        t.levels.shrink_to_fit();
        ++next_texture_;
        return false;
    }
    auto view = [&](int index) -> VkImageView {
        return index >= 0 && size_t(index) < images_.size() ? images_[size_t(index)].view : VK_NULL_HANDLE;
    };
    for (const AvatarMaterial& m : data_.materials) {
        GpuMaterial g;
        MeshData mesh;
        mesh.positions = m.positions;
        mesh.normals = m.normals;
        mesh.uvs = m.uvs;
        mesh.indices = m.indices;
        g.mesh = renderer.create_mesh(mesh);
        if (!m.morphs.empty()) {
            for (auto& buffer : g.morphed) {
                buffer = vk.create_buffer(m.positions.size() * sizeof(glm::vec3), VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
                std::memcpy(buffer.mapped, m.positions.data(), m.positions.size() * sizeof(glm::vec3));
            }
        }
        VkImageView base = view(m.textures[0]);
        if (!base) {
            g.solid = upload(vk, solid_texture(m.diffuse.r, m.diffuse.g, m.diffuse.b, m.diffuse.a));
            base = g.solid.view;
        }
        g.set = renderer.material_set(base, view(m.textures[1]), view(m.textures[2]), view(m.textures[3]),
                                      view(m.textures[4]));
        gpu_.push_back(std::move(g));
    }
    ready_ = true;
    LOG("Blendshapes available: %zu", data_.shapes.size());
    return true;
}

// The other side's name (FooLeft <-> FooRight), if the avatar has that shape
static std::string mirrored_shape(const std::string& name, const std::vector<std::string>& shapes) {
    auto ends_with = [&](const char* suffix) {
        size_t n = std::strlen(suffix);
        return name.size() > n && name.compare(name.size() - n, n, suffix) == 0;
    };
    std::string other;
    if (ends_with("Left")) other = name.substr(0, name.size() - 4) + "Right";
    else if (ends_with("Right")) other = name.substr(0, name.size() - 5) + "Left";
    else return name;
    return std::find(shapes.begin(), shapes.end(), other) != shapes.end() ? other : name;
}

void Avatar::set_weights(const std::map<std::string, float>& weights) {
    if (!loaded_) return;  // the loader thread still owns data_
    if (mirror_names_.size() != data_.shapes.size()) {
        mirror_names_.clear();
        for (const auto& name : data_.shapes) mirror_names_.push_back(mirrored_shape(name, data_.shapes));
    }
    std::vector<std::pair<uint32_t, float>> active;
    for (uint32_t i = 0; i < data_.shapes.size(); ++i) {
        auto it = weights.find(mirror_names_[i]);
        if (it != weights.end() && std::abs(it->second) > 1e-4f) active.emplace_back(i, it->second);
    }
    if (active.size() > MAX_ACTIVE_MORPHS) {
        std::partial_sort(active.begin(), active.begin() + MAX_ACTIVE_MORPHS, active.end(),
                          [](const auto& a, const auto& b) { return std::abs(a.second) > std::abs(b.second); });
        active.resize(MAX_ACTIVE_MORPHS);
    }
    if (active != active_) {
        active_ = std::move(active);
        ++weights_version_;
    }
}

void Avatar::update(VkContext&, int slot) {
    if (!ready_) return;
    for (size_t mi = 0; mi < gpu_.size(); ++mi) {
        const AvatarMaterial& m = data_.materials[mi];
        GpuMaterial& g = gpu_[mi];
        if (m.morphs.empty() || g.version[size_t(slot)] == weights_version_) continue;
        scratch_ = m.positions;
        for (const auto& morph : m.morphs) {
            auto it = std::find_if(active_.begin(), active_.end(),
                                   [&](const auto& a) { return a.first == morph.shape; });
            if (it == active_.end()) continue;
            float k = it->second * morph.scale;
            const int16_t* q = morph.delta.data();
            for (size_t e = 0; e < morph.vertex.size(); ++e, q += 3) {
                glm::vec3& p = scratch_[morph.vertex[e]];
                p.x += k * q[0];
                p.y += k * q[1];
                p.z += k * q[2];
            }
        }
        std::memcpy(g.morphed[size_t(slot)].mapped, scratch_.data(), scratch_.size() * sizeof(glm::vec3));
        g.version[size_t(slot)] = weights_version_;
    }
}

void Avatar::draw(Renderer& renderer, int slot, const glm::mat4& model, Renderer::Pass pass) {
    if (!ready_) return;
    for (size_t mi = 0; mi < gpu_.size(); ++mi) {
        const AvatarMaterial& m = data_.materials[mi];
        if ((pass == Renderer::Pass::Blended) != m.transparent) continue;
        const GpuMaterial& g = gpu_[mi];
        renderer.draw(g.mesh, g.set, model, m.params, pass, m.morphs.empty() ? nullptr : &g.morphed[size_t(slot)]);
    }
}

std::optional<DialogPlacement> Avatar::dialog_placement(const glm::mat4& model, float chin_clearance,
                                                        float head_distance, float cover_drop,
                                                        float big_pane_height) const {
    // Vertices displaced by JawOpen: low-percentile y = chin, high-percentile
    // z = face front (percentiles ignore stray vertices)
    auto region = [&](auto matches) {
        std::vector<glm::vec3> points;
        for (const auto& m : data_.materials) {
            for (const auto& morph : m.morphs) {
                if (!matches(data_.shapes[morph.shape])) continue;
                for (size_t e = 0; e < morph.vertex.size(); ++e) {
                    const int16_t* q = &morph.delta[e * 3];
                    if (std::max({std::abs(q[0]), std::abs(q[1]), std::abs(q[2])}) * morph.scale > 1e-6f)
                        points.push_back(m.positions[morph.vertex[e]]);
                }
            }
        }
        return points;
    };
    auto percentile = [](std::vector<float> v, float p) {
        std::sort(v.begin(), v.end());
        float idx = p / 100.0f * float(v.size() - 1);
        size_t lo = size_t(idx), hi = std::min(lo + 1, v.size() - 1);
        return v[lo] + (v[hi] - v[lo]) * (idx - float(lo));
    };
    auto jaw = region([](const std::string& s) { return s == "JawOpen"; });
    if (jaw.empty()) return std::nullopt;
    std::vector<float> ys, zs;
    for (const auto& p : jaw) {
        ys.push_back(p.y);
        zs.push_back(p.z);
    }
    const glm::vec3 offset(model[3]);
    float chin_y = percentile(ys, 1) + offset.y;
    float face_z = percentile(zs, 99) + offset.z;
    DialogPlacement placement;
    placement.label_top = chin_y - chin_clearance;
    placement.position = glm::vec3(offset.x, placement.label_top - big_pane_height, face_z + head_distance);
    auto eyes = region([](const std::string& s) { return s.rfind("Eye", 0) == 0; });
    if (!eyes.empty()) {
        std::vector<float> eye_ys;
        for (const auto& p : eyes) eye_ys.push_back(p.y);
        placement.cover_center = percentile(eye_ys, 50) + offset.y - cover_drop;
    }
    return placement;
}

void Avatar::destroy(VkContext& vk, Renderer& renderer) {
    if (loader_.joinable()) loader_.join();
    for (auto& g : gpu_) {
        renderer.destroy_mesh(g.mesh);
        for (auto& b : g.morphed) vk.destroy_buffer(b);
        vk.destroy_image(g.solid);
    }
    gpu_.clear();
    for (auto& img : images_) vk.destroy_image(img);
    images_.clear();
}
