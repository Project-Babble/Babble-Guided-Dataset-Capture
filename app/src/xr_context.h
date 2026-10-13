#pragma once
// OpenXR instance/session on Vulkan (XR_KHR_vulkan_enable2): creates the
// Vulkan instance and device through the runtime, the per-eye swapchains, the
// STAGE play space and the controller actions (port of OpenXRApp's
// initialize_* methods in test_model_renderer.py).

#include "vk_context.h"

#include <vulkan/vulkan.h>
#ifndef XR_USE_GRAPHICS_API_VULKAN
#define XR_USE_GRAPHICS_API_VULKAN
#endif
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>

#include <glm/glm.hpp>

#include <array>
#include <optional>
#include <string>
#include <vector>

#define XR_CHECK(expr)                                                                   \
    do {                                                                                 \
        XrResult xr_check_result_ = (expr);                                              \
        if (XR_FAILED(xr_check_result_))                                                 \
            fail(std::string(#expr) + " failed: XrResult " + std::to_string(xr_check_result_)); \
    } while (0)

enum Hand { HAND_LEFT = 0, HAND_RIGHT = 1 };

struct EyeSwapchain {
    XrSwapchain swapchain = XR_NULL_HANDLE;
    uint32_t width = 0, height = 0;
    std::vector<VkImage> images;
};

struct ControllerState {
    std::optional<glm::mat4> pose;   // aim pose in the play space (untracked: empty)
    bool trigger_held = false;
    bool trigger_pressed = false;    // true only on the press frame
};

class XrContext {
public:
    XrInstance instance = XR_NULL_HANDLE;
    XrSystemId system = XR_NULL_SYSTEM_ID;
    XrSession session = XR_NULL_HANDLE;
    XrSpace play_space = XR_NULL_HANDLE;
    XrEnvironmentBlendMode blend_mode = XR_ENVIRONMENT_BLEND_MODE_OPAQUE;
    bool passthrough = false;          // ALPHA_BLEND available and used
    VkFormat color_format = VK_FORMAT_UNDEFINED;
    std::array<EyeSwapchain, 2> eyes;
    std::array<ControllerState, 2> controllers;
    // Display refresh rate from XR_FB_display_refresh_rate (0 = unknown or
    // extension missing). The eye cameras follow it (HANDOFF.md section 0).
    float display_refresh_rate = 0.0f;

    // Instance + system, then the Vulkan instance/device via the runtime
    void create(VkContext& vk, float render_scale, bool want_passthrough);
    void create_session(VkContext& vk);
    void destroy();

    // Session state handling; returns false once the app should exit
    bool poll_events(bool& session_running);
    void poll_input(XrTime display_time);
    void request_exit();

private:
    void create_actions();
    void request_max_refresh_rate();

    PFN_xrEnumerateDisplayRefreshRatesFB enumerate_refresh_rates_ = nullptr;
    PFN_xrGetDisplayRefreshRateFB get_refresh_rate_ = nullptr;
    PFN_xrRequestDisplayRefreshRateFB request_refresh_rate_ = nullptr;

    XrActionSet action_set_ = XR_NULL_HANDLE;
    XrAction pose_action_ = XR_NULL_HANDLE;
    XrAction trigger_action_ = XR_NULL_HANDLE;
    std::array<XrPath, 2> hand_paths_{};
    std::array<XrSpace, 2> hand_spaces_{};
    std::vector<XrViewConfigurationView> view_configs_;
    bool exit_requested_ = false;
};

// XrPosef -> column-major model matrix [R | p]
glm::mat4 pose_matrix(const XrPosef& pose);
// Vulkan clip-space projection (y down, depth 0..1) from an XR fov
glm::mat4 projection_matrix(const XrFovf& fov, float near_z, float far_z);
