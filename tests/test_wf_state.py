"""research/wf_state: state mapping, veto, causality of the day labels."""
import unittest

import numpy as np

from research import wf_state as ws


class Combine(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(ws.combine("UP", "UP"), ws.TREND_UP)
        self.assertEqual(ws.combine("DOWN", "DOWN"), ws.TREND_DOWN)
        self.assertEqual(ws.combine("RANGE", "ROTATION"), ws.BALANCE)
        for t, v in (("UP", "DOWN"), ("UP", "ROTATION"), ("RANGE", "UP"), (None, "UP"),
                     ("UP", None), (None, None)):
            self.assertEqual(ws.combine(t, v), ws.UNCLEAR)

    def test_veto(self):
        self.assertTrue(ws.veto_day(ws.TREND_UP, "THIN_DOWN"))
        self.assertTrue(ws.veto_day(ws.TREND_DOWN, "THIN_UP"))
        self.assertFalse(ws.veto_day(ws.TREND_UP, "THIN_UP"))
        self.assertFalse(ws.veto_day(ws.BALANCE, "THIN_DOWN"))


class Causality(unittest.TestCase):
    def test_turtle_label_for_day_uses_prior_bars_only(self):
        n = 80
        days = [f"d{i:03d}" for i in range(n)]
        c = np.full(n, 100.0)
        c[70] = 200.0                                   # breakout close on day 70
        h, lo = c + 1, c - 1
        lab = ws.turtle_by_day((days, h, lo, c))
        self.assertNotEqual(lab["d070"], "UP")          # not known during day 70
        self.assertEqual(lab["d071"], "UP")             # known the day after

    def test_value_needs_contiguous_history(self):
        import sessions
        start = sessions.session_start_ms_from_id("2026-03-01")
        rows = []
        for i in range(25):
            day = sessions.session_id(start + i * sessions.MS_DAY)
            rows.append({"day": day, "poc": 100.0 + (5 if i >= 10 else 0), "atr": 1.0})
        del rows[12]                                    # a gap: windows spanning it are invalid
        v = ws.value_by_day(rows)
        self.assertIsNone(v[rows[19]["day"]])           # its 20-day window spans the gap
        self.assertEqual(v[rows[0]["day"]], None)       # not enough history


if __name__ == "__main__":
    unittest.main()
