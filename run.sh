source ./venv/bin/activate
# Launching from the nested desktop needs environment corrections (SSH
# shells already have all of these right):
#  1. The nested plasma session has a private XDG_RUNTIME_DIR
#     (/run/user/<uid>/nested_plasma); SteamVR IPC lives under the real one,
#     and with the nested dir the XR session dies with HandleInvalidError.
#  2. The XR binding is GLX, so the window must be X11 — but GLFW prefers
#     Wayland whenever WAYLAND_DISPLAY is set, giving an EGL window and no
#     GLX context. Unset it.
#  3. PYOPENGL_PLATFORM=glx: PyOpenGL guesses EGL when XDG_SESSION_TYPE is
#     "wayland" and then sees "no valid context" despite a live GLX context.
#  4. LD_LIBRARY_PATH points into Steam runtime libs and the Mesa/vk_*
#     overrides are Steam-session tuning; scrub them.
#  5. ENABLE_GAMESCOPE_WSI=1 makes the Gamescope WSI layer try to hook our
#     plain Xwayland swapchain; it fails and its error dialog breaks things.
export XDG_RUNTIME_DIR=/run/user/$(id -u)
unset WAYLAND_DISPLAY LD_LIBRARY_PATH MESA_GL_VERSION_OVERRIDE MESA_GLSL_VERSION_OVERRIDE \
      vk_khr_present_wait vk_wsi_force_swapchain_to_current_extent vk_xwayland_wait_ready
export GLFW_PLATFORM=x11 DISPLAY=:0 MESA_LOADER_DRIVER_OVERRIDE=zink \
       DISABLE_GAMESCOPE_WSI=1 PYOPENGL_PLATFORM=glx
# Sharp UI text: SteamVR injects a fragment density map (fixed foveation)
# into app swapchains, and the dialog sits low in the view where shading
# density drops — text blurs in a way no texture filtering can fix.
# Disabling it is free here (62% vs 66% GPU busy, same 144 fps).
export DISABLE_VULKAN_FDM_INJECTION_LAYER=1
# Launch-log debugging stays readable even when output goes to a file
export PYTHONUNBUFFERED=1
# GPU knobs (all optional): FRAME_EYES_MSAA=2|4, FRAME_EYES_RENDER_SCALE,
# FRAME_EYES_SKY=full|lite|off, FRAME_EYES_FPS_LOG=1
python test_model_renderer.py
