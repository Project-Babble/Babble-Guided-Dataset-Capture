# Babble Dataset Capture

Guided face-expression capture for the Steam Frame, installed and launched as
an optional component by Babble Frame Runtime. The app shows a head
demonstrating each expression, walks the wearer through a routine with audio
cues, and sends segment labels to the runtime, which records its own cameras.


## Build (desktop development)

```sh
git lfs pull
python tools/convert_assets.py              # PNG textures unless toktx is installed
cmake -S . -B build -G Ninja && cmake --build build
```

`cmake --install build --component app --prefix stage` produces the release
layout: `manifest.json`, `bin/babble-dataset-capture` and `assets/`.

## Release (Steam Frame, aarch64)

CI (`.github/workflows/release.yml`) cross-compiles in a `debian:bookworm`
container with `packaging/ci_build.sh`, converting textures to ASTC. A `v*` tag
whose version matches `project(VERSION)` in `CMakeLists.txt` publishes a
release with the tarball.

```sh
docker run --rm -v "$PWD:/src" -w /src debian:bookworm packaging/ci_build.sh
```

## Run

The runtime launches the app with `BABBLE_CAPTURE_SOCKET` set. Without the
runtime, use the mock:

```sh
python tools/mock_runtime.py --socket /run/user/$UID/bdc.sock &
BABBLE_CAPTURE_SOCKET=/run/user/$UID/bdc.sock stage/bin/babble-dataset-capture
```

Settings (environment): `FRAME_EYES_ROUTINE=eye|mouth` (initial routine),
`FRAME_EYES_MSAA`, `FRAME_EYES_RENDER_SCALE`, `FRAME_EYES_SKY=full|lite|off`,
`FRAME_EYES_FPS_LOG=1`, `BABBLE_CAPTURE_ASSETS`. Optional narration clips go
in `assets/narration/<name>.wav`.

## Tests

```sh
python -m unittest discover -s tests       # needs numpy for some; builds build/capture_test for the C++ client tests
build/babble-dataset-capture --audio-selftest cues.wav   # renders the cue sequence to a WAV
```
