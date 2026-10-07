import unittest
from unittest.mock import patch

import config
from data import klines as klines_mod
from data.store import KlineCache
from data.ws_feed import KlineFeed, candle_from_payload, market_base, stale_sockets

MIN = 60_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % (24 * MIN * 60))


def _payload(open_time, closed=True, close=100.5):
    return {"t": open_time, "T": open_time + MIN - 1, "s": "BTCUSDT", "i": "1m",
            "o": "100.0", "c": str(close), "h": "101.0", "l": "99.0", "v": "12.5",
            "n": 40, "x": closed, "q": "1250.0", "V": "7.5", "Q": "750.0"}


def _candle(open_time, close=100.5):
    return candle_from_payload(_payload(open_time, close=close))


class _Rest:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        raise AssertionError(f"unexpected REST call {name}")


class PayloadTests(unittest.TestCase):
    def test_a_closed_kline_maps_to_the_rest_candle_fields(self):
        candle = _candle(T0)
        self.assertEqual(candle.open_time, T0)
        self.assertEqual(candle.close_time, T0 + MIN - 1)
        self.assertEqual((candle.open, candle.high, candle.low, candle.close),
                         (100.0, 101.0, 99.0, 100.5))
        self.assertEqual((candle.volume, candle.quote_volume, candle.trades),
                         (12.5, 1250.0, 40))
        self.assertEqual((candle.taker_buy_base, candle.taker_buy_quote), (7.5, 750.0))

    def test_a_forming_kline_is_not_a_candle(self):
        self.assertIsNone(candle_from_payload(_payload(T0, closed=False)))

    def test_market_base_follows_the_testnet_flag(self):
        with patch.object(config, "USE_TESTNET", True):
            self.assertIn("binancefuture", market_base())
        with patch.object(config, "USE_TESTNET", False):
            self.assertIn("fstream.binance.com/market", market_base())


class ContiguityTests(unittest.TestCase):
    def _cache_with(self, minutes):
        cache = KlineCache(_Rest())
        cache._minute["BTCUSDT"] = {T0 + i * MIN: _candle(T0 + i * MIN) for i in range(minutes)}
        return cache

    def test_the_next_minute_extends_the_series(self):
        cache = self._cache_with(3)
        self.assertTrue(cache.ingest("BTCUSDT", _candle(T0 + 3 * MIN, close=102.0)))
        self.assertEqual(max(cache._minute["BTCUSDT"]), T0 + 3 * MIN)

    def test_a_skipped_minute_is_dropped_for_rest_to_fill(self):
        cache = self._cache_with(3)
        self.assertFalse(cache.ingest("BTCUSDT", _candle(T0 + 5 * MIN)))
        self.assertEqual(max(cache._minute["BTCUSDT"]), T0 + 2 * MIN)

    def test_no_series_means_no_ingest(self):
        cache = KlineCache(_Rest())
        self.assertFalse(cache.ingest("BTCUSDT", _candle(T0)))
        self.assertNotIn("BTCUSDT", cache._minute)


class StalenessTests(unittest.TestCase):
    def test_only_sockets_silent_past_the_threshold_are_stale(self):
        self.assertEqual(stale_sockets({0: 0.0, 1: 50.0}, now=60.0, stale_after=45.0), [0])

    def test_a_recent_socket_is_not_stale(self):
        self.assertEqual(stale_sockets({0: 40.0}, now=60.0, stale_after=45.0), [])


class FeedDefaultsTests(unittest.TestCase):
    def test_the_feed_is_on_by_default(self):
        self.assertTrue(config.WS_ENABLED)

    def test_set_symbols_is_a_noop_for_an_unchanged_set(self):
        feed = KlineFeed(lambda symbol, candle: None)
        with patch.object(feed, "stop") as stop, patch.object(feed, "running", return_value=True):
            feed._symbols = ("AAAUSDT",)
            feed.set_symbols(["AAAUSDT"])
        stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
