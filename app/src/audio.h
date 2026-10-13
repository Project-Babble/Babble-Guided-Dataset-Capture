#pragma once
// Realtime state-following audio cues: port of cue_audio.py. The engine keeps
// no schedule of its own; the guided flow sends state changes and the audio
// follows within a block (~5 ms), so a hitch stretches audio and visuals
// together. Output goes straight to PipeWire (no paplay); every call is a
// silent no-op when audio is unavailable — the routine never depends on it.
//
// Palette: a noise "sweep bed" whose filter glides between closed (mmm) and
// open (ahh), a rising double ding (segment start), a falling one (segment
// end) and a thunk (button press).

#include <memory>
#include <string>

class CueAudio {
public:
    CueAudio();
    ~CueAudio();
    CueAudio(const CueAudio&) = delete;
    CueAudio& operator=(const CueAudio&) = delete;

    void bed(bool on);                          // noise on/off (40 ms ramp)
    void target(float openness, float glide = 0.10f);
    // Glide 90 % of the way to openness, arriving at deadline (monotonic
    // seconds), then hover until land()
    void lean(float openness, double deadline);
    void land();
    void mark(const std::string& kind);         // "start" | "end" | "thunk"
    void narration(const std::string& wav_path);  // replaces a playing clip
    void stop();

    // Renders cue_audio.py's scripted self-test sequence to a WAV, no device
    static bool selftest(const std::string& wav_path);

    struct Impl;
private:
    std::unique_ptr<Impl> impl_;
};
