import unittest
from types import SimpleNamespace
from unittest.mock import patch

import config
from execution.router import OrderRouter
from ops.runtime import StateStore
from setups import orderflow_reversal as ofr
from setups import zone_watch
from setups.base import MARKET_ENTRY, Candidate, RejectReason
from state_machine import StateMachine
from tests.factories import FakeSpec
from tests.fakevenue import FakeCatalog, FakeVenue
from tests.test_orderflow_reversal import MIN, _Levels, _Profile, _bar, _candles

SYMBOL = "TESTUSDT"
VAL_INDEX = 2


def _base():
    return [_bar(100.5, 100.6, 100.4, 100.5, 10.0, 0.0)] * 60


def _arrival():
    return _bar(100.2, 100.3, 99.05, 99.3, 10.0, 0.0)


def _absorb():
    """High volume, aggression against the reversal, closes back up - gate 1's bar.
    Kept close to the zone (high 99.15) so the structural trigger it seeds stays inside
    the plan's too-far distance - see the ENTRY_TOO_FAR tests for what happens when it
    is not."""
    return _bar(99.1, 99.15, 99.0, 99.12, 30.0, -0.5)


def _prev():
    return _bar(99.12, 99.13, 99.05, 99.1, 10.0, -0.3)


def _follow():
    return _bar(99.08, 99.12, 99.05, 99.1, 12.0, 0.4)


def _minute(o, h, l, c, v=0.0, taker=0.0):
    return [(o, h, l, c, v, taker)]


def _absorbed_then(extra):
    """Touch from above, then gate 1 (absorption), then whatever minutes `extra` supplies."""
    return _candles(_base() + [_arrival(), _absorb(), _prev(), _follow()] + extra)


def _ctx(candles, index, weekly_levels=None, dev_levels=None):
    return SimpleNamespace(
        symbol=SYMBOL, session_id="2026-01-01", as_of=candles[index].close_time, atr=1.0,
        session_candles=tuple(candles[:index + 1]), prior_levels=_Levels(),
        prior_profile=_Profile(), naked_pocs=(), dev_levels=dev_levels, dev_shape=None,
        prior_shape=None, migration=None, open_relationship=None,
        maturity={"mature": False}, weekly_levels=weekly_levels,
        spec=None, mark_price=0.0, last_price=0.0, profile_row=lambda: {},
    )


def _drive(watch, candles, weekly_levels=None, dev_levels=None):
    entries, rejects = [], []
    for index in range(len(candles)):
        candidate, rejections = watch.observe(
            _ctx(candles, index, weekly_levels=weekly_levels, dev_levels=dev_levels))
        rejects.extend(rejections)
        if candidate is not None:
            entries.append(candidate)
    return entries, rejects


class ZoneWatchTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(config, "S4_OFR_ENABLED", True),
            patch.object(config, "BIAS_FILTER_ENABLED", False),
            patch.object(config, "OFR_VISIT_RULE", "any"),
            patch.object(config, "OFR_USE_RECORDED_FLOW", False),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_both_gates_hold_and_the_structural_break_enters_at_market(self):
        candles = _absorbed_then([_minute(99.1, 99.2, 99.1, 99.17, 5.0, 2.5)])
        entries, rejects = _drive(zone_watch.ZoneWatch(), candles)

        self.assertEqual(rejects, [])
        self.assertEqual(len(entries), 1)
        candidate = entries[0]
        self.assertEqual(candidate.direction, "BUY")
        self.assertEqual(candidate.attributes["entry_mode"], MARKET_ENTRY)
        self.assertAlmostEqual(candidate.entry_price, 99.17)
        self.assertLess(candidate.stop_price, 99.0)
        self.assertEqual(candidate.attributes["ofr_zone_kind"], "VAL")
        # No modifier fired in this fixture - the gates alone are sufficient under the
        # redesign, which is itself the behaviour under test.
        self.assertEqual(candidate.attributes["ofr_signals"], "")
        self.assertEqual(candidate.attributes["ofr_conviction"], 0)
        # The whole 60-bar base sits well above the zone, so the capped default is
        # what a genuinely sustained approach records - recorded only, never a gate.
        self.assertEqual(candidate.attributes["approach_persistence"],
                         config.OFR_APPROACH_PERSISTENCE_MAX_BARS)

    def test_weekly_poc_distance_atr_is_recorded_against_the_zone_being_tested(self):
        """LEVEL SIGNIFICANCE modifier: is the (already prior-session) zone ALSO
        close to the separate weekly composite's POC - recorded, not gated."""
        candles = _absorbed_then([_minute(99.1, 99.2, 99.1, 99.17, 5.0, 2.5)])
        weekly_levels = SimpleNamespace(poc_price=97.0)
        entries, _ = _drive(zone_watch.ZoneWatch(), candles, weekly_levels=weekly_levels)

        candidate = entries[0]
        # atr=1.0 in this fixture, so ATR-normalised distance equals the raw gap.
        self.assertAlmostEqual(candidate.attributes["weekly_poc_distance_atr"],
                               candidate.level_price - 97.0)

    def test_weekly_poc_distance_atr_is_none_without_a_weekly_bundle(self):
        """A fresh listing with no completed prior calendar week - not an error."""
        candles = _absorbed_then([_minute(99.1, 99.2, 99.1, 99.17, 5.0, 2.5)])
        entries, _ = _drive(zone_watch.ZoneWatch(), candles)

        self.assertIsNone(entries[0].attributes["weekly_poc_distance_atr"])

    def test_dev_vwap_zscore_is_recorded_against_the_entry_price(self):
        """Mean-reversion modifier: how many of TODAY's own VWAP sigmas the entry
        sits from TODAY's live average - recorded, not gated. Deliberately its
        own attribute name, not weekly_poc_distance_atr's sibling - see
        zone_watch.py's comment on why this isn't called vwap_zscore_at_level."""
        candles = _absorbed_then([_minute(99.1, 99.2, 99.1, 99.17, 5.0, 2.5)])
        dev_levels = SimpleNamespace(vwap=99.0, vwap_upper_1sd=99.5)
        entries, _ = _drive(zone_watch.ZoneWatch(), candles, dev_levels=dev_levels)

        candidate = entries[0]
        self.assertAlmostEqual(candidate.attributes["dev_vwap_zscore_at_entry"],
                               (candidate.entry_price - 99.0) / 0.5)

    def test_dev_vwap_zscore_is_none_without_a_developing_profile_yet(self):
        """No candles in today's session yet - not an error."""
        candles = _absorbed_then([_minute(99.1, 99.2, 99.1, 99.17, 5.0, 2.5)])
        entries, _ = _drive(zone_watch.ZoneWatch(), candles)

        self.assertIsNone(entries[0].attributes["dev_vwap_zscore_at_entry"])

    def test_gate_1_holding_without_a_structural_break_does_not_enter(self):
        candles = _absorbed_then([_minute(99.1, 99.14, 99.1, 99.1, 5.0, 2.5)])
        entries, _ = _drive(zone_watch.ZoneWatch(), candles)
        self.assertEqual(entries, [])

    def test_price_moving_without_absorption_does_not_enter(self):
        flat = [_bar(99.1, 99.12, 99.08, 99.1, 10.0, 0.0)] * 4
        candles = _candles(_base() + [_arrival()] + flat
                           + [_minute(99.1, 99.3, 99.1, 99.25, 5.0, 2.5)])
        entries, _ = _drive(zone_watch.ZoneWatch(), candles)
        self.assertEqual(entries, [])

    def test_close_beyond_the_zone_on_the_wrong_side_ends_the_watch(self):
        # Entirely below the zone (high 98.85 < zone.low 98.9), so nothing here can touch
        # it and start a legitimate second visit - isolates the wrong-way ending itself.
        tail = [(98.85, 98.85, 98.8, 98.8, 10.0, 0.0)] + [(98.8, 98.82, 98.78, 98.8, 1.0, 0.0)] * 5
        candles = _candles(_base() + [_arrival(), tail])
        watch = zone_watch.ZoneWatch()
        entries, _ = _drive(watch, candles)
        self.assertEqual(entries, [])
        self.assertEqual(watch._symbols[SYMBOL].active, {})

    def test_a_wrong_way_exit_allows_a_genuine_new_visit_from_the_other_side(self):
        # The flip side of the above: price that comes back from BELOW the zone after a
        # BUY watch ends wrong-way is a real new visit under any-visit, and may enter as
        # a SELL. Not a bug - the watch tracks visits, not "one chance per session".
        wrong = _minute(98.85, 98.85, 98.8, 98.8, 10.0, 0.0)
        candles = _candles(_base() + [_arrival(), wrong, _absorb(), _prev(), _follow(),
                                      _minute(99.1, 99.2, 99.1, 99.17, 5.0, 2.5)])
        watch = zone_watch.ZoneWatch()
        entries, _ = _drive(watch, candles)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].direction, "BUY")
        self.assertEqual(entries[0].attributes["ofr_visit_number"], 2)

    def test_a_break_further_than_the_plan_allows_is_refused_once_and_the_watch_stays_open(self):
        wide_absorb = _bar(99.3, 99.4, 99.0, 99.35, 30.0, -0.5)
        candles = _candles(
            _base() + [_arrival(), wide_absorb, _prev(), _follow()]
            + [_minute(99.1, 99.45, 99.1, 99.42, 5.0, 2.5),
               _minute(99.1, 99.45, 99.1, 99.42, 5.0, 2.5)])
        watch = zone_watch.ZoneWatch()
        entries, rejects = _drive(watch, candles)
        self.assertEqual(entries, [])
        self.assertEqual([r.reason for r in rejects], [RejectReason.ENTRY_TOO_FAR])
        self.assertIn(VAL_INDEX, watch._symbols[SYMBOL].active)
        self.assertNotIn(VAL_INDEX, watch._symbols[SYMBOL].done)

    def test_a_later_pullback_within_distance_enters_after_a_too_far_refusal(self):
        candles = _absorbed_then([_minute(99.1, 99.5, 99.1, 99.5, 5.0, 2.5),
                                  _minute(99.17, 99.2, 99.1, 99.17, 5.0, 2.5)])
        entries, rejects = _drive(zone_watch.ZoneWatch(), candles)
        self.assertEqual([r.reason for r in rejects], [RejectReason.ENTRY_TOO_FAR])
        self.assertEqual(len(entries), 1)
        self.assertAlmostEqual(entries[0].entry_price, 99.17)

    def test_second_visit_is_watched_under_any_visit_and_ignored_under_first(self):
        wrong = _minute(98.85, 98.85, 98.8, 98.8, 10.0, 0.0)
        back = _minute(99.0, 99.05, 98.95, 99.0, 5.0, 0.0)
        candles = _candles(_base() + [_arrival(), wrong, back])

        watch = zone_watch.ZoneWatch()
        _drive(watch, candles)
        self.assertEqual(watch._symbols[SYMBOL].active[VAL_INDEX].side, "SELL")

        with patch.object(config, "OFR_VISIT_RULE", "first"):
            first = zone_watch.ZoneWatch()
            _drive(first, candles)
        self.assertNotIn(VAL_INDEX, first._symbols[SYMBOL].active)

    def test_minutes_already_seen_are_not_processed_twice(self):
        candles = _absorbed_then([_minute(99.1, 99.2, 99.1, 99.17, 5.0, 2.5)])
        watch = zone_watch.ZoneWatch()
        entries, _ = _drive(watch, candles)
        self.assertEqual(len(entries), 1)
        again, _ = watch.observe(_ctx(candles, len(candles) - 1))
        self.assertIsNone(again)

    def test_a_new_session_drops_every_watch(self):
        candles = _candles(_base() + [_arrival()])
        watch = zone_watch.ZoneWatch()
        _drive(watch, candles)
        self.assertIn(VAL_INDEX, watch._symbols[SYMBOL].active)
        ctx = _ctx(candles, len(candles) - 1)
        ctx.session_id = "2026-01-02"
        watch.observe(ctx)
        self.assertEqual(watch._symbols[SYMBOL].session_id, "2026-01-02")


class ApproachModifierTests(unittest.TestCase):
    """gate 1 reads the approach leg once - a unit test of _gate1 directly, since building
    a full zone-watch fixture with a genuine CVD divergence on the way in is otherwise a
    lot of bars to get right for a property already covered by test_orderflow_reversal."""

    def test_a_diverging_approach_is_recorded_as_a_modifier_not_a_gate(self):
        # The exact rows test_orderflow_reversal.SignalTests already proves trigger CVD
        # divergence for a BUY - all comfortably above the zone below, so approach_leg
        # takes the whole run as the leg in, none of it as "at the zone".
        rows = ([(100, 100.1, 99.2, 99.5, 10.0, -0.4)] * 5
                + [(99.5, 99.6, 99.0, 99.2, 10.0, -0.4)]
                + [(99.2, 99.3, 98.5, 98.8, 10.0, 0.4)] * 6)
        from data.klines import Candle
        approach = [Candle(open_time=i, open=o, high=h, low=l, close=c, volume=v,
                           close_time=i, quote_volume=0, trades=0,
                           taker_buy_base=v * (1.0 + d) / 2.0, taker_buy_quote=0)
                   for i, (o, h, l, c, v, d) in enumerate(rows)]
        zone = ofr.Zone("VAL", 90.0, 91.0)
        absorb_bar = Candle(open_time=100, open=90.5, high=90.8, low=90.0, close=90.6,
                            volume=30.0, close_time=100, quote_volume=0, trades=0,
                            taker_buy_base=30.0 * 0.25, taker_buy_quote=0)
        # _absorption excludes the very last bar from its search range, so gate 1 only
        # sees the absorb bar once at least one more bar follows it.
        after = Candle(open_time=101, open=90.6, high=90.65, low=90.55, close=90.6,
                       volume=1.0, close_time=101, quote_volume=0, trades=0,
                       taker_buy_base=0.5, taker_buy_quote=0)
        bars = approach + [absorb_bar, after]
        absorb_index = len(bars) - 2

        watch = zone_watch._Watch(zone=zone, index=0, side="BUY", visits=1, started_ms=0)
        zone_watch.ZoneWatch()._gate1(watch, bars)
        self.assertEqual(watch.absorb_index, absorb_index)
        self.assertTrue(watch.approach_cvd)


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(config, "S4_OFR_ENABLED", True),
            patch.object(config, "BIAS_FILTER_ENABLED", False),
            patch.object(config, "OFR_VISIT_RULE", "any"),
            patch.object(config, "OFR_USE_RECORDED_FLOW", False),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_a_watch_resumes_after_restart_and_enters_as_if_never_stopped(self):
        candles = _absorbed_then([_minute(99.1, 99.2, 99.1, 99.17, 5.0, 2.5)])
        cut = len(candles) - 1
        store = StateStore(":memory:")

        before = zone_watch.ZoneWatch(store)
        for index in range(cut):
            before.observe(_ctx(candles, index))
        self.assertIsNotNone(before._symbols[SYMBOL].active[VAL_INDEX].absorb_index)

        after = zone_watch.ZoneWatch(store)
        after.restore()
        candidate, _ = after.observe(_ctx(candles, cut))
        self.assertIsNotNone(candidate)
        self.assertAlmostEqual(candidate.entry_price, 99.17)

    def test_a_saved_entry_is_not_repeated_after_restart(self):
        candles = _absorbed_then([_minute(99.1, 99.2, 99.1, 99.17, 5.0, 2.5)])
        store = StateStore(":memory:")
        first = zone_watch.ZoneWatch(store)
        entries, _ = _drive(first, candles)
        self.assertEqual(len(entries), 1)

        after = zone_watch.ZoneWatch(store)
        after.restore()
        entries, _ = _drive(after, candles)
        self.assertEqual(entries, [])


class BiasAtEntryTests(unittest.TestCase):
    """The composite bias can drift between watch-start and the structural break actually
    firing minutes later - _start() only filters the cheap, early case. _enter() re-reads
    bias fresh (it already did, for recording) and must also refuse the trade if the read
    has turned clearly opposed by then, without ending the watch - gate 2 may re-fire on a
    later bar once the bias clears, same as any other bar where _enter() declines."""

    def setUp(self):
        self.patches = [
            patch.object(config, "S4_OFR_ENABLED", True),
            patch.object(config, "BIAS_FILTER_ENABLED", True),
            patch.object(config, "OFR_VISIT_RULE", "any"),
            patch.object(config, "OFR_USE_RECORDED_FLOW", False),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_a_bias_that_turns_opposed_by_entry_is_refused_and_the_watch_stays_open(self):
        candles = _absorbed_then([_minute(99.1, 99.2, 99.1, 99.17, 5.0, 2.5)])
        with patch.object(zone_watch.bias_mod, "session_bias",
                          side_effect=[("NEUTRAL", 0), ("BEAR", 2)]):
            watch = zone_watch.ZoneWatch()
            entries, rejects = _drive(watch, candles)

        self.assertEqual(entries, [])
        self.assertEqual([r.reason for r in rejects], [RejectReason.BIAS_OPPOSED])
        self.assertIn(VAL_INDEX, watch._symbols[SYMBOL].active)
        self.assertNotIn(VAL_INDEX, watch._symbols[SYMBOL].done)

    def test_a_favorable_bias_at_both_checks_still_enters(self):
        candles = _absorbed_then([_minute(99.1, 99.2, 99.1, 99.17, 5.0, 2.5)])
        with patch.object(zone_watch.bias_mod, "session_bias",
                          side_effect=[("NEUTRAL", 0), ("BULL", 1)]):
            watch = zone_watch.ZoneWatch()
            entries, rejects = _drive(watch, candles)

        self.assertEqual(rejects, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].direction, "BUY")


class DoubleVoteVetoTests(unittest.TestCase):
    """A stronger, separate veto from the composite bias filter above - see
    setups.base.weekly_and_vwap_both_oppose's own docstring for the validation.
    BIAS_FILTER_ENABLED is off throughout so these tests isolate the new check."""

    def setUp(self):
        self.patches = [
            patch.object(config, "S4_OFR_ENABLED", True),
            patch.object(config, "BIAS_FILTER_ENABLED", False),
            patch.object(config, "DOUBLE_VOTE_VETO_ENABLED", True),
            patch.object(config, "OFR_VISIT_RULE", "any"),
            patch.object(config, "OFR_USE_RECORDED_FLOW", False),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_both_votes_opposing_the_buy_refuses_it(self):
        candles = _absorbed_then([_minute(99.1, 99.2, 99.1, 99.17, 5.0, 2.5)])
        # zone.center (~99.1) sits well BELOW this POC -> a SELL vote.
        weekly_levels = SimpleNamespace(poc_price=102.0)
        # entry (~99.17) sits well ABOVE this VWAP -> a SELL vote too.
        dev_levels = SimpleNamespace(vwap=95.0, vwap_upper_1sd=95.5)
        entries, rejects = _drive(zone_watch.ZoneWatch(), candles,
                                  weekly_levels=weekly_levels, dev_levels=dev_levels)

        self.assertEqual(entries, [])
        self.assertEqual([r.reason for r in rejects], [RejectReason.WEEKLY_VWAP_OPPOSED])

    def test_only_one_reading_opposing_still_enters(self):
        """No shared opinion between the two readings to veto with."""
        candles = _absorbed_then([_minute(99.1, 99.2, 99.1, 99.17, 5.0, 2.5)])
        weekly_levels = SimpleNamespace(poc_price=102.0)   # SELL vote, alone
        entries, rejects = _drive(zone_watch.ZoneWatch(), candles, weekly_levels=weekly_levels)

        self.assertEqual(rejects, [])
        self.assertEqual(len(entries), 1)

    def test_disabling_the_flag_lets_the_trade_through(self):
        candles = _absorbed_then([_minute(99.1, 99.2, 99.1, 99.17, 5.0, 2.5)])
        weekly_levels = SimpleNamespace(poc_price=102.0)
        dev_levels = SimpleNamespace(vwap=95.0, vwap_upper_1sd=95.5)
        with patch.object(config, "DOUBLE_VOTE_VETO_ENABLED", False):
            entries, rejects = _drive(zone_watch.ZoneWatch(), candles,
                                      weekly_levels=weekly_levels, dev_levels=dev_levels)

        self.assertEqual(rejects, [])
        self.assertEqual(len(entries), 1)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(config, "ACTIVE_SETUPS", ["S4-OFR"])
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_a_setup_ineligible_under_the_open_relationship_is_refused(self):
        ctx = _ctx(_candles([_bar(100.0, 100.1, 99.9, 100.0, 10.0, 0.0)]), 0)
        ctx.open_relationship = SimpleNamespace(label="OUTSIDE_RANGE")
        refusal = StateMachine().admission(ctx, "S4-OFR")
        self.assertEqual(refusal.reason, RejectReason.OPEN_RELATIONSHIP_WRONG)

    def test_an_eligible_setup_is_admitted(self):
        ctx = _ctx(_candles([_bar(100.0, 100.1, 99.9, 100.0, 10.0, 0.0)]), 0)
        self.assertIsNone(StateMachine().admission(ctx, "S4-OFR"))


class MarketEntryRouterTests(unittest.TestCase):
    def setUp(self):
        self.spec = FakeSpec()
        self.venue = FakeVenue(self.spec, mark_price=99.15, bid=99.14, ask=99.16)
        self.router = OrderRouter(self.venue, FakeCatalog(self.spec), None)

    def _market_candidate(self):
        return Candidate(
            setup="S4-OFR", symbol=SYMBOL, direction="BUY", session_id="2026-01-01",
            entry_price=99.15, stop_price=98.8, target_price=100.5, quantity=1.0,
            attributes={"entry_mode": MARKET_ENTRY},
        )

    def test_a_market_entry_keeps_its_reference_price_and_is_sent_as_market(self):
        candidate = self._market_candidate()
        book = self.venue.book_ticker(SYMBOL)
        self.assertEqual(OrderRouter.resolve_entry_price(candidate, book, self.spec), 99.15)

        order, error = self.router.place_entry(candidate, book)
        self.assertIsNone(error)
        params = [kwargs for name, kwargs in self.venue.calls if name == "new_order"][-1]
        self.assertEqual(params["type"], "MARKET")
        self.assertNotIn("price", params)
        self.assertNotIn("timeInForce", params)


if __name__ == "__main__":
    unittest.main()
