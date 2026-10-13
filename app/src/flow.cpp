#include "flow.h"

#include "common.h"
#include "version.h"

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <set>

namespace {

constexpr double PI = 3.14159265358979323846;
const char* const RELAX_CUE = "Relax — neutral face";

std::string replace_all(std::string s, const std::string& from, const std::string& to) {
    for (size_t pos = 0; (pos = s.find(from, pos)) != std::string::npos; pos += to.size()) s.replace(pos, from.size(), to);
    return s;
}

std::string lower_ascii(std::string s) {
    for (char& c : s) {
        if (c >= 'A' && c <= 'Z') c = char(c - 'A' + 'a');
    }
    return s;
}

std::string number(double v) {
    if (std::abs(v - std::round(v)) < 1e-9) return std::to_string(int64_t(std::llround(v)));
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%g", v);
    return buf;
}

std::optional<double> optional_number(const json& j, const char* key) {
    if (j.contains(key) && j[key].is_number()) return j[key].get<double>();
    return std::nullopt;
}

}  // namespace

RoutineData RoutineData::load(const std::string& path) {
    std::ifstream f(path);
    if (!f) fail("cannot open " + path);
    json j = json::parse(f);
    RoutineData d;
    const json& timing = j.at("timing");
    d.prep_seconds = timing.at("prep_seconds").get<double>();
    d.relax_seconds = timing.at("relax_seconds").get<double>();
    d.alternate_period = timing.at("alternate_period_seconds").get<double>();
    d.prompt_seconds = timing.at("freeform_prompt_seconds").get<double>();
    d.shuffle_seconds = timing.at("freeform_shuffle_seconds").get<double>();
    d.consent_version = j.at("consent_version").get<std::string>();
    for (auto& [name, pair] : j.at("bipolar_shapes").items()) {
        d.bipolar[name] = {pair.at(0).get<std::string>(), pair.at(1).get<std::string>()};
    }
    for (auto& [name, r] : j.at("routines").items()) {
        Routine routine;
        routine.hold_seconds = r.at("hold_seconds").get<double>();
        routine.alternate_seconds = r.at("alternate_seconds").get<double>();
        routine.freeform_seconds = r.at("freeform_seconds").get<double>();
        routine.freeform_prompts = r.at("freeform_prompts").get<std::vector<std::string>>();
        if (r.contains("freeform_shape_prefixes") && r["freeform_shape_prefixes"].is_array())
            routine.freeform_prefixes = r["freeform_shape_prefixes"].get<std::vector<std::string>>();
        routine.freeform_intro = r.at("freeform_intro").get<std::string>();
        for (const json& s : r.at("steps")) {
            RoutineStep step;
            step.display = s.at("display").get<std::string>();
            step.short_name = s.value("short", step.display);
            step.log = s.at("log").get<std::string>();
            step.mode = s.value("mode", "");
            step.cue_on = s.value("cue_on", step.display);
            step.cue_off = s.value("cue_off", RELAX_CUE);
            step.hold_label = s.value("hold_label", "Hold that expression!");
            step.audio_invert = s.value("audio_invert", false);
            step.hold = optional_number(s, "hold");
            step.seconds = optional_number(s, "seconds");
            step.period = optional_number(s, "period");
            step.index = int(routine.steps.size());
            for (auto& [shape, weight] : s.at("pose").items()) step.pose[shape] = weight.get<float>();
            routine.steps.push_back(std::move(step));
        }
        d.routines[name] = std::move(routine);
    }
    for (const json& s : j.at("onboarding")) {
        OnboardingStep step;
        step.name = s.at("name").get<std::string>();
        step.label = s.at("label").get<std::string>();
        step.demo = s.value("demo", false);
        step.practice = s.value("practice", false);
        step.routine_choice = s.value("routine_choice", false);
        step.refresh_warning = s.value("refresh_warning", false);
        if (s.contains("buttons")) {
            for (const json& b : s["buttons"]) step.buttons.emplace_back(b.at(0).get<std::string>(), b.at(1).get<std::string>());
        }
        d.onboarding.push_back(std::move(step));
    }
    if (!d.routines.count("eye")) fail(path + ": no eye routine");
    d.raw = std::move(j);
    return d;
}

json RoutineData::routine_def(const std::string& name) const {
    json def = raw.at("routines").at(name);
    def["timing"] = raw.at("timing");
    def["bipolar_shapes"] = raw.at("bipolar_shapes");
    def["consent_version"] = raw.at("consent_version");
    def["app_version"] = APP_VERSION;
    return def;
}

GuidedFlow::GuidedFlow(RoutineData data, Dialog& dialog, CueAudio& cues, Recorder& recorder,
                       std::string narration_dir, std::string routine)
    : data_(std::move(data)), dialog_(dialog), cues_(cues), recorder_(recorder),
      narration_dir_(std::move(narration_dir)), routine_name_(std::move(routine)) {
    if (!data_.routines.count(routine_name_)) {
        LOG_ERR("Unknown routine '%s'; using eye", routine_name_.c_str());
        routine_name_ = "eye";
    }
    onboarding_active_ = !data_.onboarding.empty();
}

void GuidedFlow::play_narration(const std::string& name) {
    std::string path = narration_dir_ + "/" + name + ".wav";
    std::error_code ec;
    if (std::filesystem::is_regular_file(path, ec)) cues_.narration(path);
}

std::vector<RoutineStep> GuidedFlow::compute_sequence() const {
    std::set<std::string> available(shape_names_.begin(), shape_names_.end());
    std::vector<RoutineStep> sequence;
    for (RoutineStep step : routine().steps) {
        // Bipolar smile/frown axes resolve to one of two one-directional shapes
        std::map<std::string, float> resolved;
        for (const auto& [name, weight] : step.pose) {
            auto it = data_.bipolar.find(name);
            if (it != data_.bipolar.end()) {
                resolved[weight >= 0 ? it->second.first : it->second.second] += std::abs(weight);
            } else {
                resolved[name] += weight;
            }
        }
        std::string missing;
        for (const auto& [name, weight] : resolved) {
            if (!available.count(name)) missing += (missing.empty() ? "" : ", ") + name;
        }
        if (!missing.empty()) {
            LOG("Warning: calibration step '%s' skipped, missing blendshapes: %s", step.display.c_str(), missing.c_str());
            continue;
        }
        step.pose = std::move(resolved);
        sequence.push_back(std::move(step));
    }
    return sequence;
}

void GuidedFlow::start() {
    if (onboarding_active_) enter_onboarding_step(0);
    else finish_onboarding();
}

void GuidedFlow::on_model_ready(const std::vector<std::string>& shape_names) {
    model_ready_ = true;
    shape_names_ = shape_names;
    sequence_ = compute_sequence();
    if (sequence_.empty()) {
        LOG("Warning: no usable target expressions, expression dialog disabled");
        dialog_.set_label("No target expressions available", true, true);
        dialog_.set_buttons({{"Exit", [this] { request_exit(); }}});
        onboarding_active_ = false;
        start_flow_when_ready_ = false;
    }
}

void GuidedFlow::on_model_failed() {
    dialog_.set_label("The head model failed to load.\nCheck the console output.", true, true);
    dialog_.set_buttons({{"Exit", [this] { request_exit(); }}});
    onboarding_active_ = false;
    start_flow_when_ready_ = false;
    pending_step_.reset();
}

// --- expression flow ------------------------------------------------------------

void GuidedFlow::show_expression() {
    const RoutineStep& step = current_step();
    dialog_.set_label(step.display + "  (" + std::to_string(index_ + 1) + "/" + std::to_string(sequence_.size()) + ")");
    targets_ = step.pose;
    play_narration(step.log);
    LOG("[expression] pose %s", step.log.c_str());
}

void GuidedFlow::show_done() {
    targets_.clear();  // back to a relaxed face
    cues_.bed(false);
    cues_.mark("end");
    play_narration("done");
    LOG("[expression] sequence complete");
    dialog_.set_label("All done — thank you!\nSaving the recording...", true, true);
    dialog_.set_buttons();
    recorder_.finish([this](const StopResult& result) { offer_upload(result, "All done — thank you!"); });
}

void GuidedFlow::offer_upload(const StopResult& result, const std::string& headline) {
    // Only a session the runtime finished cleanly ("status":"complete") can
    // be uploaded: not after a stop timeout, an error or a failed session
    if (!result.complete) {
        std::string text = headline;
        if (!result.session.empty()) {
            text += "\nThe recording didn't finish cleanly";
            text += result.reason.empty() ? "." : "\n(" + result.reason + ").";
        }
        show_finished(text);
        return;
    }
    if (!recorder_.upload_available() || !recorder_.consent) {
        show_finished(headline);
        return;
    }
    const std::string session = result.session;
    dialog_.set_label(headline + "\nUpload this recording to help\nimprove Babble face tracking?", true, true);
    dialog_.set_buttons({{"Upload",
                          [this, session] {
                              // The runtime packages, encrypts and uploads in the
                              // background and resumes after restarts; the app may
                              // exit any time
                              dialog_.set_buttons();
                              recorder_.upload(session, [this](std::optional<std::string> failure) {
                                  show_finished(failure ? "The upload couldn't be queued:\n" + *failure
                                                        : "Thanks! Uploading in the background.\n"
                                                          "You can close the app at any time.");
                              });
                          }},
                         {"Not now", [this, headline] { show_finished(headline); }}});
}

void GuidedFlow::on_runtime_stop(const StopResult& result) {
    // The runtime ended the session itself (low disk, shutting down): end the
    // routine like after our own stop and say why
    LOG("[capture] session %s stopped by the runtime: %s", result.session.c_str(), result.reason.c_str());
    phase_ = Phase::None;
    phase_start_ns_.reset();
    alt_segment_start_ns_.reset();
    practice_ = false;
    onboarding_active_ = false;
    pending_step_.reset();
    start_flow_when_ready_ = false;
    targets_.clear();
    cues_.bed(false);
    cues_.mark("end");
    std::string headline = "The runtime stopped the recording";
    headline += result.reason.empty() ? "." : ":\n" + result.reason;
    offer_upload(StopResult{result.session, result.complete, ""}, headline);
}

void GuidedFlow::show_finished(const std::string& message) {
    bool multi = message.find('\n') != std::string::npos;
    dialog_.set_label(message, multi, multi);
    // finish_onboarding waits for the head model if a runtime stop came early
    dialog_.set_buttons({{"Restart", [this] { finish_onboarding(); }}, {"Exit", [this] { request_exit(); }}});
}

void GuidedFlow::show_freeform_intro() {
    targets_.clear();
    dialog_.set_label(routine().freeform_intro, true, true);
    dialog_.set_buttons({{"Let's go!", [this] { with_recording([this] { on_freeform_start(); }); }},
                         {"Skip", [this] { show_done(); }}});
    // "freeform_ready", not "freeform_intro": that one belongs to the onboarding pane
    play_narration("freeform_ready");
    LOG("[expression] freeform intro");
}

void GuidedFlow::on_freeform_start() {
    start_phase(Phase::Freeform, routine().freeform_seconds);
    dialog_.set_buttons();
    freeform_next_shuffle_ = 0.0;
    cues_.mark("start");
    play_narration("freeform_start");
}

void GuidedFlow::start_phase(Phase phase, double seconds) {
    phase_ = phase;
    phase_seconds_ = seconds;
    phase_until_ = monotonic_seconds() + seconds;
    phase_seconds_shown_ = -1;
    phase_start_ns_ = monotonic_ns();
}

void GuidedFlow::log_record(std::string name, int64_t start_ns, int64_t end_ns, const std::string& phase, int step) {
    // Sent as each segment ends, never batched until stop. Practice reps keep
    // their "Practice_" label and get the "practice" phase.
    std::string segment_phase = phase;
    if (practice_) {
        name = "Practice_" + name;
        segment_phase = "practice";
    }
    recorder_.segment(name, start_ns, end_ns, step, segment_phase);
    LOG("[log] recorded: %s (%.2fs, step %d, %s)", name.c_str(), double(end_ns - start_ns) / 1e9, step,
        segment_phase.c_str());
}

void GuidedFlow::log_phase(const std::string& name, const std::string& phase) {
    if (!phase_start_ns_) return;
    int step = phase == "freeform" ? -1 : current_step().index;
    log_record(name, *phase_start_ns_, monotonic_ns(), phase, step);
}

void GuidedFlow::begin_hold_phase() {
    const RoutineStep& step = current_step();
    cues_.bed(true);
    if (step.mode == "alternate") {
        // One extra cycle up front: the first ON segment is logged as
        // "<log>_warmup" for ingestion to discard (reading/transition)
        double period = step.period.value_or(data_.alternate_period);
        start_phase(Phase::Alternate, step.seconds.value_or(routine().alternate_seconds) + period);
        alt_on_ = false;
        alt_segment_start_ns_.reset();
        alt_lean_id_ = -1;
        alt_segments_logged_ = 0;
        set_alternate_state(true);
    } else {
        start_phase(Phase::Hold, step.hold.value_or(routine().hold_seconds));
        cues_.target(step.audio_invert ? 0.70f : 0.30f, 0.10f);
    }
}

std::string GuidedFlow::alternate_label(int seconds) const {
    const RoutineStep& step = current_step();
    std::string cue = alt_on_ ? step.cue_on : step.cue_off;
    if (alt_segments_logged_ == 0) cue = "Practice: " + lower_ascii(cue);  // warmup cycle
    return cue + "  (" + std::to_string(seconds) + ")";
}

void GuidedFlow::log_alternate_segment(int64_t end_ns) {
    if (!alt_segment_start_ns_) return;
    std::string name = alt_on_ ? current_step().log : "Neutral_alt";
    std::string phase = alt_on_ ? "alternate_on" : "alternate_off";
    int seg_index = alt_segments_logged_++;
    if (seg_index == 0 && alt_on_) {
        name += "_warmup";
        phase = "warmup";
    }
    log_record(name, *alt_segment_start_ns_, end_ns, phase, current_step().index);
}

void GuidedFlow::set_alternate_state(bool on) {
    // Flip the cue: log the segment that just ended, move the head model (and
    // the wearer) to the new state, refresh the label
    int64_t now_ns = monotonic_ns();
    log_alternate_segment(now_ns);
    alt_on_ = on;
    alt_segment_start_ns_ = now_ns;
    bool inv = current_step().audio_invert;
    cues_.target(inv ? (on ? 0.0f : 1.0f) : (on ? 1.0f : 0.0f), 0.05f);  // land on the actual flip
    targets_.clear();
    if (on) targets_ = current_step().pose;
    double remaining = phase_until_ - monotonic_seconds();
    dialog_.set_label(alternate_label(std::max(1, int(std::ceil(remaining)))));
}

void GuidedFlow::finish_alternate() {
    log_alternate_segment(monotonic_ns());
    alt_segment_start_ns_.reset();
    targets_.clear();
    cues_.bed(false);
    cues_.mark("end");
    next_step();
}

void GuidedFlow::on_ready() {
    // Recording must be live before any labelled hold (normally the consent
    // pane already started it); if it can't run, pause
    with_recording([this] { begin_prep(); });
}

void GuidedFlow::begin_prep() {
    // Short get-ready pause so the transition isn't logged as part of the
    // hold; buttons hide so a stray trigger pull can't skip ahead mid-hold
    start_phase(Phase::Prep, data_.prep_seconds);
    dialog_.set_buttons();
    cues_.bed(false);
    cues_.mark("thunk");
    cues_.mark("start");  // rising ding in silence; the bed enters at the hold
}

void GuidedFlow::begin_relax_phase() {
    log_phase(current_step().log, "hold");
    start_phase(Phase::Relax, data_.relax_seconds);
    targets_.clear();
    cues_.bed(false);
    cues_.mark("end");
}

void GuidedFlow::advance_expression() {
    log_phase("Neutral", "relax");
    next_step();
}

void GuidedFlow::next_step() {
    phase_ = Phase::None;
    phase_start_ns_.reset();
    if (practice_) {
        // Practice rep complete: hand over to the real sequence
        practice_ = false;
        targets_.clear();
        dialog_.set_label("Nice work! That was just practice.\nNow we record the real thing.", true, true);
        dialog_.set_buttons({{"Start", [this] { start_expression_flow(); }}, {"Exit", [this] { request_exit(); }}});
        play_narration("practice_done");
        return;
    }
    ++index_;
    if (size_t(index_) >= sequence_.size()) {
        show_freeform_intro();
    } else {
        set_ready_buttons();
        show_expression();
    }
}

void GuidedFlow::on_back() {
    index_ = std::max(0, index_ - 1);
    cues_.bed(false);  // never leave the bed running outside a phase
    show_expression();
}

void GuidedFlow::start_expression_flow() {
    onboarding_active_ = false;
    index_ = 0;
    practice_ = false;
    set_ready_buttons();
    show_expression();
}

void GuidedFlow::set_ready_buttons() {
    dialog_.set_buttons({{"Ready", [this] { on_ready(); }}, {"Back", [this] { on_back(); }}});
}

// --- onboarding -----------------------------------------------------------------

void GuidedFlow::on_onboarding_next() { enter_onboarding_step(onboarding_index_ + 1); }

void GuidedFlow::on_onboarding_back() { enter_onboarding_step(std::max(0, onboarding_index_ - 1), -1); }

void GuidedFlow::accept_consent() {
    // The accepted terms version travels with "start" and is stored with the
    // session (uploads require it)
    recorder_.consent = json{{"version", data_.consent_version}, {"accepted_ns", monotonic_ns()}};
    LOG("[consent] accepted terms %s", data_.consent_version.c_str());
}

void GuidedFlow::on_consent() {
    // Recording starts now so the demo and the practice rep are captured too
    accept_consent();
    with_recording([this] { on_onboarding_next(); });
}

void GuidedFlow::require_consent(std::function<void()> then) {
    if (recorder_.consent) {
        then();
        return;
    }
    // The onboarding's consent pane, shown on its own (Skip intro, or any
    // path that reaches recording without it)
    auto pane = std::find_if(data_.onboarding.begin(), data_.onboarding.end(), [](const OnboardingStep& s) {
        return std::any_of(s.buttons.begin(), s.buttons.end(), [](const auto& b) { return b.second == "consent"; });
    });
    if (pane == data_.onboarding.end()) fail("routines.json: no consent pane");
    std::string agree = "I agree";
    for (const auto& [text, action] : pane->buttons) {
        if (action == "consent") agree = text;
    }
    onboarding_active_ = false;
    play_narration(pane->name);
    dialog_.set_label(pane->label, true, true);
    dialog_.set_buttons({{agree,
                          [this, then] {
                              accept_consent();
                              then();
                          }},
                         {"Exit", [this] { request_exit(); }}});
}

void GuidedFlow::choose_routine(const std::string& name) {
    routine_name_ = name;
    if (model_ready_) sequence_ = compute_sequence();
    LOG("[expression] routine: %s", name.c_str());
    on_onboarding_next();
}

bool GuidedFlow::onboarding_step_visible(const OnboardingStep& step) const {
    if (step.routine_choice) return recorder_.mouth_available() && data_.routines.count("mouth");
    if (step.refresh_warning) return refresh_warning_needed();
    return true;
}

// SteamVR runs the eye cameras at the display rate (capped at 90 fps), and
// this app's own session sets that rate, so the runtime's live "status" is
// what counts; the XR session's own rate covers a runtime that reports none
bool GuidedFlow::refresh_warning_needed() const {
    if (routine_name_ != "eye") return false;
    int hz = recorder_.display_hz();
    if (hz == 0) hz = int(std::lround(xr_display_hz_));
    int fps = recorder_.camera_fps();
    // The measured rates go to the log only, never on screen
    LOG("[capture] eye: display %d Hz, camera %d fps", hz, fps);
    return (hz > 0 && hz < 144) || (fps > 0 && fps < 90);
}

std::string GuidedFlow::onboarding_label(const OnboardingStep& step) const {
    return replace_all(step.label, "{freeform_seconds}", number(routine().freeform_seconds));
}

void GuidedFlow::check_rates_then(std::function<void()> then) {
    dialog_.set_label("One moment...", true, true);
    dialog_.set_buttons();
    recorder_.refresh_status([this, then] {
        rates_fresh_ = true;
        then();
        rates_fresh_ = false;
    });
}

void GuidedFlow::skip_onboarding() {
    // Skipping the intro still warns about a low refresh rate before an eye
    // routine
    auto warning = std::find_if(data_.onboarding.begin(), data_.onboarding.end(),
                                [](const OnboardingStep& s) { return s.refresh_warning; });
    // and still goes through the consent pane
    auto finish = [this] { require_consent([this] { finish_onboarding(); }); };
    if (warning == data_.onboarding.end() || routine_name_ != "eye") {
        finish();
        return;
    }
    if (!rates_fresh_) {
        check_rates_then([this] { skip_onboarding(); });
        return;
    }
    if (!refresh_warning_needed()) {
        finish();
        return;
    }
    dialog_.set_label(onboarding_label(*warning), true, true);
    dialog_.set_buttons({{"Continue", finish}, {"Back", [this] { enter_onboarding_step(0); }}});
}

void GuidedFlow::finish_onboarding() {
    onboarding_active_ = false;
    pending_step_.reset();
    if (model_ready_ && !sequence_.empty()) {
        start_expression_flow();
    } else {
        // Model still loading (e.g. the intro was skipped quickly)
        dialog_.set_label("Loading the head model...", true, true);
        dialog_.set_buttons();
        start_flow_when_ready_ = true;
    }
}

void GuidedFlow::enter_onboarding_step(int index, int direction) {
    // Skip panes that don't apply; index 0 (welcome) always applies
    const int count = int(data_.onboarding.size());
    while (index > 0 && index < count) {
        const OnboardingStep& candidate = data_.onboarding[size_t(index)];
        if (candidate.refresh_warning && routine_name_ == "eye" && !rates_fresh_) {
            // The rates in "hello" predate this app's XR session: ask again
            check_rates_then([this, index, direction] { enter_onboarding_step(index, direction); });
            return;
        }
        if (onboarding_step_visible(candidate)) break;
        index += direction;
    }
    if (index >= count) {
        finish_onboarding();
        return;
    }
    index = std::max(0, index);
    const OnboardingStep& step = data_.onboarding[size_t(index)];
    if ((step.demo || step.practice) && !model_ready_) {
        // Park until the background load delivers the head model
        pending_step_ = index;
        dialog_.set_label("One moment, preparing\nthe demonstration...", true, true);
        dialog_.set_buttons();
        return;
    }
    onboarding_index_ = index;
    pending_step_.reset();
    practice_ = false;
    targets_.clear();
    play_narration(step.name);
    if (step.practice) {
        if (sequence_.empty()) {
            finish_onboarding();
            return;
        }
        practice_ = true;
        index_ = 0;
        targets_ = sequence_[0].pose;  // the model holds the face
        dialog_.set_label(replace_all(step.label, "{name}", sequence_[0].short_name), true);
        dialog_.set_buttons({{"Ready", [this] { on_ready(); }}, {"Back", [this] { on_onboarding_back(); }}});
        return;
    }
    dialog_.set_label(onboarding_label(step), true, !step.demo);
    if (step.routine_choice) {
        dialog_.set_buttons({{"Eyes", [this] { choose_routine("eye"); }}, {"Mouth", [this] { choose_routine("mouth"); }}});
        return;
    }
    std::vector<std::pair<std::string, Dialog::Callback>> buttons;
    for (const auto& [text, action] : step.buttons) {
        Dialog::Callback cb;
        if (action == "next") cb = [this] { on_onboarding_next(); };
        else if (action == "back") cb = [this] { on_onboarding_back(); };
        else if (action == "skip") cb = [this] { skip_onboarding(); };
        else if (action == "exit") cb = [this] { request_exit(); };
        else if (action == "consent") cb = [this] { on_consent(); };
        buttons.emplace_back(text, std::move(cb));
    }
    dialog_.set_buttons(std::move(buttons));
}

// --- recording ------------------------------------------------------------------

void GuidedFlow::with_recording(std::function<void()> then) {
    if (recorder_.recording()) {
        then();
        return;
    }
    // Never record without consent: a session without it can't be uploaded
    if (!recorder_.consent) {
        require_consent([this, then] { with_recording(then); });
        return;
    }
    dialog_.set_label("Starting the recording...", true, true);
    dialog_.set_buttons();
    recorder_.ensure_started(routine_name_, data_.routine_def(routine_name_), [this, then](bool ok) {
        if (ok) then();
        else show_recording_problem(recorder_.problem_text());
    });
}

void GuidedFlow::show_recording_problem(const std::string& message) {
    // Stop prompting, drop the unfinished segment, offer Retry (a fresh
    // session) that resumes where the user was
    if (onboarding_active_) {
        int resume_index = onboarding_index_;
        resume_ = [this, resume_index] { enter_onboarding_step(resume_index); };
    } else if (phase_ == Phase::Freeform || size_t(index_) >= sequence_.size()) {
        resume_ = [this] { show_freeform_intro(); };
    } else {
        resume_ = [this] {
            set_ready_buttons();
            show_expression();
        };
    }
    recovery_active_ = true;
    phase_ = Phase::None;
    phase_start_ns_.reset();
    alt_segment_start_ns_.reset();
    targets_.clear();
    cues_.bed(false);
    recorder_.finish();
    LOG("[capture] paused: %s", message.c_str());
    dialog_.set_label(message, true, true);
    if (recorder_.retryable()) {
        dialog_.set_buttons({{"Retry", [this] { on_recording_retry(); }}, {"Exit", [this] { request_exit(); }}});
    } else {
        dialog_.set_buttons({{"Exit", [this] { request_exit(); }}});
    }
}

void GuidedFlow::request_exit() {
    // Wait for "stopped" (up to Recorder::STOP_REPLY_SECONDS) while still
    // rendering, so the runtime closes the session before the app goes
    if (!recorder_.recording()) {
        exit_requested_ = true;
        return;
    }
    recovery_active_ = true;   // no recording-problem pane while stopping
    phase_ = Phase::None;
    targets_.clear();
    cues_.bed(false);
    dialog_.set_label("Saving the recording...", true, true);
    dialog_.set_buttons();
    recorder_.finish([this](const StopResult&) { exit_requested_ = true; });
}

void GuidedFlow::on_recording_retry() {
    dialog_.set_label("Starting the recording...", true, true);
    dialog_.set_buttons();
    recorder_.ensure_started(routine_name_, data_.routine_def(routine_name_), [this](bool ok) {
        if (!ok) {
            show_recording_problem(recorder_.problem_text());
            return;
        }
        recovery_active_ = false;
        if (resume_) resume_();
    });
}

// --- per frame ------------------------------------------------------------------

void GuidedFlow::frame(double now) {
    // Recording health: pause the routine as soon as the runtime stops
    // recording usable data
    if (auto stop = recorder_.take_runtime_stop()) {
        recovery_active_ = false;
        on_runtime_stop(*stop);
    } else if (!recovery_active_) {
        if (auto problem = recorder_.problem(routine_name_)) show_recording_problem(*problem);
    }

    // Onboarding bookkeeping: resume a step waiting on the model, start the
    // main flow once it's ready, drive the demo pane's example expression
    if (pending_step_ && model_ready_) enter_onboarding_step(*pending_step_);
    if (start_flow_when_ready_ && model_ready_ && !sequence_.empty()) {
        start_flow_when_ready_ = false;
        start_expression_flow();
    }
    if (onboarding_active_ && !pending_step_ && size_t(onboarding_index_) < data_.onboarding.size() &&
        data_.onboarding[size_t(onboarding_index_)].demo && !sequence_.empty()) {
        float scale = float(std::sin(PI * std::fmod(now, 2.0) / 2.0));
        targets_.clear();
        for (const auto& [shape, weight] : sequence_[0].pose) targets_[shape] = weight * scale;
    }

    // Freeform: the model riffs on a fresh random mix every shuffle
    if (phase_ == Phase::Freeform && !shape_names_.empty() && now >= freeform_next_shuffle_) {
        freeform_next_shuffle_ = now + data_.shuffle_seconds;
        targets_.clear();
        std::vector<std::string> riff;
        for (const auto& s : shape_names_) {
            if (routine().freeform_prefixes.empty() ||
                std::any_of(routine().freeform_prefixes.begin(), routine().freeform_prefixes.end(),
                            [&](const std::string& p) { return s.rfind(p, 0) == 0; }))
                riff.push_back(s);
        }
        if (riff.empty()) riff = shape_names_;
        std::shuffle(riff.begin(), riff.end(), rng_);
        std::uniform_real_distribution<float> weight(0.3f, 1.0f);
        for (size_t i = 0; i < std::min<size_t>(3, riff.size()); ++i) targets_[riff[i]] = weight(rng_);
    }

    if (phase_ == Phase::None) return;
    double remaining = phase_until_ - now;
    if (remaining > 0 && phase_ == Phase::Alternate) {
        // Steady metronome: first half of each cycle is the expression
        double period = current_step().period.value_or(data_.alternate_period);
        double elapsed = phase_seconds_ - remaining;
        bool cue_on = std::fmod(elapsed, period) < period / 2;
        // Audio leans into the predicted flip 100 ms early and lands only
        // when the flip actually happens
        double half = period / 2;
        int flip_id = int(elapsed / half) + 1;
        double to_flip = flip_id * half - elapsed;
        if (to_flip <= 0.10 && alt_lean_id_ != flip_id) {
            alt_lean_id_ = flip_id;
            float lean_to = flip_id % 2 == 0 ? 1.0f : 0.0f;
            if (current_step().audio_invert) lean_to = 1.0f - lean_to;
            cues_.lean(lean_to, now + to_flip);
        }
        if (cue_on != alt_on_) set_alternate_state(cue_on);
    }
    if (remaining <= 0) {
        switch (phase_) {
        case Phase::Prep: begin_hold_phase(); break;
        case Phase::Hold: begin_relax_phase(); break;
        case Phase::Alternate: finish_alternate(); break;
        case Phase::Freeform:
            log_phase("Freeform", "freeform");
            phase_ = Phase::None;
            phase_start_ns_.reset();
            show_done();
            break;
        default: advance_expression(); break;
        }
        return;
    }
    int seconds = int(std::ceil(remaining));
    if (seconds == phase_seconds_shown_) return;
    phase_seconds_shown_ = seconds;
    if (phase_ == Phase::Freeform) {
        const auto& prompts = routine().freeform_prompts;
        if (prompts.empty()) return;
        double elapsed = routine().freeform_seconds - remaining;
        const std::string& prompt = prompts[size_t(elapsed / data_.prompt_seconds) % prompts.size()];
        dialog_.set_label(prompt + " (" + std::to_string(seconds) + ")");
    } else if (phase_ == Phase::Alternate) {
        dialog_.set_label(alternate_label(seconds));
    } else {
        if (phase_ == Phase::Hold) {
            // Stepped countdown: up to 5 notches, fully open (or closed, for
            // audio-inverted steps) at the boundary
            int total = std::max(int(phase_seconds_), 1);
            int notches = std::min(5, total);
            if (seconds <= notches) {
                int idx = notches - seconds;
                float level = 0.30f + 0.62f * float(idx) / float(std::max(notches - 1, 1));
                if (current_step().audio_invert) level = 1.0f - level;
                cues_.target(level, 0.08f);
            }
        }
        std::string text = phase_ == Phase::Prep    ? "Get ready... (" + std::to_string(seconds) + ")"
                           : phase_ == Phase::Hold  ? current_step().hold_label + " (" + std::to_string(seconds) + ")"
                                                    : "Relax your face (" + std::to_string(seconds) + ")";
        dialog_.set_label(text);
    }
}
