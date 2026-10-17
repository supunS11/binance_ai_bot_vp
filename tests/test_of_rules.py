import unittest

import numpy as np

from research import of_rules as rules

RS = 0.1                       # row size
ZONE = (98.0, 99.0)            # support zone for BUY scenarios
T0 = 600                       # touch minute; bar 120 at 5-minute bars


class Day:
    """Synthetic per-minute footprint. Before the touch: quiet, price 100, 5 bought + 5 sold
    per minute (reference median bar volume = 50 at 5m)."""

    def __init__(self):
        self.o = np.full(1440, np.nan); self.h = self.o.copy(); self.l = self.o.copy(); self.c = self.o.copy()
        self.trades = {}                                     # minute -> list of (row, buy, sell)
        for m in range(T0 - 150, T0):
            self.minute(m, 100.0, 100.0, 100.0, 100.0, 5, 5)

    def minute(self, m, o, h, l, c, buy, sell, rows=None):
        self.o[m], self.h[m], self.l[m], self.c[m] = o, h, l, c
        if rows is None:
            rows = [(int(np.floor(c / RS)), buy, sell)]
        self.trades[m] = rows

    def bar(self, b, o, h, l, c, buy, sell, size=5):
        """One bar: flat at o, high reached in minute 1, low in minute 2, close in the last."""
        a = b * size
        for k in range(size):
            mo = o if k < size - 1 else o
            mc = o if k < size - 1 else c
            mh = h if k == 1 else max(mo, mc)
            ml = l if k == 2 else min(mo, mc)
            self.minute(a + k, mo, mh, ml, mc, buy / size, sell / size)

    def flat(self, b, price, vol=50, size=5):
        self.bar(b, price, price, price, price, vol / 2, vol / 2, size)

    def raw(self):
        mins, rows, buys, sells = [], [], [], []
        for m, lst in sorted(self.trades.items()):
            for r, bu, se in lst:
                mins.append(m); rows.append(r); buys.append(bu); sells.append(se)
        return {"minute": np.array(mins), "row": np.array(rows), "buy": np.array(buys),
                "sell": np.array(sells), "row_size": np.array([RS])}

    def mirrored(self):
        """Exact mirror about 100: prices p -> 200 - p, buyers <-> sellers."""
        m = Day.__new__(Day)
        m.o, m.h, m.l, m.c = 200 - self.o, 200 - self.l, 200 - self.h, 200 - self.c
        m.trades = {k: [(int(np.floor((200 - self.c[k]) / RS)), se, bu) for _, bu, se in v]
                    for k, v in self.trades.items()}
        return m

    def run(self, side="BUY", zone=ZONE, size=5, setting="MEDIUM", touch=T0):
        flow = rules.DayFlow(self.raw(), (self.o, self.h, self.l, self.c), side)
        return rules.evaluate(flow, zone[0], zone[1], touch, size, setting)


def absorption_day():
    d = Day()
    d.bar(120, 99.5, 99.5, 98.6, 98.6, buy=20, sell=60)     # TEST: sellers push in, close at low
    d.bar(121, 98.6, 98.9, 98.5, 98.85, buy=30, sell=90)    # HOLD-ABS: heavy selling, close high
    d.bar(122, 98.85, 99.7, 98.6, 99.6, buy=60, sell=20)    # TURN: buyers + close above 99.5
    return d


def late_turn_day(turn_bar):
    """TEST at bar 120, HOLD at 121 (ends minute 609), flat until the TURN at turn_bar."""
    d = Day()
    d.bar(120, 99.5, 99.5, 98.6, 98.6, buy=20, sell=60)
    d.bar(121, 98.6, 98.9, 98.5, 98.85, buy=30, sell=90)
    for b in range(122, turn_bar):
        d.flat(b, 98.85)
    d.bar(turn_bar, 98.85, 99.7, 98.6, 99.6, buy=60, sell=20)
    for b in range(turn_bar + 1, turn_bar + 5):
        d.flat(b, 99.6)
    return d


class RuleTests(unittest.TestCase):
    def test_absorption_then_turn_confirms_at_the_turn_bar(self):
        r = absorption_day().run()
        self.assertEqual((r["outcome"], r["path"], r["minute"]), ("CONFIRM", "ABS", 614))
        self.assertAlmostEqual(r["hold_low"], 98.5)
        self.assertEqual((r["test_min"], r["hold_min"]), (604, 609))

    def test_sell_side_is_the_exact_mirror(self):
        r = absorption_day().mirrored().run(side="SELL", zone=(101.0, 102.0))
        self.assertEqual((r["outcome"], r["path"], r["minute"]), ("CONFIRM", "ABS", 614))
        self.assertAlmostEqual(r["hold_low"], 101.5)                 # the hold HIGH for a sell

    def test_exhaustion_path(self):
        d = Day()
        d.bar(120, 99.5, 99.5, 98.6, 98.6, buy=20, sell=60)          # TEST
        d.bar(121, 98.6, 98.8, 98.4, 98.7, buy=15, sell=20)          # new low on weak selling
        d.bar(122, 98.7, 99.7, 98.5, 99.6, buy=60, sell=20)          # TURN
        r = d.run()
        self.assertEqual((r["outcome"], r["path"], r["minute"]), ("CONFIRM", "EXH", 614))

    def test_acceptance_below_the_zone_cancels(self):
        d = Day()
        d.bar(120, 99.5, 99.5, 97.4, 97.5, buy=20, sell=60)
        d.bar(121, 97.5, 97.6, 97.2, 97.3, buy=20, sell=40)
        r = d.run()
        self.assertEqual((r["outcome"], r["path"], r["minute"]), ("CANCEL", "ACCEPT", 609))

    def test_one_close_below_then_back_is_not_a_cancel(self):
        d = absorption_day()
        d.bar(121, 98.6, 98.9, 97.8, 97.9, buy=30, sell=90)           # dips and closes below once
        d.bar(122, 97.9, 98.9, 97.9, 98.8, buy=40, sell=40)
        self.assertNotEqual(d.run()["outcome"], "CANCEL")

    def test_turn_without_a_hold_does_not_confirm(self):
        d = Day()
        d.bar(120, 99.5, 99.5, 98.6, 98.6, buy=20, sell=60)          # TEST, no hold
        d.bar(121, 98.6, 99.7, 98.6, 99.6, buy=60, sell=20)          # buyers, but no hold first
        for b in range(122, 135):
            d.flat(b, 99.6)
        self.assertEqual(d.run()["outcome"], "NO_SIGNAL")

    def test_quiet_touch_is_no_signal(self):
        d = Day()
        for b in range(120, 135):
            d.flat(b, 98.9)
        self.assertEqual(d.run()["outcome"], "NO_SIGNAL")

    def test_failed_hold_is_discarded(self):
        d = absorption_day()
        d.bar(122, 98.85, 98.9, 98.3, 98.35, buy=20, sell=60)         # undercuts, closes weak
        d.bar(123, 98.35, 99.7, 98.4, 99.6, buy=60, sell=20)          # would have been a turn
        self.assertEqual(d.run()["outcome"], "NO_SIGNAL")

    def test_undercut_that_is_itself_exhaustion_becomes_the_new_hold(self):
        d = absorption_day()
        d.bar(122, 98.85, 98.9, 98.3, 98.8, buy=30, sell=30)          # new low, weak selling, closes high
        d.bar(123, 98.8, 99.7, 98.6, 99.6, buy=60, sell=20)
        r = d.run()
        self.assertEqual((r["outcome"], r["path"], r["minute"]), ("CONFIRM", "EXH", 619))
        self.assertAlmostEqual(r["hold_low"], 98.3)

    def test_sixty_minute_limit(self):
        # V1 has no turn window, so this isolates the 60-minute limit
        self.assertEqual(late_turn_day(131).run(setting="MEDIUM_V1")["outcome"], "CONFIRM")   # ends 659
        self.assertEqual(late_turn_day(132).run(setting="MEDIUM_V1")["outcome"], "NO_SIGNAL")  # ends 664

    # ---- revision R2
    def test_r2_turn_must_come_within_30_minutes_of_the_hold(self):
        self.assertEqual(late_turn_day(127).run()["outcome"], "CONFIRM")      # 30 min after the hold
        self.assertEqual(late_turn_day(128).run()["outcome"], "NO_SIGNAL")    # 35 min: hold expired
        self.assertEqual(late_turn_day(128).run(setting="MEDIUM_V1")["outcome"], "CONFIRM")

    def test_r2_turn_needs_real_volume(self):
        d = absorption_day()
        d.bar(122, 98.85, 99.7, 98.6, 99.6, buy=40, sell=15)             # 55 = 1.1 x typical
        self.assertEqual(d.run()["outcome"], "NO_SIGNAL")
        self.assertEqual(d.run(setting="MEDIUM_V1")["outcome"], "CONFIRM")

    def test_r2_barely_past_the_edge_is_not_a_cancel(self):
        d = Day()
        d.bar(120, 99.5, 99.5, 97.9, 97.95, buy=20, sell=60)             # closes 0.05 below the zone
        d.bar(121, 97.95, 98.0, 97.9, 97.95, buy=20, sell=40)
        self.assertNotEqual(d.run()["outcome"], "CANCEL")
        self.assertEqual(d.run(setting="MEDIUM_V1")["outcome"], "CANCEL")

    def test_r2_low_volume_drift_below_is_not_a_cancel(self):
        d = Day()
        d.bar(120, 98.5, 98.5, 97.4, 97.5, buy=5, sell=15)               # 20 = 0.4 x typical
        d.bar(121, 97.5, 97.6, 97.2, 97.3, buy=5, sell=15)
        self.assertNotEqual(d.run()["outcome"], "CANCEL")
        self.assertEqual(d.run(setting="MEDIUM_V1")["outcome"], "CANCEL")

    def test_later_bars_cannot_change_a_decision(self):
        a = absorption_day().run()
        d = absorption_day()
        for b in range(123, 135):
            d.bar(b, 99.6, 99.6, 90.0, 90.5, buy=10, sell=500)        # crash after the decision
        self.assertEqual(d.run(), a)

    def test_settings_are_ordered_strict_to_loose(self):
        s, m, l = (rules.SETTINGS[k] for k in ("STRICT", "MEDIUM", "LOOSE"))
        for f in ("v_test", "d_test", "v_abs", "d_abs", "c_frac", "d_turn", "v_turn"):
            self.assertTrue(getattr(s, f) >= getattr(m, f) >= getattr(l, f), f)
        self.assertTrue(s.e_frac <= m.e_frac <= l.e_frac)

    def test_missing_reference_is_no_data(self):
        d = absorption_day()
        d.o[:T0] = np.nan; d.h[:T0] = np.nan; d.l[:T0] = np.nan; d.c[:T0] = np.nan
        self.assertEqual(d.run()["outcome"], "NO_DATA")


if __name__ == "__main__":
    unittest.main()
