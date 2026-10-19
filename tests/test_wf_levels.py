"""research/wf_levels: acceptance, 80% rule, value watches (T1/X/F), impulse-leg watches."""
import types
import unittest

from data.klines import Candle
from research import wf_levels as wl
from research import wf_state as ws

M1 = 60_000
T0 = 1_780_000_000_000 - (1_780_000_000_000 % (24 * 60 * M1))
LV = types.SimpleNamespace(vah=105.0, val=95.0, poc=100.0)
ATR = 10.0                                     # zone = level +/- 1.0


def minutes(path, start=T0):
    """1m candles from a list of closes (open = previous close, wick 0.05)."""
    out = []
    for k, c in enumerate(path):
        o = path[k - 1] if k else c
        out.append(Candle(start + k * M1, o, max(o, c) + 0.05, min(o, c) - 0.05, c, 1.0,
                          start + (k + 1) * M1 - 1, c, 1, 0.5, 0.5 * c))
    return out


def hold(price, n):
    return [price] * n


class Acceptance(unittest.TestCase):
    def test_two_30m_closes_needed(self):
        c = minutes(hold(100, 30) + hold(106, 30) + hold(100, 30) + hold(106, 60))
        acc = wl.acceptance_times(wl.bars30(c), LV.vah, LV.val)
        self.assertEqual(acc["ABOVE"], c[149].close_time + 1)    # bars 3 and 4 (minutes 90-149)
        self.assertNotIn("BELOW", acc)


class EightyRule(unittest.TestCase):
    def test_opened_above_two_closes_inside(self):
        c = minutes(hold(107, 30) + hold(103, 60) + hold(101, 30))
        kind, side, t, ext = wl.eighty_rule(c, wl.bars30(c), LV.vah, LV.val)
        self.assertEqual((kind, side), ("R", "SELL"))
        self.assertEqual(t, c[89].close_time + 1)
        self.assertAlmostEqual(ext, 107.05)

    def test_control_when_inside_only_once(self):
        c = minutes(hold(107, 30) + hold(103, 30) + hold(107, 60))
        kind, side, t, _ = wl.eighty_rule(c, wl.bars30(c), LV.vah, LV.val)
        self.assertEqual((kind, side, t), ("R_CTRL", "SELL", c[59].close_time + 1))

    def test_opened_inside_is_none(self):
        c = minutes(hold(100, 120))
        self.assertIsNone(wl.eighty_rule(c, wl.bars30(c), LV.vah, LV.val))


class ValueWatches(unittest.TestCase):
    def test_t1_after_acceptance_above(self):
        path = hold(100, 30) + hold(108, 60) + [107, 106.5, 106.2, 105.8] + hold(107, 20)
        c = minutes(path)
        w = wl.value_watches(ws.TREND_UP, False, LV, ATR, c, wl.bars30(c))
        t1 = [x for x in w if x["path"] == "T1"]
        self.assertEqual(len(t1), 1)
        self.assertEqual((t1[0]["side"], t1[0]["kind"], t1[0]["i"]), ("BUY", "VAH", 93))   # minute 93 is the first low <= 106

    def test_no_t1_on_veto_day_or_without_acceptance(self):
        path = hold(100, 30) + hold(108, 60) + [107, 105.8] + hold(107, 20)
        c = minutes(path)
        self.assertEqual(wl.value_watches(ws.TREND_UP, True, LV, ATR, c, wl.bars30(c)), [])
        c2 = minutes(hold(108, 20) + [105.8] + hold(107, 20))       # no 2 x 30m closes above
        self.assertEqual(wl.value_watches(ws.TREND_UP, False, LV, ATR, c2, wl.bars30(c2)), [])

    def test_balance_fade_blocked_after_acceptance(self):
        fade = minutes(hold(100, 40) + [97, 95.8] + hold(98, 30))
        w = wl.value_watches(ws.BALANCE, False, LV, ATR, fade, wl.bars30(fade))
        self.assertEqual([(x["path"], x["side"], x["kind"]) for x in w], [("F", "BUY", "VAL")])
        late = minutes(hold(100, 30) + hold(108, 60) + hold(100, 30) + [97, 95.8] + hold(98, 10))
        w = wl.value_watches(ws.BALANCE, False, LV, ATR, late, wl.bars30(late))
        self.assertFalse([x for x in w if x["path"] == "F" and x["kind"] == "VAL"])
        self.assertTrue([x for x in w if x["path"] == "X" and x["side"] == "BUY"])

    def test_t1_cancelled_after_reclaim_of_value(self):
        # The BASEDUSDT sequence: accepted below VAL (2 x 30m at 94.5, still inside the zone),
        # back inside value for two 30m closes (97), then a dip below the zone and back up
        # into it: the breakdown no longer stands - no T1.
        path = hold(100, 30) + hold(94.5, 60) + hold(97, 60) + [93.5, 93.0, 94.2] + hold(93, 10)
        c = minutes(path)
        acc = wl.acceptance_times(wl.bars30(c), LV.vah, LV.val)
        self.assertEqual(wl.reclaim_time(wl.bars30(c), LV.vah, LV.val, acc["BELOW"]),
                         c[149].close_time + 1)
        w = wl.value_watches(ws.TREND_DOWN, False, LV, ATR, c, wl.bars30(c))
        self.assertFalse([x for x in w if x["path"] == "T1"])
        # Same retest without the reclaim: T1 stands.
        c2 = minutes(hold(100, 30) + hold(94.5, 60) + [93.0, 94.2] + hold(93, 10))
        w2 = wl.value_watches(ws.TREND_DOWN, False, LV, ATR, c2, wl.bars30(c2))
        self.assertEqual([(x["path"], x["side"]) for x in w2], [("T1", "SELL")])

    def test_unclear_state_has_no_watches(self):
        c = minutes(hold(100, 30) + hold(108, 60) + [105.8] + hold(107, 20))
        self.assertEqual(wl.value_watches(ws.UNCLEAR, False, LV, ATR, c, wl.bars30(c)), [])


if __name__ == "__main__":
    unittest.main()
