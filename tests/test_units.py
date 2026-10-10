"""Unit tests for the parts where a silent error would be most expensive.

Three areas, chosen because each has a failure mode that no downstream test would
catch:

  ACCEPTANCE   the S2/S3 discriminator. It had a bug that made it return ~1.000 on
               every excursion, so it looked like it was working while discriminating
               nothing. The tests below assert it SEPARATES thin from heavy, not
               merely that it returns a number.
  FILTERS      rounding DIRECTION. Rounding to nearest is accepted by the venue and
               silently wrong: a stop rounded toward entry is tighter than the setup
               asked for, which inflates position size.
  RISK         sizing from the stop. An error here scales every loss.
"""
import sys
import unittest
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import acceptance                                         # noqa: E402
import config                                             # noqa: E402
import risk                                               # noqa: E402
import sessions                                           # noqa: E402
from exchange import filters                              # noqa: E402
from profile import builder as profile_builder            # noqa: E402
from setups import base                                   # noqa: E402
from setups.base import Candidate, RejectReason           # noqa: E402
from tests import factories as f                          # noqa: E402

_MARGIN_PIN = None


def setUpModule():
    # RiskSizingTests exercises stop-based sizing specifically; FixedMarginSizingTests
    # overrides this per-test where it tests the fixed-margin path. Pinned so a live
    # .env enabling it (as for a small live test) cannot flip these assumptions.
    global _MARGIN_PIN
    _MARGIN_PIN = patch.object(config, "FIXED_MARGIN_SIZING_ENABLED", False)
    _MARGIN_PIN.start()


def tearDownModule():
    _MARGIN_PIN.stop()


START = 1_758_844_800_000


class _Levels:
    """Minimal levels stand-in: acceptance only reads vah/val."""
    def __init__(self, val=99.0, vah=101.0, poc_price=100.0):
        self.val = val
        self.vah = vah
        self.poc_price = poc_price
        self.value_width = vah - val


def _series(inside_count, inside_volume, outside_count, outside_volume,
            outside_price, inside_price=100.0, back_inside=0):
    """Candles: a stretch inside value, an excursion outside, optional return."""
    out = []
    ts = START
    for _ in range(inside_count):
        out.append(f.candle(ts, inside_price, inside_price + 0.05,
                            inside_price - 0.05, inside_price, inside_volume))
        ts += 60_000
    for _ in range(outside_count):
        out.append(f.candle(ts, outside_price, outside_price + 0.05,
                            outside_price - 0.05, outside_price, outside_volume))
        ts += 60_000
    for _ in range(back_inside):
        out.append(f.candle(ts, inside_price, inside_price + 0.05,
                            inside_price - 0.05, inside_price, inside_volume))
        ts += 60_000
    return out


class AcceptanceDiscriminatesTests(unittest.TestCase):
    """The VOLUME-RATE arm: does it separate thin from heavy?

    PINNED TO ACCEPT_DISCRIMINATOR="volume_rate" ON PURPOSE. Phase 0 measured this
    quantity as inert (AUC ~0.50 with excursion distance held fixed, against 0.61 for
    duration), so the default discriminator is now "duration" and these fixtures - built
    entirely around volume rates - no longer describe the default path.

    They are kept and pinned rather than deleted, because the volume-rate branch is the
    ABLATION ARM: a completed Phase 1 run exists under it and is the control for the
    change. A branch nobody tests is a branch that quietly rots, and then the control is
    worthless when it is needed.

    The duration arm has its own class below.
    """

    def setUp(self):
        self.levels = _Levels()
        patcher = patch.object(config, "ACCEPT_DISCRIMINATOR", "volume_rate")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_thin_excursion_is_not_accepted(self):
        # Outside volume far below the session's normal rate: an advertised price
        # nobody transacted at.
        candles = _series(40, 100.0, 10, 5.0, 97.5)
        verdict = acceptance.evaluate(candles, self.levels, atr=2.0)
        self.assertEqual(verdict.side, "BELOW")
        self.assertFalse(verdict.accepted)
        self.assertLess(verdict.volume_rate_ratio, 1.0)

    def test_heavy_excursion_is_accepted(self):
        # Outside volume at or above the session's normal rate: real discovery.
        candles = _series(40, 100.0, 10, 300.0, 97.5)
        verdict = acceptance.evaluate(candles, self.levels, atr=2.0)
        self.assertEqual(verdict.side, "BELOW")
        self.assertTrue(verdict.accepted)
        self.assertGreater(verdict.volume_rate_ratio,
                           config.ACCEPT_MIN_VOLUME_RATE_RATIO)

    def test_ratio_actually_varies_with_volume(self):
        """THE regression test for the bug that made this module inert.

        The broken version measured outside volume as a fraction of the excursion
        window's own volume, which is ~1.0 by construction. If that returns, these
        ratios collapse to the same number and the discriminator stops discriminating.
        """
        ratios = []
        for outside_volume in (1.0, 10.0, 100.0, 1000.0):
            candles = _series(40, 100.0, 10, outside_volume, 97.5)
            verdict = acceptance.evaluate(candles, self.levels, atr=2.0)
            ratios.append(verdict.volume_rate_ratio)

        self.assertEqual(ratios, sorted(ratios), "ratio must rise with volume")
        self.assertGreater(max(ratios) - min(ratios), 1.0,
                           f"ratio barely moved across 1000x volume: {ratios}")

    def test_rejected_verdict_on_thin_excursion_that_returned(self):
        candles = _series(40, 100.0, 10, 5.0, 97.5, back_inside=5)
        verdict = acceptance.evaluate(candles, self.levels, atr=2.0)
        self.assertTrue(verdict.returned_inside)
        self.assertTrue(verdict.rejected)

    def test_excursion_distance_is_positive_after_a_return(self):
        """The other bug: measuring the wrong window gave a NEGATIVE distance."""
        candles = _series(40, 100.0, 10, 5.0, 97.5, back_inside=5)
        verdict = acceptance.evaluate(candles, self.levels, atr=2.0)
        self.assertGreater(verdict.excursion_distance, 0.0)
        self.assertGreater(verdict.excursion_distance_atr, 0.0)

    def test_no_excursion_reads_inside(self):
        candles = _series(40, 100.0, 0, 0.0, 97.5)
        verdict = acceptance.evaluate(candles, self.levels, atr=2.0)
        self.assertEqual(verdict.verdict, acceptance.INSIDE)

    # ------------------------------------------------------------------
    # The denominator must be INDEPENDENT of the excursion it measures.
    # Found in Phase 0 calibration on live data; see acceptance._baseline_rate.
    # ------------------------------------------------------------------

    def test_ratio_does_not_drift_to_one_as_the_excursion_lengthens(self):
        """THE regression test for the dilution bug.

        The broken version averaged the denominator over the WHOLE session, excursion
        included. With M excursion candles at rate r_out and N-M inside at r_in,
        session_rate = (M*r_out + (N-M)*r_in)/N, so the ratio is dragged toward 1.0 in
        proportion to M - hardest on the long excursions S3 exists to trade.

        A thin excursion must read thin no matter how long it runs.
        """
        ratios = []
        for outside_count in (5, 20, 60, 200):
            candles = _series(40, 100.0, outside_count, 5.0, 97.5)
            verdict = acceptance.evaluate(candles, self.levels, atr=2.0,
                                          prior_rate=100.0)
            ratios.append(verdict.volume_rate_ratio)

        self.assertLess(max(ratios), 0.5,
                        f"thin excursion drifted toward 1.0 as it lengthened: {ratios}")
        self.assertLess(max(ratios) - min(ratios), 0.05,
                        f"ratio should be length-invariant, got {ratios}")

    def test_session_opening_outside_value_falls_back_to_the_prior_session(self):
        """The degenerate case that was 22% of real events.

        Every candle closed outside value, so the session offers no independent
        baseline at all. The old code made outside_rate == session_rate, giving
        ratio == 1.000 exactly - which cleared the acceptance threshold and armed S3 on
        a measurement containing no information.
        """
        candles = _series(0, 0.0, 12, 5.0, 97.5)
        verdict = acceptance.evaluate(candles, self.levels, atr=2.0,
                                      prior_rate=100.0)
        self.assertEqual(verdict.baseline_source, "PRIOR_SESSION")
        self.assertNotAlmostEqual(verdict.volume_rate_ratio, 1.0, places=3)
        self.assertLess(verdict.volume_rate_ratio, 0.5,
                        "a thin all-outside session must still read as thin")
        self.assertFalse(verdict.accepted)

    def test_all_outside_session_still_discriminates_thin_from_heavy(self):
        """With a prior baseline the degenerate case becomes measurable again."""
        thin = acceptance.evaluate(_series(0, 0.0, 12, 5.0, 97.5),
                                   self.levels, atr=2.0, prior_rate=100.0)
        heavy = acceptance.evaluate(_series(0, 0.0, 12, 400.0, 97.5),
                                    self.levels, atr=2.0, prior_rate=100.0)
        self.assertLess(thin.volume_rate_ratio, heavy.volume_rate_ratio)
        self.assertFalse(thin.accepted)
        self.assertTrue(heavy.accepted)

    def test_no_baseline_at_all_refuses_rather_than_reading_as_thin(self):
        """With no reference the answer must be NO_BASELINE, never a tradeable verdict.

        This is the asymmetry that makes the refusal necessary: with no denominator the
        ratio computes to 0.0, which is the thinnest possible reading and would arm S2 -
        a trade taken because the measurement was missing.
        """
        candles = _series(0, 0.0, 12, 5.0, 97.5)
        verdict = acceptance.evaluate(candles, self.levels, atr=2.0, prior_rate=0.0)
        self.assertEqual(verdict.verdict, acceptance.NO_BASELINE)
        self.assertEqual(verdict.baseline_source, "NONE")
        self.assertFalse(verdict.accepted)
        self.assertFalse(verdict.rejected,
                         "NO_BASELINE must not read as a rejection - that would arm S2")

    def test_prior_session_baseline_is_only_a_fallback(self):
        """When the session has its own baseline, it is preferred over yesterday's."""
        candles = _series(40, 100.0, 10, 5.0, 97.5)
        verdict = acceptance.evaluate(candles, self.levels, atr=2.0,
                                      prior_rate=999_999.0)
        self.assertEqual(verdict.baseline_source, "SESSION")
        self.assertAlmostEqual(verdict.session_rate, 100.0, places=6)

    def test_pending_between_the_thresholds(self):
        """The gap must be reachable, or the two setups are not mutually exclusive."""
        levels = self.levels
        found_pending = False
        for outside_volume in [v / 4.0 for v in range(1, 400)]:
            candles = _series(40, 100.0, 10, outside_volume, 97.5)
            verdict = acceptance.evaluate(candles, levels, atr=2.0)
            if verdict.verdict == acceptance.PENDING and verdict.side != "NONE":
                found_pending = True
                break
        self.assertTrue(found_pending, "no volume produces a PENDING verdict")


class FilterRoundingTests(unittest.TestCase):
    """Rounding direction, which is where a plausible-looking bug hides."""

    def setUp(self):
        self.spec = f.FakeSpec(tick_size="0.01", step_size="0.001")

    def test_buy_entry_rounds_down(self):
        price = filters.round_price_passive(100.0567, "0.01", "BUY")
        self.assertEqual(price, Decimal("100.05"))

    def test_sell_entry_rounds_up(self):
        price = filters.round_price_passive(100.0512, "0.01", "SELL")
        self.assertEqual(price, Decimal("100.06"))

    def test_long_stop_rounds_away_from_entry(self):
        # A long's stop is below entry, so DOWN is wider - never tighter.
        stop = filters.round_stop_price(99.0567, "0.01", "LONG")
        self.assertEqual(stop, Decimal("99.05"))

    def test_short_stop_rounds_away_from_entry(self):
        stop = filters.round_stop_price(101.0512, "0.01", "SHORT")
        self.assertEqual(stop, Decimal("101.06"))

    def test_long_target_rounds_toward_entry(self):
        target = filters.round_target_price(102.0567, "0.01", "LONG")
        self.assertEqual(target, Decimal("102.05"))

    def test_quantity_always_rounds_down(self):
        self.assertEqual(filters.round_quantity(1.2349, "0.001"), Decimal("1.234"))
        self.assertEqual(filters.round_quantity(1.2351, "0.001"), Decimal("1.235"))

    def test_float_artefacts_do_not_survive(self):
        # 0.1 + 0.2 == 0.30000000000000004 in float; the wire string must be clean.
        price = filters.round_price_passive(0.1 + 0.2, "0.01", "BUY")
        self.assertEqual(filters.price_str(price, f.FakeSpec(tick_size="0.01")),
                         "0.30")

    def test_wire_format_is_never_scientific(self):
        spec = f.FakeSpec(tick_size="0.00000001")
        price = filters.round_price_passive(0.000000015, "0.00000001", "BUY")
        text = filters.price_str(price, spec)
        self.assertNotIn("e", text.lower())
        self.assertEqual(text, "0.00000001")

    def test_stop_that_would_trigger_is_refused(self):
        with self.assertRaises(filters.FilterRejection):
            filters.check_stop_triggerable(101.0, 100.0, "LONG")
        with self.assertRaises(filters.FilterRejection):
            filters.check_stop_triggerable(99.0, 100.0, "SHORT")

    def test_valid_stop_passes(self):
        self.assertTrue(filters.check_stop_triggerable(99.0, 100.0, "LONG"))
        self.assertTrue(filters.check_stop_triggerable(101.0, 100.0, "SHORT"))

    def test_notional_floor_is_enforced(self):
        spec = f.FakeSpec(min_notional="5")
        with self.assertRaises(filters.FilterRejection):
            filters.check_notional(1.0, 1.0, spec)
        self.assertTrue(filters.check_notional(10.0, 1.0, spec))


class RiskSizingTests(unittest.TestCase):
    """Size comes from the stop distance, never from leverage."""

    def setUp(self):
        self.spec = f.FakeSpec()
        self.state = risk.PortfolioState(equity=10_000.0,
                                        available_balance=10_000.0)

    def _candidate(self, entry, stop, target):
        return Candidate(setup="S1-POC", symbol="TESTUSDT", direction="BUY",
                         entry_price=entry, stop_price=stop, target_price=target)

    def test_risk_amount_is_a_fixed_fraction_of_equity(self):
        candidate = self._candidate(100.0, 99.0, 102.0)
        rejection = risk.size(candidate, self.state, self.spec)
        self.assertIsNone(rejection)
        self.assertAlmostEqual(candidate.risk_amount,
                               10_000.0 * config.RISK_PCT, places=6)

    def test_loss_at_stop_equals_the_risk_budget(self):
        """The defining property: a stop-out costs RISK_PCT regardless of distance."""
        for stop in (99.5, 99.0, 95.0, 90.0):
            candidate = self._candidate(100.0, stop, 110.0)
            self.assertIsNone(risk.size(candidate, self.state, self.spec))
            loss = (100.0 - stop) * candidate.quantity
            self.assertAlmostEqual(loss, 10_000.0 * config.RISK_PCT, delta=1.0)

    def test_wider_stop_gives_smaller_size(self):
        tight = self._candidate(100.0, 99.5, 102.0)
        wide = self._candidate(100.0, 95.0, 110.0)
        risk.size(tight, self.state, self.spec)
        risk.size(wide, self.state, self.spec)
        self.assertGreater(tight.quantity, wide.quantity)

    def test_zero_risk_distance_is_refused(self):
        candidate = self._candidate(100.0, 100.0, 102.0)
        rejection = risk.size(candidate, self.state, self.spec)
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.reason, RejectReason.STOP_TOO_TIGHT)

    def test_direction_cap_blocks_a_crowded_side(self):
        state = risk.PortfolioState(equity=10_000.0, available_balance=10_000.0)
        for index in range(config.MAX_CONCURRENT_PER_DIRECTION):
            state.open_positions[f"SYM{index}USDT"] = 1.0    # longs
        candidate = self._candidate(100.0, 99.0, 102.0)
        # check_limits also enforces TRADING_ENABLED; patch it for this assertion.
        original = config.TRADING_ENABLED
        config.TRADING_ENABLED = True
        try:
            rejection = risk.check_limits(candidate, state)
        finally:
            config.TRADING_ENABLED = original
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.reason, RejectReason.RISK_LIMIT_DIRECTION)

    def test_consecutive_loss_limit_blocks_within_the_cooldown(self):
        state = risk.PortfolioState(equity=10_000.0, available_balance=10_000.0,
                                    consecutive_losses=config.CONSECUTIVE_LOSS_LIMIT,
                                    last_loss_closed_at=sessions.now_ms())
        candidate = self._candidate(100.0, 99.0, 102.0)
        original = config.TRADING_ENABLED
        config.TRADING_ENABLED = True
        try:
            rejection = risk.check_limits(candidate, state)
        finally:
            config.TRADING_ENABLED = original
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.reason, RejectReason.RISK_LIMIT_CONSECUTIVE)

    def test_consecutive_loss_limit_releases_once_the_cooldown_has_passed(self):
        """2026-10-10: without this, a losing streak with no open position left to
        produce the win that clears it refused every new entry forever - recoverable
        only by a human. The raw count is still over the limit here; only the AGE of
        the most recent loss has changed."""
        cooldown_ms = config.CONSECUTIVE_LOSS_COOLDOWN_HOURS * 3_600_000
        state = risk.PortfolioState(
            equity=10_000.0, available_balance=10_000.0,
            consecutive_losses=config.CONSECUTIVE_LOSS_LIMIT,
            last_loss_closed_at=sessions.now_ms() - int(cooldown_ms) - 1_000)
        candidate = self._candidate(100.0, 99.0, 102.0)
        original = config.TRADING_ENABLED
        config.TRADING_ENABLED = True
        try:
            rejection = risk.check_limits(candidate, state)
        finally:
            config.TRADING_ENABLED = original
        self.assertIsNone(rejection)

    def test_consecutive_loss_limit_ignores_cooldown_when_under_the_limit(self):
        """A streak that never reached the limit is not gated on age at all."""
        state = risk.PortfolioState(
            equity=10_000.0, available_balance=10_000.0,
            consecutive_losses=config.CONSECUTIVE_LOSS_LIMIT - 1,
            last_loss_closed_at=sessions.now_ms())
        candidate = self._candidate(100.0, 99.0, 102.0)
        original = config.TRADING_ENABLED
        config.TRADING_ENABLED = True
        try:
            rejection = risk.check_limits(candidate, state)
        finally:
            config.TRADING_ENABLED = original
        self.assertIsNone(rejection)

    def test_paper_mode_skips_only_the_trading_flag(self):
        """Observation mode must still be sized, or its journal is worthless.

        With paper=False a disabled bot refuses at TRADING_DISABLED before sizing, so
        the journal records that the flag is off and nothing about what the system would
        have done - which cannot be compared against a replay of the same sessions.
        """
        state = risk.PortfolioState(equity=10_000.0, available_balance=10_000.0)
        candidate = self._candidate(100.0, 99.0, 102.0)

        original = config.TRADING_ENABLED
        config.TRADING_ENABLED = False
        try:
            blocked = risk.check_limits(candidate, state)
            observed = risk.check_limits(candidate, state, paper=True)
        finally:
            config.TRADING_ENABLED = original

        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.reason, RejectReason.TRADING_DISABLED)
        self.assertIsNone(observed, "paper mode must pass the trading-flag check")

    def test_paper_mode_still_enforces_real_portfolio_limits(self):
        """Paper skips the flag, NOT the limits - otherwise the record is fiction."""
        state = risk.PortfolioState(equity=10_000.0, available_balance=10_000.0)
        for index in range(config.MAX_CONCURRENT_PER_DIRECTION):
            state.open_positions[f"SYM{index}USDT"] = 1.0
        candidate = self._candidate(100.0, 99.0, 102.0)

        original = config.TRADING_ENABLED
        config.TRADING_ENABLED = False
        try:
            rejection = risk.check_limits(candidate, state, paper=True)
        finally:
            config.TRADING_ENABLED = original

        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.reason, RejectReason.RISK_LIMIT_DIRECTION)

    def test_realised_r_is_measured_against_the_original_stop(self):
        # A stop-out that slipped 20% past the stop must read worse than -1R, or
        # slippage becomes invisible exactly where it costs most.
        outcome = risk.realised_r(entry_price=100.0, exit_price=98.8,
                                  stop_price=99.0, direction="BUY", quantity=1.0)
        self.assertLess(outcome["gross_r"], -1.0)
        self.assertLess(outcome["net_r"], outcome["gross_r"])


class FixedMarginSizingTests(unittest.TestCase):
    """FIXED_MARGIN_SIZING_ENABLED (opt-in, default off): quantity comes from
    MARGIN_PER_TRADE * LEVERAGE instead of from the stop, bot_ds-style."""

    def setUp(self):
        self.spec = f.FakeSpec()
        self.state = risk.PortfolioState(equity=10_000.0, available_balance=10_000.0)

    def _candidate(self, entry, stop, target=102.0):
        return Candidate(setup="S1-POC", symbol="TESTUSDT", direction="BUY",
                         entry_price=entry, stop_price=stop, target_price=target)

    def test_quantity_comes_from_margin_times_leverage_not_the_stop(self):
        candidate = self._candidate(100.0, 99.0)
        with patch.object(config, "FIXED_MARGIN_SIZING_ENABLED", True), \
             patch.object(config, "MARGIN_PER_TRADE", 5.0), \
             patch.object(config, "LEVERAGE", 5):
            rejection = risk.size(candidate, self.state, self.spec)
        self.assertIsNone(rejection)
        self.assertAlmostEqual(candidate.quantity, 0.25, places=6)
        self.assertAlmostEqual(candidate.notional, 25.0, places=6)

    def test_quantity_does_not_change_when_the_stop_moves(self):
        """The defining property of this mode: unlike stop-based sizing, the stop
        distance has no say in quantity at all - only margin and leverage do."""
        with patch.object(config, "FIXED_MARGIN_SIZING_ENABLED", True), \
             patch.object(config, "MARGIN_PER_TRADE", 5.0), \
             patch.object(config, "LEVERAGE", 5):
            tight = self._candidate(100.0, 99.5)
            wide = self._candidate(100.0, 90.0)
            risk.size(tight, self.state, self.spec)
            risk.size(wide, self.state, self.spec)
        self.assertAlmostEqual(tight.quantity, wide.quantity, places=6)
        # but the dollar risk that falls out of it is NOT constant - the whole tradeoff
        self.assertGreater(wide.risk_amount, tight.risk_amount)

    def test_insufficient_margin_when_available_balance_is_below_the_target(self):
        state = risk.PortfolioState(equity=10_000.0, available_balance=2.0)
        candidate = self._candidate(100.0, 99.0)
        with patch.object(config, "FIXED_MARGIN_SIZING_ENABLED", True), \
             patch.object(config, "MARGIN_PER_TRADE", 5.0), \
             patch.object(config, "LEVERAGE", 5):
            rejection = risk.size(candidate, state, self.spec)
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.reason, RejectReason.INSUFFICIENT_MARGIN)

    def test_zero_margin_per_trade_is_refused(self):
        candidate = self._candidate(100.0, 99.0)
        with patch.object(config, "FIXED_MARGIN_SIZING_ENABLED", True), \
             patch.object(config, "MARGIN_PER_TRADE", 0.0):
            rejection = risk.size(candidate, self.state, self.spec)
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.reason, RejectReason.INSUFFICIENT_MARGIN)


class StructuralTargetTests(unittest.TestCase):
    """The target chooser must not make structural mode disappear on some sessions."""

    class _Levels:
        poc_price = 103.0
        vah = 105.0
        val = 95.0

    def test_a_near_level_does_not_sink_the_setup(self):
        """Nearest-unconditionally is a selection bias, not just a worse target.

        A naked POC a fraction above entry yields ~0.2R, which reward_below_minimum
        rejects - so the candidate vanished entirely even though a perfectly good
        structural level sat further out in the same list. That removed setups
        specifically on sessions where a near level existed, which is not a random
        subset, so structural mode was measured on a different population from
        fixed-R mode.
        """
        price, kind = base.structural_target(
            100.0, "BUY", self._Levels(), naked_pocs=[100.2], stop_price=99.0)

        implied_r = abs(price - 100.0) / 1.0
        self.assertGreaterEqual(implied_r, config.TARGET_MIN_R,
                                f"chose {kind} @ {price} = {implied_r:.2f}R")
        self.assertEqual(price, 103.0, "should skip to the POC")

    def test_nearest_worthwhile_level_is_preferred_over_the_furthest(self):
        """Still nearest-first - closest is likeliest to be reached."""
        price, kind = base.structural_target(
            100.0, "BUY", self._Levels(), naked_pocs=[100.2], stop_price=99.5)
        # risk 0.5; POC at 103 is 6R, VAH at 105 is 10R. Both clear 1.2R -> take POC.
        self.assertEqual(price, 103.0, f"expected the nearer of the two, got {kind}")

    def test_falls_back_to_the_furthest_when_nothing_clears_the_floor(self):
        """An honest reading of the best the profile had, then a normal rejection."""
        class Tight:
            poc_price = 100.1
            vah = 100.3
            val = 95.0

        price, kind = base.structural_target(
            100.0, "BUY", Tight(), stop_price=99.0)
        self.assertEqual(price, 100.3,
                         "should report the furthest available, not the nearest")

    def test_direction_is_respected(self):
        for direction, expected_side in (("BUY", "above"), ("SELL", "below")):
            price, _ = base.structural_target(
                100.0, direction, self._Levels(), stop_price=(99.0 if direction == "BUY"
                                                              else 101.0))
            if expected_side == "above":
                self.assertGreater(price, 100.0)
            else:
                self.assertLess(price, 100.0)

    def test_no_viable_level_reports_nothing_rather_than_guessing(self):
        class Behind:
            poc_price = 98.0
            vah = 99.0
            val = 95.0

        price, kind = base.structural_target(100.0, "BUY", Behind(),
                                             stop_price=99.0)
        self.assertIsNone(price)
        self.assertEqual(kind, "")

    def test_hvns_and_prior_extreme_default_to_none_and_change_nothing(self):
        """PLAN item 17's enrichment is opt-in - omitting it must reproduce the
        exact, already-proven candidate set."""
        baseline = base.structural_target(100.0, "BUY", self._Levels(),
                                          naked_pocs=[100.2], stop_price=99.0)
        explicit_none = base.structural_target(100.0, "BUY", self._Levels(),
                                               naked_pocs=[100.2], stop_price=99.0,
                                               hvns=None, prior_extreme=None)
        self.assertEqual(baseline, explicit_none)


class StructuralTargetEnrichmentTests(unittest.TestCase):
    """PLAN item 17: HVN peaks and the prior session's opposite extreme as
    additional structural-target candidates, both opt-in via caller-supplied
    `hvns`/`prior_extreme` - config.py's flags gate whether a setup passes them in
    at all, not this function's own behaviour."""

    class _Levels:
        poc_price = 103.0
        vah = 105.0
        val = 95.0

    class _Node:
        def __init__(self, peak_price):
            self.peak_price = peak_price

    def test_an_hvn_peak_can_be_chosen_when_nearer_than_poc(self):
        price, kind = base.structural_target(
            100.0, "BUY", self._Levels(), stop_price=99.0,
            hvns=[self._Node(101.5)])
        self.assertEqual(price, 101.5)
        self.assertEqual(kind, "HVN")

    def test_an_hvn_behind_the_entry_is_skipped_like_any_other_level(self):
        price, kind = base.structural_target(
            100.0, "BUY", self._Levels(), stop_price=99.0,
            hvns=[self._Node(98.0)])
        self.assertEqual(price, 103.0, "the behind-entry HVN must not be chosen")

    def test_multiple_hvns_the_nearest_worthwhile_one_wins(self):
        price, kind = base.structural_target(
            100.0, "BUY", self._Levels(), stop_price=99.0,
            hvns=[self._Node(108.0), self._Node(101.5)])
        self.assertEqual(price, 101.5)
        self.assertEqual(kind, "HVN")

    def test_prior_extreme_is_a_candidate_when_ahead_of_entry(self):
        price, kind = base.structural_target(
            100.0, "BUY", self._Levels(), stop_price=99.0,
            prior_extreme=101.5)
        self.assertEqual(price, 101.5)
        self.assertEqual(kind, "priorExtreme")

    def test_prior_extreme_behind_the_entry_is_skipped(self):
        price, kind = base.structural_target(
            100.0, "BUY", self._Levels(), stop_price=99.0,
            prior_extreme=99.5)
        self.assertEqual(price, 103.0, "a prior extreme behind entry is not a target")

    def test_direction_is_respected_for_prior_extreme(self):
        price, kind = base.structural_target(
            100.0, "SELL", self._Levels(), stop_price=101.0,
            prior_extreme=98.5)
        self.assertEqual(price, 98.5)
        self.assertEqual(kind, "priorExtreme")

    def test_hvn_and_prior_extreme_combine_with_the_existing_candidates(self):
        """All candidate sources feed the same nearest-worthwhile selection."""
        price, kind = base.structural_target(
            100.0, "BUY", self._Levels(), naked_pocs=[100.2], stop_price=99.5,
            hvns=[self._Node(101.8)], prior_extreme=110.0)
        # risk 0.5: nPOC 100.2 is 0.4R (below min), HVN 101.8 is 3.6R, POC 103
        # is 6R, prior_extreme 110 is 20R. Nearest WORTHWHILE is the HVN.
        self.assertEqual(price, 101.8)
        self.assertEqual(kind, "HVN")


class ChooseTargetEnrichmentTests(unittest.TestCase):
    """`choose_target` threads hvns/prior_extreme straight through - thin wiring,
    one test per parameter is enough."""

    class _Levels:
        poc_price = 103.0
        vah = 105.0
        val = 95.0

    class _Node:
        def __init__(self, peak_price):
            self.peak_price = peak_price

    def test_hvns_reach_structural_target_through_choose_target(self):
        price, kind = base.choose_target(
            100.0, 99.0, "BUY", self._Levels(), mode="structural",
            hvns=[self._Node(101.5)])
        self.assertEqual(price, 101.5)
        self.assertEqual(kind, "structural:HVN")

    def test_prior_extreme_reaches_structural_target_through_choose_target(self):
        price, kind = base.choose_target(
            100.0, 99.0, "BUY", self._Levels(), mode="structural",
            prior_extreme=101.5)
        self.assertEqual(price, 101.5)
        self.assertEqual(kind, "structural:priorExtreme")


class StructuralTargetPreferNearTests(unittest.TestCase):
    """PLAN item 25: CALIBRATION.md's diagnosis found the naked POC - the far
    candidate set - wins the plain nearest-worthwhile sort on 63-66% of S2-VAR
    trades despite being much further away on average, and those trades win half
    as often as ones aimed at the session's own POC/VAH/VAL. `prefer_near=True`
    tests ranking the near set first, falling to the far set only when nothing
    near clears min_r."""

    class _Levels:
        poc_price = 103.0
        vah = 105.0
        val = 95.0

    def test_default_is_unchanged_a_nearer_far_candidate_still_wins(self):
        """Regression safety: omitting prefer_near must reproduce the exact
        existing flat-list, nearest-worthwhile-wins behaviour."""
        price, kind = base.structural_target(
            100.0, "BUY", self._Levels(), naked_pocs=[100.5], stop_price=99.9)
        # risk 0.1: nPOC at 100.5 is 5R (clears), POC at 103 is 30R. Flat-list
        # nearest-worthwhile is the nPOC, even though it is the "far" group.
        self.assertEqual(price, 100.5)
        self.assertEqual(kind, "nPOC")

    def test_prefer_near_picks_the_near_level_even_when_a_far_one_is_nearer(self):
        """The exact scenario the finding diagnosed: a far candidate (naked POC)
        sits NEARER than the session's own POC but must not be chosen once the
        near group has something that clears min_r."""
        price, kind = base.structural_target(
            100.0, "BUY", self._Levels(), naked_pocs=[100.5], stop_price=99.0,
            prefer_near=True)
        # risk 1.0: nPOC at 100.5 is 0.5R (near, but FAR GROUP), POC at 103 is
        # 3R (NEAR GROUP, clears min_r 1.2). prefer_near must take the POC.
        self.assertEqual(price, 103.0)
        self.assertEqual(kind, "POC")

    def test_falls_through_to_far_when_nothing_near_clears_min_r(self):
        """A tight value area: POC/VAH are both too close to clear min_r, so
        prefer_near must still reach the naked POC rather than giving up."""
        class Tight:
            poc_price = 100.1
            vah = 100.2
            val = 95.0

        price, kind = base.structural_target(
            100.0, "BUY", Tight(), naked_pocs=[103.0], stop_price=99.0,
            prefer_near=True)
        self.assertEqual(price, 103.0)
        self.assertEqual(kind, "nPOC")

    def test_furthest_available_fallback_when_nothing_anywhere_clears_min_r(self):
        class Tight:
            poc_price = 100.1
            vah = 100.2
            val = 95.0

        price, kind = base.structural_target(
            100.0, "BUY", Tight(), naked_pocs=[100.15], stop_price=99.0,
            prefer_near=True)
        # Neither 100.1 (POC), 100.2 (VAH) nor 100.15 (nPOC) clears 1.2R on a
        # 1.0 risk. The furthest of the three (VAH, 100.2) is the honest answer.
        self.assertEqual(price, 100.2)
        self.assertEqual(kind, "VAH")

    def test_no_near_levels_at_all_falls_straight_to_far(self):
        price, kind = base.structural_target(
            100.0, "BUY", None, naked_pocs=[103.0], stop_price=99.0,
            prefer_near=True)
        self.assertEqual(price, 103.0)
        self.assertEqual(kind, "nPOC")

    def test_direction_is_respected_under_prefer_near(self):
        price, kind = base.structural_target(
            100.0, "SELL", self._Levels(), naked_pocs=[94.0], stop_price=101.0,
            prefer_near=True)
        self.assertEqual(price, 95.0, "SELL's near candidate (VAL) must win")
        self.assertEqual(kind, "VAL")

    def test_prefer_near_without_a_stop_price_still_prefers_near(self):
        price, kind = base.structural_target(
            100.0, "BUY", self._Levels(), naked_pocs=[100.5], prefer_near=True)
        self.assertEqual(price, 103.0, "near group wins even with risk unknown")
        self.assertEqual(kind, "POC")


class ChooseTargetPreferNearTests(unittest.TestCase):
    """Thin wiring, same pattern as ChooseTargetEnrichmentTests."""

    class _Levels:
        poc_price = 103.0
        vah = 105.0
        val = 95.0

    def test_prefer_near_reaches_structural_target_through_choose_target(self):
        price, kind = base.choose_target(
            100.0, 99.0, "BUY", self._Levels(), mode="structural",
            naked_pocs=[100.5], prefer_near=True)
        self.assertEqual(price, 103.0)
        self.assertEqual(kind, "structural:POC")

    def test_choose_target_reports_the_fallback(self):
        """A silent fallback would contaminate the Phase 2 mode comparison."""
        class Behind:
            poc_price = 98.0
            vah = 99.0
            val = 95.0

        price, kind = base.choose_target(100.0, 99.0, "BUY", Behind(),
                                         mode="structural")
        self.assertIn("fallback", kind)
        self.assertAlmostEqual(price, 100.0 + 1.0 * config.TARGET_FIXED_R, places=6)


class MultibinDeltaNormalizedTests(unittest.TestCase):
    """PLAN item 6: delta_opposed widened from one bin to a window around the level.

    The single-bin figure (`bin_delta_normalized`, already recorded elsewhere) and
    this one must be able to disagree - otherwise the "multi-bin" name would be a
    fiction and the new gate would just be delta_opposed with extra config. Every
    fixture here is built so the level's own bin is neutral (delta=0) while its
    neighbours are not, so a test that only read the single bin would get 0.0 and a
    correct multi-bin read would not.
    """

    def _profile(self, bins):
        """bins: {index: (volume, delta)}."""
        profile = profile_builder.Profile(
            symbol="TESTUSDT", window_start=0, window_end=86_400_000,
            as_of=86_400_000, bin_size=1.0,
            volume={i: v for i, (v, _) in bins.items()},
            delta={i: d for i, (_, d) in bins.items()},
        )
        profile.total_volume = sum(v for v, _ in bins.values())
        return profile

    def test_reads_neighbouring_bins_the_single_bin_figure_would_miss(self):
        # Level at 100.5 -> bin 100, neutral. Bins 99 and 101 are fully one-sided.
        profile = self._profile({
            99: (10.0, 10.0), 100: (10.0, 0.0), 101: (10.0, 10.0),
        })
        single_bin = profile.delta_at(100) / profile.volume_at(100)
        self.assertEqual(single_bin, 0.0)

        result = base.multibin_delta_normalized(profile, 100.5, window_bins=1)
        # (10 + 0 + 10) / (10 + 10 + 10) = 20/30
        self.assertAlmostEqual(result, 20.0 / 30.0, places=9)
        self.assertNotEqual(result, single_bin,
                            "must differ from the single-bin figure to be a real "
                            "multi-bin read, not a relabelled copy of it")

    def test_window_defaults_to_config_not_hardcoded(self):
        profile = self._profile({
            98: (10.0, 10.0), 99: (10.0, 0.0), 100: (10.0, 0.0),
            101: (10.0, 0.0), 102: (10.0, 10.0),
        })
        with patch.object(config, "MULTIBIN_DELTA_WINDOW_BINS", 0):
            narrow = base.multibin_delta_normalized(profile, 100.5)
        with patch.object(config, "MULTIBIN_DELTA_WINDOW_BINS", 2):
            wide = base.multibin_delta_normalized(profile, 100.5)

        self.assertAlmostEqual(narrow, 0.0, places=9,
                               msg="window=0 must read only bin 100")
        self.assertAlmostEqual(wide, 20.0 / 50.0, places=9,
                               msg="window=2 must reach bins 98 and 102 too")

    def test_explicit_window_overrides_config(self):
        profile = self._profile({99: (10.0, 5.0), 100: (10.0, 0.0), 101: (10.0, 5.0)})
        with patch.object(config, "MULTIBIN_DELTA_WINDOW_BINS", 0):
            result = base.multibin_delta_normalized(profile, 100.5, window_bins=1)
        self.assertAlmostEqual(result, 10.0 / 30.0, places=9,
                               msg="an explicit window_bins must win over config")

    def test_no_volume_in_the_window_reads_as_unmeasured_not_neutral(self):
        profile = self._profile({})
        result = base.multibin_delta_normalized(profile, 100.5, window_bins=2)
        self.assertIsNone(result, "no flow measured must not read as flow of zero")

    def test_none_profile_is_unmeasured(self):
        self.assertIsNone(base.multibin_delta_normalized(None, 100.5))


class VwapZscoreTests(unittest.TestCase):
    """CALIBRATION.md finding 32: VWAP + its bands were already computed for every
    profile (profile/levels.py) but never exposed as a candidate measurement. This
    standardises price-vs-VWAP distance by the VWAP band's own sigma, the same way
    every other distance in this project is standardised by ATR."""

    def _levels(self, vwap, upper_1sd):
        return SimpleNamespace(vwap=vwap, vwap_upper_1sd=upper_1sd)

    def test_price_at_vwap_reads_zero(self):
        levels = self._levels(vwap=100.0, upper_1sd=101.0)
        self.assertAlmostEqual(base.vwap_zscore(levels, 100.0), 0.0, places=9)

    def test_price_one_sigma_above_reads_one(self):
        levels = self._levels(vwap=100.0, upper_1sd=101.0)
        self.assertAlmostEqual(base.vwap_zscore(levels, 101.0), 1.0, places=9)

    def test_price_two_sigma_below_reads_negative_two(self):
        levels = self._levels(vwap=100.0, upper_1sd=101.0)
        self.assertAlmostEqual(base.vwap_zscore(levels, 98.0), -2.0, places=9)

    def test_a_narrower_band_produces_a_larger_zscore_for_the_same_distance(self):
        wide = self._levels(vwap=100.0, upper_1sd=104.0)
        narrow = self._levels(vwap=100.0, upper_1sd=101.0)
        self.assertLess(base.vwap_zscore(wide, 102.0), base.vwap_zscore(narrow, 102.0),
                        "the same 2.0 distance must standardise to a smaller number "
                        "against a wider band - that's the whole point of dividing "
                        "by sigma instead of reporting raw distance")

    def test_zero_width_band_is_unmeasured_not_a_division_by_zero(self):
        levels = self._levels(vwap=100.0, upper_1sd=100.0)
        self.assertIsNone(base.vwap_zscore(levels, 105.0))

    def test_none_levels_is_unmeasured(self):
        self.assertIsNone(base.vwap_zscore(None, 100.0))

    def test_none_vwap_is_unmeasured(self):
        levels = self._levels(vwap=None, upper_1sd=101.0)
        self.assertIsNone(base.vwap_zscore(levels, 100.0))


class WeeklyPocDistanceAtrTests(unittest.TestCase):
    """PLAN item 21: `ctx.weekly_levels` was a dead field, architected for and
    never populated. This is its "measure before gate" companion, same pattern
    as `vwap_zscore` - a signed, ATR-normalised distance to last week's POC."""

    def _levels(self, poc_price):
        return SimpleNamespace(poc_price=poc_price)

    def test_price_above_weekly_poc_reads_positive(self):
        levels = self._levels(poc_price=100.0)
        self.assertAlmostEqual(base.weekly_poc_distance_atr(levels, 102.0, atr=2.0),
                               1.0, places=9)

    def test_price_below_weekly_poc_reads_negative(self):
        levels = self._levels(poc_price=100.0)
        self.assertAlmostEqual(base.weekly_poc_distance_atr(levels, 96.0, atr=2.0),
                               -2.0, places=9)

    def test_price_at_weekly_poc_reads_zero(self):
        levels = self._levels(poc_price=100.0)
        self.assertAlmostEqual(base.weekly_poc_distance_atr(levels, 100.0, atr=2.0),
                               0.0, places=9)

    def test_none_weekly_levels_is_unmeasured(self):
        self.assertIsNone(base.weekly_poc_distance_atr(None, 100.0, atr=2.0))

    def test_zero_or_none_atr_is_unmeasured_not_a_division_error(self):
        levels = self._levels(poc_price=100.0)
        self.assertIsNone(base.weekly_poc_distance_atr(levels, 102.0, atr=0.0))
        self.assertIsNone(base.weekly_poc_distance_atr(levels, 102.0, atr=None))


class WeeklyAndVwapBothOpposeTests(unittest.TestCase):
    """Validated 2026-10-09 against 560 pooled S4-OFR trades - see the function's
    own docstring for the numbers. A stronger, separate veto from the 6-vote
    composite: that composite alone does not catch this population (60/126 of
    these trades still had the composite agreeing, 66 more neutral)."""

    def test_both_readings_opposing_a_buy_vetoes_it(self):
        # weekly_distance > 0 votes BUY, dev_vwap_z > 0 votes SELL - so a BUY here
        # needs weekly_distance < 0 and dev_vwap_z > 0 to have both oppose it.
        self.assertTrue(base.weekly_and_vwap_both_oppose(-1.0, 0.5, "BUY"))

    def test_both_readings_opposing_a_sell_vetoes_it(self):
        self.assertTrue(base.weekly_and_vwap_both_oppose(1.0, -0.5, "SELL"))

    def test_both_readings_supporting_the_trade_does_not_veto(self):
        self.assertFalse(base.weekly_and_vwap_both_oppose(1.0, -0.5, "BUY"))
        self.assertFalse(base.weekly_and_vwap_both_oppose(-1.0, 0.5, "SELL"))

    def test_the_two_readings_disagreeing_with_each_other_does_not_veto(self):
        """Weekly says BUY, VWAP says SELL (or vice versa) - no shared opinion to
        veto with, regardless of which side the trade is on."""
        self.assertFalse(base.weekly_and_vwap_both_oppose(1.0, 0.5, "BUY"))
        self.assertFalse(base.weekly_and_vwap_both_oppose(1.0, 0.5, "SELL"))

    def test_either_reading_missing_does_not_veto(self):
        self.assertFalse(base.weekly_and_vwap_both_oppose(None, 0.5, "BUY"))
        self.assertFalse(base.weekly_and_vwap_both_oppose(-1.0, None, "BUY"))
        self.assertFalse(base.weekly_and_vwap_both_oppose(None, None, "BUY"))

    def test_either_reading_flat_does_not_veto(self):
        self.assertFalse(base.weekly_and_vwap_both_oppose(0.0, 0.5, "BUY"))
        self.assertFalse(base.weekly_and_vwap_both_oppose(-1.0, 0.0, "BUY"))


class CostInRTests(unittest.TestCase):
    """Fees expressed in R - the only way they are comparable across trades."""

    def test_tighter_stop_means_higher_cost_in_r(self):
        tight = Candidate(setup="S1-LVN", symbol="T", direction="BUY",
                          entry_price=100.0, stop_price=99.9, target_price=100.2)
        wide = Candidate(setup="S1-POC", symbol="T", direction="BUY",
                         entry_price=100.0, stop_price=98.0, target_price=104.0)
        self.assertGreater(tight.round_trip_cost_r(), wide.round_trip_cost_r())

    def test_net_r_is_below_gross_r(self):
        candidate = Candidate(setup="S2-VAR", symbol="T", direction="BUY",
                              entry_price=100.0, stop_price=99.0,
                              target_price=102.0)
        self.assertAlmostEqual(candidate.r_multiple, 2.0, places=6)
        self.assertLess(candidate.net_r_at_target(), candidate.r_multiple)


class RejectCoverageTests(unittest.TestCase):
    """No reason may be unreachable, and none may be a duplicate string."""

    def test_reason_values_are_unique(self):
        values = [reason.value for reason in RejectReason]
        self.assertEqual(len(values), len(set(values)))

    def test_every_reason_is_a_plain_upper_snake_string(self):
        for reason in RejectReason:
            self.assertRegex(reason.value, r"^[A-Z][A-Z0-9_]*$")


class ConfigReferenceTests(unittest.TestCase):
    """Every config attribute the code reads must exist.

    THE BUG THIS CATCHES IS INVISIBLE TO ORDINARY TESTS. A gate's reject message is
    built only when the gate FIRES, so a stale config name inside that f-string sits on
    a branch the happy path never takes. `poc_unstable` referenced
    STABILITY_MAX_SHIFT_BINS - renamed months earlier when the threshold was split into
    POC and value-area variants - and would have raised AttributeError the first time it
    met an unstable POC, mid-scan, in live. 96 passing tests and a clean live scan never
    touched it, because none of them had an unstable POC to reject.

    Static reachability is the right instrument here: the failing line is reached by
    market data, not by a code path a test can be relied on to enumerate.
    """

    def _config_references(self):
        import ast
        root = Path(__file__).resolve().parents[1]
        found = {}
        for path in sorted(root.rglob("*.py")):
            parts = path.relative_to(root).parts
            # __pycache__ is never project code; venv is only ever present when a
            # virtualenv happens to be nested inside the repo (e.g. on a deploy host)
            # rather than beside it - its vendored packages are not this project's code
            # and reference their own "config" objects that have nothing to do with
            # this module.
            if "__pycache__" in parts or "venv" in parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:                       # pragma: no cover
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "config"):
                    found.setdefault(node.attr, []).append(
                        f"{path.relative_to(root)}:{node.lineno}")
        return found

    def test_no_code_reads_a_config_attribute_that_does_not_exist(self):
        references = self._config_references()
        self.assertGreater(len(references), 50,
                           "the scan found suspiciously few references")

        missing = {name: where for name, where in references.items()
                   if not hasattr(config, name)}
        self.assertEqual(
            missing, {},
            "config attributes referenced but not defined:\n" + "\n".join(
                f"  config.{name} at {', '.join(where)}"
                for name, where in sorted(missing.items())))

    def test_every_gate_can_format_its_own_rejection(self):
        """Exercise each gate's failure branch, which is where stale names hide.

        Complements the static check by actually running the f-strings: a name built
        dynamically, or read from a nested object, would pass the AST scan and still
        fail here.
        """
        from gates import profiles as gate_profiles
        from setups.base import Candidate

        gates = {gate for setup in ("S1-POC", "S1-LVN", "S2-VAR", "S3-BRK")
                 for gate in gate_profiles.gates_for(setup)}
        self.assertGreater(len(gates), 8)

        # A context and candidate built to FAIL as much as possible, so the greatest
        # number of gates take their reject branch rather than returning None.
        ctx = _FailingContext()
        candidate = Candidate(
            setup="S1-POC", symbol="TESTUSDT", direction="BUY",
            session_id="2026-09-27", entry_price=100.0, stop_price=100.0,
            target_price=100.0001, quantity=1.0, level_price=100.0, atr=1.0,
        )

        formatted = 0
        for gate in gates:
            try:
                result = gate(candidate, ctx)
            except AttributeError as exc:
                self.fail(f"{gate.__name__} raised AttributeError on its reject "
                          f"path: {exc}")
            except Exception:                         # noqa: BLE001
                # A gate may legitimately refuse this deliberately-broken context in
                # other ways; only a missing attribute is the bug under test.
                continue
            if result is not None:
                self.assertIsInstance(result.detail, str)
                formatted += 1

        self.assertGreater(formatted, 3,
                           "too few gates actually rejected, so little was exercised")


class ConfirmIntervalRescalingTests(unittest.TestCase):
    """PLAN item 7's prerequisite: candle-count thresholds must track CONFIRM_INTERVAL.

    Without this, a confirm-interval ablation silently changes what a threshold MEANS
    instead of just how granularly it is measured - "13 candles outside value" is
    3.25h at 15m and 0.22h at 1m, a different claim, not the same one sampled finer.
    """

    def test_default_interval_reproduces_the_exact_historical_candle_counts(self):
        """The regression lock: nothing may change for anyone who touches nothing."""
        self.assertEqual(config.CONFIRM_INTERVAL, "15m")
        self.assertEqual(config.CONFIRM_INTERVAL_MINUTES, 15)
        self.assertEqual(config.ACCEPT_MIN_CANDLES, 3)
        self.assertEqual(config.ACCEPT_MIN_CANDLES_OUTSIDE, 13)
        self.assertEqual(config.REJECT_MAX_CANDLES_OUTSIDE, 5)
        self.assertEqual(config.ACCEPT_MIN_BASELINE_CANDLES, 6)
        self.assertEqual(config.CONFIRM_CONSECUTIVE_CANDLES, 2)

    def test_interval_minutes_reads_known_intervals_and_falls_back_on_unknown(self):
        self.assertEqual(config.interval_minutes("15m"), 15)
        self.assertEqual(config.interval_minutes("5m"), 5)
        self.assertEqual(config.interval_minutes("1h"), 60)
        self.assertEqual(config.interval_minutes("bogus", default_minutes=15), 15)
        self.assertEqual(config.interval_minutes("bogus", default_minutes=7), 7)

    def test_a_finer_interval_rescales_the_candle_count_up(self):
        """Same real-world duration, more candles to reach it at a finer interval."""
        with patch.object(config, "CONFIRM_INTERVAL_MINUTES", 5):
            # 195 minutes / 5 = 39, not 13 (what it would wrongly stay at unscaled).
            self.assertEqual(config.candles_for_minutes(195), 39)
            self.assertEqual(config.candles_for_minutes(75), 15)
            self.assertEqual(config.candles_for_minutes(90), 18)
            self.assertEqual(config.candles_for_minutes(45), 9)
            self.assertEqual(config.candles_for_minutes(30), 6)

    def test_a_coarser_interval_rescales_the_candle_count_down(self):
        with patch.object(config, "CONFIRM_INTERVAL_MINUTES", 60):
            # 195 minutes / 60 = 3.25 -> rounds to 3, not floors to 2.
            self.assertEqual(config.candles_for_minutes(195), 3)

    def test_never_rescales_to_zero_candles(self):
        """A duration shorter than one candle must still require at least one."""
        with patch.object(config, "CONFIRM_INTERVAL_MINUTES", 240):
            self.assertEqual(config.candles_for_minutes(30), 1)


class _FailingStability:
    poc_shift_bins = 99.0
    vah_shift_bins = 99.0
    val_shift_bins = 99.0
    poc_prominence = 0.01
    poc_stable = False
    value_bounds_stable = False
    poc_prominent = False


class _FailingProfile:
    total_quote_volume = 1.0
    candle_count = 1
    total_volume = 1.0
    source_interval = "1m"
    coverage = 0.01
    volume = {1: 1.0}
    low, high, range = 99.0, 101.0, 2.0
    bin_size = 0.01

    def histogram(self):
        return [(1, 1.0)]


class _FailingLevels:
    poc_price, poc_volume, poc_prominence = 100.0, 1.0, 0.01
    vah, val = 100.0005, 99.9995
    value_width = 0.001
    value_fraction = 0.7
    hvns, lvns = [], []

    def hvn_containing(self, price):
        return None

    def nearest_lvn_between(self, a, b):
        return None


class _FailingContext:
    """A context engineered so as many gates as possible take their reject branch."""
    symbol = "TESTUSDT"
    session_id = "2026-09-27"
    as_of = 1_758_844_800_000
    atr = 1.0
    spec = None
    last_price = mark_price = 100.0
    prior_profile = _FailingProfile()
    prior_levels = _FailingLevels()
    prior_stability = _FailingStability()
    prior_shape = None
    dev_profile = _FailingProfile()
    dev_levels = _FailingLevels()
    dev_shape = None
    open_relationship = None
    migration = None
    confirm_candles = ()
    session_candles = ()
    naked_pocs = ()
    weekly_levels = None
    maturity = {"mature": False, "reason": "test", "elapsed_minutes": 1,
                "volume_fraction": 0.01}

    def profile_row(self):
        return {}


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TargetCandidatesRecordingTests(unittest.TestCase):
    """The counterfactual study reads this field, so it has to list every level on the
    trade's side of entry, including the ones the live rule does not currently use."""

    class _Levels:
        poc_price = 100.0
        vah = 102.0
        val = 98.0
        hvns = []

    def test_buy_lists_only_levels_above_entry_regardless_of_flags(self):
        from setups.base import target_candidates
        text = target_candidates(99.0, "BUY", self._Levels(), naked_pocs=[101.5, 97.0],
                                 hvns=[], prior_extreme=103.0)
        self.assertEqual(text, "POC:100.0;VAH:102.0;nPOC:101.5;priorExtreme:103.0")

    def test_sell_lists_only_levels_below_entry(self):
        from setups.base import target_candidates
        text = target_candidates(101.0, "SELL", self._Levels(), naked_pocs=[97.0],
                                 hvns=[], prior_extreme=96.0)
        self.assertEqual(text, "POC:100.0;VAL:98.0;nPOC:97.0;priorExtreme:96.0")

    def test_empty_when_nothing_on_the_trade_side(self):
        from setups.base import target_candidates
        self.assertEqual(target_candidates(200.0, "BUY", self._Levels()), "")
