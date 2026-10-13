#include "renderer.h"

#include "common.h"
#include "textures.h"

#include <algorithm>
#include <cstring>

namespace {
const uint32_t kMeshVert[] = {
#include "mesh.vert.spv.inc"
};
const uint32_t kMeshFrag[] = {
#include "mesh.frag.spv.inc"
};
const uint32_t kSkyVert[] = {
#include "sky.vert.spv.inc"
};
const uint32_t kSkyFrag[] = {
#include "sky.frag.spv.inc"
};

struct PushConstants {
    glm::mat4 model;
    glm::vec4 material;
    glm::vec4 emission;
};
static_assert(sizeof(PushConstants) == 96);

VkShaderModule shader_module(VkDevice device, const uint32_t* code, size_t bytes) {
    VkShaderModuleCreateInfo info{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
    info.codeSize = bytes;
    info.pCode = code;
    VkShaderModule module;
    VK_CHECK(vkCreateShaderModule(device, &info, nullptr, &module));
    return module;
}
}  // namespace

void Renderer::init(VkContext& vk, XrContext& xr, int msaa_samples) {
    vk_ = &vk;
    xr_ = &xr;
    VkDevice device = vk.device;

    // MSAA: the highest supported count not above the request
    VkSampleCountFlags counts = vk.properties.limits.framebufferColorSampleCounts &
                                vk.properties.limits.framebufferDepthSampleCounts;
    samples_ = VK_SAMPLE_COUNT_1_BIT;
    for (int s : {8, 4, 2}) {
        if (s <= msaa_samples && (counts & VkSampleCountFlags(s))) {
            samples_ = VkSampleCountFlagBits(s);
            break;
        }
    }
    LOG("MSAA %dx (requested %d)", int(samples_), msaa_samples);

    // Depth-only formats (no stencil aspect to manage)
    for (VkFormat f : {VK_FORMAT_D32_SFLOAT, VK_FORMAT_X8_D24_UNORM_PACK32, VK_FORMAT_D16_UNORM}) {
        if (vk.format_supported(f, VK_FORMAT_FEATURE_DEPTH_STENCIL_ATTACHMENT_BIT)) {
            depth_format_ = f;
            break;
        }
    }

    // Render pass: [msaa colour, depth, resolve -> swapchain] or [swapchain, depth]
    const bool msaa = samples_ != VK_SAMPLE_COUNT_1_BIT;
    std::vector<VkAttachmentDescription> attachments;
    VkAttachmentDescription color{};
    color.format = xr.color_format;
    color.samples = samples_;
    color.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    color.storeOp = msaa ? VK_ATTACHMENT_STORE_OP_DONT_CARE : VK_ATTACHMENT_STORE_OP_STORE;
    color.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    color.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    color.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    color.finalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    attachments.push_back(color);
    VkAttachmentDescription depth{};
    depth.format = depth_format_;
    depth.samples = samples_;
    depth.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    depth.storeOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    depth.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    depth.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    depth.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    depth.finalLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    attachments.push_back(depth);
    if (msaa) {
        // Resolve averages colour AND alpha, so silhouette edges also blend
        // smoothly against passthrough
        VkAttachmentDescription resolve = color;
        resolve.samples = VK_SAMPLE_COUNT_1_BIT;
        resolve.loadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        resolve.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
        attachments.push_back(resolve);
    }
    VkAttachmentReference color_ref{0, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
    VkAttachmentReference depth_ref{1, VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL};
    VkAttachmentReference resolve_ref{2, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = 1;
    subpass.pColorAttachments = &color_ref;
    subpass.pDepthStencilAttachment = &depth_ref;
    subpass.pResolveAttachments = msaa ? &resolve_ref : nullptr;
    VkSubpassDependency dependency{};
    dependency.srcSubpass = VK_SUBPASS_EXTERNAL;
    dependency.dstSubpass = 0;
    dependency.srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT;
    dependency.dstStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT | VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT;
    dependency.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT | VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT;
    VkRenderPassCreateInfo rp{VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO};
    rp.attachmentCount = uint32_t(attachments.size());
    rp.pAttachments = attachments.data();
    rp.subpassCount = 1;
    rp.pSubpasses = &subpass;
    rp.dependencyCount = 1;
    rp.pDependencies = &dependency;
    VK_CHECK(vkCreateRenderPass(device, &rp, nullptr, &render_pass_));

    create_targets();

    // Descriptor layouts: set 0 = per-eye frame uniforms, set 1 = material
    VkDescriptorSetLayoutBinding frame_binding{0, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC, 1,
                                               VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT};
    VkDescriptorSetLayoutCreateInfo layout{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
    layout.bindingCount = 1;
    layout.pBindings = &frame_binding;
    VK_CHECK(vkCreateDescriptorSetLayout(device, &layout, nullptr, &frame_layout_));
    VkDescriptorSetLayoutBinding material_bindings[5];
    for (uint32_t i = 0; i < 5; ++i) {
        material_bindings[i] = {i, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1, VK_SHADER_STAGE_FRAGMENT_BIT};
    }
    layout.bindingCount = 5;
    layout.pBindings = material_bindings;
    VK_CHECK(vkCreateDescriptorSetLayout(device, &layout, nullptr, &material_layout_));
    VkDescriptorSetLayout set_layouts[2] = {frame_layout_, material_layout_};
    VkPushConstantRange push{VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT, 0, sizeof(PushConstants)};
    VkPipelineLayoutCreateInfo pl{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
    pl.setLayoutCount = 2;
    pl.pSetLayouts = set_layouts;
    pl.pushConstantRangeCount = 1;
    pl.pPushConstantRanges = &push;
    VK_CHECK(vkCreatePipelineLayout(device, &pl, nullptr, &pipeline_layout_));

    VkDescriptorPoolSize sizes[2] = {{VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC, FRAMES_IN_FLIGHT},
                                     {VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 5 * 512}};
    VkDescriptorPoolCreateInfo pool{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
    pool.flags = VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT;
    pool.maxSets = 512 + FRAMES_IN_FLIGHT;
    pool.poolSizeCount = 2;
    pool.pPoolSizes = sizes;
    VK_CHECK(vkCreateDescriptorPool(device, &pool, nullptr, &pool_));

    // Samplers: materials repeat; UI text clamps with max anisotropy and a
    // sharpening LOD bias (text is read at a steep angle below the chin)
    VkSamplerCreateInfo sampler{VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO};
    sampler.magFilter = sampler.minFilter = VK_FILTER_LINEAR;
    sampler.mipmapMode = VK_SAMPLER_MIPMAP_MODE_LINEAR;
    sampler.addressModeU = sampler.addressModeV = sampler.addressModeW = VK_SAMPLER_ADDRESS_MODE_REPEAT;
    sampler.anisotropyEnable = vk.anisotropy;
    sampler.maxAnisotropy = std::min(8.0f, vk.properties.limits.maxSamplerAnisotropy);
    sampler.maxLod = VK_LOD_CLAMP_NONE;
    VK_CHECK(vkCreateSampler(device, &sampler, nullptr, &material_sampler_));
    sampler.addressModeU = sampler.addressModeV = sampler.addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    sampler.maxAnisotropy = std::min(16.0f, vk.properties.limits.maxSamplerAnisotropy);
    sampler.mipLodBias = std::max(-0.75f, -vk.properties.limits.maxSamplerLodBias);
    VK_CHECK(vkCreateSampler(device, &sampler, nullptr, &ui_sampler_));

    default_normal = upload(vk, solid_texture(0.5f, 0.5f, 1.0f));
    default_white = upload(vk, solid_texture(1.0f, 1.0f, 1.0f));
    default_black = upload(vk, solid_texture(0.0f, 0.0f, 0.0f));

    create_pipelines();

    // Per-slot command buffers, fences and the per-eye uniform buffer
    uniform_stride_ = sizeof(FrameUniforms);
    VkDeviceSize align = vk.properties.limits.minUniformBufferOffsetAlignment;
    if (align) uniform_stride_ = (uniform_stride_ + align - 1) / align * align;
    for (auto& slot : slots_) {
        VkCommandBufferAllocateInfo alloc{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
        alloc.commandPool = vk.command_pool;
        alloc.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
        alloc.commandBufferCount = 1;
        VK_CHECK(vkAllocateCommandBuffers(device, &alloc, &slot.cmd));
        VkFenceCreateInfo fence{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
        VK_CHECK(vkCreateFence(device, &fence, nullptr, &slot.fence));
        slot.uniforms = vk.create_buffer(uniform_stride_ * 2, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT);
        VkDescriptorSetAllocateInfo set_alloc{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
        set_alloc.descriptorPool = pool_;
        set_alloc.descriptorSetCount = 1;
        set_alloc.pSetLayouts = &frame_layout_;
        VK_CHECK(vkAllocateDescriptorSets(device, &set_alloc, &slot.frame_set));
        VkDescriptorBufferInfo buffer_info{slot.uniforms.buffer, 0, sizeof(FrameUniforms)};
        VkWriteDescriptorSet write{VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};
        write.dstSet = slot.frame_set;
        write.descriptorCount = 1;
        write.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC;
        write.pBufferInfo = &buffer_info;
        vkUpdateDescriptorSets(device, 1, &write, 0, nullptr);
    }
}

void Renderer::create_targets() {
    const bool msaa = samples_ != VK_SAMPLE_COUNT_1_BIT;
    for (int eye = 0; eye < 2; ++eye) {
        const EyeSwapchain& sc = xr_->eyes[eye];
        EyeTarget& t = targets_[eye];
        if (msaa) {
            t.color_msaa = vk_->create_image(sc.width, sc.height, 1, xr_->color_format,
                                             VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSIENT_ATTACHMENT_BIT,
                                             samples_);
        }
        t.depth = vk_->create_image(sc.width, sc.height, 1, depth_format_,
                                    VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSIENT_ATTACHMENT_BIT,
                                    samples_, VK_IMAGE_ASPECT_DEPTH_BIT);
        for (VkImage image : sc.images) {
            VkImageViewCreateInfo view{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
            view.image = image;
            view.viewType = VK_IMAGE_VIEW_TYPE_2D;
            view.format = xr_->color_format;
            view.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
            VkImageView image_view;
            VK_CHECK(vkCreateImageView(vk_->device, &view, nullptr, &image_view));
            t.swapchain_views.push_back(image_view);
            std::vector<VkImageView> views = msaa ? std::vector<VkImageView>{t.color_msaa.view, t.depth.view, image_view}
                                                  : std::vector<VkImageView>{image_view, t.depth.view};
            VkFramebufferCreateInfo fb{VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO};
            fb.renderPass = render_pass_;
            fb.attachmentCount = uint32_t(views.size());
            fb.pAttachments = views.data();
            fb.width = sc.width;
            fb.height = sc.height;
            fb.layers = 1;
            VkFramebuffer framebuffer;
            VK_CHECK(vkCreateFramebuffer(vk_->device, &fb, nullptr, &framebuffer));
            t.framebuffers.push_back(framebuffer);
        }
    }
}

VkPipeline Renderer::make_pipeline(VkShaderModule vert, VkShaderModule frag, bool mesh, bool blend, bool depth) {
    VkPipelineShaderStageCreateInfo stages[2] = {
        {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, nullptr, 0, VK_SHADER_STAGE_VERTEX_BIT, vert, "main"},
        {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, nullptr, 0, VK_SHADER_STAGE_FRAGMENT_BIT, frag, "main"},
    };
    VkVertexInputBindingDescription bindings[2] = {
        {0, sizeof(glm::vec3), VK_VERTEX_INPUT_RATE_VERTEX},
        {1, sizeof(float) * 5, VK_VERTEX_INPUT_RATE_VERTEX},
    };
    VkVertexInputAttributeDescription attributes[3] = {
        {0, 0, VK_FORMAT_R32G32B32_SFLOAT, 0},
        {1, 1, VK_FORMAT_R32G32B32_SFLOAT, 0},
        {2, 1, VK_FORMAT_R32G32_SFLOAT, sizeof(float) * 3},
    };
    VkPipelineVertexInputStateCreateInfo vertex_input{VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO};
    if (mesh) {
        vertex_input.vertexBindingDescriptionCount = 2;
        vertex_input.pVertexBindingDescriptions = bindings;
        vertex_input.vertexAttributeDescriptionCount = 3;
        vertex_input.pVertexAttributeDescriptions = attributes;
    }
    VkPipelineInputAssemblyStateCreateInfo assembly{VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO};
    assembly.topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;
    VkPipelineViewportStateCreateInfo viewport{VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO};
    viewport.viewportCount = 1;
    viewport.scissorCount = 1;
    VkPipelineRasterizationStateCreateInfo raster{VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO};
    raster.polygonMode = VK_POLYGON_MODE_FILL;
    raster.cullMode = VK_CULL_MODE_NONE;  // the GL app never enabled culling
    raster.frontFace = VK_FRONT_FACE_COUNTER_CLOCKWISE;
    raster.lineWidth = 1.0f;
    VkPipelineMultisampleStateCreateInfo multisample{VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO};
    multisample.rasterizationSamples = samples_;
    VkPipelineDepthStencilStateCreateInfo depth_state{VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO};
    depth_state.depthTestEnable = depth;
    depth_state.depthWriteEnable = depth && !blend;
    depth_state.depthCompareOp = VK_COMPARE_OP_LESS;
    VkPipelineColorBlendAttachmentState blend_attachment{};
    blend_attachment.colorWriteMask = VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT |
                                      VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT;
    if (blend) {
        // glBlendFunc(SRC_ALPHA, ONE_MINUS_SRC_ALPHA) applies to alpha too
        blend_attachment.blendEnable = VK_TRUE;
        blend_attachment.srcColorBlendFactor = blend_attachment.srcAlphaBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA;
        blend_attachment.dstColorBlendFactor = blend_attachment.dstAlphaBlendFactor =
            VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
        blend_attachment.colorBlendOp = blend_attachment.alphaBlendOp = VK_BLEND_OP_ADD;
    }
    VkPipelineColorBlendStateCreateInfo blend_state{VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO};
    blend_state.attachmentCount = 1;
    blend_state.pAttachments = &blend_attachment;
    VkDynamicState dynamic[2] = {VK_DYNAMIC_STATE_VIEWPORT, VK_DYNAMIC_STATE_SCISSOR};
    VkPipelineDynamicStateCreateInfo dynamic_state{VK_STRUCTURE_TYPE_PIPELINE_DYNAMIC_STATE_CREATE_INFO};
    dynamic_state.dynamicStateCount = 2;
    dynamic_state.pDynamicStates = dynamic;
    VkGraphicsPipelineCreateInfo info{VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO};
    info.stageCount = 2;
    info.pStages = stages;
    info.pVertexInputState = &vertex_input;
    info.pInputAssemblyState = &assembly;
    info.pViewportState = &viewport;
    info.pRasterizationState = &raster;
    info.pMultisampleState = &multisample;
    info.pDepthStencilState = &depth_state;
    info.pColorBlendState = &blend_state;
    info.pDynamicState = &dynamic_state;
    info.layout = pipeline_layout_;
    info.renderPass = render_pass_;
    VkPipeline pipeline;
    VK_CHECK(vkCreateGraphicsPipelines(vk_->device, VK_NULL_HANDLE, 1, &info, nullptr, &pipeline));
    return pipeline;
}

void Renderer::create_pipelines() {
    VkDevice device = vk_->device;
    VkShaderModule mesh_vert = shader_module(device, kMeshVert, sizeof(kMeshVert));
    VkShaderModule mesh_frag = shader_module(device, kMeshFrag, sizeof(kMeshFrag));
    VkShaderModule sky_vert = shader_module(device, kSkyVert, sizeof(kSkyVert));
    VkShaderModule sky_frag = shader_module(device, kSkyFrag, sizeof(kSkyFrag));
    opaque_ = make_pipeline(mesh_vert, mesh_frag, true, false, true);
    blended_ = make_pipeline(mesh_vert, mesh_frag, true, true, true);
    sky_ = make_pipeline(sky_vert, sky_frag, false, false, false);
    for (VkShaderModule m : {mesh_vert, mesh_frag, sky_vert, sky_frag}) vkDestroyShaderModule(device, m, nullptr);
}

VkDescriptorSet Renderer::material_set(VkImageView base, VkImageView normal, VkImageView metallic,
                                       VkImageView ao, VkImageView emission, bool ui) {
    VkDescriptorSetAllocateInfo alloc{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
    alloc.descriptorPool = pool_;
    alloc.descriptorSetCount = 1;
    alloc.pSetLayouts = &material_layout_;
    VkDescriptorSet set;
    VK_CHECK(vkAllocateDescriptorSets(vk_->device, &alloc, &set));
    VkImageView views[5] = {base, normal ? normal : default_normal.view, metallic ? metallic : default_white.view,
                            ao ? ao : default_white.view, emission ? emission : default_black.view};
    VkDescriptorImageInfo infos[5];
    VkWriteDescriptorSet writes[5];
    for (uint32_t i = 0; i < 5; ++i) {
        infos[i] = {ui && i == 0 ? ui_sampler_ : material_sampler_, views[i],
                    VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL};
        writes[i] = {VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};
        writes[i].dstSet = set;
        writes[i].dstBinding = i;
        writes[i].descriptorCount = 1;
        writes[i].descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
        writes[i].pImageInfo = &infos[i];
    }
    vkUpdateDescriptorSets(vk_->device, 5, writes, 0, nullptr);
    return set;
}

void Renderer::free_material_set(VkDescriptorSet set) {
    VkDevice device = vk_->device;
    VkDescriptorPool pool = pool_;
    vk_->defer([device, pool, set] { vkFreeDescriptorSets(device, pool, 1, &set); });
}

GpuMesh Renderer::create_mesh(const MeshData& data) {
    GpuMesh mesh;
    mesh.vertex_count = uint32_t(data.positions.size());
    mesh.positions = vk_->create_buffer(data.positions.size() * sizeof(glm::vec3), VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
    std::memcpy(mesh.positions.mapped, data.positions.data(), data.positions.size() * sizeof(glm::vec3));
    mesh.attributes = vk_->create_buffer(data.positions.size() * sizeof(float) * 5, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT);
    auto* attr = static_cast<float*>(mesh.attributes.mapped);
    for (size_t i = 0; i < data.positions.size(); ++i) {
        attr[i * 5 + 0] = data.normals[i].x;
        attr[i * 5 + 1] = data.normals[i].y;
        attr[i * 5 + 2] = data.normals[i].z;
        attr[i * 5 + 3] = data.uvs[i].x;
        attr[i * 5 + 4] = data.uvs[i].y;
    }
    if (!data.indices.empty()) {
        mesh.index_count = uint32_t(data.indices.size());
        mesh.indices = vk_->create_buffer(data.indices.size() * 4, VK_BUFFER_USAGE_INDEX_BUFFER_BIT);
        std::memcpy(mesh.indices.mapped, data.indices.data(), data.indices.size() * 4);
    }
    return mesh;
}

void Renderer::destroy_mesh(GpuMesh& mesh) {
    vk_->destroy_buffer(mesh.positions);
    vk_->destroy_buffer(mesh.attributes);
    vk_->destroy_buffer(mesh.indices);
    mesh = GpuMesh{};
}

VkCommandBuffer Renderer::begin_frame() {
    slot_ = int(frame_index_ % FRAMES_IN_FLIGHT);
    Slot& slot = slots_[slot_];
    if (slot.submitted) {
        VK_CHECK(vkWaitForFences(vk_->device, 1, &slot.fence, VK_TRUE, UINT64_MAX));
        slot.submitted = false;
        vk_->frame_completed(slot.frame);
    }
    vk_->set_current_frame(frame_index_);
    VK_CHECK(vkResetCommandBuffer(slot.cmd, 0));
    VkCommandBufferBeginInfo begin{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    begin.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    VK_CHECK(vkBeginCommandBuffer(slot.cmd, &begin));
    return slot.cmd;
}

void Renderer::begin_eye(int eye, uint32_t image_index, const FrameUniforms& uniforms) {
    Slot& slot = slots_[slot_];
    current_eye_ = eye;
    std::memcpy(static_cast<uint8_t*>(slot.uniforms.mapped) + uniform_stride_ * eye, &uniforms, sizeof(uniforms));
    const EyeSwapchain& sc = xr_->eyes[eye];
    VkClearValue clears[3];
    clears[0].color = {{0.0f, 0.0f, 0.0f, 0.0f}};  // alpha 0: passthrough shows through
    clears[1].depthStencil = {1.0f, 0};
    clears[2].color = clears[0].color;
    VkRenderPassBeginInfo begin{VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO};
    begin.renderPass = render_pass_;
    begin.framebuffer = targets_[eye].framebuffers[image_index];
    begin.renderArea = {{0, 0}, {sc.width, sc.height}};
    begin.clearValueCount = samples_ != VK_SAMPLE_COUNT_1_BIT ? 3 : 2;
    begin.pClearValues = clears;
    vkCmdBeginRenderPass(slot.cmd, &begin, VK_SUBPASS_CONTENTS_INLINE);
    VkViewport viewport{0.0f, 0.0f, float(sc.width), float(sc.height), 0.0f, 1.0f};
    VkRect2D scissor{{0, 0}, {sc.width, sc.height}};
    vkCmdSetViewport(slot.cmd, 0, 1, &viewport);
    vkCmdSetScissor(slot.cmd, 0, 1, &scissor);
    uint32_t offset = uint32_t(uniform_stride_ * eye);
    vkCmdBindPipeline(slot.cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, sky_);
    vkCmdBindDescriptorSets(slot.cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline_layout_, 0, 1, &slot.frame_set, 1,
                            &offset);
    vkCmdDraw(slot.cmd, 3, 1, 0, 0);
    bound_pipeline_ = sky_;
}

void Renderer::draw(const GpuMesh& mesh, VkDescriptorSet material, const glm::mat4& model,
                    const MaterialParams& params, Pass pass, const Buffer* positions_override) {
    VkCommandBuffer cmd = slots_[slot_].cmd;
    VkPipeline pipeline = pass == Pass::Blended ? blended_ : opaque_;
    if (pipeline != bound_pipeline_) {
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline);
        bound_pipeline_ = pipeline;
    }
    vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline_layout_, 1, 1, &material, 0, nullptr);
    PushConstants push{model, glm::vec4(params.bump_scale, params.metallic, params.smoothness, params.occlusion),
                       glm::vec4(params.emission, 0.0f)};
    vkCmdPushConstants(cmd, pipeline_layout_, VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT, 0,
                       sizeof(push), &push);
    VkBuffer buffers[2] = {positions_override ? positions_override->buffer : mesh.positions.buffer,
                           mesh.attributes.buffer};
    VkDeviceSize offsets[2] = {0, 0};
    vkCmdBindVertexBuffers(cmd, 0, 2, buffers, offsets);
    if (mesh.index_count) {
        vkCmdBindIndexBuffer(cmd, mesh.indices.buffer, 0, VK_INDEX_TYPE_UINT32);
        vkCmdDrawIndexed(cmd, mesh.index_count, 1, 0, 0, 0);
    } else {
        vkCmdDraw(cmd, mesh.vertex_count, 1, 0, 0);
    }
}

void Renderer::end_eye() {
    vkCmdEndRenderPass(slots_[slot_].cmd);
}

void Renderer::end_frame() {
    Slot& slot = slots_[slot_];
    VK_CHECK(vkEndCommandBuffer(slot.cmd));
    VK_CHECK(vkResetFences(vk_->device, 1, &slot.fence));
    VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};
    submit.commandBufferCount = 1;
    submit.pCommandBuffers = &slot.cmd;
    VK_CHECK(vkQueueSubmit(vk_->queue, 1, &submit, slot.fence));
    slot.submitted = true;
    slot.frame = frame_index_;
    ++frame_index_;
}

void Renderer::destroy() {
    if (!vk_) return;
    VkDevice device = vk_->device;
    vkDeviceWaitIdle(device);
    vk_->flush_deferred();
    for (auto& slot : slots_) {
        vkDestroyFence(device, slot.fence, nullptr);
        vkFreeCommandBuffers(device, vk_->command_pool, 1, &slot.cmd);
        vk_->destroy_buffer(slot.uniforms);
    }
    for (auto& t : targets_) {
        for (auto fb : t.framebuffers) vkDestroyFramebuffer(device, fb, nullptr);
        for (auto view : t.swapchain_views) vkDestroyImageView(device, view, nullptr);
        vk_->destroy_image(t.color_msaa);
        vk_->destroy_image(t.depth);
    }
    for (VkPipeline p : {opaque_, blended_, sky_}) vkDestroyPipeline(device, p, nullptr);
    vk_->destroy_image(default_normal);
    vk_->destroy_image(default_white);
    vk_->destroy_image(default_black);
    vkDestroySampler(device, material_sampler_, nullptr);
    vkDestroySampler(device, ui_sampler_, nullptr);
    vkDestroyDescriptorPool(device, pool_, nullptr);
    vkDestroyPipelineLayout(device, pipeline_layout_, nullptr);
    vkDestroyDescriptorSetLayout(device, frame_layout_, nullptr);
    vkDestroyDescriptorSetLayout(device, material_layout_, nullptr);
    vkDestroyRenderPass(device, render_pass_, nullptr);
    vk_ = nullptr;
}
