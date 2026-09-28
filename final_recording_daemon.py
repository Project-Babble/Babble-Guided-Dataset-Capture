#!/usr/bin/env python3
"""Steam Frame camera recording daemon.

Records the face (V4L2) and eye (DSP shared-memory) cameras into a single
800x800 quad-view mp4 at 120 fps, plus a binary timestamp db with one '>3q'
record per video frame: (face_l, face_r, eye) capture times from time.time_ns().

Three ways to use it:

1. Standalone CLI (records until Ctrl+C):
       python final_recording_daemon.py [-o out.mp4] [-s stamps.bin] [--fps 120]

2. Imported, in-process (runs on background threads):
       from final_recording_daemon import RecordingDaemon
       daemon = RecordingDaemon("session.mp4", "stamps.bin", verbose=False)
       daemon.start()
       ...
       daemon.stop()
   (also usable as a context manager)

3. Imported, as a child process — recommended when the caller has its own
   realtime loop (e.g. the VR renderer): the recorder gets its own GIL and
   cannot affect the caller's frame timing:
       import final_recording_daemon as recording
       proc = recording.launch_subprocess("session.mp4", "stamps.bin")
       ...
       recording.stop_subprocess(proc)  # SIGINT -> graceful save
"""
import argparse
import array
import json
import mmap
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import numpy as np

import camera_setup
from camera_setup import CameraSetupError

# Configuration
FACE_PRIMARY_DEV = "/dev/video9"
FACE_SECONDARY_DEV = "/dev/video13"
FACE_WIDTH, FACE_HEIGHT = 400, 400

EYE_WIDTH, EYE_HEIGHT = 512, 512
EYE_L_BASE = 0x12349f
EYE_R_BASE = 0x22340d

# Control-block base inside the DSP shared memory. The 2026-07-31 SteamVR
# dsp_service update moved it from 0x124000; the layout relative to this base
# (idx @ +0x80/+0x1001c0, locks after, ring buffers @ +0xc0/+0x100200 with
# 0x40040 stride) is unchanged.
DSP_CTRL_BASE = 0x234000

DEFAULT_OUTPUT = "quad_stereo_decoupled.mp4"
DEFAULT_STAMPS = "stampdb.bin"

class DSPClient:
    """Client to retrieve shared memory file descriptors and control the camera stream."""
    def __init__(self, version=17):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(b'\0dsp_service')

        _, ancdata, _, _ = self.sock.recvmsg(256, socket.CMSG_LEN(8))
        self.fds = array.array("i", ancdata[0][2])
        self.sock.send(struct.pack("<I", version))

        # Map region 1 (Data/Buffers) at offset 0
        self.mmap_data = mmap.mmap(
            self.fds[0], 0x1000000, mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE, offset=0
        )
        self.mmap = self.mmap_data

        # Map region 2 (Control Block) at offset 0x80000000
        self.mmap_ctrl = mmap.mmap(
            self.fds[0], 0x1000000, mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE, offset=0x80000000
        )

    def set_dsp_fps(self, target_fps=120.0):
        """Attempts to set the target sensor frame rate on the DSP."""
        ctrl_base = DSP_CTRL_BASE
        self.mmap_ctrl[ctrl_base + 0x10 : ctrl_base + 0x14] = struct.pack("<f", target_fps)

    def start_streaming(self):
        """Forces streaming initialization registers to be rewritten on every run."""
        ctrl_base = DSP_CTRL_BASE

        # Reset all buffer locks to 1 (Free)
        for i in range(8):
            self.mmap_ctrl[ctrl_base + 0x84 + i*4 : ctrl_base + 0x84 + i*4 + 4] = struct.pack("<I", 1)
            self.mmap_ctrl[ctrl_base + 0x1001c4 + i*4 : ctrl_base + 0x1001c4 + i*4 + 4] = struct.pack("<I", 1)

        # Force-write stream enables
        self.mmap_ctrl[ctrl_base + 0x48 : ctrl_base + 0x50] = struct.pack("<II", 1, 1)

        # Force transition trigger
        self.mmap_ctrl[ctrl_base + 0x04 : ctrl_base + 0x08] = struct.pack("<I", 1)

    def get_latest_stereo_pair(self):
        ctrl_base = DSP_CTRL_BASE
        frame_bytes = EYE_WIDTH * EYE_HEIGHT

        idx_l_bytes = self.mmap_ctrl[ctrl_base + 0x80 : ctrl_base + 0x84]
        idx_r_bytes = self.mmap_ctrl[ctrl_base + 0x1001c0 : ctrl_base + 0x1001c4]

        idx_l = struct.unpack("<I", idx_l_bytes)[0]
        idx_r = struct.unpack("<I", idx_r_bytes)[0]

        # 0xffffffff means not-ready; anything else > 7 means the control-block
        # layout moved again (see DSP_CTRL_BASE) — don't chase a wild pointer.
        if idx_l > 7 or idx_r > 7:
            idx_l = 0
            idx_r = 0

        addr_l = ctrl_base + 0xc0 + (idx_l * 0x40040)
        addr_r = ctrl_base + 0x100200 + (idx_r * 0x40040)

        frame_l = np.frombuffer(
            self.mmap_data,
            dtype=np.uint8,
            count=frame_bytes,
            offset=addr_l
        ).reshape((EYE_HEIGHT, EYE_WIDTH))

        frame_r = np.frombuffer(
            self.mmap_data,
            dtype=np.uint8,
            count=frame_bytes,
            offset=addr_r
        ).reshape((EYE_HEIGHT, EYE_WIDTH))

        self.mmap_ctrl[ctrl_base + 0x84 + idx_l*4 : ctrl_base + 0x84 + idx_l*4 + 4] = struct.pack("<I", 1)
        self.mmap_ctrl[ctrl_base + 0x1001c4 + idx_r*4 : ctrl_base + 0x1001c4 + idx_r*4 + 4] = struct.pack("<I", 1)

        return frame_l, frame_r

def getOutputStream(file, width, height, framerate=120, format="gray", codec="libx264", audioFile=None,
                    preset="ultrafast", crf=10, lossless=False, maxrate_kbps=None):
    # -qp 0 is true lossless; otherwise rate control is CRF (lower = higher quality,
    # x264's default is 23). maxrate_kbps adds a VBV cap on top of CRF: quality
    # stays at the CRF target while cheap, and the rate ceiling engages on noisy
    # content — this is what enforces the session size budget.
    # NOTE: lossless gray produces High 4:4:4 Predictive, which most hardware
    # decoders cannot play (renders as grey macroblock soup).
    quality = ("-preset", preset) + (("-qp", "0") if lossless else ("-crf", str(crf)))
    if not lossless:
        # Dark-region fidelity, measured against the lossless reference
        # (2026-08-27): 10-bit internal precision + narrow quantizer deadzones
        # + no DCT decimation keep the faint dark texture that default x264
        # smooths away (it is the data that matters when gained up). No pixel
        # transform is applied anywhere. NOTE: 10-bit H.264 is software-decode
        # only — fine for a training pipeline.
        quality += ("-pix_fmt", "yuv420p10le",
                    "-x264-params", "deadzone-inter=6:deadzone-intra=6:no-dct-decimate=1")
    if maxrate_kbps and not lossless:
        quality += ("-maxrate", f"{int(maxrate_kbps)}k", "-bufsize", f"{int(2 * maxrate_kbps)}k")
    proc = subprocess.Popen([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-r", str(framerate),
        "-f", "rawvideo",
        "-pix_fmt", format,
        "-s", f"{width}x{height}",
        "-i", "-",
        *(("-i", audioFile, "-c:a", "copy", "-c:v", codec, *quality, "-shortest", file)
          if audioFile is not None else ("-c:v", codec, *quality, file))
    ], stdin=subprocess.PIPE)
    return proc.stdin.write, proc

# --- Thread-Safe Pointer Swapping Buffer ---

class SafeFrameBuffer:
    """A thread-safe container that keeps only the latest frame pointer."""
    def __init__(self, default_frame):
        self.lock = threading.Lock()
        self.frame = default_frame
        self.stamp = 0

    def set(self, frame, stamp):
        with self.lock:
            self.frame = frame
            self.stamp = stamp

    def get(self):
        with self.lock:
            return self.frame, self.stamp

# --- Asynchronous Thread Workers ---

def face_reader_worker(pipe, size, buffer, stop_event, thread_name):
    """Continuously reads raw bytes from the V4L2 pipe and updates the shared buffer."""
    while not stop_event.is_set():
        try:
            raw_data = pipe.read(size)
            if not raw_data or len(raw_data) < size:
                break
            frame = np.frombuffer(raw_data, dtype=np.uint8).reshape((FACE_HEIGHT, FACE_WIDTH))
            buffer.set(frame, time.time_ns())
        except Exception as e:
            print(f"\n[ERROR] Exception in {thread_name}: {e}")
            traceback.print_exc()
            break

def dsp_reader_worker(dsp_client, buffer, stop_event):
    """Continuously polls indices and fetches eye frames unthrottled."""
    ctrl_base = DSP_CTRL_BASE
    last_l = -1
    last_r = -1

    while not stop_event.is_set():
        try:
            # Query the DSP indices
            idx_l = struct.unpack("<I", dsp_client.mmap_ctrl[ctrl_base + 0x80 : ctrl_base + 0x84])[0]
            idx_r = struct.unpack("<I", dsp_client.mmap_ctrl[ctrl_base + 0x1001c0 : ctrl_base + 0x1001c4])[0]

            if idx_l > 7 or idx_r > 7:
                # not-ready (0xffffffff) or an unrecognized layout — never index with it
                time.sleep(0.001)
                continue

            if idx_l != last_l or idx_r != last_r:
                frame_l, frame_r = dsp_client.get_latest_stereo_pair()
                buffer.set((frame_l, frame_r), time.time_ns())
                last_l = idx_l
                last_r = idx_r
            else:
                # 500us yield to prevent thread spin locks
                time.sleep(0.0005)
        except Exception as e:
            print(f"\n[ERROR] Exception in DSP Thread: {e}")
            traceback.print_exc()
            break

# --- Recording Daemon ---

class RecordingDaemon:
    """Owns the whole capture pipeline: V4L2 face streams, DSP eye stream,
    ffmpeg encoding and the timestamp db. start() returns once capture threads
    are running; stop() shuts everything down and finalizes the mp4. Both are
    idempotent, and the class works as a context manager."""

    def __init__(self, output_path=DEFAULT_OUTPUT, stamp_path=DEFAULT_STAMPS,
                 fps=120.0, verbose=True, exposure=None, analog_gain=None,
                 preset="ultrafast", crf=6, lossless=True, eye_only=None):
        self.output_path = output_path
        self.stamp_path = stamp_path
        self.fps = fps
        self.verbose = verbose
        self.exposure = exposure          # None leaves the sensor value as-is
        self.analog_gain = analog_gain    # None leaves the sensor value as-is
        self.preset = preset
        self.crf = crf
        self.lossless = lossless
        # Eye-only mode: skips the OV6211 module / FPGA-gated routing / v4l2
        # face streams; only the internal eye cameras (dsp_service shm) are
        # captured. eye_only=None (default) auto-detects: eye-only when no
        # mouth-tracker accessory is present. Requesting face capture without
        # the accessory is an error. The 800x800 quad canvas is kept in both
        # modes (face quadrants stay black in eye-only) so downstream
        # ingestion sees one fixed geometry.
        if eye_only is None:
            # verify_stream: registers and subdevs can claim a tracker that is
            # not really there; only a delivered frame counts before we commit
            # a whole session to face capture.
            eye_only = not camera_setup.mouth_tracker_present(verify_stream=True)
            print(f"[camera] mouth tracker {'absent or feed dead -> eye-only capture' if eye_only else 'verified (live frame) -> full capture'}")
        elif not eye_only and not camera_setup.mouth_tracker_present(verify_stream=True):
            raise CameraSetupError(
                "face capture requested (--no-eye-only) but the mouth tracker is "
                "absent or its video feed delivers no frames")
        self.eye_only = eye_only
        self.canvas_w = 800
        self.canvas_h = 800
        self.frame_count = 0

        self._stop_event = threading.Event()
        self._running = False
        self._face_procs = []
        self._capture_threads = []
        self._record_thread = None
        self._dsp = None
        self._ffmpeg_proc = None
        self._write_frame = None
        self._stamp_writer = None
        self._buf_l = self._buf_r = self._buf_eye = None

    @property
    def running(self):
        return self._running and not self._stop_event.is_set()

    def _log(self, message):
        if self.verbose:
            print(message)

    def start(self):
        if self._running:
            return self
        self._stop_event.clear()
        self.frame_count = 0

        if self.eye_only:
            self._log("Eye-only mode: skipping face camera bootstrap and streams")
            self._face_procs = []
        else:
            # Full camera bootstrap (module, FPGA-checked routing + pad formats,
            # node formats, exposure/gain) — replaces the manual web UI step
            self._log("Preparing cameras (module / routing / controls)...")
            camera_setup.ensure_cameras_ready(exposure=self.exposure,
                                              analog_gain=self.analog_gain)

            self._log("Starting face camera streams...")
            self._face_procs = [
                subprocess.Popen(
                    ["v4l2-ctl", "-d", dev, "--stream-mmap", "--stream-to=-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                for dev in (FACE_PRIMARY_DEV, FACE_SECONDARY_DEV)
            ]

        self._log("Connecting to DSP shared memory...")
        try:
            self._dsp = DSPClient()
        except Exception:
            for proc in self._face_procs:
                proc.terminate()
            self._face_procs = []
            raise

        # Force the DSP streaming registers to initialize
        self._dsp.start_streaming()
        self._dsp.set_dsp_fps(self.fps)

        default_face = np.zeros((FACE_HEIGHT, FACE_WIDTH), dtype=np.uint8)
        default_eye = np.zeros((EYE_HEIGHT, EYE_WIDTH), dtype=np.uint8)
        self._buf_l = SafeFrameBuffer(default_face)
        self._buf_r = SafeFrameBuffer(default_face)
        self._buf_eye = SafeFrameBuffer((default_eye, default_eye))

        self._capture_threads = [
            threading.Thread(
                target=dsp_reader_worker,
                args=(self._dsp, self._buf_eye, self._stop_event),
                daemon=True),
        ]
        if not self.eye_only:
            self._capture_threads += [
                threading.Thread(
                    target=face_reader_worker,
                    args=(self._face_procs[0].stdout, FACE_WIDTH * FACE_HEIGHT,
                          self._buf_l, self._stop_event, "Face_L_Thread"),
                    daemon=True),
                threading.Thread(
                    target=face_reader_worker,
                    args=(self._face_procs[1].stdout, FACE_WIDTH * FACE_HEIGHT,
                          self._buf_r, self._stop_event, "Face_R_Thread"),
                    daemon=True),
            ]
        for thread in self._capture_threads:
            thread.start()

        self._write_frame, self._ffmpeg_proc = getOutputStream(
            self.output_path, self.canvas_w, self.canvas_h, framerate=int(self.fps),
            format="gray", codec="libx264",
            preset=self.preset, crf=self.crf, lossless=self.lossless
        )
        self._stamp_writer = open(self.stamp_path, "wb")

        self._record_thread = threading.Thread(target=self._record_loop, daemon=True)
        self._record_thread.start()
        self._running = True
        self._log(f"Recording to {self.output_path} (stamps: {self.stamp_path})")
        return self

    def _record_loop(self):
        canvas = np.zeros((self.canvas_h, self.canvas_w), dtype=np.uint8)
        triple_timestamp_packer = struct.Struct('>3q')

        # Software interval timer: pace the muxer at exactly self.fps
        target_interval = 1.0 / self.fps
        next_frame_time = time.perf_counter() + target_interval

        try:
            while not self._stop_event.is_set():
                # Grab the absolute newest pointers from the buffers (non-blocking)
                face_l, stamp_fl = self._buf_l.get()
                face_r, stamp_fr = self._buf_r.get()
                eye_data, stamp_e = self._buf_eye.get()
                eye_l, eye_r = eye_data

                self._stamp_writer.write(triple_timestamp_packer.pack(stamp_fl, stamp_fr, stamp_e))

                # The active 400x400 window inside the 512x512 eye buffer sits at
                # rows 0..399, cols 64..463 for both eyes since the 2026-07-31
                # dsp_service update (it used to differ per eye).
                canvas[0:400, 0:400] = face_l
                canvas[0:400, 400:800] = face_r
                canvas[400:800, 0:400] = eye_l[0:400, 64:464]
                canvas[400:800, 400:800] = eye_r[0:400, 64:464]

                self._write_frame(canvas.tobytes())

                self.frame_count += 1
                if self.frame_count == int(2 * self.fps):
                    checks = (("eye", stamp_e),) if self.eye_only else \
                        (("face_l", stamp_fl), ("face_r", stamp_fr), ("eye", stamp_e))
                    for name, stamp in checks:
                        if stamp == 0:
                            print(f"\n[WARN] No {name} frames 2s into recording — "
                                  f"that stream is recording blank")
                if self.verbose and self.frame_count % 120 == 0:
                    print(f"Processed {self.frame_count} frames...", end="\r")

                # High-precision sleep scheduling
                sleep_time = next_frame_time - time.perf_counter()
                if sleep_time > 0:
                    time.sleep(sleep_time)
                next_frame_time += target_interval
        except Exception as e:
            if not self._stop_event.is_set():
                print(f"\n[ERROR] Recording loop stopped: {e}")
                traceback.print_exc()
                self._stop_event.set()

    def stop(self):
        if not self._running:
            return
        self._stop_event.set()

        if self._record_thread is not None:
            self._record_thread.join(timeout=2.0)
        if self._stamp_writer is not None:
            self._stamp_writer.close()

        # Gracefully shut down V4L2 processes
        for proc in self._face_procs:
            proc.terminate()
        for proc in self._face_procs:
            proc.wait()

        if self._ffmpeg_proc is not None:
            if self._ffmpeg_proc.stdin:
                self._ffmpeg_proc.stdin.close()
            self._ffmpeg_proc.wait()

        for thread in self._capture_threads:
            thread.join(timeout=1.0)

        self._running = False
        print(f"Capture completed. Saved {self.frame_count} frames to {self.output_path}")

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, tb):
        self.stop()

# --- Subprocess helpers (own GIL; no impact on the caller's frame loop) ---

def launch_subprocess(output_path=DEFAULT_OUTPUT, stamp_path=DEFAULT_STAMPS,
                      fps=120.0, exposure=None, analog_gain=None,
                      preset=None, crf=None, lossless=None, eye_only=None,
                      python=sys.executable):
    """Runs this daemon as a child process and returns its Popen handle.

    Stop it with stop_subprocess(proc) — or a terminal Ctrl+C, which the child
    shares — so the mp4 is finalized cleanly.
    """
    cmd = [
        python, os.path.abspath(__file__),
        "-o", output_path, "-s", stamp_path, "--fps", str(fps),
    ]
    if exposure is not None:
        cmd += ["--exposure", str(exposure)]
    if analog_gain is not None:
        cmd += ["--gain", str(analog_gain)]
    if preset is not None:
        cmd += ["--preset", preset]
    if crf is not None:
        cmd += ["--crf", str(crf)]
    if lossless is not None:
        cmd += ["--lossless" if lossless else "--no-lossless"]
    if eye_only is not None:
        cmd += ["--eye-only" if eye_only else "--no-eye-only"]
    return subprocess.Popen(cmd)

def stop_subprocess(proc, timeout=10.0):
    """Gracefully stops a launch_subprocess() child: SIGINT triggers its normal
    save/cleanup path; escalates to terminate/kill only if it hangs."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

def main(argv=None):
    parser = argparse.ArgumentParser(description="Steam Frame face/eye camera recorder")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT, help="output mp4 path")
    parser.add_argument("-s", "--stamps", default=DEFAULT_STAMPS, help="timestamp db path")
    parser.add_argument("--fps", type=float, default=120.0, help="capture/encode frame rate")
    parser.add_argument("--exposure", type=int, default=None,
                        help="sensor exposure (1..434); omit to leave unchanged")
    parser.add_argument("--gain", type=int, default=None,
                        help="sensor analogue gain (1..1023); omit to leave unchanged")
    parser.add_argument("--preset", default="ultrafast",
                        help="x264 preset (default: ultrafast; lossless runs ~12x realtime)")
    parser.add_argument("--crf", type=int, default=6,
                        help="x264 CRF, lower = higher quality (default: 6; x264's own default is 23)")
    parser.add_argument("--lossless", action=argparse.BooleanOptionalAction, default=True,
                        help="pixel-perfect encode (-qp 0, default: on for bootstrap "
                             "training data). Produces High 4:4:4 profile — software "
                             "decode only; hardware decoders show garbage.")
    parser.add_argument("--eye-only", action=argparse.BooleanOptionalAction, default=None,
                        help="capture only the internal eye cameras. Default: auto — "
                             "eye-only when no mouth tracker is detected on the lower "
                             "port. --no-eye-only demands the face cameras and errors "
                             "if the tracker is absent")
    args = parser.parse_args(argv)

    # Keep the whole capture stack off the A520 cores (0-1): they lack some
    # ISA extensions and are reserved for system responsiveness.
    try:
        os.sched_setaffinity(0, set(range(2, 8)))
    except OSError:
        pass

    daemon = RecordingDaemon(args.output, args.stamps, fps=args.fps,
                             exposure=args.exposure, analog_gain=args.gain,
                             preset=args.preset, crf=args.crf, lossless=args.lossless,
                             eye_only=args.eye_only)
    try:
        daemon.start()
    except Exception as e:
        print(f"Could not start recording: {e}")
        sys.exit(1)

    print("Recording. Press Ctrl+C to stop and save...")
    try:
        while daemon.running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nStopping capture...")
    finally:
        daemon.stop()


if __name__ == "__main__":
    main()
