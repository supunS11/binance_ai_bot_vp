"""research/wf_composite + the new wf_levels watches (FC, breakout legs, duplicate drop) and the
composite target candidates."""
import unittest
from types import SimpleNamespace

import sessions
from data.klines import Candle
from research import wf_composite as wc
from research import wf_levels as wl
from research import wf_state as ws
from research import wf_trade as wt

DAY0 = sessions.session_start_ms_from_id("2026-05-11")


def c1(t, o, h, l, c, v=10.0):
    return Candle(open_time=t, open=o, high=h, low=l, close=c, volume=v, close_time=t + 59_999,
                  quote_volume=v * c, trades=1, taker_buy_base=v / 2, taker_buy_quote=v * c / 2)


def row(day_offset, poc, width=2.0):
    return {"day": sessions.session_id(DAY0 + day_offset * sessions.MS_DAY), "poc": poc,
            "vah": poc + width / 2, "val": poc - width / 2, "atr": 4.0, "high": poc + 3,
            "low": poc - 3}


class MidLabelTest(unittest.TestCase):
    def test_labels(self):
        sid = sessions.session_id(DAY0)
        up = [row(-4, 100), row(-3, 103), row(-2, 106), row(-1, 109)]
        self.assertEqual(wc.mid_label(up, sid), wc.MID_UP)
        down = [row(-4, 109), row(-3, 106), row(-2, 103), row(-1, 100)]
        self.assertEqual(wc.mid_label(down, sid), wc.MID_DOWN)
        mixed = [row(-4, 100), row(-3, 103), row(-2, 100), row(-1, 103)]
        self.assertEqual(wc.mid_label(mixed, sid), wc.MID_ROT)
        self.assertIsNone(wc.mid_label(up[1:], sid))                       # too few days
        holed = [row(-5, 97)] + up[:2] + up[3:]
        self.assertIsNone(wc.mid_label(holed, sid))                         # not contiguous

    def test_alignment_and_fc_sides(self):
        self.assertEqual(wc.alignment(ws.TREND_UP, wc.MID_UP), wc.ALIGNED)
        self.assertEqual(wc.alignment(ws.TREND_UP, wc.MID_ROT), wc.RANGE)
        self.assertEqual(wc.alignment(ws.TREND_UP, wc.MID_DOWN), wc.CONFLICT)
        self.assertEqual(wc.alignment(ws.TREND_DOWN, wc.MID_DOWN), wc.ALIGNED)
        self.assertEqual(wc.alignment(ws.BALANCE, wc.MID_UP), "")
        self.assertEqual(wc.alignment(ws.TREND_UP, None), "")
        self.assertEqual(wc.fc_sides(ws.BALANCE, ""), ("BUY", "SELL"))
        self.assertEqual(wc.fc_sides(ws.TREND_UP, wc.RANGE), ("BUY",))
        self.assertEqual(wc.fc_sides(ws.TREND_DOWN, wc.RANGE), ("SELL",))
        self.assertEqual(wc.fc_sides(ws.TREND_UP, wc.ALIGNED), ())


class CompositeTest(unittest.TestCase):
    def test_composite_from_ten_days(self):
        candles = []
        for d in range(10, 0, -1):
            s = DAY0 - d * sessions.MS_DAY
            for m in range(1440):
                p = 100 + (2 if m % 7 == 0 else 0) + (m % 3) * 0.5
                candles.append(c1(s + m * 60_000, p, p + 0.5, p - 0.5, p))
        comp = wc.composite("XUSDT", candles, DAY0, 0.25, 4.0)
        self.assertIsNotNone(comp)
        self.assertTrue(comp.val <= comp.poc <= comp.vah)
        self.assertTrue(comp.low <= comp.val and comp.vah <= comp.high)
        self.assertIsNone(wc.composite("XUSDT", candles[:500], DAY0, 0.25, 4.0))
        later = candles + [c1(DAY0 + 60_000, 200, 201, 199, 200, 1e6)]   # today is ignored
        self.assertEqual(wc.composite("XUSDT", later, DAY0, 0.25, 4.0).poc, comp.poc)

    def test_confluence(self):
        comp = SimpleNamespace(vah=110.0, val=90.0, poc=100.0, hvns=[(104, 106, 105.0)])
        self.assertEqual(wc.confluent((99.6, 100.4), comp, 4.0), 1)          # on the POC
        self.assertEqual(wc.confluent((104.8, 105.6), comp, 4.0), 1)         # on an HVN peak
        self.assertEqual(wc.confluent((95.6, 96.4), comp, 4.0), 0)
        self.assertEqual(wc.confluent((95.6, 96.4), None, 4.0), 0)
        fc = {"path": "FC", "zone": (89.6, 90.4)}
        self.assertEqual(wc.watch_confluence(fc, comp, 4.0), "")
        self.assertEqual(wc.value_watch_targets(comp, "BUY"), (100.0, 110.0))
        self.assertEqual(wc.value_watch_targets(comp, "SELL"), (100.0, 90.0))


class CompositeWatchTest(unittest.TestCase):
    def _session(self, prices):
        return [c1(DAY0 + i * 60_000, p, p + 0.1, p - 0.1, p) for i, p in enumerate(prices)]

    def test_fc_buy_at_composite_val(self):
        comp = SimpleNamespace(vah=110.0, val=90.0, poc=100.0, hvns=[])
        candles = self._session([95.0] * 30 + [93, 91.5, 90.2, 90.0, 91.0] + [92.0] * 30)
        w = wl.composite_watches(ws.BALANCE, comp, 4.0, candles, wl.bars30(candles))
        self.assertEqual([(x["path"], x["side"], x["kind"], x["i"]) for x in w],
                         [("FC", "BUY", "CVAL", 32)])
        self.assertEqual(wl.composite_watches(ws.BALANCE, comp, 4.0, candles, wl.bars30(candles),
                                              ("SELL",)), [])

    def test_fc_needs_no_acceptance_outside_first(self):
        comp = SimpleNamespace(vah=110.0, val=90.0, poc=100.0, hvns=[])
        below = [85.0] * 60                      # two 30m closes below cVAL = accepted below
        candles = self._session(below + [95.0] * 5 + [90.2] * 3)
        self.assertEqual(wl.composite_watches(ws.BALANCE, comp, 4.0, candles, wl.bars30(candles)),
                         [])

    def test_duplicate_fc_dropped(self):
        f = {"path": "F", "side": "BUY", "zone": (89.5, 90.5)}
        fc = {"path": "FC", "side": "BUY", "zone": (89.9, 90.7)}
        fc_far = {"path": "FC", "side": "BUY", "zone": (80.0, 80.8)}
        fc_other_side = {"path": "FC", "side": "SELL", "zone": (89.9, 90.7)}
        self.assertEqual(wl.drop_duplicate_fc([f, fc, fc_far, fc_other_side]),
                         [f, fc_far, fc_other_side])

    def test_breakout_legs_only_on_balance(self):
        lv = SimpleNamespace(vah=110.0, val=90.0, poc=100.0, hvns=[])
        self.assertEqual(wl.breakout_leg_watches(ws.TREND_UP, lv, 4.0, [], self._session([100] * 5),
                                                 [], lambda a, b: None), [])
        self.assertEqual(wl.breakout_leg_watches(ws.BALANCE, lv, 4.0, [], self._session([100] * 5),
                                                 [], lambda a, b: None), [])     # no acceptance


class TargetsTest(unittest.TestCase):
    def test_composite_levels_are_target_candidates(self):
        lv = SimpleNamespace(vah=105, val=95, poc_price=100, hvns=[])
        prof = SimpleNamespace(high=108, low=92)
        comp = SimpleNamespace(vah=120.0, val=80.0, poc=99.0, hvns=[(113, 115, 114.0)])
        c = wt.target_candidates(lv, prof, [], 98, 102, [], 2.0, wc.composite_levels(comp))
        for p in (120.0, 80.0, 99.0, 114.0):
            self.assertIn(p, c)
        t1, t2 = wt.pick_targets("BUY", 101.0, 3.0, c)        # 2R = 107
        self.assertEqual((t1, t2), (105, 108))


class DpocTest(unittest.TestCase):
    def test_position(self):
        lv = SimpleNamespace(vah=105.0, val=95.0)
        above = [c1(DAY0 + i * 60_000, 108, 108.2, 107.8, 108) for i in range(30)]
        inside = [c1(DAY0 + i * 60_000, 100, 100.2, 99.8, 100) for i in range(30)]
        self.assertEqual(wc.dpoc_position("XUSDT", above, DAY0, 0.25, lv), "ABOVE")
        self.assertEqual(wc.dpoc_position("XUSDT", inside, DAY0, 0.25, lv), "INSIDE")
        self.assertEqual(wc.dpoc_position("XUSDT", [], DAY0, 0.25, lv), "")


if __name__ == "__main__":
    unittest.main()
