import unittest
from types import SimpleNamespace

from data.klines import Candle
from research import replay

MIN = 60_000
STEP = 1_700_000_000_000


def _node(low, high):
    return SimpleNamespace(low_price=low, high_price=high, is_poc=False, peak_price=(low + high) / 2)


def _levels(poc, vah, val):
    return SimpleNamespace(poc_price=poc, vah=vah, val=val, hvns=[])


class _Scanner:
    def __init__(self, bundle):
        self._bundle = bundle

    def frozen_bundle(self, symbol, session_id, as_of):
        return self._bundle, ""


class _Cache:
    def __init__(self, candles):
        self._candles = candles

    def minute_candles(self, symbol, start_ms, end_ms, force=False):
        return [c for c in self._candles if start_ms <= c.open_time < end_ms]


def _candle(minutes_before, low, high):
    open_time = STEP - minutes_before * MIN
    return Candle(open_time=open_time, open=100.0, high=high, low=low, close=100.0, volume=1.0,
                  close_time=open_time + MIN - 1, quote_volume=100.0, trades=1,
                  taker_buy_base=0.5, taker_buy_quote=50.0)


class S4PrecheckTests(unittest.TestCase):
    def setUp(self):
        self.bundle = SimpleNamespace(levels=_levels(poc=100.5, vah=101.0, val=99.0),
                                      atr=1.0)

    def test_price_far_from_every_zone_is_skipped(self):
        candles = [_candle(m, low=105.0, high=105.5) for m in range(1, 31)]
        near = replay._s4_zone_near(_Scanner(self.bundle), _Cache(candles), "X", "S", STEP)
        self.assertFalse(near)

    def test_a_touch_inside_the_window_keeps_the_minute(self):
        candles = [_candle(m, low=105.0, high=105.5) for m in range(1, 31)]
        candles.append(_candle(12, low=100.9, high=101.2))
        near = replay._s4_zone_near(_Scanner(self.bundle), _Cache(candles), "X", "S", STEP)
        self.assertTrue(near)

    def test_missing_bundle_keeps_the_minute_for_the_full_path(self):
        near = replay._s4_zone_near(_Scanner(None), _Cache([]), "X", "S", STEP)
        self.assertTrue(near)


if __name__ == "__main__":
    unittest.main()
