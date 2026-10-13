#pragma once
// The application: owns XR, Vulkan, renderer, avatar, dialog, audio, the
// capture recorder and the guided flow, and runs the XR frame loop.

#include "settings.h"

#include <atomic>

class App {
public:
    explicit App(Settings settings) : settings_(std::move(settings)) {}
    int run();

    // Set from the SIGTERM/SIGINT handler: the runtime stops the app this way
    static std::atomic<bool> terminate_requested;

private:
    Settings settings_;
};
