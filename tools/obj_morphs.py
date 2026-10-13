"""
Parses the Blender blendshape exports (blendshapes/basis.obj plus one OBJ per
shape, from blender_export_blendshapes.py) into the morph cache
blendshapes/_morph_cache.npz that tools/convert_assets.py reads. Moved here
from the retired Python prototype; only needed with --rebuild-cache, and
needs pywavefront for the basis parse.
"""

import os
import json

import numpy as np

BLENDSHAPE_BASIS = "basis.obj"

def parse_obj_materials(obj_path):
    """Parses an OBJ into {material_name: ((N, 8) float32 T2F_N3F_V3F array, material)}."""
    import pywavefront
    scene = pywavefront.Wavefront(obj_path, collect_faces=True)
    materials = {}
    for name, material in scene.materials.items():
        # Skip materials that do not contain any geometry
        if not material.vertices:
            continue
        materials[name] = (np.array(material.vertices, dtype=np.float32).reshape(-1, 8), material)
    return materials

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
