"""research/wf_trade: entries A/B, targets, skip rules, bracket fill-minute handling."""
import unittest

import numpy as np

from research import wf_trade as wt


def arr(o, h, l, c):
    return tuple(np.array(x, dtype=float) for x in (o, h, l, c))


class Entries(unittest.TestCase):
    def test_a_fills_on_break_of_ini_high_next_bar_only(self):
        a = arr([10] * 12, [10, 10, 10.2, 10, 10, 10, 10, 10, 11, 11, 11, 11], [9.9] * 12, [10] * 12)
        self.assertEqual(wt.entry_a(a, 0, "BUY", 10.1, 0.01), (2, 10.11))
        late = arr([10] * 12, [10] * 7 + [11] * 5, [9.9] * 12, [10] * 12)
        self.assertIsNone(wt.entry_a(late, 0, "BUY", 10.1, 0.01))      # break after 5 minutes

    def test_a_gap_fills_at_open(self):
        a = arr([10, 10.5], [10, 10.6], [10, 10.4], [10, 10.5])
        self.assertEqual(wt.entry_a(a, 0, "BUY", 10.1, 0.01), (1, 10.5))

    def test_a_sell_mirror(self):
        a = arr([10] * 3, [10.1] * 3, [10, 9.85, 9.8], [10] * 3)
        self.assertEqual(wt.entry_a(a, 0, "SELL", 9.9, 0.01), (1, 9.89))

    def test_b_needs_trade_through(self):
        a = arr([10] * 4, [10] * 4, [10, 9.5, 9.49, 9.48], [10] * 4)
        self.assertEqual(wt.entry_b(a, 0, "BUY", 9.5, 0.01, 3), (3, 9.5))  # 9.49 is not < 9.49
        self.assertIsNone(wt.entry_b(a, 0, "BUY", 9.5, 0.01, 2))           # window ended


class Targets(unittest.TestCase):
    def test_t1_nearest_t2_first_at_2r(self):
        t1, t2 = wt.pick_targets("BUY", 100, 1.0, [99, 100.5, 101.5, 102.5, 104])
        self.assertEqual((t1, t2), (100.5, 102.5))
        t1, t2 = wt.pick_targets("SELL", 100, 1.0, [101, 99.5, 97.9, 97])
        self.assertEqual((t1, t2), (99.5, 97.9))

    def test_plan_skips(self):
        self.assertEqual(wt.plan("BUY", 100, 101, 102, 103), "stop_side")
        self.assertEqual(wt.plan("BUY", 100, 99.99, 101, 103), "stop_too_tight")
        self.assertEqual(wt.plan("BUY", 100, 99, 101, None), "no_t2")
        self.assertEqual(wt.plan("BUY", 100, 99, 101, 101.5), "t2_under_2r")
        self.assertEqual(wt.plan("BUY", 100, 99, 101, 102), "")


class Bracket(unittest.TestCase):
    def test_fill_minute_counts_only_against(self):
        # fill minute spikes to the target but also dips to the stop: stop (conservative)
        a = arr([100, 100], [103, 100], [98.9, 100], [100, 100])
        self.assertEqual(wt.run_bracket(a, 0, "BUY", 100, 99, 102)["outcome"], "STOP")
        # target on the fill minute alone is not counted
        b = arr([100, 100, 100], [103, 100, 102.5], [99.5, 99.5, 99.5], [100, 100, 102])
        r = wt.run_bracket(b, 0, "BUY", 100, 99, 102)
        self.assertEqual((r["outcome"], r["gross"]), ("TARGET", 2.0))


if __name__ == "__main__":
    unittest.main()
