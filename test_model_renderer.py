"""
GLFW_PLATFORM=x11 DISPLAY=:0 MESA_LOADER_DRIVER_OVERRIDE=zink python test_model_renderer.py
"""


import os
import glob
import json
import math
import time
import random
import atexit
import ctypes
import shutil
import struct
import logging
import threading
import traceback
import subprocess
import numpy as np
import glfw
import OpenGL
# The OpenXR runtime (including SteamVR's foveation injection layer) shares our
# GL context and can leave stale GL error flags. PyOpenGL checks glGetError()
# after every call and would misattribute those flags to our next call, crashing
# at random points. Disabling the per-call check fixes that and also removes
# Python overhead from every GL call. Shader compile/link keep explicit checks.
OpenGL.ERROR_CHECKING = False
import OpenGL.GL as gl
import xr
from pyrr import Matrix44, Quaternion
from PIL import Image, ImageDraw, ImageFont
import pywavefront

# Silence pywavefront's per-statement warnings (e.g. smoothing groups "s 1",
# which Blender writes into every OBJ but which have no effect on our rendering)
logging.getLogger("pywavefront").setLevel(logging.ERROR)

# XrSpaceLocationFlags bits (spec values, used to validate controller poses)
XR_SPACE_LOCATION_ORIENTATION_VALID_BIT = 0x1
XR_SPACE_LOCATION_POSITION_VALID_BIT = 0x2

HAND_LEFT = 0
HAND_RIGHT = 1
HAND_NAMES = ("left", "right")

# Morphable head model, exported from Blender via blender_export_blendshapes.py
BLENDSHAPE_DIR = "blendshapes"
BLENDSHAPE_BASIS = "basis.obj"
MORPH_CACHE_FILE = "_morph_cache.npz"
DEMO_ANIMATE_BLENDSHAPES = True  # Sweep through each blendshape for testing

# Normal/metallic/AO maps extracted from the Unity asset (extract_unity_materials.py)
MATERIAL_MAPS_FILE = "material_maps.json"
NORMAL_MAP_MAX_SIZE = 2048  # Downsample larger maps at load to save GPU memory
PARAM_MAP_MAX_SIZE = 1024   # Metallic/AO are low-frequency; 1k is plenty

# 3D UI (ConfirmationBox) appearance
UI_TEXT_COLOR = (235, 235, 240)
UI_LABEL_BG = (24, 26, 34)
UI_BUTTON_BG = (45, 48, 62)
UI_BUTTON_BG_HOVER = (70, 110, 190)
EXPRESSION_UI = True  # Drive blendshapes with the guided expression dialog
# Stage-space dialog position used while the model is still loading; once the
# morphable head is ready the dialog snaps to a position computed from the
# mesh itself (see dialog_position_below_chin). This fallback matches the
# value computed for the current head model.
UI_DIALOG_POSITION = (0.0, 1.07, -1.07)
DIALOG_CHIN_CLEARANCE = 0.06   # Gap (m) between chin and the below-chin label top
DIALOG_HEAD_DISTANCE = 0.25    # Dialog distance (m) in front of the face
# Text-only panes (no demonstration to watch) cover the face instead. They
# center DIALOG_COVER_DROP below the eye line: slightly-below-eye-level is the
# comfortable reading height, and it keeps the jump from the below-chin panes
# modest so the dialog isn't lost between modes. UI_DIALOG_COVER_Y is the
# fallback while the model loads; the measured value replaces it once ready.
DIALOG_COVER_DROP = 0.12
UI_DIALOG_COVER_Y = 1.50
EXPRESSION_PREP_SECONDS = 2     # "Get ready" pause between Ready and the hold
EXPRESSION_RELAX_SECONDS = 3    # Return-to-neutral capture logged after each hold
ALTERNATE_PERIOD_SECONDS = 2.0  # "alternate" steps: one expression+neutral cycle (1 s each)
# Hold / alternate / freeform lengths come from the active routine (see ROUTINES)

# Freeform stage after the guided sequence: the user moves their face every
# way they can. Logged as a single "Freeform" record — unlabelled, but highly
# diverse data (can be auto-labelled later by other systems). Prompts rotate
# to keep ideas flowing, and the head model riffs along on random blendshape
# mixes as inspiration. Duration, prompts and which shapes the model riffs on
# are per routine (see ROUTINES).
FREEFORM_PROMPT_SECONDS = 5   # How long each suggestion stays up
FREEFORM_SHUFFLE_SECONDS = 1.2  # How often the model picks a new random face
MOUTH_FREEFORM_PROMPTS = [
    "Make the silliest face you can!",
    "Chew some imaginary gum!",
    "Big yawn... then a huge grin!",
    "Puff your cheeks side to side!",
    "Silently say the alphabet!",
    "Wiggle your jaw all around!",
    "Raise one brow, then the other!",
    "Scrunch your whole face up!",
    "Look around with wide eyes!",
    "Pucker, smile, pucker, smile!",
    "Stick out your tongue and wave it!",
    "Pretend to chew a giant toffee!",
]
EYE_FREEFORM_PROMPTS = [
    "Blink as fast as you can!",
    "Wink left, then wink right!",
    "Squint hard, then go wide-eyed!",
    "Look around in every direction!",
    "Slowly close, then pop them open!",
    "Angry brows... then surprised!",
    "Raise one brow at a time!",
    "Scrunch your eyes up tight!",
]

# Onboarding: narrated intro panes shown while the head model loads in the
# background. Each pane may have a NARRATION_DIR/<name>.wav voice-over clip,
# played if present (expression names, "done", "practice_done" too). The
# "demo" pane animates the head through an example expression; the "practice"
# pane runs one rehearsal rep whose log records get a "Practice_" prefix.
ONBOARDING = True
NARRATION_DIR = "narration"
ONBOARDING_STEPS = [
    {"name": "welcome",
     "label": "Welcome, and thanks for contributing!\n"
              "Aim a laser at a button and pull\nthe trigger to press it.",
     "buttons": (("Continue", "next"), ("Skip intro", "skip"))},
    {"name": "purpose",
     "label": "You will be shown a series of facial\n"
              "expressions. Copy each one with your\n"
              "own face and hold it for a few seconds.",
     "buttons": (("Continue", "next"), ("Back", "back"))},
    {"name": "freeform_intro",
     "label": "After the guided part comes free play:\n"
              "{freeform_seconds} seconds to make every face\n"
              "you can think of. The sillier, the better!",
     "buttons": (("Continue", "next"), ("Back", "back"))},
    {"name": "consent",
     "label": "While this app is open, your headset's\n"
              "face and eye cameras are recorded to\n"
              "build a face-tracking dataset. Continue?",
     "buttons": (("I agree", "next"), ("Exit", "exit"))},
    {"name": "demo",
     "label": "The head in front of you demonstrates\n"
              "each expression, like this one.\nTry copying it with your own face!",
     "demo": True,
     "buttons": (("Continue", "next"), ("Back", "back"))},
    {"name": "practice",
     "label": "Let's practice once with: {name}\n"
              "Press Ready, make the face, and hold\nit until the countdown finishes.",
     "practice": True},
]

# Antialiasing: per-eye MSAA sample count. Smooths the silhouette edges of
# the UI pills, head model, cones and lasers against the background (and
# softens the alpha edge against passthrough). Clamped to the GPU maximum;
# falls back to no MSAA automatically if unsupported. 1 disables.
# GPU load knobs (the Frame's GPU is the app's bottleneck — see HANDOFF):
# MSAA sample count, render-target scale relative to the runtime's
# recommendation (the compositor upscales), sky shader detail, and an
# optional 5-second average FPS printout for measuring the effect.
MSAA_SAMPLES = int(os.environ.get("FRAME_EYES_MSAA", "4"))
RENDER_SCALE = float(os.environ.get("FRAME_EYES_RENDER_SCALE", "1.0"))
SKY_DETAIL = {"off": 0.0, "lite": 1.0, "full": 2.0}.get(
    os.environ.get("FRAME_EYES_SKY", "full"), 2.0)
FPS_LOG = os.environ.get("FRAME_EYES_FPS_LOG") == "1"

# Controller cone + laser pointer (buttons can be triggered from a distance)
CONE_LENGTH = 0.15   # Controller cone length (m); the laser starts at its tip
LASER_LENGTH = 2.0   # Laser beam reach (m) when not pointing at a button
LASER_WIDTH = 0.004  # Laser beam thickness (m)

# Background: a northern night sky. The official Project Babble brand colors
# (https://docs.babble.diy/docs/branding) are carried by the aurora — Babble
# Sea Green (#539E8A) in the primary curtain, Babble Pink (#F4C2C2) in the
# higher secondary curtain — dancing over a deep night-teal sky with
# twinkling stars above a black horizon. RGB 0..1.
BACKGROUND_TOP_COLOR = (0.027, 0.078, 0.094)      # deep night teal
BACKGROUND_HORIZON_COLOR = (0.008, 0.012, 0.014)  # near-black horizon
BACKGROUND_FLOOR_COLOR = (0.055, 0.094, 0.086)    # only seen w/o passthrough
AURORA_COLOR_PRIMARY = (0.326, 0.870, 0.660)      # Babble Sea Green, glowing
AURORA_COLOR_SECONDARY = (0.990, 0.640, 0.700)    # Babble Pink, glowing
AURORA_STRENGTH = 0.45  # overall curtain brightness; 0.0 disables the aurora
AURORA_SPEED = 1.6      # animation rate multiplier (1.0 = original, slower)
STAR_STRENGTH = 0.65    # star brightness; 0.0 disables the stars

# Fade the lower half of the world into the headset's passthrough cameras so
# users keep sight of their own body and the real floor. Implemented with the
# OpenXR ALPHA_BLEND environment blend mode: the compositor shows the cameras
# wherever our framebuffer alpha is below 1. Falls back to an opaque floor
# color when the runtime doesn't offer ALPHA_BLEND.
PASSTHROUGH_LOWER_WORLD = True
PASSTHROUGH_FULL_BELOW_DEG = -35.0  # fully passthrough at/below this view elevation
PASSTHROUGH_FADE_TOP_DEG = -8.0     # fully virtual world at/above this elevation

# Calibration routine presented by the guided flow, in this order. Unlike a
# plain list of single blendshapes, each step is a *pose*: one or more shape
# coefficients held together. The combinations and exact weights come from
# labelled capture data (see new_routine.py). Each step carries:
#   "display" - friendly instruction shown in the headset UI
#   "log"     - short, stable id written to the _expressions.bin hold log
#   "pose"    - {coefficient_name: weight}, the exact weights to drive
#
# Pose coefficient names use the source (VRCFT-style unified-expression) naming.
# Most map 1:1 onto this model's blendshape OBJ stems; the only exceptions are
# the bipolar smile/frown axes, which this model has as separate one-directional
# shapes (see BIPOLAR_SHAPES / resolve_pose). A step whose resolved shapes are
# not all present in the loaded set is skipped with a warning.
#
# Optional per-step keys:
#   "hold"       - hold length in seconds (default: the routine's hold_seconds)
#   "hold_label" - instruction shown during the hold (default "Hold that expression!")
#   "mode"       - "alternate": instead of a hold, the wearer alternates the
#                  expression and neutral in time with the head model for
#                  "seconds" (default: the routine's alternate_seconds), one
#                  cycle every ALTERNATE_PERIOD_SECONDS. Each cued segment is
#                  logged separately: on-segments under the step's log id,
#                  off-segments as "Neutral_alt". "cue_on"/"cue_off" are the
#                  labels shown for the two halves of the cycle.
#
# Set a routine's steps to None to fall back to sweeping every loaded
# blendshape alphabetically, one at a time.
MOUTH_ROUTINE = [
    {"display": "Open your mouth wide", "log": "jaw_open",
     "pose": {"JawOpen": 1.0}},
    {"display": "Drop your jaw, lips together", "log": "jaw_open_lips_closed",
     "pose": {"MouthClosed": 1.0, "JawOpen": 1.0}},
    {"display": "Open mouth, raise your upper lip", "log": "open_upper_lip_raise",
     "pose": {"MouthUpperUpLeft": 1.0, "MouthUpperUpRight": 1.0, "JawOpen": 1.0}},
    {"display": "Raise your upper lip, mouth ajar", "log": "ajar_upper_lip_raise",
     "pose": {"MouthUpperUpLeft": 1.0, "MouthUpperUpRight": 1.0,
              "JawOpen": 0.4629859661233837}},
    {"display": "Bare your teeth, mouth ajar", "log": "ajar_both_lips_apart",
     "pose": {"MouthLowerDown": 1.0, "MouthUpperUpLeft": 1.0,
              "MouthUpperUpRight": 1.0, "JawOpen": 0.4629859661233837}},
    {"display": "Bare your teeth", "log": "both_lips_apart",
     "pose": {"MouthLowerDown": 1.0, "MouthUpperUpLeft": 1.0,
              "MouthUpperUpRight": 1.0}},
    {"display": "Smile and bare your teeth", "log": "smile_lips_apart",
     "pose": {"SmileFrownLeft": 1.0, "SmileFrownRight": 1.0, "MouthLowerDown": 1.0,
              "MouthUpperUpLeft": 1.0, "MouthUpperUpRight": 1.0}},
    {"display": "Smile, raise your upper lip", "log": "smile_upper_lip_raise",
     "pose": {"SmileFrownLeft": 1.0, "SmileFrownRight": 1.0,
              "MouthUpperUpLeft": 1.0, "MouthUpperUpRight": 1.0}},
    {"display": "Open smile, upper lip raised", "log": "smile_open_upper_lip_raise",
     "pose": {"SmileFrownLeft": 1.0, "SmileFrownRight": 1.0, "MouthUpperUpLeft": 1.0,
              "MouthUpperUpRight": 1.0, "JawOpen": 0.6596732304014008}},
    {"display": "Big open smile", "log": "smile_open",
     "pose": {"SmileFrownLeft": 1.0, "SmileFrownRight": 1.0,
              "JawOpen": 0.6596732304014008}},
    {"display": "Smile", "log": "smile",
     "pose": {"SmileFrownLeft": 1.0, "SmileFrownRight": 1.0}},
    {"display": "Smile, right side only", "log": "smile_right",
     "pose": {"SmileFrownRight": 1.0}},
    {"display": "Smile, left side only", "log": "smile_left",
     "pose": {"SmileFrownLeft": 1.0}},
    {"display": "Press your lips together", "log": "mouth_press",
     "pose": {"MouthPress": 1.0}},
    {"display": "Pucker your lips", "log": "lip_pucker",
     "pose": {"LipPucker": 1.0}},
    {"display": "Funnel your lips", "log": "lip_funnel",
     "pose": {"LipFunnel": 1.0}},
    {"display": "Funnel your lips, mouth open", "log": "lip_funnel_open",
     "pose": {"LipFunnel": 1.0, "JawOpen": 0.6389069655845905}},
    {"display": "Suck your lips in, mouth open", "log": "lip_suck_open",
     "pose": {"LipSuckLower": 1.0, "LipSuckUpper": 1.0, "JawOpen": 0.6389069655845905}},
    {"display": "Suck your lips inward", "log": "lip_suck",
     "pose": {"LipSuckLower": 1.0, "LipSuckUpper": 1.0}},
    {"display": "Smile while puckering", "log": "smile_pucker",
     "pose": {"SmileFrownLeft": 1.0, "SmileFrownRight": 1.0, "LipPucker": 1.0}},
    {"display": "Frown, stretch lips, bare teeth", "log": "frown_stretch_lips_apart",
     "pose": {"MouthStretchRight": 0.5884875067349138, "MouthStretchLeft": 0.605639437971444,
              "SmileFrownLeft": -0.4215550915948276, "SmileFrownRight": -0.4822596056707974,
              "MouthLowerDown": 1.0, "MouthUpperUpLeft": 1.0, "MouthUpperUpRight": 1.0}},
    {"display": "Frown, stretch lips, mouth open", "log": "frown_stretch_open",
     "pose": {"MouthStretchRight": 0.5884875067349138, "MouthStretchLeft": 0.605639437971444,
              "SmileFrownLeft": -0.4215550915948276, "SmileFrownRight": -0.4822596056707974,
              "MouthLowerDown": 1.0, "MouthUpperUpLeft": 1.0, "MouthUpperUpRight": 1.0,
              "JawOpen": 0.6281382593615301}},
    {"display": "Open your mouth halfway", "log": "jaw_open_half",
     "pose": {"JawOpen": 0.5146505421605603}},
    {"display": "Pucker and funnel together", "log": "pucker_funnel",
     "pose": {"LipPucker": 0.7720553694100216, "LipFunnel": 0.45659600619612073}},
    {"display": "Puff out both cheeks", "log": "cheek_puff",
     "pose": {"CheekPuffLeft": 1.0, "CheekPuffRight": 1.0}},
    {"display": "Puff out your right cheek", "log": "cheek_puff_right",
     "pose": {"CheekPuffRight": 1.0}},
    {"display": "Puff out your left cheek", "log": "cheek_puff_left",
     "pose": {"CheekPuffLeft": 1.0}},
    {"display": "Flare your nostrils", "log": "nostril_flare",
     "pose": {"NostrilDilatorLeft": 1.0, "NostrilDilatorRight": 1.0}},
    {"display": "Shift your mouth to the right", "log": "mouth_right",
     "pose": {"MouthRight": 1.0}},
    {"display": "Shift your mouth to the left", "log": "mouth_left",
     "pose": {"MouthLeft": 1.0}},
    {"display": "Stick out your tongue", "log": "tongue_out",
     "pose": {"TongueOut": 1.0, "JawOpen": 0.45}},
    {"display": "Tongue out, point it up", "log": "tongue_up",
     "pose": {"TongueOut": 1.0, "TongueUp": 1.0, "JawOpen": 0.45}},
    {"display": "Tongue out, point it down", "log": "tongue_down",
     "pose": {"TongueOut": 1.0, "TongueDown": 1.0, "JawOpen": 0.45}},
    {"display": "Tongue out, point it right", "log": "tongue_right",
     "pose": {"TongueOut": 1.0, "TongueRight": 1.0, "JawOpen": 0.45}},
    {"display": "Tongue out, point it left", "log": "tongue_left",
     "pose": {"TongueOut": 1.0, "TongueLeft": 1.0, "JawOpen": 0.45}},
    {"display": "Push your chin up, pout your lower lip", "log": "chin_raise",
     "pose": {"MouthShrugLower": 1.0}},
]

# Eye/brow routine. For each expression: a hold while the wearer shifts the
# headset around on their face (the eye cameras see the expression from many
# offsets), then an "alternate" step toggling expression <-> neutral in time
# with the head model at a steady rate.
def _eye_steps(display, cue, log, pose, audio_invert=False, hold_label=None,
               short=None):
    # audio_invert: the cue noise normally goes OPEN while the expression is
    # held. For expressions that close the eyes that feels backwards, so the
    # noise closes with the eyes instead (and the countdown staircase runs
    # toward closed).
    # hold_label: overrides the during-hold text. Eyes-closed steps put the
    # full instructions on the pre-Ready card (readable with open eyes) and
    # keep the hold text to a glanceable shout.
    # short: compact name for panes that embed the step in a sentence (the
    # practice pane); defaults to the display text.
    return [
        {"display": display, "short": short or display,
         "log": f"{log}_shift", "pose": pose,
         "audio_invert": audio_invert,
         "hold_label": hold_label or f"{cue} — now shift the headset\naround on your face!"},
        {"display": f"Alternate: {cue.lower()}, then relax,\nin time with the head",
         "log": f"{log}_alt", "pose": pose, "mode": "alternate",
         "audio_invert": audio_invert,
         "cue_on": cue, "cue_off": "Relax — neutral face"},
    ]

EYE_ROUTINE = (
    _eye_steps("Keep your eyes closed and nudge the headset\n"
               "around until you hear the completion tone.",
               "Eyes closed", "eye_closed", {"EyeClosed": 1.0},
               audio_invert=True, hold_label="CLOSE YOUR EYES!",
               short="Close your eyes")
    + _eye_steps("Squint your eyes", "Squint", "eye_squint", {"EyeSquint": 1.0},
                 audio_invert=True)
    + _eye_steps("Open your eyes wide", "Eyes wide", "eye_wide", {"EyeWide": 1.0})
    + _eye_steps("Angry face: pull your brows down", "Brows down", "brow_down",
                 {"BrowDown": 1.0}, audio_invert=True)
)

ROUTINES = {
    "mouth": {"steps": MOUTH_ROUTINE, "needs_mouth_tracker": True,
              "hold_seconds": 3, "alternate_seconds": 10,
              "freeform_seconds": 60, "freeform_prompts": MOUTH_FREEFORM_PROMPTS,
              "freeform_shape_prefixes": None,  # model riffs on every loaded shape
              "freeform_intro": "Last step: free play!\n"
                                "Move your face every way you can think\n"
                                "of. Go wild — nothing is too silly!"},
    "eye":   {"steps": EYE_ROUTINE, "needs_mouth_tracker": False,
              "hold_seconds": 5, "alternate_seconds": 10,
              "freeform_seconds": 20, "freeform_prompts": EYE_FREEFORM_PROMPTS,
              "freeform_shape_prefixes": ("Eye", "Brow", "CheekSquint"),
              "freeform_intro": "Last step: free play!\n"
                                "Move your eyes and brows every way you\n"
                                "can think of — blink, squint, wink, stare!"},
}
ACTIVE_ROUTINE = "eye"  # which routine the guided flow runs

_routine = ROUTINES[ACTIVE_ROUTINE]
CALIBRATION_ROUTINE = _routine["steps"]
EXPRESSION_HOLD_SECONDS = _routine["hold_seconds"]
ALTERNATE_SECONDS = _routine["alternate_seconds"]
FREEFORM_SECONDS = _routine["freeform_seconds"]
FREEFORM_PROMPTS = _routine["freeform_prompts"]
FREEFORM_SHAPE_PREFIXES = _routine["freeform_shape_prefixes"]
FREEFORM_INTRO_LABEL = _routine["freeform_intro"]

# Bipolar coefficient axes this model doesn't have as a single shape. The
# routine's SmileFrown{Left,Right} reads positive for a smile and negative for
# a frown, so it resolves to one of two one-directional model shapes by sign.
BIPOLAR_SHAPES = {
    "SmileFrownLeft":  ("MouthSmileLeft", "MouthFrownLeft"),
    "SmileFrownRight": ("MouthSmileRight", "MouthFrownRight"),
}

def resolve_pose(pose):
    """Translates a routine pose's coefficient names into this model's blendshape
    weights, splitting bipolar smile/frown axes into their positive (smile) and
    negative (frown) shapes. Returns {model_shape_name: weight} (weights >= 0)."""
    resolved = {}
    for name, weight in pose.items():
        if name in BIPOLAR_SHAPES:
            smile_shape, frown_shape = BIPOLAR_SHAPES[name]
            target = smile_shape if weight >= 0 else frown_shape
            resolved[target] = resolved.get(target, 0.0) + abs(weight)
        else:
            resolved[name] = resolved.get(name, 0.0) + weight
    return resolved

# Face/eye camera recorder (final_recording_daemon.py) runs whenever the app does
ENABLE_RECORDING_DAEMON = True
RECORDING_DAEMON_IN_PROCESS = False  # False: child process (own GIL, recommended)
CAMERA_EXPOSURE = 160     # Face sensor exposure, 1..434 (None = leave unchanged)
CAMERA_ANALOG_GAIN = 1  # Face sensor analogue gain, 1..1023 (None = leave unchanged)

class SubMesh:
    """Represents a renderable partition of the OBJ model mapped to a specific material."""
    def __init__(self, vao, vbo, vertex_count, texture_id, position_vbo=None, base_positions=None):
        self.vao = vao
        self.vbo = vbo
        self.vertex_count = vertex_count
        self.texture_id = texture_id
        # Blendshape state (None/empty for static meshes)
        self.position_vbo = position_vbo
        self.base_positions = base_positions  # (N, 3) float32 neutral positions
        self.deltas = {}                      # shape name -> (N, 3) float32 offsets
        # GPU morph state (set by build_morph_texture / update_blendshapes)
        self.morph_tex = None                 # RGBA32F deltas texture
        self.morph_rows_per_shape = 0
        self.morph_slot = {}                  # shape name -> texture slot
        self.morph_active_slots = np.zeros(0, dtype=np.int32)
        self.morph_active_weights = np.zeros(0, dtype=np.float32)
        self.transparent = False  # Drawn in the blended pass after opaque geometry
        # Extra material maps + scalars (set by apply_material_maps; the GL ids
        # default to neutral 1x1 textures so the draw path never branches)
        self.material_name = None
        self.normal_tex = None
        self.metallic_tex = None
        self.ao_tex = None
        self.emission_tex = None
        self.bump_scale = 1.0
        self.metallic_factor = 0.0
        self.smoothness = 0.3
        self.occlusion_strength = 0.0
        self.emission_color = (0.0, 0.0, 0.0)

def decode_image(img_path, max_size=None):
    """CPU half of texture loading: decodes (and optionally downsizes) an
    image to raw RGBA bytes. Returns (width, height, rgba_bytes, has_alpha)
    where has_alpha is True if any texel is not fully opaque. No GL calls, so
    this is safe to run on a background thread.
    """
    img = Image.open(img_path)
    if max_size and max(img.size) > max_size:
        scale = max_size / max(img.size)
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.BILINEAR,
        )
    img = img.transpose(Image.FLIP_TOP_BOTTOM)
    rgba = img.convert("RGBA")
    has_alpha = "A" in img.getbands() and rgba.getchannel("A").getextrema()[0] < 255
    return img.width, img.height, rgba.tobytes(), has_alpha

_ANISO = None
def apply_max_anisotropy(limit=8.0):
    """Enables anisotropic filtering on the currently bound 2D texture.

    Trilinear alone picks its mip level from the STEEPEST screen-space
    derivative, so any surface viewed at an angle — the dialog text sits below
    the chin and is read at a steep downward tilt — samples a mip far blurrier
    than the horizontal detail could support. Anisotropy fixes exactly that.
    No-ops when the driver lacks the extension."""
    global _ANISO
    if _ANISO == 0.0:
        return
    try:
        from OpenGL.GL.EXT.texture_filter_anisotropic import (
            GL_TEXTURE_MAX_ANISOTROPY_EXT, GL_MAX_TEXTURE_MAX_ANISOTROPY_EXT)
        if _ANISO is None:
            _ANISO = float(gl.glGetFloatv(GL_MAX_TEXTURE_MAX_ANISOTROPY_EXT))
            print(f"Anisotropic filtering: {min(limit, _ANISO):.0f}x (max {_ANISO:.0f}x)")
        if _ANISO > 1.0:
            gl.glTexParameterf(gl.GL_TEXTURE_2D, GL_TEXTURE_MAX_ANISOTROPY_EXT,
                               min(limit, _ANISO))
    except Exception:
        _ANISO = 0.0

def upload_texture_rgba(width, height, img_data):
    """GL half of texture loading: uploads raw RGBA bytes and builds mipmaps."""
    tex_id = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
    gl.glTexImage2D(
        gl.GL_TEXTURE_2D, 0, gl.GL_RGBA,
        width, height, 0,
        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, img_data
    )
    gl.glGenerateMipmap(gl.GL_TEXTURE_2D)

    # Configure filtering for mipmapping
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
    apply_max_anisotropy()
    return tex_id

def load_texture(img_path, max_size=None):
    """Loads an image texture and uploads it to the GPU (decode + upload).

    Returns (texture_id, has_alpha); the mesh needs the transparent render
    pass when has_alpha is True.
    """
    width, height, img_data, has_alpha = decode_image(img_path, max_size)
    return upload_texture_rgba(width, height, img_data), has_alpha

def create_solid_color_texture(diffuse_color):
    """Generates a 1x1 solid texture from MTL diffuse coordinates if no image texture is mapped."""
    # diffuse_color is typically [r, g, b] or [r, g, b, a] in float format [0.0 - 1.0]
    color = list(diffuse_color)
    if len(color) == 3:
        color.append(1.0) # Add alpha channel if missing

    # Convert floats to unsigned bytes
    pixel_data = bytes([int(c * 255) for c in color])

    tex_id = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
    gl.glTexImage2D(
        gl.GL_TEXTURE_2D, 0, gl.GL_RGBA,
        1, 1, 0,
        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, pixel_data
    )
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    return tex_id

def create_interleaved_vao(vertex_data):
    """Creates a VAO/VBO for interleaved T2F_N3F_V3F data (8 floats / 32 bytes per vertex).

    Attribute layout matches the shaders: 0 = UV, 1 = normal, 2 = position.
    Returns (vao, vbo, vertex_count).
    """
    vao = gl.glGenVertexArrays(1)
    vbo = gl.glGenBuffers(1)

    gl.glBindVertexArray(vao)
    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
    gl.glBufferData(gl.GL_ARRAY_BUFFER, vertex_data.nbytes, vertex_data, gl.GL_STATIC_DRAW)

    gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(0))
    gl.glEnableVertexAttribArray(0)
    gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(8))
    gl.glEnableVertexAttribArray(1)
    gl.glVertexAttribPointer(2, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(20))
    gl.glEnableVertexAttribArray(2)

    gl.glBindVertexArray(0)
    return vao, vbo, len(vertex_data) // 8

def create_morphable_vao(vertex_data):
    """Creates a VAO for a blendshape-animated mesh.

    All vertex data is static: positions hold the neutral pose and morphing
    happens in the vertex shader (deltas texture + weight uniforms), so no
    buffer is ever re-uploaded after load.
    Returns (vao, static_vbo, position_vbo, base_positions).
    """
    vertex_data = vertex_data.reshape(-1, 8)
    base_positions = np.ascontiguousarray(vertex_data[:, 5:8])

    vao = gl.glGenVertexArrays(1)
    static_vbo = gl.glGenBuffers(1)
    position_vbo = gl.glGenBuffers(1)

    gl.glBindVertexArray(vao)
    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, static_vbo)
    gl.glBufferData(gl.GL_ARRAY_BUFFER, vertex_data.nbytes, vertex_data, gl.GL_STATIC_DRAW)
    gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(0))
    gl.glEnableVertexAttribArray(0)
    gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(8))
    gl.glEnableVertexAttribArray(1)

    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, position_vbo)
    gl.glBufferData(gl.GL_ARRAY_BUFFER, base_positions.nbytes, base_positions, gl.GL_STATIC_DRAW)
    gl.glVertexAttribPointer(2, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
    gl.glEnableVertexAttribArray(2)

    gl.glBindVertexArray(0)
    return vao, static_vbo, position_vbo, base_positions

# Most shapes an easing frame can drive at once; must match the shader arrays.
MAX_ACTIVE_MORPHS = 24

def build_morph_texture(sub_mesh):
    """Packs all of a sub-mesh's blendshape deltas into one RGBA32F texture.

    Runs once at load. Shape slot s occupies rows_per_shape texture rows;
    vertex v sits at texel (v % width, s * rows_per_shape + v / width). The
    vertex shader fetches from this layout (see VERTEX_SHADER), so per-frame
    morphing needs only weight uniforms — no vertex re-uploads.
    """
    if not sub_mesh.deltas:
        return
    names = sorted(sub_mesh.deltas)
    count = len(sub_mesh.base_positions)
    width = min(count, 4096)
    rows = (count + width - 1) // width
    data = np.zeros((len(names) * rows, width, 4), dtype=np.float32)
    for slot, name in enumerate(names):
        flat = np.zeros((rows * width, 3), dtype=np.float32)
        flat[:count] = sub_mesh.deltas[name]
        data[slot * rows:(slot + 1) * rows, :, :3] = flat.reshape(rows, width, 3)
    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA32F, width, data.shape[0], 0,
                    gl.GL_RGBA, gl.GL_FLOAT, data)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    sub_mesh.morph_tex = tex
    sub_mesh.morph_rows_per_shape = rows
    sub_mesh.morph_slot = {name: slot for slot, name in enumerate(names)}

def parse_obj_materials(obj_path):
    """Parses an OBJ into {material_name: ((N, 8) float32 T2F_N3F_V3F array, material)}."""
    scene = pywavefront.Wavefront(obj_path, collect_faces=True)
    materials = {}
    for name, material in scene.materials.items():
        # Skip materials that do not contain any geometry
        if not material.vertices:
            continue
        materials[name] = (np.array(material.vertices, dtype=np.float32).reshape(-1, 8), material)
    return materials

def create_material_texture(texture_path, diffuse):
    """Creates a GL texture for a material: image file if mapped, else solid diffuse.

    Returns (texture_id, has_alpha).
    """
    if texture_path:
        return load_texture(texture_path)
    color = list(diffuse)
    has_alpha = len(color) == 4 and color[3] < 1.0
    return create_solid_color_texture(diffuse), has_alpha

def load_multi_material_obj(obj_path):
    """Parses an OBJ file, creating separate VAO/VBOs and textures for each material group."""
    sub_meshes = []
    for name, (vertex_data, material) in parse_obj_materials(obj_path).items():
        texture_id, has_alpha = create_material_texture(
            material.texture.path if material.texture else None,
            material.diffuse,
        )
        vao, vbo, vertex_count = create_interleaved_vao(vertex_data)
        sub_mesh = SubMesh(vao, vbo, vertex_count, texture_id)
        sub_mesh.transparent = has_alpha
        sub_mesh.material_name = name
        sub_meshes.append(sub_mesh)
    return sub_meshes

def read_obj_positions(obj_path):
    """Fast path: reads only the raw 'v x y z' lines of an OBJ, in file order."""
    rows = []
    with open(obj_path) as obj_file:
        for line in obj_file:
            if line.startswith("v "):
                parts = line.split()
                rows.append((parts[1], parts[2], parts[3]))
    return np.array(rows, dtype=np.float32)

def build_corner_mapping(obj_path):
    """Builds {material: (N,) int32 raw-vertex index per expanded face corner}.

    Mirrors pywavefront's face expansion (file order, fan triangulation) so that
    raw_positions[mapping[mat]] reproduces that material's expanded vertex
    positions. Lets shape OBJs be parsed by reading only their 'v' lines instead
    of a full (slow) pywavefront parse.
    """
    mapping = {}
    current = None
    with open(obj_path) as obj_file:
        for line in obj_file:
            if line.startswith("usemtl"):
                current = line.split(None, 1)[1].strip()
                mapping.setdefault(current, [])
            elif line.startswith("f "):
                indices = [int(corner.split("/")[0]) - 1 for corner in line.split()[1:]]
                corners = mapping[current]
                # Fan-triangulate like pywavefront (no-op for triangle exports)
                for k in range(1, len(indices) - 1):
                    corners.extend((indices[0], indices[k], indices[k + 1]))
    return {name: np.array(idx, dtype=np.int32) for name, idx in mapping.items()}

def build_morph_data(basis_path, shape_paths):
    """Parses the basis + one-OBJ-per-blendshape exports into numpy morph data (no GL).

    Every OBJ comes from blender_export_blendshapes.py with identical settings on
    identical topology, so vertex ordering is identical across files and deltas
    are computed by direct subtraction. Only the basis gets a full pywavefront
    parse; shapes use the fast 'v'-lines reader (pywavefront on every file would
    take many minutes for a full blendshape set on this CPU).
    Returns (materials, shape_names) where each material dict holds the basis
    vertices, texture info and a {shape_name: (N, 3) delta} dict.
    """
    print("Parsing basis mesh (pywavefront, slow)...", flush=True)
    materials = []
    by_name = {}
    for name, (vertices, material) in parse_obj_materials(basis_path).items():
        entry = {
            "name": name,
            "vertices": vertices,
            "texture": material.texture.path if material.texture else None,
            "diffuse": list(material.diffuse),
            "deltas": {},
        }
        materials.append(entry)
        by_name[name] = entry

    # Verify the fast corner mapping reproduces pywavefront's expansion exactly
    # (same source text parses to bit-identical float32), then use it for shapes.
    # Falls back to full pywavefront parsing per shape if anything mismatches.
    basis_raw = read_obj_positions(basis_path)
    fast_mapping = None
    try:
        mapping = build_corner_mapping(basis_path)
        for entry in materials:
            indices = mapping.get(entry["name"])
            if indices is None or len(indices) != len(entry["vertices"]) or \
                    not np.array_equal(basis_raw[indices], entry["vertices"][:, 5:8]):
                raise ValueError(f"corner mapping mismatch for material '{entry['name']}'")
        fast_mapping = mapping
    except Exception as exc:
        print(f"Fast OBJ path unavailable ({exc}), using pywavefront for every shape", flush=True)

    shape_names = []
    for index, shape_path in enumerate(shape_paths):
        shape_name = os.path.splitext(os.path.basename(shape_path))[0]
        print(f"Parsing blendshape {index + 1}/{len(shape_paths)}: {shape_name}", flush=True)
        used = False

        if fast_mapping is not None:
            shape_raw = read_obj_positions(shape_path)
            if len(shape_raw) != len(basis_raw):
                print(f"Warning: '{shape_name}' vertex count differs from basis, skipping")
                continue
            delta_raw = shape_raw - basis_raw
            if np.abs(delta_raw).max() > 1e-6:
                for entry in materials:
                    delta = delta_raw[fast_mapping[entry["name"]]]
                    # Most shapes only move part of the head; store deltas only
                    # for the materials they actually affect
                    if np.abs(delta).max() > 1e-6:
                        entry["deltas"][shape_name] = np.ascontiguousarray(delta)
                        used = True
        else:
            for name, (vertices, _material) in parse_obj_materials(shape_path).items():
                entry = by_name.get(name)
                if entry is None or len(vertices) != len(entry["vertices"]):
                    print(f"Warning: '{shape_name}' geometry does not match basis for material '{name}', skipping")
                    continue
                delta = vertices[:, 5:8] - entry["vertices"][:, 5:8]
                if np.abs(delta).max() > 1e-6:
                    entry["deltas"][shape_name] = np.ascontiguousarray(delta)
                    used = True

        if used:
            shape_names.append(shape_name)
        else:
            print(f"Warning: blendshape '{shape_name}' moves no vertices, ignoring")
    return materials, shape_names

def save_morph_cache(cache_path, materials, shape_names):
    arrays = {}
    manifest = {"shape_names": shape_names, "materials": []}
    for i, mat in enumerate(materials):
        manifest["materials"].append({
            "name": mat["name"],
            "texture": mat["texture"],
            "diffuse": mat["diffuse"],
            "delta_shapes": list(mat["deltas"].keys()),
        })
        arrays[f"vertices_{i}"] = mat["vertices"]
        for shape, delta in mat["deltas"].items():
            arrays[f"delta_{i}_{shape}"] = delta
    np.savez_compressed(cache_path, manifest=json.dumps(manifest), **arrays)

def load_morph_cache(cache_path):
    archive = np.load(cache_path, allow_pickle=False)
    manifest = json.loads(str(archive["manifest"]))
    materials = []
    for i, mat in enumerate(manifest["materials"]):
        materials.append({
            "name": mat["name"],
            "vertices": archive[f"vertices_{i}"],
            "texture": mat["texture"],
            "diffuse": mat["diffuse"],
            "deltas": {shape: archive[f"delta_{i}_{shape}"] for shape in mat["delta_shapes"]},
        })
    return materials, manifest["shape_names"]

def load_morph_data_cached(blendshape_dir):
    """CPU half of loading the morphable head: basis mesh + per-blendshape
    deltas as numpy data, no GL.

    Parsing dozens of OBJs with pywavefront is slow, so the parsed numpy data is
    cached in an .npz that is rebuilt whenever any source OBJ changes.
    Returns (materials, shape_names); safe to run on a background thread.
    """
    basis_path = os.path.join(blendshape_dir, BLENDSHAPE_BASIS)
    shape_paths = sorted(
        path for path in glob.glob(os.path.join(blendshape_dir, "*.obj"))
        if os.path.basename(path) != BLENDSHAPE_BASIS
    )
    cache_path = os.path.join(blendshape_dir, MORPH_CACHE_FILE)
    source_mtime = max(os.path.getmtime(path) for path in [basis_path] + shape_paths)

    materials = None
    if os.path.isfile(cache_path) and os.path.getmtime(cache_path) >= source_mtime:
        try:
            materials, shape_names = load_morph_cache(cache_path)
            print(f"Loaded {len(shape_names)} blendshapes from cache")
        except Exception as exc:
            print(f"Morph cache unreadable ({exc}), re-parsing OBJs")
            materials = None
    if materials is None:
        materials, shape_names = build_morph_data(basis_path, shape_paths)
        save_morph_cache(cache_path, materials, shape_names)
        print(f"Parsed {len(shape_names)} blendshapes, cached to {cache_path}")
    return materials, shape_names

def load_morphable_model(blendshape_dir):
    """Loads the morphable head synchronously: parsed data + GL buffers.
    Returns (sub_meshes, shape_names). The app itself loads via
    BackgroundModelLoader instead, to keep the render loop responsive."""
    materials, shape_names = load_morph_data_cached(blendshape_dir)
    sub_meshes = []
    for mat in materials:
        vao, static_vbo, position_vbo, base_positions = create_morphable_vao(mat["vertices"])
        texture_id, has_alpha = create_material_texture(mat["texture"], mat["diffuse"])
        sub_mesh = SubMesh(vao, static_vbo, len(base_positions), texture_id,
                           position_vbo=position_vbo, base_positions=base_positions)
        sub_mesh.deltas = mat["deltas"]
        build_morph_texture(sub_mesh)
        sub_mesh.transparent = has_alpha
        sub_mesh.material_name = mat["name"]
        sub_meshes.append(sub_mesh)
    return sub_meshes, shape_names

def read_material_maps_config():
    """Reads MATERIAL_MAPS_FILE (generated by extract_unity_materials.py).
    Returns {material_name: params} ({} when the file is absent)."""
    if not os.path.isfile(MATERIAL_MAPS_FILE):
        print(f"No {MATERIAL_MAPS_FILE} found, rendering without normal/metallic/AO maps")
        return {}
    with open(MATERIAL_MAPS_FILE) as maps_file:
        config = json.load(maps_file)
    print(f"Loaded material maps for {len(config)} materials")
    return config

def assign_material_params(sub_mesh, params, load_map,
                           default_normal, default_white, default_black):
    """Assigns one sub-mesh its normal/metallic/AO/emission maps and scalar
    parameters from a MATERIAL_MAPS_FILE entry. params=None (or missing maps)
    fall back to neutral 1x1 defaults, so the render path stays uniform.
    load_map(path, max_size) must return a GL texture id."""
    if params is None:
        sub_mesh.normal_tex = default_normal
        sub_mesh.metallic_tex = default_white
        sub_mesh.ao_tex = default_white
        sub_mesh.emission_tex = default_black
        return
    sub_mesh.normal_tex = (load_map(params["normal_map"], NORMAL_MAP_MAX_SIZE)
                           if params["normal_map"] else default_normal)
    sub_mesh.metallic_tex = (load_map(params["metallic_map"], PARAM_MAP_MAX_SIZE)
                             if params["metallic_map"] else default_white)
    sub_mesh.ao_tex = (load_map(params["ao_map"], PARAM_MAP_MAX_SIZE)
                       if params["ao_map"] else default_white)
    sub_mesh.emission_tex = (load_map(params["emission_map"], NORMAL_MAP_MAX_SIZE)
                             if params.get("emission_map") else default_black)
    sub_mesh.bump_scale = params["bump_scale"]
    # With a metallic map the texture R provides the value (factor 1);
    # without one the scalar applies to the white default
    sub_mesh.metallic_factor = 1.0 if params["metallic_map"] else params["metallic"]
    sub_mesh.smoothness = params["smoothness"]
    sub_mesh.occlusion_strength = params["occlusion_strength"] if params["ao_map"] else 0.0
    # Emission only when a map exists (Unity gates it on the _EMISSION keyword;
    # several materials have a white color but no map and must not glow)
    sub_mesh.emission_color = (tuple(params.get("emission_color", (0.0, 0.0, 0.0)))
                               if params.get("emission_map") else (0.0, 0.0, 0.0))

def apply_material_maps(sub_meshes, default_normal, default_white, default_black):
    """Attaches the normal/metallic/AO/emission maps to every sub-mesh,
    loading each map file once (synchronous decode + upload)."""
    config = read_material_maps_config()
    texture_cache = {}
    def load_map(path, max_size):
        if path not in texture_cache:
            texture_cache[path] = load_texture(path, max_size=max_size)[0]
        return texture_cache[path]

    for sub_mesh in sub_meshes:
        assign_material_params(sub_mesh, config.get(sub_mesh.material_name),
                               load_map, default_normal, default_white, default_black)

class BackgroundModelLoader:
    """Loads the morphable head without stalling the render loop.

    A worker thread does all the CPU-heavy work (morph cache / OBJ parsing,
    image decoding); the render thread then drains a queue of small GL upload
    tasks via pump_gl(), a few milliseconds per frame, so the app can show UI
    (e.g. onboarding) from the very first frame. sub_meshes and shape_names
    are complete once pump_gl() returns True; error carries a traceback string
    if the worker failed.
    """

    def __init__(self, blendshape_dir, default_normal, default_white, default_black):
        self.blendshape_dir = blendshape_dir
        self.defaults = (default_normal, default_white, default_black)
        self.sub_meshes = []
        self.shape_names = []
        self.ready = False
        self.error = None
        self._parsed = None
        self._gl_tasks = None
        self._thread = threading.Thread(target=self._parse_worker, daemon=True)
        self._thread.start()

    def _parse_worker(self):
        try:
            materials, shape_names = load_morph_data_cached(self.blendshape_dir)
            config = read_material_maps_config()
            images = {}
            for mat in materials:
                if mat["texture"] and mat["texture"] not in images:
                    images[mat["texture"]] = decode_image(mat["texture"])
            for params in config.values():
                for key, max_size in (("normal_map", NORMAL_MAP_MAX_SIZE),
                                      ("metallic_map", PARAM_MAP_MAX_SIZE),
                                      ("ao_map", PARAM_MAP_MAX_SIZE),
                                      ("emission_map", NORMAL_MAP_MAX_SIZE)):
                    path = params.get(key)
                    if path and path not in images:
                        images[path] = decode_image(path, max_size=max_size)
            self._parsed = (materials, shape_names, config, images)
        except Exception:
            self.error = traceback.format_exc()

    def _build_gl_tasks(self):
        materials, shape_names, config, images = self._parsed
        self.shape_names = shape_names
        uploaded = {}

        def get_texture(path, _max_size=None):
            # max_size was already applied at decode time, on the worker
            if path not in uploaded:
                width, height, img_data, _ = images[path]
                uploaded[path] = upload_texture_rgba(width, height, img_data)
            return uploaded[path]

        def make_submesh_task(mat):
            def task():
                vao, static_vbo, position_vbo, base_positions = create_morphable_vao(mat["vertices"])
                if mat["texture"]:
                    texture_id = get_texture(mat["texture"])
                    has_alpha = images[mat["texture"]][3]
                else:
                    texture_id = create_solid_color_texture(mat["diffuse"])
                    has_alpha = len(mat["diffuse"]) == 4 and mat["diffuse"][3] < 1.0
                sub_mesh = SubMesh(vao, static_vbo, len(base_positions), texture_id,
                                   position_vbo=position_vbo, base_positions=base_positions)
                sub_mesh.deltas = mat["deltas"]
                build_morph_texture(sub_mesh)
                sub_mesh.transparent = has_alpha
                sub_mesh.material_name = mat["name"]
                assign_material_params(sub_mesh, config.get(mat["name"]),
                                       get_texture, *self.defaults)
                self.sub_meshes.append(sub_mesh)
            return task

        # One task per unique texture upload, then one per sub-mesh: each
        # slice of work stays small enough to hide inside a frame
        tasks = [(lambda path=path: get_texture(path)) for path in images]
        tasks += [make_submesh_task(mat) for mat in materials]
        return tasks

    def pump_gl(self, budget_seconds=0.003):
        """Runs queued GL work on the render thread within a small per-frame
        time budget. Returns True once the model is fully ready."""
        if self.ready or self.error:
            return self.ready
        if self._gl_tasks is None:
            if self._parsed is None:
                return False  # worker still parsing/decoding
            self._gl_tasks = self._build_gl_tasks()
        deadline = time.perf_counter() + budget_seconds
        while self._gl_tasks and time.perf_counter() < deadline:
            self._gl_tasks.pop(0)()
        if not self._gl_tasks:
            self.ready = True
            print(f"Model ready: {len(self.sub_meshes)} sub-meshes, "
                  f"{len(self.shape_names)} blendshapes")
        return self.ready

def create_cone_mesh(length=0.15, radius=0.02, segments=32):
    """Generates a smooth-shaded cone pointing down -Z (the OpenXR controller
    "forward").

    Apex sits at (0, 0, -length), base ring at the origin, so when rendered with a
    controller pose matrix the cone points where the controller points. The
    side uses exact analytic cone normals per vertex (the apex takes its
    triangle's mid-angle normal), so lighting wraps smoothly around the
    surface; the base cap stays flat. Output is interleaved T2F_N3F_V3F.
    """
    apex = (0.0, 0.0, -length)
    slant = math.hypot(length, radius)
    data = []

    def emit(p, n):
        data.extend([0.0, 0.0, *n, *p])

    def side_normal(angle):
        # Outward normal of the cone surface at this ring angle
        return (length * math.cos(angle) / slant,
                length * math.sin(angle) / slant,
                -radius / slant)

    for i in range(segments):
        a0 = 2.0 * math.pi * i / segments
        a1 = 2.0 * math.pi * (i + 1) / segments
        p0 = (radius * math.cos(a0), radius * math.sin(a0), 0.0)
        p1 = (radius * math.cos(a1), radius * math.sin(a1), 0.0)
        # smooth side
        emit(apex, side_normal((a0 + a1) / 2))
        emit(p0, side_normal(a0))
        emit(p1, side_normal(a1))
        # flat base cap
        emit((0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
        emit(p1, (0.0, 0.0, 1.0))
        emit(p0, (0.0, 0.0, 1.0))

    return np.array(data, dtype=np.float32)

def create_box_mesh():
    """Unit cube centered at the origin, in interleaved T2F_N3F_V3F format.

    The front (+Z) face carries the full texture; all other faces sample the
    (0, 0) texel, so they take on the texture's background/corner color.
    Scale to the desired size with the model matrix.
    """
    data = []
    def quad(a, b, c, d, normal, uvs):
        for p, uv in ((a, uvs[0]), (b, uvs[1]), (c, uvs[2])):
            data.extend([*uv, *normal, *p])
        for p, uv in ((a, uvs[0]), (c, uvs[2]), (d, uvs[3])):
            data.extend([*uv, *normal, *p])

    s = 0.5
    full = [(0, 0), (1, 0), (1, 1), (0, 1)]
    corner = [(0, 0)] * 4
    quad((-s, -s, s), (s, -s, s), (s, s, s), (-s, s, s), (0, 0, 1), full)        # front
    quad((s, -s, -s), (-s, -s, -s), (-s, s, -s), (s, s, -s), (0, 0, -1), corner) # back
    quad((-s, -s, -s), (-s, -s, s), (-s, s, s), (-s, s, -s), (-1, 0, 0), corner) # left
    quad((s, -s, s), (s, -s, -s), (s, s, -s), (s, s, s), (1, 0, 0), corner)      # right
    quad((-s, s, s), (s, s, s), (s, s, -s), (-s, s, -s), (0, 1, 0), corner)      # top
    quad((-s, -s, -s), (s, -s, -s), (s, -s, s), (-s, -s, s), (0, -1, 0), corner) # bottom
    return np.array(data, dtype=np.float32)

def rounded_rect_outline(width, height, radius, segments=10):
    """CCW outline of a rounded rectangle in the XY plane, centered on the
    origin, as a list of ((x, y), (nx, ny)) point/outward-normal pairs.
    radius is clamped to the half-extents (radius = height/2 gives a pill);
    segments is the arc resolution per corner."""
    radius = min(radius, width / 2, height / 2)
    cx, cy = width / 2 - radius, height / 2 - radius
    arcs = (((cx, cy), 0.0), ((-cx, cy), 90.0), ((-cx, -cy), 180.0), ((cx, -cy), 270.0))
    outline = []
    for (px, py), start in arcs:
        for k in range(segments + 1):
            angle = math.radians(start + 90.0 * k / segments)
            outline.append(((px + radius * math.cos(angle),
                             py + radius * math.sin(angle)),
                            (math.cos(angle), math.sin(angle))))
    return outline

def create_rounded_box_mesh(width, height, depth, radius, segments=10,
                            profile_segments=6):
    """Pill mesh rounded in all three dimensions, interleaved T2F_N3F_V3F.

    The plan view is a rounded rectangle (corner `radius`); the front/back
    edges are filleted through the depth with a circular profile of radius
    min(depth/2, radius) — a full semicircle when the depth allows, i.e. a
    true 3D pill. Geometrically this is the Minkowski sum of a sphere with an
    inset rounded-rect slab, so every surface point has an exact analytic
    normal and shading is fully smooth.

    Built at actual size (model matrix must only translate/uniform-scale).
    The texture projects along +Z across the whole front half, wrapping over
    the front fillet; the back half samples the (0, 0) texel, taking the
    texture's background color.
    """
    edge = min(depth / 2, radius)
    inner = rounded_rect_outline(width - 2 * edge, height - 2 * edge,
                                 radius - edge, segments)
    wall_half = depth / 2 - edge  # straight side wall half-height (0 = full pill)
    data = []

    def front_uv(x, y):
        return (x / width + 0.5, y / height + 0.5)

    def ring(alpha, z_base):
        """Outline ring at fillet angle alpha (-pi/2 back pole .. +pi/2 front
        pole), inflated `edge` outward from the inner slab outline."""
        cos_a, sin_a = math.cos(alpha), math.sin(alpha)
        points = []
        for (x, y), (nx, ny) in inner:
            px, py = x + edge * cos_a * nx, y + edge * cos_a * ny
            uv = front_uv(px, py) if alpha >= 0.0 else (0.0, 0.0)
            points.append(((px, py, z_base + edge * sin_a),
                           (cos_a * nx, cos_a * ny, sin_a), uv))
        return points

    rings = [ring(math.radians(-90.0 + 90.0 * k / profile_segments), -wall_half)
             for k in range(profile_segments + 1)]
    if wall_half > 1e-9:
        rings.append(ring(0.0, wall_half))
    rings += [ring(math.radians(90.0 * k / profile_segments), wall_half)
              for k in range(1, profile_segments + 1)]

    def emit(vertex):
        position, normal, uv = vertex
        data.extend([*uv, *normal, *position])

    count = len(inner)
    for ring0, ring1 in zip(rings, rings[1:]):
        for i in range(count):
            j = (i + 1) % count
            emit(ring0[i]); emit(ring0[j]); emit(ring1[j])
            emit(ring0[i]); emit(ring1[j]); emit(ring1[i])

    # Flat caps over the inner outline; the pole rings end exactly on these
    # outlines, so the surface closes watertight
    hz = depth / 2
    for i in range(count):
        (x0, y0), _ = inner[i]
        (x1, y1), _ = inner[(i + 1) % count]
        data.extend([0.5, 0.5, 0, 0, 1, 0.0, 0.0, hz])
        data.extend([*front_uv(x0, y0), 0, 0, 1, x0, y0, hz])
        data.extend([*front_uv(x1, y1), 0, 0, 1, x1, y1, hz])
        data.extend([0.0, 0.0, 0, 0, -1, 0.0, 0.0, -hz])
        data.extend([0.0, 0.0, 0, 0, -1, x1, y1, -hz])
        data.extend([0.0, 0.0, 0, 0, -1, x0, y0, -hz])
    return np.array(data, dtype=np.float32)

# UI typeface: IBM Plex Sans, the Project Babble brand font
# (https://docs.babble.diy/docs/branding), bundled in FONT_DIR under the SIL
# OFL. Labels use Bold Italic per the brand's style; buttons stay Bold
# (upright) for control legibility. Falls back to system fonts if missing.
FONT_DIR = "fonts"
_UI_FONT_PATHS = {}
def _find_ui_font(style="bold"):
    """Returns a .ttf path for style 'bold' or 'bolditalic'."""
    if style not in _UI_FONT_PATHS:
        names = {
            "bold": ("IBMPlexSans-Bold.ttf", "LiberationSans-Bold.ttf"),
            "bolditalic": ("IBMPlexSans-BoldItalic.ttf", "LiberationSans-BoldItalic.ttf"),
        }[style]
        candidates = []
        for name in names:
            candidates += glob.glob(os.path.join(FONT_DIR, name))
            candidates += glob.glob(f"/usr/share/fonts/**/{name}", recursive=True)
        candidates += glob.glob("/usr/share/fonts/**/*.ttf", recursive=True)
        _UI_FONT_PATHS[style] = candidates[0] if candidates else ""
        if candidates:
            print(f"UI font ({style}): {candidates[0]}")
    return _UI_FONT_PATHS[style]

def create_text_texture(text, tex_width, tex_height, bg, fg=UI_TEXT_COLOR,
                        style="bold", margins=(24, 24)):
    """Rasterizes centered (optionally multi-line, via '\\n') text onto a solid
    background and uploads it as a GL texture. The (0, 0) texel is always
    background, so box faces with corner UVs render as the plain background
    color. style: 'bold' or 'bolditalic' (see _find_ui_font). margins: total
    (x, y) texture pixels excluded from the text fit — callers on rounded
    meshes pass margins covering the fillet so text stays on the flat face."""
    img = Image.new("RGBA", (tex_width, tex_height), (*bg, 255))
    draw = ImageDraw.Draw(img)

    font_path = _find_ui_font(style)
    size = int(tex_height * 0.5)
    while True:
        font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        spacing = int(size * 0.4)
        bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing)
        if not font_path or size <= 14 or (bbox[2] - bbox[0] <= tex_width - margins[0]
                                           and bbox[3] - bbox[1] <= tex_height - margins[1]):
            break
        size = int(size * 0.85)
    draw.multiline_text(
        ((tex_width - (bbox[2] - bbox[0])) / 2 - bbox[0],
         (tex_height - (bbox[3] - bbox[1])) / 2 - bbox[1]),
        text, font=font, fill=(*fg, 255), spacing=spacing, align="center",
    )

    img = img.transpose(Image.FLIP_TOP_BOTTOM)  # GL UV origin is bottom-left
    tex_id = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, tex_width, tex_height, 0,
                    gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, img.tobytes())
    gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
    apply_max_anisotropy(16.0)  # text is read at a steep angle; max quality
    # The texture is ~2x oversampled for its on-screen size, so trilinear
    # always samples BETWEEN mip levels — and every mip is an average that
    # bleeds bright glyphs outward into the dark background (the "blurred
    # outwards" look). Bias sampling toward the sharp top mip; the slight
    # extra shimmer is a good trade for legible text.
    try:
        gl.glTexParameterf(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_LOD_BIAS, -0.75)
    except Exception:
        pass  # ES-only context: no per-texture LOD bias, keep trilinear
    return tex_id

def dialog_position_below_chin(sub_meshes, model_matrix):
    """Computes a stage-space dialog position just below the head model's chin
    and about a head-length in front of its face.

    The chin and face front are located objectively from the vertices that the
    JawOpen blendshape displaces: the jaw region's low-percentile y is the chin
    height and its high-percentile z the face front (percentiles keep stray
    vertices from skewing the result). DIALOG_CHIN_CLEARANCE leaves room for
    the chin to drop on open-jaw expressions (~4 cm on this model).

    Returns (position, label_top_y, cover_center_y) for ConfirmationBox's
    position/anchor_top/cover_center — the dialog hangs from the label-top
    anchor, so tall panes grow downward and never overlap the mouth during
    demonstrations, while cover-mode panes center on the eye line (median y of
    the Eye*-shape region; None when absent). Returns None if no JawOpen data
    exists.
    """
    jaw_regions = [sm.base_positions[np.abs(sm.deltas["JawOpen"]).max(axis=1) > 1e-6]
                   for sm in sub_meshes if "JawOpen" in sm.deltas]
    if not jaw_regions:
        return None
    jaw = np.vstack(jaw_regions)
    chin_y = np.percentile(jaw[:, 1], 1) + model_matrix[3, 1]
    face_z = np.percentile(jaw[:, 2], 99) + model_matrix[3, 2]
    label_top = chin_y - DIALOG_CHIN_CLEARANCE
    big_pane_height = 0.065 + ConfirmationBox.BIG_LABEL_SIZE[1]  # above origin
    position = np.array([model_matrix[3, 0],
                         label_top - big_pane_height,
                         face_z + DIALOG_HEAD_DISTANCE], dtype=np.float32)

    eye_regions = [sm.base_positions[np.abs(delta).max(axis=1) > 1e-6]
                   for sm in sub_meshes
                   for name, delta in sm.deltas.items() if name.startswith("Eye")]
    cover_center = (float(np.median(np.vstack(eye_regions)[:, 1])) + model_matrix[3, 1]
                    - DIALOG_COVER_DROP
                    if eye_regions else None)
    return position, label_top, cover_center

def intersect_ray_aabb(origin, direction, center, half):
    """Slab-method ray vs axis-aligned-box test.

    Returns the entry distance along the (unit) ray, 0.0 if the origin is
    already inside the box, or None on a miss.
    """
    t_near, t_far = 0.0, np.inf
    for axis in range(3):
        if abs(direction[axis]) < 1e-9:
            if abs(origin[axis] - center[axis]) > half[axis]:
                return None
            continue
        t1 = (center[axis] - half[axis] - origin[axis]) / direction[axis]
        t2 = (center[axis] + half[axis] - origin[axis]) / direction[axis]
        if t1 > t2:
            t1, t2 = t2, t1
        t_near = max(t_near, t1)
        t_far = min(t_far, t2)
        if t_near > t_far:
            return None
    return t_near

class ConfirmationBox:
    """A 3D label-plus-two-buttons dialog operated with the controller cones.

    Buttons highlight (and slightly grow) while a controller cone overlaps
    them or while a controller's laser ray points at them, and fire their
    callback when the trigger is pressed during a hover. set_label() and
    set_buttons() can be called at any time, e.g. from inside a button
    callback, to drive a multi-step flow.
    """
    LABEL_SIZE = np.array([0.44, 0.09, 0.02], dtype=np.float32)
    BIG_LABEL_SIZE = np.array([0.44, 0.26, 0.02], dtype=np.float32)  # multi-line panes
    BUTTON_SIZE = np.array([0.16, 0.07, 0.035], dtype=np.float32)
    BUTTON_CORNER_RADIUS = 0.035   # = half the button height: a full pill
    LABEL_CORNER_RADIUS = 0.028    # gently rounded pane corners
    HOVER_MARGIN = 0.012   # AABB padding (m) so hovering feels forgiving
    BUTTON_OFFSETS = (np.array([-0.105, 0.0, 0.0], dtype=np.float32),
                      np.array([0.105, 0.0, 0.0], dtype=np.float32))

    def __init__(self, position):
        self.position = np.asarray(position, dtype=np.float32)
        # When set (world y), the dialog hangs from this label-top height
        # instead of standing on position[1]: tall panes grow downward, so the
        # label always sits at the same height (e.g. just below a chin)
        self.anchor_top = None
        # Cover mode (per label, see set_label): the whole dialog centers on
        # this height instead — used by text-only panes that sit over the face
        self.cover_center = UI_DIALOG_COVER_Y
        self.cover = False
        # Rounded pill meshes are built at actual element size (their smooth
        # side normals don't survive non-uniform scaling), one per size
        self._mesh_cache = {}  # (w, h, d, radius) -> (vao, vertex_count)
        self._label_tex_cache = {}  # (text, big) -> texture id
        self.label_tex = None
        self.label_size = self.LABEL_SIZE
        self.buttons = []
        # Per hand: distance along the laser ray to the nearest hovered button
        # this frame (None = pointing at nothing). Used to clip the laser beam.
        self.ray_hit_distances = [None, None]
        self.set_label(" ")

    def _mesh_for(self, size, radius):
        key = (float(size[0]), float(size[1]), float(size[2]), radius)
        if key not in self._mesh_cache:
            vao, _, vertex_count = create_interleaved_vao(
                create_rounded_box_mesh(size[0], size[1], size[2], radius))
            self._mesh_cache[key] = (vao, vertex_count)
        return self._mesh_cache[key]

    @staticmethod
    def _text_margins(size, radius, tex_width, tex_height):
        """Total (x, y) texture-pixel margins that keep text on the element's
        flat front face: the 3D edge fillet consumes min(depth/2, radius)
        meters all around the rim, plus a small aesthetic pad."""
        edge = min(float(size[2]) / 2, radius)
        return (int(tex_width * 2 * edge / float(size[0])) + 24,
                int(tex_height * 2 * edge / float(size[1])) + 24)

    def effective_position(self):
        """Dialog origin (button-row center) honoring cover/anchor_top modes."""
        if self.cover and self.cover_center is not None:
            position = self.position.copy()
            # Center the label+buttons cluster (buttons bottom to label top)
            cluster_center = (0.065 + self.label_size[1] - self.BUTTON_SIZE[1] / 2) / 2
            position[1] = self.cover_center - cluster_center
            return position
        if self.anchor_top is None:
            return self.position
        position = self.position.copy()
        position[1] = self.anchor_top - (0.065 + self.label_size[1])
        return position

    def set_label(self, text, big=False, cover=False):
        """big=True uses the taller multi-line pane (for onboarding text);
        cover=True additionally centers the dialog over the face (text-only
        panes with nothing to watch behind them).

        Textures are cached by (text, big): countdown labels repeat every
        second, and re-rasterizing + re-uploading them on the render thread
        caused a once-per-second frame hitch."""
        self.label_size = self.BIG_LABEL_SIZE if big else self.LABEL_SIZE
        self.cover = cover
        key = (text, big)
        tex = self._label_tex_cache.get(key)
        if tex is None:
            if len(self._label_tex_cache) >= 96:
                old_key = next(iter(self._label_tex_cache))
                gl.glDeleteTextures([self._label_tex_cache.pop(old_key)])
            tex_height = int(1024 * self.label_size[1] / self.label_size[0])
            tex = create_text_texture(
                text, 1024, tex_height, UI_LABEL_BG, style="bolditalic",
                margins=self._text_margins(self.label_size, self.LABEL_CORNER_RADIUS,
                                           1024, tex_height))
            self._label_tex_cache[key] = tex
        self.label_tex = tex

    def set_buttons(self, *definitions):
        """definitions: (text, callback) pairs, e.g. set_buttons(("Yes", f), ("No", g))."""
        for button in self.buttons:
            gl.glDeleteTextures([button["tex"], button["tex_hover"]])
        self.buttons = []
        margins = self._text_margins(self.BUTTON_SIZE, self.BUTTON_CORNER_RADIUS,
                                     512, 224)
        for (text, callback), offset in zip(definitions, self.BUTTON_OFFSETS):
            self.buttons.append({
                "callback": callback,
                "offset": offset,
                "hovered": False,
                "tex": create_text_texture(text, 512, 224, UI_BUTTON_BG,
                                           margins=margins),
                "tex_hover": create_text_texture(text, 512, 224, UI_BUTTON_BG_HOVER,
                                                 margins=margins),
            })

    def update(self, probe_points_per_hand, rays_per_hand, pressed_edges):
        """Updates hover state and fires callbacks.

        probe_points_per_hand: per hand, a list of world-space points sampled
        along the controller cone. rays_per_hand: per hand, a world-space
        (origin, unit direction) laser ray, or None when untracked — pointing
        the ray at a button hovers it just like touching it with the cone.
        pressed_edges: per hand, True on the frame the trigger was pressed.
        """
        self.ray_hit_distances = [None, None]
        position = self.effective_position()
        for button in self.buttons:
            center = position + button["offset"]
            half = self.BUTTON_SIZE / 2 + self.HOVER_MARGIN
            hovering_hands = []
            for hand, points in enumerate(probe_points_per_hand):
                cone_hit = any(np.all(np.abs(point - center) <= half) for point in points)
                ray = rays_per_hand[hand]
                ray_hit = (intersect_ray_aabb(ray[0], ray[1], center, half)
                           if ray is not None else None)
                if ray_hit is not None:
                    best = self.ray_hit_distances[hand]
                    self.ray_hit_distances[hand] = (ray_hit if best is None
                                                    else min(best, ray_hit))
                if cone_hit or ray_hit is not None:
                    hovering_hands.append(hand)
            button["hovered"] = bool(hovering_hands)
            if any(pressed_edges[hand] for hand in hovering_hands) and button["callback"]:
                button["callback"]()

    def _draw_element(self, model_loc, offset, mesh, tex, scale=1.0):
        """mesh: (vao, vertex_count) built at actual size; scale stays uniform."""
        vao, vertex_count = mesh
        matrix = np.identity(4, dtype=np.float32)
        matrix[0, 0] = matrix[1, 1] = matrix[2, 2] = scale
        matrix[3, :3] = self.effective_position() + offset
        gl.glBindVertexArray(vao)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, matrix)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, vertex_count)

    def draw(self, model_loc):
        """Draws the dialog. Expects texture unit 0 active and units 1-4 bound
        to the neutral material defaults (as set up for the controller cones)."""
        # The label sits just above the buttons whatever its current height
        label_offset = np.array([0.0, 0.065 + self.label_size[1] / 2, 0.0],
                                dtype=np.float32)
        self._draw_element(model_loc, label_offset,
                           self._mesh_for(self.label_size, self.LABEL_CORNER_RADIUS),
                           self.label_tex)
        button_mesh = self._mesh_for(self.BUTTON_SIZE, self.BUTTON_CORNER_RADIUS)
        for button in self.buttons:
            self._draw_element(
                model_loc, button["offset"], button_mesh,
                button["tex_hover"] if button["hovered"] else button["tex"],
                scale=1.08 if button["hovered"] else 1.0,
            )

# --- Lit, normal-mapped shaders for OpenGL ES 3.0 ---
VERTEX_SHADER = """
#version 300 es
layout(location = 0) in vec2 aTexCoord; // UV coordinates
layout(location = 1) in vec3 aNormal;   // Vertex normals
layout(location = 2) in vec3 aPos;      // Vertex positions (neutral pose)

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;

// GPU blendshapes: all shape deltas live in one RGBA32F texture, packed at
// load time (see build_morph_texture). Shape slot s occupies morph_rows rows;
// vertex v sits at texel (v % W, s * morph_rows + v / W). Per frame only the
// active weights change — no vertex data is ever re-uploaded.
uniform highp sampler2D morph_deltas;   // unit 5
uniform int morph_count;                // active shapes; 0 = rigid draw
uniform int morph_rows;                 // texture rows per shape
uniform int morph_slots[24];
uniform float morph_weights[24];

out vec2 TexCoord;
out vec3 WorldPos;
out vec3 WorldNormal;

void main() {
    vec3 pos = aPos;
    if (morph_count > 0) {
        int W = textureSize(morph_deltas, 0).x;
        int col = gl_VertexID % W;
        int row = gl_VertexID / W;
        for (int i = 0; i < morph_count; ++i) {
            ivec2 tc = ivec2(col, morph_slots[i] * morph_rows + row);
            pos += morph_weights[i] * texelFetch(morph_deltas, tc, 0).xyz;
        }
    }
    vec4 world = model * vec4(pos, 1.0);
    WorldPos = world.xyz;
    WorldNormal = mat3(model) * aNormal; // model has no non-uniform scale
    TexCoord = aTexCoord;
    gl_Position = projection * view * world;
}
"""

FRAGMENT_SHADER = """
#version 300 es
precision highp float;

in vec2 TexCoord;
in vec3 WorldPos;
in vec3 WorldNormal;
out vec4 FragColor;

uniform sampler2D base_map;     // unit 0: albedo (alpha = cutout/blend)
uniform sampler2D normal_map;   // unit 1: tangent-space normal (flat default)
uniform sampler2D metallic_map; // unit 2: metallic in R (white default)
uniform sampler2D ao_map;       // unit 3: ambient occlusion in R (white default)
uniform sampler2D emission_map; // unit 4: emissive color (black default)

uniform float bump_scale;          // normal map intensity (Unity _BumpScale)
uniform float metallic_factor;     // scales metallic_map.r
uniform float smoothness;          // 0..1 gloss (Unity _GlossMapScale/_Glossiness)
uniform float occlusion_strength;  // 0..1 AO blend (Unity _OcclusionStrength)
uniform vec3 emission_color;       // multiplies emission_map (Unity _EmissionColor)
uniform vec3 camera_pos;

const vec3 LIGHT_DIR = vec3(0.302, 0.805, 0.503); // pre-normalized, world space
const vec3 LIGHT_COLOR = vec3(0.9);
const vec3 AMBIENT = vec3(0.4);

vec3 perturb_normal() {
    // Screen-space derivative TBN: needs no tangent vertex attributes, and
    // keeps working when blendshapes morph the positions.
    vec3 n_tex = texture(normal_map, TexCoord).xyz * 2.0 - 1.0;
    n_tex.xy *= bump_scale;
    vec3 N = normalize(WorldNormal);
    vec3 dp1 = dFdx(WorldPos);
    vec3 dp2 = dFdy(WorldPos);
    vec2 duv1 = dFdx(TexCoord);
    vec2 duv2 = dFdy(TexCoord);
    vec3 dp2perp = cross(dp2, N);
    vec3 dp1perp = cross(N, dp1);
    vec3 T = dp2perp * duv1.x + dp1perp * duv2.x;
    vec3 B = dp2perp * duv1.y + dp1perp * duv2.y;
    // max() guard keeps degenerate UVs (e.g. controller cones) from NaN-ing
    float invmax = inversesqrt(max(max(dot(T, T), dot(B, B)), 1e-12));
    return normalize(mat3(T * invmax, B * invmax, N) * normalize(n_tex));
}

void main() {
    vec4 base = texture(base_map, TexCoord);
    if (base.a < 0.1) discard; // alpha-cutout for card-style geometry (eyelashes)

    vec3 N = perturb_normal();
    float metallic = texture(metallic_map, TexCoord).r * metallic_factor;
    float ao = mix(1.0, texture(ao_map, TexCoord).r, occlusion_strength);

    vec3 V = normalize(camera_pos - WorldPos);
    vec3 H = normalize(LIGHT_DIR + V);
    float ndl = max(dot(N, LIGHT_DIR), 0.0);
    float ndh = max(dot(N, H), 0.0);

    // Blinn-Phong with a gloss-driven exponent; metals tint their specular
    float spec_power = exp2(smoothness * 10.0 + 1.0);
    float spec = pow(ndh, spec_power) * smoothness * ndl;

    vec3 diffuse_color = base.rgb * (1.0 - metallic);
    vec3 spec_color = mix(vec3(0.04), base.rgb, metallic);
    vec3 color = diffuse_color * (AMBIENT * ao + LIGHT_COLOR * ndl)
               + spec_color * LIGHT_COLOR * spec
               + texture(emission_map, TexCoord).rgb * emission_color;
    FragColor = vec4(color, base.a);
}
"""

# Fullscreen sky/passthrough background. The alpha channel is what makes the
# passthrough fade work: with the ALPHA_BLEND environment blend mode the
# compositor shows the cameras wherever framebuffer alpha is below 1.
BACKGROUND_VERTEX_SHADER = """
#version 300 es
out vec2 ndc;
void main() {
    // Fullscreen triangle from gl_VertexID; no vertex buffers needed
    vec2 pos = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2)) * 2.0 - 1.0;
    ndc = pos;
    gl_Position = vec4(pos, 0.999, 1.0);
}
"""

BACKGROUND_FRAGMENT_SHADER = """
#version 300 es
precision highp float;
in vec2 ndc;
out vec4 FragColor;

uniform mat4 inv_proj;        // per-eye inverse projection
uniform mat4 inv_view;        // per-eye inverse view (rotation part used)
uniform vec3 top_color;
uniform vec3 horizon_color;
uniform vec3 floor_color;     // only seen when passthrough is unavailable
uniform vec3 aurora_primary;
uniform vec3 aurora_secondary;
uniform float aurora_strength;
uniform float aurora_speed;
uniform float star_strength;
uniform float time;           // seconds since app start, same for both eyes
uniform float fade_low;       // sin(elevation): fully passthrough at/below
uniform float fade_high;      // sin(elevation): fully virtual at/above
uniform float passthrough_on; // 1.0 = alpha fade to cameras, 0.0 = opaque floor
uniform float sky_detail;     // 2 = full, 1 = one curtain + stars, 0 = gradient only

float hash21(vec2 p) {
    return fract(sin(dot(p, vec2(41.31, 289.13))) * 43758.5453);
}

// Slow layered undulation along the horizon. Integer phi multipliers keep
// every term continuous across the atan seam behind the user.
float aurora_wave(float phi, float t) {
    return sin(phi * 2.0 + t * 0.100)
         + 0.55 * sin(phi * 5.0 - t * 0.063 + 1.7)
         + 0.30 * sin(phi * 9.0 + t * 0.151 + 4.2);
}

// One aurora curtain: a gaussian band in elevation whose center/width ripple
// along phi, with faint vertical ray striations. Fades out near the horizon
// (clear of the task zone) AND well before the zenith — phi is degenerate at
// the pole, so a curtain allowed to reach it pinches into a triangular
// spike. The striation amplitude also tapers as longitude lines converge.
float curtain(float phi, float elev, float t, float seed) {
    float w = aurora_wave(phi + seed * 2.39, t + seed * 13.0);
    float center = 0.40 + 0.07 * seed + 0.11 * w;
    float width = 0.14 + 0.05 * sin(phi * 3.0 - t * 0.047 + seed * 5.0);
    float band = exp(-pow((elev - center) / width, 2.0));
    float horiz = sqrt(max(1.0 - elev * elev, 0.0));
    float rays = 0.78 + 0.22 * sin(phi * 36.0 + w * 4.0 + seed * 9.0)
                        * smoothstep(0.35, 0.70, horiz);
    float zenith_fade = 1.0 - smoothstep(0.62, 0.85, elev);
    return band * rays * zenith_fade * smoothstep(0.06, 0.24, elev);
}

// Sparse twinkling stars on a cube-face grid: mapping the view direction to
// its dominant axis' cube face keeps cells near-uniform over the whole sky
// (no pole pinching at the zenith). Each star draws size, brightness,
// twinkle depth and a warm-to-cool tint from its own hash, like a real sky.
// Star discs stay inside their cell, so face seams never clip them. Fades
// in above the task zone.
vec3 stars(vec3 dir, float t) {
    float horizon_fade = smoothstep(0.12, 0.35, dir.y);
    if (horizon_fade <= 0.0) return vec3(0.0);
    vec3 a = abs(dir);
    vec2 uv;
    float face;
    if (a.y >= a.x && a.y >= a.z) {
        uv = dir.xz / a.y; face = 0.0;
    } else if (a.x >= a.z) {
        uv = dir.yz / a.x; face = dir.x > 0.0 ? 1.0 : 2.0;
    } else {
        uv = dir.xy / a.z; face = dir.z > 0.0 ? 3.0 : 4.0;
    }
    vec2 grid = (uv * 0.5 + 0.5) * 22.0;
    vec2 cell = floor(grid) + face * 91.7;
    float h = hash21(cell);
    if (h < 0.78) return vec3(0.0);
    vec2 star_pos = vec2(hash21(cell + 7.13), hash21(cell + 3.71)) * 0.6 + 0.2;
    float mag = hash21(cell + 11.9);                  // per-star magnitude
    float radius = mix(0.05, 0.16, mag * mag);        // many small, few large
    float bright = mix(0.22, 1.0, mag * mag * mag);   // big ones also brighter
    float d = length(fract(grid) - star_pos);
    // dimmer stars shimmer more; bright ones hold steadier
    float twinkle = 1.0 - (0.5 - 0.3 * mag) * (0.5 + 0.5 * sin(t * (0.8 + 2.2 * h) + h * 41.0));
    vec3 tint = mix(vec3(1.00, 0.84, 0.66), vec3(0.72, 0.84, 1.00), hash21(cell + 5.3));
    return (1.0 - smoothstep(0.0, radius, d)) * bright * twinkle * tint * horizon_fade;
}

void main() {
    // World-space view ray for this pixel; dir.y is sin(view elevation)
    vec4 v = inv_proj * vec4(ndc, 1.0, 1.0);
    vec3 dir = normalize(mat3(inv_view) * v.xyz);
    float elev = dir.y;
    float phi = atan(dir.x, -dir.z);

    vec3 sky = mix(horizon_color, top_color, smoothstep(0.0, 0.85, max(elev, 0.0)));
    // sky_detail is dynamically uniform, so these branches skip the aurora
    // and star math entirely on lower settings (GPU load relief)
    if (sky_detail > 0.5) {
        float at = time * aurora_speed;
        sky += aurora_strength * curtain(phi, elev, at, 0.0) * aurora_primary;
        if (sky_detail > 1.5) {
            sky += aurora_strength * 0.7 * curtain(phi, elev, at, 1.0) * aurora_secondary;
        }
        sky += star_strength * stars(dir, time);
    }

    float fade = smoothstep(fade_low, fade_high, elev);
    if (passthrough_on > 0.5) {
        FragColor = vec4(sky, fade);
    } else {
        FragColor = vec4(mix(floor_color, sky, fade), 1.0);
    }
}
"""

def compile_shader(source, shader_type):
    # Clear any stale OpenGL errors left over from initialization
    while gl.glGetError() != gl.GL_NO_ERROR:
        pass

    shader = gl.glCreateShader(shader_type)

    # Check if the actual creation generated an error
    err = gl.glGetError()
    if err != gl.GL_NO_ERROR:
        raise RuntimeError(f"glCreateShader failed with error code: {err}")

    gl.glShaderSource(shader, source)
    gl.glCompileShader(shader)
    if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
        raise RuntimeError(gl.glGetShaderInfoLog(shader).decode())
    return shader

def create_shader_program(vertex_source, fragment_source):
    """Compiles and links the shader program, freeing the intermediate shader objects."""
    vertex_shader = compile_shader(vertex_source, gl.GL_VERTEX_SHADER)
    fragment_shader = compile_shader(fragment_source, gl.GL_FRAGMENT_SHADER)
    program = gl.glCreateProgram()
    gl.glAttachShader(program, vertex_shader)
    gl.glAttachShader(program, fragment_shader)
    gl.glLinkProgram(program)
    if not gl.glGetProgramiv(program, gl.GL_LINK_STATUS):
        raise RuntimeError(gl.glGetProgramInfoLog(program).decode())
    gl.glDeleteShader(vertex_shader)
    gl.glDeleteShader(fragment_shader)
    return program

def make_projection_matrix(fov, near, far):
    """Generates an asymmetric frustum projection matrix using pyrr."""
    # Convert OpenXR angles to frustum bounds at the near plane
    l = math.tan(fov.angle_left) * near
    r = math.tan(fov.angle_right) * near
    u = math.tan(fov.angle_up) * near
    d = math.tan(fov.angle_down) * near

    # Generate the standard perspective projection matrix
    proj = Matrix44.perspective_projection_bounds(l, r, d, u, near, far)
    return np.array(proj, dtype=np.float32)

def make_view_matrix(pose):
    """Generates a correct View matrix from the headset pose using pyrr."""
    # 1. Create translation matrix for the negated position
    translation = Matrix44.from_translation([
        -pose.position.x,
        -pose.position.y,
        -pose.position.z
    ])

    # 2. Create rotation matrix from the OpenXR quaternion
    q = Quaternion([
        -pose.orientation.x,
        -pose.orientation.y,
        -pose.orientation.z,
        pose.orientation.w
    ])
    rotation = Matrix44.from_quaternion(q).inverse

    # 3. Combine them: View = Rotation_inverse * Translation_inverse
    view = rotation * translation
    return np.array(view, dtype=np.float32)

def pose_to_model_matrix(pose):
    """Builds a model matrix from an XrPosef (controller pose in the play space).

    glUniformMatrix4fv is called with transpose=GL_FALSE, so this row-major numpy
    array is read column-major by GL: storing rotation transposed and translation
    in the last row yields the standard column-vector matrix [R | p].
    """
    x = pose.orientation.x
    y = pose.orientation.y
    z = pose.orientation.z
    w = pose.orientation.w
    rotation = np.array([
        [1 - 2*(y*y + z*z), 2*(x*y - z*w),     2*(x*z + y*w)],
        [2*(x*y + z*w),     1 - 2*(x*x + z*z), 2*(y*z - x*w)],
        [2*(x*z - y*w),     2*(y*z + x*w),     1 - 2*(x*x + y*y)],
    ], dtype=np.float32)

    matrix = np.identity(4, dtype=np.float32)
    matrix[:3, :3] = rotation.T
    matrix[3, :3] = (pose.position.x, pose.position.y, pose.position.z)
    return matrix

def start_recording_daemon(session):
    """Starts the camera recorder for the lifetime of the app and returns a stop
    callable (None if it could not start). Outputs carry the shared session
    timestamp so they pair with that session's expression log.
    """
    output_path = f"recording_{session}.mp4"
    stamp_path = f"recording_{session}_stamps.bin"
    try:
        import final_recording_daemon as recording
        if RECORDING_DAEMON_IN_PROCESS:
            daemon = recording.RecordingDaemon(output_path, stamp_path, verbose=False,
                                               exposure=CAMERA_EXPOSURE,
                                               analog_gain=CAMERA_ANALOG_GAIN)
            daemon.start()
            print(f"Recording daemon running in-process -> {output_path}")
            return daemon.stop
        proc = recording.launch_subprocess(output_path, stamp_path,
                                           exposure=CAMERA_EXPOSURE,
                                           analog_gain=CAMERA_ANALOG_GAIN)
        print(f"Recording daemon subprocess started (pid {proc.pid}) -> {output_path}")
        return lambda: recording.stop_subprocess(proc)
    except Exception as exc:
        print(f"Warning: recording daemon failed to start: {exc}")
        return None

def append_expression_record(log_path, start_ns, end_ns, shape_name):
    """Appends one expression-hold record to the binary log.

    Record layout: '>2q' start/end timestamps from time.time_ns(), then one
    byte holding the UTF-8 byte length of the shape name, then the name itself
    (truncated to 255 bytes, the most a single length byte can describe).
    """
    name_bytes = shape_name.encode("utf-8")[:255]
    with open(log_path, "ab") as log_file:
        log_file.write(struct.pack(">2qB", start_ns, end_ns, len(name_bytes)))
        log_file.write(name_bytes)

_narration_proc = None
def play_narration(name):
    """Plays NARRATION_DIR/<name>.wav fire-and-forget (paplay or aplay), if
    both the clip and a player exist; missing clips are silently skipped, so
    narration is opt-in per file. A clip still playing is stopped first so
    panes don't talk over each other."""
    global _narration_proc
    path = os.path.join(NARRATION_DIR, f"{name}.wav")
    if not os.path.isfile(path):
        return
    player = shutil.which("paplay") or shutil.which("aplay")
    if player is None:
        return
    if _narration_proc is not None and _narration_proc.poll() is None:
        _narration_proc.terminate()
    _narration_proc = subprocess.Popen([player, path], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)

# --- OpenXR Application ---
class OpenXRApp:
    def __init__(self):
        self.instance = None
        self.system_id = None
        self.session = None
        self.swapchains = []
        self.play_space = None

        # Controller input state
        self.action_set = None
        self.hand_paths = None
        self.hand_spaces = []
        self.button_actions = {}
        self.button_callbacks = {}
        self.controller_matrices = [None, None]
        self.trigger_held = [False, False]
        self.trigger_pressed_edges = [False, False]  # True only on the press frame

        # Blendshape state
        self.morph_sub_meshes = []
        self.blendshape_coefficients = {}
        self.blendshapes_dirty = False

    def initialize_xr(self):
        # 1. Create OpenXR Instance
        # Request OpenGL graphics binding extension
        extensions = [xr.KHR_OPENGL_ENABLE_EXTENSION_NAME]
        self.instance = xr.create_instance(
            xr.InstanceCreateInfo(
                application_info=xr.ApplicationInfo(
                    application_name="OpenXR Python Sphere",
                    api_version=xr.XR_API_VERSION_1_0, #XR_CURRENT_API_VERSION,
                ),
                enabled_extension_names=extensions,
            )
        )

        # 2. Get System (Headset)
        self.system_id = xr.get_system(
            self.instance,
            xr.SystemGetInfo(form_factor=xr.FormFactor.HEAD_MOUNTED_DISPLAY)
        )

    def initialize_graphics_and_session(self):
        # Initialize GLFW for context creation
        if not glfw.init():
            raise RuntimeError("Failed to initialize GLFW")

        glfw.window_hint(glfw.VISIBLE, glfw.FALSE)

        glfw.window_hint(glfw.CLIENT_API, glfw.OPENGL_ES_API)
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 0)

        # Create a hidden dummy window to hold the OpenGL context
        self.window = glfw.create_window(640, 480, "OpenXR Dummy Context", None, None)
        if not self.window:
            glfw.terminate()
            raise RuntimeError("Failed to create GLFW window")
        glfw.make_context_current(self.window)

        # --- Add this line to create the Framebuffer ---
        self.fbo = gl.glGenFramebuffers(1)

        # Get graphics requirements
        graphics_requirements = xr.get_opengl_graphics_requirements_khr(self.instance, self.system_id)

        # Bind OpenXR to OpenGL context
        # On Linux/X11 setup:
        from OpenGL.GLX import glXGetCurrentContext, glXGetCurrentDisplay
        graphics_binding = xr.GraphicsBindingOpenGLXlibKHR(
            x_display=glXGetCurrentDisplay(),
            glx_context=glXGetCurrentContext(),
            # Additional X11/GLX parameters may be required depending on platform
        )

        # 3. Create Session
        self.session = xr.create_session(
            self.instance,
            xr.SessionCreateInfo(
                next=graphics_binding,
                system_id=self.system_id
            )
        )

        # 4. Create Reference Space
        self.play_space = xr.create_reference_space(
            self.session,
            xr.ReferenceSpaceCreateInfo(
                reference_space_type=xr.ReferenceSpaceType.STAGE, # Floor-aligned
                pose_in_reference_space=xr.Posef()
            )
        )

        # Get View configuration views (left/right eyes)
        views = xr.enumerate_view_configuration_views(
            self.instance, self.system_id, xr.ViewConfigurationType.PRIMARY_STEREO
        )
        # Optional GPU relief: render below the recommended size and let the
        # compositor upscale. The scaled value is written back into the view
        # structs so the swapchain, FBOs, viewport and layer rect all agree.
        for view in views:
            view.recommended_image_rect_width = max(
                2, int(view.recommended_image_rect_width * RENDER_SCALE)) & ~1
            view.recommended_image_rect_height = max(
                2, int(view.recommended_image_rect_height * RENDER_SCALE)) & ~1
        print(f"Eye buffers: "
              f"{views[0].recommended_image_rect_width}x"
              f"{views[0].recommended_image_rect_height} per eye "
              f"(scale {RENDER_SCALE}), MSAA request {MSAA_SAMPLES}x")

        # 5. Query supported swapchain formats from the runtime
        supported_formats = xr.enumerate_swapchain_formats(self.session)
        print("Runtime supported formats:", [hex(f) for f in supported_formats])

        # Prefer a format with an alpha channel — the passthrough fade needs
        # destination alpha to survive into the compositor. Otherwise take the
        # runtime's most-preferred format; fall back to GL_RGBA8 if none.
        alpha_formats = (0x8058, 0x8C43, 0x881A, 0x805B)  # RGBA8, SRGB8_ALPHA8, RGBA16F, RGBA16
        with_alpha = [f for f in supported_formats if f in alpha_formats]
        if with_alpha or supported_formats:
            selected_format = (with_alpha or supported_formats)[0]
        else:
            selected_format = 0x8058 # GL_RGBA8

        print(f"Selecting swapchain format: {hex(selected_format)}")

        for view in views:
            swapchain_info = xr.SwapchainCreateInfo(
                create_flags=0,                                           # Explicitly set to 0
                usage_flags=xr.SwapchainUsageFlags.COLOR_ATTACHMENT_BIT,
                format=selected_format,
                sample_count=1,
                width=view.recommended_image_rect_width,
                height=view.recommended_image_rect_height,
                face_count=1,                                             # Must be 1 for a standard 2D texture
                array_size=1,                                             # Must be 1 for non-array texture
                mip_count=1,                                              # Must be 1 for no mipmaps
            )
            swapchain = xr.create_swapchain(self.session, swapchain_info)
            # Retrieve the texture IDs allocated by the OpenXR runtime
            images = xr.enumerate_swapchain_images(swapchain, xr.SwapchainImageOpenGLKHR)
            self.swapchains.append((swapchain, images, view))

        # Per-eye render targets (MSAA when available), matching the format
        self._setup_eye_framebuffers(views, selected_format)

    def _setup_eye_framebuffers(self, views, color_format):
        """Per-eye render targets. Preferred path: multisampled color+depth
        renderbuffers in their own FBO (resolved into the swapchain texture
        after each frame) for antialiased edges. Falls back to plain depth
        renderbuffers and direct swapchain rendering when MSAA isn't
        available. The MSAA color format matches the swapchain so the resolve
        blit is format-identical, as GLES requires."""
        self.msaa_fbos = []
        self.depth_buffers = []
        max_samples = int(gl.glGetIntegerv(gl.GL_MAX_SAMPLES))
        self.msaa_samples = max(1, min(MSAA_SAMPLES, max_samples))
        if self.msaa_samples > 1:
            try:
                for view in views:
                    width = view.recommended_image_rect_width
                    height = view.recommended_image_rect_height
                    color_rbo = gl.glGenRenderbuffers(1)
                    gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, color_rbo)
                    gl.glRenderbufferStorageMultisample(
                        gl.GL_RENDERBUFFER, self.msaa_samples, color_format,
                        width, height)
                    depth_rbo = gl.glGenRenderbuffers(1)
                    gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, depth_rbo)
                    gl.glRenderbufferStorageMultisample(
                        gl.GL_RENDERBUFFER, self.msaa_samples,
                        gl.GL_DEPTH_COMPONENT24, width, height)
                    fbo = gl.glGenFramebuffers(1)
                    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
                    gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                                 gl.GL_RENDERBUFFER, color_rbo)
                    gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_ATTACHMENT,
                                                 gl.GL_RENDERBUFFER, depth_rbo)
                    status = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)
                    if status != gl.GL_FRAMEBUFFER_COMPLETE:
                        raise RuntimeError(f"framebuffer incomplete (status {hex(status)})")
                    self.msaa_fbos.append(fbo)
                print(f"MSAA {self.msaa_samples}x enabled (GPU max: {max_samples}x)")
            except Exception as exc:
                print(f"MSAA unavailable ({exc}); rendering without antialiasing")
                self.msaa_fbos = []
                self.msaa_samples = 1
        if not self.msaa_fbos:
            for view in views:
                depth_rbo = gl.glGenRenderbuffers(1)
                gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, depth_rbo)
                # 24-bit depth storage matching this eye's resolution
                gl.glRenderbufferStorage(
                    gl.GL_RENDERBUFFER,
                    gl.GL_DEPTH_COMPONENT24,
                    view.recommended_image_rect_width,
                    view.recommended_image_rect_height
                )
                self.depth_buffers.append(depth_rbo)
        gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, 0)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)

    def initialize_actions(self):
        """Creates the action set, controller pose/button actions, suggested bindings
        and per-hand action spaces. Must run after the session is created."""
        self.action_set = xr.create_action_set(
            instance=self.instance,
            create_info=xr.ActionSetCreateInfo(
                action_set_name="gameplay",
                localized_action_set_name="Gameplay",
                priority=0,
            ),
        )

        self.hand_paths = (xr.Path * 2)(
            xr.string_to_path(self.instance, "/user/hand/left"),
            xr.string_to_path(self.instance, "/user/hand/right"),
        )

        def make_action(name, action_type):
            return xr.create_action(
                action_set=self.action_set,
                create_info=xr.ActionCreateInfo(
                    action_type=action_type,
                    action_name=name,
                    localized_action_name=name.replace("_", " ").title(),
                    count_subaction_paths=len(self.hand_paths),
                    subaction_paths=self.hand_paths,
                ),
            )

        self.hand_pose_action = make_action("hand_pose", xr.ActionType.POSE_INPUT)
        self.button_actions = {
            "trigger": make_action("trigger", xr.ActionType.BOOLEAN_INPUT),
            "squeeze": make_action("squeeze", xr.ActionType.BOOLEAN_INPUT),
            "a": make_action("a_button", xr.ActionType.BOOLEAN_INPUT),
            "b": make_action("b_button", xr.ActionType.BOOLEAN_INPUT),
            "menu": make_action("menu_button", xr.ActionType.BOOLEAN_INPUT),
        }

        # Suggested bindings per interaction profile. Booleans bound to .../value
        # paths are thresholded to on/off by the runtime as per the OpenXR spec.
        pose = self.hand_pose_action
        trigger = self.button_actions["trigger"]
        squeeze = self.button_actions["squeeze"]
        a_btn = self.button_actions["a"]
        b_btn = self.button_actions["b"]
        menu = self.button_actions["menu"]

        profile_bindings = {
            "/interaction_profiles/khr/simple_controller": [
                (pose, "/user/hand/left/input/aim/pose"),
                (pose, "/user/hand/right/input/aim/pose"),
                (trigger, "/user/hand/left/input/select/click"),
                (trigger, "/user/hand/right/input/select/click"),
                (menu, "/user/hand/left/input/menu/click"),
                (menu, "/user/hand/right/input/menu/click"),
            ],
            "/interaction_profiles/valve/index_controller": [
                (pose, "/user/hand/left/input/aim/pose"),
                (pose, "/user/hand/right/input/aim/pose"),
                (trigger, "/user/hand/left/input/trigger/click"),
                (trigger, "/user/hand/right/input/trigger/click"),
                (squeeze, "/user/hand/left/input/squeeze/value"),
                (squeeze, "/user/hand/right/input/squeeze/value"),
                (a_btn, "/user/hand/left/input/a/click"),
                (a_btn, "/user/hand/right/input/a/click"),
                (b_btn, "/user/hand/left/input/b/click"),
                (b_btn, "/user/hand/right/input/b/click"),
            ],
            "/interaction_profiles/oculus/touch_controller": [
                (pose, "/user/hand/left/input/aim/pose"),
                (pose, "/user/hand/right/input/aim/pose"),
                (trigger, "/user/hand/left/input/trigger/value"),
                (trigger, "/user/hand/right/input/trigger/value"),
                (squeeze, "/user/hand/left/input/squeeze/value"),
                (squeeze, "/user/hand/right/input/squeeze/value"),
                (a_btn, "/user/hand/left/input/x/click"),
                (a_btn, "/user/hand/right/input/a/click"),
                (b_btn, "/user/hand/left/input/y/click"),
                (b_btn, "/user/hand/right/input/b/click"),
                (menu, "/user/hand/left/input/menu/click"),
            ],
        }

        for profile, bindings in profile_bindings.items():
            suggested = [
                xr.ActionSuggestedBinding(
                    action=action,
                    binding=xr.string_to_path(self.instance, path),
                )
                for action, path in bindings
            ]
            try:
                xr.suggest_interaction_profile_bindings(
                    instance=self.instance,
                    suggested_bindings=xr.InteractionProfileSuggestedBinding(
                        interaction_profile=xr.string_to_path(self.instance, profile),
                        count_suggested_bindings=len(suggested),
                        suggested_bindings=(xr.ActionSuggestedBinding * len(suggested))(*suggested),
                    ),
                )
            except xr.exception.XrException as exc:
                print(f"Skipping bindings for {profile}: {exc}")

        # One action space per hand to locate the controllers in the play space
        self.hand_spaces = [
            xr.create_action_space(
                session=self.session,
                create_info=xr.ActionSpaceCreateInfo(
                    action=self.hand_pose_action,
                    subaction_path=hand_path,
                    pose_in_action_space=xr.Posef(),
                ),
            )
            for hand_path in self.hand_paths
        ]

        xr.attach_session_action_sets(
            session=self.session,
            attach_info=xr.SessionActionSetsAttachInfo(
                count_action_sets=1,
                action_sets=ctypes.pointer(self.action_set),
            ),
        )

        # Kept alive as an attribute: sync_actions stores a pointer to this
        self._active_action_set = xr.ActiveActionSet(
            action_set=self.action_set,
            subaction_path=xr.NULL_PATH,
        )

    def register_button_callback(self, button, callback):
        """Registers callback(hand: str, pressed: bool) for a button edge event.

        Valid button names: 'trigger', 'squeeze', 'a', 'b', 'menu'.
        The callback fires once on press and once on release.
        """
        self.button_callbacks.setdefault(button, []).append(callback)

    def _dispatch_button_event(self, hand_name, button, pressed):
        for callback in self.button_callbacks.get(button, []):
            callback(hand_name, pressed)

    def set_blendshape_coefficients(self, coefficients):
        """Sets the blendshape weights, e.g. {'jawOpen': 0.7, 'mouthSmileLeft': 0.2}.

        Shapes not listed return to 0. Cheap to call as often as you like (e.g.
        per tracker packet); vertex data is recomputed and uploaded at most once
        per rendered frame.
        """
        self.blendshape_coefficients = dict(coefficients)
        self.blendshapes_dirty = True

    def update_blendshapes(self):
        """Refreshes each sub-mesh's active morph slot/weight arrays.

        Morphing runs on the GPU (deltas texture + weight uniforms), so this
        touches no vertex data and makes no GL calls — draw_sub_mesh uploads
        the small uniform arrays computed here.
        """
        if not self.blendshapes_dirty:
            return
        self.blendshapes_dirty = False
        active = [(name, weight) for name, weight in self.blendshape_coefficients.items()
                  if abs(weight) > 1e-4]
        for sub_mesh in self.morph_sub_meshes:
            relevant = [(sub_mesh.morph_slot[name], weight) for name, weight in active
                        if name in sub_mesh.morph_slot]
            if len(relevant) > MAX_ACTIVE_MORPHS:
                relevant = sorted(relevant, key=lambda sw: -abs(sw[1]))[:MAX_ACTIVE_MORPHS]
            sub_mesh.morph_active_slots = np.array([s for s, _ in relevant], dtype=np.int32)
            sub_mesh.morph_active_weights = np.array([w for _, w in relevant], dtype=np.float32)

    def controller_probe_points(self, hand, distances=(0.075, 0.15)):
        """World-space points along the controller cone axis, used for UI hit
        tests. Returns [] when the controller is untracked."""
        matrix = self.controller_matrices[hand]
        if matrix is None:
            return []
        # Row-vector transform: local @ matrix[:3,:3] == R @ local
        return [np.array([0.0, 0.0, -d], dtype=np.float32) @ matrix[:3, :3] + matrix[3, :3]
                for d in distances]

    def controller_ray(self, hand):
        """World-space (origin, unit direction) of the controller's laser ray —
        the cone's pointing axis. Returns None when the controller is untracked."""
        matrix = self.controller_matrices[hand]
        if matrix is None:
            return None
        direction = np.array([0.0, 0.0, -1.0], dtype=np.float32) @ matrix[:3, :3]
        return matrix[3, :3].copy(), direction

    def poll_input(self, predicted_display_time):
        """Syncs the action set, updates controller pose matrices and fires
        button callbacks for any state changes since the last sync."""
        self.trigger_pressed_edges = [False, False]
        try:
            xr.sync_actions(
                self.session,
                xr.ActionsSyncInfo(
                    count_active_action_sets=1,
                    active_action_sets=ctypes.pointer(self._active_action_set),
                ),
            )
        except xr.exception.SessionNotFocused:
            # SteamVR keeps the session unfocused while it's in the background
            # (e.g. dashboard open or just launched). Input is unavailable until
            # focus arrives, so hide the controllers and try again next frame.
            self.controller_matrices = [None, None]
            self.trigger_held = [False, False]
            return

        for hand in (HAND_LEFT, HAND_RIGHT):
            location = xr.locate_space(
                space=self.hand_spaces[hand],
                base_space=self.play_space,
                time=predicted_display_time,
            )
            valid_bits = (XR_SPACE_LOCATION_POSITION_VALID_BIT |
                          XR_SPACE_LOCATION_ORIENTATION_VALID_BIT)
            if location.location_flags & valid_bits == valid_bits:
                self.controller_matrices[hand] = pose_to_model_matrix(location.pose)
            else:
                self.controller_matrices[hand] = None

            for button, action in self.button_actions.items():
                state = xr.get_action_state_boolean(
                    self.session,
                    xr.ActionStateGetInfo(
                        action=action,
                        subaction_path=self.hand_paths[hand],
                    ),
                )
                if not state.is_active:
                    continue
                pressed = bool(state.current_state)
                if button == "trigger":
                    self.trigger_held[hand] = pressed
                if state.changed_since_last_sync:
                    if button == "trigger" and pressed:
                        self.trigger_pressed_edges[hand] = True
                    self._dispatch_button_event(HAND_NAMES[hand], button, pressed)

    def run(self):
        # One session timestamp names all of this run's outputs:
        # recording_<session>.mp4 / _stamps.bin / _expressions.bin
        session = time.strftime("%Y%m%d_%H%M%S")
        expression_log_path = f"recording_{session}_expressions.bin"

        # The recording daemon does NOT start with the app: camera bring-up and
        # the encoder would compete with model/UI loading (visible hitching),
        # and video from before the first interaction carries no labels anyway.
        # It starts on the first button press (welcome pane's Next/Skip, or the
        # first Ready when onboarding is off); atexit then covers every exit
        # path (normal close, EXITING state, crash, Ctrl+C).
        recorder_state = {"started": False}

        def ensure_recording_started():
            if recorder_state["started"] or not ENABLE_RECORDING_DAEMON:
                return
            recorder_state["started"] = True
            stop_recorder = start_recording_daemon(session)
            if stop_recorder is not None:
                atexit.register(stop_recorder)

        self.initialize_xr()
        self.initialize_graphics_and_session()
        self.initialize_actions()

        # Demo callbacks: log every button edge to the console
        for button_name in self.button_actions:
            self.register_button_callback(
                button_name,
                lambda hand, pressed, b=button_name:
                    print(f"[input] {hand} {b} {'pressed' if pressed else 'released'}")
            )

        # Compile shaders and cache uniform locations (avoids per-frame lookups)
        shader_program = create_shader_program(VERTEX_SHADER, FRAGMENT_SHADER)
        projection_loc = gl.glGetUniformLocation(shader_program, "projection")
        view_loc = gl.glGetUniformLocation(shader_program, "view")
        model_loc = gl.glGetUniformLocation(shader_program, "model")
        camera_pos_loc = gl.glGetUniformLocation(shader_program, "camera_pos")
        bump_scale_loc = gl.glGetUniformLocation(shader_program, "bump_scale")
        metallic_factor_loc = gl.glGetUniformLocation(shader_program, "metallic_factor")
        smoothness_loc = gl.glGetUniformLocation(shader_program, "smoothness")
        occlusion_loc = gl.glGetUniformLocation(shader_program, "occlusion_strength")
        emission_color_loc = gl.glGetUniformLocation(shader_program, "emission_color")
        morph_count_loc = gl.glGetUniformLocation(shader_program, "morph_count")
        morph_rows_loc = gl.glGetUniformLocation(shader_program, "morph_rows")
        morph_slots_loc = gl.glGetUniformLocation(shader_program, "morph_slots")
        morph_weights_loc = gl.glGetUniformLocation(shader_program, "morph_weights")

        # Samplers have fixed texture units; set them once
        gl.glUseProgram(shader_program)
        gl.glUniform1i(gl.glGetUniformLocation(shader_program, "base_map"), 0)
        gl.glUniform1i(gl.glGetUniformLocation(shader_program, "normal_map"), 1)
        gl.glUniform1i(gl.glGetUniformLocation(shader_program, "metallic_map"), 2)
        gl.glUniform1i(gl.glGetUniformLocation(shader_program, "ao_map"), 3)
        gl.glUniform1i(gl.glGetUniformLocation(shader_program, "emission_map"), 4)
        gl.glUniform1i(gl.glGetUniformLocation(shader_program, "morph_deltas"), 5)
        gl.glUniform1i(morph_count_loc, 0)

        # Neutral 1x1 fallbacks: flat tangent-space normal, white (no-op
        # AO/metallic) and black (no emission)
        default_normal = create_solid_color_texture([0.5, 0.5, 1.0])
        default_white = create_solid_color_texture([1.0, 1.0, 1.0])
        default_black = create_solid_color_texture([0.0, 0.0, 0.0])

        gl.glEnable(gl.GL_DEPTH_TEST)

        # Background sky + passthrough fade (see BACKGROUND_*/PASSTHROUGH_*)
        background_program = create_shader_program(BACKGROUND_VERTEX_SHADER,
                                                   BACKGROUND_FRAGMENT_SHADER)
        bg_inv_proj_loc = gl.glGetUniformLocation(background_program, "inv_proj")
        bg_inv_view_loc = gl.glGetUniformLocation(background_program, "inv_view")
        background_vao = gl.glGenVertexArrays(1)  # empty: vertices from gl_VertexID
        gl.glUseProgram(background_program)
        gl.glUniform3f(gl.glGetUniformLocation(background_program, "top_color"),
                       *BACKGROUND_TOP_COLOR)
        gl.glUniform3f(gl.glGetUniformLocation(background_program, "horizon_color"),
                       *BACKGROUND_HORIZON_COLOR)
        gl.glUniform3f(gl.glGetUniformLocation(background_program, "floor_color"),
                       *BACKGROUND_FLOOR_COLOR)
        gl.glUniform3f(gl.glGetUniformLocation(background_program, "aurora_primary"),
                       *AURORA_COLOR_PRIMARY)
        gl.glUniform3f(gl.glGetUniformLocation(background_program, "aurora_secondary"),
                       *AURORA_COLOR_SECONDARY)
        gl.glUniform1f(gl.glGetUniformLocation(background_program, "aurora_strength"),
                       AURORA_STRENGTH)
        gl.glUniform1f(gl.glGetUniformLocation(background_program, "aurora_speed"),
                       AURORA_SPEED)
        gl.glUniform1f(gl.glGetUniformLocation(background_program, "star_strength"),
                       STAR_STRENGTH)
        gl.glUniform1f(gl.glGetUniformLocation(background_program, "sky_detail"),
                       SKY_DETAIL)
        gl.glUniform1f(gl.glGetUniformLocation(background_program, "fade_low"),
                       math.sin(math.radians(PASSTHROUGH_FULL_BELOW_DEG)))
        gl.glUniform1f(gl.glGetUniformLocation(background_program, "fade_high"),
                       math.sin(math.radians(PASSTHROUGH_FADE_TOP_DEG)))
        bg_time_loc = gl.glGetUniformLocation(background_program, "time")
        sky_start_time = time.monotonic()

        # Use ALPHA_BLEND when the runtime offers it: the compositor then
        # blends our frame over the passthrough cameras by framebuffer alpha
        blend_mode = xr.EnvironmentBlendMode.OPAQUE
        passthrough_active = False
        try:
            supported_blend_modes = xr.enumerate_environment_blend_modes(
                self.instance, self.system_id,
                xr.ViewConfigurationType.PRIMARY_STEREO)
            print("Supported environment blend modes:",
                  [xr.EnvironmentBlendMode(m).name for m in supported_blend_modes])
            if (PASSTHROUGH_LOWER_WORLD
                    and xr.EnvironmentBlendMode.ALPHA_BLEND in supported_blend_modes):
                blend_mode = xr.EnvironmentBlendMode.ALPHA_BLEND
                passthrough_active = True
        except Exception as exc:
            print(f"Could not enumerate environment blend modes ({exc})")
        if PASSTHROUGH_LOWER_WORLD and not passthrough_active:
            print("Passthrough lower world unavailable (no ALPHA_BLEND); "
                  "rendering an opaque floor color instead")
        gl.glUniform1f(gl.glGetUniformLocation(background_program, "passthrough_on"),
                       1.0 if passthrough_active else 0.0)

        # Load the head model: morphable if a Blender blendshape export exists,
        # otherwise fall back to the static OBJ. The morphable head takes 10+
        # seconds to parse, so it loads in the background while the user goes
        # through onboarding; until it's ready only the UI/cones render.
        model_loader = None
        model_ready = False
        shape_names = []
        opaque_meshes = []
        transparent_meshes = []
        if os.path.isfile(os.path.join(BLENDSHAPE_DIR, BLENDSHAPE_BASIS)):
            model_loader = BackgroundModelLoader(BLENDSHAPE_DIR, default_normal,
                                                 default_white, default_black)
        else:
            print(f"No '{BLENDSHAPE_DIR}/{BLENDSHAPE_BASIS}' found, loading static untitled.obj")
            sub_meshes = load_multi_material_obj("untitled.obj")
            apply_material_maps(sub_meshes, default_normal, default_white, default_black)
            # Meshes with alpha (e.g. eyelash cards) draw in a blended pass
            opaque_meshes = [sm for sm in sub_meshes if not sm.transparent]
            transparent_meshes = [sm for sm in sub_meshes if sm.transparent]
            model_ready = True

        # Guided expression dialog + onboarding. The dialog only needs a box
        # mesh and text textures, so it exists from the very first frame:
        # onboarding plays while the head model loads in the background, and
        # the expression sequence starts once both have finished.
        expression_ui = None
        expression_targets = {}
        expression_sequence = []  # filled in once the model loader finishes

        def compute_expression_sequence(available_names):
            # Each entry is a step dict: {"display", "log", "pose"} where pose is
            # already resolved to this model's blendshape names.
            if CALIBRATION_ROUTINE is None:
                # Fallback: sweep every loaded blendshape alphabetically
                return [{"display": name, "log": name, "pose": {name: 1.0}}
                        for name in available_names]
            available = set(available_names)
            sequence = []
            for step in CALIBRATION_ROUTINE:
                pose = resolve_pose(step["pose"])
                missing = [s for s in pose if s not in available]
                if missing:
                    print(f"Warning: calibration step '{step['display']}' skipped, "
                          f"missing blendshapes: {missing}")
                    continue
                sequence.append({**step, "pose": pose})
            return sequence

        if EXPRESSION_UI and model_loader is not None:
            expression_ui = ConfirmationBox(position=UI_DIALOG_POSITION)
            # phase: None (waiting at the dialog), "prep" (get ready), "hold"
            # (user holds the expression), "relax" (user returns to neutral).
            # Hold and relax each log their own start/end record — "relax"
            # under the name "Neutral", and practice reps under a "Practice_"
            # prefix so they are easy to filter downstream.
            # Realtime audio cues: a state-following child process (own GIL).
            # Every call is fail-safe; the sequence never depends on audio.
            try:
                from cue_audio import CueAudio
                cues = CueAudio()
            except Exception as exc:
                print(f"[cue_audio] unavailable: {exc}")
                class _NoCues:
                    def __getattr__(self, _name):
                        return lambda *a, **k: None
                cues = _NoCues()
            self._cues = cues

            sequence_state = {"index": 0, "phase": None, "phase_until": None,
                              "phase_seconds": 0.0, "phase_seconds_shown": None,
                              "phase_start_ns": None, "practice": False,
                              "freeform_next_shuffle": 0.0,
                              "alt_on": False, "alt_segment_start_ns": None,
                              "alt_lean_id": None}
            onboarding_state = {"index": 0,
                                "active": bool(ONBOARDING and ONBOARDING_STEPS),
                                "pending_step": None,
                                "start_flow_when_ready": False}

            def show_expression():
                step = expression_sequence[sequence_state["index"]]
                expression_ui.set_label(f"{step['display']}  ({sequence_state['index'] + 1}/{len(expression_sequence)})")
                expression_targets.clear()
                expression_targets.update(step["pose"])
                play_narration(step["log"])
                print(f"[expression] pose {step['log']}: {step['pose']}")

            def show_done():
                expression_ui.set_label("All done — thank you!")
                expression_ui.set_buttons(("Restart", start_expression_flow), ("Exit", on_exit))
                expression_targets.clear()  # back to a relaxed face
                cues.bed(False)
                cues.mark("end")
                play_narration("done")
                print("[expression] sequence complete")

            def show_freeform_intro():
                expression_targets.clear()
                expression_ui.set_label(FREEFORM_INTRO_LABEL, big=True, cover=True)
                expression_ui.set_buttons(("Let's go!", on_freeform_start),
                                          ("Skip", show_done))
                # "freeform_ready", not "freeform_intro": that name belongs to
                # the onboarding pane, which has different wording
                play_narration("freeform_ready")
                print("[expression] freeform intro")

            def on_freeform_start():
                start_phase("freeform", FREEFORM_SECONDS)
                expression_ui.set_buttons()
                sequence_state["freeform_next_shuffle"] = 0.0
                cues.mark("start")
                play_narration("freeform_start")

            def start_phase(phase, seconds):
                sequence_state["phase"] = phase
                sequence_state["phase_seconds"] = seconds
                sequence_state["phase_until"] = time.monotonic() + seconds
                sequence_state["phase_seconds_shown"] = None
                sequence_state["phase_start_ns"] = time.time_ns()

            def log_record(shape_name, start_ns, end_ns):
                if sequence_state["practice"]:
                    shape_name = "Practice_" + shape_name
                append_expression_record(expression_log_path, start_ns, end_ns, shape_name)
                print(f"[log] recorded: {shape_name} ({(end_ns - start_ns) / 1e9:.2f}s)")

            def log_phase(shape_name):
                log_record(shape_name, sequence_state["phase_start_ns"], time.time_ns())

            def current_step():
                return expression_sequence[sequence_state["index"]]

            def begin_hold_phase():
                # Prep finished: "alternate" steps cue expression/neutral in
                # time with the head model; everything else is a plain hold
                step = current_step()
                cues.bed(True)
                if step.get("mode") == "alternate":
                    # One extra cycle up front: the wearer is still reading and
                    # transitioning during the first ON segment, so it is
                    # logged as "<log>_warmup" for ingestion to discard, and
                    # the phase is lengthened so the usable data is unchanged.
                    period = step.get("period", ALTERNATE_PERIOD_SECONDS)
                    start_phase("alternate", step.get("seconds", ALTERNATE_SECONDS) + period)
                    sequence_state["alt_on"] = False
                    sequence_state["alt_segment_start_ns"] = None
                    sequence_state["alt_lean_id"] = None
                    sequence_state["alt_segments_logged"] = 0
                    set_alternate_state(True)
                else:
                    start_phase("hold", step.get("hold", EXPRESSION_HOLD_SECONDS))
                    cues.target(0.70 if step.get("audio_invert", False) else 0.30, 0.10)

            def alternate_label(seconds):
                step = current_step()
                cue = step.get("cue_on", step["display"]) if sequence_state["alt_on"] \
                    else step.get("cue_off", "Relax — neutral face")
                if sequence_state.get("alt_segments_logged", 1) == 0:
                    cue = f"Practice: {cue.lower()}"   # warmup cycle, not recorded as data
                return f"{cue}  ({seconds})"

            def log_alternate_segment(end_ns):
                start_ns = sequence_state["alt_segment_start_ns"]
                if start_ns is None:
                    return
                name = current_step()["log"] if sequence_state["alt_on"] else "Neutral_alt"
                seg_index = sequence_state.get("alt_segments_logged", 0)
                sequence_state["alt_segments_logged"] = seg_index + 1
                if seg_index == 0 and sequence_state["alt_on"]:
                    name += "_warmup"   # first ON segment: reading/transition, discard
                log_record(name, start_ns, end_ns)

            def set_alternate_state(on):
                # Flip the cue: log the segment that just ended, move the head
                # model (and the wearer) to the new state, refresh the label
                now_ns = time.time_ns()
                log_alternate_segment(now_ns)
                sequence_state["alt_on"] = on
                sequence_state["alt_segment_start_ns"] = now_ns
                inv = current_step().get("audio_invert", False)
                cues.target((0.0 if on else 1.0) if inv else (1.0 if on else 0.0),
                            0.05)  # land on the actual flip
                expression_targets.clear()
                if on:
                    expression_targets.update(current_step()["pose"])
                remaining = sequence_state["phase_until"] - time.monotonic()
                expression_ui.set_label(alternate_label(max(1, math.ceil(remaining))))

            def finish_alternate():
                log_alternate_segment(time.time_ns())
                sequence_state["alt_segment_start_ns"] = None
                expression_targets.clear()
                cues.bed(False)
                cues.mark("end")
                next_step()

            def on_ready():
                # Safety net: recording must be live before any labeled hold
                # (normally the welcome pane's first press already started it)
                ensure_recording_started()
                # Short get-ready pause so the transition onto the expression
                # isn't logged as part of the hold; buttons hide so a stray
                # trigger pull can't skip ahead mid-hold
                start_phase("prep", EXPRESSION_PREP_SECONDS)
                expression_ui.set_buttons()
                cues.bed(False)
                cues.mark("thunk")
                cues.mark("start")       # rising ding in silence; bed enters at the hold

            def begin_relax_phase():
                # Expression hold finished: log it, then capture the return to
                # neutral — the model relaxes too so the user can mirror it
                log_phase(current_step()["log"])
                start_phase("relax", EXPRESSION_RELAX_SECONDS)
                expression_targets.clear()
                cues.bed(False)
                cues.mark("end")

            def advance_expression():
                log_phase("Neutral")
                next_step()

            def next_step():
                sequence_state["phase"] = None
                sequence_state["phase_start_ns"] = None
                if sequence_state["practice"]:
                    # Practice rep complete: hand over to the real sequence
                    sequence_state["practice"] = False
                    expression_targets.clear()
                    expression_ui.set_label("Nice work! That was just practice.\n"
                                            "Now we record the real thing.", big=True, cover=True)
                    expression_ui.set_buttons(("Start", start_expression_flow),
                                              ("Exit", on_exit))
                    play_narration("practice_done")
                    return
                sequence_state["index"] += 1
                if sequence_state["index"] >= len(expression_sequence):
                    show_freeform_intro()
                else:
                    expression_ui.set_buttons(("Ready", on_ready), ("Back", on_back))
                    show_expression()

            def on_back():
                sequence_state["index"] = max(0, sequence_state["index"] - 1)
                cues.bed(False)  # never leave the bed running outside a phase
                show_expression()

            def on_exit():
                print("[expression] exit requested from dialog")
                glfw.set_window_should_close(self.window, True)

            def start_expression_flow():
                onboarding_state["active"] = False
                sequence_state["index"] = 0
                sequence_state["practice"] = False
                expression_ui.set_buttons(("Ready", on_ready), ("Back", on_back))
                show_expression()

            def on_onboarding_next():
                # First press on the welcome pane: bring the cameras up now,
                # while the user reads — the load-time hitching is behind us
                ensure_recording_started()
                enter_onboarding_step(onboarding_state["index"] + 1)

            def on_onboarding_back():
                enter_onboarding_step(max(0, onboarding_state["index"] - 1))

            def finish_onboarding():
                onboarding_state["active"] = False
                onboarding_state["pending_step"] = None
                if model_ready and expression_sequence:
                    start_expression_flow()
                else:
                    # Model still loading (e.g. the intro was skipped quickly);
                    # the main loop starts the flow once it's ready
                    expression_ui.set_label("Loading the head model...", big=True, cover=True)
                    expression_ui.set_buttons()
                    onboarding_state["start_flow_when_ready"] = True

            def enter_onboarding_step(index):
                if index >= len(ONBOARDING_STEPS):
                    finish_onboarding()
                    return
                step = ONBOARDING_STEPS[index]
                if (step.get("demo") or step.get("practice")) and not model_ready:
                    # Park until the background load delivers the head model;
                    # the main loop re-enters this step when it's ready
                    onboarding_state["pending_step"] = index
                    expression_ui.set_label("One moment, preparing\nthe demonstration...",
                                            big=True, cover=True)
                    expression_ui.set_buttons()
                    return
                onboarding_state["index"] = index
                onboarding_state["pending_step"] = None
                sequence_state["practice"] = False
                expression_targets.clear()
                play_narration(step["name"])
                if step.get("practice"):
                    if not expression_sequence:
                        finish_onboarding()
                        return
                    practice_step = expression_sequence[0]
                    sequence_state["practice"] = True
                    sequence_state["index"] = 0
                    expression_targets.update(practice_step["pose"])  # the model holds the face
                    expression_ui.set_label(
                        step["label"].format(
                            name=practice_step.get("short", practice_step["display"])),
                        big=True)
                    expression_ui.set_buttons(("Ready", on_ready),
                                              ("Back", on_onboarding_back))
                    return
                expression_ui.set_label(step["label"].format(freeform_seconds=FREEFORM_SECONDS),
                                        big=True, cover=not step.get("demo"))
                actions = {"next": on_onboarding_next, "back": on_onboarding_back,
                           "skip": finish_onboarding, "exit": on_exit}
                expression_ui.set_buttons(*((text, actions[action])
                                            for text, action in step["buttons"]))

            if onboarding_state["active"]:
                enter_onboarding_step(0)
            else:
                finish_onboarding()
        else:
            # No expression UI (demo mode): there is no button press to wait
            # for, so record from launch like before
            ensure_recording_started()

        def draw_sub_mesh(sub_mesh):
            """Binds the sub-mesh's full material (4 texture units + scalars),
            sets its morph uniforms (count 0 for rigid meshes) and draws."""
            active = sub_mesh.morph_active_slots
            if sub_mesh.morph_tex is not None and len(active):
                gl.glActiveTexture(gl.GL_TEXTURE5)
                gl.glBindTexture(gl.GL_TEXTURE_2D, sub_mesh.morph_tex)
                gl.glUniform1i(morph_count_loc, len(active))
                gl.glUniform1i(morph_rows_loc, sub_mesh.morph_rows_per_shape)
                gl.glUniform1iv(morph_slots_loc, len(active), active)
                gl.glUniform1fv(morph_weights_loc, len(active), sub_mesh.morph_active_weights)
            else:
                gl.glUniform1i(morph_count_loc, 0)
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, sub_mesh.texture_id)
            gl.glActiveTexture(gl.GL_TEXTURE1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, sub_mesh.normal_tex)
            gl.glActiveTexture(gl.GL_TEXTURE2)
            gl.glBindTexture(gl.GL_TEXTURE_2D, sub_mesh.metallic_tex)
            gl.glActiveTexture(gl.GL_TEXTURE3)
            gl.glBindTexture(gl.GL_TEXTURE_2D, sub_mesh.ao_tex)
            gl.glActiveTexture(gl.GL_TEXTURE4)
            gl.glBindTexture(gl.GL_TEXTURE_2D, sub_mesh.emission_tex)
            gl.glUniform1f(bump_scale_loc, sub_mesh.bump_scale)
            gl.glUniform1f(metallic_factor_loc, sub_mesh.metallic_factor)
            gl.glUniform1f(smoothness_loc, sub_mesh.smoothness)
            gl.glUniform1f(occlusion_loc, sub_mesh.occlusion_strength)
            gl.glUniform3f(emission_color_loc, *sub_mesh.emission_color)
            gl.glBindVertexArray(sub_mesh.vao)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, sub_mesh.vertex_count)

        # Controller cone: shared geometry, color swaps when the trigger is held
        cone_vao, cone_vbo, cone_vertex_count = create_interleaved_vao(
            create_cone_mesh(length=CONE_LENGTH))
        cone_texture_idle = create_solid_color_texture([0.85, 0.85, 0.9])
        cone_texture_pressed = create_solid_color_texture([1.0, 0.45, 0.1])

        # Laser beam: a unit cube stretched along -Z each frame, from the cone
        # tip to the hovered button (or LASER_LENGTH into the distance)
        laser_vao, _, laser_vertex_count = create_interleaved_vao(create_box_mesh())
        laser_texture_idle = create_solid_color_texture([0.45, 0.7, 1.0])
        laser_texture_pressed = create_solid_color_texture([1.0, 0.45, 0.1])

        # The OBJ model is static at 1.5m in front of the stage origin
        obj_model_matrix = np.identity(4, dtype=np.float32)
        obj_model_matrix[3, 2] = -1.5

        session_running = False
        fps_frames = 0
        fps_window_start = time.monotonic()

        # Main Event Loop
        while not glfw.window_should_close(self.window):
            glfw.poll_events()

            # Poll OpenXR events
            while True:
                try:
                    event = xr.poll_event(self.instance)
                except xr.exception.EventUnavailable:
                    break  # Exit the loop cleanly when no more events are available

                if event.type == xr.StructureType.EVENT_DATA_SESSION_STATE_CHANGED:
                    state_changed = ctypes.cast(
                        ctypes.byref(event),
                        ctypes.POINTER(xr.EventDataSessionStateChanged)
                    ).contents

                    if state_changed.state == xr.SessionState.READY:
                        xr.begin_session(
                            self.session,
                            xr.SessionBeginInfo(xr.ViewConfigurationType.PRIMARY_STEREO)
                        )
                        session_running = True
                    elif state_changed.state == xr.SessionState.STOPPING:
                        xr.end_session(self.session)
                        session_running = False
                    elif state_changed.state in (xr.SessionState.EXITING,
                                                 xr.SessionState.LOSS_PENDING):
                        glfw.set_window_should_close(self.window, True)

            if not session_running:
                # Keep loading the model while the XR session spins up
                if model_loader is not None and not model_ready and not model_loader.error:
                    model_loader.pump_gl(budget_seconds=0.04)
                time.sleep(0.05)  # Don't busy-spin while the session is idle
                continue

            # Frame setup
            frame_state = xr.wait_frame(self.session)
            xr.begin_frame(self.session)

            # Sync controller poses and fire button callbacks
            self.poll_input(frame_state.predicted_display_time)

            # Drain a small slice of the background model load each frame;
            # when the last task completes, wire the model into the scene
            if model_loader is not None and not model_ready:
                if model_loader.error:
                    print(f"[ERROR] Head model failed to load:\n{model_loader.error}")
                    if expression_ui is not None:
                        expression_ui.set_label("The head model failed to load.\n"
                                                "Check the console output.", big=True, cover=True)
                        expression_ui.set_buttons(("Exit", on_exit))
                        onboarding_state["active"] = False
                        onboarding_state["start_flow_when_ready"] = False
                    model_loader = None  # reported; keep the app/UI alive
                elif model_loader.pump_gl():
                    model_ready = True
                    sub_meshes = model_loader.sub_meshes
                    shape_names = model_loader.shape_names
                    self.morph_sub_meshes = [sm for sm in sub_meshes if sm.deltas]
                    opaque_meshes = [sm for sm in sub_meshes if not sm.transparent]
                    transparent_meshes = [sm for sm in sub_meshes if sm.transparent]
                    print(f"Blendshapes available: {shape_names}")
                    expression_sequence[:] = compute_expression_sequence(shape_names)
                    if expression_ui is not None:
                        placement = dialog_position_below_chin(
                            self.morph_sub_meshes, obj_model_matrix)
                        if placement is not None:
                            position, label_top, cover_center = placement
                            expression_ui.position = position
                            expression_ui.anchor_top = label_top
                            if cover_center is not None:
                                expression_ui.cover_center = cover_center
                            print(f"Dialog placed from mesh: label top y="
                                  f"{label_top:.3f}, cover center y="
                                  f"{expression_ui.cover_center:.3f}")
                    if expression_ui is not None and not expression_sequence:
                        print("Warning: no usable target expressions, expression dialog disabled")
                        expression_ui.set_label("No target expressions available", big=True, cover=True)
                        expression_ui.set_buttons(("Exit", on_exit))
                        onboarding_state["active"] = False
                        onboarding_state["start_flow_when_ready"] = False

            layers = []
            if frame_state.should_render:
                if expression_ui is not None:
                    now = time.monotonic()

                    # Onboarding bookkeeping: resume a step that was waiting on
                    # the model, start the main flow once it's ready, and drive
                    # the demo pane's example expression
                    if onboarding_state["pending_step"] is not None and model_ready:
                        enter_onboarding_step(onboarding_state["pending_step"])
                    if (onboarding_state["start_flow_when_ready"] and model_ready
                            and expression_sequence):
                        onboarding_state["start_flow_when_ready"] = False
                        start_expression_flow()
                    if (onboarding_state["active"]
                            and onboarding_state["pending_step"] is None
                            and ONBOARDING_STEPS[onboarding_state["index"]].get("demo")
                            and expression_sequence):
                        demo_pose = expression_sequence[0]["pose"]
                        expression_targets.clear()
                        scale = math.sin(math.pi * (now % 2.0) / 2.0)
                        for shape, weight in demo_pose.items():
                            expression_targets[shape] = weight * scale

                    # During freeform the model riffs along: a fresh random mix
                    # of blendshapes every shuffle, to keep inspiring movement
                    if sequence_state["phase"] == "freeform" and shape_names:
                        if now >= sequence_state["freeform_next_shuffle"]:
                            sequence_state["freeform_next_shuffle"] = now + FREEFORM_SHUFFLE_SECONDS
                            expression_targets.clear()
                            riff_shapes = [s for s in shape_names
                                           if FREEFORM_SHAPE_PREFIXES is None
                                           or s.startswith(FREEFORM_SHAPE_PREFIXES)] or shape_names
                            for shape in random.sample(riff_shapes, k=min(3, len(riff_shapes))):
                                expression_targets[shape] = random.uniform(0.3, 1.0)

                    # Timed phases after Ready: get ready, hold the expression,
                    # then relax to neutral; freeform runs once at the end of
                    # the sequence. Labels update once per second.
                    if sequence_state["phase"] is not None:
                        remaining = sequence_state["phase_until"] - now
                        if remaining > 0 and sequence_state["phase"] == "alternate":
                            # Steady metronome: first half of each cycle is the
                            # expression, second half neutral
                            period = current_step().get("period", ALTERNATE_PERIOD_SECONDS)
                            elapsed = sequence_state["phase_seconds"] - remaining
                            cue_on = (elapsed % period) < period / 2
                            # Audio leans into the predicted flip 100 ms early
                            # and lands only when the flip actually happens —
                            # if the loop hitches, the audio stretches with it
                            half = period / 2
                            flip_id = int(elapsed // half) + 1
                            to_flip = flip_id * half - elapsed
                            if to_flip <= 0.10 and sequence_state["alt_lean_id"] != flip_id:
                                sequence_state["alt_lean_id"] = flip_id
                                lean_to = 1.0 if flip_id % 2 == 0 else 0.0
                                if current_step().get("audio_invert", False):
                                    lean_to = 1.0 - lean_to
                                cues.lean(lean_to, now + to_flip)
                            if cue_on != sequence_state["alt_on"]:
                                set_alternate_state(cue_on)
                        if remaining <= 0:
                            if sequence_state["phase"] == "prep":
                                begin_hold_phase()
                            elif sequence_state["phase"] == "hold":
                                begin_relax_phase()
                            elif sequence_state["phase"] == "alternate":
                                finish_alternate()
                            elif sequence_state["phase"] == "freeform":
                                log_phase("Freeform")
                                sequence_state["phase"] = None
                                sequence_state["phase_start_ns"] = None
                                show_done()
                            else:
                                advance_expression()
                        else:
                            seconds = math.ceil(remaining)
                            if seconds != sequence_state["phase_seconds_shown"]:
                                sequence_state["phase_seconds_shown"] = seconds
                                if sequence_state["phase"] == "freeform":
                                    elapsed = FREEFORM_SECONDS - remaining
                                    prompt = FREEFORM_PROMPTS[
                                        int(elapsed // FREEFORM_PROMPT_SECONDS) % len(FREEFORM_PROMPTS)]
                                    expression_ui.set_label(f"{prompt} ({seconds})")
                                elif sequence_state["phase"] == "alternate":
                                    expression_ui.set_label(alternate_label(seconds))
                                else:
                                    if sequence_state["phase"] == "hold":
                                        # Stepped countdown: up to 5 notches,
                                        # fully open (or fully closed, for
                                        # audio-inverted steps) = boundary next
                                        total = max(int(sequence_state["phase_seconds"]), 1)
                                        notches = min(5, total)
                                        if seconds <= notches:
                                            idx = notches - seconds
                                            level = 0.30 + 0.62 * idx / max(notches - 1, 1)
                                            if current_step().get("audio_invert", False):
                                                level = 1.0 - level
                                            cues.target(level, 0.08)
                                    hold_text = current_step().get("hold_label", "Hold that expression!")
                                    phase_labels = {
                                        "prep": f"Get ready... ({seconds})",
                                        "hold": f"{hold_text} ({seconds})",
                                        "relax": f"Relax your face ({seconds})",
                                    }
                                    expression_ui.set_label(phase_labels[sequence_state["phase"]])

                    # Hover/trigger interaction with the dialog: near-field cone
                    # overlap or far-field laser pointing both count as hover
                    probes = [self.controller_probe_points(hand)
                              for hand in (HAND_LEFT, HAND_RIGHT)]
                    rays = [self.controller_ray(hand)
                            for hand in (HAND_LEFT, HAND_RIGHT)]
                    expression_ui.update(probes, rays, self.trigger_pressed_edges)

                    # Ease the face toward the current target expression
                    dt = min(now - getattr(self, "_last_anim_time", now), 0.1)
                    self._last_anim_time = now
                    blend = min(1.0, dt * 8.0)
                    current = self.blendshape_coefficients
                    new_weights = {}
                    for name in set(current) | set(expression_targets):
                        target = expression_targets.get(name, 0.0)
                        value = current.get(name, 0.0)
                        value = target if abs(target - value) < 0.005 else value + (target - value) * blend
                        if abs(value) > 1e-4:
                            new_weights[name] = value
                    if new_weights != current:
                        self.set_blendshape_coefficients(new_weights)
                elif DEMO_ANIMATE_BLENDSHAPES and shape_names:
                    # Fallback demo: sweep each blendshape 0 -> 1 -> 0 over 2s
                    t = time.monotonic()
                    shape = shape_names[int(t / 2.0) % len(shape_names)]
                    if shape != getattr(self, "_demo_shape", None):
                        self._demo_shape = shape
                        print(f"[demo] animating blendshape: {shape}")
                    self.set_blendshape_coefficients({shape: math.sin(math.pi * (t % 2.0) / 2.0)})
                self.update_blendshapes()

                # Locate views (get eye positions and projections)
                view_state, views = xr.locate_views(
                    self.session,
                    xr.ViewLocateInfo(
                        view_configuration_type=xr.ViewConfigurationType.PRIMARY_STEREO,
                        display_time=frame_state.predicted_display_time,
                        space=self.play_space
                    )
                )

                projection_layers = []

                # One sky-animation timestamp per frame so both eyes render
                # the identical aurora/star state (stereo-consistent)
                sky_time = time.monotonic() - sky_start_time

                # Render each eye
                for i, (swapchain, images, view_config) in enumerate(self.swapchains):
                    image_index = xr.acquire_swapchain_image(swapchain)

                    wait_info = xr.SwapchainImageWaitInfo(timeout=xr.INFINITE_DURATION)
                    xr.wait_swapchain_image(swapchain, wait_info)

                    # 1. Bind this eye's render target: the multisampled FBO
                    # when MSAA is on (resolved into the swapchain texture
                    # after drawing), else the swapchain texture directly
                    if self.msaa_fbos:
                        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.msaa_fbos[i])
                    else:
                        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
                        gl.glFramebufferTexture2D(
                            gl.GL_FRAMEBUFFER,
                            gl.GL_COLOR_ATTACHMENT0,
                            gl.GL_TEXTURE_2D,
                            images[image_index].image,
                            0
                        )
                        # Attach the corresponding eye's depth buffer
                        gl.glFramebufferRenderbuffer(
                            gl.GL_FRAMEBUFFER,
                            gl.GL_DEPTH_ATTACHMENT,
                            gl.GL_RENDERBUFFER,
                            self.depth_buffers[i]
                        )

                    # 2. Set viewport size and clear the render target
                    # (alpha 0: with ALPHA_BLEND, alpha is what keeps the
                    # passthrough cameras visible behind the scene)
                    gl.glViewport(0, 0, view_config.recommended_image_rect_width, view_config.recommended_image_rect_height)
                    gl.glClearColor(0.0, 0.0, 0.0, 0.0)
                    gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)

                    # Projection & View matrices
                    proj_matrix = make_projection_matrix(views[i].fov, near=0.1, far=100.0)
                    view_matrix = make_view_matrix(views[i].pose)

                    # Background pass: night sky + aurora + stars, whose alpha
                    # fades the lower world into passthrough (or floor color)
                    gl.glDisable(gl.GL_DEPTH_TEST)
                    gl.glDepthMask(gl.GL_FALSE)
                    gl.glUseProgram(background_program)
                    gl.glUniform1f(bg_time_loc, sky_time)
                    gl.glUniformMatrix4fv(bg_inv_proj_loc, 1, gl.GL_FALSE,
                                          np.linalg.inv(proj_matrix).astype(np.float32))
                    gl.glUniformMatrix4fv(bg_inv_view_loc, 1, gl.GL_FALSE,
                                          np.linalg.inv(view_matrix).astype(np.float32))
                    gl.glBindVertexArray(background_vao)
                    gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
                    gl.glDepthMask(gl.GL_TRUE)
                    gl.glEnable(gl.GL_DEPTH_TEST)

                    gl.glUseProgram(shader_program)

                    gl.glUniformMatrix4fv(projection_loc, 1, gl.GL_FALSE, proj_matrix)
                    gl.glUniformMatrix4fv(view_loc, 1, gl.GL_FALSE, view_matrix)
                    gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, obj_model_matrix)
                    eye_pos = views[i].pose.position
                    gl.glUniform3f(camera_pos_loc, eye_pos.x, eye_pos.y, eye_pos.z)

                    # --- Pass 1: opaque sub-meshes ---
                    for sub_mesh in opaque_meshes:
                        draw_sub_mesh(sub_mesh)
                    # ---------------------------------

                    # --- Draw a cone at each tracked controller ---
                    # Rigid geometry: no morphing from here on this frame
                    gl.glUniform1i(morph_count_loc, 0)
                    # Neutral maps/scalars: lightly glossy solid plastic
                    gl.glActiveTexture(gl.GL_TEXTURE1)
                    gl.glBindTexture(gl.GL_TEXTURE_2D, default_normal)
                    gl.glActiveTexture(gl.GL_TEXTURE2)
                    gl.glBindTexture(gl.GL_TEXTURE_2D, default_white)
                    gl.glActiveTexture(gl.GL_TEXTURE3)
                    gl.glBindTexture(gl.GL_TEXTURE_2D, default_white)
                    gl.glActiveTexture(gl.GL_TEXTURE4)
                    gl.glBindTexture(gl.GL_TEXTURE_2D, default_black)
                    gl.glUniform1f(bump_scale_loc, 1.0)
                    gl.glUniform1f(metallic_factor_loc, 0.0)
                    gl.glUniform1f(smoothness_loc, 0.4)
                    gl.glUniform1f(occlusion_loc, 0.0)
                    gl.glUniform3f(emission_color_loc, 0.0, 0.0, 0.0)
                    gl.glActiveTexture(gl.GL_TEXTURE0)
                    gl.glBindVertexArray(cone_vao)
                    for hand in (HAND_LEFT, HAND_RIGHT):
                        controller_matrix = self.controller_matrices[hand]
                        if controller_matrix is None:
                            continue
                        gl.glBindTexture(
                            gl.GL_TEXTURE_2D,
                            cone_texture_pressed if self.trigger_held[hand] else cone_texture_idle
                        )
                        gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, controller_matrix)
                        gl.glDrawArrays(gl.GL_TRIANGLES, 0, cone_vertex_count)

                    # Laser beams: start at the cone tip and end at the hovered
                    # button (slightly short of its face to avoid z-fighting),
                    # or LASER_LENGTH out when pointing at nothing. Rendered
                    # UNLIT (emission-only: black base kills diffuse/ambient,
                    # smoothness 0 kills specular) so the beam color is
                    # constant from every angle, like a glowing laser
                    gl.glBindTexture(gl.GL_TEXTURE_2D, default_black)  # unit 0: base
                    gl.glUniform1f(smoothness_loc, 0.0)
                    gl.glUniform3f(emission_color_loc, 1.0, 1.0, 1.0)
                    gl.glActiveTexture(gl.GL_TEXTURE4)  # beam color via emission
                    gl.glBindVertexArray(laser_vao)
                    for hand in (HAND_LEFT, HAND_RIGHT):
                        controller_matrix = self.controller_matrices[hand]
                        if controller_matrix is None:
                            continue
                        hit = (expression_ui.ray_hit_distances[hand]
                               if expression_ui is not None else None)
                        end = LASER_LENGTH if hit is None else hit - 0.005
                        seg_length = max(end - CONE_LENGTH, 0.01)
                        beam = np.identity(4, dtype=np.float32)
                        beam[0, 0] = beam[1, 1] = LASER_WIDTH
                        beam[2, 2] = seg_length
                        beam[3, 2] = -(CONE_LENGTH + seg_length / 2)
                        gl.glBindTexture(
                            gl.GL_TEXTURE_2D,
                            laser_texture_pressed if self.trigger_held[hand] else laser_texture_idle
                        )
                        gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE,
                                              beam @ controller_matrix)
                        gl.glDrawArrays(gl.GL_TRIANGLES, 0, laser_vertex_count)
                    # Restore the neutral material the dialog draw expects:
                    # black emission on unit 4, unit 0 active, cone scalars
                    gl.glBindTexture(gl.GL_TEXTURE_2D, default_black)
                    gl.glUniform3f(emission_color_loc, 0.0, 0.0, 0.0)
                    gl.glUniform1f(smoothness_loc, 0.4)
                    gl.glActiveTexture(gl.GL_TEXTURE0)
                    # ----------------------------------------------

                    # --- Draw the expression dialog (reuses the cones' neutral
                    # material bindings on units 1-4) ---
                    if expression_ui is not None:
                        expression_ui.draw(model_loc)
                    # ------------------------------------------------------

                    # --- Pass 2: transparent sub-meshes (eyelashes etc.) ---
                    # Alpha-blended over the opaque scene; depth test still on so
                    # they hide behind the head, but depth writes off so soft
                    # edges don't punch holes in each other.
                    if transparent_meshes:
                        gl.glEnable(gl.GL_BLEND)
                        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
                        gl.glDepthMask(gl.GL_FALSE)
                        gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, obj_model_matrix)
                        for sub_mesh in transparent_meshes:
                            draw_sub_mesh(sub_mesh)
                        gl.glDepthMask(gl.GL_TRUE)
                        gl.glDisable(gl.GL_BLEND)
                    # -------------------------------------------------------

                    if self.msaa_fbos:
                        # Resolve the multisampled render into the swapchain
                        # texture (averages color AND alpha, so silhouette
                        # edges also blend smoothly against passthrough)
                        width = view_config.recommended_image_rect_width
                        height = view_config.recommended_image_rect_height
                        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self.fbo)
                        gl.glFramebufferTexture2D(
                            gl.GL_DRAW_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                            gl.GL_TEXTURE_2D, images[image_index].image, 0)
                        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self.msaa_fbos[i])
                        gl.glBlitFramebuffer(0, 0, width, height, 0, 0, width, height,
                                             gl.GL_COLOR_BUFFER_BIT, gl.GL_NEAREST)
                    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)

                    xr.release_swapchain_image(swapchain)

                    # Define projection layer for composition
                    projection_layers.append(xr.CompositionLayerProjectionView(
                        pose=views[i].pose,
                        fov=views[i].fov,
                        sub_image=xr.SwapchainSubImage(
                            swapchain=swapchain,
                            image_rect=xr.Rect2Di(
                                offset=xr.Offset2Di(x=0, y=0),
                                extent=xr.Extent2Di(
                                    width=view_config.recommended_image_rect_width,
                                    height=view_config.recommended_image_rect_height
                                )
                            ),
                            image_array_index=0
                        )
                    ))

                # Submit frame. Our shaders write straight (unpremultiplied)
                # alpha, so tell the compositor when it will be blending.
                layer = xr.CompositionLayerProjection(
                    layer_flags=(xr.CompositionLayerFlags.UNPREMULTIPLIED_ALPHA_BIT
                                 if passthrough_active else 0),
                    space=self.play_space,
                    views=projection_layers
                )
                layers = [ctypes.byref(layer)]

            xr.end_frame(
                self.session,
                xr.FrameEndInfo(
                    display_time=frame_state.predicted_display_time,
                    environment_blend_mode=blend_mode,
                    layers=layers
                )
            )

            if FPS_LOG:
                fps_frames += 1
                fps_now = time.monotonic()
                if fps_now - fps_window_start >= 5.0:
                    print(f"[fps] {fps_frames / (fps_now - fps_window_start):.1f} "
                          f"avg over {fps_now - fps_window_start:.1f}s")
                    fps_frames = 0
                    fps_window_start = fps_now

        # Cleanup
        if getattr(self, "_cues", None) is not None:
            self._cues.stop()
        if self.session is not None:
            xr.destroy_session(self.session)
        if self.instance is not None:
            xr.destroy_instance(self.instance)
        glfw.terminate()

if __name__ == "__main__":
    # Keep the whole app (and every child: recorder, ffmpeg, cue audio) off
    # the A520 cores (0-1): they lack some ISA extensions and are reserved
    # for system responsiveness.
    try:
        os.sched_setaffinity(0, set(range(2, 8)))
    except OSError:
        pass
    # A mouth calibration routine cannot run without the lower-port mouth
    # tracker: its expressions are invisible to the eye cameras and every
    # face-labelled record would be garbage. Fail before any UI comes up.
    if _routine.get("needs_mouth_tracker"):
        import camera_setup
        # verify_stream: the FPGA VCINT state can be set in software with no
        # cameras behind it — only a delivered frame proves the tracker
        if not camera_setup.mouth_tracker_present(verify_stream=True):
            raise SystemExit(
                f"[error] ACTIVE_ROUTINE={ACTIVE_ROUTINE!r} needs the mouth tracker "
                "(lower-port camera accessory), but it is absent or its video "
                "feed delivers no frames. Attach the tracker (and load the "
                "stereo subdev module) or set ACTIVE_ROUTINE = 'eye'.")
    app = OpenXRApp()
    app.run()
