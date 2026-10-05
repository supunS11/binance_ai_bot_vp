import csv
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import sessions
from research import dataset


class SessionIdRangeEndAsOfTests(unittest.TestCase):
    """`end_as_of` is what makes a second, independent historical window fetchable
    (CALIBRATION.md finding 22) - previously the range could only end "yesterday"."""

    def test_default_behaviour_is_unchanged_when_end_as_of_is_omitted(self):
        now = sessions.now_ms()
        with_default = dataset.session_id_range(5)
        explicit_now = dataset.session_id_range(5, end_as_of=now)
        self.assertEqual(with_default, explicit_now)

    def test_end_as_of_shifts_the_window_into_the_past(self):
        end = sessions.to_ms(datetime.strptime("2026-05-30", "%Y-%m-%d"))
        ids = dataset.session_id_range(120, end_as_of=end)
        self.assertEqual(len(ids), 120)
        self.assertEqual(ids[0], "2026-01-30")
        self.assertEqual(ids[-1], "2026-05-29")

    def test_the_shifted_window_never_touches_the_end_date_itself(self):
        end = sessions.to_ms(datetime.strptime("2026-05-30", "%Y-%m-%d"))
        ids = dataset.session_id_range(120, end_as_of=end)
        self.assertNotIn("2026-05-30", ids)

    def test_ids_are_returned_oldest_first(self):
        end = sessions.to_ms(datetime.strptime("2026-05-30", "%Y-%m-%d"))
        ids = dataset.session_id_range(10, end_as_of=end)
        self.assertEqual(ids, sorted(ids))


class FetchDailyEndAsOfTests(unittest.TestCase):
    """`fetch_daily` used to ignore `end_as_of` entirely and always fetch relative
    to real `now()` - so a historical window's 1m session candles landed correctly
    in the past while its daily candles (used for the scanner's history-length
    check) landed in the present. Undetected until `data_cache_window3`
    (2025-10-02..2026-01-29) produced zero trades: every session failed
    INSUFFICIENT_DAILY_HISTORY because the daily series covered 2026-04-22..
    2026-09-28 instead. `data_cache_window2` had the identical bug, just partially
    masked by lucky timing."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.captured = {}

        def fake_fetch(rest, symbol, interval, start_ms, end_ms, drop_unclosed=True):
            self.captured["start_ms"] = start_ms
            self.captured["end_ms"] = end_ms
            return []
        self.fetch_patcher = patch("research.dataset.klines_mod.fetch", fake_fetch)
        self.fetch_patcher.start()
        self.addCleanup(self.fetch_patcher.stop)

    def test_end_as_of_anchors_the_daily_fetch_in_the_requested_window(self):
        end = sessions.to_ms(datetime.strptime("2026-05-30", "%Y-%m-%d"))
        dataset.fetch_daily(rest=None, root=self.root, symbol="BTCUSDT", days=120,
                            end_as_of=end)
        # session_id_range(120, end_as_of=end) == 2026-01-30..2026-05-29, so the
        # daily fetch's END must land there too, not at real "now".
        self.assertEqual(self.captured["end_ms"], end)

    def test_default_behaviour_is_unchanged_when_end_as_of_is_omitted(self):
        before = sessions.now_ms()
        dataset.fetch_daily(rest=None, root=self.root, symbol="BTCUSDT", days=120)
        after = sessions.now_ms()
        self.assertGreaterEqual(self.captured["end_ms"], before)
        self.assertLessEqual(self.captured["end_ms"], after)

    def test_start_still_leaves_the_documented_40_day_history_buffer(self):
        end = sessions.to_ms(datetime.strptime("2026-05-30", "%Y-%m-%d"))
        dataset.fetch_daily(rest=None, root=self.root, symbol="BTCUSDT", days=120,
                            end_as_of=end)
        expected_start = sessions.session_start_ms(end) - 160 * sessions.MS_DAY
        self.assertEqual(self.captured["start_ms"], expected_start)

    def test_build_threads_end_as_of_through_to_fetch_daily(self):
        with patch("research.dataset.fetch_daily") as mock_fetch_daily, \
             patch("research.dataset.fetch_symbol", return_value=(0, 0, 0)), \
             patch("research.dataset.symbols_mod.SymbolCatalog") as mock_catalog_cls:
            mock_catalog_cls.return_value.get.return_value = None
            end = sessions.to_ms(datetime.strptime("2026-05-30", "%Y-%m-%d"))
            dataset.build(root=self.root, symbols_wanted=["BTCUSDT"], days=5,
                          rest=object(), end_as_of=end)
            self.assertEqual(mock_fetch_daily.call_args.kwargs.get("end_as_of"), end)
            self.assertEqual(mock_fetch_daily.call_args.args[1:], (self.root, "BTCUSDT", 5))


class _FakeFundingRest:
    """Queues canned pages, records every call's params - a stand-in for RestClient
    that never touches the network."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def funding_rate_history(self, symbol, start_ms=None, end_ms=None, limit=1000):
        self.calls.append({"symbol": symbol, "start_ms": start_ms,
                           "end_ms": end_ms, "limit": limit})
        if not self.pages:
            return []
        return self.pages.pop(0)


def _record(funding_time, rate="0.0001", mark="100.0"):
    return {"fundingTime": funding_time, "fundingRate": rate, "markPrice": mark}


class FetchFundingTests(unittest.TestCase):
    """`fetch_funding`: CALIBRATION.md's follow-up on the user's OI research - open
    interest is capped at ~30 days of history on Binance's free API, but funding rate
    is not (verified empirically back to contract inception), so it is the one of the
    two that can actually be backtested against this project's existing corpus."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _read(self, symbol):
        path = os.path.join(self.root, "funding", symbol, "funding.csv")
        with open(path, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_end_as_of_anchors_the_window_like_fetch_daily(self):
        end = sessions.to_ms(datetime.strptime("2026-05-30", "%Y-%m-%d"))
        rest = _FakeFundingRest([[_record(1234)]])
        dataset.fetch_funding(rest, self.root, "BTCUSDT", days=120, end_as_of=end)
        self.assertEqual(rest.calls[0]["end_ms"], end)
        expected_start = sessions.session_start_ms(end) - 160 * sessions.MS_DAY
        self.assertEqual(rest.calls[0]["start_ms"], expected_start)

    def test_default_behaviour_uses_now_when_end_as_of_is_omitted(self):
        before = sessions.now_ms()
        rest = _FakeFundingRest([[_record(1234)]])
        dataset.fetch_funding(rest, self.root, "BTCUSDT", days=120)
        after = sessions.now_ms()
        self.assertGreaterEqual(rest.calls[0]["end_ms"], before)
        self.assertLessEqual(rest.calls[0]["end_ms"], after)

    def test_a_single_partial_page_stops_pagination(self):
        rest = _FakeFundingRest([[_record(100), _record(200), _record(300)]])
        count = dataset.fetch_funding(rest, self.root, "BTCUSDT", days=5)
        self.assertEqual(count, 3)
        self.assertEqual(len(rest.calls), 1,
                         "a page shorter than the limit means there's nothing "
                         "more to fetch - a second call would be wasted")

    def test_a_full_page_triggers_a_second_page_starting_after_the_last_record(self):
        end = sessions.to_ms(datetime.strptime("2026-05-30", "%Y-%m-%d"))
        start = sessions.session_start_ms(end) - 45 * sessions.MS_DAY
        # Fake fundingTimes anchored to the real `start` the fetch will compute -
        # they must be >= cursor for the "did this page move us forward" check to
        # behave as it would against a real, epoch-timestamped response.
        first_page = [_record(start + t) for t in range(0, 1000)]
        second_page = [_record(start + 1500)]
        rest = _FakeFundingRest([first_page, second_page])
        count = dataset.fetch_funding(rest, self.root, "BTCUSDT", days=5,
                                      end_as_of=end)
        self.assertEqual(count, 1001)
        self.assertEqual(len(rest.calls), 2)
        self.assertEqual(rest.calls[1]["start_ms"], start + 999 + 1,
                         "the next page must start right after the previous "
                         "page's LAST record, not restart from the beginning")

    def test_no_records_writes_nothing_and_returns_zero(self):
        rest = _FakeFundingRest([[]])
        count = dataset.fetch_funding(rest, self.root, "BTCUSDT", days=5)
        self.assertEqual(count, 0)
        self.assertFalse(
            os.path.exists(os.path.join(self.root, "funding", "BTCUSDT", "funding.csv")))

    def test_records_are_persisted_and_readable_back(self):
        rest = _FakeFundingRest([[_record(100, rate="0.00012", mark="83000.5")]])
        dataset.fetch_funding(rest, self.root, "BTCUSDT", days=5)
        rows = self._read("BTCUSDT")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["funding_time"], "100")
        self.assertEqual(rows[0]["funding_rate"], "0.00012")
        self.assertEqual(rows[0]["mark_price"], "83000.5")

    def test_a_failed_request_is_caught_and_returns_zero(self):
        class BrokenRest:
            def funding_rate_history(self, *a, **k):
                raise RuntimeError("network")
        count = dataset.fetch_funding(BrokenRest(), self.root, "BTCUSDT", days=5)
        self.assertEqual(count, 0)


class BuildWithFundingTests(unittest.TestCase):
    """`--with-funding` is opt-in and defaults False, so it must be provably inert
    unless explicitly requested - every OTHER build() test in this file (and every
    test elsewhere that calls build()) implicitly asserts the False-by-default case
    by never setting it and still passing."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _build(self, **kwargs):
        with patch("research.dataset.fetch_daily"), \
             patch("research.dataset.fetch_symbol", return_value=(0, 0, 0)), \
             patch("research.dataset.symbols_mod.SymbolCatalog") as mock_catalog_cls, \
             patch("research.dataset.fetch_funding") as mock_fetch_funding:
            mock_catalog_cls.return_value.get.return_value = None
            dataset.build(root=self.root, symbols_wanted=["BTCUSDT"], days=5,
                          rest=object(), **kwargs)
            return mock_fetch_funding

    def test_funding_is_not_fetched_by_default(self):
        mock_fetch_funding = self._build()
        mock_fetch_funding.assert_not_called()

    def test_with_funding_true_fetches_it_per_symbol(self):
        mock_fetch_funding = self._build(with_funding=True)
        mock_fetch_funding.assert_called_once()

    def test_with_funding_threads_end_as_of_through_too(self):
        end = sessions.to_ms(datetime.strptime("2026-05-30", "%Y-%m-%d"))
        mock_fetch_funding = self._build(with_funding=True, end_as_of=end)
        self.assertEqual(mock_fetch_funding.call_args.kwargs.get("end_as_of"), end)


if __name__ == "__main__":
    unittest.main()
