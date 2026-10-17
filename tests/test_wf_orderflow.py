"""research/wf_orderflow: recipes DEF_INI / EXH_INI / FA_DEF_INI on synthetic footprints."""
import unittest

import numpy as np

from research import of_rules
from research import wf_orderflow as wo
from tests.test_of_rules import RS, T0, ZONE, Day, absorption_day


def add_stack(d, minute, first_row, n=4, buy=12.0, sell=1.0):
    """Add n adjacent rows of aggressive buying (diagonal imbalances) to one minute."""
    d.trades[minute] = list(d.trades[minute]) + [(first_row + k, buy, sell) for k in range(n)]


def mirror(d):
    """Exact mirror about 100 that keeps every row: price p -> 200 - p, buy <-> sell."""
    m = Day.__new__(Day)
    m.o, m.h, m.l, m.c = 200 - d.o, 200 - d.l, 200 - d.h, 200 - d.c
    top = int(round(200 / RS))
    m.trades = {k: [(top - r - 1, se, bu) for r, bu, se in v] for k, v in d.trades.items()}
    return m


def run(d, recipe, side="BUY", zone=ZONE):
    flow = of_rules.DayFlow(d.raw(), (d.o, d.h, d.l, d.c), side)
    return wo.evaluate(flow, zone[0], zone[1], T0, recipe)


def exhaustion_day():
    d = Day()
    d.bar(120, 99.5, 99.5, 98.6, 98.6, buy=20, sell=60)          # TEST
    d.bar(121, 98.6, 98.8, 98.4, 98.7, buy=15, sell=20)          # EXH: new low, weak selling
    d.bar(122, 98.7, 99.7, 98.5, 99.6, buy=60, sell=20)          # turn bar
    return d


class Recipes(unittest.TestCase):
    def test_turn_without_stack_is_not_ini(self):
        d = absorption_day()
        self.assertEqual(d.run()["outcome"], "CONFIRM")              # the plain story confirms
        self.assertNotEqual(run(d, "DEF_INI")["outcome"], "CONFIRM")  # INI needs a stack

    def test_def_ini_with_stack_confirms(self):
        d = absorption_day()
        add_stack(d, 614, 991)
        r = run(d, "DEF_INI")
        self.assertEqual((r["outcome"], r["path"], r["minute"]), ("CONFIRM", "ABS", 614))
        self.assertEqual(r["stack_row"], 991)
        self.assertAlmostEqual(r["ini_high"], 99.7)
        self.assertAlmostEqual(r["hold_low"], 98.5)

    def test_gap_in_rows_breaks_the_stack(self):
        d = absorption_day()
        d.trades[614] = list(d.trades[614]) + [(991, 12, 1), (992, 12, 1), (994, 12, 1)]
        self.assertNotEqual(run(d, "DEF_INI")["outcome"], "CONFIRM")

    def test_exh_ini_rejects_absorption_hold(self):
        d = absorption_day()
        add_stack(d, 614, 991)
        self.assertNotEqual(run(d, "EXH_INI")["outcome"], "CONFIRM")
        e = exhaustion_day()
        add_stack(e, 614, 991)
        r = run(e, "EXH_INI")
        self.assertEqual((r["outcome"], r["path"]), ("CONFIRM", "EXH"))

    def test_fa_needs_a_failed_poke(self):
        d = absorption_day()
        add_stack(d, 614, 991)
        self.assertNotEqual(run(d, "FA_DEF_INI")["outcome"], "CONFIRM")    # never below 98.0
        p = absorption_day()
        p.bar(121, 98.6, 98.9, 97.8, 98.85, buy=30, sell=90)               # poke to 97.8, back in
        add_stack(p, 614, 991)
        r = run(p, "FA_DEF_INI")
        self.assertEqual(r["outcome"], "CONFIRM")
        self.assertTrue(r["fa"])

    def test_accepted_below_is_cancel_not_fa(self):
        d = Day()
        d.bar(120, 99.5, 99.5, 97.4, 97.5, buy=20, sell=60)
        d.bar(121, 97.5, 97.6, 97.2, 97.3, buy=20, sell=40)
        r = run(d, "FA_DEF_INI")
        self.assertEqual((r["outcome"], r["path"]), ("CANCEL", "ACCEPT"))
        self.assertFalse(r["fa"])

    def test_sell_is_exact_mirror(self):
        d = absorption_day()
        add_stack(d, 614, 991)
        r = run(mirror(d), "DEF_INI", side="SELL", zone=(101.0, 102.0))
        self.assertEqual((r["outcome"], r["path"], r["minute"]), ("CONFIRM", "ABS", 614))
        self.assertAlmostEqual(r["hold_low"], 101.5)
        self.assertAlmostEqual(r["ini_high"], 100.3)                  # the INI bar's LOW for a sell
        self.assertEqual(r["stack_row"], int(round(200 / RS)) - 991 - 1)

    def test_causal_future_bars_do_not_change_decision(self):
        d = absorption_day()
        add_stack(d, 614, 991)
        a = run(d, "DEF_INI")
        for b in range(123, 140):
            d.bar(b, 99.6, 99.6, 90.0, 90.5, buy=1, sell=500)          # crash after the decision
        self.assertEqual(run(d, "DEF_INI"), a)


if __name__ == "__main__":
    unittest.main()


class TwoDay(unittest.TestCase):
    def _two(self, d, prev=None, side="BUY"):
        pr, po = (prev.raw(), (prev.o, prev.h, prev.l, prev.c)) if prev else (None, None)
        return wo.TwoDayFlow(pr, po, d.raw(), (d.o, d.h, d.l, d.c), side)

    def test_without_previous_day_identical_to_single_day(self):
        d = absorption_day()
        add_stack(d, 614, 991)
        one = run(d, "DEF_INI")
        two = wo.evaluate(self._two(d), ZONE[0], ZONE[1], T0 + wo.DAY, "DEF_INI")
        self.assertEqual(two["minute"] - wo.DAY, one["minute"])
        self.assertEqual({k: v for k, v in two.items() if k != "minute"},
                         {k: v for k, v in one.items() if k != "minute"})

    def test_early_touch_uses_previous_day_reference(self):
        prev = Day()
        for m in range(1440 - 150, 1440):
            prev.minute(m, 100.0, 100.0, 100.0, 100.0, 5, 5)
        d = Day.__new__(Day)
        d.o = np.full(1440, np.nan); d.h = d.o.copy(); d.l = d.o.copy(); d.c = d.o.copy()
        d.trades = {}
        d.bar(0, 100, 100, 100, 100, 25, 25)
        d.bar(1, 99.5, 99.5, 98.6, 98.6, buy=20, sell=60)        # TEST at minute 5
        d.bar(2, 98.6, 98.9, 98.5, 98.85, buy=30, sell=90)       # HOLD-ABS
        d.bar(3, 98.85, 99.7, 98.6, 99.6, buy=60, sell=20)       # INI
        add_stack(d, 19, 991)
        flat = of_rules.DayFlow(d.raw(), (d.o, d.h, d.l, d.c), "BUY")
        self.assertEqual(wo.evaluate(flat, ZONE[0], ZONE[1], 5, "DEF_INI")["outcome"], "NO_DATA")
        r = wo.evaluate(self._two(d, prev), ZONE[0], ZONE[1], 5 + wo.DAY, "DEF_INI")
        self.assertEqual((r["outcome"], r["minute"] - wo.DAY), ("CONFIRM", 19))

    def test_previous_day_rows_rebinned_to_current_grid(self):
        prev = Day()
        raw0 = prev.raw()
        raw0["row_size"] = np.array([0.2])                     # coarser rows yesterday
        d = absorption_day()
        f = wo.TwoDayFlow(raw0, (prev.o, prev.h, prev.l, prev.c), d.raw(), (d.o, d.h, d.l, d.c), "BUY")
        r_prev = f.row[: len(raw0["row"])]
        expect = np.floor((raw0["row"] + 0.5) * 0.2 / RS).astype(int)
        self.assertTrue(np.array_equal(r_prev, expect))
