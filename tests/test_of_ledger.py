"""research/of_ledger: accumulated order-flow read on synthetic footprints.

The synthetic market: each minute has random aggressive buy/sell volume, and price moves in
proportion to the minute's delta (impact LAM per unit) plus a little noise - the normal
relation between order flow and price (Cont, Kukanov & Stoikov). Scenarios are appended after
a 1,000-minute reference."""
import unittest

import numpy as np

from research import of_ledger as ol
from research import of_rules

RS = 0.01
LAM = 0.002
T0 = 1000
ZONE = (99.8, 100.2)


class Market:
    def __init__(self, seed=1, start=103.0):
        rng = np.random.default_rng(seed)
        self.rng = rng
        self.buy, self.sell, self.p = [], [], [start]
        for _ in range(T0):
            b, s = rng.gamma(4, 5), rng.gamma(4, 5)
            self.step(b, s)
        # drift down to just above the zone for the touch
        while self.p[-1] > 100.25:
            self.step(5, 25)
        self.touch = len(self.buy)

    def step(self, b, s, impact=LAM, noise=0.01):
        self.buy.append(b)
        self.sell.append(s)
        self.p.append(self.p[-1] + impact * (b - s) + self.rng.normal(0, noise))

    def flat(self, b, s, price):
        self.buy.append(b)
        self.sell.append(s)
        self.p.append(price)

    def flow(self, side="BUY", mirror=False):
        n = len(self.buy)
        p = np.array(self.p)
        o, c = p[:-1], p[1:]
        h, l = np.maximum(o, c) + 0.005, np.minimum(o, c) - 0.005
        buy, sell = np.array(self.buy), np.array(self.sell)
        if mirror:
            o, c, h, l = 200 - o, 200 - c, 200 - l, 200 - h
            buy, sell = sell, buy
        minute = np.arange(n)
        rows = np.floor(c / RS).astype(int)
        raw = {"minute": minute, "row": rows, "buy": buy, "sell": sell, "row_size": np.array([RS])}
        pad = lambda x: np.concatenate([x, np.full(1440 - n, np.nan)])    # noqa: E731
        return of_rules.DayFlow(raw, (pad(o), pad(h), pad(l), pad(c)), side)

    def run(self, side="BUY", mirror=False, zone=ZONE):
        return ol.evaluate(self.flow(side, mirror), zone[0], zone[1], self.touch)


def absorption_then_response(m):
    for _ in range(8):                       # heavy selling, price pinned at the zone
        m.flat(10, 90, 100.0)
    for _ in range(10):                      # buyers lift price with normal impact
        m.step(80, 10)
    for _ in range(30):
        m.step(20, 20)
    return m


class Ledger(unittest.TestCase):
    def test_absorption_then_response_confirms(self):
        r = absorption_then_response(Market()).run()
        self.assertEqual((r["outcome"], r["path"]), ("CONFIRM", "ABS"))
        s = r["stages"]
        self.assertTrue(s["attempt"] <= s["absorption"] < s["response"])

    def test_selling_with_normal_impact_is_not_absorption(self):
        m = Market()
        for _ in range(40):
            m.step(10, 90)                   # sellers push price down freely
        r = m.run()
        self.assertEqual((r["outcome"], r["path"]), ("VETO", "BREAK"))
        self.assertNotIn("absorption", r["stages"])

    def test_acceptance_below_zone_vetoes(self):
        m = Market()
        for _ in range(5):
            m.step(10, 60)
        for _ in range(12):
            m.flat(20, 22, 99.7)             # sitting below the zone with mild net selling
        r = m.run()
        self.assertEqual((r["outcome"], r["path"]), ("VETO", "ACCEPT"))

    def test_quiet_touch_is_no_signal(self):
        m = Market()
        for _ in range(60):
            m.flat(20, 20, 100.3)
        self.assertEqual(m.run()["outcome"], "NO_SIGNAL")

    def test_exhaustion_then_response_confirms(self):
        m = Market()
        for _ in range(2):
            m.step(5, 95)                    # attempt with result: new lows, still in the zone
        m.flat(15, 15, m.p[-1] + 0.02)
        m.flat(19, 3, m.p[-1] - 0.03)        # one more low, almost no selling, CVD higher
        for _ in range(12):
            m.step(90, 10)
        for _ in range(30):
            m.step(20, 20)
        r = m.run()
        self.assertEqual((r["outcome"], r["path"]), ("CONFIRM", "EXH"))

    def test_response_without_price_result_does_not_confirm(self):
        m = Market()
        for _ in range(8):
            m.flat(10, 90, 100.0)            # absorption
        for _ in range(20):
            m.flat(90, 10, 100.05)           # heavy buying that does NOT lift price
        r = m.run()
        self.assertNotEqual(r["outcome"], "CONFIRM")
        self.assertIn("absorption", r["stages"])

    def test_selling_above_the_zone_is_not_absorption_at_the_level(self):
        m = Market()
        m.flat(20, 20, 100.15)               # touch the zone top
        for _ in range(10):
            m.flat(10, 90, 100.6)            # heavy selling, but executed well ABOVE the zone
        r = m.run()
        self.assertIn("attempt", r["stages"])
        self.assertNotIn("absorption", r["stages"])

    def test_sell_side_is_the_mirror(self):
        m = absorption_then_response(Market())
        a = m.run()
        b = m.run(side="SELL", mirror=True, zone=(200 - ZONE[1], 200 - ZONE[0]))
        self.assertEqual((a["outcome"], a["path"], a["minute"]), (b["outcome"], b["path"], b["minute"]))

    def test_causal(self):
        m = absorption_then_response(Market())
        a = m.run()
        for _ in range(20):
            m.step(0, 500)                   # crash after the decision
        self.assertEqual(m.run()["minute"], a["minute"])
        self.assertEqual(m.run()["outcome"], a["outcome"])


if __name__ == "__main__":
    unittest.main()
