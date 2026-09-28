#!/usr/bin/env python3
"""Standalone bootstrap + runtime control for the Steam Frame face cameras.

Replicates the camera-init work the control web UI (webui_app.py) performs, so
scripts can record without ever opening it:

  1. Loads the OV6211 stereo sensor kernel module (same parameters as the UI).
  2. Routes both camera pipelines via driver2/scripts/route-lower-dual-vcint.sh,
     which also checks the FPGA VCINT state and writes the pad-level media-ctl
     formats (an inline fallback replicates this if the script is missing).
  3. Applies the capture format on both video nodes.
  4. Sets the sensor controls — exposure and analogue gain by default.

Library use:
    import camera_setup
    camera_setup.ensure_cameras_ready(exposure=210, analog_gain=160)
    camera_setup.set_exposure(300)       # dynamic, any time the module is up
    camera_setup.set_analog_gain(400)

CLI use:
    python camera_setup.py --status                 # show current values
    python camera_setup.py --exposure 300 --gain 400
    python camera_setup.py --init                   # full bootstrap
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

#MODULE_DIR = Path("/home/steamos/driver2")
MODULE_NAME = "steamframe_ov6211_stereo_subdev"
# Same parameters the web UI passes to insmod
MODULE_PARAMS = [
    "auto_setup_media_links=0",
    "primary_csiphy_entity_name=msm_csiphy3",
    "secondary_csiphy_entity_name=msm_csiphy5",
    "primary_csid_entity_name=msm_csid3",
    "secondary_csid_entity_name=msm_csid4",
    "primary_csid_source_pad=1",
    "secondary_csid_source_pad=1",
    "primary_rdi_entity_name=msm_vfe3_rdi0",
    "secondary_rdi_entity_name=msm_vfe4_rdi0",
    "primary_csid_base=0x0accb000",
    "secondary_csid_base=0x0acd0000",
]
ROUTE_SCRIPT = "./route-lower-dual-vcint.sh"

VIDEO_NODES = ("/dev/video9", "/dev/video13")
WIDTH, HEIGHT, PIXEL_FORMAT = 400, 400, "BA81"
PAD_FORMAT = f"SBGGR8_1X8/{WIDTH}x{HEIGHT} field:none"

EXPOSURE_CONTROL = "exposure"          # 1..434 lines, driver default 210
ANALOG_GAIN_CONTROL = "analogue_gain"  # 1..1023, driver default 160


class CameraSetupError(RuntimeError):
    pass


def _run(args, check=True, timeout=8.0):
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise CameraSetupError(f"command timed out: {' '.join(args)}") from exc
    if check and proc.returncode:
        detail = proc.stderr.decode("utf-8", "replace").strip() or \
                 proc.stdout.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
        raise CameraSetupError(f"{' '.join(args)} failed: {detail}")
    return proc


FPGA_READ_REG = "/usr/lib/deckard-fpga/read_reg"
FPGA_VCINT_PRESENT = "0x00021211"  # lower port in VCINT mode with accessory

def _fpga_reports_vcint():
    """Reads the FPGA lower-port state. Returns True/False, or None when the
    tool is unavailable. NOTE: software can set VCINT in anticipation of
    cameras that never appear — a True here is necessary, not sufficient."""
    if not (os.path.exists(FPGA_READ_REG) and os.access(FPGA_READ_REG, os.X_OK)):
        return None
    try:
        proc = _run([FPGA_READ_REG, "0x12"], check=False, timeout=4.0)
        out = proc.stdout.decode("utf-8", "replace").strip()
        if out:
            return out.split()[-1] == FPGA_VCINT_PRESENT
    except CameraSetupError:
        pass
    return None

def _grab_one_frame(node, timeout_s):
    """Pulls a single frame from a video node. The only real proof a feed is
    alive — everything above it (registers, subdevs) can lie."""
    try:
        proc = subprocess.run(
            ["v4l2-ctl", "-d", node, "--stream-mmap", "--stream-count=1",
             "--stream-to=/dev/null"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout_s)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        return False

def mouth_tracker_present(verify_stream=False, stream_timeout=3.0):
    """Reports if the lower-port mouth-tracker accessory is attached and
    usable. Layered so no single lying source decides:

    1. The OV6211 subdev must exist — the sensor answered on i2c when the
       module probed. (No subdev = no module or no sensor: not usable now.)
    2. If readable, the FPGA register must show VCINT camera mode.
    3. With verify_stream=True (use before recording or a mouth routine):
       routes the pipelines and pulls ONE real frame from the primary face
       node, with a timeout. VCINT can be set in software with no cameras
       behind it; only an arriving frame proves the feed.
    """
    if find_sensor_subdev() is None:
        return False
    if _fpga_reports_vcint() is False:
        return False
    if not verify_stream:
        return True
    try:
        route_cameras(True)
        apply_video_formats()
    except CameraSetupError:
        return False
    if _grab_one_frame(VIDEO_NODES[0], stream_timeout):
        return True
    try:
        route_cameras(False)   # leave the media graph as we found it
    except CameraSetupError:
        pass
    return False

def module_loaded():
    proc = _run(["lsmod"], check=False)
    needle = MODULE_NAME.encode()
    return any(line.split(maxsplit=1)[0] == needle for line in proc.stdout.splitlines())


def load_module():
    """Loads the sensor module if needed. insmod requires root; without it a
    clear error explains the one-time manual command to run."""
    if module_loaded():
        return
    print("Please load stereo subdev kernel module first!")
    quit()


def find_sensor_subdev():
    """Finds the OV6211 control subdev (primary preferred, like the web UI)."""
    root = Path("/sys/class/video4linux")
    fallbacks = []
    for entry in sorted(root.glob("v4l-subdev*")):
        try:
            name = (entry / "name").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        dev = f"/dev/{entry.name}"
        if "sf-ov6211-pri" in name or "steamframe-ov6211-primary" in name:
            return dev
        if "ov6211" in name:
            fallbacks.append(dev)
    return fallbacks[0] if fallbacks else None


def _require_subdev():
    subdev = find_sensor_subdev()
    if not subdev:
        raise CameraSetupError("OV6211 sensor subdev not found; is the module loaded?")
    return subdev


def get_control(name):
    subdev = _require_subdev()
    proc = _run(["v4l2-ctl", "-d", subdev, f"--get-ctrl={name}"])
    line = proc.stdout.decode("utf-8", "replace").strip()
    match = re.search(r"^([a-zA-Z0-9_]+):\s*(-?\d+)", line)
    if not match:
        raise CameraSetupError(f"could not parse control value: {line!r}")
    return int(match.group(2))


def set_control(name, value):
    """Sets a sensor control and returns the read-back value."""
    subdev = _require_subdev()
    _run(["v4l2-ctl", "-d", subdev, f"--set-ctrl={name}={int(value)}"])
    return get_control(name)


def set_exposure(value):
    """Sets sensor exposure (1..434 lines at 400x400)."""
    return set_control(EXPOSURE_CONTROL, value)


def set_analog_gain(value):
    """Sets sensor analogue gain (1..1023)."""
    return set_control(ANALOG_GAIN_CONTROL, value)


def route_cameras(enable=True):
    """Routes both camera pipelines. Prefers the driver helper script (FPGA
    VCINT check + pad formats), mirroring the web UI; falls back to inline
    media-ctl commands that replicate it."""
    if os.path.exists(ROUTE_SCRIPT):
        _run([str(ROUTE_SCRIPT), "on" if enable else "off"], timeout=15.0)
        return

    links = [
        '"sf-ov6211-pri 0-0061":0 -> "msm_csiphy3":0',
        '"msm_csiphy3":1 -> "msm_csid3":0',
        '"msm_csid3":1 -> "msm_vfe3_rdi0":0',
        '"sf-ov6211-sec 0-0060":0 -> "msm_csiphy5":0',
        '"msm_csiphy5":1 -> "msm_csid4":0',
        '"msm_csid4":1 -> "msm_vfe4_rdi0":0',
    ]
    flag = "[1]" if enable else "[0]"
    for link in links:
        _run(["media-ctl", "-d", "/dev/media0", "-l", f"{link} {flag}"], check=enable)
    if not enable:
        return
    pads = [
        '"sf-ov6211-pri 0-0061":0', '"msm_csiphy3":0', '"msm_csiphy3":1',
        '"msm_csid3":0', '"msm_csid3":1', '"msm_vfe3_rdi0":0',
        '"sf-ov6211-sec 0-0060":0', '"msm_csiphy5":0', '"msm_csiphy5":1',
        '"msm_csid4":0', '"msm_csid4":1', '"msm_vfe4_rdi0":0',
    ]
    for pad in pads:
        _run(["media-ctl", "-d", "/dev/media0", "-V", f"{pad} [fmt:{PAD_FORMAT}]"],
             check=False)


def apply_video_formats():
    for node in VIDEO_NODES:
        _run([
            "v4l2-ctl", "-d", node,
            f"--set-fmt-video=width={WIDTH},height={HEIGHT},pixelformat={PIXEL_FORMAT}",
        ], check=False)


def ensure_cameras_ready(exposure=None, analog_gain=None, extra_controls=None):
    """Full bootstrap: module + routing + formats + controls. Replaces the
    'open the web UI once' step. Pass None to leave a control untouched.
    Returns the sensor subdev path."""
    load_module()

    # The subdev can take a moment to appear after insmod
    deadline = time.monotonic() + 2.0
    while find_sensor_subdev() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    subdev = _require_subdev()

    route_cameras(True)
    apply_video_formats()

    if exposure is not None:
        set_exposure(exposure)
    if analog_gain is not None:
        set_analog_gain(analog_gain)
    for name, value in (extra_controls or {}).items():
        set_control(name, value)
    return subdev


def status():
    subdev = find_sensor_subdev()
    result = {"module_loaded": module_loaded(), "subdev": subdev}
    if subdev:
        result["exposure"] = get_control(EXPOSURE_CONTROL)
        result["analog_gain"] = get_control(ANALOG_GAIN_CONTROL)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Steam Frame face camera setup/control")
    parser.add_argument("--init", action="store_true",
                        help="full bootstrap (module, routing, formats, controls)")
    parser.add_argument("--exposure", type=int, help="set exposure (1..434)")
    parser.add_argument("--gain", type=int, help="set analogue gain (1..1023)")
    parser.add_argument("--status", action="store_true", help="print current state")
    args = parser.parse_args(argv)

    try:
        if args.init:
            subdev = ensure_cameras_ready(exposure=args.exposure, analog_gain=args.gain)
            print(f"Cameras ready (subdev {subdev})")
        else:
            if args.exposure is not None:
                print(f"exposure = {set_exposure(args.exposure)}")
            if args.gain is not None:
                print(f"analog_gain = {set_analog_gain(args.gain)}")
        if args.status or not (args.init or args.exposure is not None or args.gain is not None):
            print(status())
    except CameraSetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
