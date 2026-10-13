#include "xr_context.h"

#include "common.h"
#include "version.h"

#include <glm/gtc/quaternion.hpp>

#include <algorithm>
#include <cmath>
#include <cstring>

template <typename T>
static T load_xr_function(XrInstance instance, const char* name) {
    PFN_xrVoidFunction fn = nullptr;
    XR_CHECK(xrGetInstanceProcAddr(instance, name, &fn));
    return reinterpret_cast<T>(fn);
}

void XrContext::create(VkContext& vk, float render_scale, bool want_passthrough) {
    // XR_FB_display_refresh_rate is optional: without it the session keeps
    // whatever rate SteamVR picks for this app (72 Hz in the first run)
    uint32_t available_count = 0;
    XR_CHECK(xrEnumerateInstanceExtensionProperties(nullptr, 0, &available_count, nullptr));
    std::vector<XrExtensionProperties> available(available_count, {XR_TYPE_EXTENSION_PROPERTIES});
    XR_CHECK(xrEnumerateInstanceExtensionProperties(nullptr, available_count, &available_count, available.data()));
    auto offered = [&](const char* name) {
        return std::any_of(available.begin(), available.end(),
                           [&](const XrExtensionProperties& e) { return std::strcmp(e.extensionName, name) == 0; });
    };
    std::vector<const char*> extensions = {XR_KHR_VULKAN_ENABLE2_EXTENSION_NAME};
    const bool refresh_rate_ext = offered(XR_FB_DISPLAY_REFRESH_RATE_EXTENSION_NAME);
    if (refresh_rate_ext) extensions.push_back(XR_FB_DISPLAY_REFRESH_RATE_EXTENSION_NAME);
    else LOG("[xr] %s not offered; the display rate is up to SteamVR", XR_FB_DISPLAY_REFRESH_RATE_EXTENSION_NAME);

    XrInstanceCreateInfo info{XR_TYPE_INSTANCE_CREATE_INFO};
    std::strncpy(info.applicationInfo.applicationName, "Babble Dataset Capture",
                 XR_MAX_APPLICATION_NAME_SIZE - 1);
    info.applicationInfo.applicationVersion = 1;
    std::strncpy(info.applicationInfo.engineName, "babble-dataset-capture", XR_MAX_ENGINE_NAME_SIZE - 1);
    info.applicationInfo.apiVersion = XR_API_VERSION_1_0;
    info.enabledExtensionCount = uint32_t(extensions.size());
    info.enabledExtensionNames = extensions.data();
    XR_CHECK(xrCreateInstance(&info, &instance));
    if (refresh_rate_ext) {
        enumerate_refresh_rates_ =
            load_xr_function<PFN_xrEnumerateDisplayRefreshRatesFB>(instance, "xrEnumerateDisplayRefreshRatesFB");
        get_refresh_rate_ = load_xr_function<PFN_xrGetDisplayRefreshRateFB>(instance, "xrGetDisplayRefreshRateFB");
        request_refresh_rate_ =
            load_xr_function<PFN_xrRequestDisplayRefreshRateFB>(instance, "xrRequestDisplayRefreshRateFB");
    }

    XrSystemGetInfo system_info{XR_TYPE_SYSTEM_GET_INFO};
    system_info.formFactor = XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY;
    XR_CHECK(xrGetSystem(instance, &system_info, &system));

    // Vulkan instance + device through the runtime (it adds what it needs)
    auto get_requirements = load_xr_function<PFN_xrGetVulkanGraphicsRequirements2KHR>(
        instance, "xrGetVulkanGraphicsRequirements2KHR");
    auto create_vk_instance = load_xr_function<PFN_xrCreateVulkanInstanceKHR>(
        instance, "xrCreateVulkanInstanceKHR");
    auto get_vk_device = load_xr_function<PFN_xrGetVulkanGraphicsDevice2KHR>(
        instance, "xrGetVulkanGraphicsDevice2KHR");
    auto create_vk_device = load_xr_function<PFN_xrCreateVulkanDeviceKHR>(
        instance, "xrCreateVulkanDeviceKHR");

    XrGraphicsRequirementsVulkan2KHR requirements{XR_TYPE_GRAPHICS_REQUIREMENTS_VULKAN2_KHR};
    XR_CHECK(get_requirements(instance, system, &requirements));

    VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
    app.pApplicationName = "Babble Dataset Capture";
    app.pEngineName = "babble-dataset-capture";
    app.apiVersion = VK_API_VERSION_1_1;
    VkInstanceCreateInfo vk_info{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    vk_info.pApplicationInfo = &app;
    XrVulkanInstanceCreateInfoKHR xr_vk_info{XR_TYPE_VULKAN_INSTANCE_CREATE_INFO_KHR};
    xr_vk_info.systemId = system;
    xr_vk_info.pfnGetInstanceProcAddr = vkGetInstanceProcAddr;
    xr_vk_info.vulkanCreateInfo = &vk_info;
    VkResult vk_result = VK_SUCCESS;
    XR_CHECK(create_vk_instance(instance, &xr_vk_info, &vk.instance, &vk_result));
    VK_CHECK(vk_result);

    XrVulkanGraphicsDeviceGetInfoKHR device_get{XR_TYPE_VULKAN_GRAPHICS_DEVICE_GET_INFO_KHR};
    device_get.systemId = system;
    device_get.vulkanInstance = vk.instance;
    XR_CHECK(get_vk_device(instance, &device_get, &vk.physical));

    uint32_t family_count = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(vk.physical, &family_count, nullptr);
    std::vector<VkQueueFamilyProperties> families(family_count);
    vkGetPhysicalDeviceQueueFamilyProperties(vk.physical, &family_count, families.data());
    vk.queue_family = UINT32_MAX;
    for (uint32_t i = 0; i < family_count; ++i) {
        if (families[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) {
            vk.queue_family = i;
            break;
        }
    }
    if (vk.queue_family == UINT32_MAX) fail("no graphics queue family");

    VkPhysicalDeviceFeatures supported{};
    vkGetPhysicalDeviceFeatures(vk.physical, &supported);
    VkPhysicalDeviceFeatures features{};
    features.samplerAnisotropy = supported.samplerAnisotropy;
    vk.anisotropy = supported.samplerAnisotropy;

    float priority = 1.0f;
    VkDeviceQueueCreateInfo queue_info{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
    queue_info.queueFamilyIndex = vk.queue_family;
    queue_info.queueCount = 1;
    queue_info.pQueuePriorities = &priority;
    VkDeviceCreateInfo device_info{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
    device_info.queueCreateInfoCount = 1;
    device_info.pQueueCreateInfos = &queue_info;
    device_info.pEnabledFeatures = &features;
    XrVulkanDeviceCreateInfoKHR xr_device_info{XR_TYPE_VULKAN_DEVICE_CREATE_INFO_KHR};
    xr_device_info.systemId = system;
    xr_device_info.pfnGetInstanceProcAddr = vkGetInstanceProcAddr;
    xr_device_info.vulkanPhysicalDevice = vk.physical;
    xr_device_info.vulkanCreateInfo = &device_info;
    XR_CHECK(create_vk_device(instance, &xr_device_info, &vk.device, &vk_result));
    VK_CHECK(vk_result);
    vk.init_after_device();
    LOG("Vulkan device: %s", vk.properties.deviceName);

    // Eye buffer sizes, optionally scaled (the compositor upscales)
    uint32_t view_count = 0;
    XR_CHECK(xrEnumerateViewConfigurationViews(instance, system, XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO,
                                               0, &view_count, nullptr));
    view_configs_.assign(view_count, {XR_TYPE_VIEW_CONFIGURATION_VIEW});
    XR_CHECK(xrEnumerateViewConfigurationViews(instance, system, XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO,
                                               view_count, &view_count, view_configs_.data()));
    if (view_count != 2) fail("expected a stereo view configuration");
    for (uint32_t i = 0; i < 2; ++i) {
        eyes[i].width = std::max(2u, uint32_t(view_configs_[i].recommendedImageRectWidth * render_scale)) & ~1u;
        eyes[i].height = std::max(2u, uint32_t(view_configs_[i].recommendedImageRectHeight * render_scale)) & ~1u;
    }
    LOG("Eye buffers: %ux%u per eye (scale %.2f)", eyes[0].width, eyes[0].height, render_scale);

    // ALPHA_BLEND lets the compositor show the passthrough cameras wherever
    // framebuffer alpha is below 1 (the lower-world fade)
    uint32_t mode_count = 0;
    xrEnumerateEnvironmentBlendModes(instance, system, XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO, 0,
                                     &mode_count, nullptr);
    std::vector<XrEnvironmentBlendMode> modes(mode_count);
    xrEnumerateEnvironmentBlendModes(instance, system, XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO,
                                     mode_count, &mode_count, modes.data());
    if (want_passthrough &&
        std::find(modes.begin(), modes.end(), XR_ENVIRONMENT_BLEND_MODE_ALPHA_BLEND) != modes.end()) {
        blend_mode = XR_ENVIRONMENT_BLEND_MODE_ALPHA_BLEND;
        passthrough = true;
    } else {
        blend_mode = modes.empty() ? XR_ENVIRONMENT_BLEND_MODE_OPAQUE : modes[0];
        LOG("Passthrough lower world unavailable (no ALPHA_BLEND); rendering an opaque floor");
    }
}

void XrContext::create_session(VkContext& vk) {
    XrGraphicsBindingVulkan2KHR binding{XR_TYPE_GRAPHICS_BINDING_VULKAN2_KHR};
    binding.instance = vk.instance;
    binding.physicalDevice = vk.physical;
    binding.device = vk.device;
    binding.queueFamilyIndex = vk.queue_family;
    binding.queueIndex = 0;
    XrSessionCreateInfo info{XR_TYPE_SESSION_CREATE_INFO};
    info.next = &binding;
    info.systemId = system;
    XR_CHECK(xrCreateSession(instance, &info, &session));

    XrReferenceSpaceCreateInfo space{XR_TYPE_REFERENCE_SPACE_CREATE_INFO};
    space.referenceSpaceType = XR_REFERENCE_SPACE_TYPE_STAGE;  // floor-aligned
    space.poseInReferenceSpace.orientation.w = 1.0f;
    if (XR_FAILED(xrCreateReferenceSpace(session, &space, &play_space))) {
        LOG("STAGE space unavailable; using LOCAL");
        space.referenceSpaceType = XR_REFERENCE_SPACE_TYPE_LOCAL;
        XR_CHECK(xrCreateReferenceSpace(session, &space, &play_space));
    }

    // Swapchain format: like the Python app, the runtime's most-preferred
    // format that has an alpha channel (the passthrough fade needs
    // destination alpha), so colours look the same as before
    uint32_t count = 0;
    XR_CHECK(xrEnumerateSwapchainFormats(session, 0, &count, nullptr));
    std::vector<int64_t> formats(count);
    XR_CHECK(xrEnumerateSwapchainFormats(session, count, &count, formats.data()));
    const int64_t alpha_formats[] = {VK_FORMAT_R8G8B8A8_UNORM, VK_FORMAT_R8G8B8A8_SRGB,
                                     VK_FORMAT_B8G8R8A8_UNORM, VK_FORMAT_B8G8R8A8_SRGB,
                                     VK_FORMAT_R16G16B16A16_SFLOAT};
    color_format = VK_FORMAT_UNDEFINED;
    for (int64_t f : formats) {
        if (std::find(std::begin(alpha_formats), std::end(alpha_formats), f) != std::end(alpha_formats)) {
            color_format = VkFormat(f);
            break;
        }
    }
    if (color_format == VK_FORMAT_UNDEFINED) {
        if (formats.empty()) fail("runtime offers no swapchain formats");
        color_format = VkFormat(formats[0]);
    }
    LOG("Swapchain format: VkFormat %d", int(color_format));

    for (auto& eye : eyes) {
        XrSwapchainCreateInfo sc{XR_TYPE_SWAPCHAIN_CREATE_INFO};
        sc.usageFlags = XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT;
        sc.format = color_format;
        sc.sampleCount = 1;
        sc.width = eye.width;
        sc.height = eye.height;
        sc.faceCount = 1;
        sc.arraySize = 1;
        sc.mipCount = 1;
        XR_CHECK(xrCreateSwapchain(session, &sc, &eye.swapchain));
        uint32_t image_count = 0;
        XR_CHECK(xrEnumerateSwapchainImages(eye.swapchain, 0, &image_count, nullptr));
        std::vector<XrSwapchainImageVulkan2KHR> images(image_count, {XR_TYPE_SWAPCHAIN_IMAGE_VULKAN2_KHR});
        XR_CHECK(xrEnumerateSwapchainImages(eye.swapchain, image_count, &image_count,
                                            reinterpret_cast<XrSwapchainImageBaseHeader*>(images.data())));
        eye.images.clear();
        for (auto& img : images) eye.images.push_back(img.image);
    }
    create_actions();
}

void XrContext::create_actions() {
    XrActionSetCreateInfo set_info{XR_TYPE_ACTION_SET_CREATE_INFO};
    std::strcpy(set_info.actionSetName, "gameplay");
    std::strcpy(set_info.localizedActionSetName, "Gameplay");
    XR_CHECK(xrCreateActionSet(instance, &set_info, &action_set_));
    XR_CHECK(xrStringToPath(instance, "/user/hand/left", &hand_paths_[HAND_LEFT]));
    XR_CHECK(xrStringToPath(instance, "/user/hand/right", &hand_paths_[HAND_RIGHT]));

    auto make_action = [&](const char* name, const char* localized, XrActionType type) {
        XrActionCreateInfo info{XR_TYPE_ACTION_CREATE_INFO};
        std::strcpy(info.actionName, name);
        std::strcpy(info.localizedActionName, localized);
        info.actionType = type;
        info.countSubactionPaths = 2;
        info.subactionPaths = hand_paths_.data();
        XrAction action;
        XR_CHECK(xrCreateAction(action_set_, &info, &action));
        return action;
    };
    pose_action_ = make_action("hand_pose", "Hand Pose", XR_ACTION_TYPE_POSE_INPUT);
    trigger_action_ = make_action("trigger", "Trigger", XR_ACTION_TYPE_BOOLEAN_INPUT);

    // Suggested bindings per interaction profile; booleans bound to .../value
    // are thresholded by the runtime
    struct Binding { XrAction action; const char* path; };
    struct Profile { const char* name; std::vector<Binding> bindings; };
    const Profile profiles[] = {
        {"/interaction_profiles/khr/simple_controller",
         {{pose_action_, "/user/hand/left/input/aim/pose"}, {pose_action_, "/user/hand/right/input/aim/pose"},
          {trigger_action_, "/user/hand/left/input/select/click"},
          {trigger_action_, "/user/hand/right/input/select/click"}}},
        {"/interaction_profiles/valve/index_controller",
         {{pose_action_, "/user/hand/left/input/aim/pose"}, {pose_action_, "/user/hand/right/input/aim/pose"},
          {trigger_action_, "/user/hand/left/input/trigger/click"},
          {trigger_action_, "/user/hand/right/input/trigger/click"}}},
        {"/interaction_profiles/oculus/touch_controller",
         {{pose_action_, "/user/hand/left/input/aim/pose"}, {pose_action_, "/user/hand/right/input/aim/pose"},
          {trigger_action_, "/user/hand/left/input/trigger/value"},
          {trigger_action_, "/user/hand/right/input/trigger/value"}}},
    };
    for (const auto& profile : profiles) {
        std::vector<XrActionSuggestedBinding> suggested;
        for (const auto& b : profile.bindings) {
            XrPath path;
            XR_CHECK(xrStringToPath(instance, b.path, &path));
            suggested.push_back({b.action, path});
        }
        XrInteractionProfileSuggestedBinding info{XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING};
        XR_CHECK(xrStringToPath(instance, profile.name, &info.interactionProfile));
        info.countSuggestedBindings = uint32_t(suggested.size());
        info.suggestedBindings = suggested.data();
        XrResult result = xrSuggestInteractionProfileBindings(instance, &info);
        if (XR_FAILED(result)) LOG("Skipping bindings for %s: XrResult %d", profile.name, int(result));
    }

    for (int hand = 0; hand < 2; ++hand) {
        XrActionSpaceCreateInfo info{XR_TYPE_ACTION_SPACE_CREATE_INFO};
        info.action = pose_action_;
        info.subactionPath = hand_paths_[hand];
        info.poseInActionSpace.orientation.w = 1.0f;
        XR_CHECK(xrCreateActionSpace(session, &info, &hand_spaces_[hand]));
    }
    XrSessionActionSetsAttachInfo attach{XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO};
    attach.countActionSets = 1;
    attach.actionSets = &action_set_;
    XR_CHECK(xrAttachSessionActionSets(session, &attach));
}

bool XrContext::poll_events(bool& session_running) {
    XrEventDataBuffer event{XR_TYPE_EVENT_DATA_BUFFER};
    bool keep_running = true;
    while (xrPollEvent(instance, &event) == XR_SUCCESS) {
        if (event.type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
            auto* changed = reinterpret_cast<XrEventDataSessionStateChanged*>(&event);
            switch (changed->state) {
            case XR_SESSION_STATE_READY: {
                XrSessionBeginInfo begin{XR_TYPE_SESSION_BEGIN_INFO};
                begin.primaryViewConfigurationType = XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;
                XR_CHECK(xrBeginSession(session, &begin));
                session_running = true;
                request_max_refresh_rate();
                break;
            }
            case XR_SESSION_STATE_STOPPING:
                xrEndSession(session);
                session_running = false;
                if (exit_requested_) keep_running = false;
                break;
            case XR_SESSION_STATE_EXITING:
            case XR_SESSION_STATE_LOSS_PENDING:
                keep_running = false;
                break;
            default:
                break;
            }
        } else if (event.type == XR_TYPE_EVENT_DATA_DISPLAY_REFRESH_RATE_CHANGED_FB) {
            auto* changed = reinterpret_cast<XrEventDataDisplayRefreshRateChangedFB*>(&event);
            display_refresh_rate = changed->toDisplayRefreshRate;
            LOG("[xr] display refresh rate changed: %.1f -> %.1f Hz", changed->fromDisplayRefreshRate,
                changed->toDisplayRefreshRate);
        } else if (event.type == XR_TYPE_EVENT_DATA_INSTANCE_LOSS_PENDING) {
            keep_running = false;
        }
        event = {XR_TYPE_EVENT_DATA_BUFFER};
    }
    return keep_running;
}

// The eye cameras run at the display rate (72 Hz display: 72 fps; 144 Hz:
// 90 fps), and an app's session sets the display rate, so ask for the highest
// one offered. Failures only get logged: the flow warns when the rate the
// runtime reports stays low.
void XrContext::request_max_refresh_rate() {
    if (!enumerate_refresh_rates_) return;
    uint32_t count = 0;
    if (XR_FAILED(enumerate_refresh_rates_(session, 0, &count, nullptr)) || count == 0) {
        LOG("[xr] no display refresh rates offered");
        return;
    }
    std::vector<float> rates(count);
    if (XR_FAILED(enumerate_refresh_rates_(session, count, &count, rates.data()))) return;
    rates.resize(count);
    std::string list;
    for (float r : rates) list += (list.empty() ? "" : ", ") + std::to_string(int(std::lround(r)));
    float current = 0.0f;
    get_refresh_rate_(session, &current);
    const float best = *std::max_element(rates.begin(), rates.end());
    XrResult result = request_refresh_rate_(session, best);
    float now = current;
    get_refresh_rate_(session, &now);
    display_refresh_rate = now;
    LOG("[xr] display refresh rates offered: %s Hz; was %.1f Hz, requested %.1f Hz (XrResult %d), now %.1f Hz",
        list.c_str(), current, best, int(result), now);
}

void XrContext::request_exit() {
    if (exit_requested_) return;
    exit_requested_ = true;
    if (session) xrRequestExitSession(session);
}

void XrContext::poll_input(XrTime display_time) {
    for (auto& c : controllers) c.trigger_pressed = false;
    XrActiveActionSet active{action_set_, XR_NULL_PATH};
    XrActionsSyncInfo sync{XR_TYPE_ACTIONS_SYNC_INFO};
    sync.countActiveActionSets = 1;
    sync.activeActionSets = &active;
    XrResult result = xrSyncActions(session, &sync);
    if (result == XR_SESSION_NOT_FOCUSED || XR_FAILED(result)) {
        // Unfocused (e.g. dashboard open): no input, hide the controllers
        for (auto& c : controllers) {
            c.pose.reset();
            c.trigger_held = false;
        }
        return;
    }
    for (int hand = 0; hand < 2; ++hand) {
        ControllerState& c = controllers[hand];
        XrSpaceLocation location{XR_TYPE_SPACE_LOCATION};
        const XrSpaceLocationFlags valid =
            XR_SPACE_LOCATION_POSITION_VALID_BIT | XR_SPACE_LOCATION_ORIENTATION_VALID_BIT;
        if (XR_SUCCEEDED(xrLocateSpace(hand_spaces_[hand], play_space, display_time, &location)) &&
            (location.locationFlags & valid) == valid) {
            c.pose = pose_matrix(location.pose);
        } else {
            c.pose.reset();
        }
        XrActionStateGetInfo get{XR_TYPE_ACTION_STATE_GET_INFO};
        get.action = trigger_action_;
        get.subactionPath = hand_paths_[hand];
        XrActionStateBoolean state{XR_TYPE_ACTION_STATE_BOOLEAN};
        if (XR_SUCCEEDED(xrGetActionStateBoolean(session, &get, &state)) && state.isActive) {
            bool pressed = state.currentState;
            c.trigger_held = pressed;
            if (state.changedSinceLastSync && pressed) c.trigger_pressed = true;
        }
    }
}

void XrContext::destroy() {
    for (auto& eye : eyes) {
        if (eye.swapchain) xrDestroySwapchain(eye.swapchain);
        eye.swapchain = XR_NULL_HANDLE;
    }
    for (auto& space : hand_spaces_) {
        if (space) xrDestroySpace(space);
        space = XR_NULL_HANDLE;
    }
    if (play_space) xrDestroySpace(play_space);
    if (action_set_) xrDestroyActionSet(action_set_);
    if (session) xrDestroySession(session);
    if (instance) xrDestroyInstance(instance);
    play_space = XR_NULL_HANDLE;
    action_set_ = XR_NULL_HANDLE;
    session = XR_NULL_HANDLE;
    instance = XR_NULL_HANDLE;
}

glm::mat4 pose_matrix(const XrPosef& pose) {
    glm::quat q(pose.orientation.w, pose.orientation.x, pose.orientation.y, pose.orientation.z);
    glm::mat4 m = glm::mat4_cast(q);
    m[3] = glm::vec4(pose.position.x, pose.position.y, pose.position.z, 1.0f);
    return m;
}

glm::mat4 projection_matrix(const XrFovf& fov, float near_z, float far_z) {
    // Vulkan clip space: x right, y down, z in [0, 1]
    const float left = std::tan(fov.angleLeft), right = std::tan(fov.angleRight);
    const float down = std::tan(fov.angleDown), up = std::tan(fov.angleUp);
    const float width = right - left;
    const float height = down - up;  // y flipped
    glm::mat4 m(0.0f);
    m[0][0] = 2.0f / width;
    m[1][1] = 2.0f / height;
    m[2][0] = (right + left) / width;
    m[2][1] = (up + down) / height;
    m[2][2] = -far_z / (far_z - near_z);
    m[2][3] = -1.0f;
    m[3][2] = -(far_z * near_z) / (far_z - near_z);
    return m;
}
