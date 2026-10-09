import asyncio
import threading
import time
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


class ShutdownRaceTests(unittest.TestCase):
    """2026-10-09: a kline-feed thread died with "Cannot close a running event
    loop". Root cause: _run()'s finally block read self._loop rather than the
    loop IT created, so a thread whose shutdown overran set_symbols()'s restart
    could end up closing a NEWER thread's (running) loop instead of its own."""

    def test_run_closes_its_own_loop_even_if_self_loop_is_reassigned_mid_shutdown(self):
        feed = KlineFeed(lambda symbol, candle: None)
        release = threading.Event()

        async def _blocking_main(symbols):
            # Stand-in for a _main() still finishing shutdown - held open until
            # the test reassigns self._loop out from under it, exactly as a
            # second set_symbols() starting a new thread would.
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, release.wait)

        with patch.object(feed, "_main", side_effect=_blocking_main):
            thread = threading.Thread(target=feed._run)
            thread.start()
            for _ in range(200):
                if feed._loop is not None:
                    break
                time.sleep(0.01)
            else:
                self.fail("feed._run() never assigned a loop")

            original_loop = feed._loop
            impostor_loop = asyncio.new_event_loop()
            impostor_thread = threading.Thread(target=impostor_loop.run_forever)
            impostor_thread.start()
            feed._loop = impostor_loop   # the real race: a newer thread's loop

            try:
                release.set()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

                self.assertTrue(
                    original_loop.is_closed(),
                    "_run must close the loop IT created, not whatever self._loop "
                    "happens to point to by the time it finishes")
                self.assertFalse(
                    impostor_loop.is_closed(),
                    "a newer thread's loop must survive an older thread's "
                    "delayed shutdown, not get force-closed out from under it")
            finally:
                impostor_loop.call_soon_threadsafe(impostor_loop.stop)
                impostor_thread.join(timeout=5)
                impostor_loop.close()


class BoundedShutdownTests(unittest.TestCase):
    """A task that does not respond to cancellation (a stuck socket close, a
    swallowed CancelledError) must not be able to keep _main() - and so the
    whole feed thread - alive forever."""

    def test_main_gives_up_on_an_uncooperative_task_within_the_timeout(self):
        feed = KlineFeed(lambda symbol, candle: None)

        async def _stubborn(*args, **kwargs):
            # Ignores exactly ONE cancellation - enough to blow through _main's
            # own (short, patched) shutdown timeout - then actually stops on a
            # second, so asyncio.run()'s own end-of-test cleanup (which cancels
            # whatever is still pending) does not itself hang forever.
            ignored_once = False
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    if ignored_once:
                        raise
                    ignored_once = True

        async def scenario():
            with patch.object(feed, "_socket", side_effect=_stubborn), \
                 patch.object(feed, "_watchdog", side_effect=_stubborn), \
                 patch.object(config, "WS_SHUTDOWN_TIMEOUT_SECONDS", 0.2):
                main_task = asyncio.ensure_future(feed._main(["BTCUSDT"]))
                for _ in range(200):
                    if feed._stop_event is not None:
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail("_main never created its stop event")
                feed._stop_event.set()
                started = time.monotonic()
                await asyncio.wait_for(main_task, timeout=2.0)
                return time.monotonic() - started

        elapsed = asyncio.run(scenario())
        self.assertLess(elapsed, 1.0,
                       "_main must give up on an uncooperative task within "
                       "WS_SHUTDOWN_TIMEOUT_SECONDS, not hang until the "
                       "surrounding test's own timeout")


if __name__ == "__main__":
    unittest.main()
