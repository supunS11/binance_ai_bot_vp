import unittest

import numpy as np

from research import trend_methods as tm

N = 400


def series(kind, seed=1):
    """Synthetic daily candles: steady rise, steady fall, or flat noise."""
    rng = np.random.default_rng(seed)
    drift = {"up": 0.004, "down": -0.004, "flat": 0.0}[kind]
    ret = drift + rng.normal(0, 0.002 if kind != "flat" else 0.01, N)
    c = 100 * np.exp(np.cumsum(ret))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.003, N))
    l = np.minimum(o, c) * (1 - rng.uniform(0, 0.003, N))
    v = rng.uniform(1000, 2000, N)
    return h, l, c, v


class TrendMethodTests(unittest.TestCase):
    def test_steady_rise_reads_up_for_every_method(self):
        h, l, c, v = series("up")
        for name, labels in tm.all_methods(h, l, c, v).items():
            if name in ("M3a", "M4a", "M6a"):
                continue                       # ADX may call a smooth drift weak; tested below
            self.assertEqual(labels[-1], tm.UP, name)

    def test_steady_fall_reads_down_for_every_method(self):
        h, l, c, v = series("down")
        for name, labels in tm.all_methods(h, l, c, v).items():
            if name in ("M3a", "M4a", "M6a"):
                continue
            self.assertEqual(labels[-1], tm.DOWN, name)

    def test_no_method_reads_the_opposite_direction(self):
        for kind, wrong in (("up", tm.DOWN), ("down", tm.UP)):
            h, l, c, v = series(kind, seed=3)
            for name, labels in tm.all_methods(h, l, c, v).items():
                tail = [x for x in labels[-100:] if x is not None]
                self.assertNotIn(wrong, tail, f"{name} on a steady {kind} series")

    def test_flat_noise_reads_mostly_range_for_three_state_methods(self):
        # noise around a fixed level (not a random walk): no direction at any lookback
        rng = np.random.default_rng(5)
        flat = 100 + rng.normal(0, 1, N)
        h, l, v = flat + 0.5, flat - 0.5, np.full(N, 1000.0)
        m = tm.all_methods(h, l, flat, v)
        # Measured behaviour on pure noise (20 seeds, reported in the study): only the ADX
        # variants say RANGE; M1 0.33, M7 0.01 (its 15 MAs sit together and flip with each
        # close), M2 0.5, M5 0.52, E1 0.61, two-state M3/M4/M6 never. These are properties
        # of the published methods, not implementation errors, so only the ADX filter is
        # asserted here.
        for name in ("M3a", "M4a", "M6a"):
            tail = [x for x in m[name][-100:] if x is not None]
            self.assertGreater(tail.count(tm.RANGE) / len(tail), 0.9, name)

    def test_turtle_enters_on_55_day_breakout_and_exits_on_20_day_low(self):
        c = np.array([100.0] * 60 + [110.0] + [111.0] * 25 + [105.0] + [90.0])
        h, l = c + 0.5, c - 0.5
        lab = tm.m_turtle(h, l, c)
        self.assertEqual(lab[59], tm.RANGE)
        self.assertEqual(lab[60], tm.UP)
        self.assertEqual(lab[85], tm.UP)
        self.assertEqual(lab[86], tm.RANGE)   # below the 20-day low, above the 55-day low
        self.assertEqual(lab[87], tm.DOWN)    # below the 55-day low

    def test_labels_for_a_day_use_only_earlier_candles(self):
        h, l, c, v = series("up")
        full = tm.day_labels(tm.all_methods(h, l, c, v)["M4"])
        cut = tm.day_labels(tm.all_methods(h[:300], l[:300], c[:300], v[:300])["M4"])
        self.assertEqual(full[:300], cut)
        c2 = c.copy()
        c2[299] = c2[299] * 0.5                       # change day 299 only
        changed = tm.day_labels(tm.all_methods(h, l, c2, v)["M4"])
        self.assertEqual(changed[:300], full[:300])   # day 299's own label cannot see it

    def test_tv_rating_counts_fifteen_votes(self):
        h, l, c, v = series("up")
        s = tm.tv_ma_rating(h, l, c, v)
        vals = s[~np.isnan(s)]
        self.assertTrue(np.allclose(vals * 15, np.round(vals * 15)))   # mean of 15 integer votes

    def test_ensemble_needs_a_strong_majority(self):
        U, D, R = tm.UP, tm.DOWN, tm.RANGE
        self.assertEqual(tm.ensemble([U], [U], [U], [D], [R])[0], R)   # mean 0.2
        self.assertEqual(tm.ensemble([U], [U], [U], [R], [R])[0], U)   # mean 0.6


if __name__ == "__main__":
    unittest.main()
