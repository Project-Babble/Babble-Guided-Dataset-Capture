#pragma once
// Texture decoding (CPU, safe on a worker thread) and upload. Formats: KTX2
// without supercompression (ASTC from tools/convert_assets.py) and PNG/TGA
// via stb_image. Like the Python app, colour data is sampled raw (UNORM, no
// sRGB decode) so materials look the same as before.

#include "vk_context.h"

#include <cstdint>
#include <string>
#include <vector>

struct TextureData {
    uint32_t width = 0, height = 0;
    VkFormat format = VK_FORMAT_R8G8B8A8_UNORM;
    std::vector<std::vector<uint8_t>> levels;  // level 0 first
    bool generate_mips = false;                // RGBA8 sources: blit the chain
    bool has_alpha = false;
};

// Decodes a .ktx2/.png/.tga file; throws Error on failure
TextureData decode_texture(const std::string& path);
TextureData solid_texture(float r, float g, float b, float a = 1.0f);
TextureData rgba_texture(uint32_t width, uint32_t height, std::vector<uint8_t> pixels, bool mips);

// Picks a decodable sibling when the device can't sample `data.format`
// (e.g. ASTC on a desktop GPU): returns false if unsupported
bool texture_supported(const VkContext& vk, const TextureData& data);
Image upload(VkContext& vk, const TextureData& data);
