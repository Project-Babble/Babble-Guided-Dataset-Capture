#pragma once
// Vulkan device (created through the OpenXR runtime, see xr_context.cpp) plus
// the small resource helpers the renderer needs: host-visible buffers,
// device-local images with staged uploads, one-shot submits and deferred
// destruction of objects the GPU may still be using.

#include <vulkan/vulkan.h>

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#define VK_CHECK(expr)                                                                   \
    do {                                                                                 \
        VkResult vk_check_result_ = (expr);                                              \
        if (vk_check_result_ != VK_SUCCESS)                                              \
            fail(std::string(#expr) + " failed: VkResult " + std::to_string(vk_check_result_)); \
    } while (0)

struct Buffer {
    VkBuffer buffer = VK_NULL_HANDLE;
    VkDeviceMemory memory = VK_NULL_HANDLE;
    VkDeviceSize size = 0;
    void* mapped = nullptr;  // persistently mapped (host-visible, coherent)
};

struct Image {
    VkImage image = VK_NULL_HANDLE;
    VkDeviceMemory memory = VK_NULL_HANDLE;
    VkImageView view = VK_NULL_HANDLE;
    VkFormat format = VK_FORMAT_UNDEFINED;
    uint32_t width = 0, height = 0, mip_levels = 1;
};

class VkContext {
public:
    VkInstance instance = VK_NULL_HANDLE;
    VkPhysicalDevice physical = VK_NULL_HANDLE;
    VkDevice device = VK_NULL_HANDLE;
    VkQueue queue = VK_NULL_HANDLE;
    uint32_t queue_family = 0;
    VkCommandPool command_pool = VK_NULL_HANDLE;
    VkPhysicalDeviceProperties properties{};
    VkPhysicalDeviceMemoryProperties memory_properties{};
    bool anisotropy = false;

    void init_after_device();   // queue, pool, properties
    void destroy();

    uint32_t memory_type(uint32_t type_bits, VkMemoryPropertyFlags flags) const;
    Buffer create_buffer(VkDeviceSize size, VkBufferUsageFlags usage);
    void destroy_buffer(Buffer& buffer);
    Image create_image(uint32_t width, uint32_t height, uint32_t mips, VkFormat format,
                       VkImageUsageFlags usage, VkSampleCountFlagBits samples = VK_SAMPLE_COUNT_1_BIT,
                       VkImageAspectFlags aspect = VK_IMAGE_ASPECT_COLOR_BIT);
    void destroy_image(Image& image);
    bool format_supported(VkFormat format, VkFormatFeatureFlags features) const;

    // Uploads mip level data (tightly packed, level 0 first) into a sampled
    // image; when generate_mips, only level 0 is given and the rest are blitted.
    Image upload_texture(uint32_t width, uint32_t height, VkFormat format,
                         const std::vector<std::vector<uint8_t>>& levels, bool generate_mips);

    void submit_now(const std::function<void(VkCommandBuffer)>& record);

    // Destroy `release` once the frames in flight now have completed
    void defer(std::function<void()> release);
    void frame_completed(uint64_t frame);   // called with the frame index whose fence signalled
    void set_current_frame(uint64_t frame) { current_frame_ = frame; }
    void flush_deferred();                  // device idle: release everything

private:
    struct Deferred { uint64_t frame; std::function<void()> release; };
    std::vector<Deferred> deferred_;
    uint64_t current_frame_ = 0;
};
