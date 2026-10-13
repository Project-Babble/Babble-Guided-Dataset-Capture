"""Round-trip check of tools/convert_assets.py output against the morph cache.

    python tools/convert_assets.py && python -m unittest tests.test_avatar_bin
Skipped when numpy or dist-assets/avatar.bin is missing."""

import os
import json
import struct
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AVATAR = os.path.join(ROOT, "dist-assets", "avatar.bin")
CACHE = os.path.join(ROOT, "blendshapes", "_morph_cache.npz")

try:
    import numpy as np
except ImportError:
    np = None


def read_avatar(path):
    data = open(path, "rb").read()
    pos = 0

    def take(fmt):
        nonlocal pos
        values = struct.unpack_from("<" + fmt, data, pos)
        pos += struct.calcsize("<" + fmt)
        return values

    def string():
        nonlocal pos
        (n,) = take("H")
        text = data[pos:pos + n].decode("utf-8")
        pos += n
        pos += -pos % 4
        return text

    def array(dtype, count):
        nonlocal pos
        arr = np.frombuffer(data, dtype=dtype, count=count, offset=pos)
        pos += arr.nbytes
        return arr

    assert data[:4] == b"BBAV"
    pos = 4
    (version,) = take("I")
    shapes = [string() for _ in range(take("I")[0])]
    textures = [string() for _ in range(take("I")[0])]
    materials = []
    for _ in range(take("I")[0]):
        mat = {"name": string()}
        mat["textures"] = take("5i")
        mat["diffuse"] = take("4f")
        (mat["flags"],) = take("I")
        mat["params"] = take("7f")
        vcount, icount = take("II")
        mat["vertices"] = array([("pos", "<f4", 3), ("normal", "<i2", 4), ("uv", "<f4", 2)], vcount)
        mat["indices"] = array("<u4", icount)
        mat["morphs"] = {}
        for _ in range(take("I")[0]):
            shape_index, scale, count = take("IfI")
            idx = array("<u4", count)
            q = array("<i2", count * 3).reshape(count, 3)
            pos += -pos % 4
            mat["morphs"][shapes[shape_index]] = (idx, q.astype(np.float32) / 32767 * scale)
        materials.append(mat)
    assert pos == len(data), f"{len(data) - pos} trailing bytes"
    return version, shapes, textures, materials


@unittest.skipIf(np is None or not os.path.isfile(AVATAR), "needs numpy and dist-assets/avatar.bin")
class AvatarBinTest(unittest.TestCase):
    def test_round_trip(self):
        version, shapes, textures, materials = read_avatar(AVATAR)
        self.assertEqual(version, 1)
        for rel in textures:
            self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(AVATAR), rel)), rel)
        cache = np.load(CACHE, allow_pickle=False)
        manifest = json.loads(str(cache["manifest"]))
        self.assertEqual(shapes, manifest["shape_names"])
        self.assertEqual([m["name"] for m in materials], [m["name"] for m in manifest["materials"]])
        for i, (mat, ref) in enumerate(zip(materials, manifest["materials"])):
            corners = cache[f"vertices_{i}"]
            v = mat["vertices"][mat["indices"]]
            np.testing.assert_array_equal(v["pos"], corners[:, 5:8])
            np.testing.assert_array_equal(v["uv"][:, 0], corners[:, 0])
            np.testing.assert_allclose(v["uv"][:, 1], 1.0 - corners[:, 1], atol=1e-6)
            normals = corners[:, 2:5] / np.linalg.norm(corners[:, 2:5], axis=1, keepdims=True)
            np.testing.assert_allclose(v["normal"][:, :3] / 32767, normals, atol=1e-4)
            self.assertEqual(set(mat["morphs"]), set(ref["delta_shapes"]))
            for shape, (idx, delta) in mat["morphs"].items():
                full = np.zeros((len(mat["vertices"]), 3), np.float32)
                full[idx] = delta
                np.testing.assert_allclose(full[mat["indices"]], cache[f"delta_{i}_{shape}"],
                                           atol=1e-6, err_msg=f"{mat['name']}/{shape}")


if __name__ == "__main__":
    unittest.main()
