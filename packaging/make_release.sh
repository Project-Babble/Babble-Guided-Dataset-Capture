#!/usr/bin/env bash
# Builds the release assets from a CMake build directory:
#   dist/babble-dataset-capture-<version>-linuxarm64.tar.gz
#   dist/babble-dataset-capture-<version>-linuxarm64.tar.gz.sha256  (sha256sum format)
#   dist/manifest.json  (same file as inside the tarball; the runtime reads it
#                        first to pick a release whose protocol matches)
# Usage: packaging/make_release.sh <build-dir> [arch-suffix, default linuxarm64]
set -euo pipefail

build_dir=${1:?usage: make_release.sh <build-dir> [arch]}
arch=${2:-linuxarm64}
repo_root=$(cd "$(dirname "$0")/.." && pwd)
dist="$repo_root/dist"

version=$(sed -n 's/.*"version":"\([^"]*\)".*/\1/p' "$build_dir/manifest.json")
[ -n "$version" ] || { echo "no version in $build_dir/manifest.json" >&2; exit 1; }
[ -f "$repo_root/dist-assets/avatar.bin" ] || { echo "run tools/convert_assets.py first" >&2; exit 1; }

name="babble-dataset-capture-$version-$arch"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
cmake --install "$build_dir" --prefix "$stage/$name" --strip --component app

mkdir -p "$dist"
tarball="$dist/$name.tar.gz"
# Reproducible: fixed order, owner and mtime; contents at the archive root
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='@0' \
    -C "$stage/$name" -czf "$tarball" .
(cd "$dist" && sha256sum "$name.tar.gz" > "$name.tar.gz.sha256")
cp "$stage/$name/manifest.json" "$dist/manifest.json"

echo "release assets in $dist:"
ls -la "$dist"
