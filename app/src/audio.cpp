#include "audio.h"

#include "common.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <complex>
#include <cstring>
#include <fstream>
#include <map>
#include <mutex>
#include <optional>
#include <random>
#include <vector>

#if HAVE_PIPEWIRE
#include <pipewire/pipewire.h>
#include <spa/param/audio/format-utils.h>
#endif

namespace {

constexpr int SR = 48000;
constexpr int HOP = 256;          // 5.3 ms reaction granularity
constexpr int WIN = 1024;
constexpr float BED_LEVEL = 0.028f;
constexpr float DING_LEVEL = 0.22f;
constexpr double PI = 3.14159265358979323846;

using cfloat = std::complex<double>;

// In-place iterative radix-2 FFT (inverse unscaled)
void fft(std::vector<cfloat>& a, bool inverse) {
    const size_t n = a.size();
    for (size_t i = 1, j = 0; i < n; ++i) {
        size_t bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) std::swap(a[i], a[j]);
    }
    for (size_t len = 2; len <= n; len <<= 1) {
        double angle = 2 * PI / double(len) * (inverse ? 1 : -1);
        cfloat wlen(std::cos(angle), std::sin(angle));
        for (size_t i = 0; i < n; i += len) {
            cfloat w(1);
            for (size_t k = 0; k < len / 2; ++k) {
                cfloat u = a[i + k], v = a[i + k + len / 2] * w;
                a[i + k] = u + v;
                a[i + k + len / 2] = u - v;
                w *= wlen;
            }
        }
    }
}

double source_shape(double f) {
    f = std::max(f, 1.0);
    double shape = 1.0 / std::pow(f, 0.55);
    shape *= f * f / (f * f + 180.0 * 180.0);
    shape *= 1.0 / (1.0 + std::pow(f / 5000.0, 4));
    return shape;
}

// Streaming STFT overlap-add filter whose mask follows openness
class SweepFilter {
public:
    SweepFilter() : rng_(std::random_device{}()) {
        window_.resize(WIN);
        for (int i = 0; i < WIN; ++i) window_[i] = 0.5 - 0.5 * std::cos(2 * PI * i / (WIN - 1));
        in_.assign(WIN, 0.0);
        ola_.assign(WIN, 0.0);
    }

    void block(double openness, float* out) {
        std::memmove(in_.data(), in_.data() + HOP, (WIN - HOP) * sizeof(double));
        for (int i = WIN - HOP; i < WIN; ++i) in_[i] = normal_(rng_);
        std::vector<cfloat> spec(WIN);
        for (int i = 0; i < WIN; ++i) spec[i] = in_[i] * window_[i];
        fft(spec, false);
        const std::vector<double>& m = mask(openness);
        for (int k = 0; k <= WIN / 2; ++k) {
            spec[k] *= m[k];
            if (k > 0 && k < WIN / 2) spec[WIN - k] *= m[k];
        }
        fft(spec, true);
        std::memmove(ola_.data(), ola_.data() + HOP, (WIN - HOP) * sizeof(double));
        std::fill(ola_.begin() + (WIN - HOP), ola_.end(), 0.0);
        for (int i = 0; i < WIN; ++i) ola_[i] += spec[i].real() / WIN * window_[i];
        for (int i = 0; i < HOP; ++i) out[i] = float(ola_[i] / 1.5);  // hann 4x overlap sums to 1.5
    }

private:
    const std::vector<double>& mask(double openness) {
        int key = int(openness * 63.5);
        auto it = cache_.find(key);
        if (it != cache_.end()) return it->second;
        double oo = key / 63.0;
        double center = 280 * std::pow(700.0 / 280.0, oo);
        double cutoff = 1200 * std::pow(5000.0 / 1200.0, oo);
        std::vector<double> m(WIN / 2 + 1);
        double sum_sq = 0;
        for (int k = 0; k <= WIN / 2; ++k) {
            double f = std::max(double(k) * SR / WIN, 1.0);
            double v = std::exp(-0.5 * std::pow(std::log2(f / center) / (0.55 + 0.35 * oo), 2));
            v += 0.35 * oo * std::exp(-0.5 * std::pow(std::log2(f / (2.2 * center)) / 0.5, 2));
            v *= 1.0 / (1.0 + std::pow(f / cutoff, 6));
            v *= source_shape(double(k) * SR / WIN);
            m[k] = v;
            sum_sq += v * v;
        }
        double gain = std::sqrt(sum_sq / m.size() * 2) + 1e-9;
        for (double& v : m) v /= gain;
        return cache_.emplace(key, std::move(m)).first->second;
    }

    std::vector<double> window_, in_, ola_;
    std::map<int, std::vector<double>> cache_;
    std::mt19937 rng_;
    std::normal_distribution<double> normal_;
};

std::vector<float> kalimba(double f0, double dur, double level) {
    int n = int(dur * SR);
    std::vector<float> x(n);
    const double partials[4][3] = {{1.00, 1.0, 6.0}, {2.02, 0.45, 9.0}, {3.93, 0.18, 14.0}, {6.1, 0.07, 20.0}};
    int attack = int(0.004 * SR);
    for (int i = 0; i < n; ++i) {
        double t = double(i) / SR, v = 0;
        for (auto& p : partials) v += p[1] * std::sin(2 * PI * f0 * p[0] * t) * std::exp(-p[2] * t / dur * 3);
        if (i < attack) v *= double(i) / (attack - 1);
        x[i] = float(v / 1.7 * level);
    }
    return x;
}

std::vector<float> double_ding(bool rising) {
    double first = rising ? 587.33 : 784.0, second = rising ? 784.0 : 587.33;
    auto a = kalimba(first, 0.9, 1.0), b = kalimba(second, 1.0, 0.8);
    std::vector<float> out(int(1.25 * SR), 0.0f);
    for (size_t i = 0; i < a.size(); ++i) out[i] += a[i];
    int off = int(0.16 * SR);
    for (size_t i = 0; i < b.size() && off + i < out.size(); ++i) out[off + i] += b[i];
    for (float& v : out) v *= DING_LEVEL;
    return out;
}

std::vector<float> thunk() {
    int n = int(0.09 * SR);
    std::vector<float> x(n);
    for (int i = 0; i < n; ++i) {
        double t = double(i) / SR;
        double v = std::sin(2 * PI * 110 * t) * std::exp(-t * 45) + 0.4 * std::sin(2 * PI * 165 * t) * std::exp(-t * 60);
        x[i] = float(v * 0.35);
    }
    return x;
}

// 16-bit PCM WAV -> mono float at SR (linear resampling); empty on failure
std::vector<float> load_wav(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return {};
    std::vector<char> data((std::istreambuf_iterator<char>(f)), {});
    if (data.size() < 44 || std::memcmp(data.data(), "RIFF", 4) || std::memcmp(data.data() + 8, "WAVE", 4)) return {};
    uint16_t channels = 0, bits = 0;
    uint32_t rate = 0;
    size_t pos = 12;
    const char* pcm = nullptr;
    uint32_t pcm_size = 0;
    while (pos + 8 <= data.size()) {
        uint32_t size;
        std::memcpy(&size, data.data() + pos + 4, 4);
        if (!std::memcmp(data.data() + pos, "fmt ", 4) && size >= 16) {
            std::memcpy(&channels, data.data() + pos + 10, 2);
            std::memcpy(&rate, data.data() + pos + 12, 4);
            std::memcpy(&bits, data.data() + pos + 22, 2);
        } else if (!std::memcmp(data.data() + pos, "data", 4)) {
            pcm = data.data() + pos + 8;
            pcm_size = uint32_t(std::min<size_t>(size, data.size() - pos - 8));
        }
        pos += 8 + size + (size & 1);
    }
    if (!pcm || bits != 16 || channels == 0 || rate == 0) return {};
    size_t frames = pcm_size / (2 * channels);
    std::vector<float> mono(frames);
    for (size_t i = 0; i < frames; ++i) {
        float sum = 0;
        for (int c = 0; c < channels; ++c) {
            int16_t s;
            std::memcpy(&s, pcm + (i * channels + c) * 2, 2);
            sum += s / 32768.0f;
        }
        mono[i] = sum / channels;
    }
    if (rate == SR) return mono;
    size_t out_n = size_t(double(frames) * SR / rate);
    std::vector<float> out(out_n);
    for (size_t i = 0; i < out_n; ++i) {
        double src = double(i) * rate / SR;
        size_t j = size_t(src);
        double frac = src - j;
        float a = mono[std::min(j, frames - 1)], b = mono[std::min(j + 1, frames - 1)];
        out[i] = float(a + (b - a) * frac);
    }
    return out;
}

struct Command {
    enum Kind { Bed, Target, Lean, Land, Mark, Narration } kind;
    bool on = false;
    double value = 0, glide = 0, deadline = 0;
    std::shared_ptr<const std::vector<float>> clip;
};

// The engine (cue_audio.py Engine); all calls on the audio thread
class Engine {
public:
    Engine() {
        marks_["start"] = std::make_shared<const std::vector<float>>(double_ding(true));
        marks_["end"] = std::make_shared<const std::vector<float>>(double_ding(false));
        marks_["thunk"] = std::make_shared<const std::vector<float>>(thunk());
    }

    std::shared_ptr<const std::vector<float>> mark_clip(const std::string& kind) const {
        auto it = marks_.find(kind);
        return it == marks_.end() ? nullptr : it->second;
    }

    void handle(const Command& c, double now) {
        switch (c.kind) {
        case Command::Bed: bed_on_ = c.on; break;
        case Command::Target:
            glide_ = Glide{openness_, c.value, now, now + std::max(c.glide, 0.01)};
            has_lean_ = false;
            break;
        case Command::Lean: {
            double hover = openness_ + 0.9 * (c.value - openness_);
            glide_ = Glide{openness_, hover, now, std::max(c.deadline, now + 0.02)};
            lean_to_ = c.value;
            has_lean_ = true;
            break;
        }
        case Command::Land:
            if (has_lean_) {
                glide_ = Glide{openness_, lean_to_, now, now + 0.03};
                has_lean_ = false;
            }
            break;
        case Command::Mark:
            if (c.clip) playing_.push_back({c.clip, 0, false});
            break;
        case Command::Narration:
            playing_.erase(std::remove_if(playing_.begin(), playing_.end(),
                                          [](const Playing& p) { return p.narration; }),
                           playing_.end());
            if (c.clip) playing_.push_back({c.clip, 0, true});
            break;
        }
    }

    void block(double now, float* out) {
        advance_openness(now);
        double step = double(HOP) / SR / 0.04;
        bed_gain_ = bed_on_ ? std::min(1.0, bed_gain_ + step) : std::max(0.0, bed_gain_ - step);
        if (bed_gain_ > 0.0) {
            filter_.block(openness_, out);
            for (int i = 0; i < HOP; ++i) out[i] *= float(bed_gain_ * BED_LEVEL);
        } else {
            std::fill(out, out + HOP, 0.0f);
        }
        for (auto& p : playing_) {
            size_t take = std::min<size_t>(HOP, p.clip->size() - p.pos);
            for (size_t i = 0; i < take; ++i) out[i] += (*p.clip)[p.pos + i];
            p.pos += take;
        }
        playing_.erase(std::remove_if(playing_.begin(), playing_.end(),
                                      [](const Playing& p) { return p.pos >= p.clip->size(); }),
                       playing_.end());
        for (int i = 0; i < HOP; ++i) out[i] = std::clamp(out[i], -1.0f, 1.0f);
    }

private:
    struct Glide { double from, to, t0, t1; };
    struct Playing { std::shared_ptr<const std::vector<float>> clip; size_t pos; bool narration; };

    void advance_openness(double now) {
        if (!glide_) return;
        if (now >= glide_->t1) {
            openness_ = glide_->to;
            if (!has_lean_) glide_.reset();
        } else {
            double phase = (now - glide_->t0) / (glide_->t1 - glide_->t0);
            openness_ = glide_->from + (glide_->to - glide_->from) * (1 - std::cos(PI * phase)) / 2;
        }
    }

    SweepFilter filter_;
    std::map<std::string, std::shared_ptr<const std::vector<float>>> marks_;
    std::vector<Playing> playing_;
    double openness_ = 0, bed_gain_ = 0;
    bool bed_on_ = false, has_lean_ = false;
    double lean_to_ = 0;
    std::optional<Glide> glide_;
};

}  // namespace

struct CueAudio::Impl {
    Engine engine;
    std::mutex mutex;
    std::vector<Command> queue;
    std::vector<float> pending;   // generated but not yet delivered samples
    bool running = false;
#if HAVE_PIPEWIRE
    pw_thread_loop* loop = nullptr;
    pw_stream* stream = nullptr;
    spa_hook listener{};
#endif

    void push(Command c) {
        if (!running) return;
        std::lock_guard<std::mutex> lock(mutex);
        queue.push_back(std::move(c));
    }

    // Audio thread: fill n samples, generating whole blocks as needed
    void fill(float* dst, size_t n) {
        double now = monotonic_seconds();
        {
            std::unique_lock<std::mutex> lock(mutex, std::try_to_lock);
            if (lock.owns_lock()) {
                for (auto& c : queue) engine.handle(c, now);
                queue.clear();
            }
        }
        float block[HOP];
        while (pending.size() < n) {
            engine.block(now, block);
            pending.insert(pending.end(), block, block + HOP);
            now += double(HOP) / SR;
        }
        std::memcpy(dst, pending.data(), n * sizeof(float));
        pending.erase(pending.begin(), pending.begin() + long(n));
    }

#if HAVE_PIPEWIRE
    static void on_process(void* data) {
        auto* self = static_cast<Impl*>(data);
        pw_buffer* b = pw_stream_dequeue_buffer(self->stream);
        if (!b) return;
        spa_buffer* buf = b->buffer;
        auto* dst = static_cast<float*>(buf->datas[0].data);
        if (dst) {
            uint32_t n = buf->datas[0].maxsize / sizeof(float);
            if (b->requested) n = std::min<uint32_t>(uint32_t(b->requested), n);
            self->fill(dst, n);
            buf->datas[0].chunk->offset = 0;
            buf->datas[0].chunk->stride = sizeof(float);
            buf->datas[0].chunk->size = n * sizeof(float);
        }
        pw_stream_queue_buffer(self->stream, b);
    }
#endif

    bool start() {
#if HAVE_PIPEWIRE
        pw_init(nullptr, nullptr);
        loop = pw_thread_loop_new("cue-audio", nullptr);
        if (!loop) return false;
        static const pw_stream_events events = [] {
            pw_stream_events e{};
            e.version = PW_VERSION_STREAM_EVENTS;
            e.process = &Impl::on_process;
            return e;
        }();
        pw_properties* props = pw_properties_new(
            PW_KEY_MEDIA_TYPE, "Audio", PW_KEY_MEDIA_CATEGORY, "Playback",
            PW_KEY_MEDIA_ROLE, "Game", PW_KEY_NODE_LATENCY, "256/48000",
            PW_KEY_APP_NAME, "Babble Dataset Capture", nullptr);
        stream = pw_stream_new_simple(pw_thread_loop_get_loop(loop), "Dataset capture cues",
                                      props, &events, this);
        if (!stream) return false;
        uint8_t buffer[1024];
        spa_pod_builder builder{};
        spa_pod_builder_init(&builder, buffer, sizeof(buffer));
        spa_audio_info_raw info{};
        info.format = SPA_AUDIO_FORMAT_F32;
        info.rate = SR;
        info.channels = 1;
        info.position[0] = SPA_AUDIO_CHANNEL_MONO;
        const spa_pod* params[1] = {spa_format_audio_raw_build(&builder, SPA_PARAM_EnumFormat, &info)};
        if (pw_stream_connect(stream, PW_DIRECTION_OUTPUT, PW_ID_ANY,
                              pw_stream_flags(PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS |
                                              PW_STREAM_FLAG_RT_PROCESS),
                              params, 1) < 0) {
            return false;
        }
        if (pw_thread_loop_start(loop) < 0) return false;
        running = true;
        return true;
#else
        return false;
#endif
    }

    void shutdown() {
#if HAVE_PIPEWIRE
        if (loop) pw_thread_loop_stop(loop);
        if (stream) pw_stream_destroy(stream);
        if (loop) pw_thread_loop_destroy(loop);
        if (loop) pw_deinit();
        stream = nullptr;
        loop = nullptr;
#endif
        running = false;
    }
};

CueAudio::CueAudio() : impl_(std::make_unique<Impl>()) {
    if (!impl_->start()) {
        LOG("[cue_audio] audio unavailable; cues are silent");
        impl_->shutdown();
    }
}

CueAudio::~CueAudio() { stop(); }

void CueAudio::bed(bool on) { impl_->push({Command::Bed, on}); }
void CueAudio::target(float openness, float glide) {
    Command c{Command::Target};
    c.value = openness;
    c.glide = glide;
    impl_->push(c);
}
void CueAudio::lean(float openness, double deadline) {
    Command c{Command::Lean};
    c.value = openness;
    c.deadline = deadline;
    impl_->push(c);
}
void CueAudio::land() { impl_->push({Command::Land}); }
void CueAudio::mark(const std::string& kind) {
    Command c{Command::Mark};
    c.clip = impl_->engine.mark_clip(kind);
    impl_->push(c);
}
void CueAudio::narration(const std::string& wav_path) {
    if (!impl_->running) return;
    auto pcm = load_wav(wav_path);
    if (pcm.empty()) return;  // clips are optional, per file
    Command c{Command::Narration};
    c.clip = std::make_shared<const std::vector<float>>(std::move(pcm));
    impl_->push(c);
}
void CueAudio::stop() {
    if (impl_) impl_->shutdown();
}

bool CueAudio::selftest(const std::string& wav_path) {
    Engine engine;
    struct Step { double t; Command c; };
    auto target = [](double o, double g) { Command c{Command::Target}; c.value = o; c.glide = g; return c; };
    auto mark = [&](const char* k) { Command c{Command::Mark}; c.clip = engine.mark_clip(k); return c; };
    std::vector<Step> script = {
        {0.10, mark("thunk")}, {0.40, mark("start")},
        {1.60, Command{Command::Bed, true}}, {1.60, target(0.30, 0.10)},
        {2.70, target(0.45, 0.08)}, {3.70, target(0.60, 0.08)},
        {4.70, target(0.75, 0.08)}, {5.70, target(0.92, 0.08)},
        {6.70, Command{Command::Bed, false}}, {7.00, mark("end")},
    };
    const double total = 8.5;
    int blocks = int(total * SR / HOP);
    std::vector<int16_t> pcm;
    pcm.reserve(size_t(blocks) * HOP);
    size_t next = 0;
    float out[HOP];
    for (int b = 0; b < blocks; ++b) {
        double t = double(b) * HOP / SR;
        while (next < script.size() && script[next].t <= t) engine.handle(script[next++].c, t);
        engine.block(t, out);
        for (float v : out) pcm.push_back(int16_t(v * 32767));
    }
    std::ofstream f(wav_path, std::ios::binary);
    if (!f) return false;
    uint32_t data_size = uint32_t(pcm.size() * 2), riff = 36 + data_size, fmt_size = 16, rate = SR, byte_rate = SR * 2;
    uint16_t pcm_fmt = 1, channels = 1, align = 2, bits = 16;
    f.write("RIFF", 4); f.write(reinterpret_cast<char*>(&riff), 4); f.write("WAVE", 4);
    f.write("fmt ", 4); f.write(reinterpret_cast<char*>(&fmt_size), 4);
    f.write(reinterpret_cast<char*>(&pcm_fmt), 2); f.write(reinterpret_cast<char*>(&channels), 2);
    f.write(reinterpret_cast<char*>(&rate), 4); f.write(reinterpret_cast<char*>(&byte_rate), 4);
    f.write(reinterpret_cast<char*>(&align), 2); f.write(reinterpret_cast<char*>(&bits), 2);
    f.write("data", 4); f.write(reinterpret_cast<char*>(&data_size), 4);
    f.write(reinterpret_cast<const char*>(pcm.data()), std::streamsize(data_size));
    LOG("selftest wrote %s (%.1fs)", wav_path.c_str(), total);
    return bool(f);
}
