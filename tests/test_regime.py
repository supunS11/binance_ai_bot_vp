"""research/regime.py: the efficiency-ratio cross-check for PLAN item 11.

A pure price-series statistic, so these tests assert it actually SEPARATES a clean
trend from chop - not merely that it returns a number in [0, 1] - the same discipline
test_units.py's AcceptanceDiscriminatesTests holds acceptance.py to.
"""
import unittest

from research.regime import efficiency_ratio
from tests import factories as f

START = 1_758_844_800_000


def _walk(closes):
    """One candle per close, minute-spaced, open==prior close so there's no gap."""
    candles = []
    ts = START
    prev = closes[0]
    for price in closes:
        candles.append(f.candle(ts, prev, max(prev, price), min(prev, price), price))
        prev = price
        ts += 60_000
    return candles


class EfficiencyRatioTests(unittest.TestCase):
    def test_a_straight_line_move_reads_close_to_one(self):
        closes = [100.0 + i * 0.1 for i in range(50)]     # steadily up, no chop
        self.assertAlmostEqual(efficiency_ratio(_walk(closes)), 1.0, places=9)

    def test_pure_back_and_forth_with_no_net_move_reads_zero(self):
        closes = [100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0]
        self.assertAlmostEqual(efficiency_ratio(_walk(closes)), 0.0, places=9)

    def test_chop_with_a_small_net_drift_reads_low_but_not_zero(self):
        # Oscillates hard but drifts up by 1 over ten swings of 2 each - a lot of
        # movement for a little progress, which is exactly what "choppy" means.
        closes = [100.0]
        up = True
        for _ in range(10):
            closes.append(closes[-1] + (2.0 if up else -1.8))
            up = not up
        ratio = efficiency_ratio(_walk(closes))
        self.assertLess(ratio, 0.3, "heavy chop must read as inefficient")
        self.assertGreater(ratio, 0.0, "there IS a net move - not literally zero")

    def test_trend_reads_higher_than_chop_on_the_same_total_distance_travelled(self):
        """THE regression test: a trend and chop covering equal ground must differ."""
        trend = [100.0 + i * 0.2 for i in range(30)]
        chop = [100.0]
        up = True
        for _ in range(29):
            chop.append(chop[-1] + (0.2 if up else -0.18))
            up = not up
        trend_er = efficiency_ratio(_walk(trend))
        chop_er = efficiency_ratio(_walk(chop))
        self.assertGreater(trend_er, chop_er)
        self.assertGreater(trend_er - chop_er, 0.5,
                           "must separate clearly, not just narrowly outrank")

    def test_flat_session_reads_zero_not_a_division_error(self):
        closes = [100.0] * 20
        self.assertEqual(efficiency_ratio(_walk(closes)), 0.0)

    def test_too_few_candles_is_unmeasured(self):
        self.assertIsNone(efficiency_ratio([]))
        self.assertIsNone(efficiency_ratio(_walk([100.0])))

    def test_none_input_is_unmeasured(self):
        self.assertIsNone(efficiency_ratio(None))

    def test_bounded_to_zero_one(self):
        """Net move can never exceed total move by construction - assert it stays so
        across a mixed, randomish-looking path, not just the hand-picked cases above."""
        closes = [100.0]
        for i in range(200):
            step = ((i * 37) % 11 - 5) * 0.13     # deterministic, not literally random
            closes.append(closes[-1] + step)
        ratio = efficiency_ratio(_walk(closes))
        self.assertGreaterEqual(ratio, 0.0)
        self.assertLessEqual(ratio, 1.0)


if __name__ == "__main__":
    unittest.main()
