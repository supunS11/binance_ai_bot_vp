import unittest

import numpy as np

from research.confirm_lab import simulate


def _arr(*rows):
    h, l, c = zip(*rows)
    return np.array(h, float), np.array(l, float), np.array(c, float)


class SimulateTests(unittest.TestCase):
    def test_buy_target(self):
        h, l, c = _arr((100.5, 99.5, 100), (102.1, 100, 102))
        self.assertEqual(simulate(h, l, c, "BUY", 100, 99, 102, False), ("TARGET", 2.0, 1))

    def test_stop_wins_a_tie(self):
        h, l, c = _arr((102.5, 98.5, 100),)
        self.assertEqual(simulate(h, l, c, "BUY", 100, 99, 102, False)[0], "STOP")

    def test_breakeven_after_plus_one_r(self):
        h, l, c = _arr((101.2, 99.8, 101), (100.5, 99.9, 100))
        self.assertEqual(simulate(h, l, c, "BUY", 100, 99, 102, True), ("BREAKEVEN", 0.0, 1))
        self.assertEqual(simulate(h, l, c, "BUY", 100, 99, 102, False)[0], "UNRESOLVED")

    def test_breakeven_not_triggered_on_the_same_bar(self):
        h, l, c = _arr((101.2, 99.95, 101), (102.2, 100.5, 102))
        self.assertEqual(simulate(h, l, c, "BUY", 100, 99, 102, True)[0], "TARGET")

    def test_sell_mirror(self):
        h, l, c = _arr((100.5, 99.5, 100), (100.2, 97.9, 98))
        self.assertEqual(simulate(h, l, c, "SELL", 100, 101, 98, False), ("TARGET", 2.0, 1))
        h, l, c = _arr((101.1, 99.9, 101),)
        self.assertEqual(simulate(h, l, c, "SELL", 100, 101, 98, False)[0], "STOP")


if __name__ == "__main__":
    unittest.main()
