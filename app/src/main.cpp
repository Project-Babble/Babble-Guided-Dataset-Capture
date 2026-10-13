// Babble Dataset Capture: guided face-expression capture for Babble Frame
// Runtime. The runtime launches this app (bin/babble-dataset-capture from the
// release tarball) and records its own cameras; this app shows the avatar and
// routine, plays cues and sends segment labels over the capture socket.

#include "app.h"
#include "audio.h"
#include "common.h"

#include <csignal>
#include <cstdlib>

static void on_signal(int) { App::terminate_requested = true; }

int main(int argc, char** argv) {
    Settings settings = Settings::load(argc, argv);
    if (settings.audio_selftest) return CueAudio::selftest(settings.audio_selftest_path) ? 0 : 1;

    // Sharp UI text: SteamVR injects a fragment density map (fixed foveation)
    // into app swapchains, and the dialog sits low in the view where shading
    // density drops. The Python prototype disabled it too; the runtime
    // launches this binary directly, so do it here, before the Vulkan instance
    // exists (an explicit value in the environment wins).
    setenv("DISABLE_VULKAN_FDM_INJECTION_LAYER", "1", 0);

    // The runtime stops the app with SIGTERM: leave the frame loop so the
    // recording is stopped cleanly ("stop", then wait briefly for "stopped")
    struct sigaction action{};
    action.sa_handler = on_signal;
    sigemptyset(&action.sa_mask);
    sigaction(SIGTERM, &action, nullptr);
    sigaction(SIGINT, &action, nullptr);
    signal(SIGPIPE, SIG_IGN);

    try {
        return App(std::move(settings)).run();
    } catch (const std::exception& e) {
        LOG_ERR("[fatal] %s", e.what());
        return 1;
    }
}
