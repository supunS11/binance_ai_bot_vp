import unittest

import numpy as np

from data.klines import Candle
from research import confirm_study as cs
from setups.orderflow_reversal import Zone


def _c(i, o, h, l, c, vol=10.0, buy=5.0):
    t = i * 60_000
    return Candle(open_time=t, open=o, high=h, low=l, close=c, volume=vol,
                  close_time=t + 59_999, quote_volume=vol * c, trades=10,
                  taker_buy_base=buy, taker_buy_quote=buy * c)


class AlignmentTests(unittest.TestCase):
    def test_trend_and_shape_both_have_to_allow(self):
        self.assertTrue(cs.aligned("UP", "BULL", "BUY"))
        self.assertFalse(cs.aligned("UP", None, "SELL"))
        self.assertFalse(cs.aligned("ROTATION", "BEAR", "BUY"))
        self.assertTrue(cs.aligned(None, "NEUTRAL", "SELL"))


class DirectionOutcomeTests(unittest.TestCase):
    def test_buy_correct_wrong_ambiguous(self):
        h = np.array([100, 101.0, 102.5]); l = np.array([100, 99.5, 100.0])
        self.assertEqual(cs.direction_outcome(h, l, 0, "BUY", 100.0, 2.0), "CORRECT")
        h = np.array([100, 100.5, 101.0]); l = np.array([100, 97.9, 99.0])
        self.assertEqual(cs.direction_outcome(h, l, 0, "BUY", 100.0, 2.0), "WRONG")
        h = np.array([100, 102.5]); l = np.array([100, 97.5])
        self.assertEqual(cs.direction_outcome(h, l, 0, "BUY", 100.0, 2.0), "AMBIGUOUS")

    def test_confirming_bar_itself_is_ignored(self):
        h = np.array([105.0, 100.5]); l = np.array([95.0, 99.5])
        self.assertEqual(cs.direction_outcome(h, l, 0, "SELL", 100.0, 2.0), "NONE")

    def test_sell_mirror(self):
        h = np.array([100, 100.5]); l = np.array([100, 97.9])
        self.assertEqual(cs.direction_outcome(h, l, 0, "SELL", 100.0, 2.0), "CORRECT")


class FailedAuctionTests(unittest.TestCase):
    zone = Zone("VAL", 100.0, 101.0)

    def test_dip_below_then_reclaim_is_a_buy(self):
        candles = [_c(0, 103, 103.5, 102.5, 103), _c(1, 103, 103, 100.5, 101.2),
                   _c(2, 101.2, 101.3, 99.5, 99.8), _c(3, 99.8, 101.5, 99.7, 101.3)]
        self.assertEqual(cs.failed_auction(candles, self.zone, atr=10.0), (3, "BUY"))

    def test_too_deep_is_not_a_failed_auction(self):
        candles = [_c(0, 103, 103.5, 102.5, 103), _c(1, 103, 103, 100.5, 101.2),
                   _c(2, 101.2, 101.3, 99.5, 99.8), _c(3, 99.8, 99.9, 96.0, 97.0),
                   _c(4, 97.0, 101.5, 97.0, 101.3)]
        self.assertIsNone(cs.failed_auction(candles, self.zone, atr=10.0))

    def test_holding_without_breaking_is_not_a_failed_auction(self):
        candles = [_c(0, 103, 103.5, 102.5, 103), _c(1, 103, 103, 100.5, 101.2),
                   _c(2, 101.2, 102.0, 100.6, 101.8)]
        self.assertIsNone(cs.failed_auction(candles, self.zone, atr=10.0))


if __name__ == "__main__":
    unittest.main()
