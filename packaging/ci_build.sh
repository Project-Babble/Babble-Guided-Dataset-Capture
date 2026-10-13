#!/usr/bin/env bash
# Release build for the Steam Frame (aarch64), run as root inside a
# debian:bookworm container (GitHub Actions, or locally:
#   docker run --rm -v "$PWD:/src" -w /src debian:bookworm packaging/ci_build.sh).
# bookworm's glibc (2.36) is older than SteamOS's, so the binary runs there.
# Produces dist/ (see packaging/make_release.sh).
set -euo pipefail

KTX_VERSION=4.4.2
KTX_DEB_SHA256=${KTX_DEB_SHA256:-}   # pinned below after first verification

export DEBIAN_FRONTEND=noninteractive
dpkg --add-architecture arm64
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    ca-certificates curl git git-lfs file xz-utils \
    cmake ninja-build pkg-config glslc python3 python3-numpy python3-pil \
    g++-aarch64-linux-gnu libc6-dev-arm64-cross \
    libvulkan-dev:arm64 libpipewire-0.3-dev:arm64 libx11-dev:arm64 >/dev/null

# KTX-Software (toktx) for ASTC texture compression; host tool only
if ! command -v toktx >/dev/null; then
    deb=/tmp/ktx.deb
    curl -fsSL -o "$deb" \
        "https://github.com/KhronosGroup/KTX-Software/releases/download/v$KTX_VERSION/KTX-Software-$KTX_VERSION-Linux-x86_64.deb"
    expected_sha1=$(curl -fsSL "https://github.com/KhronosGroup/KTX-Software/releases/download/v$KTX_VERSION/KTX-Software-$KTX_VERSION-Linux-x86_64.deb.sha1" | cut -d' ' -f1)
    echo "$expected_sha1  $deb" | sha1sum -c -
    if [ -n "$KTX_DEB_SHA256" ]; then echo "$KTX_DEB_SHA256  $deb" | sha256sum -c -; fi
    apt-get install -y -qq "$deb" >/dev/null
fi

git config --global --add safe.directory "$PWD" || true

python3 tools/convert_assets.py --format ktx2
cmake -S . -B build-arm64 -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE=cmake/aarch64-linux-gnu.cmake -DCMAKE_BUILD_TYPE=Release -DBUILD_APP_TESTS=OFF
cmake --build build-arm64
file build-arm64/babble-dataset-capture
packaging/make_release.sh build-arm64 linuxarm64
