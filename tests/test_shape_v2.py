import unittest

import numpy as np

from research import shape_v2 as sv

P = {"SEP": 0.25, "VALLEY": 0.3, "MASS": 0.15, "FLAT": 1.8, "CENTRE": 0.6}


def day(volumes, open_frac=0.5, close_frac=0.5):
    """Synthetic profile on prices 0..len-1 with a 70% value area around the POC."""
    vol = np.asarray(volumes, float)
    prices = np.arange(len(vol), dtype=float)
    order = np.argsort(-vol)
    keep, acc = [], 0.0
    for i in order:
        keep.append(i)
        acc += vol[i]
        if acc >= 0.7 * vol.sum():
            break
    lo, hi = prices[min(keep)], prices[max(keep)]
    return {"bins": {str(p): v for p, v in zip(prices, vol) if v > 0}, "low": 0.0,
            "high": float(len(vol) - 1), "open": open_frac * (len(vol) - 1),
            "close": close_frac * (len(vol) - 1), "vah": hi, "val": lo,
            "poc": prices[int(np.argmax(vol))]}


def bump(n, centre, width, height):
    x = np.arange(n)
    return height * np.exp(-0.5 * ((x - centre) / width) ** 2)


class ShapeV2Tests(unittest.TestCase):
    N = 60

    def test_central_bulge_is_d(self):
        self.assertEqual(sv.classify_v2(day(bump(self.N, 30, 7, 10) + 0.2), p=P), "D")

    def test_bulge_at_top_with_thin_tail_below_is_p(self):
        self.assertEqual(sv.classify_v2(day(bump(self.N, 48, 5, 10) + 0.2), p=P), "P")

    def test_bulge_at_bottom_is_b(self):
        self.assertEqual(sv.classify_v2(day(bump(self.N, 11, 5, 10) + 0.2), p=P), "b")

    def test_two_bulges_with_a_thin_neck_is_double_distribution(self):
        v = bump(self.N, 14, 4, 10) + bump(self.N, 46, 4, 9) + 0.2
        self.assertEqual(sv.classify_v2(day(v), p=P), "B")

    def test_one_spiky_bin_does_not_make_a_double_distribution(self):
        v = bump(self.N, 30, 7, 10) + 0.2
        v[5] = 9.0                                   # single spike far from the POC
        self.assertNotEqual(sv.classify_v2(day(v), p=P), "B")

    def test_flat_one_directional_day_is_thin(self):
        v = np.ones(self.N) + np.random.default_rng(1).uniform(0, 0.3, self.N)
        self.assertEqual(sv.classify_v2(day(v, 0.05, 0.95), p=P), "THIN_UP")
        self.assertEqual(sv.classify_v2(day(v, 0.95, 0.05), p=P), "THIN_DOWN")

    def test_small_separate_tail_cluster_is_skipped_not_guessed(self):
        v = bump(self.N, 44, 5, 10) + bump(self.N, 8, 2.5, 6) + 0.2   # P with a small low cluster
        s = sv.rows_profile(day(v)["bins"], 0.0, self.N - 1.0)
        self.assertTrue(0.15 <= sv.b_strength(s, sv.FINAL) < 0.25)
        self.assertEqual(sv.classify_v2(day(v), p=sv.FINAL), "SKIP")
        self.assertEqual(sv.classify_v2(day(v), p=P), "B")       # without the skip band

    def test_final_still_calls_a_real_double_distribution_b(self):
        v = bump(self.N, 14, 4, 10) + bump(self.N, 46, 4, 9) + 0.2
        self.assertEqual(sv.classify_v2(day(v), p=sv.FINAL), "B")

    def test_final_leaves_clear_single_bulges_alone(self):
        self.assertEqual(sv.classify_v2(day(bump(self.N, 30, 7, 10) + 0.2), p=sv.FINAL), "D")
        self.assertEqual(sv.classify_v2(day(bump(self.N, 48, 5, 10) + 0.2), p=sv.FINAL), "P")

    def test_b_split_sits_in_the_neck_between_the_two_bulges(self):
        v = bump(self.N, 14, 4, 10) + bump(self.N, 46, 4, 9) + 0.2
        split = sv.b_split_price(day(v), sv.FINAL)
        self.assertTrue(22 < split < 38, split)
        self.assertIsNone(sv.b_split_price(day(bump(self.N, 30, 7, 10) + 0.2), sv.FINAL))

    def test_near_empty_day_is_degenerate(self):
        self.assertIsNone(sv.classify_v2(day([0, 3, 5, 2, 0, 0]), p=P))


if __name__ == "__main__":
    unittest.main()
