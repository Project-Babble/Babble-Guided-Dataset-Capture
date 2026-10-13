#pragma once
// Runtime settings, mostly from the same FRAME_EYES_* environment variables
// the Python app reads, plus the asset directory.

#include <string>

struct Settings {
    int msaa_samples = 4;          // FRAME_EYES_MSAA (1 disables)
    float render_scale = 1.0f;     // FRAME_EYES_RENDER_SCALE
    float sky_detail = 2.0f;       // FRAME_EYES_SKY: off=0, lite=1, full=2
    bool fps_log = false;          // FRAME_EYES_FPS_LOG=1
    std::string routine = "eye";   // FRAME_EYES_ROUTINE: initial routine
    std::string asset_dir;         // BABBLE_CAPTURE_ASSETS or <exe>/../assets
    std::string capture_socket;    // BABBLE_CAPTURE_SOCKET or the default path
    bool audio_selftest = false;   // --audio-selftest <wav>
    std::string audio_selftest_path;

    static Settings load(int argc, char** argv);
};
