import unittest
from types import SimpleNamespace
from unittest.mock import patch

import config
from scanner import Scanner


class _Rest:
    def __init__(self, tickers):
        self.tickers = tickers
        self.calls = 0

    def ticker_24hr(self):
        self.calls += 1
        return self.tickers


class _Catalog:
    def tradable_symbols(self):
        return ["AAAUSDT", "BBBUSDT"]


TICKERS = [{"symbol": "AAAUSDT", "quoteVolume": "10"},
           {"symbol": "BBBUSDT", "quoteVolume": "20"},
           {"symbol": "CCCUSDT", "quoteVolume": "999"}]


class PinnedUniverseTests(unittest.TestCase):
    def test_pinned_symbols_keep_their_order_and_skip_untradable(self):
        scanner = Scanner(_Rest(TICKERS), _Catalog(), cache=None)
        with patch.object(config, "SCAN_SYMBOLS", ["BBBUSDT", "CCCUSDT", "AAAUSDT"]):
            rows = scanner.trade_universe()
        self.assertEqual([r["symbol"] for r in rows], ["BBBUSDT", "AAAUSDT"])
        self.assertEqual([r["qv_rank"] for r in rows], [1, 3])

    def test_pinned_list_bypasses_the_volume_floor(self):
        scanner = Scanner(_Rest(TICKERS), _Catalog(), cache=None)
        with patch.object(config, "SCAN_SYMBOLS", ["AAAUSDT"]), \
             patch.object(config, "TRADE_MIN_24H_QUOTE_VOLUME", 1_000_000.0):
            self.assertEqual([r["symbol"] for r in scanner.trade_universe()], ["AAAUSDT"])


class TickerCacheTests(unittest.TestCase):
    def test_tickers_are_refetched_only_after_the_refresh_interval(self):
        rest = _Rest(TICKERS)
        scanner = Scanner(rest, _Catalog(), cache=None)
        with patch.object(config, "SCAN_SYMBOLS", []), \
             patch.object(config, "WATCHLIST_REFRESH_SECONDS", 300.0), \
             patch("scanner.time.monotonic", side_effect=[0.0, 10.0, 400.0]), \
             patch("exchange.symbols.rank_by_quote_volume", return_value=[]):
            scanner.trade_universe()
            scanner.trade_universe()
            scanner.trade_universe()
        self.assertEqual(rest.calls, 2)


if __name__ == "__main__":
    unittest.main()
