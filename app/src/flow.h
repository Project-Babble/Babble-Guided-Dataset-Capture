#pragma once
// The guided capture flow: onboarding panes, the expression routine (prep /
// hold / relax / alternate phases), freeform play, and the recording
// lifecycle. Ported from the Python prototype's OpenXRApp.run(); the tables
// come from assets/routines.json.

#include "audio.h"
#include "capture.h"
#include "dialog.h"

#include <functional>
#include <map>
#include <optional>
#include <random>
#include <string>
#include <vector>

struct RoutineStep {
    std::string display, short_name, log, mode, cue_on, cue_off, hold_label;
    std::map<std::string, float> pose;   // resolved to model shapes by GuidedFlow
    bool audio_invert = false;
    std::optional<double> hold, seconds, period;
    int index = -1;   // position in the routine's steps (routine_def.steps)
};

struct Routine {
    std::vector<RoutineStep> steps;
    double hold_seconds = 3, alternate_seconds = 10, freeform_seconds = 60;
    std::vector<std::string> freeform_prompts;
    std::vector<std::string> freeform_prefixes;   // empty: riff on every shape
    std::string freeform_intro;
};

struct OnboardingStep {
    std::string name, label;
    bool demo = false, practice = false, routine_choice = false, refresh_warning = false;
    std::vector<std::pair<std::string, std::string>> buttons;  // (text, action)
};

struct RoutineData {
    double prep_seconds = 2, relax_seconds = 3, alternate_period = 2, prompt_seconds = 5, shuffle_seconds = 1.2;
    std::string consent_version;
    std::map<std::string, std::pair<std::string, std::string>> bipolar;
    std::map<std::string, Routine> routines;
    std::vector<OnboardingStep> onboarding;
    json raw;   // the file as loaded

    static RoutineData load(const std::string& path);
    // Protocol 2 "routine_def" for "start": routines.<name> plus the shared
    // timing, bipolar shapes and consent version, and the app version
    json routine_def(const std::string& name) const;
};

class GuidedFlow {
public:
    GuidedFlow(RoutineData data, Dialog& dialog, CueAudio& cues, Recorder& recorder, std::string narration_dir,
               std::string routine);

    void start();
    void on_model_ready(const std::vector<std::string>& shape_names);
    void on_model_failed();
    void frame(double now);   // per-frame state machine (call before dialog.update)

    const std::map<std::string, float>& targets() const { return targets_; }
    bool exit_requested() const { return exit_requested_; }
    const std::string& routine_name() const { return routine_name_; }
    // The display rate the XR session reports (0 = unknown); the fallback
    // when the runtime doesn't report one
    void set_xr_display_hz(float hz) { xr_display_hz_ = hz; }

private:
    enum class Phase { None, Prep, Hold, Relax, Alternate, Freeform };

    const Routine& routine() const { return data_.routines.at(routine_name_); }
    std::vector<RoutineStep> compute_sequence() const;
    const RoutineStep& current_step() const { return sequence_[size_t(index_)]; }
    void play_narration(const std::string& name);

    // Expression flow
    void show_expression();
    void show_done();
    void show_finished(const std::string& message);
    void offer_upload(const StopResult& result, const std::string& headline);
    void on_runtime_stop(const StopResult& result);
    void show_freeform_intro();
    void on_freeform_start();
    void start_phase(Phase phase, double seconds);
    // phase: the protocol's segment phase; step: index into routine_def.steps
    // (-1 outside the steps)
    void log_record(std::string name, int64_t start_ns, int64_t end_ns, const std::string& phase, int step);
    void log_phase(const std::string& name, const std::string& phase);
    void begin_hold_phase();
    std::string alternate_label(int seconds) const;
    void log_alternate_segment(int64_t end_ns);
    void set_alternate_state(bool on);
    void finish_alternate();
    void on_ready();
    void begin_prep();
    void begin_relax_phase();
    void advance_expression();
    void next_step();
    void on_back();
    void start_expression_flow();
    void set_ready_buttons();

    // Onboarding
    void on_onboarding_next();
    void on_onboarding_back();
    void on_consent();
    // Runs then() once consent is given, showing the consent pane first if needed
    void require_consent(std::function<void()> then);
    void accept_consent();
    void choose_routine(const std::string& name);
    bool onboarding_step_visible(const OnboardingStep& step) const;
    bool refresh_warning_needed() const;
    std::string onboarding_label(const OnboardingStep& step) const;
    void check_rates_then(std::function<void()> then);
    void skip_onboarding();
    void finish_onboarding();
    void enter_onboarding_step(int index, int direction = 1);

    // Recording
    void with_recording(std::function<void()> then);
    void show_recording_problem(const std::string& message);
    void on_recording_retry();
    void request_exit();

    RoutineData data_;
    Dialog& dialog_;
    CueAudio& cues_;
    Recorder& recorder_;
    std::string narration_dir_;
    std::string routine_name_;
    std::mt19937 rng_{std::random_device{}()};

    std::map<std::string, float> targets_;
    std::vector<std::string> shape_names_;
    std::vector<RoutineStep> sequence_;
    bool model_ready_ = false;
    bool exit_requested_ = false;

    int index_ = 0;
    Phase phase_ = Phase::None;
    double phase_until_ = 0, phase_seconds_ = 0;
    int phase_seconds_shown_ = -1;
    std::optional<int64_t> phase_start_ns_;
    bool practice_ = false;
    double freeform_next_shuffle_ = 0;
    bool alt_on_ = false;
    std::optional<int64_t> alt_segment_start_ns_;
    int alt_lean_id_ = -1;
    int alt_segments_logged_ = 0;

    int onboarding_index_ = 0;
    bool onboarding_active_ = false;
    std::optional<int> pending_step_;
    bool start_flow_when_ready_ = false;
    bool rates_fresh_ = false;    // a "status" arrived for the pane being entered
    float xr_display_hz_ = 0.0f;

    bool recovery_active_ = false;
    std::function<void()> resume_;
};
