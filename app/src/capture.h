#pragma once
// Client for the Babble Frame Runtime capture control socket (protocol 2,
// HANDOFF.md section 3) and the Recorder the guided flow drives. The runtime
// records its own cameras; the app starts/stops sessions and sends one
// "segment" label as each segment ends (CLOCK_MONOTONIC ns). Everything is
// non-blocking and pumped once per frame, so the XR frame loop never stalls,
// except the startup hello and the shutdown stop, which wait briefly.

#include <nlohmann/json.hpp>

#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <string>

using json = nlohmann::json;

struct CaptureFailure {
    std::string code;     // runtime code (busy, no_source, disk, unavailable, bad_request,
                          // protocol) or local (no_socket, disconnected, timeout)
    std::string message;
};

class CaptureClient {
public:
    using Reply = std::function<void(const json& reply)>;  // op "error" on failure

    explicit CaptureClient(std::string path) : path_(std::move(path)) {}
    ~CaptureClient() { close("closed"); }
    CaptureClient(const CaptureClient&) = delete;
    CaptureClient& operator=(const CaptureClient&) = delete;

    bool open(CaptureFailure& failure);
    void close(const std::string& reason);
    bool connected() const { return fd_ >= 0; }
    const std::string& path() const { return path_; }

    void request(json message, Reply on_reply, double timeout_s = 5.0);
    bool send(const json& message);
    void pump();
    // Pumps (sleeping in poll) until done() or the timeout; returns done()
    bool wait(const std::function<bool()>& done, double timeout_s);

    std::optional<json> last_status;          // latest "status" (reply or event)
    double last_status_time = 0.0;
    std::optional<json> last_event_error;     // latest id-less "error"
    std::optional<json> runtime_stop;         // id-less "stopped": the runtime stopped on its own
    std::string close_reason;

private:
    struct Pending { Reply reply; double deadline; };
    void dispatch(const std::string& line);
    void flush();
    void fail_pending(const std::string& code, const std::string& message);

    std::string path_;
    int fd_ = -1;
    int64_t next_id_ = 1;
    std::map<int64_t, Pending> pending_;
    std::string in_buf_, out_buf_;
};

// How a session ended: "stopped" from the runtime (the reply to "stop", or
// an id-less event when the runtime stopped on its own), or no reply
struct StopResult {
    std::string session;     // empty: no session was running
    bool complete = false;   // "stopped" with "status":"complete"; only then offer upload
    std::string reason;      // the runtime's reason, or why no "stopped" came
};

class Recorder {
public:
    explicit Recorder(std::string socket_path) : client_(std::move(socket_path)) {}

    // Startup: connect + hello, waiting up to timeout_s. Failure is not fatal:
    // the flow explains it and offers Retry when recording is first needed.
    bool connect_blocking(double timeout_s);
    void pump() { client_.pump(); }

    // routine_def: the routine's definition, stored by the runtime as
    // routine.json next to the recording
    void ensure_started(const std::string& routine, const json& routine_def, std::function<void(bool ok)> done);
    // step: index into routine_def.steps, -1 outside the steps; phase: practice,
    // hold, relax, alternate_on, alternate_off, warmup or freeform
    void segment(const std::string& label, int64_t start_ns, int64_t end_ns, int step, const std::string& phase);
    // Stops the running session; done(result), result.session empty if none ran. The runtime
    // closes its encoders before "stopped", which can take a while with long
    // sessions, so the reply gets STOP_REPLY_SECONDS; an "upload" sent before
    // "stopped" is refused.
    static constexpr double STOP_REPLY_SECONDS = 15.0;
    void finish(std::function<void(const StopResult& result)> done = nullptr);
    void finish_blocking(double timeout_s);
    void upload(const std::string& session,
                std::function<void(std::optional<std::string> failure)> done);
    void close();

    // Per frame: the runtime stopped the running session on its own (low
    // disk, shutting down); the session is over, like after finish()
    std::optional<StopResult> take_runtime_stop();
    // Per frame while a session runs: why it no longer records usable data
    std::optional<std::string> problem(const std::string& routine);
    std::string problem_text() const;
    bool retryable() const { return !error_ || error_->code != "protocol"; }

    bool recording() const { return !session_.empty(); }
    bool busy() const { return busy_; }
    bool mouth_available() const;
    bool upload_available() const;
    // Asks for a fresh "status"; done() runs on the reply, an error or a timeout
    void refresh_status(std::function<void()> done);
    // Eye display rate and camera rate from the latest "status" (live, and
    // after the XR session may have changed the display rate), else "hello"
    int display_hz() const;    // 0 = unknown
    int camera_fps() const;    // 0 = unknown

    std::optional<json> consent;   // {"version", "accepted_ns"} once accepted

private:
    void hello(std::function<void(bool ok)> done);
    void set_error(const json& reply);
    int eye_value(const char* key) const;

    CaptureClient client_;
    std::optional<json> hello_;
    std::string session_;
    std::optional<CaptureFailure> error_;
    double source_bad_since_ = -1.0;
    double not_recording_since_ = -1.0;
    bool busy_ = false;
};
