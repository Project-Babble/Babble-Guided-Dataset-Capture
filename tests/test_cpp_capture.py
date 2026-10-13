"""The C++ Recorder (build/capture_test) against tools/mock_runtime.py.

    cmake -S . -B build && cmake --build build && python -m unittest tests.test_cpp_capture
Skipped when build/capture_test hasn't been built."""

import os
import sys
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
BINARY = os.path.join(ROOT, "build", "capture_test")

from mock_runtime import MockRuntime  # noqa: E402


def short_socket_path():
    # sun_path is limited to 108 bytes; temp dirs can be longer
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, f"bdc-test-{os.getpid()}.sock")


@unittest.skipUnless(os.path.isfile(BINARY), "build/capture_test not built")
class CppCaptureTest(unittest.TestCase):
    def setUp(self):
        self.path = short_socket_path()
        self.runtime = None

    def tearDown(self):
        if self.runtime is not None:
            self.runtime.close()

    def run_client(self, *args):
        env = dict(os.environ, BABBLE_CAPTURE_SOCKET=self.path)
        return subprocess.run([BINARY, *args], env=env, capture_output=True, text=True, timeout=30)

    def test_full_session(self):
        self.runtime = MockRuntime(self.path, verbose=False, mouth_kind=None, display_hz=72).start()
        result = self.run_client()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("connect: ok mouth=0 display_hz=72 upload=1", result.stdout)
        session = self.runtime.sessions[-1]
        self.assertEqual([s[0] for s in session["segments"]], ["Shape0", "Shape1", "Shape2"])
        self.assertEqual([(s[3], s[4]) for s in session["segments"]], [(0, "hold"), (1, "hold"), (2, "hold")])
        self.assertEqual(session["routine_def"]["app_version"], "test")
        self.assertEqual(session["consent"]["version"], "test")
        self.assertEqual(self.runtime.upload_queue[-1], session["session"])
        hello = self.runtime.received[0]
        self.assertEqual((hello["op"], hello["protocol"], hello["app"]), ("hello", 2, "babble-dataset-capture"))

    def test_rates_from_status_and_slow_stop(self):
        # hello predates the app's XR session; status has the live rate. The
        # runtime may take a while to close its encoders before "stopped".
        self.runtime = MockRuntime(self.path, verbose=False, display_hz=120,
                                   session_display_hz=72, stop_delay=6.0).start()
        result = self.run_client()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("connect: ok mouth=1 display_hz=120", result.stdout)
        self.assertIn("status: display_hz=72 camera_fps=72", result.stdout)
        self.assertIn("stop: ok", result.stdout)
        self.assertEqual(self.runtime.upload_queue[-1], self.runtime.sessions[-1]["session"])

    def test_runtime_stops_on_its_own(self):
        self.runtime = MockRuntime(self.path, verbose=False, runtime_stop_after=0.3).start()
        result = self.run_client("runtime-stop")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("runtime stop: recording=0", result.stdout)
        self.assertIn("complete=1 reason=disk", result.stdout)
        self.assertEqual(self.runtime.upload_queue[-1], self.runtime.sessions[-1]["session"])

    def test_failed_session_not_uploaded(self):
        self.runtime = MockRuntime(self.path, verbose=False, stop_status="failed").start()
        result = self.run_client()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("complete=0 reason=stopped by the app: encoder failed", result.stdout)
        self.assertEqual(self.runtime.upload_queue, [])

    def test_protocol_mismatch(self):
        self.runtime = MockRuntime(self.path, verbose=False, protocol=1).start()
        result = self.run_client()
        self.assertEqual(result.returncode, 3)
        self.assertIn("don't match", result.stdout)

    def test_no_runtime(self):
        result = self.run_client()
        self.assertEqual(result.returncode, 3)
        self.assertIn("Can't reach Babble Frame Runtime", result.stdout)


if __name__ == "__main__":
    unittest.main()
