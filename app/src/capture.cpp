#include "capture.h"

#include "common.h"
#include "version.h"

#include <cerrno>
#include <cmath>
#include <cstring>
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <vector>

static json local_error(const std::string& code, const std::string& message) {
    return json{{"op", "error"}, {"code", code}, {"message", message}};
}

// --- CaptureClient ----------------------------------------------------------

bool CaptureClient::open(CaptureFailure& failure) {
    close("reconnecting");
    int fd = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        failure = {"no_socket", std::strerror(errno)};
        return false;
    }
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    if (path_.size() >= sizeof(addr.sun_path)) {
        ::close(fd);
        failure = {"no_socket", "socket path too long: " + path_};
        return false;
    }
    std::memcpy(addr.sun_path, path_.c_str(), path_.size() + 1);
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        failure = {"no_socket", path_ + ": " + std::strerror(errno)};
        ::close(fd);
        return false;
    }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
    fd_ = fd;
    close_reason.clear();
    in_buf_.clear();
    out_buf_.clear();
    last_status.reset();
    last_event_error.reset();
    runtime_stop.reset();
    return true;
}

void CaptureClient::close(const std::string& reason) {
    if (fd_ < 0) return;
    ::close(fd_);
    fd_ = -1;
    if (close_reason.empty()) close_reason = reason;
    fail_pending("disconnected", reason);
}

void CaptureClient::fail_pending(const std::string& code, const std::string& message) {
    auto pending = std::move(pending_);
    pending_.clear();
    for (auto& [id, p] : pending) p.reply(local_error(code, message));
}

bool CaptureClient::send(const json& message) {
    if (fd_ < 0) return false;
    out_buf_ += message.dump() + "\n";
    flush();
    return fd_ >= 0;
}

void CaptureClient::flush() {
    while (fd_ >= 0 && !out_buf_.empty()) {
        ssize_t n = ::send(fd_, out_buf_.data(), out_buf_.size(), MSG_NOSIGNAL);
        if (n > 0) {
            out_buf_.erase(0, size_t(n));
        } else if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            return;  // kernel buffer full: the rest goes out on the next pump
        } else if (n < 0 && errno == EINTR) {
            continue;
        } else {
            close(std::string("send failed: ") + std::strerror(errno));
        }
    }
}

void CaptureClient::request(json message, Reply on_reply, double timeout_s) {
    if (fd_ < 0) {
        on_reply(local_error("disconnected", close_reason.empty() ? "not connected" : close_reason));
        return;
    }
    int64_t id = next_id_++;
    message["id"] = id;
    pending_[id] = Pending{std::move(on_reply), monotonic_seconds() + timeout_s};
    if (!send(message)) return;  // close() already failed the pending reply
}

void CaptureClient::pump() {
    if (fd_ < 0) return;
    flush();
    char buf[65536];
    std::string closed;  // set when the connection ended during this read
    while (fd_ >= 0) {
        ssize_t n = ::recv(fd_, buf, sizeof(buf), 0);
        if (n > 0) {
            in_buf_.append(buf, size_t(n));
            continue;
        }
        if (n == 0) {
            closed = "runtime closed the connection";
        } else if (errno == EINTR) {
            continue;
        } else if (errno != EAGAIN && errno != EWOULDBLOCK) {
            closed = std::string("read failed: ") + std::strerror(errno);
        }
        break;
    }
    // Dispatch what arrived before closing: the runtime sends its final
    // reply (e.g. a protocol error) and then hangs up
    size_t newline;
    while ((newline = in_buf_.find('\n')) != std::string::npos) {
        std::string line = in_buf_.substr(0, newline);
        in_buf_.erase(0, newline + 1);
        if (line.find_first_not_of(" \t\r") != std::string::npos) dispatch(line);
    }
    if (!closed.empty()) close(closed);
    // Collect first: a reply callback may close the client or send requests
    double now = monotonic_seconds();
    std::vector<Reply> expired;
    for (auto it = pending_.begin(); it != pending_.end();) {
        if (now >= it->second.deadline) {
            expired.push_back(std::move(it->second.reply));
            it = pending_.erase(it);
        } else {
            ++it;
        }
    }
    for (auto& reply : expired) reply(local_error("timeout", "the runtime did not reply"));
}

void CaptureClient::dispatch(const std::string& line) {
    json message = json::parse(line, nullptr, false);
    if (message.is_discarded() || !message.is_object()) {
        LOG_ERR("[capture] ignoring malformed line: %.200s", line.c_str());
        return;
    }
    std::string op = message.value("op", "");
    if (op == "status") {
        last_status = message;
        last_status_time = monotonic_seconds();
    }
    if (message.contains("id") && message["id"].is_number_integer()) {
        auto it = pending_.find(message["id"].get<int64_t>());
        if (it != pending_.end()) {
            Reply reply = std::move(it->second.reply);
            pending_.erase(it);
            reply(message);
            return;
        }
    }
    if (op == "stopped") {
        runtime_stop = message;
        LOG("[capture] the runtime stopped the recording: %s", message.value("reason", "?").c_str());
    } else if (op == "error") {
        last_event_error = message;
        LOG_ERR("[capture] runtime error: %s: %s", message.value("code", "?").c_str(),
                message.value("message", "").c_str());
    }
}

bool CaptureClient::wait(const std::function<bool()>& done, double timeout_s) {
    double deadline = monotonic_seconds() + timeout_s;
    while (!done()) {
        double left = deadline - monotonic_seconds();
        if (left <= 0 || fd_ < 0) {
            pump();  // expire / fail what's pending
            break;
        }
        pollfd p{fd_, short(POLLIN | (out_buf_.empty() ? 0 : POLLOUT)), 0};
        ::poll(&p, 1, std::max(1, int(left * 1000)));
        pump();
    }
    return done();
}

// --- Recorder -----------------------------------------------------------------

void Recorder::set_error(const json& reply) {
    error_ = CaptureFailure{reply.value("code", "unknown"), reply.value("message", "")};
}

void Recorder::hello(std::function<void(bool ok)> done) {
    CaptureFailure failure;
    if (!client_.open(failure)) {
        error_ = failure;
        LOG("[capture] runtime unavailable: %s", failure.message.c_str());
        done(false);
        return;
    }
    json message = {{"op", "hello"}, {"protocol", CAPTURE_PROTOCOL},
                    {"app", "babble-dataset-capture"}, {"version", APP_VERSION}};
    client_.request(message, [this, done](const json& reply) {
        if (reply.value("op", "") == "error") {
            set_error(reply);
            client_.close("hello refused");
            hello_.reset();
            LOG("[capture] hello failed: %s: %s", error_->code.c_str(), error_->message.c_str());
            done(false);
            return;
        }
        if (reply.value("protocol", -1) != CAPTURE_PROTOCOL) {
            error_ = CaptureFailure{"protocol", "runtime speaks protocol " +
                                    std::to_string(reply.value("protocol", -1))};
            client_.close("protocol mismatch");
            hello_.reset();
            done(false);
            return;
        }
        hello_ = reply;
        error_.reset();
        LOG("[capture] connected to runtime %s (%s): sources %s",
            reply.value("runtime", "?").c_str(), reply.value("profile", "?").c_str(),
            reply.value("sources", json::object()).dump().c_str());
        done(true);
    }, 2.0);
}

bool Recorder::connect_blocking(double timeout_s) {
    bool finished = false, ok = false;
    hello([&](bool result) { finished = true; ok = result; });
    client_.wait([&] { return finished; }, timeout_s);
    return ok;
}

static StopResult stop_result(const std::string& session, const json& stopped) {
    StopResult result{session};
    if (stopped.value("op", "") != "stopped") {
        result.reason = stopped.value("message", stopped.value("code", "no reply"));
        return result;
    }
    result.session = stopped.value("session", session);
    result.complete = stopped.value("status", "") == "complete";
    result.reason = stopped.value("reason", "");
    if (!result.complete && stopped.contains("error") && stopped["error"].is_string()) {
        result.reason += (result.reason.empty() ? "" : ": ") + stopped["error"].get<std::string>();
    }
    return result;
}

void Recorder::ensure_started(const std::string& routine, const json& routine_def,
                              std::function<void(bool ok)> done) {
    if (!session_.empty() && client_.connected()) {
        done(true);
        return;
    }
    if (busy_) return;  // a start is already in flight; its callback reports
    session_.clear();
    busy_ = true;
    auto start = [this, routine, routine_def, done]() {
        if (hello_ && hello_->contains("recording") && hello_->at("recording") == false) {
            error_ = CaptureFailure{"unavailable", "runtime reports no recorder"};
            busy_ = false;
            done(false);
            return;
        }
        json message = {{"op", "start"}, {"routine", routine}, {"routine_def", routine_def}};
        if (consent) message["consent"] = *consent;
        client_.request(message, [this, done](const json& reply) {
            busy_ = false;
            if (reply.value("op", "") == "error") {
                set_error(reply);
                LOG("[capture] start refused: %s: %s", error_->code.c_str(), error_->message.c_str());
                done(false);
                return;
            }
            session_ = reply.value("session", "session");
            client_.last_status.reset();
            client_.runtime_stop.reset();
            error_.reset();
            source_bad_since_ = -1.0;
            not_recording_since_ = -1.0;
            LOG("[capture] recording session %s -> %s", session_.c_str(),
                reply.value("dir", "?").c_str());
            done(true);
        });
    };
    if (client_.connected() && hello_) {
        start();
        return;
    }
    hello([this, start, done](bool ok) {
        if (ok) {
            start();
        } else {
            busy_ = false;
            done(false);
        }
    });
}

void Recorder::segment(const std::string& label, int64_t start_ns, int64_t end_ns, int step,
                       const std::string& phase) {
    if (session_.empty()) return;
    if (!client_.send({{"op", "segment"}, {"label", label}, {"start_ns", start_ns}, {"end_ns", end_ns},
                       {"step", step}, {"phase", phase}})) {
        error_ = CaptureFailure{"disconnected", client_.close_reason};
    }
}

void Recorder::finish(std::function<void(const StopResult&)> done) {
    std::string session = std::move(session_);
    session_.clear();
    if (session.empty()) {
        if (done) done(StopResult{});
        return;
    }
    client_.request({{"op", "stop"}}, [this, session, done](const json& reply) {
        StopResult result = stop_result(session, reply);
        // The runtime stopped on its own just before our "stop" arrived: its
        // "stopped" event is the outcome
        if (reply.value("op", "") == "error" && client_.runtime_stop &&
            client_.runtime_stop->value("session", "") == session) {
            result = stop_result(session, *client_.runtime_stop);
            client_.runtime_stop.reset();
        }
        if (reply.value("op", "") == "error") {
            // The runtime finalises the session itself when the socket drops
            LOG("[capture] stop failed (%s); the runtime finalises on disconnect",
                reply.value("code", "?").c_str());
        } else {
            LOG("[capture] stopped %s (%s, %s): frames %s, dropped %s", session.c_str(),
                reply.value("status", "?").c_str(), result.reason.c_str(),
                reply.value("frames", json::object()).dump().c_str(),
                reply.value("dropped", json::object()).dump().c_str());
        }
        if (done) done(result);
    }, STOP_REPLY_SECONDS);
}

std::optional<StopResult> Recorder::take_runtime_stop() {
    if (!client_.runtime_stop) return std::nullopt;
    json stopped = std::move(*client_.runtime_stop);
    client_.runtime_stop.reset();
    std::string session = stopped.value("session", "");
    if (session_.empty() || (!session.empty() && session != session_)) return std::nullopt;  // not ours
    StopResult result = stop_result(session_, stopped);
    session_.clear();
    return result;
}

void Recorder::finish_blocking(double timeout_s) {
    bool finished = false;
    finish([&](const StopResult&) { finished = true; });
    client_.wait([&] { return finished; }, timeout_s);
}

void Recorder::upload(const std::string& session,
                      std::function<void(std::optional<std::string>)> done) {
    client_.request({{"op", "upload"}, {"session", session}}, [done](const json& reply) {
        if (reply.value("op", "") == "error") {
            std::string why = reply.value("message", "");
            done(why.empty() ? reply.value("code", "error") : why);
        } else {
            done(std::nullopt);
        }
    });
}

void Recorder::close() {
    client_.close("closed");
}

std::optional<std::string> Recorder::problem(const std::string& routine) {
    if (session_.empty()) return std::nullopt;
    if (!client_.connected()) {
        error_ = CaptureFailure{"disconnected", client_.close_reason};
        return problem_text();
    }
    if (!client_.last_status) return std::nullopt;
    const json& status = *client_.last_status;
    if (status.contains("recording") && status["recording"] == false) {
        // The runtime announces its own stops with a "stopped" event, which
        // the flow handles; this covers a runtime that stopped silently
        double now = monotonic_seconds();
        if (not_recording_since_ < 0) not_recording_since_ = now;
        else if (now - not_recording_since_ >= 2.0) return std::string("The runtime stopped the recording.");
        return std::nullopt;
    }
    not_recording_since_ = -1.0;
    std::string needed = routine == "mouth" ? "mouth" : "eye";
    std::string state;
    if (status.contains("sources") && status["sources"].contains(needed)) {
        const json& source = status["sources"][needed];
        if (source.is_object()) state = source.value("state", "");
        else if (source.is_string()) state = source.get<std::string>();
    }
    if (state != "Streaming") {
        double now = monotonic_seconds();
        if (source_bad_since_ < 0) {
            source_bad_since_ = now;
        } else if (now - source_bad_since_ >= 2.0) {
            return "The " + needed + " camera stopped streaming\n(" +
                   (state.empty() ? "missing" : state) + ").";
        }
    } else {
        source_bad_since_ = -1.0;
    }
    return std::nullopt;
}

std::string Recorder::problem_text() const {
    if (!error_) return "Recording isn't available.";
    static const std::map<std::string, std::string> text = {
        {"no_socket", "Can't reach Babble Frame Runtime.\nMake sure it's running."},
        {"protocol", "This app and the runtime don't match.\n"
                     "Update the dataset capture app\n(or the runtime)."},
        {"unavailable", "The runtime can't record right now\n(no video encoder available)."},
        {"busy", "Another recording is already running."},
        {"no_source", "A camera this routine needs\nisn't available."},
        {"disk", "Not enough disk space to record."},
        {"disconnected", "Lost the connection to the runtime."},
        {"timeout", "The runtime stopped responding."},
    };
    auto it = text.find(error_->code);
    if (it != text.end()) return it->second;
    return "Recording failed:\n" + error_->code;
}

static const json* source_of(const std::optional<json>& message, const char* name) {
    if (!message || !message->contains("sources")) return nullptr;
    const json& sources = (*message)["sources"];
    if (!sources.is_object() || !sources.contains(name)) return nullptr;
    return &sources[name];
}

bool Recorder::mouth_available() const {
    const json* mouth = source_of(hello_, "mouth");
    return mouth && mouth->is_object() && mouth->contains("kind") && !(*mouth)["kind"].is_null();
}

bool Recorder::upload_available() const {
    return hello_ && hello_->value("upload", false);
}

void Recorder::refresh_status(std::function<void()> done) {
    client_.request({{"op", "status"}}, [done](const json&) { done(); }, 2.0);
}

int Recorder::eye_value(const char* key) const {
    for (const auto* message : {&client_.last_status, &hello_}) {
        const json* eye = source_of(*message, "eye");
        if (eye && eye->is_object() && eye->contains(key) && (*eye)[key].is_number()) {
            return int(std::lround((*eye)[key].get<double>()));
        }
    }
    return 0;
}

int Recorder::display_hz() const { return eye_value("display_hz"); }

int Recorder::camera_fps() const { return eye_value("camera_fps"); }
