"""The naked-POC registry, which supplies every structural target.

Two failure modes matter here and neither announces itself:

  1. A POC reported as naked after price traded through it. The setup then targets a
     level with no unfinished business behind it - a plausible-looking target with no
     thesis. Nothing errors; the trade simply has a worse target than the journal says.

  2. Lookahead. The registry's whole job is to look BACKWARD over sessions, and the
     nakedness test legitimately reads sessions AFTER the POC formed. That makes it the
     one place in the profile engine where reading later data is correct, which makes it
     the one place a genuine leak would look normal. Three separate bounds are asserted
     below.

The `>` versus `>=` distinction in _traded_through gets its own test: a POC's own
session contains its own POC by definition, so a single character there would make every
POC non-naked forever and the feature would silently return an empty list.
"""
import unittest

from profile import composite


class _Daily:
    """Minimal daily candle: the registry reads open_time, high and low only."""

    def __init__(self, open_time, high, low):
        self.open_time = open_time
        self.high = high
        self.low = low


class _Profile:
    def __init__(self, high, low):
        self.high = high
        self.low = low


class _Levels:
    def __init__(self, poc_price):
        self.poc_price = poc_price


MS_DAY = 86_400_000
# 2026-09-01 00:00:00 UTC. Asserted below rather than trusted: session ids are compared
# as strings, so an epoch off by a year makes every candidate fall outside the boundary
# and every test return an empty list - which looks exactly like the feature not working.
DAY0 = 1_788_220_800_000


def _session_id(index):
    import sessions
    return sessions.session_id(DAY0 + index * MS_DAY)


class EpochFixtureTests(unittest.TestCase):
    """The fixture's own constant, pinned so a silent year offset cannot recur."""

    def test_day0_is_the_session_the_fixtures_name(self):
        self.assertEqual(_session_id(0), "2026-09-01")
        self.assertEqual(_session_id(1), "2026-09-02")
        self.assertEqual(_session_id(3), "2026-09-04")


class RecordingTests(unittest.TestCase):

    def setUp(self):
        self.registry = composite.NakedPocRegistry()

    def test_records_and_counts(self):
        self.assertTrue(self.registry.record("BTCUSDT", "2026-09-01", 100.0, 105.0, 95.0))
        self.assertEqual(self.registry.sessions_known("BTCUSDT"), 1)

    def test_symbol_is_case_insensitive(self):
        self.registry.record("btcusdt", "2026-09-01", 100.0, 105.0, 95.0)
        self.assertEqual(self.registry.sessions_known("BTCUSDT"), 1)

    def test_re_recording_one_session_is_idempotent(self):
        for _ in range(4):
            self.registry.record("BTCUSDT", "2026-09-01", 100.0, 105.0, 95.0)
        self.assertEqual(self.registry.sessions_known("BTCUSDT"), 1)

    def test_a_non_positive_poc_is_refused(self):
        # Storing it would put a zero price into a target list.
        self.assertFalse(self.registry.record("BTCUSDT", "2026-09-01", 0.0, 105.0, 95.0))
        self.assertFalse(self.registry.record("BTCUSDT", "2026-09-01", None, 105.0, 95.0))
        self.assertEqual(self.registry.sessions_known("BTCUSDT"), 0)

    def test_an_inverted_range_is_refused(self):
        self.assertFalse(self.registry.record("BTCUSDT", "2026-09-01", 100.0, 95.0, 105.0))
        self.assertEqual(self.registry.sessions_known("BTCUSDT"), 0)

    def test_record_bundle_reads_the_true_poc_not_a_price_average(self):
        """The whole point of the module: levels.poc_price, not (H+L+C)/3.

        The profile's range is deliberately lopsided against its POC, which is what a
        trend session looks like. A typical-price approximation would have landed near
        100.0; the POC is at 91.0.
        """
        profile = _Profile(high=130.0, low=90.0)
        levels = _Levels(poc_price=91.0)
        self.assertTrue(
            self.registry.record_bundle("BTCUSDT", "2026-09-01", profile, levels))
        naked = self.registry.naked("BTCUSDT", "2026-09-02")
        self.assertEqual(len(naked), 1)
        self.assertAlmostEqual(naked[0].price, 91.0)

    def test_record_bundle_tolerates_missing_levels(self):
        self.assertFalse(
            self.registry.record_bundle("BTCUSDT", "2026-09-01", _Profile(1.0, 1.0), None))
        self.assertFalse(
            self.registry.record_bundle("BTCUSDT", "2026-09-01", None, _Levels(100.0)))


class NakednessTests(unittest.TestCase):
    """Whether a later session's range covered the POC."""

    def setUp(self):
        self.registry = composite.NakedPocRegistry()

    def test_a_poc_never_revisited_is_naked(self):
        self.registry.record("BTCUSDT", "2026-09-01", 100.0, 101.0, 99.0)
        daily = [_Daily(DAY0, 101.0, 99.0),
                 _Daily(DAY0 + MS_DAY, 110.0, 105.0),      # gapped above, never back
                 _Daily(DAY0 + 2 * MS_DAY, 115.0, 108.0)]
        naked = self.registry.naked("BTCUSDT", _session_id(3), daily_candles=daily)
        self.assertEqual([round(item.price, 4) for item in naked], [100.0])

    def test_a_poc_traded_through_later_is_not_naked(self):
        self.registry.record("BTCUSDT", "2026-09-01", 100.0, 101.0, 99.0)
        daily = [_Daily(DAY0, 101.0, 99.0),
                 _Daily(DAY0 + MS_DAY, 110.0, 98.0)]       # range spans 100.0
        naked = self.registry.naked("BTCUSDT", _session_id(2), daily_candles=daily)
        self.assertEqual(naked, [])

    def test_touching_the_exact_poc_price_counts_as_traded_through(self):
        self.registry.record("BTCUSDT", "2026-09-01", 100.0, 101.0, 99.0)
        daily = [_Daily(DAY0, 101.0, 99.0),
                 _Daily(DAY0 + MS_DAY, 100.0, 95.0)]       # high lands exactly on it
        self.assertEqual(
            self.registry.naked("BTCUSDT", _session_id(2), daily_candles=daily), [])

    def test_a_pocs_own_session_does_not_cancel_it(self):
        """The `>` versus `>=` guard.

        A session's own range necessarily contains its own POC. If the traded-through
        scan included the forming session, every POC would be cancelled the instant it
        was recorded and the registry would always return an empty list - a total
        feature failure that raises nothing.
        """
        self.registry.record("BTCUSDT", "2026-09-01", 100.0, 105.0, 95.0)
        daily = [_Daily(DAY0, 105.0, 95.0)]                # the POC's OWN session only
        naked = self.registry.naked("BTCUSDT", _session_id(1), daily_candles=daily)
        self.assertEqual(len(naked), 1)

    def test_newest_first_and_sessions_ago_counts_up(self):
        self.registry.record("BTCUSDT", "2026-09-01", 50.0, 51.0, 49.0)
        self.registry.record("BTCUSDT", "2026-09-02", 60.0, 61.0, 59.0)
        self.registry.record("BTCUSDT", "2026-09-03", 70.0, 71.0, 69.0)
        naked = self.registry.naked("BTCUSDT", _session_id(3), daily_candles=[])
        self.assertEqual([item.session_id for item in naked],
                         ["2026-09-03", "2026-09-02", "2026-09-01"])
        self.assertEqual([item.sessions_ago for item in naked], [1, 2, 3])

    def test_limit_keeps_the_most_recent(self):
        for index in range(1, 8):
            self.registry.record("BTCUSDT", f"2026-09-0{index}",
                                 float(index * 10), float(index * 10 + 1),
                                 float(index * 10 - 1))
        naked = self.registry.naked("BTCUSDT", "2026-09-09", daily_candles=[], limit=3)
        self.assertEqual([item.session_id for item in naked],
                         ["2026-09-07", "2026-09-06", "2026-09-05"])

    def test_lookback_bounds_how_far_back_it_looks(self):
        registry = composite.NakedPocRegistry(lookback_sessions=2)
        # Each session's range hugs its OWN poc, so no session trades through another's
        # and the only thing that can shorten the list is the lookback bound. A shared
        # wide range here would let every session cancel every other and the test would
        # pass for the wrong reason.
        for index in range(1, 6):
            poc = float(index * 10)
            registry.record("BTCUSDT", f"2026-09-0{index}", poc, poc + 1.0, poc - 1.0)
        naked = registry.naked("BTCUSDT", "2026-09-09", daily_candles=[], limit=10)
        self.assertEqual([item.session_id for item in naked],
                         ["2026-09-05", "2026-09-04"])

    def test_falls_back_to_recorded_ranges_when_no_daily_series(self):
        """Replay has no daily series loaded for every symbol; the test still has to work."""
        self.registry.record("BTCUSDT", "2026-09-01", 100.0, 101.0, 99.0)
        self.registry.record("BTCUSDT", "2026-09-02", 200.0, 205.0, 98.0)  # spans 100
        naked = self.registry.naked("BTCUSDT", _session_id(3), daily_candles=None)
        self.assertEqual([item.session_id for item in naked], ["2026-09-02"])

    def test_unknown_symbol_is_empty_not_an_error(self):
        self.assertEqual(self.registry.naked("NOPEUSDT", "2026-09-02"), [])

    def test_prices_returns_bare_floats(self):
        self.registry.record("BTCUSDT", "2026-09-01", 100.0, 101.0, 99.0)
        prices = self.registry.prices("BTCUSDT", _session_id(2), daily_candles=[])
        self.assertEqual(prices, [100.0])
        self.assertIsInstance(prices[0], float)


class LookaheadTests(unittest.TestCase):
    """The registry reads later sessions on purpose, so its bounds need pinning."""

    def setUp(self):
        self.registry = composite.NakedPocRegistry()

    def test_the_current_sessions_own_poc_is_never_returned(self):
        """Leak 1: today's POC is not a prior POC.

        A developing session's POC is not a level price left behind - it is where price
        is right now. Returning it as a target would be targeting the present.
        """
        self.registry.record("BTCUSDT", "2026-09-05", 500.0, 501.0, 499.0)
        self.registry.record("BTCUSDT", "2026-09-04", 400.0, 401.0, 399.0)
        naked = self.registry.naked("BTCUSDT", "2026-09-05", daily_candles=[])
        self.assertEqual([item.session_id for item in naked], ["2026-09-04"])

    def test_sessions_at_or_after_the_boundary_never_cancel_a_poc(self):
        """Leak 2: a session at or after the boundary is future data.

        The POC here survives only because the session that would have cancelled it is
        the current one. Counting it would mean deciding today's naked list using
        today's eventual range.
        """
        self.registry.record("BTCUSDT", "2026-09-01", 100.0, 101.0, 99.0)
        daily = [_Daily(DAY0, 101.0, 99.0),
                 _Daily(DAY0 + MS_DAY, 150.0, 50.0)]       # the CURRENT session
        naked = self.registry.naked("BTCUSDT", _session_id(1), daily_candles=daily)
        self.assertEqual(len(naked), 1)

    def test_the_current_range_cancels_when_supplied(self):
        """The legitimate intra-session update.

        Price passing a level TODAY does finish the business there, and the caller can
        say so - but only from candles up to the decision time, which is why this is an
        explicit argument rather than something the registry reads for itself.
        """
        self.registry.record("BTCUSDT", "2026-09-01", 100.0, 101.0, 99.0)
        naked = self.registry.naked("BTCUSDT", _session_id(1), daily_candles=[],
                                    current_high=105.0, current_low=98.0)
        self.assertEqual(naked, [])

    def test_a_partial_current_range_is_ignored_rather_than_half_applied(self):
        # Only one bound supplied is not enough to decide containment, and guessing the
        # other would invent data.
        self.registry.record("BTCUSDT", "2026-09-01", 100.0, 101.0, 99.0)
        self.assertEqual(
            len(self.registry.naked("BTCUSDT", _session_id(1), daily_candles=[],
                                    current_high=105.0)), 1)
        self.assertEqual(
            len(self.registry.naked("BTCUSDT", _session_id(1), daily_candles=[],
                                    current_low=98.0)), 1)


class CoverageTests(unittest.TestCase):
    """An empty list has two opposite causes and they must be distinguishable."""

    def test_coverage_grows_with_recorded_sessions(self):
        registry = composite.NakedPocRegistry(lookback_sessions=10)
        self.assertEqual(registry.coverage("BTCUSDT"), 0.0)
        for index in range(1, 6):
            registry.record("BTCUSDT", f"2026-09-0{index}", 100.0 + index, 200.0, 1.0)
        self.assertAlmostEqual(registry.coverage("BTCUSDT"), 0.5)

    def test_coverage_is_capped_at_one(self):
        registry = composite.NakedPocRegistry(lookback_sessions=2)
        for index in range(1, 6):
            registry.record("BTCUSDT", f"2026-09-0{index}", 100.0 + index, 200.0, 1.0)
        self.assertEqual(registry.coverage("BTCUSDT"), 1.0)

    def test_empty_list_from_a_cold_start_is_distinguishable_from_a_revisited_one(self):
        cold = composite.NakedPocRegistry()
        self.assertEqual(cold.naked("BTCUSDT", "2026-09-05"), [])
        self.assertEqual(cold.coverage("BTCUSDT"), 0.0)

        revisited = composite.NakedPocRegistry()
        revisited.record("BTCUSDT", "2026-09-01", 100.0, 101.0, 99.0)
        daily = [_Daily(DAY0, 101.0, 99.0), _Daily(DAY0 + MS_DAY, 110.0, 90.0)]
        self.assertEqual(
            revisited.naked("BTCUSDT", _session_id(2), daily_candles=daily), [])
        self.assertGreater(revisited.coverage("BTCUSDT"), 0.0)


class JournalLoadTests(unittest.TestCase):

    class _Journal:
        def __init__(self, rows):
            self.rows = rows
            self.calls = 0

        def recent_profile_pocs(self, symbol, limit=30):
            self.calls += 1
            return self.rows

    def test_loads_rows_into_the_registry(self):
        journal = self._Journal([
            {"session_id": "2026-09-02", "poc_price": 200.0, "high": 201.0, "low": 199.0},
            {"session_id": "2026-09-01", "poc_price": 100.0, "high": 101.0, "low": 99.0},
        ])
        registry = composite.NakedPocRegistry()
        self.assertEqual(registry.load_from_journal(journal, "BTCUSDT"), 2)
        self.assertEqual(registry.sessions_known("BTCUSDT"), 2)

    def test_a_failing_journal_leaves_the_registry_empty_rather_than_raising(self):
        """A missing target hint must never stop a scan."""
        class Broken:
            def recent_profile_pocs(self, symbol, limit=30):
                raise RuntimeError("database is locked")

        registry = composite.NakedPocRegistry()
        self.assertEqual(registry.load_from_journal(Broken(), "BTCUSDT"), 0)
        self.assertEqual(registry.sessions_known("BTCUSDT"), 0)

    def test_unusable_rows_are_skipped_not_stored(self):
        journal = self._Journal([
            {"session_id": "2026-09-02", "poc_price": None, "high": 1.0, "low": 0.5},
            {"session_id": "2026-09-01", "poc_price": 100.0, "high": 101.0, "low": 99.0},
        ])
        registry = composite.NakedPocRegistry()
        self.assertEqual(registry.load_from_journal(journal, "BTCUSDT"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
