"""End-to-end pipeline tests on constructed sessions.

These are the tests that prove the parts compose: a hand-built prior session with a
known POC and value area, a hand-built current session that approaches it, and an
assertion that the correct setup fires with the correct direction and geometry.

WHAT THESE TESTS CAN AND CANNOT DO. They verify CORRECTNESS - that the code computes
what it claims, that conditions gate in the right direction, that a candidate's stop
is on the losing side of its entry. They cannot verify CALIBRATION, because the
fixtures embed an assumption about what real profiles look like and that assumption
is the thing a threshold needs calibrating against. See CALIBRATION.md.
"""
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import acceptance as acceptance_mod                               # noqa: E402
import config                                                     # noqa: E402
import sessions                                                   # noqa: E402
from data import klines as klines_mod                             # noqa: E402
from gates import profiles as gate_profiles                       # noqa: E402
from profile import builder, relations, shape as shape_mod        # noqa: E402
from profile import stability as stability_mod                    # noqa: E402
from setups import lvn_rejection, poc_rotation, va_breakout, va_reversion  # noqa: E402
from setups.base import vwap_zscore, weekly_poc_distance_atr       # noqa: E402
from setups.context import SetupContext                           # noqa: E402
from state_machine import StateMachine                            # noqa: E402
from tests import factories as f                                  # noqa: E402

DAY = sessions.MS_DAY
PREV_START = (1_758_844_800_000 // DAY) * DAY
CUR_START = PREV_START + DAY

# ALL FOUR SETUPS PINNED ENABLED FOR THIS WHOLE MODULE. Phase 1 + the matched-random
# control (CALIBRATION.md finding 15) moved every setup's shipped default to False: none
# beat a random entry at the same location, and S3-BRK was measurably WORSE. That is a
# statement about whether to TRADE a setup live, not about whether its detection logic is
# correct - and this file tests detection logic exclusively, on hand-built fixtures a real
# market may never produce. Without this pin, every test below would immediately return
# SETUP_DISABLED against the shipped default and the file would test nothing.
_setup_enabled_patchers = [
    patch.object(config, "S1_POC_ENABLED", True),
    patch.object(config, "S1_LVN_ENABLED", True),
    patch.object(config, "S2_ENABLED", True),
    patch.object(config, "S3_ENABLED", True),
]


def setUpModule():
    for patcher in _setup_enabled_patchers:
        patcher.start()


def tearDownModule():
    for patcher in _setup_enabled_patchers:
        patcher.stop()


def build_ctx(prior_candles, current_candles, bin_size=0.05, atr=2.5,
              typical_volume=1.0, weekly_levels=None):
    """Assemble a SetupContext the same way scanner.build_context does."""
    prior_profile = builder.profile_as_of("TESTUSDT", prior_candles, PREV_START,
                                         PREV_START + DAY, PREV_START + DAY, bin_size)
    from profile import levels as levels_mod
    prior_levels = levels_mod.compute(prior_profile)
    migration = shape_mod.intra_session_poc_migration(
        builder, "TESTUSDT", prior_candles, PREV_START, PREV_START + DAY,
        PREV_START + DAY, bin_size, atr)
    prior_shape = shape_mod.classify(prior_profile, prior_levels,
                                    intra_poc_migration_atr=migration)
    prior_stability = stability_mod.assess("TESTUSDT", prior_candles, PREV_START,
                                          PREV_START + DAY, PREV_START + DAY,
                                          bin_size, prior_levels)

    as_of = current_candles[-1].close_time
    dev_profile = builder.profile_as_of("TESTUSDT", current_candles, CUR_START,
                                       CUR_START + DAY, as_of, bin_size)
    dev_levels = levels_mod.compute(dev_profile)

    open_rel = relations.classify_open(current_candles[0].open, prior_levels,
                                      prior_profile, atr)
    confirm = klines_mod.resample(current_candles, config.CONFIRM_INTERVAL, "1m")

    return SetupContext(
        symbol="TESTUSDT",
        spec=f.FakeSpec(),
        as_of=as_of,
        session_id=sessions.session_id(CUR_START),
        prior_profile=prior_profile,
        prior_levels=prior_levels,
        prior_shape=prior_shape,
        prior_stability=prior_stability,
        dev_profile=dev_profile,
        dev_levels=dev_levels,
        maturity={"elapsed_minutes": 9999, "volume_fraction": 1.0, "mature": True},
        open_relationship=open_rel,
        migration=None,
        atr=atr,
        confirm_candles=tuple(confirm),
        session_candles=tuple(current_candles),
        last_price=current_candles[-1].close,
        mark_price=current_candles[-1].close,
        weekly_levels=weekly_levels,
    )


def walk(start_ts, prices, volume=10.0, wick=0.0, buy_fraction=0.5):
    """Candles closing at each given price, optionally with an overshoot wick."""
    out = []
    ts = start_ts
    for price in prices:
        out.append(f.candle(ts, price, price + wick, price - wick, price,
                            volume, buy_fraction))
        ts += 60_000
    return out


class BalancedPriorSession(unittest.TestCase):
    """The fixture itself must be what the tests assume it is."""

    def setUp(self):
        self.prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0,
                                    tail=0.25)
        self.profile, self.levels, self.shape = f.make_profile(
            "TESTUSDT", self.prior, PREV_START, PREV_START + DAY, bin_size=0.05)

    def test_prior_session_is_balanced(self):
        self.assertEqual(self.shape.label, "D")
        self.assertEqual(self.shape.auction_state, "BALANCE")

    def test_poc_is_central(self):
        self.assertAlmostEqual(self.levels.poc_price, 100.0, delta=0.1)

    def test_value_area_brackets_the_poc(self):
        self.assertLess(self.levels.val, self.levels.poc_price)
        self.assertGreater(self.levels.vah, self.levels.poc_price)

    def test_value_fraction_meets_target(self):
        # Whole-pair expansion overshoots; it must never UNDER-cover.
        self.assertGreaterEqual(self.levels.value_fraction, config.VALUE_AREA_PCT)


class S1PocRotationTests(unittest.TestCase):
    """Open above value, travel down to the POC, reject there -> BUY."""

    def setUp(self):
        self.prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0,
                                    tail=0.25)
        _, self.levels, _ = f.make_profile("TESTUSDT", self.prior, PREV_START,
                                           PREV_START + DAY, bin_size=0.05)
        # Open comfortably above VAH but inside the prior high.
        self.open_price = self.levels.vah + self.levels.value_width * 0.30

    def _current(self, final_prices, wick=0.0):
        prices = [self.open_price] * 30
        step = (self.levels.poc_price - self.open_price) / 60
        prices += [self.open_price + step * i for i in range(60)]
        prices += final_prices
        return walk(CUR_START, prices, wick=wick)

    def test_fires_buy_when_poc_rejects_from_above(self):
        # Wick below the POC, closing back above it: a long rejection.
        poc = self.levels.poc_price
        current = self._current([poc - 0.05] * 5, wick=0.0)
        # Replace the last candles with an explicit wick-and-reclaim.
        ts = current[-1].open_time + 60_000
        for index in range(15):
            current.append(f.candle(ts, poc, poc + 0.30, poc - 0.60, poc + 0.25,
                                    12.0, 0.70))
            ts += 60_000

        ctx = build_ctx(self.prior, current)
        self.assertTrue(ctx.open_relationship.outside_value)
        self.assertFalse(ctx.open_relationship.outside_range)

        result = poc_rotation.detect(ctx)
        self.assertFalse(result.is_rejection,
                         f"expected a candidate, got {getattr(result, 'reason', None)} "
                         f"{getattr(result, 'detail', '')}")
        self.assertEqual(result.direction, "BUY")
        self.assertEqual(result.level_kind, "POC")

    def test_stop_is_below_entry_for_a_buy(self):
        poc = self.levels.poc_price
        current = self._current([])
        ts = current[-1].open_time + 60_000
        for _ in range(15):
            current.append(f.candle(ts, poc, poc + 0.30, poc - 0.60, poc + 0.25,
                                    12.0, 0.70))
            ts += 60_000

        result = poc_rotation.detect(build_ctx(self.prior, current))
        self.assertFalse(result.is_rejection)
        self.assertLess(result.stop_price, result.entry_price)
        self.assertGreater(result.target_price, result.entry_price)
        self.assertGreater(result.risk_distance, 0)

    def test_rejected_when_session_opens_inside_value(self):
        inside = self.levels.poc_price
        current = walk(CUR_START, [inside] * 120)
        result = poc_rotation.detect(build_ctx(self.prior, current))
        self.assertTrue(result.is_rejection)
        self.assertEqual(result.reason.value, "OPEN_RELATIONSHIP_WRONG")

    def test_plan_17_flags_now_default_on_in_the_live_setup(self):
        """PLAN item 17 shipped on the 3-window-replicated evidence in findings
        46/47: the enriched candidate set (HVNs + prior-session extreme) is now
        the live default, not an opt-in. TARGET_PREFER_NEAR_LEVEL (item 25)
        stayed default off - finding 45 found it a near no-op."""
        self.assertTrue(config.TARGET_INCLUDE_HVN)
        self.assertTrue(config.TARGET_INCLUDE_PRIOR_EXTREME)
        self.assertFalse(config.TARGET_PREFER_NEAR_LEVEL)
        poc = self.levels.poc_price
        current = self._current([poc - 0.05] * 5, wick=0.0)
        ts = current[-1].open_time + 60_000
        for _ in range(15):
            current.append(f.candle(ts, poc, poc + 0.30, poc - 0.60, poc + 0.25,
                                    12.0, 0.70))
            ts += 60_000
        with patch.object(config, "TARGET_MODE", "structural"):
            result = poc_rotation.detect(build_ctx(self.prior, current))
        self.assertFalse(result.is_rejection)
        prior_high = max(candle.high for candle in self.prior)
        self.assertAlmostEqual(result.target_price, prior_high, delta=1e-9)
        self.assertEqual(result.attributes.get("target_kind"), "structural:priorExtreme",
                         "with the item 17 flags now default True, the enriched "
                         "candidate set is what fires without any patching")

    def test_plan_17_flags_disabled_still_falls_back_to_the_pre_item_17_candidate_set(self):
        """Regression coverage for the pre-item-17 behaviour: with both flags
        explicitly off, the original proven candidate set (POC/VAH/VAL/naked
        POC only) must still produce exactly what it always did."""
        poc = self.levels.poc_price
        current = self._current([poc - 0.05] * 5, wick=0.0)
        ts = current[-1].open_time + 60_000
        for _ in range(15):
            current.append(f.candle(ts, poc, poc + 0.30, poc - 0.60, poc + 0.25,
                                    12.0, 0.70))
            ts += 60_000
        with patch.object(config, "TARGET_MODE", "structural"), \
             patch.object(config, "TARGET_INCLUDE_HVN", False), \
             patch.object(config, "TARGET_INCLUDE_PRIOR_EXTREME", False):
            result = poc_rotation.detect(build_ctx(self.prior, current))
        self.assertFalse(result.is_rejection)
        self.assertEqual(result.attributes.get("target_kind"), "structural:VAH")

    def test_plan_17_prior_extreme_flag_reaches_the_live_setup(self):
        """Opt-in path: with the flag on, the prior session's own high becomes a
        usable candidate - proves the config flag genuinely reaches
        poc_rotation.detect(), not just research/replay.py's CLI override.

        In this fixture VAH sits too close to entry to clear TARGET_MIN_R, so
        the proven candidate set's baseline (above) falls back to VAH anyway as
        the best available even though it does not really clear the floor - the
        exact failure mode item 17 exists to improve on. With the flag on, the
        prior session's high clears the floor for real and is preferred."""
        poc = self.levels.poc_price
        current = self._current([poc - 0.05] * 5, wick=0.0)
        ts = current[-1].open_time + 60_000
        for _ in range(15):
            current.append(f.candle(ts, poc, poc + 0.30, poc - 0.60, poc + 0.25,
                                    12.0, 0.70))
            ts += 60_000
        with patch.object(config, "TARGET_MODE", "structural"), \
             patch.object(config, "TARGET_INCLUDE_PRIOR_EXTREME", True):
            result = poc_rotation.detect(build_ctx(self.prior, current))
        self.assertFalse(result.is_rejection)
        prior_high = max(candle.high for candle in self.prior)
        self.assertAlmostEqual(result.target_price, prior_high, delta=1e-9)
        self.assertEqual(result.attributes.get("target_kind"), "structural:priorExtreme")

    def test_plan_25_prefer_near_flag_reaches_the_live_setup(self):
        """With item 17's enrichment flags off and no naked POCs in this
        fixture, there are no far candidates at all, so prefer_near has
        nothing to reorder and must reach the identical VAH-fallback result
        as the plain baseline - proving the flag is read by
        poc_rotation.detect() without changing behaviour it has no business
        changing."""
        poc = self.levels.poc_price
        current = self._current([poc - 0.05] * 5, wick=0.0)
        ts = current[-1].open_time + 60_000
        for _ in range(15):
            current.append(f.candle(ts, poc, poc + 0.30, poc - 0.60, poc + 0.25,
                                    12.0, 0.70))
            ts += 60_000
        with patch.object(config, "TARGET_MODE", "structural"), \
             patch.object(config, "TARGET_INCLUDE_HVN", False), \
             patch.object(config, "TARGET_INCLUDE_PRIOR_EXTREME", False), \
             patch.object(config, "TARGET_PREFER_NEAR_LEVEL", True):
            result = poc_rotation.detect(build_ctx(self.prior, current))
        self.assertFalse(result.is_rejection)
        self.assertEqual(result.attributes.get("target_kind"), "structural:VAH")
        self.assertAlmostEqual(result.target_price, self.levels.vah, delta=1e-9)

    def test_rejected_when_price_never_reaches_the_poc(self):
        prices = [self.open_price] * 120
        result = poc_rotation.detect(build_ctx(self.prior, walk(CUR_START, prices)))
        self.assertTrue(result.is_rejection)
        self.assertEqual(result.reason.value, "PRICE_NOT_AT_LEVEL")

    def test_default_stop_mode_is_buffer(self):
        self.assertEqual(config.S1_STOP_MODE, "buffer")

    def test_buffer_mode_geometry_is_unchanged_by_the_lvn_refactor(self):
        """Belt-and-suspenders, same pattern as S3's ConfirmedModeUnchangedTests:
        the refactor that added S1_STOP_MODE touched detect() end to end, so the
        default arm's exact stop price must still match the pre-refactor formula."""
        poc = self.levels.poc_price
        current = self._current([poc - 0.05] * 5, wick=0.0)
        ts = current[-1].open_time + 60_000
        for _ in range(15):
            current.append(f.candle(ts, poc, poc + 0.30, poc - 0.60, poc + 0.25,
                                    12.0, 0.70))
            ts += 60_000
        result = poc_rotation.detect(build_ctx(self.prior, current))
        self.assertFalse(result.is_rejection)
        tick = float(f.FakeSpec().tick_size)
        expected_buffer = max(2.5 * config.S1_STOP_BUFFER_ATR,
                              tick * config.S1_STOP_BUFFER_TICKS)
        # This fixture approaches from ABOVE -> BUY -> stop is BELOW the POC.
        self.assertEqual(result.direction, "BUY")
        self.assertAlmostEqual(result.stop_price, poc - expected_buffer, places=9)
        self.assertEqual(result.attributes["stop_reference"], "poc_buffer")


class S1LvnStopModeTests(unittest.TestCase):
    """config.S1_STOP_MODE="lvn": stop past the nearest thin area, not the POC itself.

    See CALIBRATION.md finding 15 / poc_rotation.py's module docstring for the
    diagnosis this tests: a fixed buffer past the POC sits in the densest,
    most rotation-prone part of the distribution.
    """

    def setUp(self):
        patcher = patch.object(config, "S1_STOP_MODE", "lvn")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0,
                                    tail=0.25)
        _, self.levels, _ = f.make_profile("TESTUSDT", self.prior, PREV_START,
                                           PREV_START + DAY, bin_size=0.05)
        self.open_price = self.levels.vah + self.levels.value_width * 0.30

    def _current(self, final_prices, wick=0.0):
        prices = [self.open_price] * 30
        step = (self.levels.poc_price - self.open_price) / 60
        prices += [self.open_price + step * i for i in range(60)]
        prices += final_prices
        return walk(CUR_START, prices, wick=wick)

    def _fire(self):
        poc = self.levels.poc_price
        current = self._current([poc - 0.05] * 5, wick=0.0)
        ts = current[-1].open_time + 60_000
        for _ in range(15):
            current.append(f.candle(ts, poc, poc + 0.30, poc - 0.60, poc + 0.25,
                                    12.0, 0.70))
            ts += 60_000
        return poc_rotation.detect(build_ctx(self.prior, current))

    def test_stop_reference_is_always_present_and_valid(self):
        """Whether or not this fixture happens to produce a qualifying LVN, the
        candidate must always record which reference it actually used - never
        silently omitted, matching the precedent in va_breakout's own LVN test."""
        result = self._fire()
        self.assertFalse(result.is_rejection,
                         f"got {getattr(result, 'reason', None)} "
                         f"{getattr(result, 'detail', '')}")
        self.assertIn(result.attributes["stop_reference"],
                      ("lvn", "value_edge_fallback"))
        self.assertEqual(result.attributes["stop_mode"], "lvn")

    def test_stop_is_further_from_poc_than_the_default_buffer_would_be(self):
        """The whole point: an LVN or the value edge is further out than a small
        fixed buffer, on a fixture shaped like a real balanced session."""
        result = self._fire()
        self.assertFalse(result.is_rejection)
        poc = self.levels.poc_price
        default_buffer = max(2.5 * config.S1_STOP_BUFFER_ATR,
                             float(f.FakeSpec().tick_size) * config.S1_STOP_BUFFER_TICKS)
        self.assertGreater(abs(result.stop_price - poc), default_buffer)

    def test_falls_back_to_the_value_edge_when_no_lvn_exists_between_poc_and_edge(self):
        """A single dense spike has no LVNs at all - _compute_stop must not crash,
        it must fall back to the value-area edge."""
        import types
        empty_levels = types.SimpleNamespace(
            lvns=[], vah=101.0, val=99.0,
            nearest_lvn_beyond=lambda price, direction, outer_bound=None: None,
        )
        profile = types.SimpleNamespace(high=105.0, low=95.0)
        # floor_atr small enough that it does not dominate here - the floor's own
        # arithmetic is covered separately below.
        stop, reference = poc_rotation._compute_stop(
            empty_levels, profile, poc=100.0, direction="SELL",
            buffer=0.05, floor_atr=0.05, atr=2.5)
        self.assertEqual(reference, "value_edge_fallback")
        self.assertAlmostEqual(stop, 101.0 + 0.05, places=9)

    def test_floor_applies_when_the_nearest_lvn_sits_very_close_to_the_poc(self):
        """An LVN a few ticks past the POC must not produce a stop that tight -
        the volatility floor must win over the raw LVN-plus-buffer distance."""
        import types
        node = types.SimpleNamespace(low_price=100.05, high_price=100.10)
        close_levels = types.SimpleNamespace(
            lvns=[node], vah=101.0, val=99.0,
            nearest_lvn_beyond=lambda price, direction, outer_bound=None: node,
        )
        profile = types.SimpleNamespace(high=105.0, low=95.0)
        stop, reference = poc_rotation._compute_stop(
            close_levels, profile, poc=100.0, direction="SELL",
            buffer=0.05, floor_atr=0.6, atr=2.5)
        self.assertEqual(reference, "lvn")
        # Floor is 0.6 * 2.5 = 1.5, far wider than the LVN's own +0.05 buffer.
        self.assertAlmostEqual(stop, 100.0 + 1.5, places=9)

    def test_buy_side_searches_below_the_poc(self):
        """Direction symmetry: a BUY's stop searches BELOW the POC, not above."""
        import types
        seen = {}

        def spy(price, direction, outer_bound=None):
            seen["direction"] = direction
            seen["outer_bound"] = outer_bound
            return None

        levels_ns = types.SimpleNamespace(lvns=[], vah=101.0, val=99.0,
                                          nearest_lvn_beyond=spy)
        profile = types.SimpleNamespace(high=105.0, low=95.0)
        poc_rotation._compute_stop(levels_ns, profile, poc=100.0, direction="BUY",
                                   buffer=0.05, floor_atr=0.6, atr=2.5)
        self.assertEqual(seen["direction"], "BELOW")
        self.assertEqual(seen["outer_bound"], 95.0)


class BinDeltaAttributeTests(unittest.TestCase):
    """`bin_delta_normalized`: the raw measurement Phase 3's delta gate would sweep.

    Recorded unconditionally on S1-POC and S2-VAR candidates (config.py's Phase 3
    worklist), computed the identical way gates.checks.delta_opposed would gate on it -
    same bin_index/delta_at/volume_at calls - but stored as a value rather than a
    pass/fail, so the ablation can be swept from stored rows instead of a fresh replay
    per threshold. `boxy_session`'s core candles are neutral (buy_fraction=0.5) by
    default, so every existing fixture implicitly asserts 0.0; `buy_fraction=0.8`
    skews every core candle identically, so the bin the POC/value-bound falls in
    inherits that same skew exactly: 2*0.8 - 1 = 0.6.
    """

    def test_neutral_session_reads_zero_at_the_poc(self):
        prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0, tail=0.25)
        _, levels, _ = f.make_profile("TESTUSDT", prior, PREV_START, PREV_START + DAY,
                                      bin_size=0.05)
        open_price = levels.vah + levels.value_width * 0.30
        poc = levels.poc_price
        prices = [open_price] * 30
        step = (poc - open_price) / 60
        prices += [open_price + step * i for i in range(60)]
        current = walk(CUR_START, prices)
        ts = current[-1].open_time + 60_000
        for _ in range(15):
            current.append(f.candle(ts, poc, poc + 0.30, poc - 0.60, poc + 0.25,
                                    12.0, 0.70))
            ts += 60_000

        ctx = build_ctx(prior, current)
        result = poc_rotation.detect(ctx)
        self.assertFalse(result.is_rejection)
        self.assertAlmostEqual(result.attributes["bin_delta_normalized"], 0.0,
                               delta=1e-9)
        self.assertAlmostEqual(result.attributes["multi_bin_delta_normalized"], 0.0,
                               delta=1e-9,
                               msg="a uniformly neutral session must read neutral "
                                   "over the window too, not just at one bin")
        # The two remaining opinion-gate inputs, checked against the same levels
        # object the gate itself would read - catches a wrong-level or wrong-price
        # wiring bug even though the HVN geometry itself is tested elsewhere.
        self.assertEqual(
            result.attributes["stop_inside_hvn"],
            ctx.prior_levels.hvn_containing(result.stop_price) is not None)
        lo, hi = sorted((result.entry_price, result.target_price))
        self.assertEqual(
            result.attributes["target_behind_hvn"],
            any(lo < node.center_price < hi for node in ctx.prior_levels.hvns))
        # PLAN follow-up (finding 32): same wiring check as the HVN fields above -
        # recompute independently against the same levels object the candidate
        # was built from, catching a wrong-level wiring bug.
        self.assertEqual(
            result.attributes["vwap_zscore_at_level"],
            vwap_zscore(ctx.prior_levels, result.level_price))
        self.assertEqual(
            result.attributes["weekly_poc_distance_atr"],
            weekly_poc_distance_atr(ctx.weekly_levels, result.level_price, ctx.atr))

    def test_skewed_session_reads_the_skew_at_the_poc(self):
        prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0, tail=0.25,
                               buy_fraction=0.8)
        _, levels, _ = f.make_profile("TESTUSDT", prior, PREV_START, PREV_START + DAY,
                                      bin_size=0.05)
        open_price = levels.vah + levels.value_width * 0.30
        poc = levels.poc_price
        prices = [open_price] * 30
        step = (poc - open_price) / 60
        prices += [open_price + step * i for i in range(60)]
        current = walk(CUR_START, prices)
        ts = current[-1].open_time + 60_000
        for _ in range(15):
            current.append(f.candle(ts, poc, poc + 0.30, poc - 0.60, poc + 0.25,
                                    12.0, 0.70))
            ts += 60_000

        result = poc_rotation.detect(build_ctx(prior, current))
        self.assertFalse(result.is_rejection)
        self.assertAlmostEqual(result.attributes["bin_delta_normalized"], 0.6,
                               delta=1e-6)
        self.assertAlmostEqual(result.attributes["multi_bin_delta_normalized"], 0.6,
                               delta=1e-6,
                               msg="a uniformly skewed session must carry the same "
                                   "skew over the window as at the single bin")

    def test_s2_var_neutral_session_reads_zero_at_the_value_bound(self):
        prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0, tail=0.25)
        _, levels, _ = f.make_profile("TESTUSDT", prior, PREV_START, PREV_START + DAY,
                                      bin_size=0.05)
        poc, val = levels.poc_price, levels.val
        candles = walk(CUR_START, [poc] * 60, volume=20.0)
        ts = candles[-1].open_time + 60_000
        below = val - 1.2
        for _ in range(30):
            candles.append(f.candle(ts, below, below + 0.05, below - 0.05, below,
                                    1.0, 0.5))
            ts += 60_000
        for _ in range(30):
            candles.append(f.candle(ts, poc, poc + 0.05, poc - 0.05, poc, 20.0, 0.6))
            ts += 60_000

        ctx = build_ctx(prior, candles)
        patcher = patch.object(config, "ACCEPT_DISCRIMINATOR", "volume_rate")
        patcher.start()
        self.addCleanup(patcher.stop)
        result = va_reversion.detect(ctx)
        self.assertFalse(result.is_rejection,
                         f"got {getattr(result, 'reason', None)} "
                         f"{getattr(result, 'detail', '')}")
        self.assertAlmostEqual(result.attributes["bin_delta_normalized"], 0.0,
                               delta=1e-9)
        self.assertAlmostEqual(result.attributes["multi_bin_delta_normalized"], 0.0,
                               delta=1e-9)
        self.assertEqual(
            result.attributes["stop_inside_hvn"],
            ctx.prior_levels.hvn_containing(result.stop_price) is not None)
        lo, hi = sorted((result.entry_price, result.target_price))
        self.assertEqual(
            result.attributes["target_behind_hvn"],
            any(lo < node.center_price < hi for node in ctx.prior_levels.hvns))
        self.assertEqual(
            result.attributes["vwap_zscore_at_level"],
            vwap_zscore(ctx.prior_levels, result.level_price))
        self.assertEqual(
            result.attributes["weekly_poc_distance_atr"],
            weekly_poc_distance_atr(ctx.weekly_levels, result.level_price, ctx.atr))

    def test_s2_var_skewed_session_reads_the_skew_at_the_value_bound(self):
        prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0, tail=0.25,
                               buy_fraction=0.8)
        _, levels, _ = f.make_profile("TESTUSDT", prior, PREV_START, PREV_START + DAY,
                                      bin_size=0.05)
        poc, val = levels.poc_price, levels.val
        candles = walk(CUR_START, [poc] * 60, volume=20.0)
        ts = candles[-1].open_time + 60_000
        below = val - 1.2
        for _ in range(30):
            candles.append(f.candle(ts, below, below + 0.05, below - 0.05, below,
                                    1.0, 0.5))
            ts += 60_000
        for _ in range(30):
            candles.append(f.candle(ts, poc, poc + 0.05, poc - 0.05, poc, 20.0, 0.6))
            ts += 60_000

        ctx = build_ctx(prior, candles)
        patcher = patch.object(config, "ACCEPT_DISCRIMINATOR", "volume_rate")
        patcher.start()
        self.addCleanup(patcher.stop)
        result = va_reversion.detect(ctx)
        self.assertFalse(result.is_rejection,
                         f"got {getattr(result, 'reason', None)} "
                         f"{getattr(result, 'detail', '')}")
        self.assertAlmostEqual(result.attributes["bin_delta_normalized"], 0.6,
                               delta=1e-6)
        self.assertAlmostEqual(result.attributes["multi_bin_delta_normalized"], 0.6,
                               delta=1e-6)

    def test_a_populated_weekly_bundle_actually_flows_through_to_the_candidate(self):
        """Every other test in this class builds `ctx` with `weekly_levels=None`
        (build_ctx's default), so the wiring checks above only prove None==None.
        This is the one case that proves the field genuinely carries a real
        value end to end, not just that both sides of the equality are absent."""
        prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0, tail=0.25)
        _, levels, _ = f.make_profile("TESTUSDT", prior, PREV_START, PREV_START + DAY,
                                      bin_size=0.05)
        open_price = levels.vah + levels.value_width * 0.30
        poc = levels.poc_price
        prices = [open_price] * 30
        step = (poc - open_price) / 60
        prices += [open_price + step * i for i in range(60)]
        current = walk(CUR_START, prices)
        ts = current[-1].open_time + 60_000
        for _ in range(15):
            current.append(f.candle(ts, poc, poc + 0.30, poc - 0.60, poc + 0.25,
                                    12.0, 0.70))
            ts += 60_000

        weekly_prior = f.boxy_session(PREV_START, center=poc - 5.0, half_width=1.0,
                                      tail=0.25)
        _, weekly_levels, _ = f.make_profile(
            "TESTUSDT", weekly_prior, PREV_START, PREV_START + DAY, bin_size=0.05)
        self.assertNotAlmostEqual(weekly_levels.poc_price, poc, places=2,
                                  msg="the weekly POC must sit somewhere DIFFERENT "
                                      "from the daily POC, or this test cannot "
                                      "distinguish 'wired correctly' from 'wired "
                                      "to the wrong levels object'")

        ctx = build_ctx(prior, current, weekly_levels=weekly_levels)
        result = poc_rotation.detect(ctx)
        self.assertFalse(result.is_rejection)
        expected = weekly_poc_distance_atr(weekly_levels, result.level_price, ctx.atr)
        self.assertIsNotNone(expected)
        self.assertAlmostEqual(result.attributes["weekly_poc_distance_atr"],
                               expected, places=9)


class DeltaOpposedMultibinGateTests(unittest.TestCase):
    """The gate half of PLAN item 6: config-gated, direction-aware, opinion-default-off.

    Unlike the attribute tests above (which check the setup modules record the right
    number), this checks gates.checks.delta_opposed_multibin actually acts on it -
    the two can drift apart if the gate reads something other than what gets stored.
    """

    def _ctx(self, profile):
        import types
        return types.SimpleNamespace(prior_profile=profile, symbol="TESTUSDT")

    def _profile(self, bins):
        return builder.Profile(
            symbol="TESTUSDT", window_start=0, window_end=86_400_000,
            as_of=86_400_000, bin_size=1.0,
            volume={i: v for i, (v, _) in bins.items()},
            delta={i: d for i, (_, d) in bins.items()},
        )

    def test_disabled_by_default(self):
        from gates import checks
        from setups.base import Candidate

        profile = self._profile({100: (10.0, 10.0)})
        candidate = Candidate(setup="S1-POC", symbol="TESTUSDT", direction="BUY",
                              entry_price=100.0, stop_price=99.0, target_price=102.0,
                              level_price=100.5)
        self.assertIsNone(checks.delta_opposed_multibin(candidate, self._ctx(profile)),
                          "GATE_DELTA_OPPOSED_MULTIBIN_ENABLED defaults False")

    def test_rejects_flow_that_opposes_the_direction_once_enabled(self):
        from gates import checks
        from setups.base import Candidate, RejectReason

        # Window (default 2) all one-sided AGAINST a BUY: heavy selling at the level.
        profile = self._profile({98: (10.0, -10.0), 99: (10.0, -10.0),
                                 100: (10.0, -10.0), 101: (10.0, -10.0),
                                 102: (10.0, -10.0)})
        candidate = Candidate(setup="S1-POC", symbol="TESTUSDT", direction="BUY",
                              entry_price=100.0, stop_price=99.0, target_price=102.0,
                              level_price=100.5)
        with patch.object(config, "GATE_DELTA_OPPOSED_MULTIBIN_ENABLED", True):
            rejection = checks.delta_opposed_multibin(candidate, self._ctx(profile))
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection.reason, RejectReason.DELTA_OPPOSED_MULTIBIN)

    def test_passes_flow_that_agrees_with_the_direction(self):
        from gates import checks
        from setups.base import Candidate

        profile = self._profile({98: (10.0, 10.0), 99: (10.0, 10.0),
                                 100: (10.0, 10.0), 101: (10.0, 10.0),
                                 102: (10.0, 10.0)})
        candidate = Candidate(setup="S1-POC", symbol="TESTUSDT", direction="BUY",
                              entry_price=100.0, stop_price=99.0, target_price=102.0,
                              level_price=100.5)
        with patch.object(config, "GATE_DELTA_OPPOSED_MULTIBIN_ENABLED", True):
            rejection = checks.delta_opposed_multibin(candidate, self._ctx(profile))
        self.assertIsNone(rejection)


class S2LvnStopModeTests(unittest.TestCase):
    """config.S2_STOP_MODE="lvn": stop past a thin area beyond the excursion extreme.

    Default ("extreme") is unaffected - these tests are about the new arm and the
    unit-level fallback/floor arithmetic, not a regression check on the default
    (S2ReversionTests/S2ReversionDurationTests already cover that path unchanged).
    """

    def setUp(self):
        self.mode_patcher = patch.object(config, "S2_STOP_MODE", "lvn")
        self.mode_patcher.start()
        self.addCleanup(self.mode_patcher.stop)
        self.discriminator_patcher = patch.object(config, "ACCEPT_DISCRIMINATOR",
                                                   "volume_rate")
        self.discriminator_patcher.start()
        self.addCleanup(self.discriminator_patcher.stop)
        self.prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0,
                                    tail=0.25)
        _, self.levels, _ = f.make_profile("TESTUSDT", self.prior, PREV_START,
                                           PREV_START + DAY, bin_size=0.05)

    def _fire(self):
        poc, val = self.levels.poc_price, self.levels.val
        candles = walk(CUR_START, [poc] * 60, volume=20.0)
        ts = candles[-1].open_time + 60_000
        below = val - 1.2
        for _ in range(30):
            candles.append(f.candle(ts, below, below + 0.05, below - 0.05, below,
                                    1.0, 0.5))
            ts += 60_000
        for _ in range(30):
            candles.append(f.candle(ts, poc, poc + 0.05, poc - 0.05, poc, 20.0, 0.6))
            ts += 60_000
        return va_reversion.detect(build_ctx(self.prior, candles))

    def test_stop_reference_is_always_present_and_valid(self):
        result = self._fire()
        self.assertFalse(result.is_rejection,
                         f"got {getattr(result, 'reason', None)} "
                         f"{getattr(result, 'detail', '')}")
        self.assertIn(result.attributes["stop_reference"],
                      ("lvn", "extreme_buffer_fallback"))
        self.assertEqual(result.attributes["stop_mode"], "lvn")

    def test_default_mode_is_extreme(self):
        self.mode_patcher.stop()
        self.assertEqual(config.S2_STOP_MODE, "extreme")
        self.mode_patcher.start()

    def test_falls_back_to_extreme_buffer_when_no_lvn_qualifies(self):
        """Same contract as S1's fallback: 'lvn' must never be tighter than the
        setup's own existing default when nothing thin exists beyond the extreme."""
        import types
        empty_levels = types.SimpleNamespace(
            lvns=[],
            nearest_lvn_beyond=lambda price, direction, outer_bound=None: None,
        )
        profile = types.SimpleNamespace(high=105.0, low=95.0)
        # floor_atr small enough not to dominate here - the floor's own arithmetic
        # is covered separately below.
        stop, reference = va_reversion._compute_stop(
            empty_levels, profile, extreme=98.0, direction="BUY",
            buffer=0.10, floor_atr=0.01, atr=2.5)
        self.assertEqual(reference, "extreme_buffer_fallback")
        self.assertAlmostEqual(stop, 98.0 - 0.10, places=9)

    def test_floor_applies_when_the_nearest_lvn_sits_very_close_to_the_extreme(self):
        import types
        node = types.SimpleNamespace(low_price=96.90, high_price=96.95)
        close_levels = types.SimpleNamespace(
            lvns=[node],
            nearest_lvn_beyond=lambda price, direction, outer_bound=None: node,
        )
        profile = types.SimpleNamespace(high=105.0, low=95.0)
        stop, reference = va_reversion._compute_stop(
            close_levels, profile, extreme=97.0, direction="BUY",
            buffer=0.05, floor_atr=0.6, atr=2.5)
        self.assertEqual(reference, "lvn")
        # Floor is 0.6 * 2.5 = 1.5 below the extreme, far past the LVN's own -0.05.
        self.assertAlmostEqual(stop, 97.0 - 1.5, places=9)


class S2ReversionTests(unittest.TestCase):
    """Open inside value, push below VAL on thin volume, close back inside -> BUY.

    PINNED TO THE VOLUME-RATE ARM. These fixtures vary `outside_volume` to express "thin"
    and "heavy", which is meaningful only when volume rate is the discriminator. The
    default is now duration (Phase 0: volume rate ~0.50 AUC with distance held fixed,
    duration 0.61), and under it a 30-minute excursion is short regardless of how much
    volume it carried - so `outside_volume=400` produces REJECTED, not FAILED_AUCTION,
    and these assertions no longer describe the default path.

    Kept and pinned because the volume-rate branch is the ablation control. Duration-arm
    equivalents are in S2ReversionDurationTests.
    """

    def setUp(self):
        patcher = patch.object(config, "ACCEPT_DISCRIMINATOR", "volume_rate")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0,
                                    tail=0.25)
        _, self.levels, _ = f.make_profile("TESTUSDT", self.prior, PREV_START,
                                           PREV_START + DAY, bin_size=0.05)

    def _excursion(self, outside_volume, back_inside=True):
        poc, val = self.levels.poc_price, self.levels.val
        candles = walk(CUR_START, [poc] * 60, volume=20.0)
        ts = candles[-1].open_time + 60_000
        # Excursion below VAL, at the configured volume.
        below = val - 1.2
        for _ in range(30):
            candles.append(f.candle(ts, below, below + 0.05, below - 0.05, below,
                                    outside_volume, 0.5))
            ts += 60_000
        if back_inside:
            for _ in range(30):
                candles.append(f.candle(ts, poc, poc + 0.05, poc - 0.05, poc,
                                        20.0, 0.6))
                ts += 60_000
        return candles

    def test_fires_buy_on_rejected_excursion_below_value(self):
        ctx = build_ctx(self.prior, self._excursion(outside_volume=1.0))
        self.assertTrue(ctx.open_relationship.inside_value)
        result = va_reversion.detect(ctx)
        self.assertFalse(result.is_rejection,
                         f"got {getattr(result, 'reason', None)} "
                         f"{getattr(result, 'detail', '')}")
        self.assertEqual(result.direction, "BUY")
        self.assertEqual(result.level_kind, "VAL")
        self.assertLess(result.stop_price, result.entry_price)

    def test_declines_when_excursion_was_accepted(self):
        """Heavy volume outside value is never S2's trade.

        Which refusal applies depends on whether price is still out there. Still
        outside -> ACCEPTANCE_CONTRADICTS, it is S3's move. Already reclaimed ->
        FAILED_AUCTION_NOT_TRADED: real business was done at the new prices and then
        undone, which is a different and stronger event than the thin spike S2 fades,
        and is out of v1 scope.
        """
        ctx = build_ctx(self.prior, self._excursion(outside_volume=400.0))
        result = va_reversion.detect(ctx)
        self.assertTrue(result.is_rejection)
        self.assertIn(result.reason.value,
                      ("ACCEPTANCE_CONTRADICTS", "ACCEPTANCE_PENDING",
                       "FAILED_AUCTION_NOT_TRADED"))

    def test_heavy_reclaimed_excursion_is_a_failed_auction(self):
        """The specific classification, asserted so it cannot silently regress."""
        ctx = build_ctx(self.prior, self._excursion(outside_volume=400.0,
                                                   back_inside=True))
        result = va_reversion.detect(ctx)
        self.assertTrue(result.is_rejection)
        self.assertEqual(result.reason.value, "FAILED_AUCTION_NOT_TRADED")

    def test_declines_while_price_is_still_outside(self):
        ctx = build_ctx(self.prior, self._excursion(outside_volume=1.0,
                                                   back_inside=False))
        result = va_reversion.detect(ctx)
        self.assertTrue(result.is_rejection)


class S2ReversionDurationTests(unittest.TestCase):
    """The DEFAULT arm: acceptance decided by TIME outside value.

    The discriminator here is how long price held outside value, not how much volume it
    carried. Phase 0 measured the continuation rate rising monotonically with duration
    across every band - 28.4% under an hour to 73.1% past eight - while the volume-rate
    ratio it replaced sat at ~0.50 once excursion distance was held fixed.

    So the fixtures vary MINUTES OUTSIDE, holding volume constant, which is the exact
    mirror of the volume-rate class above. Both excursions below carry identical volume;
    only their length differs, and that alone must move the verdict. If it does not, the
    new discriminator is as inert as the old one and these tests are the only thing that
    would say so.

    Durations are expressed in confirmation candles and converted to minutes, so a change
    to CONFIRM_INTERVAL cannot quietly turn a "long" excursion into a short one.
    """

    def setUp(self):
        patcher = patch.object(config, "ACCEPT_DISCRIMINATOR", "duration")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0,
                                    tail=0.25)
        _, self.levels, _ = f.make_profile("TESTUSDT", self.prior, PREV_START,
                                           PREV_START + DAY, bin_size=0.05)
        self.confirm_minutes = klines_mod.INTERVAL_MS[config.CONFIRM_INTERVAL] // 60_000

    def _excursion(self, confirm_candles_outside, back_inside=True, volume=20.0):
        """An excursion lasting `confirm_candles_outside` confirmation candles.

        Volume is deliberately the SAME for inside and outside candles, so the volume-rate
        ratio is ~1.0 throughout and cannot be what moves the verdict. Whatever these
        tests measure, it is not volume.
        """
        poc, val = self.levels.poc_price, self.levels.val
        candles = walk(CUR_START, [poc] * 120, volume=volume)
        ts = candles[-1].open_time + 60_000
        below = val - 1.2
        # +1 so the run is unambiguously at least this many CLOSED confirm candles after
        # resampling, rather than landing exactly on a boundary.
        minutes = (confirm_candles_outside + 1) * self.confirm_minutes
        for _ in range(minutes):
            candles.append(f.candle(ts, below, below + 0.05, below - 0.05, below,
                                    volume, 0.5))
            ts += 60_000
        if back_inside:
            for _ in range(2 * self.confirm_minutes):
                candles.append(f.candle(ts, poc, poc + 0.05, poc - 0.05, poc,
                                        volume, 0.6))
                ts += 60_000
        return candles

    def test_brief_excursion_that_returned_is_rejected_and_fires_s2(self):
        ctx = build_ctx(self.prior,
                        self._excursion(config.REJECT_MAX_CANDLES_OUTSIDE - 2))
        result = va_reversion.detect(ctx)
        self.assertFalse(result.is_rejection,
                         f"got {getattr(result, 'reason', None)} "
                         f"{getattr(result, 'detail', '')}")
        self.assertEqual(result.direction, "BUY")
        self.assertEqual(result.level_kind, "VAL")

    def test_long_excursion_that_returned_is_a_failed_auction_not_s2(self):
        """The classification that duration reads better than volume rate.

        Price held outside value for hours and then came back. That is real business done
        at the new prices and then undone - a different, stronger event than the brief
        spike S2 fades, and out of v1 scope. Volume is identical to the brief case.
        """
        ctx = build_ctx(self.prior,
                        self._excursion(config.ACCEPT_MIN_CANDLES_OUTSIDE + 2))
        result = va_reversion.detect(ctx)
        self.assertTrue(result.is_rejection)
        self.assertEqual(result.reason.value, "FAILED_AUCTION_NOT_TRADED")

    def test_duration_alone_flips_the_verdict_at_identical_volume(self):
        """THE regression test for the new discriminator being inert.

        Two excursions, same volume everywhere, differing only in length. If the verdict
        does not change, duration is not deciding anything - which is precisely the failure
        the volume-rate ratio turned out to have, and it went unnoticed through three
        separate bug fixes to this module because nothing asserted this.
        """
        brief = va_reversion.detect(build_ctx(
            self.prior, self._excursion(config.REJECT_MAX_CANDLES_OUTSIDE - 2)))
        long_run = va_reversion.detect(build_ctx(
            self.prior, self._excursion(config.ACCEPT_MIN_CANDLES_OUTSIDE + 2)))
        self.assertFalse(brief.is_rejection)
        self.assertTrue(long_run.is_rejection)

    def test_no_volume_baseline_does_not_block_a_duration_verdict(self):
        """Duration has no denominator, so it needs no baseline.

        Under the volume-rate arm a session with no independent baseline is refused, and
        correctly: the ratio computes to 0.0, which reads as the thinnest possible
        excursion and would arm S2 on a session that has said nothing. Duration cannot be
        contaminated that way - there is nothing to divide - so refusing here would discard
        a sound reading. This is the one place the two arms must legitimately differ.
        """
        poc, val = self.levels.poc_price, self.levels.val
        below = val - 1.2
        candles = []
        ts = CUR_START
        # Opens OUTSIDE value and never trades inside: no in-session baseline exists.
        minutes = (config.REJECT_MAX_CANDLES_OUTSIDE - 2 + 1) * self.confirm_minutes
        for _ in range(minutes):
            candles.append(f.candle(ts, below, below + 0.05, below - 0.05, below,
                                    20.0, 0.5))
            ts += 60_000
        for _ in range(2 * self.confirm_minutes):
            candles.append(f.candle(ts, poc, poc + 0.05, poc - 0.05, poc, 20.0, 0.6))
            ts += 60_000

        confirm = klines_mod.resample(candles, config.CONFIRM_INTERVAL, "1m")
        verdict = acceptance_mod.evaluate(confirm, self.levels, atr=2.5, prior_rate=0.0)
        self.assertEqual(verdict.baseline_source, "NONE")
        self.assertNotEqual(verdict.verdict, acceptance_mod.NO_BASELINE)

        with patch.object(config, "ACCEPT_DISCRIMINATOR", "volume_rate"):
            same = acceptance_mod.evaluate(confirm, self.levels, atr=2.5,
                                          prior_rate=0.0)
        self.assertEqual(same.verdict, acceptance_mod.NO_BASELINE)


class MutualExclusivityTests(unittest.TestCase):
    """S2 and S3 must never both produce a candidate from the same data.

    This is the property the whole state machine rests on. If it can be violated, the
    bot can hold two opposite candidates on one symbol and the arbiter - not the
    market - decides the trade.
    """

    def setUp(self):
        self.prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0,
                                    tail=0.25)
        _, self.levels, _ = f.make_profile("TESTUSDT", self.prior, PREV_START,
                                           PREV_START + DAY, bin_size=0.05)

    def _scenario(self, outside_volume, back_inside, pullback):
        poc, val = self.levels.poc_price, self.levels.val
        candles = walk(CUR_START, [poc] * 60, volume=20.0)
        ts = candles[-1].open_time + 60_000
        below = val - 1.5
        for _ in range(45):
            candles.append(f.candle(ts, below, below + 0.05, below - 0.05, below,
                                    outside_volume, 0.35))
            ts += 60_000
        if pullback:
            for price in (below + 0.5, below + 0.6, below - 0.2, below - 0.8):
                candles.append(f.candle(ts, price, price + 0.05, price - 0.05, price,
                                        outside_volume, 0.35))
                ts += 60_000
        if back_inside:
            for _ in range(20):
                candles.append(f.candle(ts, poc, poc + 0.05, poc - 0.05, poc,
                                        20.0, 0.6))
                ts += 60_000
        return build_ctx(self.prior, candles)

    def test_never_both(self):
        for outside_volume in (0.5, 5.0, 50.0, 500.0):
            for back_inside in (True, False):
                for pullback in (True, False):
                    ctx = self._scenario(outside_volume, back_inside, pullback)
                    s2 = va_reversion.detect(ctx)
                    s3 = va_breakout.detect(ctx)
                    both = (not s2.is_rejection) and (not s3.is_rejection)
                    self.assertFalse(
                        both,
                        f"S2 and S3 both fired: vol={outside_volume} "
                        f"back_inside={back_inside} pullback={pullback}")


class SetupReachabilityTests(unittest.TestCase):
    """EVERY setup must be able to fire on a scenario built to suit it.

    THE BUG CLASS THIS EXISTS FOR. A setup that can NEVER produce a candidate passes
    every correctness test trivially - no wrong candidate is ever emitted, no geometry
    is ever violated, no gate is ever mis-applied - and its rejections read as ordinary
    market states in the journal. S3-BRK was in exactly that condition: its continuation
    test compared the signal candle's close against a maximum that INCLUDED that
    candle's own high, so `close > extreme` was unsatisfiable, and when the candle did
    make a new high it became the extreme and the structure returned None instead.
    Verified dead at 0 of 30,000 random windows, while two live scans reported
    NO_CONTINUATION and looked entirely reasonable.

    Correctness tests cannot catch this. Only asking "can this fire at all?" can.
    """

    def setUp(self):
        self.prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0,
                                    tail=0.25)
        _, self.levels, _ = f.make_profile("TESTUSDT", self.prior, PREV_START,
                                           PREV_START + DAY, bin_size=0.05)

    def _session(self, steps):
        ts = CUR_START
        candles = []
        for price, volume, count in steps:
            for _ in range(count):
                candles.append(f.candle(ts, price, price + 0.05, price - 0.05,
                                        price, volume, 0.6))
                ts += 60_000
        return build_ctx(self.prior, candles)

    def test_s3_breakout_can_fire(self):
        """Accepted break, shallow pullback, continuation past the pre-pullback high."""
        vah, poc = self.levels.vah, self.levels.poc_price
        brk = vah + 1.2
        ctx = self._session([
            (poc, 20.0, 40),          # in-value baseline for the acceptance denominator
            (brk, 300.0, 300),        # accepted break; drags the developing POC up
            (brk + 0.8, 300.0, 40),   # the extreme
            (brk + 0.3, 250.0, 25),   # shallow pullback, still outside value
            (brk + 1.4, 300.0, 15),   # continuation
        ])
        result = va_breakout.detect(ctx)
        self.assertFalse(
            result.is_rejection,
            f"S3 must be able to fire on an ideal breakout; got "
            f"{getattr(result, 'reason', None)} - {getattr(result, 'detail', '')}")
        self.assertEqual(result.direction, "BUY")
        self.assertLess(result.stop_price, result.entry_price)
        self.assertGreater(result.target_price, result.entry_price)

    def test_s3_breakout_can_fire_downward(self):
        """The mirror case - a symmetric bug would hide in one direction only."""
        val, poc = self.levels.val, self.levels.poc_price
        brk = val - 1.2
        ctx = self._session([
            (poc, 20.0, 40),
            (brk, 300.0, 300),
            (brk - 0.8, 300.0, 40),
            (brk - 0.3, 250.0, 25),
            (brk - 1.4, 300.0, 15),
        ])
        result = va_breakout.detect(ctx)
        self.assertFalse(
            result.is_rejection,
            f"S3 must fire on a downward break too; got "
            f"{getattr(result, 'reason', None)} - {getattr(result, 'detail', '')}")
        self.assertEqual(result.direction, "SELL")
        self.assertGreater(result.stop_price, result.entry_price)
        self.assertLess(result.target_price, result.entry_price)

    def test_s2_reversion_can_fire(self):
        """Thin excursion below value, then a close back inside."""
        poc, val = self.levels.poc_price, self.levels.val
        ctx = self._session([
            (poc, 20.0, 60),
            (val - 1.5, 1.0, 45),      # thin excursion - rejected, not accepted
            (poc, 20.0, 20),           # back inside value
        ])
        result = va_reversion.detect(ctx)
        self.assertFalse(
            result.is_rejection,
            f"S2 must be able to fire on an ideal rejection; got "
            f"{getattr(result, 'reason', None)} - {getattr(result, 'detail', '')}")
        self.assertEqual(result.direction, "BUY")

    def test_no_setup_is_structurally_dead(self):
        """A blunt aggregate: across a broad sweep, each setup fires at least once.

        S1 needs the session to OPEN outside value, which the scenarios above cannot
        express, so it is swept separately here rather than given an ideal fixture.
        """
        from setups import lvn_rejection, poc_rotation

        fired = set()
        poc = self.levels.poc_price
        vah, val = self.levels.vah, self.levels.val

        for opened_at in (vah + 0.30, val - 0.30):
            for approach in (poc, poc + 0.02, poc - 0.02):
                ts = CUR_START
                candles = []
                # Open away from value, then travel to the POC and reject off it.
                for _ in range(40):
                    candles.append(f.candle(ts, opened_at, opened_at + 0.05,
                                            opened_at - 0.05, opened_at, 20.0, 0.5))
                    ts += 60_000
                rising = opened_at < poc
                for _ in range(20):
                    high = approach + (0.25 if rising else 0.05)
                    low = approach - (0.05 if rising else 0.25)
                    close = approach - 0.18 if rising else approach + 0.18
                    candles.append(f.candle(ts, approach, high, low, close, 20.0,
                                            0.2 if rising else 0.8))
                    ts += 60_000
                ctx = build_ctx(self.prior, candles)
                for name, detector in (("S1-POC", poc_rotation.detect),
                                       ("S1-LVN", lvn_rejection.detect)):
                    if not detector(ctx).is_rejection:
                        fired.add(name)

        self.assertIn("S1-POC", fired,
                      "S1-POC never fired across the whole sweep - it may be dead")


class S3AcceptedEntryTests(unittest.TestCase):
    """S3_ENTRY_MODE="accepted" - the experiment CALIBRATION.md finding 15 motivates.

    The matched-random control found S3's "confirmed" entries score like twins entered
    AFTER them, never like twins entered BEFORE - waiting for a pullback and a new high
    past it arrives after the part of the move that pays. "accepted" tests entering the
    bar acceptance itself first confirms, before any pullback exists.

    These tests pin the actual behavioural difference - fires without a pullback, stops
    off the value bound rather than a pullback extreme - and a direct regression check
    that leaving S3_ENTRY_MODE at its default reproduces "confirmed" exactly, since the
    refactor that added this mode touched the entire body of va_breakout.detect().
    """

    def setUp(self):
        self.prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0,
                                    tail=0.25)
        _, self.levels, _ = f.make_profile("TESTUSDT", self.prior, PREV_START,
                                           PREV_START + DAY, bin_size=0.05)
        patcher = patch.object(config, "S3_ENTRY_MODE", "accepted")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _session(self, steps):
        ts = CUR_START
        candles = []
        for price, volume, count in steps:
            for _ in range(count):
                candles.append(f.candle(ts, price, price + 0.05, price - 0.05,
                                        price, volume, 0.6))
                ts += 60_000
        return build_ctx(self.prior, candles)

    def test_fires_with_no_pullback_at_all(self):
        """THE key behavioural difference. A monotonic break with no retracement:
        `_confirmed_entry` would reject this NO_CONTINUATION ("no pullback has formed
        yet"), because _pullback_structure needs a completed extreme-then-retrace to
        even return a structure. "accepted" needs none of that.
        """
        vah, poc = self.levels.vah, self.levels.poc_price
        brk = vah + 1.2
        ctx = self._session([
            (poc, 20.0, 40),
            (brk, 300.0, 300),        # straight through acceptance, never pulls back
        ])
        result = va_breakout.detect(ctx)
        self.assertFalse(
            result.is_rejection,
            f"accepted mode must fire without a pullback; got "
            f"{getattr(result, 'reason', None)} - {getattr(result, 'detail', '')}")
        self.assertEqual(result.direction, "BUY")
        self.assertEqual(result.attributes["stop_reference"], "value_bound")

    def test_fires_with_no_pullback_downward(self):
        val, poc = self.levels.val, self.levels.poc_price
        brk = val - 1.2
        ctx = self._session([
            (poc, 20.0, 40),
            (brk, 300.0, 300),
        ])
        result = va_breakout.detect(ctx)
        self.assertFalse(result.is_rejection,
                         f"got {getattr(result, 'reason', None)} - "
                         f"{getattr(result, 'detail', '')}")
        self.assertEqual(result.direction, "SELL")

    def test_stop_is_derived_from_the_value_bound_not_a_pullback(self):
        """The stop must sit near VAH (buffer aside), not near wherever a retrace
        happened to end - because under this mode no retrace has happened yet.
        """
        vah, poc = self.levels.vah, self.levels.poc_price
        brk = vah + 1.2
        ctx = self._session([
            (poc, 20.0, 40),
            (brk, 300.0, 300),
        ])
        result = va_breakout.detect(ctx)
        self.assertFalse(result.is_rejection)
        # Buffer is at most a couple of ATR-fractions; the stop must be close to VAH,
        # not close to the break price 1.2 above it.
        self.assertLess(abs(result.stop_price - vah), 1.0)
        self.assertLess(result.stop_price, vah)

    def test_fires_earlier_than_confirmed_mode_on_identical_data(self):
        """The whole point of the mode, made concrete: given data where CONFIRMED mode
        needs a pullback-and-continuation cycle to fire at all, ACCEPTED mode already
        fired earlier in the same candle stream (or fires here while confirmed does
        not fire at all, since this fixture never pulls back).
        """
        vah, poc = self.levels.vah, self.levels.poc_price
        brk = vah + 1.2
        ctx = self._session([
            (poc, 20.0, 40),
            (brk, 300.0, 300),
        ])
        accepted_result = va_breakout.detect(ctx)
        self.assertFalse(accepted_result.is_rejection)

        with patch.object(config, "S3_ENTRY_MODE", "confirmed"):
            confirmed_result = va_breakout.detect(ctx)
        self.assertTrue(confirmed_result.is_rejection)
        self.assertEqual(confirmed_result.reason.value, "NO_CONTINUATION")

    def test_lvn_widening_still_applies_to_the_value_bound_stop(self):
        """The LVN-widen rule is about thin liquidity, not about which mode is active -
        it must still fire when the value-bound stop happens to land inside one.
        """
        vah, poc = self.levels.vah, self.levels.poc_price
        brk = vah + 1.2
        ctx = self._session([
            (poc, 20.0, 40),
            (brk, 300.0, 300),
        ])
        if not ctx.prior_levels.lvns:
            self.skipTest("fixture produced no LVNs to test widening against")
        result = va_breakout.detect(ctx)
        self.assertFalse(result.is_rejection)
        # Whether or not THIS fixture's bound happens to land inside an LVN, the
        # attribute must always be present and boolean - never silently omitted.
        self.assertIn("widened_past_lvn", result.attributes)
        self.assertIsInstance(result.attributes["widened_past_lvn"], bool)


class ConfirmedModeUnchangedTests(unittest.TestCase):
    """The refactor that added S3_ENTRY_MODE touched va_breakout.detect() end to end.

    This is the belt-and-suspenders check: with S3_ENTRY_MODE left at its default,
    every existing S3 assertion must produce EXACTLY the values it produced before the
    refactor - the same stop, the same target, the same attributes - not merely "still
    fires". A refactor that quietly changed the confirmed arm's numbers would corrupt
    it as the control for the very comparison it exists to support.
    """

    def setUp(self):
        self.prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0,
                                    tail=0.25)
        _, self.levels, _ = f.make_profile("TESTUSDT", self.prior, PREV_START,
                                           PREV_START + DAY, bin_size=0.05)

    def test_default_mode_is_confirmed(self):
        self.assertEqual(config.S3_ENTRY_MODE, "confirmed")

    def test_exact_geometry_matches_the_pre_refactor_fixture(self):
        """Reproduces test_s3_breakout_can_fire's fixture with hand-computed
        expectations, so a drift in the confirmed arm's arithmetic fails a value
        comparison rather than only a "did it fire" check.
        """
        vah, poc = self.levels.vah, self.levels.poc_price
        brk = vah + 1.2
        ts = CUR_START
        candles = []
        for price, volume, count in [
            (poc, 20.0, 40), (brk, 300.0, 300), (brk + 0.8, 300.0, 40),
            (brk + 0.3, 250.0, 25), (brk + 1.4, 300.0, 15),
        ]:
            for _ in range(count):
                candles.append(f.candle(ts, price, price + 0.05, price - 0.05,
                                        price, volume, 0.6))
                ts += 60_000
        ctx = build_ctx(self.prior, candles)
        result = va_breakout.detect(ctx)
        self.assertFalse(result.is_rejection)

        # pullback_extreme is the candle LOW of the retrace leg, not its close/price -
        # _pullback_structure reads .low for the BUY side. Fixture candles are built as
        # (price, price+0.05, price-0.05, price), so the retrace leg at price=brk+0.3
        # has low = brk+0.25. Hand-computed from the fixture's own construction rather
        # than re-deriving it from the production code path.
        expected_pullback_extreme = brk + 0.3 - 0.05
        buffer = max(ctx.atr * config.S3_STOP_BUFFER_ATR,
                    float(ctx.spec.tick_size) * config.S1_STOP_BUFFER_TICKS
                    if ctx.spec else 0.0)
        if not result.attributes.get("widened_past_lvn"):
            self.assertAlmostEqual(result.stop_price,
                                   expected_pullback_extreme - buffer, places=6)
        self.assertEqual(result.attributes["stop_reference"], "pullback_extreme")
        self.assertIn("pullback_extreme", result.attributes)
        self.assertIn("breakout_extreme", result.attributes)


class CandidateGeometryTests(unittest.TestCase):
    """EVERY candidate from EVERY setup must have coherent level geometry.

        BUY   stop < entry < target
        SELL  target < entry < stop

    A property, not a fixture assertion: it holds for any candidate any detector can
    ever produce, so it catches a sign error in a setup this test has never heard of.
    It matters because every risk measure in the system uses absolute distances - R is
    a magnitude - so an inverted level set yields a healthy positive R and passes every
    other gate. The venue then closes the position for nothing and journals it as an
    ordinary small loss rather than as a fault.

    Both directions are swept deliberately. Symmetric fixtures are exactly what hides
    a sign error, so a scenario is run above value AND below it.
    """

    def setUp(self):
        self.prior = f.boxy_session(PREV_START, center=100.0, half_width=1.0,
                                    tail=0.25)
        _, self.levels, _ = f.make_profile("TESTUSDT", self.prior, PREV_START,
                                           PREV_START + DAY, bin_size=0.05)

    def _assert_geometry(self, candidate, label):
        entry = candidate.entry_price
        stop = candidate.stop_price
        target = candidate.target_price

        self.assertGreater(entry, 0, f"{label}: entry must be positive")
        self.assertGreater(stop, 0, f"{label}: stop must be positive")
        self.assertGreater(target, 0, f"{label}: target must be positive")

        if candidate.direction == "BUY":
            self.assertLess(stop, entry,
                            f"{label} BUY: stop {stop:.8g} must be BELOW entry "
                            f"{entry:.8g}")
            self.assertGreater(target, entry,
                               f"{label} BUY: target {target:.8g} must be ABOVE entry "
                               f"{entry:.8g}")
        else:
            self.assertGreater(stop, entry,
                               f"{label} SELL: stop {stop:.8g} must be ABOVE entry "
                               f"{entry:.8g}")
            self.assertLess(target, entry,
                            f"{label} SELL: target {target:.8g} must be BELOW entry "
                            f"{entry:.8g}")

        self.assertGreater(candidate.risk_distance, 0, f"{label}: zero risk")
        self.assertGreater(candidate.r_multiple, 0, f"{label}: non-positive R")

    def _excursion(self, side, outside_volume, back_inside, pullback):
        """A session that leaves value on `side`, optionally returning or pulling back."""
        poc = self.levels.poc_price
        bound = self.levels.val if side == "BELOW" else self.levels.vah
        outside = bound - 1.5 if side == "BELOW" else bound + 1.5

        candles = walk(CUR_START, [poc] * 60, volume=20.0)
        ts = candles[-1].open_time + 60_000
        for _ in range(45):
            candles.append(f.candle(ts, outside, outside + 0.05, outside - 0.05,
                                    outside, outside_volume, 0.35))
            ts += 60_000
        if pullback:
            steps = ((0.5, 0.6, -0.2, -0.8) if side == "BELOW"
                     else (-0.5, -0.6, 0.2, 0.8))
            for delta in steps:
                price = outside + delta
                candles.append(f.candle(ts, price, price + 0.05, price - 0.05, price,
                                        outside_volume, 0.35))
                ts += 60_000
        if back_inside:
            for _ in range(20):
                candles.append(f.candle(ts, poc, poc + 0.05, poc - 0.05, poc,
                                        20.0, 0.6))
                ts += 60_000
        return build_ctx(self.prior, candles)

    def test_s2_and_s3_geometry_holds_in_both_directions(self):
        checked = 0
        for side in ("BELOW", "ABOVE"):
            for outside_volume in (0.5, 5.0, 50.0, 500.0):
                for back_inside in (True, False):
                    for pullback in (True, False):
                        ctx = self._excursion(side, outside_volume, back_inside,
                                              pullback)
                        for name, detector in (("S2-VAR", va_reversion.detect),
                                               ("S3-BRK", va_breakout.detect)):
                            result = detector(ctx)
                            if result.is_rejection:
                                continue
                            self._assert_geometry(
                                result,
                                f"{name} side={side} vol={outside_volume} "
                                f"back={back_inside} pull={pullback}")
                            checked += 1
        self.assertGreater(checked, 0,
                           "no candidate was produced, so nothing was verified - "
                           "the sweep needs a scenario that actually fires")

    def test_s2_direction_faces_value(self):
        """A fade is taken TOWARD value: below value is bought, above value is sold."""
        for side, expected in (("BELOW", "BUY"), ("ABOVE", "SELL")):
            for volume in (0.5, 2.0):
                ctx = self._excursion(side, volume, back_inside=True, pullback=False)
                result = va_reversion.detect(ctx)
                if result.is_rejection:
                    continue
                self.assertEqual(result.direction, expected,
                                 f"excursion {side} value must be faded {expected}")

    def test_the_gate_catches_an_inverted_candidate(self):
        """The safety net itself must work, or the property above is unenforced live."""
        from gates import checks
        from setups.base import Candidate, RejectReason

        ctx = self._excursion("BELOW", 5.0, back_inside=True, pullback=False)
        cases = [
            ("BUY stop above entry", "BUY", 100.0, 101.0, 102.0),
            ("BUY target below entry", "BUY", 100.0, 99.0, 98.0),
            ("SELL stop below entry", "SELL", 100.0, 99.0, 98.0),
            ("SELL target above entry", "SELL", 100.0, 101.0, 102.0),
            ("zero stop", "BUY", 100.0, 0.0, 102.0),
        ]
        for label, direction, entry, stop, target in cases:
            candidate = Candidate(setup="S1-POC", symbol="TESTUSDT",
                                  direction=direction, session_id="2026-09-27",
                                  entry_price=entry, stop_price=stop,
                                  target_price=target, quantity=1.0)
            rejection = checks.direction_geometry(candidate, ctx)
            self.assertIsNotNone(rejection, f"{label} must be rejected")
            self.assertEqual(rejection.reason, RejectReason.GEOMETRY_INVALID, label)

        # And a well-formed candidate must pass, or the gate blocks everything.
        for direction, stop, target in (("BUY", 99.0, 102.0), ("SELL", 101.0, 98.0)):
            good = Candidate(setup="S1-POC", symbol="TESTUSDT", direction=direction,
                             session_id="2026-09-27", entry_price=100.0,
                             stop_price=stop, target_price=target, quantity=1.0)
            self.assertIsNone(checks.direction_geometry(good, ctx),
                              f"valid {direction} geometry must pass")

    def test_s3_direction_follows_the_break(self):
        """A continuation is taken WITH the break: above value is bought."""
        for side, expected in (("ABOVE", "BUY"), ("BELOW", "SELL")):
            for volume in (50.0, 500.0):
                ctx = self._excursion(side, volume, back_inside=False, pullback=True)
                result = va_breakout.detect(ctx)
                if result.is_rejection:
                    continue
                self.assertEqual(result.direction, expected,
                                 f"break {side} value must be followed {expected}")


class StateMachineEligibilityTests(unittest.TestCase):
    """Eligibility must follow the open relationship, and S1/S2 never overlap."""

    def test_inside_value_offers_s2_and_s3_only(self):
        machine = StateMachine()
        state = machine.begin_session("TESTUSDT", "2026-09-27",
                                      _FakeOpen("INSIDE_VALUE"))
        self.assertIn("S2-VAR", state.eligible)
        self.assertIn("S3-BRK", state.eligible)
        self.assertNotIn("S1-POC", state.eligible)

    def test_outside_value_offers_s1_and_s3_only(self):
        machine = StateMachine()
        state = machine.begin_session("TESTUSDT", "2026-09-27",
                                      _FakeOpen("OUTSIDE_VALUE_INSIDE_RANGE"))
        self.assertIn("S1-POC", state.eligible)
        self.assertIn("S1-LVN", state.eligible)
        self.assertNotIn("S2-VAR", state.eligible)

    def test_outside_range_offers_continuation_only(self):
        machine = StateMachine()
        state = machine.begin_session("TESTUSDT", "2026-09-27",
                                      _FakeOpen("OUTSIDE_RANGE"))
        self.assertEqual(state.eligible, ("S3-BRK",))

    def test_s1_and_s2_are_never_both_eligible(self):
        machine = StateMachine()
        for label in ("INSIDE_VALUE", "OUTSIDE_VALUE_INSIDE_RANGE", "OUTSIDE_RANGE"):
            state = machine.begin_session("TESTUSDT", "2026-09-27", _FakeOpen(label))
            has_s1 = any(setup.startswith("S1") for setup in state.eligible)
            has_s2 = "S2-VAR" in state.eligible
            self.assertFalse(has_s1 and has_s2, f"{label} offers both S1 and S2")

    def test_per_setup_cap_is_actually_enforced(self):
        """S2_MAX_PER_SESSION existed in config and was read by nothing.

        Worse than having no cap: an operator setting it to 1 would reasonably believe
        S2 was limited when it was not. A knob that appears to control risk and does
        not is a safety problem.
        """
        machine = StateMachine()
        state = machine.begin_session("TESTUSDT", "2026-09-27",
                                      _FakeOpen("INSIDE_VALUE"))

        original = config.S2_MAX_PER_SESSION
        config.S2_MAX_PER_SESSION = 1
        try:
            allowed, _ = state.can_take("S2-VAR")
            self.assertTrue(allowed, "the first S2 must be allowed")

            state.record_taken("S2-VAR")
            allowed, why = state.can_take("S2-VAR")
            self.assertFalse(allowed, "the second S2 must be capped")
            self.assertIn("S2-VAR", why)

            # The cap is per-setup, so a different setup is unaffected.
            allowed, _ = state.can_take("S3-BRK")
            self.assertTrue(allowed, "S3 must not be blocked by S2's cap")
        finally:
            config.S2_MAX_PER_SESSION = original

    def test_setups_without_a_per_setup_cap_are_only_bound_globally(self):
        machine = StateMachine()
        state = machine.begin_session("TESTUSDT", "2026-09-27",
                                      _FakeOpen("OUTSIDE_VALUE_INSIDE_RANGE"))
        state.record_taken("S1-POC")
        allowed, _ = state.can_take("S1-POC")
        self.assertTrue(allowed,
                        "S1 has no per-setup cap, so only the global cap applies")

    def test_session_setup_cap_is_enforced(self):
        machine = StateMachine()
        state = machine.begin_session("TESTUSDT", "2026-09-27",
                                      _FakeOpen("INSIDE_VALUE"))
        for _ in range(config.MAX_SETUPS_PER_SESSION_PER_SYMBOL):
            state.record_taken("S2-VAR")
        self.assertFalse(state.can_take_another())


class _FakeOpen:
    def __init__(self, label):
        self.label = label


class AntiLookaheadTests(unittest.TestCase):
    """The as_of contract is the one thing a backtest cannot be trusted without."""

    def test_profile_excludes_candles_after_as_of(self):
        candles = f.boxy_session(PREV_START, center=100.0, half_width=1.0)
        full = builder.profile_as_of("T", candles, PREV_START, PREV_START + DAY,
                                    candles[-1].close_time, 0.05)
        half_ts = candles[len(candles) // 2].close_time
        partial = builder.profile_as_of("T", candles, PREV_START, PREV_START + DAY,
                                        half_ts, 0.05)
        self.assertLess(partial.candle_count, full.candle_count)
        self.assertLessEqual(partial.total_volume, full.total_volume)

    def test_resample_drops_incomplete_buckets(self):
        # 20 one-minute candles cannot make two complete 15m buckets.
        candles = walk(CUR_START, [100.0] * 20)
        buckets = klines_mod.resample(candles, "15m", "1m")
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0].open_time, CUR_START)


if __name__ == "__main__":
    unittest.main(verbosity=2)
