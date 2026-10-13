#include "vk_context.h"

#include "common.h"

#include <algorithm>
#include <cstring>

void VkContext::init_after_device() {
    vkGetDeviceQueue(device, queue_family, 0, &queue);
    vkGetPhysicalDeviceProperties(physical, &properties);
    vkGetPhysicalDeviceMemoryProperties(physical, &memory_properties);
    VkCommandPoolCreateInfo pool{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    pool.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    pool.queueFamilyIndex = queue_family;
    VK_CHECK(vkCreateCommandPool(device, &pool, nullptr, &command_pool));
}

void VkContext::destroy() {
    if (!device) return;
    vkDeviceWaitIdle(device);
    flush_deferred();
    if (command_pool) vkDestroyCommandPool(device, command_pool, nullptr);
    command_pool = VK_NULL_HANDLE;
    // The device and instance were created through the OpenXR runtime but
    // belong to the app; destroy them after the XR session is gone.
    vkDestroyDevice(device, nullptr);
    device = VK_NULL_HANDLE;
    if (instance) vkDestroyInstance(instance, nullptr);
    instance = VK_NULL_HANDLE;
}

uint32_t VkContext::memory_type(uint32_t type_bits, VkMemoryPropertyFlags flags) const {
    for (uint32_t i = 0; i < memory_properties.memoryTypeCount; ++i) {
        if ((type_bits & (1u << i)) &&
            (memory_properties.memoryTypes[i].propertyFlags & flags) == flags) {
            return i;
        }
    }
    fail("no Vulkan memory type with flags " + std::to_string(flags));
}

Buffer VkContext::create_buffer(VkDeviceSize size, VkBufferUsageFlags usage) {
    Buffer b;
    b.size = std::max<VkDeviceSize>(size, 4);
    VkBufferCreateInfo info{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    info.size = b.size;
    info.usage = usage;
    info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    VK_CHECK(vkCreateBuffer(device, &info, nullptr, &b.buffer));
    VkMemoryRequirements req;
    vkGetBufferMemoryRequirements(device, b.buffer, &req);
    VkMemoryAllocateInfo alloc{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    alloc.allocationSize = req.size;
    alloc.memoryTypeIndex = memory_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                                                VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    VK_CHECK(vkAllocateMemory(device, &alloc, nullptr, &b.memory));
    VK_CHECK(vkBindBufferMemory(device, b.buffer, b.memory, 0));
    VK_CHECK(vkMapMemory(device, b.memory, 0, VK_WHOLE_SIZE, 0, &b.mapped));
    return b;
}

void VkContext::destroy_buffer(Buffer& b) {
    if (b.buffer) vkDestroyBuffer(device, b.buffer, nullptr);
    if (b.memory) vkFreeMemory(device, b.memory, nullptr);
    b = Buffer{};
}

Image VkContext::create_image(uint32_t width, uint32_t height, uint32_t mips, VkFormat format,
                              VkImageUsageFlags usage, VkSampleCountFlagBits samples,
                              VkImageAspectFlags aspect) {
    Image img;
    img.format = format;
    img.width = width;
    img.height = height;
    img.mip_levels = mips;
    VkImageCreateInfo info{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    info.imageType = VK_IMAGE_TYPE_2D;
    info.format = format;
    info.extent = {width, height, 1};
    info.mipLevels = mips;
    info.arrayLayers = 1;
    info.samples = samples;
    info.tiling = VK_IMAGE_TILING_OPTIMAL;
    info.usage = usage;
    info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    VK_CHECK(vkCreateImage(device, &info, nullptr, &img.image));
    VkMemoryRequirements req;
    vkGetImageMemoryRequirements(device, img.image, &req);
    VkMemoryAllocateInfo alloc{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    alloc.allocationSize = req.size;
    // MSAA attachments are transient where the GPU supports lazily allocated
    // memory (tilers like the Frame's Adreno keep them on chip)
    uint32_t type = UINT32_MAX;
    if (usage & VK_IMAGE_USAGE_TRANSIENT_ATTACHMENT_BIT) {
        for (uint32_t i = 0; i < memory_properties.memoryTypeCount; ++i) {
            if ((req.memoryTypeBits & (1u << i)) &&
                (memory_properties.memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_LAZILY_ALLOCATED_BIT)) {
                type = i;
                break;
            }
        }
    }
    alloc.memoryTypeIndex = type != UINT32_MAX ? type
                                               : memory_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    VK_CHECK(vkAllocateMemory(device, &alloc, nullptr, &img.memory));
    VK_CHECK(vkBindImageMemory(device, img.image, img.memory, 0));
    VkImageViewCreateInfo view{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    view.image = img.image;
    view.viewType = VK_IMAGE_VIEW_TYPE_2D;
    view.format = format;
    view.subresourceRange = {aspect, 0, mips, 0, 1};
    VK_CHECK(vkCreateImageView(device, &view, nullptr, &img.view));
    return img;
}

void VkContext::destroy_image(Image& img) {
    if (img.view) vkDestroyImageView(device, img.view, nullptr);
    if (img.image) vkDestroyImage(device, img.image, nullptr);
    if (img.memory) vkFreeMemory(device, img.memory, nullptr);
    img = Image{};
}

bool VkContext::format_supported(VkFormat format, VkFormatFeatureFlags features) const {
    VkFormatProperties props;
    vkGetPhysicalDeviceFormatProperties(physical, format, &props);
    return (props.optimalTilingFeatures & features) == features;
}

void VkContext::submit_now(const std::function<void(VkCommandBuffer)>& record) {
    VkCommandBufferAllocateInfo alloc{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    alloc.commandPool = command_pool;
    alloc.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    alloc.commandBufferCount = 1;
    VkCommandBuffer cmd;
    VK_CHECK(vkAllocateCommandBuffers(device, &alloc, &cmd));
    VkCommandBufferBeginInfo begin{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    begin.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    VK_CHECK(vkBeginCommandBuffer(cmd, &begin));
    record(cmd);
    VK_CHECK(vkEndCommandBuffer(cmd));
    VkFenceCreateInfo fence_info{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    VkFence fence;
    VK_CHECK(vkCreateFence(device, &fence_info, nullptr, &fence));
    VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};
    submit.commandBufferCount = 1;
    submit.pCommandBuffers = &cmd;
    VK_CHECK(vkQueueSubmit(queue, 1, &submit, fence));
    VK_CHECK(vkWaitForFences(device, 1, &fence, VK_TRUE, UINT64_MAX));
    vkDestroyFence(device, fence, nullptr);
    vkFreeCommandBuffers(device, command_pool, 1, &cmd);
}

static void barrier(VkCommandBuffer cmd, VkImage image, uint32_t base_mip, uint32_t mips,
                    VkImageLayout from, VkImageLayout to, VkAccessFlags src_access,
                    VkAccessFlags dst_access, VkPipelineStageFlags src_stage,
                    VkPipelineStageFlags dst_stage) {
    VkImageMemoryBarrier b{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    b.oldLayout = from;
    b.newLayout = to;
    b.srcAccessMask = src_access;
    b.dstAccessMask = dst_access;
    b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b.image = image;
    b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, base_mip, mips, 0, 1};
    vkCmdPipelineBarrier(cmd, src_stage, dst_stage, 0, 0, nullptr, 0, nullptr, 1, &b);
}

Image VkContext::upload_texture(uint32_t width, uint32_t height, VkFormat format,
                                const std::vector<std::vector<uint8_t>>& levels, bool generate_mips) {
    uint32_t mips = uint32_t(levels.size());
    if (generate_mips) {
        mips = 1;
        for (uint32_t s = std::max(width, height); s > 1; s >>= 1) ++mips;
    }
    VkImageUsageFlags usage = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    if (generate_mips) usage |= VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
    Image img = create_image(width, height, mips, format, usage);

    VkDeviceSize total = 0;
    for (auto& level : levels) total += (level.size() + 15) & ~VkDeviceSize(15);
    Buffer staging = create_buffer(total, VK_BUFFER_USAGE_TRANSFER_SRC_BIT);
    std::vector<VkBufferImageCopy> copies;
    VkDeviceSize offset = 0;
    for (uint32_t i = 0; i < levels.size(); ++i) {
        std::memcpy(static_cast<uint8_t*>(staging.mapped) + offset, levels[i].data(), levels[i].size());
        VkBufferImageCopy copy{};
        copy.bufferOffset = offset;
        copy.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, i, 0, 1};
        copy.imageExtent = {std::max(1u, width >> i), std::max(1u, height >> i), 1};
        copies.push_back(copy);
        offset += (levels[i].size() + 15) & ~VkDeviceSize(15);
    }

    submit_now([&](VkCommandBuffer cmd) {
        barrier(cmd, img.image, 0, mips, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                0, VK_ACCESS_TRANSFER_WRITE_BIT, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                VK_PIPELINE_STAGE_TRANSFER_BIT);
        vkCmdCopyBufferToImage(cmd, staging.buffer, img.image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                               uint32_t(copies.size()), copies.data());
        if (generate_mips) {
            for (uint32_t i = 1; i < mips; ++i) {
                barrier(cmd, img.image, i - 1, 1, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                        VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_ACCESS_TRANSFER_WRITE_BIT,
                        VK_ACCESS_TRANSFER_READ_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                        VK_PIPELINE_STAGE_TRANSFER_BIT);
                VkImageBlit blit{};
                blit.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, i - 1, 0, 1};
                blit.srcOffsets[1] = {int32_t(std::max(1u, width >> (i - 1))),
                                      int32_t(std::max(1u, height >> (i - 1))), 1};
                blit.dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, i, 0, 1};
                blit.dstOffsets[1] = {int32_t(std::max(1u, width >> i)), int32_t(std::max(1u, height >> i)), 1};
                vkCmdBlitImage(cmd, img.image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, img.image,
                               VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &blit, VK_FILTER_LINEAR);
            }
            barrier(cmd, img.image, 0, mips - 1, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                    VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_ACCESS_TRANSFER_READ_BIT,
                    VK_ACCESS_SHADER_READ_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                    VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT);
            barrier(cmd, img.image, mips - 1, 1, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                    VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_ACCESS_TRANSFER_WRITE_BIT,
                    VK_ACCESS_SHADER_READ_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                    VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT);
        } else {
            barrier(cmd, img.image, 0, mips, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                    VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_ACCESS_TRANSFER_WRITE_BIT,
                    VK_ACCESS_SHADER_READ_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                    VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT);
        }
    });
    destroy_buffer(staging);
    return img;
}

void VkContext::defer(std::function<void()> release) {
    deferred_.push_back({current_frame_, std::move(release)});
}

void VkContext::frame_completed(uint64_t frame) {
    auto it = std::partition(deferred_.begin(), deferred_.end(),
                             [frame](const Deferred& d) { return d.frame > frame; });
    std::vector<Deferred> ready(std::make_move_iterator(it), std::make_move_iterator(deferred_.end()));
    deferred_.erase(it, deferred_.end());
    for (auto& d : ready) d.release();
}

void VkContext::flush_deferred() {
    auto all = std::move(deferred_);
    deferred_.clear();
    for (auto& d : all) d.release();
}
