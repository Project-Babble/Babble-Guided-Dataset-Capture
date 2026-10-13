#include "textures.h"

#include "common.h"

#include "stb_image.h"

#include <algorithm>
#include <cstring>
#include <fstream>

namespace {

std::vector<uint8_t> read_file(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) fail("cannot open " + path);
    return std::vector<uint8_t>((std::istreambuf_iterator<char>(f)), {});
}

// sRGB-tagged formats are sampled as their UNORM twins (see textures.h)
VkFormat raw_format(VkFormat format) {
    switch (format) {
    case VK_FORMAT_R8G8B8A8_SRGB: return VK_FORMAT_R8G8B8A8_UNORM;
    case VK_FORMAT_B8G8R8A8_SRGB: return VK_FORMAT_B8G8R8A8_UNORM;
    case VK_FORMAT_R8G8B8_SRGB: return VK_FORMAT_R8G8B8_UNORM;
    case VK_FORMAT_ASTC_4x4_SRGB_BLOCK: return VK_FORMAT_ASTC_4x4_UNORM_BLOCK;
    case VK_FORMAT_ASTC_5x5_SRGB_BLOCK: return VK_FORMAT_ASTC_5x5_UNORM_BLOCK;
    case VK_FORMAT_ASTC_6x6_SRGB_BLOCK: return VK_FORMAT_ASTC_6x6_UNORM_BLOCK;
    case VK_FORMAT_ASTC_8x8_SRGB_BLOCK: return VK_FORMAT_ASTC_8x8_UNORM_BLOCK;
    case VK_FORMAT_BC7_SRGB_BLOCK: return VK_FORMAT_BC7_UNORM_BLOCK;
    case VK_FORMAT_ETC2_R8G8B8A8_SRGB_BLOCK: return VK_FORMAT_ETC2_R8G8B8A8_UNORM_BLOCK;
    default: return format;
    }
}

TextureData decode_ktx2(const std::string& path) {
    std::vector<uint8_t> file = read_file(path);
    static const uint8_t magic[12] = {0xAB, 'K', 'T', 'X', ' ', '2', '0', 0xBB, '\r', '\n', 0x1A, '\n'};
    if (file.size() < 80 || std::memcmp(file.data(), magic, 12)) fail(path + ": not a KTX2 file");
    auto u32 = [&](size_t off) { uint32_t v; std::memcpy(&v, file.data() + off, 4); return v; };
    auto u64 = [&](size_t off) { uint64_t v; std::memcpy(&v, file.data() + off, 8); return v; };
    TextureData t;
    t.format = raw_format(VkFormat(u32(12)));
    t.width = u32(20);
    t.height = u32(24);
    uint32_t layers = u32(32), faces = u32(36), level_count = std::max(1u, u32(40));
    uint32_t supercompression = u32(44);
    if (supercompression != 0) fail(path + ": supercompressed KTX2 is not supported");
    if (layers > 1 || faces != 1) fail(path + ": only plain 2D KTX2 textures are supported");
    if (t.format == VK_FORMAT_UNDEFINED) fail(path + ": KTX2 without a VkFormat");
    const size_t level_index = 80;
    if (file.size() < level_index + level_count * 24) fail(path + ": truncated level index");
    for (uint32_t i = 0; i < level_count; ++i) {
        uint64_t offset = u64(level_index + i * 24), length = u64(level_index + i * 24 + 8);
        if (offset + length > file.size()) fail(path + ": truncated level data");
        t.levels.emplace_back(file.begin() + long(offset), file.begin() + long(offset + length));
    }
    t.has_alpha = true;  // unknown without decoding; materials carry the flag
    return t;
}

}  // namespace

TextureData decode_texture(const std::string& path) {
    if (path.size() > 5 && path.compare(path.size() - 5, 5, ".ktx2") == 0) return decode_ktx2(path);
    int w = 0, h = 0, channels = 0;
    stbi_uc* pixels = stbi_load(path.c_str(), &w, &h, &channels, 4);
    if (!pixels) fail(path + ": " + stbi_failure_reason());
    std::vector<uint8_t> rgba(pixels, pixels + size_t(w) * size_t(h) * 4);
    stbi_image_free(pixels);
    TextureData t = rgba_texture(uint32_t(w), uint32_t(h), std::move(rgba), true);
    t.has_alpha = false;
    if (channels == 4 || channels == 2) {
        for (size_t i = 3; i < t.levels[0].size(); i += 4) {
            if (t.levels[0][i] < 255) {
                t.has_alpha = true;
                break;
            }
        }
    }
    return t;
}

TextureData solid_texture(float r, float g, float b, float a) {
    auto byte = [](float v) { return uint8_t(std::clamp(v, 0.0f, 1.0f) * 255.0f); };
    return rgba_texture(1, 1, {byte(r), byte(g), byte(b), byte(a)}, false);
}

TextureData rgba_texture(uint32_t width, uint32_t height, std::vector<uint8_t> pixels, bool mips) {
    TextureData t;
    t.width = width;
    t.height = height;
    t.format = VK_FORMAT_R8G8B8A8_UNORM;
    t.levels.push_back(std::move(pixels));
    t.generate_mips = mips && (width > 1 || height > 1);
    return t;
}

bool texture_supported(const VkContext& vk, const TextureData& data) {
    VkFormatFeatureFlags needed = VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT;
    if (data.generate_mips) needed |= VK_FORMAT_FEATURE_BLIT_SRC_BIT | VK_FORMAT_FEATURE_BLIT_DST_BIT;
    return vk.format_supported(data.format, needed);
}

Image upload(VkContext& vk, const TextureData& data) {
    return vk.upload_texture(data.width, data.height, data.format, data.levels, data.generate_mips);
}
