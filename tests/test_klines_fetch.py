"""klines.fetch asks for no more candles than the range holds (request weight), and still
paginates long ranges."""
import unittest
from unittest.mock import patch

from data import klines as klines_mod

MIN = 60_000
T0 = 1_790_000_000_000 // MIN * MIN


class FakeRest:
    def __init__(self, last_open):
        self.last_open = last_open
        self.limits = []

    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=1500):
        self.limits.append(limit)
        rows, t = [], start_ms
        while t < end_ms and t <= self.last_open and len(rows) < limit:
            rows.append([t, "1", "1", "1", "1", "1", t + MIN - 1, "1", 1, "0.5", "0.5", "0"])
            t += MIN
        return rows


class FetchLimitTest(unittest.TestCase):
    def setUp(self):
        self.now = patch.object(klines_mod.sessions, "now_ms", return_value=T0 + 10_000 * MIN)
        self.now.start()

    def tearDown(self):
        self.now.stop()

    def test_tail_fetch_uses_small_limit(self):
        rest = FakeRest(T0 + 5_000 * MIN)
        got = klines_mod.fetch(rest, "BTCUSDT", "1m", T0, T0 + 2 * MIN)
        self.assertEqual(len(got), 2)
        self.assertLessEqual(rest.limits[0], 100)          # weight 1, not 10

    def test_long_range_still_paginates(self):
        rest = FakeRest(T0 + 5_000 * MIN)
        got = klines_mod.fetch(rest, "BTCUSDT", "1m", T0, T0 + 3_200 * MIN)
        self.assertEqual(len(got), 3_200)
        self.assertEqual(rest.limits[:2], [1500, 1500])


if __name__ == "__main__":
    unittest.main()
