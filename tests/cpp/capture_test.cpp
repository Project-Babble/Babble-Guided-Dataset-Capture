// Drives the C++ Recorder through a full session against the capture socket
// in BABBLE_CAPTURE_SOCKET (tests/test_cpp_capture.py runs it against
// tools/mock_runtime.py). Prints one result line per step; exit code 0 when
// every step succeeded. With "runtime-stop", waits for the runtime to stop the
// session on its own instead of stopping it.

#include "capture.h"
#include "common.h"

#include <cstdlib>
#include <cstring>
#include <thread>

static bool wait_for(Recorder& r, const bool& flag, double seconds) {
    double deadline = monotonic_seconds() + seconds;
    while (!flag && monotonic_seconds() < deadline) {
        r.pump();
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    return flag;
}

int main(int argc, char** argv) {
    const bool runtime_stop = argc > 1 && std::strcmp(argv[1], "runtime-stop") == 0;
    const char* path = std::getenv("BABBLE_CAPTURE_SOCKET");
    if (!path) return 2;
    Recorder r(path);
    if (!r.connect_blocking(2.0)) {
        LOG("connect: failed: %s", r.problem_text().c_str());
        return 3;
    }
    LOG("connect: ok mouth=%d display_hz=%d upload=%d", r.mouth_available(), r.display_hz(), r.upload_available());
    bool refreshed = false;
    r.refresh_status([&] { refreshed = true; });
    if (!wait_for(r, refreshed, 3.0)) return 7;
    LOG("status: display_hz=%d camera_fps=%d", r.display_hz(), r.camera_fps());
    r.consent = json{{"version", "test"}, {"accepted_ns", monotonic_ns()}};

    bool started = false, ok = false;
    json steps = json::array();
    for (int i = 0; i < 3; ++i) steps.push_back({{"log", "Shape" + std::to_string(i)}});
    json routine_def = {{"steps", steps}, {"app_version", "test"}};
    r.ensure_started("eye", routine_def, [&](bool result) { started = true; ok = result; });
    if (!wait_for(r, started, 5.0) || !ok) {
        LOG("start: failed: %s", r.problem_text().c_str());
        return 4;
    }
    LOG("start: ok");
    int64_t t0 = monotonic_ns();
    for (int i = 0; i < 3; ++i) r.segment("Shape" + std::to_string(i), t0 + i * 1000, t0 + i * 1000 + 500, i, "hold");

    StopResult result;
    if (runtime_stop) {
        double deadline = monotonic_seconds() + 10.0;
        std::optional<StopResult> stop;
        while (!(stop = r.take_runtime_stop()) && monotonic_seconds() < deadline) {
            r.pump();
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        if (!stop) return 5;
        result = *stop;
        LOG("runtime stop: recording=%d", r.recording());
    } else {
        bool finished = false;
        r.finish([&](const StopResult& s) { finished = true; result = s; });
        if (!wait_for(r, finished, Recorder::STOP_REPLY_SECONDS + 1.0)) return 5;
    }
    if (result.session.empty()) return 5;
    LOG("stop: ok %s complete=%d reason=%s", result.session.c_str(), result.complete, result.reason.c_str());
    if (!result.complete) {
        r.close();
        return 0;
    }
    const std::string session = result.session;

    bool uploaded = false;
    std::optional<std::string> failure;
    r.upload(session, [&](std::optional<std::string> f) { uploaded = true; failure = f; });
    if (!wait_for(r, uploaded, 5.0) || failure) {
        LOG("upload: failed: %s", failure.value_or("timeout").c_str());
        return 6;
    }
    LOG("upload: ok");
    r.close();
    return 0;
}
