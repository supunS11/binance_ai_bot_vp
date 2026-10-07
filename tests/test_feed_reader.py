import gzip
import json
import os
import shutil
import tempfile
import unittest

from data import feed_reader as fr

MIN = 60_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % (24 * 60 * MIN))


class StackedImbalanceTests(unittest.TestCase):
    def test_three_adjacent_buy_dominant_levels_stack(self):
        levels = {"100.0": [9.0, 1.0], "100.1": [8.0, 2.0], "100.2": [7.0, 2.0], "100.3": [1.0, 1.0]}
        self.assertTrue(fr.stacked_imbalance(levels, "BUY", 0.1, 3.0, 3, 99.9, 100.3))

    def test_a_gap_in_price_breaks_the_run(self):
        levels = {"100.0": [9.0, 1.0], "100.1": [8.0, 2.0], "100.3": [7.0, 2.0]}
        self.assertFalse(fr.stacked_imbalance(levels, "BUY", 0.1, 3.0, 3, 99.9, 100.3))

    def test_sell_side_needs_sell_dominance(self):
        levels = {"100.0": [9.0, 1.0], "100.1": [8.0, 2.0], "100.2": [7.0, 2.0]}
        self.assertFalse(fr.stacked_imbalance(levels, "SELL", 0.1, 3.0, 3, 99.9, 100.3))

    def test_no_tick_size_is_not_a_measurement(self):
        self.assertFalse(fr.stacked_imbalance({"1": [9, 1]}, "BUY", 0.0, 3.0, 3, 0.0, 2.0))

    def test_a_real_stack_outside_the_zone_band_does_not_count(self):
        # Same run as the first test, but the zone is elsewhere. A stack is a statement
        # about THIS level, not about whether aggressive trading happened somewhere nearby.
        levels = {"100.0": [9.0, 1.0], "100.1": [8.0, 2.0], "100.2": [7.0, 2.0]}
        self.assertFalse(fr.stacked_imbalance(levels, "BUY", 0.1, 3.0, 3, 95.0, 95.5))


class RestingOrderTests(unittest.TestCase):
    def _sample(self, bid_qty):
        return {"t": T0, "b": [["100.0", "1"], ["99.9", "1"], ["99.8", str(bid_qty)]],
                "a": [["100.1", "1"]]}

    def test_a_large_bid_held_in_the_band_is_resting(self):
        samples = [self._sample(10) for _ in range(10)]
        self.assertTrue(fr.resting_large_orders(samples, "BUY", 99.7, 99.9, 3.0, 0.6))

    def test_a_large_bid_that_appears_briefly_is_not_resting(self):
        samples = [self._sample(10)] + [self._sample(1) for _ in range(9)]
        self.assertFalse(fr.resting_large_orders(samples, "BUY", 99.7, 99.9, 3.0, 0.6))

    def test_a_large_bid_outside_the_band_does_not_count(self):
        samples = [self._sample(10) for _ in range(10)]
        self.assertFalse(fr.resting_large_orders(samples, "BUY", 99.0, 99.5, 3.0, 0.6))


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, kind, records):
        folder = os.path.join(self.root, kind, "BTCUSDT")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, next(fr._days(T0, T0)) + ".jsonl.gz")
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def test_a_window_before_the_recorder_started_is_unavailable(self):
        self._write("trades", [{"m": T0 + 10 * MIN, "lv": {"1": [1, 0]}}])
        self.assertIsNone(fr.trade_levels(self.root, "BTCUSDT", T0, T0 + 5 * MIN))

    def test_a_window_across_a_gap_marker_is_unavailable(self):
        self._write("trades", [{"m": T0, "lv": {"1": [1, 0]}},
                               {"gap_from": T0 + 2 * MIN, "gap_to": T0 + 4 * MIN},
                               {"m": T0 + 5 * MIN, "lv": {"1": [1, 0]}}])
        self.assertIsNone(fr.trade_levels(self.root, "BTCUSDT", T0, T0 + 6 * MIN))

    def test_a_covered_window_sums_the_minutes(self):
        self._write("trades", [{"m": T0, "lv": {"1": [1, 2]}},
                               {"m": T0 + MIN, "lv": {"1": [3, 0]}}])
        self.assertEqual(fr.trade_levels(self.root, "BTCUSDT", T0, T0 + 2 * MIN),
                         {"1": [4.0, 2.0]})

    def test_trade_minutes_keeps_the_minute_boundary(self):
        self._write("trades", [{"m": T0, "lv": {"1": [1, 2]}},
                               {"m": T0 + MIN, "lv": {"1": [3, 0]}}])
        self.assertEqual(fr.trade_minutes(self.root, "BTCUSDT", T0, T0 + 2 * MIN),
                         [{"m": T0, "lv": {"1": [1, 2]}}, {"m": T0 + MIN, "lv": {"1": [3, 0]}}])

    def test_trade_minutes_is_none_when_not_covered(self):
        self._write("trades", [{"m": T0 + 10 * MIN, "lv": {"1": [1, 0]}}])
        self.assertIsNone(fr.trade_minutes(self.root, "BTCUSDT", T0, T0 + 5 * MIN))


if __name__ == "__main__":
    unittest.main()
