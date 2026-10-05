import unittest
from datetime import datetime

import sessions
from scanner import Scanner, WeeklyBundle
from tests import factories as f

MONDAY = sessions.to_ms(datetime(2026, 9, 28))     # a Monday 00:00 UTC -
                                                    # week_start_ms of 2026-09-30


class _FakeCache:
    """Just enough of KlineCache's interface for `frozen_weekly_bundle` -
    `minute_candles` is the only method that method actually calls."""

    def __init__(self, candles_by_symbol=None):
        self._candles = candles_by_symbol or {}
        self.calls = []

    def minute_candles(self, symbol, start_ms, end_ms, force=False):
        self.calls.append((symbol, start_ms, end_ms))
        series = self._candles.get(symbol.upper(), [])
        return [c for c in series if start_ms <= c.open_time < end_ms]


def _week_of_candles(start_ts, center=100.0, days=7):
    """One boxy session's worth of candles repeated across `days`, so the
    composite has real, non-degenerate volume at every bin `boxy_session` uses."""
    out = []
    for day in range(days):
        out.extend(f.boxy_session(start_ts + day * sessions.MS_DAY, center=center))
    return out


class FrozenWeeklyBundleTests(unittest.TestCase):
    """PLAN item 21: `Scanner.frozen_weekly_bundle`, new. `ctx.weekly_levels` and
    the calendar-week helpers it's built from (`sessions.previous_week`) existed
    but had no caller before this - this is that caller's own test coverage."""

    def _scanner(self, candles_by_symbol=None):
        cache = _FakeCache(candles_by_symbol)
        return Scanner(rest=None, catalog=None, cache=cache), cache

    def test_returns_none_when_the_week_has_no_candles(self):
        scanner, _ = self._scanner({})
        # A Wednesday inside the week whose PREVIOUS week we're asking about.
        as_of = sessions.to_ms(datetime(2026, 9, 30))
        result = scanner.frozen_weekly_bundle("TESTUSDT", as_of, bin_size=0.05)
        self.assertIsNone(result)

    def test_builds_a_composite_profile_and_levels_from_a_full_prior_week(self):
        prior_week_start = MONDAY - 7 * sessions.MS_DAY
        candles = _week_of_candles(prior_week_start, center=100.0)
        scanner, cache = self._scanner({"TESTUSDT": candles})
        as_of = sessions.to_ms(datetime(2026, 9, 30))     # inside MONDAY's week

        bundle = scanner.frozen_weekly_bundle("TESTUSDT", as_of, bin_size=0.05)
        self.assertIsInstance(bundle, WeeklyBundle)
        self.assertIsNotNone(bundle.levels)
        self.assertGreater(bundle.profile.total_volume, 0)
        # A full week of boxy sessions all centred on 100.0 must produce a POC
        # near 100.0, not near some other price the wrong window would pick up.
        self.assertAlmostEqual(bundle.levels.poc_price, 100.0, delta=1.5)

    def test_requests_exactly_the_previous_calendar_week_not_the_current_one(self):
        candles = _week_of_candles(MONDAY - 7 * sessions.MS_DAY)
        scanner, cache = self._scanner({"TESTUSDT": candles})
        as_of = sessions.to_ms(datetime(2026, 9, 30))

        scanner.frozen_weekly_bundle("TESTUSDT", as_of, bin_size=0.05)
        self.assertEqual(len(cache.calls), 1)
        _, start_ms, end_ms = cache.calls[0]
        self.assertEqual(start_ms, MONDAY - 7 * sessions.MS_DAY)
        self.assertEqual(end_ms, MONDAY)

    def test_is_cached_for_the_whole_week_not_refetched_every_call(self):
        candles = _week_of_candles(MONDAY - 7 * sessions.MS_DAY)
        scanner, cache = self._scanner({"TESTUSDT": candles})

        monday_morning = sessions.to_ms(datetime(2026, 9, 28, 1))
        wednesday = sessions.to_ms(datetime(2026, 9, 30))
        sunday_night = sessions.to_ms(datetime(2026, 10, 4, 23))

        first = scanner.frozen_weekly_bundle("TESTUSDT", monday_morning, bin_size=0.05)
        second = scanner.frozen_weekly_bundle("TESTUSDT", wednesday, bin_size=0.05)
        third = scanner.frozen_weekly_bundle("TESTUSDT", sunday_night, bin_size=0.05)

        self.assertIs(first, second)
        self.assertIs(second, third)
        self.assertEqual(len(cache.calls), 1,
                         "three calls inside the same week must fetch once, not "
                         "three times - that's the whole point of freezing it")

    def test_recomputes_once_the_calendar_week_actually_rolls_over(self):
        two_weeks = _week_of_candles(MONDAY - 14 * sessions.MS_DAY, days=14)
        scanner, cache = self._scanner({"TESTUSDT": two_weeks})

        this_week = sessions.to_ms(datetime(2026, 9, 30))
        next_week = sessions.to_ms(datetime(2026, 10, 6))

        first = scanner.frozen_weekly_bundle("TESTUSDT", this_week, bin_size=0.05)
        second = scanner.frozen_weekly_bundle("TESTUSDT", next_week, bin_size=0.05)

        self.assertIsNot(first, second)
        self.assertEqual(len(cache.calls), 2)

    def test_a_different_symbol_gets_its_own_independent_cache_entry(self):
        candles = _week_of_candles(MONDAY - 7 * sessions.MS_DAY)
        scanner, cache = self._scanner({"AAAUSDT": candles, "BBBUSDT": candles})
        as_of = sessions.to_ms(datetime(2026, 9, 30))

        a = scanner.frozen_weekly_bundle("AAAUSDT", as_of, bin_size=0.05)
        b = scanner.frozen_weekly_bundle("BBBUSDT", as_of, bin_size=0.05)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(len(cache.calls), 2)


if __name__ == "__main__":
    unittest.main()
