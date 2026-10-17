"""research/impulse_leg: leg finding, arming, cancellation, causality, mirror, LVN rule."""
import unittest

import numpy as np

from data.klines import Candle
from research import impulse_leg as il

M15 = 15 * 60_000
T0 = 1_780_000_000_000 - (1_780_000_000_000 % M15)


def bars_from(closes, wick=0.05):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append(Candle(open_time=T0 + i * M15, open=o, high=max(o, c) + wick,
                          low=min(o, c) - wick, close=c, volume=1.0,
                          close_time=T0 + (i + 1) * M15 - 1, quote_volume=c,
                          trades=1, taker_buy_base=0.5, taker_buy_quote=0.5 * c))
    return out


def up_leg():
    # base at 100, fast rise to 103 (3 ATR with ATR=1) in 12 bars, flat top, pullback
    return [100.0] * 40 + [100 + 0.25 * k for k in range(1, 13)] + [103.0] * 10 + \
        [102.9, 102.7, 102.5, 102.3]


def end_ms(b):
    return b[-1].close_time + 1


class FindLeg(unittest.TestCase):
    def test_up_leg_found_and_armed(self):
        b = bars_from(up_leg())
        leg = il.find_leg(b, 1.0, "BUY", end_ms(b))
        self.assertIsInstance(leg, dict)
        self.assertEqual(b[leg["start"]].close, 100.0)
        self.assertGreaterEqual(b[leg["end"]].close, 103.0 - il.END_ATR)   # ends near the top
        self.assertLess(leg["end"], 52)                                      # not the flat top
        self.assertEqual(b[leg["armed"]].close, 102.7)                       # first close 0.25 back

    def test_not_finished_before_pullback(self):
        b = bars_from(up_leg())
        cut = b[61].close_time + 1                                          # before any pullback
        self.assertEqual(il.find_leg(b, 1.0, "BUY", cut), "not_finished")

    def test_causal_ignores_future_bars(self):
        b = bars_from(up_leg())
        now = end_ms(b)
        crash = bars_from(up_leg() + [95.0] * 5)
        self.assertEqual(il.find_leg(b, 1.0, "BUY", now), il.find_leg(crash, 1.0, "BUY", now))

    def test_cancelled_below_start(self):
        b = bars_from(up_leg() + [101.0, 99.5])
        self.assertEqual(il.find_leg(b, 1.0, "BUY", end_ms(b)), "cancelled")

    def test_slow_move_is_not_an_impulse(self):
        closes = [100.0] * 20 + [100 + 3.0 * k / 300 for k in range(1, 301)] + [102.6]
        b = bars_from(closes)
        self.assertEqual(il.find_leg(b, 1.0, "BUY", end_ms(b)), "no_impulse")

    def test_wick_spike_alone_is_no_leg(self):
        b = bars_from([100.0] * 60)
        spike = b[30]
        b[30] = Candle(spike.open_time, 100, 105, 100, 100, 1, spike.close_time, 100, 1, .5, 50)
        self.assertIn(il.find_leg(b, 1.0, "BUY", end_ms(b)), ("no_swing", "no_impulse"))

    def test_sell_is_mirror(self):
        b = bars_from([200 - c for c in up_leg()])
        leg = il.find_leg(b, 1.0, "SELL", end_ms(b))
        self.assertIsInstance(leg, dict)
        self.assertEqual(b[leg["start"]].close, 100.0)
        self.assertEqual(b[leg["armed"]].close, 200 - 102.7)


class FindLvn(unittest.TestCase):
    def test_valley_between_bulges(self):
        p = np.array([1, 2, 6, 9, 8, 5, 2, 1, 1, 2, 5, 10, 9, 6, 3, 1, 1, 1, 1, 1], float)
        row, ratio = il.find_lvn(p)
        self.assertIn(row, (7, 8))
        self.assertLessEqual(ratio, il.VALLEY)

    def test_monotonic_tail_is_not_lvn(self):
        p = np.array([10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], float)
        self.assertIsNone(il.find_lvn(p))

    def test_shallow_dip_above_poc_floor_rejected(self):
        p = np.array([1, 4, 10, 10, 10, 10, 6, 4.5, 6, 10, 10, 10, 10, 9, 8, 4, 2, 1, 1, 1], float)
        self.assertIsNone(il.find_lvn(p))       # 4.5 > 0.4 x POC even though < 0.5 x sides


if __name__ == "__main__":
    unittest.main()
