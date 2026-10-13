"""
Stand-in for Babble Frame Runtime's capture control socket (protocol 2, see
HANDOFF.md section 3), for testing the app without the runtime or cameras.
It records nothing: it validates requests, prints them, and appends every
message it receives to a JSONL log.

    python tools/mock_runtime.py                       # default socket path
    python tools/mock_runtime.py --no-mouth --display-hz 72
    python tools/mock_runtime.py --display-hz 120 --session-display-hz 72
    python tools/mock_runtime.py --runtime-stop-after 20 --stop-status failed
    BABBLE_CAPTURE_SOCKET=/tmp/x.sock python tools/mock_runtime.py

Then run the app with the same BABBLE_CAPTURE_SOCKET (or the default path).
"""

import os
import sys
import json
import time
import signal
import socket
import argparse
import threading

PROTOCOL = 2
SEGMENT_PHASES = {"practice", "hold", "relax", "alternate_on", "alternate_off", "warmup", "freeform"}


def default_socket_path():
    path = os.environ.get("BABBLE_CAPTURE_SOCKET")
    if path:
        return path
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime_dir, "babble-frame-runtime", "capture.sock")


class MockRuntime:
    """Serves one client at a time on a Unix socket. Options mirror the
    situations the app must handle: no mouth source, no recorder, no upload
    service, a protocol mismatch, a low display refresh rate, the app's XR
    session changing the display rate after hello (session_display_hz: what
    "status" reports from then on), a slow "stopped" (stop_delay seconds,
    the encoders closing), a session that fails (stop_status "failed"), the
    runtime stopping a recording on its own (runtime_stop_after seconds in:
    an id-less "stopped"), and a source dropping out mid-session
    (lose_source_after seconds into a recording)."""

    def __init__(self, path, protocol=PROTOCOL, mouth_kind="usb", recording=True,
                 upload=True, display_hz=144, session_display_hz=None, stop_delay=0.0,
                 stop_status="complete", runtime_stop_after=None, lose_source_after=None, status_interval=1.0, log_path=None, verbose=True):
        self.path = path
        self.protocol = protocol
        self.mouth_kind = mouth_kind
        self.recording_available = recording
        self.upload_available = upload
        self.display_hz = display_hz
        self.session_display_hz = session_display_hz
        self.stop_delay = stop_delay
        self.stop_status = stop_status
        self.runtime_stop_after = runtime_stop_after
        self._greeted = False
        self.lose_source_after = lose_source_after
        self.status_interval = status_interval
        self.log_path = log_path
        self.verbose = verbose
        self.received = []        # every message received, in order
        self.segments = []        # every accepted segment message
        self.sessions = []        # finished sessions: {session, routine, routine_def, consent, segments, status}
        self.upload_queue = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self._stop = threading.Event()
        self._session = None
        self._eye_state = "Streaming"
        self._upload_progress = 0.0

    # --- lifecycle ----------------------------------------------------------

    def start(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(1)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, name="mock-runtime", daemon=True)
        self._thread.start()
        return self

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
        if self._server is not None:
            self._server.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    # --- serving ------------------------------------------------------------

    def _log(self, text):
        if self.verbose:
            print(f"[mock-runtime] {text}", flush=True)

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._log("client connected")
            self._handle(conn)
            self._log("client disconnected")

    def _sources(self):
        mouth = None if self.mouth_kind is None else {
            "kind": self.mouth_kind,
            "state": "Streaming" if self._session else "Waiting"}
        display_hz = self.display_hz
        if self._greeted and self.session_display_hz is not None:
            display_hz = self.session_display_hz
        # SteamVR: the eye cameras follow the display rate, capped at 90 fps
        return {"eye": {"state": self._eye_state, "camera_fps": min(display_hz, 90),
                        "display_hz": display_hz},
                "mouth": mouth}

    def _status(self, request_id=None):
        message = {"op": "status", "recording": self._session is not None,
                   "session": self._session["session"] if self._session else None,
                   "elapsed_ns": (time.monotonic_ns() - self._session["started_ns"])
                   if self._session else 0,
                   "sources": self._sources(),
                   "uploads": {"pending": len(self.upload_queue),
                               "active": self.upload_queue[0] if self.upload_queue else None,
                               "stage": "uploading" if self.upload_queue else None,
                               "progress": self._upload_progress}}
        if request_id is not None:
            message["id"] = request_id
        return message

    def _handle(self, conn):
        conn.settimeout(0.1)
        buffer = b""
        greeted = self._greeted = False
        last_status = time.monotonic()
        send_lock = threading.Lock()

        def send(message):
            with send_lock:
                conn.sendall((json.dumps(message) + "\n").encode("utf-8"))

        def error(request_id, code, text):
            message = {"op": "error", "code": code, "message": text}
            if request_id is not None:
                message["id"] = request_id
            send(message)

        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                except socket.timeout:
                    pass
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        error(None, "bad_request", "malformed JSON line")
                        continue
                    self._record(message)
                    request_id = message.get("id")
                    op = message.get("op")
                    if not greeted:
                        if op != "hello":
                            error(request_id, "protocol", "hello must come first")
                            return
                        if message.get("protocol") != self.protocol:
                            error(request_id, "protocol",
                                  f"runtime speaks protocol {self.protocol}")
                            return
                        greeted = True
                        send({"op": "hello", "id": request_id, "protocol": self.protocol,
                              "runtime": "mock", "profile": "release",
                              "recording": self.recording_available,
                              "upload": self.upload_available,
                              "sources": self._sources()})
                        self._greeted = True
                        continue
                    self._handle_op(op, message, request_id, send, error)
                now = time.monotonic()
                if self._session is not None and self.lose_source_after is not None:
                    if now - self._session["started_mono"] >= self.lose_source_after:
                        self._eye_state = "Lost"
                if self._session is not None and self.runtime_stop_after is not None:
                    if now - self._session["started_mono"] >= self.runtime_stop_after:
                        send(self._stopped(None, "disk: less than 1 GB free"))
                if self.upload_queue:
                    self._upload_progress = min(1.0, self._upload_progress + 0.02)
                    if self._upload_progress >= 1.0:
                        self._log(f"upload finished: {self.upload_queue.pop(0)}")
                        self._upload_progress = 0.0
                if ((self._session is not None or self.upload_queue)
                        and now - last_status >= self.status_interval):
                    last_status = now
                    send(self._status())
        except OSError as exc:
            self._log(f"connection error: {exc}")
        finally:
            if self._session is not None:
                self._log("socket dropped while recording: finalising session")
                self._finish_session("the app disconnected")
            conn.close()

    def _handle_op(self, op, message, request_id, send, error):
        if op == "start":
            if not self.recording_available:
                return error(request_id, "unavailable", "recorder unavailable")
            if self._session is not None:
                return error(request_id, "busy", "a recording is already running")
            routine = message.get("routine")
            if routine not in ("eye", "mouth"):
                return error(request_id, "bad_request", f"unknown routine {routine!r}")
            if routine == "mouth" and self.mouth_kind is None:
                return error(request_id, "no_source", "no mouth source")
            routine_def = message.get("routine_def")
            if not isinstance(routine_def, dict) or not isinstance(routine_def.get("steps"), list):
                return error(request_id, "bad_request", "start needs routine_def with steps")
            session = time.strftime("%Y%m%d-%H%M%S")
            self._eye_state = "Streaming"
            self._session = {"session": session, "routine": routine, "routine_def": routine_def,
                             "consent": message.get("consent"), "meta": message.get("meta"),
                             "segments": [], "started_ns": time.monotonic_ns(),
                             "started_mono": time.monotonic()}
            self._log(f"started {session} routine={routine} consent={message.get('consent')}")
            send({"op": "started", "id": request_id, "session": session,
                  "dir": f"/tmp/mock-captures/{session}"})
        elif op == "segment":
            if self._session is None:
                return error(None, "bad_request", "segment while not recording")
            try:
                label = str(message["label"])
                start_ns, end_ns = int(message["start_ns"]), int(message["end_ns"])
            except (KeyError, TypeError, ValueError):
                return error(None, "bad_request", "segment needs label/start_ns/end_ns")
            if end_ns < start_ns:
                return error(None, "bad_request", "segment ends before it starts")
            step, phase = message.get("step"), message.get("phase")
            steps = self._session["routine_def"]["steps"]
            if (not isinstance(step, int) or isinstance(step, bool)
                    or not -1 <= step < len(steps)):
                return error(None, "bad_request", f"segment step {step!r} out of range")
            if phase not in SEGMENT_PHASES:
                return error(None, "bad_request", f"unknown segment phase {phase!r}")
            self._session["segments"].append((label, start_ns, end_ns, step, phase))
            self.segments.append(message)
            self._log(f"segment {label} {(end_ns - start_ns) / 1e9:.2f}s step={step} {phase}")
        elif op == "stop":
            if self._session is None:
                return error(request_id, "bad_request", "not recording")
            if self.stop_delay:
                time.sleep(self.stop_delay)  # closing the encoders
            send(self._stopped(request_id, "stopped by the app"))
        elif op == "status":
            send(self._status(request_id))
        elif op == "upload":
            if not self.upload_available:
                return error(request_id, "unavailable", "no upload service configured")
            session = next((s for s in self.sessions
                            if s["session"] == message.get("session")), None)
            if session is None:
                return error(request_id, "bad_request", "unknown session")
            if not session["consent"]:
                return error(request_id, "bad_request", "session has no consent")
            if session["status"] != "complete":
                return error(request_id, "bad_request", "session is not complete")
            self.upload_queue.append(session["session"])
            send({"op": "upload_queued", "id": request_id, "session": session["session"]})
        else:
            error(request_id, "bad_request", f"unknown op {op!r}")

    def _stopped(self, request_id, reason):
        """Ends the session; the "stopped" message (id-less when the runtime
        stopped on its own)."""
        session = self._finish_session(reason)
        message = {"op": "stopped", "session": session["session"], "status": session["status"],
                   "reason": reason, "frames": {"eye": 90 * 60, "mouth": 60 * 60},
                   "dropped": {"eye": 0, "mouth": 0}}
        if session["status"] != "complete":
            message["error"] = "encoder failed"
        if request_id is not None:
            message["id"] = request_id
        return message

    def _finish_session(self, reason):
        session, self._session = self._session, None
        session["status"] = self.stop_status
        session["reason"] = reason
        with self._lock:
            self.sessions.append(session)
        self._log(f"stopped {session['session']}: {len(session['segments'])} segments")
        return session

    def _record(self, message):
        with self._lock:
            self.received.append(message)
        if self.log_path:
            with open(self.log_path, "a") as log:
                log.write(json.dumps(message) + "\n")


def _raise_interrupt(*_):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--socket", default=default_socket_path())
    parser.add_argument("--protocol", type=int, default=PROTOCOL)
    parser.add_argument("--mouth", choices=("usb", "frunk", "none"), default="usb")
    parser.add_argument("--no-mouth", dest="mouth", action="store_const", const="none")
    parser.add_argument("--no-recording", action="store_true")
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--display-hz", type=int, default=144)
    parser.add_argument("--session-display-hz", type=int, default=None,
                        help="display rate status reports after hello (the app's XR session changed it)")
    parser.add_argument("--stop-delay", type=float, default=0.0,
                        help="seconds before replying \"stopped\"")
    parser.add_argument("--stop-status", choices=("complete", "failed"), default="complete")
    parser.add_argument("--runtime-stop-after", type=float, default=None,
                        help="stop each recording this many seconds in, as the runtime would on low disk")
    parser.add_argument("--lose-source-after", type=float, default=None,
                        help="mark the eye source lost this many seconds into a recording")
    parser.add_argument("--log", default="mock_runtime_log.jsonl")
    args = parser.parse_args()
    runtime = MockRuntime(args.socket, protocol=args.protocol,
                          mouth_kind=None if args.mouth == "none" else args.mouth,
                          recording=not args.no_recording, upload=not args.no_upload,
                          display_hz=args.display_hz,
                          session_display_hz=args.session_display_hz,
                          stop_delay=args.stop_delay,
                          stop_status=args.stop_status,
                          runtime_stop_after=args.runtime_stop_after,
                          lose_source_after=args.lose_source_after,
                          log_path=args.log).start()
    print(f"[mock-runtime] listening on {args.socket}", flush=True)
    # SIGTERM (e.g. pkill) also cleans up the socket file
    signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
