"""assets/routines.json is hand-edited now; check it holds what the app needs."""

import os
import json
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTINES = os.path.join(ROOT, "assets", "routines.json")
MORPH_CACHE = os.path.join(ROOT, "blendshapes", "_morph_cache.npz")

try:
    import numpy
except ImportError:
    numpy = None


def load():
    with open(ROUTINES, encoding="utf-8") as f:
        return json.load(f)


class RoutinesJsonTest(unittest.TestCase):
    def test_structure(self):
        data = load()
        for key in ("prep_seconds", "relax_seconds", "alternate_period_seconds",
                    "freeform_prompt_seconds", "freeform_shuffle_seconds"):
            self.assertIn(key, data["timing"])
        self.assertIsInstance(data["consent_version"], str)
        self.assertEqual(set(data["routines"]), {"mouth", "eye"})
        for name, routine in data["routines"].items():
            self.assertTrue(routine["steps"], name)
            logs = [s["log"] for s in routine["steps"]]
            self.assertEqual(len(logs), len(set(logs)), f"{name}: duplicate step log labels")
            for step in routine["steps"]:
                self.assertIn("display", step)
                self.assertIn(step.get("mode", "hold"), ("hold", "alternate"), step["log"])
        panes = {p["name"] for p in data["onboarding"]}
        self.assertTrue({"welcome", "consent", "refresh_rate"} <= panes)

    @unittest.skipIf(numpy is None or not os.path.isfile(MORPH_CACHE), "needs numpy and the morph cache")
    def test_shapes_exist(self):
        archive = numpy.load(MORPH_CACHE, allow_pickle=False)
        shapes = set(json.loads(str(archive["manifest"]))["shape_names"])
        data = load()
        for virtual, (a, b) in data["bipolar_shapes"].items():
            self.assertTrue({a, b} <= shapes, virtual)
        names = shapes | set(data["bipolar_shapes"])
        for name, routine in data["routines"].items():
            for step in routine["steps"]:
                missing = set(step.get("pose", {})) - names
                self.assertFalse(missing, f"{name}/{step['log']}: unknown shapes {missing}")


if __name__ == "__main__":
    unittest.main()
