import unittest
from types import SimpleNamespace
from unittest.mock import patch

import config
from data.klines import Candle
from setups import orderflow_reversal as ofr
from setups.base import Candidate, RejectReason

MIN = 60_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % (24 * 60 * MIN))


def _bar(open_, high, low, close, volume, delta_frac):
    """One 5-minute bar as five 1-minute candles. Every minute carries the bar's full range so a
    touch of the bar is a touch of each minute, and only the first minute carries the volume."""
    taker = volume * (1.0 + delta_frac) / 2.0
    return [(open_, high, low, close, volume, taker)] + [(close, high, low, close, 0.0, 0.0)] * 4


class _Levels:
    poc_price = 100.5
    poc_bin = 0
    vah = 101.0
    val = 99.0
    vwap = 100.4
    hvns = []
    poc_volume = 1.0


class _Profile:
    high = 101.5
    low = 98.5


def _ctx(minutes, last_close_time):
    return SimpleNamespace(
        symbol="TESTUSDT", session_id="2026-01-01", as_of=last_close_time, atr=1.0,
        session_candles=tuple(minutes), prior_levels=_Levels(), prior_profile=_Profile(),
        naked_pocs=(), dev_levels=None, profile_row=lambda: {},
    )


def _candles(bars):
    out = []
    minute = 0
    for bar_minutes in bars:
        for o, h, l, c, v, tb in bar_minutes:
            t = T0 + minute * MIN
            out.append(Candle(open_time=t, open=o, high=h, low=l, close=c, volume=v,
                              close_time=t + MIN - 1, quote_volume=v * c, trades=1,
                              taker_buy_base=tb, taker_buy_quote=tb * c))
            minute += 1
    return out


def _buy_setup(extra_visit=False, follow=(99.25, 99.3, 99.2, 99.22, 12.0, 0.4)):
    base = [_bar(100.5, 100.6, 100.4, 100.5, 10.0, 0.0)] * 60
    if extra_visit:
        base += [_bar(100.2, 100.3, 98.95, 99.3, 10.0, 0.0)]
        base += [_bar(100.5, 100.6, 100.4, 100.5, 10.0, 0.0)] * 2
    arrival = _bar(100.2, 100.3, 99.05, 99.3, 10.0, 0.0)
    absorb = _bar(99.3, 99.4, 99.0, 99.35, 30.0, -0.5)
    prev = _bar(99.35, 99.36, 99.1, 99.2, 10.0, -0.3)
    follow = _bar(*follow)
    bars = base + [arrival, absorb, prev, follow]
    minutes = _candles(bars)
    return minutes


class TierTests(unittest.TestCase):
    def test_absorption_plus_cvd_is_tier_one(self):
        self.assertEqual(ofr.classify_tier({"ABS", "CVD"}, "HVN", True, False, False), "T1")

    def test_absorption_plus_supporting_is_tier_two(self):
        self.assertEqual(ofr.classify_tier({"ABS", "FLIP"}, "HVN", True, False, False), "T2")

    def test_cvd_plus_supporting_is_tier_two(self):
        self.assertEqual(ofr.classify_tier({"CVD", "FLIP"}, "HVN", True, False, False), "T2")

    def test_single_cvd_never_trades(self):
        self.assertIsNone(ofr.classify_tier({"CVD"}, "VAH", True, True, True))

    def test_single_absorption_only_under_the_exception(self):
        self.assertIsNone(ofr.classify_tier({"ABS"}, "VAH", True, True, False))
        self.assertEqual(ofr.classify_tier({"ABS"}, "VAH", True, True, True), "X")

    def test_two_supporting_signals_need_a_strong_zone(self):
        self.assertIsNone(ofr.classify_tier({"FLIP", "RESTING"}, "HVN", True, True, False))
        self.assertEqual(ofr.classify_tier({"FLIP", "RESTING"}, "VAL", True, True, False), "T3")

    def test_unavailable_signals_never_count_as_present(self):
        self.assertIsNone(ofr.classify_tier({"STACKED"}, "VAL", True, True, True))


class SignalTests(unittest.TestCase):
    def test_cvd_divergence_for_a_buy_needs_a_lower_low_and_higher_cvd(self):
        rows = ([(100, 100.1, 99.2, 99.5, 10.0, -0.4)] * 5
                + [(99.5, 99.6, 99.0, 99.2, 10.0, -0.4)]
                + [(99.2, 99.3, 98.5, 98.8, 10.0, 0.4)] * 6)
        bars = [Candle(open_time=i, open=o, high=h, low=l, close=c, volume=v, close_time=i,
                       quote_volume=0, trades=0, taker_buy_base=v * (1.0 + d) / 2.0,
                       taker_buy_quote=0)
                for i, (o, h, l, c, v, d) in enumerate(rows)]
        self.assertTrue(ofr._cvd_divergence(bars, "BUY"))


def _node(price, is_poc=False):
    return SimpleNamespace(peak_price=price, is_poc=is_poc)


def _levels(poc, vah, val, hvns=()):
    return SimpleNamespace(poc_price=poc, vah=vah, val=val, hvns=list(hvns))


class TargetLadderTests(unittest.TestCase):
    def test_nearest_hvn_that_clears_the_minimum_r_is_tp1(self):
        levels = _levels(poc=101.3, vah=102.5, val=99.0, hvns=[_node(101.5), _node(100.9)])
        (kind, price), _ = ofr._target_ladder(100.0, "BUY", 1.0, levels, [], levels.hvns, 103.0)
        self.assertEqual((kind, price), ("HVN", 101.5))

    def test_hvn_below_the_minimum_r_falls_through_to_the_poc(self):
        levels = _levels(poc=101.3, vah=102.5, val=99.0, hvns=[_node(100.9)])
        (kind, price), _ = ofr._target_ladder(100.0, "BUY", 1.0, levels, [], levels.hvns, 103.0)
        self.assertEqual((kind, price), ("POC", 101.3))

    def test_no_structural_level_clears_falls_back_to_two_r(self):
        levels = _levels(poc=100.5, vah=100.8, val=99.0)
        (kind, price), tp2 = ofr._target_ladder(100.0, "BUY", 1.0, levels, [], [], 100.6)
        self.assertEqual((kind, price), ("fixed2R", 102.0))
        self.assertEqual(tp2, (None, None))

    def test_tp2_is_the_next_level_beyond_tp1(self):
        levels = _levels(poc=101.3, vah=102.5, val=99.0, hvns=[_node(101.5)])
        _, (kind, price) = ofr._target_ladder(100.0, "BUY", 1.0, levels, [], levels.hvns, 103.0)
        self.assertEqual((kind, price), ("VAH", 102.5))

    def test_sell_side_mirrors_the_buy_side(self):
        levels = _levels(poc=98.7, vah=101.0, val=97.5, hvns=[_node(98.5)])
        (kind, price), _ = ofr._target_ladder(100.0, "SELL", 1.0, levels, [], levels.hvns, 97.0)
        self.assertEqual((kind, price), ("HVN", 98.5))


class PlanEntryStopTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(config, "S4_OFR_ENABLED", True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_passive_entry_sits_at_the_zone_edge_plus_buffer(self):
        minutes = _buy_setup()
        result = ofr.detect(_ctx(minutes, minutes[-1].close_time))
        atr15 = result.attributes["atr15"]
        self.assertAlmostEqual(result.entry_price, max(98.9 + 0.10 * atr15, 99.0))
        self.assertLess(result.entry_price, minutes[-1].close)

    def test_follow_through_further_than_half_an_atr15_is_refused(self):
        minutes = _buy_setup(follow=(99.3, 99.5, 99.25, 99.4, 12.0, 0.4))
        result = ofr.detect(_ctx(minutes, minutes[-1].close_time))
        self.assertTrue(result.is_rejection)
        self.assertIn("ENTRY_TOO_FAR", result.detail)


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(config, "S4_OFR_ENABLED", True)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_first_pullback_with_absorption_and_flip_produces_a_tier_two_buy(self):
        minutes = _buy_setup()
        result = ofr.detect(_ctx(minutes, minutes[-1].close_time))
        self.assertIsInstance(result, Candidate)
        self.assertEqual(result.direction, "BUY")
        self.assertEqual(result.attributes["ofr_tier"], "T2")
        self.assertIn("ABS", result.attributes["ofr_signals"])
        self.assertIn("FLIP", result.attributes["ofr_signals"])
        self.assertLess(result.stop_price, 98.95)

    def test_second_visit_to_the_zone_is_ignored(self):
        minutes = _buy_setup(extra_visit=True)
        result = ofr.detect(_ctx(minutes, minutes[-1].close_time))
        self.assertTrue(result.is_rejection)
        self.assertEqual(result.reason, RejectReason.NOT_FIRST_TEST)

    def test_disabled_flag_rejects_without_looking_at_data(self):
        with patch.object(config, "S4_OFR_ENABLED", False):
            result = ofr.detect(_ctx([], 0))
        self.assertTrue(result.is_rejection)
        self.assertEqual(result.reason, RejectReason.SETUP_DISABLED)


if __name__ == "__main__":
    unittest.main()
