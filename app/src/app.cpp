#include "app.h"

#include "audio.h"
#include "avatar.h"
#include "capture.h"
#include "common.h"
#include "dialog.h"
#include "flow.h"
#include "renderer.h"
#include "text.h"
#include "version.h"
#include "xr_context.h"

#include <glm/gtc/matrix_transform.hpp>

#include <chrono>
#include <cmath>
#include <map>
#include <thread>

std::atomic<bool> App::terminate_requested{false};

namespace {

// Scene constants from test_model_renderer.py
const glm::vec3 UI_DIALOG_POSITION{0.0f, 1.07f, -1.07f};  // until the head model places it
const float UI_DIALOG_COVER_Y = 1.50f;
const float DIALOG_CHIN_CLEARANCE = 0.06f;
const float DIALOG_HEAD_DISTANCE = 0.25f;
const float DIALOG_COVER_DROP = 0.12f;
const float CONE_LENGTH = 0.15f;
const float LASER_LENGTH = 2.0f;
const float LASER_WIDTH = 0.004f;
const float PASSTHROUGH_FULL_BELOW_DEG = -35.0f;
const float PASSTHROUGH_FADE_TOP_DEG = -8.0f;
const glm::vec4 BACKGROUND_TOP_COLOR{0.027f, 0.078f, 0.094f, 0.0f};
const glm::vec4 BACKGROUND_HORIZON_COLOR{0.008f, 0.012f, 0.014f, 0.0f};
const glm::vec4 BACKGROUND_FLOOR_COLOR{0.055f, 0.094f, 0.086f, 0.0f};
const glm::vec4 AURORA_COLOR_PRIMARY{0.326f, 0.870f, 0.660f, 0.0f};    // Babble Sea Green
const glm::vec4 AURORA_COLOR_SECONDARY{0.990f, 0.640f, 0.700f, 0.0f};  // Babble Pink
const float AURORA_STRENGTH = 0.45f;
const float AURORA_SPEED = 1.6f;
const float STAR_STRENGTH = 0.65f;
const double SHUTDOWN_GRACE_SECONDS = 2.0;   // SIGTERM: wait this long for "stopped"

float sin_deg(float degrees) { return std::sin(degrees * 3.14159265f / 180.0f); }

}  // namespace

int App::run() {
    LOG("Babble Dataset Capture %s (capture protocol %d)", APP_VERSION, CAPTURE_PROTOCOL);
    LOG("Assets: %s", settings_.asset_dir.c_str());

    // Connect early: hello's sources decide which routines are offered. A
    // missing runtime is not fatal; the dialog explains it and offers Retry.
    Recorder recorder(settings_.capture_socket);
    std::string routine = settings_.routine;
    if (recorder.connect_blocking(2.0) && routine == "mouth" && !recorder.mouth_available()) {
        LOG("[capture] no mouth source reported; using the eye routine");
        routine = "eye";
    }
    RoutineData routines = RoutineData::load(settings_.asset_dir + "/routines.json");

    VkContext vk;
    XrContext xr;
    Renderer renderer;
    TextRasterizer text;
    Dialog dialog;
    Avatar avatar;
    int exit_code = 0;
    {
        xr.create(vk, settings_.render_scale, true);
        xr.create_session(vk);
        renderer.init(vk, xr, settings_.msaa_samples);
        text.load(settings_.asset_dir + "/fonts");
        dialog.init(vk, renderer, text, UI_DIALOG_POSITION, UI_DIALOG_COVER_Y);
        CueAudio cues;
        GuidedFlow flow(std::move(routines), dialog, cues, recorder, settings_.asset_dir + "/narration", routine);
        avatar.start_loading(settings_.asset_dir);

        // Controller cone (colour swaps while the trigger is held) and laser
        // beam (unlit: black base, colour via emission)
        GpuMesh cone = renderer.create_mesh(cone_mesh(CONE_LENGTH));
        GpuMesh laser = renderer.create_mesh(box_mesh());
        Image cone_idle_tex = upload(vk, solid_texture(0.85f, 0.85f, 0.9f));
        Image cone_pressed_tex = upload(vk, solid_texture(1.0f, 0.45f, 0.1f));
        Image laser_idle_tex = upload(vk, solid_texture(0.45f, 0.7f, 1.0f));
        Image laser_pressed_tex = upload(vk, solid_texture(1.0f, 0.45f, 0.1f));
        VkDescriptorSet cone_idle = renderer.material_set(cone_idle_tex.view);
        VkDescriptorSet cone_pressed = renderer.material_set(cone_pressed_tex.view);
        VkDescriptorSet laser_idle = renderer.material_set(renderer.default_black.view, VK_NULL_HANDLE, VK_NULL_HANDLE,
                                                           VK_NULL_HANDLE, laser_idle_tex.view);
        VkDescriptorSet laser_pressed = renderer.material_set(renderer.default_black.view, VK_NULL_HANDLE,
                                                              VK_NULL_HANDLE, VK_NULL_HANDLE, laser_pressed_tex.view);
        MaterialParams cone_params;
        cone_params.smoothness = 0.4f;
        MaterialParams laser_params;
        laser_params.smoothness = 0.0f;
        laser_params.emission = glm::vec3(1.0f);

        // The head sits 1.5 m in front of the stage origin
        const glm::mat4 head_model = glm::translate(glm::mat4(1.0f), glm::vec3(0.0f, 0.0f, -1.5f));

        flow.start();

        bool session_running = false;
        bool model_reported = false;
        double shutdown_deadline = 0.0;
        const double start_time = monotonic_seconds();
        double last_anim = start_time;
        std::map<std::string, float> coefficients;
        int fps_frames = 0;
        double fps_window = start_time;

        while (true) {
            // Exit paths: SIGTERM from the runtime, the dialog's Exit button,
            // or the XR runtime ending the session
            if ((terminate_requested || flow.exit_requested()) && shutdown_deadline == 0.0) {
                LOG(terminate_requested ? "[app] SIGTERM: shutting down" : "[app] exit requested from dialog");
                // The dialog's Exit already waited for "stopped" while rendering
                recorder.finish_blocking(SHUTDOWN_GRACE_SECONDS);
                xr.request_exit();
                shutdown_deadline = monotonic_seconds() + SHUTDOWN_GRACE_SECONDS;
            }
            if (shutdown_deadline > 0.0 && (!session_running || monotonic_seconds() > shutdown_deadline)) break;
            if (!xr.poll_events(session_running)) break;
            flow.set_xr_display_hz(xr.display_refresh_rate);
            recorder.pump();

            if (!model_reported) {
                if (avatar.pump(vk, renderer)) {
                    model_reported = true;
                    flow.on_model_ready(avatar.shape_names());
                    if (auto placement = avatar.dialog_placement(head_model, DIALOG_CHIN_CLEARANCE, DIALOG_HEAD_DISTANCE,
                                                                 DIALOG_COVER_DROP,
                                                                 Dialog::LABEL_GAP + Dialog::BIG_LABEL_SIZE.y)) {
                        dialog.position = placement->position;
                        dialog.anchor_top = placement->label_top;
                        if (placement->cover_center) dialog.cover_center = placement->cover_center;
                        LOG("Dialog placed from mesh: label top y=%.3f, cover centre y=%.3f", placement->label_top,
                            dialog.cover_center.value_or(0.0f));
                    }
                } else if (!avatar.error().empty()) {
                    model_reported = true;
                    LOG_ERR("[ERROR] Head model failed to load: %s", avatar.error().c_str());
                    flow.on_model_failed();
                }
            }

            if (!session_running) {
                std::this_thread::sleep_for(std::chrono::milliseconds(50));  // don't busy-spin while idle
                continue;
            }

            XrFrameState frame_state{XR_TYPE_FRAME_STATE};
            XR_CHECK(xrWaitFrame(xr.session, nullptr, &frame_state));
            XR_CHECK(xrBeginFrame(xr.session, nullptr));
            xr.poll_input(frame_state.predictedDisplayTime);

            std::vector<XrCompositionLayerProjectionView> projection_views;
            XrCompositionLayerProjection layer{XR_TYPE_COMPOSITION_LAYER_PROJECTION};
            const XrCompositionLayerBaseHeader* layers[1] = {
                reinterpret_cast<const XrCompositionLayerBaseHeader*>(&layer)};
            uint32_t layer_count = 0;

            if (frame_state.shouldRender) {
                const double now = monotonic_seconds();
                flow.frame(now);

                // Hover/trigger interaction: cone overlap or laser pointing
                std::array<std::vector<glm::vec3>, 2> probes;
                std::array<std::optional<Ray>, 2> rays;
                std::array<bool, 2> pressed{};
                for (int hand = 0; hand < 2; ++hand) {
                    const ControllerState& c = xr.controllers[size_t(hand)];
                    pressed[size_t(hand)] = c.trigger_pressed;
                    if (!c.pose) continue;
                    for (float d : {0.075f, 0.15f}) probes[size_t(hand)].push_back(glm::vec3(*c.pose * glm::vec4(0, 0, -d, 1)));
                    rays[size_t(hand)] = Ray{glm::vec3((*c.pose)[3]),
                                             glm::normalize(glm::mat3(*c.pose) * glm::vec3(0, 0, -1))};
                }
                dialog.update(probes, rays, pressed);

                // Ease the head toward the flow's target expression
                double dt = std::min(now - last_anim, 0.1);
                last_anim = now;
                float blend = float(std::min(1.0, dt * 8.0));
                std::map<std::string, float> next;
                const auto& targets = flow.targets();
                auto ease = [&](const std::string& name) {
                    if (next.count(name)) return;
                    auto t = targets.find(name);
                    auto c = coefficients.find(name);
                    float target = t == targets.end() ? 0.0f : t->second;
                    float value = c == coefficients.end() ? 0.0f : c->second;
                    value = std::abs(target - value) < 0.005f ? target : value + (target - value) * blend;
                    if (std::abs(value) > 1e-4f) next[name] = value;
                };
                for (const auto& [name, w] : coefficients) ease(name);
                for (const auto& [name, w] : targets) ease(name);
                coefficients = std::move(next);
                avatar.set_weights(coefficients);

                XrViewLocateInfo locate{XR_TYPE_VIEW_LOCATE_INFO};
                locate.viewConfigurationType = XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;
                locate.displayTime = frame_state.predictedDisplayTime;
                locate.space = xr.play_space;
                XrViewState view_state{XR_TYPE_VIEW_STATE};
                std::array<XrView, 2> views{{{XR_TYPE_VIEW}, {XR_TYPE_VIEW}}};
                uint32_t view_count = 0;
                XR_CHECK(xrLocateViews(xr.session, &locate, &view_state, 2, &view_count, views.data()));

                // Acquire both eyes' images, record both into one submission
                std::array<uint32_t, 2> image_index{};
                for (int eye = 0; eye < 2; ++eye) {
                    XR_CHECK(xrAcquireSwapchainImage(xr.eyes[size_t(eye)].swapchain, nullptr, &image_index[size_t(eye)]));
                    XrSwapchainImageWaitInfo wait{XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO};
                    wait.timeout = XR_INFINITE_DURATION;
                    XR_CHECK(xrWaitSwapchainImage(xr.eyes[size_t(eye)].swapchain, &wait));
                }
                renderer.begin_frame();
                const int slot = renderer.frame_slot();
                avatar.update(vk, slot);
                const float sky_time = float(now - start_time);  // same for both eyes
                for (int eye = 0; eye < 2; ++eye) {
                    const XrView& view = views[size_t(eye)];
                    glm::mat4 proj = projection_matrix(view.fov, 0.1f, 100.0f);
                    glm::mat4 view_matrix = glm::inverse(pose_matrix(view.pose));
                    FrameUniforms u;
                    u.view_proj = proj * view_matrix;
                    u.inv_proj = glm::inverse(proj);
                    u.inv_view = glm::inverse(view_matrix);
                    u.camera_pos = glm::vec4(view.pose.position.x, view.pose.position.y, view.pose.position.z, 1.0f);
                    u.sky_a = glm::vec4(sky_time, sin_deg(PASSTHROUGH_FULL_BELOW_DEG), sin_deg(PASSTHROUGH_FADE_TOP_DEG),
                                        xr.passthrough ? 1.0f : 0.0f);
                    u.sky_b = glm::vec4(settings_.sky_detail, AURORA_STRENGTH, AURORA_SPEED, STAR_STRENGTH);
                    u.top_color = BACKGROUND_TOP_COLOR;
                    u.horizon_color = BACKGROUND_HORIZON_COLOR;
                    u.floor_color = BACKGROUND_FLOOR_COLOR;
                    u.aurora_primary = AURORA_COLOR_PRIMARY;
                    u.aurora_secondary = AURORA_COLOR_SECONDARY;

                    renderer.begin_eye(eye, image_index[size_t(eye)], u);  // sky drawn
                    avatar.draw(renderer, slot, head_model, Renderer::Pass::Opaque);
                    for (int hand = 0; hand < 2; ++hand) {
                        const ControllerState& c = xr.controllers[size_t(hand)];
                        if (!c.pose) continue;
                        renderer.draw(cone, c.trigger_held ? cone_pressed : cone_idle, *c.pose, cone_params);
                        // Laser from the cone tip to the hovered button (just
                        // short of its face) or LASER_LENGTH out
                        auto hit = dialog.ray_hit_distances[size_t(hand)];
                        float end = hit ? *hit - 0.005f : LASER_LENGTH;
                        float seg = std::max(end - CONE_LENGTH, 0.01f);
                        glm::mat4 beam = glm::translate(*c.pose, glm::vec3(0.0f, 0.0f, -(CONE_LENGTH + seg / 2)));
                        beam = glm::scale(beam, glm::vec3(LASER_WIDTH, LASER_WIDTH, seg));
                        renderer.draw(laser, c.trigger_held ? laser_pressed : laser_idle, beam, laser_params);
                    }
                    dialog.draw(renderer);
                    // Transparent pass (eyelashes): blended, depth test, no depth writes
                    avatar.draw(renderer, slot, head_model, Renderer::Pass::Blended);
                    renderer.end_eye();

                    XrCompositionLayerProjectionView pv{XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW};
                    pv.pose = view.pose;
                    pv.fov = view.fov;
                    pv.subImage.swapchain = xr.eyes[size_t(eye)].swapchain;
                    pv.subImage.imageRect = {{0, 0},
                                             {int32_t(xr.eyes[size_t(eye)].width), int32_t(xr.eyes[size_t(eye)].height)}};
                    projection_views.push_back(pv);
                }
                renderer.end_frame();
                for (int eye = 0; eye < 2; ++eye) {
                    XR_CHECK(xrReleaseSwapchainImage(xr.eyes[size_t(eye)].swapchain, nullptr));
                }
                // Shaders write straight (unpremultiplied) alpha
                layer.layerFlags = xr.passthrough ? XR_COMPOSITION_LAYER_UNPREMULTIPLIED_ALPHA_BIT : 0;
                layer.space = xr.play_space;
                layer.viewCount = uint32_t(projection_views.size());
                layer.views = projection_views.data();
                layer_count = 1;
            }

            XrFrameEndInfo end{XR_TYPE_FRAME_END_INFO};
            end.displayTime = frame_state.predictedDisplayTime;
            end.environmentBlendMode = xr.blend_mode;
            end.layerCount = layer_count;
            end.layers = layer_count ? layers : nullptr;
            XR_CHECK(xrEndFrame(xr.session, &end));

            if (settings_.fps_log) {
                ++fps_frames;
                double t = monotonic_seconds();
                if (t - fps_window >= 5.0) {
                    LOG("[fps] %.1f avg over %.1fs", fps_frames / (t - fps_window), t - fps_window);
                    fps_frames = 0;
                    fps_window = t;
                }
            }
        }

        // Cleanup: stop the recording first. SIGTERM waits briefly for
        // "stopped"; other exits (the XR runtime ending the session) wait
        // as long as a normal stop
        recorder.finish_blocking(terminate_requested ? SHUTDOWN_GRACE_SECONDS : Recorder::STOP_REPLY_SECONDS);
        recorder.close();
        cues.stop();
        vkDeviceWaitIdle(vk.device);
        dialog.destroy();
        avatar.destroy(vk, renderer);
        renderer.destroy_mesh(cone);
        renderer.destroy_mesh(laser);
        for (Image* img : {&cone_idle_tex, &cone_pressed_tex, &laser_idle_tex, &laser_pressed_tex}) vk.destroy_image(*img);
    }
    renderer.destroy();
    xr.destroy();
    vk.destroy();
    return exit_code;
}
