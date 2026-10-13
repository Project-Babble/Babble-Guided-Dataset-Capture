"""
Offline asset converter for the C++ app (not shipped). Turns blendshapes/
(~870 MB of OBJ/MTL/PNG/TGA) into a compact release asset directory:

    <out>/avatar.bin          basis mesh + sparse quantised blendshape deltas
    <out>/textures/*.ktx2     ASTC-compressed (when `toktx` is on PATH), or
    <out>/textures/*.png      optimised PNG fallback

    python tools/convert_assets.py                      # -> dist-assets/
    python tools/convert_assets.py --out DIR --format png|ktx2|auto

Requires numpy and Pillow. Mesh data comes from blendshapes/_morph_cache.npz
(--rebuild-cache re-parses the OBJs with tools/obj_morphs.py, needs pywavefront).

avatar.bin layout (little-endian, version 1). Every field starts 4-byte
aligned: str = u16 byte length + UTF-8 bytes + zero padding to 4 bytes.
    char[4] "BBAV"; u32 version
    u32 shape_count;   str shape_name[shape_count]
    u32 texture_count; str texture_path[texture_count]   (relative to <out>)
    u32 material_count; then per material:
        str  name
        i32  base_color_tex, normal_tex, metallic_tex, ao_tex, emission_tex  (-1 = none)
        f32  diffuse[4]
        u32  flags                    (bit 0: transparent, drawn in the blended pass)
        f32  bump_scale, metallic, smoothness, occlusion_strength, emission[3]
        u32  vertex_count; u32 index_count
        vertex[vertex_count]: f32 pos[3]; i16 normal[4] (snorm, w=0); f32 uv[2]
                              (uv is top-left origin: v already flipped for Vulkan)
        u32  index[index_count]       (triangle list)
        u32  morph_count; then per morph:
            u32 shape_index; f32 scale; u32 entry_count
            u32 vertex_index[entry_count]
            i16 delta[entry_count][3]  (metres = delta / 32767 * scale)
            padding to a 4-byte boundary
"""

import os
import sys
import json
import glob
import shutil
import struct
import argparse
import subprocess

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLENDSHAPE_DIR = os.path.join(ROOT, "blendshapes")
MORPH_CACHE = os.path.join(BLENDSHAPE_DIR, "_morph_cache.npz")
MATERIAL_MAPS_FILE = os.path.join(ROOT, "material_maps.json")

MAGIC = b"BBAV"
VERSION = 1
FLAG_TRANSPARENT = 1
DELTA_EPSILON = 1e-6          # same threshold the OBJ loader uses
MAX_QUANT_ERROR_M = 0.05e-3   # refuse quantisation worse than 0.05 mm

# Longest texture edge per role: what the headset resolves at arm's length
BASE_COLOR_MAX = 2048
MAP_MAX = 1024
OVERRIDE_MAX = {"Eyelash_Color.tga": 1024}

# Material defaults of the original renderer (used when a material has no
# material_maps.json entry)
DEFAULT_PARAMS = {"bump_scale": 1.0, "metallic": 0.0, "smoothness": 0.3,
                  "occlusion_strength": 0.0, "emission_color": (0.0, 0.0, 0.0)}


def load_morph_data(rebuild=False):
    """(materials, shape_names) from the committed morph cache. With rebuild
    (or no cache) it is re-parsed from the OBJs (tools/obj_morphs.py). Cache freshness isn't judged by
    mtime: git checkouts don't preserve it."""
    if rebuild or not os.path.isfile(MORPH_CACHE):
        print("Rebuilding the morph cache from the OBJs")
        import obj_morphs
        materials, shape_names = obj_morphs.build_morph_data(
            os.path.join(BLENDSHAPE_DIR, obj_morphs.BLENDSHAPE_BASIS),
            sorted(p for p in glob.glob(os.path.join(BLENDSHAPE_DIR, "*.obj"))
                   if os.path.basename(p) != obj_morphs.BLENDSHAPE_BASIS))
        obj_morphs.save_morph_cache(MORPH_CACHE, materials, shape_names)
    archive = np.load(MORPH_CACHE, allow_pickle=False)
    manifest = json.loads(str(archive["manifest"]))
    materials = []
    for i, mat in enumerate(manifest["materials"]):
        materials.append({
            "name": mat["name"], "texture": mat["texture"], "diffuse": mat["diffuse"],
            "vertices": archive[f"vertices_{i}"],
            "deltas": {s: archive[f"delta_{i}_{s}"] for s in mat["delta_shapes"]},
        })
    return materials, manifest["shape_names"]


def index_mesh(vertices, deltas, shape_order):
    """Collapses the per-corner triangle soup into unique vertices + indices.
    Corners merge only when position, normal, UV *and* every shape delta are
    bit-identical, so morphing is exactly preserved."""
    names = [s for s in shape_order if s in deltas]
    rows = np.concatenate([vertices] + [deltas[s] for s in names], axis=1)
    rows = np.ascontiguousarray(rows, dtype=np.float32)
    keys = rows.view(np.dtype((np.void, rows.dtype.itemsize * rows.shape[1]))).ravel()
    _, first, inverse = np.unique(keys, return_index=True, return_inverse=True)
    # Keep first-seen order so the vertex cache sees roughly the original order
    order = np.argsort(first)
    remap = np.empty_like(order)
    remap[order] = np.arange(len(order))
    unique_rows = first[order]
    indices = remap[inverse.ravel()].astype(np.uint32)
    return unique_rows, indices


def quantise(delta):
    """Sparse int16 deltas: (vertex_index, q[n,3], scale, max_error)."""
    moved = np.flatnonzero(np.abs(delta).max(axis=1) > DELTA_EPSILON)
    values = delta[moved]
    scale = float(np.abs(values).max()) if len(values) else 0.0
    if scale == 0.0:
        return moved.astype(np.uint32), np.zeros((0, 3), np.int16), 0.0, 0.0
    q = np.round(values / scale * 32767).astype(np.int16)
    error = float(np.abs(q.astype(np.float32) / 32767 * scale - values).max())
    return moved.astype(np.uint32), q, scale, error


def snorm16(normals):
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    n = normals / np.maximum(lengths, 1e-12)
    out = np.zeros((len(n), 4), np.int16)
    out[:, :3] = np.round(np.clip(n, -1, 1) * 32767)
    return out


def has_toktx():
    return shutil.which("toktx") is not None


def convert_texture(source, out_dir, max_size, fmt, srgb):
    """Downsizes one texture and writes it as KTX2 (ASTC) or PNG. Returns
    (relative output path, has_alpha)."""
    img = Image.open(source)
    img.load()
    has_alpha = "A" in img.getbands() and img.getchannel("A").getextrema()[0] < 255
    img = img.convert("RGBA" if has_alpha else "RGB")
    if max(img.size) > max_size:
        scale = max_size / max(img.size)
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                         Image.LANCZOS)
    stem = os.path.splitext(os.path.basename(source))[0]
    os.makedirs(os.path.join(out_dir, "textures"), exist_ok=True)
    png_rel = os.path.join("textures", stem + ".png")
    png_path = os.path.join(out_dir, png_rel)
    img.save(png_path, optimize=True)
    if fmt == "png":
        return png_rel, has_alpha
    ktx_rel = os.path.join("textures", stem + ".ktx2")
    # ASTC 6x6 (~3.6 bpp RGB/RGBA), full mip chain; no supercompression so the
    # app can upload blocks straight from the file (the tarball is gzipped)
    cmd = ["toktx", "--t2", "--encode", "astc", "--astc_blk_d", "6x6",
           "--astc_quality", "thorough", "--genmipmap",
           "--assign_oetf", "srgb" if srgb else "linear",
           os.path.join(out_dir, ktx_rel), png_path]
    subprocess.run(cmd, check=True)
    os.remove(png_path)
    return ktx_rel, has_alpha


def write_str(out, text):
    data = text.encode("utf-8")
    out += struct.pack("<H", len(data)) + data
    out += b"\0" * (-len(out) % 4)


def build(out_dir, fmt, rebuild_cache=False):
    materials, shape_names = load_morph_data(rebuild_cache)
    maps = {}
    if os.path.isfile(MATERIAL_MAPS_FILE):
        with open(MATERIAL_MAPS_FILE) as f:
            maps = json.load(f)
    else:
        print(f"No {os.path.basename(MATERIAL_MAPS_FILE)}: materials get default PBR params")

    os.makedirs(out_dir, exist_ok=True)
    textures, texture_index, texture_alpha = [], {}, {}

    def texture(path, role):
        if not path:
            return -1
        source = path if os.path.isabs(path) else os.path.join(BLENDSHAPE_DIR, path)
        if not os.path.isfile(source):
            source = os.path.join(ROOT, path)
        if source not in texture_index:
            name = os.path.basename(source)
            max_size = OVERRIDE_MAX.get(name, BASE_COLOR_MAX if role == "base" else MAP_MAX)
            rel, alpha = convert_texture(source, out_dir, max_size, fmt,
                                         srgb=role in ("base", "emission"))
            print(f"  texture {name} -> {rel} ({os.path.getsize(os.path.join(out_dir, rel)) / 1e6:.2f} MB)")
            texture_index[source] = len(textures)
            texture_alpha[source] = alpha
            textures.append(rel)
        return texture_index[source]

    body = bytearray()  # starts 4-aligned: the header before it is all 4-aligned fields
    worst_error = 0.0
    total_entries = 0
    for mat in materials:
        params = maps.get(mat["name"])
        base_tex = texture(mat["texture"], "base")
        base_source = None if base_tex < 0 else next(
            s for s, i in texture_index.items() if i == base_tex)
        diffuse = list(mat["diffuse"]) + [1.0] * (4 - len(mat["diffuse"]))
        transparent = (texture_alpha.get(base_source, False) if base_source
                       else diffuse[3] < 1.0)
        if params:
            normal_tex = texture(params.get("normal_map"), "normal")
            metallic_tex = texture(params.get("metallic_map"), "metallic")
            ao_tex = texture(params.get("ao_map"), "ao")
            emission_tex = texture(params.get("emission_map"), "emission")
            # Mirror assign_material_params: scalars apply only without a map
            metallic = 1.0 if params.get("metallic_map") else params["metallic"]
            occlusion = params["occlusion_strength"] if params.get("ao_map") else 0.0
            emission = (tuple(params.get("emission_color", (0, 0, 0)))
                        if params.get("emission_map") else (0.0, 0.0, 0.0))
            bump, smooth = params["bump_scale"], params["smoothness"]
        else:
            normal_tex = metallic_tex = ao_tex = emission_tex = -1
            d = DEFAULT_PARAMS
            bump, metallic, smooth = d["bump_scale"], d["metallic"], d["smoothness"]
            occlusion, emission = d["occlusion_strength"], d["emission_color"]

        vertices = mat["vertices"]   # (N, 8): u v nx ny nz x y z
        unique_rows, indices = index_mesh(vertices, mat["deltas"], shape_names)
        v = vertices[unique_rows]
        packed = np.zeros(len(v), dtype=[("pos", "<f4", 3), ("normal", "<i2", 4),
                                         ("uv", "<f4", 2)])
        packed["pos"] = v[:, 5:8]
        packed["normal"] = snorm16(v[:, 2:5])
        packed["uv"] = np.stack([v[:, 0], 1.0 - v[:, 1]], axis=1)

        write_str(body, mat["name"])
        body += struct.pack("<5i", base_tex, normal_tex, metallic_tex, ao_tex, emission_tex)
        body += struct.pack("<4f", *diffuse[:4])
        body += struct.pack("<I", FLAG_TRANSPARENT if transparent else 0)
        body += struct.pack("<7f", bump, metallic, smooth, occlusion, *emission)
        body += struct.pack("<II", len(v), len(indices))
        body += packed.tobytes()
        body += indices.astype("<u4").tobytes()

        morphs = []
        for shape in shape_names:
            if shape not in mat["deltas"]:
                continue
            delta = mat["deltas"][shape][unique_rows]
            idx, q, scale, error = quantise(delta)
            if error > MAX_QUANT_ERROR_M:
                raise SystemExit(f"{mat['name']}/{shape}: quantisation error {error * 1e3:.4f} mm "
                                 f"exceeds {MAX_QUANT_ERROR_M * 1e3} mm")
            worst_error = max(worst_error, error)
            if len(idx):
                morphs.append((shape_names.index(shape), scale, idx, q))
        body += struct.pack("<I", len(morphs))
        for shape_index, scale, idx, q in morphs:
            body += struct.pack("<IfI", shape_index, scale, len(idx))
            body += idx.astype("<u4").tobytes()
            body += q.astype("<i2").tobytes()
            body += b"\0" * (-len(body) % 4)
            total_entries += len(idx)
        print(f"  material {mat['name']}: {len(vertices)} corners -> {len(v)} vertices, "
              f"{len(morphs)} morphs, {'transparent' if transparent else 'opaque'}")

    out = bytearray(MAGIC + struct.pack("<I", VERSION))
    out += struct.pack("<I", len(shape_names))
    for name in shape_names:
        write_str(out, name)
    out += struct.pack("<I", len(textures))
    for rel in textures:
        write_str(out, rel.replace(os.sep, "/"))
    out += struct.pack("<I", len(materials))
    out += body
    avatar_path = os.path.join(out_dir, "avatar.bin")
    with open(avatar_path, "wb") as f:
        f.write(out)

    total = sum(os.path.getsize(os.path.join(dp, f))
                for dp, _, fs in os.walk(out_dir) for f in fs)
    print(f"avatar.bin: {len(out) / 1e6:.2f} MB, {len(shape_names)} shapes, "
          f"{total_entries} delta entries, worst quantisation error "
          f"{worst_error * 1e6:.2f} µm")
    print(f"total {out_dir}: {total / 1e6:.1f} MB")
    return avatar_path


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=os.path.join(ROOT, "dist-assets"))
    parser.add_argument("--format", choices=("auto", "ktx2", "png"), default="auto")
    parser.add_argument("--rebuild-cache", action="store_true",
                        help="re-parse the OBJs into _morph_cache.npz first")
    args = parser.parse_args()
    fmt = args.format
    if fmt == "auto":
        fmt = "ktx2" if has_toktx() else "png"
    elif fmt == "ktx2" and not has_toktx():
        raise SystemExit("--format ktx2 needs `toktx` (KTX-Software) on PATH")
    if os.path.isdir(args.out) and os.listdir(args.out):
        # Only ever replace a previous converter output
        if not os.path.isfile(os.path.join(args.out, "avatar.bin")):
            raise SystemExit(f"{args.out} is not empty and holds no avatar.bin; refusing to clear it")
        shutil.rmtree(args.out)
    print(f"Converting assets -> {args.out} (textures: {fmt})")
    build(args.out, fmt, args.rebuild_cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
