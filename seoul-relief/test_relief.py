import re
import unittest
from collections import Counter

import numpy as np

import make_relief as mr


def directed_edges(tris):
    ids = {}
    key = lambda v: ids.setdefault(tuple(np.round(v, 5)), len(ids))
    c = Counter()
    for t in tris:
        a, b, d = (key(v) for v in t)
        c.update([(a, b), (b, d), (d, a)])
    return c


def signed_volume(tris):
    return float(np.einsum("ij,ij->", tris[:, 0], np.cross(tris[:, 1], tris[:, 2])) / 6)


class Meshes(unittest.TestCase):
    def test_heightfield_is_closed_and_has_right_volume(self):
        z = np.random.default_rng(1).uniform(1, 5, (6, 8)).astype(np.float32)
        tris = mr.heightfield_solid(z, 0.5)
        c = directed_edges(tris)
        self.assertTrue(all(n == 1 and c[(b, a)] == 1 for (a, b), n in c.items()))
        self.assertGreater(signed_volume(tris), 0)

    def test_flat_plate_volume(self):
        tris = mr.heightfield_solid(np.full((5, 5), 3.0, np.float32), 1.0)
        self.assertAlmostEqual(signed_volume(tris), 4 * 4 * 3, places=3)

    def test_columns_volume_and_hidden_faces(self):
        zb = np.zeros((3, 4))
        zt = np.array([[1, 1, 2, 0], [1, 1, 2, 0], [0, 0, 0, 0]], float)
        mask = zt > 0
        tris = mr.column_solid(zb, zt, mask, 0.5)
        self.assertAlmostEqual(signed_volume(tris), (4 * 1 + 2 * 2) * 0.25, places=4)
        # mixed heights leave harmless T-junctions at corners (volume is still exact);
        # a uniform plateau must be a perfectly closed manifold
        flat = mr.column_solid(np.zeros((3, 4)), np.ones((3, 4)), np.array([[1, 1, 1, 0]] * 2 + [[0] * 4], bool), 0.5)
        c = directed_edges(flat)
        self.assertTrue(all(n == 1 and c[(b, a)] == 1 for (a, b), n in c.items()))

    def test_empty_mask(self):
        self.assertEqual(len(mr.column_solid(np.zeros((2, 2)), np.ones((2, 2)), np.zeros((2, 2), bool), 1.0)), 0)


class Parsing(unittest.TestCase):
    def test_int_ge_regex_matches_brute_force(self):
        for t in (1, 5, 9, 10, 12, 40, 99, 100, 250):
            rx = re.compile(f"^({mr.int_ge_regex(t)})$")
            for n in range(1, 1200):
                self.assertEqual(bool(rx.match(str(n))), n >= t, (t, n))

    def test_height_parsing(self):
        self.assertEqual(mr.parse_height({"height": "123.5 m"}, 3.2), 123.5)
        self.assertAlmostEqual(mr.parse_height({"building:levels": "10"}, 3.2), 32.0)
        self.assertIsNone(mr.parse_height({"height": "tall"}, 3.2))

    def test_water_relation_keeps_islands_as_land(self):
        sq = lambda x0, y0, x1, y1: [{"lon": x, "lat": y} for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))]
        rel = {"type": "relation", "members": [
            {"type": "way", "role": "outer", "geometry": sq(0, 0, 10, 10)},
            {"type": "way", "role": "inner", "geometry": sq(4, 4, 6, 6)}]}
        (poly,) = mr.parse_water([rel])
        self.assertAlmostEqual(poly.area, 100 - 4)

    def test_height_mapping_is_monotonic_and_capped(self):
        a = mr.build_parser().parse_args([])
        h = np.array([10, 40, 80, 150, 300, 555], float)
        r = mr.map_building_heights(h, a)
        self.assertEqual(r[0], 0)
        self.assertTrue(np.all(np.diff(r[1:]) >= 0))
        self.assertAlmostEqual(r[-1], a.b_max)
        self.assertAlmostEqual(r[1], a.b_min)


class EndToEnd(unittest.TestCase):
    def test_demo_runs(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            mr.main(["--demo", "--out", d, "--size", "80", "--res", "1", "--line", "TEST"])
            for f in ("body.stl", "route.stl", "text.stl", "preview.png"):
                self.assertGreater((pathlib.Path(d) / f).stat().st_size, 1000, f)


if __name__ == "__main__":
    unittest.main()
