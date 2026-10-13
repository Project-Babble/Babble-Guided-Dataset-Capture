#pragma once
// Small shared helpers: logging, monotonic time, error checks.

#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <time.h>

#define LOG(...) do { std::fprintf(stdout, __VA_ARGS__); std::fputc('\n', stdout); std::fflush(stdout); } while (0)
#define LOG_ERR(...) do { std::fprintf(stderr, __VA_ARGS__); std::fputc('\n', stderr); std::fflush(stderr); } while (0)

// CLOCK_MONOTONIC nanoseconds: the clock the runtime stamps camera frames
// with, used for every segment label (HANDOFF.md section 3).
inline int64_t monotonic_ns() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return int64_t(ts.tv_sec) * 1000000000LL + ts.tv_nsec;
}

inline double monotonic_seconds() { return double(monotonic_ns()) * 1e-9; }

struct Error : std::runtime_error {
    using std::runtime_error::runtime_error;
};

[[noreturn]] inline void fail(const std::string& message) { throw Error(message); }
