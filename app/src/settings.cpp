#include "settings.h"

#include "common.h"

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <unistd.h>

namespace fs = std::filesystem;

static const char* env(const char* name) {
    const char* value = std::getenv(name);
    return (value && *value) ? value : nullptr;
}

static std::string exe_dir() {
    char buf[4096];
    ssize_t n = readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (n <= 0) return ".";
    buf[n] = 0;
    return fs::path(buf).parent_path().string();
}

Settings Settings::load(int argc, char** argv) {
    Settings s;
    if (const char* v = env("FRAME_EYES_MSAA")) s.msaa_samples = std::max(1, std::atoi(v));
    if (const char* v = env("FRAME_EYES_RENDER_SCALE")) s.render_scale = float(std::atof(v));
    if (s.render_scale <= 0.0f) s.render_scale = 1.0f;
    if (const char* v = env("FRAME_EYES_SKY")) {
        s.sky_detail = !std::strcmp(v, "off") ? 0.0f : !std::strcmp(v, "lite") ? 1.0f : 2.0f;
    }
    if (const char* v = env("FRAME_EYES_FPS_LOG")) s.fps_log = !std::strcmp(v, "1");
    if (const char* v = env("FRAME_EYES_ROUTINE")) s.routine = v;

    if (const char* v = env("BABBLE_CAPTURE_ASSETS")) {
        s.asset_dir = v;
    } else {
        // Release layout: bin/babble-dataset-capture next to assets/
        fs::path installed = fs::path(exe_dir()) / ".." / "assets";
        s.asset_dir = fs::weakly_canonical(installed).string();
    }

    if (const char* v = env("BABBLE_CAPTURE_SOCKET")) {
        s.capture_socket = v;
    } else {
        std::string runtime_dir = env("XDG_RUNTIME_DIR") ? env("XDG_RUNTIME_DIR")
                                                         : "/run/user/" + std::to_string(getuid());
        s.capture_socket = runtime_dir + "/babble-frame-runtime/capture.sock";
    }

    for (int i = 1; i < argc; ++i) {
        if (!std::strcmp(argv[i], "--audio-selftest") && i + 1 < argc) {
            s.audio_selftest = true;
            s.audio_selftest_path = argv[++i];
        } else if (!std::strcmp(argv[i], "--assets") && i + 1 < argc) {
            s.asset_dir = argv[++i];
        }
    }
    return s;
}
