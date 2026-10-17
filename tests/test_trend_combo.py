import unittest
from types import SimpleNamespace

from research import trend_combo as tc


def bars(closes, spread=0.5):
    return [SimpleNamespace(open=c, high=c + spread, low=c - spread, close=c) for c in closes]


class ZigzagTests(unittest.TestCase):
    def test_pivots_alternate_and_ignore_small_wiggles(self):
        path = [100, 101, 100.5, 101.5, 110, 109, 110.5, 100, 99.5, 108]
        b = bars(path)
        atrs = [2.0] * len(b)
        piv = tc.zigzag_pivots(b, atrs, k=2.0)        # 4-point reversal needed
        kinds = [p[3] for p in piv]
        self.assertTrue(all(a != c for a, c in zip(kinds, kinds[1:])))   # alternate
        self.assertIn(("H", 111.0), [(p[3], p[2]) for p in piv])          # 110.5 + 0.5

    def test_pivots_never_change_when_future_bars_are_added(self):
        path = [100, 104, 108, 103, 99, 104, 109, 113, 107, 102]
        b = bars(path)
        atrs = [2.0] * len(b)
        early = [p for p in tc.zigzag_pivots(b[:7], atrs[:7], 2.0)]
        late = [p for p in tc.zigzag_pivots(b, atrs, 2.0) if p[0] < 7]
        self.assertEqual(early, late)


class StructureTests(unittest.TestCase):
    PIV = [(1, 0, 100.0, "H"), (2, 1, 90.0, "L"), (4, 3, 110.0, "H"), (6, 5, 95.0, "L")]

    def test_hh_hl_is_up_until_the_last_low_breaks(self):
        self.assertEqual(tc.structure_label(self.PIV, 7, 100.0), tc.UP)
        self.assertEqual(tc.structure_label(self.PIV, 7, 94.0), tc.ROT)

    def test_break_above_last_high_with_higher_low_is_up(self):
        piv = [(1, 0, 110.0, "H"), (2, 1, 90.0, "L"), (4, 3, 105.0, "H"), (6, 5, 95.0, "L")]
        self.assertEqual(tc.structure_label(piv, 7, 100.0), tc.ROT)      # LH + HL: contracting
        self.assertEqual(tc.structure_label(piv, 7, 106.0), tc.UP)       # breaks the LH

    def test_only_pivots_confirmed_before_the_day_count(self):
        self.assertIsNone(tc.structure_label(self.PIV, 5, 100.0))        # second low not yet known


class ValueTests(unittest.TestCase):
    def _vals(self, pocs):
        return [{"poc": p, "vah": p + 1, "val": p - 1, "atr": 2.0} for p in pocs]

    def test_steady_higher_value_is_up_and_flat_value_is_rotation(self):
        up = self._vals([100, 101.5, 103, 104.5, 106, 107.5])
        self.assertEqual(tc.value_label(up, tc.migration_scores(up), 6, 5, 3, 1.0), tc.UP)
        flat = self._vals([100, 100.1, 99.9, 100, 100.1, 100])
        self.assertEqual(tc.value_label(flat, tc.migration_scores(flat), 6, 5, 3, 1.0), tc.ROT)

    def test_value_label_ignores_the_day_itself(self):
        v = self._vals([100, 101.5, 103, 104.5, 106, 107.5, 50])
        self.assertEqual(tc.value_label(v, tc.migration_scores(v), 6, 5, 3, 1.0), tc.UP)


class CompositeValueTests(unittest.TestCase):
    def test_composite_compares_last_n_days_with_the_n_before(self):
        vals = [{"poc": p, "atr": 2.0} for p in [100] * 5 + [102] * 5 + [999]]
        self.assertEqual(tc.composite_value_label(vals, 10, 5, 0.5), tc.UP)    # +2 = 1 ATR
        self.assertEqual(tc.composite_value_label(vals, 10, 5, 1.5), tc.ROT)
        self.assertIsNone(tc.composite_value_label(vals, 9, 5, 0.5))           # not 2n days yet


class HindsightTruthTests(unittest.TestCase):
    def test_first_leg_of_a_new_downtrend_is_down_and_pullbacks_keep_the_trend(self):
        # H 110, L 100, H 120 (HH), L 104 (HL pullback), H 125 (HH), L 95 (breaks 104)
        path = [110, 100, 120, 104, 125, 95, 100]
        b = [SimpleNamespace(open=c, high=c, low=c, close=c) for c in path]
        lab = tc.truth_structure_labels(b, [4.0] * len(b), 1.0)
        self.assertEqual(lab[2], tc.UP)        # leg 120 -> 104 holds a higher low
        self.assertEqual(lab[3], tc.UP)        # leg 104 -> 125 breaks the high
        self.assertEqual(lab[4], tc.DOWN)      # leg 125 -> 95 breaks the low: new trend


class TruthTests(unittest.TestCase):
    def test_truth_trend_rotation_unclear(self):
        b = bars([100 + i for i in range(21)])
        atrs = [2.0] * 21
        self.assertEqual(tc.truth_label(b, atrs, 10, 10), tc.UP)       # +20 = 10 ATR, direct
        b = bars([100, 101] * 10 + [100])
        self.assertEqual(tc.truth_label(b, atrs, 10, 10), tc.ROT)


if __name__ == "__main__":
    unittest.main()
