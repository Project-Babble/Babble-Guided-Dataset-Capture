#!/usr/bin/env python3
"""Realtime state-following audio cues for frame_eyes.

The engine runs as a child process (own GIL, recorder-daemon pattern) and
keeps NO schedule of its own: the renderer's sequence state machine sends
one-line JSON messages and the audio follows within ~1-2 blocks (~10 ms).
If the sequence stretches because the app hitches, the audio stretches
with it — visual state, log timestamps and audio always agree.

Sound palette (settled with the offline demos in audio-cues/gen_cues.py):
  - sweep bed: noise through a filter that glides between a "closed" (mmm)
    and an "open" (ahh) shape; background level
  - rising double ding = segment start; falling = segment end
  - thunk = button-press confirmation
  The bed and the dings are never played at the same time by convention:
  the renderer turns the bed off before it sends a mark.

Protocol (JSON object per line on the child's stdin):
  {"bed": true|false}              noise on/off (40 ms gain ramp)
  {"target": o, "glide": s}        glide openness to o over s seconds
  {"lean": o, "deadline": t}       glide 90 % of the way to o, arriving at
                                   monotonic time t, then hover
  {"land": true}                   complete the pending lean (30 ms)
  {"mark": "start"|"end"|"thunk"}  mix a one-shot mark
  {"quit": true}

Parent-side use:
    from cue_audio import CueAudio
    cue = CueAudio()          # no-op shell if audio is unavailable
    cue.mark("start"); cue.bed(True); cue.target(0.3)
    ...
    cue.stop()
"""
import json
import os
import selectors
import subprocess
import sys
import time

import numpy as np

SR = 48000
HOP = 256                 # 5.3 ms reaction granularity
WIN = 1024
BED_LEVEL = 0.028         # settled background level (~-27 dB RMS)
DING_LEVEL = 0.22
PACE_AHEAD = 0.02         # keep only ~20 ms queued toward the sink

# --- synthesis (matches gen_cues.py voicing) --------------------------------

def _source_shape(f):
    """Spectral tilt of the noise source: airy hush, no rumble, soft top."""
    f = np.maximum(f, 1.0)
    shape = 1 / f ** 0.55
    shape *= f ** 2 / (f ** 2 + 180.0 ** 2)
    shape *= 1 / (1 + (f / 5000.0) ** 4)
    return shape

class SweepFilter:
    """Streaming STFT overlap-add filter whose mask follows 'openness'."""

    def __init__(self):
        self.window = np.hanning(WIN)
        self.freqs = np.fft.rfftfreq(WIN, 1 / SR)
        self.source = _source_shape(self.freqs)
        self.in_buf = np.zeros(WIN)
        self.ola = np.zeros(WIN)
        self.rng = np.random.default_rng()
        self._mask_cache = {}

    def _mask(self, openness):
        key = int(openness * 63.5)
        cached = self._mask_cache.get(key)
        if cached is None:
            oo = key / 63.0
            f = np.maximum(self.freqs, 1.0)
            center = 280 * (700 / 280) ** oo
            cutoff = 1200 * (5000 / 1200) ** oo
            mask = np.exp(-0.5 * (np.log2(f / center) / (0.55 + 0.35 * oo)) ** 2)
            mask += 0.35 * oo * np.exp(-0.5 * (np.log2(f / (2.2 * center)) / 0.5) ** 2)
            mask *= 1 / (1 + (f / cutoff) ** 6)
            mask *= self.source
            # Deterministic loudness normalization (white input, rms == gain)
            gain = np.sqrt(np.mean(mask ** 2) * 2) + 1e-9
            cached = mask / gain
            self._mask_cache[key] = cached
        return cached

    def block(self, openness):
        """Produces HOP samples of bed noise at the given openness."""
        self.in_buf = np.roll(self.in_buf, -HOP)
        self.in_buf[-HOP:] = self.rng.standard_normal(HOP)
        seg = np.fft.irfft(np.fft.rfft(self.in_buf * self.window)
                           * self._mask(openness), WIN)
        self.ola = np.roll(self.ola, -HOP)
        self.ola[-HOP:] = 0.0
        self.ola += seg * self.window
        # hann with 4x overlap sums to a constant 1.5
        return self.ola[:HOP] / 1.5

def _kalimba(f0, dur=0.9, level=1.0):
    n = int(dur * SR)
    t = np.arange(n) / SR
    partials = [(1.00, 1.0, 6.0), (2.02, 0.45, 9.0), (3.93, 0.18, 14.0), (6.1, 0.07, 20.0)]
    x = np.zeros(n)
    for ratio, amp, damp in partials:
        x += amp * np.sin(2 * np.pi * f0 * ratio * t) * np.exp(-damp * t / dur * 3)
    a = int(0.004 * SR)
    x[:a] *= np.linspace(0, 1, a)
    return x / 1.7 * level

def _double_ding(rising):
    first, second = (587.33, 784.0) if rising else (784.0, 587.33)
    a = _kalimba(first, 0.9)
    b = _kalimba(second, 1.0, 0.8)
    out = np.zeros(int(1.25 * SR))
    out[: len(a)] += a
    off = int(0.16 * SR)
    out[off : off + len(b)] += b
    return out * DING_LEVEL

def _thunk():
    n = int(0.09 * SR)
    t = np.arange(n) / SR
    x = np.sin(2 * np.pi * 110 * t) * np.exp(-t * 45)
    x += 0.4 * np.sin(2 * np.pi * 165 * t) * np.exp(-t * 60)
    return x * 0.35

# --- child process: the engine ----------------------------------------------

class Engine:
    def __init__(self, sink_write):
        self.sink_write = sink_write
        self.filter = SweepFilter()
        self.marks = {"start": _double_ding(True), "end": _double_ding(False),
                      "thunk": _thunk()}
        self.active_marks = []       # [array, position]
        self.openness = 0.0
        self.bed_gain = 0.0
        self.bed_on = False
        # glide: (from, to, t_start, t_end); lean holds at 90 % until landed
        self.glide = None
        self.lean_to = None

    def handle(self, msg):
        now = time.monotonic()
        if "bed" in msg:
            self.bed_on = bool(msg["bed"])
        if "target" in msg:
            dur = max(float(msg.get("glide", 0.1)), 0.01)
            self.glide = (self.openness, float(msg["target"]), now, now + dur)
            self.lean_to = None
        if "lean" in msg:
            to = float(msg["lean"])
            hover = self.openness + 0.9 * (to - self.openness)
            deadline = max(float(msg["deadline"]), now + 0.02)
            self.glide = (self.openness, hover, now, deadline)
            self.lean_to = to
        if msg.get("land") and self.lean_to is not None:
            self.glide = (self.openness, self.lean_to, now, now + 0.03)
            self.lean_to = None
        if "mark" in msg:
            arr = self.marks.get(msg["mark"])
            if arr is not None:
                self.active_marks.append([arr, 0])

    def _advance_openness(self, now):
        if self.glide is not None:
            o0, o1, t0, t1 = self.glide
            if now >= t1:
                self.openness = o1
                if self.lean_to is None:
                    self.glide = None
            else:
                phase = (now - t0) / (t1 - t0)
                self.openness = o0 + (o1 - o0) * (1 - np.cos(np.pi * phase)) / 2

    def block(self):
        now = time.monotonic()
        self._advance_openness(now)
        # 40 ms bed gain ramp, computed per block
        step = HOP / SR / 0.04
        self.bed_gain = min(1.0, self.bed_gain + step) if self.bed_on \
            else max(0.0, self.bed_gain - step)
        if self.bed_gain > 0.0:
            out = self.filter.block(self.openness) * (self.bed_gain * BED_LEVEL)
        else:
            out = np.zeros(HOP)
        for slot in self.active_marks:
            arr, pos = slot
            take = min(HOP, len(arr) - pos)
            out[:take] += arr[pos : pos + take]
            slot[1] += take
        self.active_marks = [s for s in self.active_marks if s[1] < len(s[0])]
        self.sink_write(np.clip(out, -1, 1))

def child_main():
    paplay = subprocess.Popen(
        ["paplay", "--raw", "--format=s16le", f"--rate={SR}", "--channels=1",
         "--latency-msec=20"],
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def sink_write(block):
        paplay.stdin.write((block * 32767).astype(np.int16).tobytes())

    engine = Engine(sink_write)
    sel = selectors.DefaultSelector()
    os.set_blocking(sys.stdin.fileno(), False)
    sel.register(sys.stdin, selectors.EVENT_READ)
    pending = ""
    start = time.monotonic()
    blocks = 0
    try:
        while True:
            # Read any state messages that have arrived
            if sel.select(timeout=0):
                chunk = sys.stdin.read(65536)
                if chunk == "":
                    break  # parent closed the pipe
                if chunk:
                    pending += chunk
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        if not line.strip():
                            continue
                        msg = json.loads(line)
                        if msg.get("quit"):
                            return
                        engine.handle(msg)
            engine.block()
            blocks += 1
            # Self-pacing: stay only PACE_AHEAD ahead of real time, so state
            # changes reach the ear within ~2 blocks
            ahead = start + blocks * HOP / SR - time.monotonic() - PACE_AHEAD
            if ahead > 0:
                time.sleep(ahead)
    finally:
        try:
            paplay.stdin.close()
            paplay.wait(2)
        except Exception:
            paplay.kill()

# --- self-test: render a scripted sequence to a wav (no audio device) -------

def selftest(path):
    import wave
    out = []

    def sink_write(block):
        out.append((block * 32767).astype(np.int16))

    engine = Engine(sink_write)
    script = [
        (0.10, {"mark": "thunk"}),
        (0.40, {"mark": "start"}),
        (1.60, {"bed": True}),
        (1.60, {"target": 0.30, "glide": 0.10}),
        (2.70, {"target": 0.45, "glide": 0.08}),
        (3.70, {"target": 0.60, "glide": 0.08}),
        (4.70, {"target": 0.75, "glide": 0.08}),
        (5.70, {"target": 0.92, "glide": 0.08}),
        (6.70, {"bed": False}),
        (7.00, {"mark": "end"}),
    ]
    total = 8.5
    fake_now = [0.0]
    real_monotonic = time.monotonic

    def fake_monotonic():
        return fake_now[0]

    time.monotonic = fake_monotonic
    try:
        n_blocks = int(total * SR / HOP)
        for b in range(n_blocks):
            t = b * HOP / SR
            fake_now[0] = t
            while script and script[0][0] <= t:
                engine.handle(script.pop(0)[1])
            engine.block()
    finally:
        time.monotonic = real_monotonic
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(np.concatenate(out).tobytes())
    print(f"selftest wrote {path} ({total:.1f}s)")

# --- parent-side API ---------------------------------------------------------

class CueAudio:
    """Fail-safe handle: if the child or paplay is unavailable, every call
    silently no-ops so the sequence never depends on audio."""

    def __init__(self):
        self.proc = None
        try:
            self.proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--child"],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as exc:
            print(f"[cue_audio] disabled: {exc}")

    def _send(self, obj):
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode())
            self.proc.stdin.flush()
        except Exception:
            self.proc = None

    def bed(self, on):
        self._send({"bed": bool(on)})

    def target(self, openness, glide=0.10):
        self._send({"target": openness, "glide": glide})

    def lean(self, openness, deadline):
        self._send({"lean": openness, "deadline": deadline})

    def land(self):
        self._send({"land": True})

    def mark(self, kind):
        self._send({"mark": kind})

    def stop(self):
        if self.proc is None:
            return
        self._send({"quit": True})
        try:
            self.proc.stdin.close()
            self.proc.wait(2)
        except Exception:
            self.proc.kill()
        self.proc = None

if __name__ == "__main__":
    if "--child" in sys.argv:
        child_main()
    elif "--selftest" in sys.argv:
        selftest(sys.argv[sys.argv.index("--selftest") + 1])
