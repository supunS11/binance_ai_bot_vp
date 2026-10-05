"""The replay simulator, which every Phase 1/2 number depends on.

These are the most important tests in the research package. A backtest does not fail
loudly - it returns a number, and an optimistic simulator returns a BETTER number. So
each of the three ways this function could flatter a result is pinned here:

  1. intrabar ambiguity scored as a win rather than a loss
  2. entries assumed to fill when price never traded there
  3. fees omitted, or applied once instead of per round trip

Every assertion is written so that the OPTIMISTIC behaviour fails it.
"""
import unittest
from unittest.mock import patch

import config
from research import replay
from tests import factories as f

START = 1_758_844_800_000


class _Candidate:
    def __init__(self, direction="BUY", entry=100.0, stop=99.0, target=102.0,
                 quantity=1.0):
        self.symbol = "TESTUSDT"
        self.direction = direction
        self.entry_price = entry
        self.stop_price = stop
        self.target_price = target
        self.quantity = quantity
        self.atr = 1.0

    @property
    def risk_distance(self):
        return abs(self.entry_price - self.stop_price)

    @property
    def r_multiple(self):
        return abs(self.target_price - self.entry_price) / self.risk_distance

    def round_trip_cost_r(self):
        from research import metrics
        return metrics.round_trip_cost_r(self.risk_distance, self.entry_price,
                                        self.quantity)


def _candles(specs, start=START):
    """specs: list of (open, high, low, close). One 1m candle each."""
    out = []
    ts = start
    for open_, high, low, close in specs:
        out.append(f.candle(ts, open_, high, low, close, 100.0))
        ts += 60_000
    return out


class AmbiguousBarTests(unittest.TestCase):
    """A bar containing both levels must be scored as a LOSS.

    This is the single most consequential modelling choice in the harness. A bar wide
    enough to hold both the stop and the target cannot say which came first, and such
    bars are volatile bars, which cluster where the trade was going badly. Scoring them
    as wins does not add a small optimism - it adds it exactly where it decides the
    result.
    """

    def test_bar_containing_both_levels_is_a_stop(self):
        candidate = _Candidate(direction="BUY", entry=100.0, stop=99.0, target=102.0)
        # Fill bar, then one huge bar spanning 98.5 to 102.5 - both levels inside.
        forward = _candles([(100.0, 100.1, 99.9, 100.0),
                            (100.0, 102.5, 98.5, 101.0)])
        result = replay.simulate(candidate, forward)

        self.assertEqual(result["outcome"], "STOP",
                         "an ambiguous bar must never be scored as a target")
        self.assertEqual(result["ambiguous_bars"], 1,
                         "the assumption must be counted so its size is visible")
        self.assertLess(result["net_r"], 0.0)

    def test_ambiguity_is_counted_even_when_resolved_later(self):
        candidate = _Candidate(direction="SELL", entry=100.0, stop=101.0, target=98.0)
        forward = _candles([(100.0, 100.1, 99.9, 100.0),
                            (100.0, 101.5, 97.5, 99.0)])
        result = replay.simulate(candidate, forward)
        self.assertEqual(result["outcome"], "STOP")
        self.assertEqual(result["ambiguous_bars"], 1)

    def test_a_clean_target_is_still_a_target(self):
        """The pessimistic rule must not swallow unambiguous wins."""
        candidate = _Candidate(direction="BUY", entry=100.0, stop=99.0, target=102.0)
        forward = _candles([(100.0, 100.1, 99.95, 100.0),
                            (100.0, 102.3, 99.9, 102.1)])
        result = replay.simulate(candidate, forward)
        self.assertEqual(result["outcome"], "TARGET")
        self.assertEqual(result["ambiguous_bars"], 0)
        self.assertAlmostEqual(result["gross_r"], 2.0, places=6)


class FillRequirementTests(unittest.TestCase):
    """An entry fills only if price actually traded there."""

    def test_entry_never_reached_is_expired_not_filled(self):
        candidate = _Candidate(direction="BUY", entry=100.0, stop=99.0, target=102.0)
        # Price gaps up and never comes back to 100.0.
        forward = _candles([(101.0, 101.5, 100.8, 101.4)] * 40)
        result = replay.simulate(candidate, forward)

        self.assertEqual(result["outcome"], "EXPIRED")
        self.assertIsNone(result["net_r"],
                          "an unfilled candidate must contribute no R at all")
        self.assertIsNone(result["bars_to_fill"])

    def test_expired_entries_are_the_ones_that_ran_away(self):
        """The reason this matters: unfilled trades are disproportionately winners.

        A buy that never filled because price ran up is exactly the trade that would
        have won. Assuming it filled at the reference price is how a backtest invents
        an edge, so the outcome must be EXPIRED rather than a free 2R.
        """
        candidate = _Candidate(direction="BUY", entry=100.0, stop=99.0, target=102.0)
        forward = _candles([(100.5, 103.0, 100.4, 102.9)] * 10)
        result = replay.simulate(candidate, forward)
        self.assertEqual(result["outcome"], "EXPIRED")
        self.assertIsNone(result["gross_r"])

    def test_fill_window_is_bounded(self):
        """A resting order does not wait for ever; live expires it too."""
        candidate = _Candidate(direction="BUY", entry=100.0, stop=99.0, target=102.0)
        confirm_ms = 900_000
        window_minutes = config.ENTRY_TIMEOUT_SECONDS // 60
        # Away for the whole fill window, then returns to the entry.
        away = [(101.0, 101.2, 100.9, 101.1)] * (window_minutes + 5)
        forward = _candles(away + [(100.0, 100.1, 99.8, 100.0),
                                   (100.0, 102.5, 99.95, 102.2)])
        result = replay.simulate(candidate, forward)
        self.assertEqual(result["outcome"], "EXPIRED",
                         "a fill after the window must not count")

    def test_sell_fills_only_when_price_trades_up_to_it(self):
        candidate = _Candidate(direction="SELL", entry=100.0, stop=101.0, target=98.0)
        # Price only ever below 100: a resting sell at 100 is never reached.
        forward = _candles([(99.0, 99.5, 98.9, 99.2)] * 40)
        self.assertEqual(replay.simulate(candidate, forward)["outcome"], "EXPIRED")

        reached = _candles([(99.9, 100.2, 99.8, 100.0),
                            (100.0, 100.1, 97.8, 97.9)])
        self.assertEqual(replay.simulate(candidate, reached)["outcome"], "TARGET")


class CostAndRTests(unittest.TestCase):
    """R must be net of a full round trip, and measured against the original stop."""

    def test_net_r_is_gross_minus_round_trip_cost(self):
        candidate = _Candidate(direction="BUY", entry=100.0, stop=99.0, target=102.0)
        forward = _candles([(100.0, 100.1, 99.95, 100.0),
                            (100.0, 102.4, 99.9, 102.2)])
        result = replay.simulate(candidate, forward)

        expected_cost = candidate.round_trip_cost_r()
        self.assertGreater(expected_cost, 0.0, "cost must not be zero")
        self.assertAlmostEqual(result["net_r"],
                               result["gross_r"] - expected_cost, places=5)
        self.assertLess(result["net_r"], result["gross_r"],
                        "net must be strictly worse than gross")

    def test_a_stop_out_is_about_minus_one_r_plus_cost(self):
        candidate = _Candidate(direction="BUY", entry=100.0, stop=99.0, target=102.0)
        forward = _candles([(100.0, 100.05, 99.9, 100.0),
                            (100.0, 100.1, 98.9, 99.0)])
        result = replay.simulate(candidate, forward)
        self.assertEqual(result["outcome"], "STOP")
        self.assertAlmostEqual(result["gross_r"], -1.0, places=6)
        self.assertLess(result["net_r"], -1.0,
                        "a stop-out must cost more than 1R once fees are paid")

    def test_mfe_and_mae_are_recorded_in_r(self):
        candidate = _Candidate(direction="BUY", entry=100.0, stop=99.0, target=102.0)
        forward = _candles([(100.0, 100.05, 99.9, 100.0),
                            (100.0, 101.5, 99.5, 101.0),
                            (100.0, 102.2, 99.9, 102.1)])
        result = replay.simulate(candidate, forward)
        self.assertGreaterEqual(result["mfe_r"], 2.0)
        self.assertGreater(result["mae_r"], 0.0)


class UnresolvedTests(unittest.TestCase):
    """A trade still open at the horizon contributes no R."""

    def test_unresolved_trade_has_no_r(self):
        candidate = _Candidate(direction="BUY", entry=100.0, stop=99.0, target=102.0)
        # Fills, then drifts inside the range for longer than MAX_HOLD_BARS.
        minutes = replay.MAX_HOLD_BARS * 15 + 60
        forward = _candles([(100.0, 100.05, 99.9, 100.0)]
                           + [(100.0, 100.4, 99.6, 100.0)] * minutes)
        result = replay.simulate(candidate, forward)

        self.assertEqual(result["outcome"], "UNRESOLVED")
        self.assertIsNone(result["net_r"],
                          "scoring an unresolved trade invents an exit "
                          "the system would not have taken")
        self.assertIsNotNone(result["bars_to_fill"])

    def test_degenerate_geometry_is_refused(self):
        candidate = _Candidate(direction="BUY", entry=100.0, stop=100.0, target=102.0)
        result = replay.simulate(candidate, _candles([(100.0, 101.0, 99.0, 100.0)]))
        self.assertEqual(result["outcome"], "EXPIRED")
        self.assertIsNone(result["net_r"])

    def test_no_forward_data_is_refused(self):
        candidate = _Candidate()
        result = replay.simulate(candidate, [])
        self.assertEqual(result["outcome"], "EXPIRED")


class ControlGeometryTests(unittest.TestCase):
    """A twin must hold geometry constant and change only location."""

    def test_twin_preserves_risk_and_reward_multiple(self):
        from research.control import _twin_levels
        for direction in ("BUY", "SELL"):
            stop, target = _twin_levels(200.0, direction, risk_distance=2.0,
                                        r_multiple=2.5)
            self.assertAlmostEqual(abs(200.0 - stop), 2.0, places=9)
            self.assertAlmostEqual(abs(target - 200.0) / 2.0, 2.5, places=9)
            if direction == "BUY":
                self.assertLess(stop, 200.0)
                self.assertGreater(target, 200.0)
            else:
                self.assertGreater(stop, 200.0)
                self.assertLess(target, 200.0)

    def test_twin_is_scored_by_the_same_simulator(self):
        """Any divergence in scoring would surface as lift that is really an artifact."""
        from research.control import _Twin
        twin = _Twin("TESTUSDT", "BUY", 100.0, 99.0, 102.0, 1.0, 1.0)
        forward = _candles([(100.0, 100.05, 99.9, 100.0),
                            (100.0, 102.3, 99.9, 102.1)])
        result = replay.simulate(twin, forward)
        self.assertEqual(result["outcome"], "TARGET")
        self.assertAlmostEqual(result["gross_r"], 2.0, places=6)


class _RowCandidate(_Candidate):
    """Adds the fields _row() reads beyond what AmbiguousBarTests/simulate() need."""

    def __init__(self, attributes=None, profile_snapshot=None, **kwargs):
        super().__init__(**kwargs)
        self.setup = "S2-VAR"
        self.session_id = "2026-09-01"
        self.as_of = START
        self.attributes = attributes or {}
        self.profile_snapshot = profile_snapshot or {}


class RowAttributeThreadingTests(unittest.TestCase):
    """The Phase 3 attribution fields must survive candidate -> CSV row -> reread.

    Each field is computed once at detection time (setups/poc_rotation.py,
    setups/va_reversion.py) and would otherwise be silently dropped by `_row()`'s
    explicit key list - the exact drift `TRADE_FIELD_TYPES` exists to catch for
    numeric fields, but a field missing from `_row()` entirely produces no error at
    all, just an always-blank column. See test_merge.py's coverage test for the
    type-declaration half of this guarantee.
    """

    def test_value_migration_label_is_read_from_the_profile_snapshot(self):
        candidate = _RowCandidate(profile_snapshot={"value_migration": "HIGHER"})
        row = replay._row(candidate, ctx=None, entry=100.0, outcome={}, qv_rank=1)
        self.assertEqual(row["value_migration"], "HIGHER")

    def test_bin_delta_normalized_is_read_from_attributes(self):
        candidate = _RowCandidate(attributes={"bin_delta_normalized": 0.6})
        row = replay._row(candidate, ctx=None, entry=100.0, outcome={}, qv_rank=1)
        self.assertAlmostEqual(row["bin_delta_normalized"], 0.6, places=9)

    def test_bin_delta_normalized_is_none_when_the_bin_had_no_volume(self):
        candidate = _RowCandidate(attributes={"bin_delta_normalized": None})
        row = replay._row(candidate, ctx=None, entry=100.0, outcome={}, qv_rank=1)
        self.assertIsNone(row["bin_delta_normalized"])

    def test_poor_and_excess_at_extreme_survive_as_ints_not_the_word_false(self):
        """The regression this guards against: csv.DictWriter stringifies a raw bool
        as "True"/"False", and coerce_row's float(value) then raises on it."""
        candidate = _RowCandidate(attributes={"poor_at_extreme": True,
                                              "excess_at_extreme": False})
        row = replay._row(candidate, ctx=None, entry=100.0, outcome={}, qv_rank=1)
        self.assertEqual(row["poor_at_extreme"], 1)
        self.assertEqual(row["excess_at_extreme"], 0)
        self.assertNotIsInstance(row["poor_at_extreme"], bool)
        restored = replay.coerce_row(dict(row))
        self.assertEqual(restored["poor_at_extreme"], 1)
        self.assertEqual(restored["excess_at_extreme"], 0)

    def test_poor_and_excess_at_extreme_stay_none_when_unmeasured(self):
        candidate = _RowCandidate(attributes={})
        row = replay._row(candidate, ctx=None, entry=100.0, outcome={}, qv_rank=1)
        self.assertIsNone(row["poor_at_extreme"])
        self.assertIsNone(row["excess_at_extreme"])

    def test_stop_inside_hvn_and_target_behind_hvn_survive_as_ints(self):
        candidate = _RowCandidate(attributes={"stop_inside_hvn": True,
                                              "target_behind_hvn": False})
        row = replay._row(candidate, ctx=None, entry=100.0, outcome={}, qv_rank=1)
        self.assertEqual(row["stop_inside_hvn"], 1)
        self.assertEqual(row["target_behind_hvn"], 0)
        restored = replay.coerce_row(dict(row))
        self.assertEqual(restored["stop_inside_hvn"], 1)
        self.assertEqual(restored["target_behind_hvn"], 0)


class ForceEnableSetupsTests(unittest.TestCase):
    """research.replay.run() must not go silently blind when a setup ships disabled.

    CALIBRATION.md finding 15: Phase 1 + the matched-random control shipped every
    S*_ENABLED default as False - a live-trading safety decision, not a statement that
    the setup's logic should be unmeasurable. Without an override, a re-run testing a fix
    to a disabled setup would call detect(), get SETUP_DISABLED unconditionally, and
    produce zero candidates while looking like an ordinary null result rather than a
    misconfigured run.

    `run()` calls dataset.load_specs(root) immediately and raises SystemExit on an empty
    corpus, which is deliberately used here as a cheap way to observe the flag state
    right after it is set, without needing an on-disk corpus fixture.
    """

    FLAGS = ("S1_POC_ENABLED", "S1_LVN_ENABLED", "S2_ENABLED", "S3_ENABLED")

    def setUp(self):
        # Start every flag False, the current shipped default, so a test that passes
        # only because the flags happened to already be True cannot hide a real bug.
        self._patchers = [patch.object(config, flag, False) for flag in self.FLAGS]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run_against_empty_corpus(self, tmp_path, **kwargs):
        with self.assertRaises(SystemExit):
            replay.run(root=str(tmp_path), **kwargs)

    def test_default_forces_every_setup_enabled(self):
        self._run_against_empty_corpus(self.id())
        for flag in self.FLAGS:
            self.assertTrue(getattr(config, flag),
                            f"{flag} should have been forced True by default")

    def test_respect_live_flags_leaves_them_untouched(self):
        self._run_against_empty_corpus(self.id(), force_enable_setups=False)
        for flag in self.FLAGS:
            self.assertFalse(getattr(config, flag),
                             f"{flag} should NOT have been touched")

    def test_cli_default_forces_enable(self):
        with patch("research.replay.run", side_effect=SystemExit) as mock_run:
            with self.assertRaises(SystemExit):
                replay.main(["--symbols", "1"])
        self.assertTrue(mock_run.call_args.kwargs["force_enable_setups"])

    def test_cli_respect_live_flags_disables_the_override(self):
        with patch("research.replay.run", side_effect=SystemExit) as mock_run:
            with self.assertRaises(SystemExit):
                replay.main(["--symbols", "1", "--respect-live-flags"])
        self.assertFalse(mock_run.call_args.kwargs["force_enable_setups"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
