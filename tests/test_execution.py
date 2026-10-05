"""Execution lifecycle against a strict fake venue.

These cover the only part of the system that had never executed: an order had never
been placed, so every rule in the execution stack was a belief about the API rather
than an observation. The fake refuses what Binance refuses, with the same error codes,
so these tests can fail - which is the entire point of writing them.

WHAT IS ASSERTED, in order of how much a violation would cost:

  1. No path leaves an open position without a stop. Including the paths that FAIL.
  2. A resting entry never blocks the loop, and is still advanced to a protected
     position once it fills.
  3. Exactly one stop and one target per position - reconciliation must not duplicate
     the leg that already exists.
  4. Sizing, rounding and R arithmetic survive a real fill at a different price.
"""
import unittest

import config
import risk
import sessions
from execution.positions import PositionManager
from execution.router import OrderRouter
from setups.base import Candidate, SetupState
from tests.factories import FakeSpec
from tests.fakevenue import FakeCatalog, FakeVenue

SYMBOL = "TESTUSDT"


def _candidate(direction="BUY", entry=100.0, stop=99.0, target=102.0, qty=1.0):
    return Candidate(
        setup="S1-POC",
        symbol=SYMBOL,
        direction=direction,
        session_id="2026-09-27",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        quantity=qty,
    )


class _Journal:
    """Records calls so tests can assert what was journaled, not just what happened."""

    def __init__(self):
        self.setups = []
        self.rejects = []
        self.opens = []
        self.closes = []

    def record_setup(self, candidate, **kwargs):
        self.setups.append((candidate.symbol, candidate.state))

    def record_reject(self, rejection, **kwargs):
        self.rejects.append(rejection.reason)

    def record_trade_open(self, managed, candidate):
        self.opens.append(managed.symbol)

    def record_trade_close(self, managed, row):
        self.closes.append(row)


class _Base(unittest.TestCase):
    def setUp(self):
        self.spec = FakeSpec()
        self.venue = FakeVenue(self.spec, mark_price=100.0, bid=99.99, ask=100.01)
        self.catalog = FakeCatalog(self.spec)
        self.journal = _Journal()
        self.router = OrderRouter(self.venue, self.catalog, self.journal)
        self.positions = PositionManager(self.venue, self.catalog, self.router,
                                         self.journal)
        self.portfolio = risk.PortfolioState(equity=10_000.0,
                                             available_balance=10_000.0)

        self._trading = config.TRADING_ENABLED
        self._fast = config.ENTRY_FAST_FILL_SECONDS
        self._poll = config.ENTRY_POLL_SECONDS
        config.TRADING_ENABLED = True
        # 0 = check once and hand off. The behaviour under test is that a resting order
        # becomes tracked state; how long open() is willing to poll first is a separate
        # concern, and sleeping for it in 20 tests would make the suite useless.
        config.ENTRY_FAST_FILL_SECONDS = 0
        config.ENTRY_POLL_SECONDS = 0.01

    def tearDown(self):
        config.TRADING_ENABLED = self._trading
        config.ENTRY_FAST_FILL_SECONDS = self._fast
        config.ENTRY_POLL_SECONDS = self._poll

    def book(self):
        return self.venue.book_ticker(SYMBOL)


class EntryDoesNotBlockTests(_Base):
    """The bug that stalled the whole bot for fifteen minutes at a time."""

    def test_unfilled_entry_becomes_pending_instead_of_blocking(self):
        managed, pending, rejection = self.positions.open(
            _candidate(), self.book(), self.portfolio)

        self.assertIsNone(rejection)
        self.assertIsNone(managed, "nothing filled, so there is no position yet")
        self.assertIsNotNone(pending, "the resting order must become tracked state")
        self.assertTrue(self.positions.has_pending(SYMBOL))

    def test_pending_entry_is_protected_once_it_fills(self):
        _, pending, _ = self.positions.open(_candidate(), self.book(), self.portfolio)
        self.assertIsNotNone(pending)

        # The market comes to the resting order on a later cycle.
        self.venue.fill(client_order_id=pending.candidate.client_order_id,
                        price=100.0)
        opened = self.positions.advance_pending()

        self.assertEqual(len(opened), 1)
        self.assertFalse(self.positions.has_pending(SYMBOL))
        self.assertEqual(self.venue.count_open(SYMBOL, order_type="STOP_MARKET"), 1)
        self.assertEqual(self.venue.count_open(SYMBOL, order_type="LIMIT",
                                               reduce_only=True), 1)

    def test_entry_filling_inside_the_fast_window_is_protected_immediately(self):
        # An order that crosses is filled by the fake at once, as a market would be.
        self.venue.ask = 99.0            # our buy at 100 is immediately fillable
        candidate = _candidate()

        # GTX would be rejected in that situation, which is itself the correct
        # behaviour; use GTC so this test exercises the immediate-fill path.
        original = config.ENTRY_TIME_IN_FORCE
        config.ENTRY_TIME_IN_FORCE = "GTC"
        try:
            managed, pending, rejection = self.positions.open(
                candidate, self.book(), self.portfolio)
            if managed is None and pending is not None:
                # The fake does not auto-fill resting limits; simulate the venue
                # filling it within the fast window.
                self.venue.fill(client_order_id=candidate.client_order_id, price=99.0)
                opened = self.positions.advance_pending()
                self.assertEqual(len(opened), 1)
                managed = opened[0]
        finally:
            config.ENTRY_TIME_IN_FORCE = original

        self.assertIsNotNone(managed)
        self.assertEqual(self.venue.count_open(SYMBOL, order_type="STOP_MARKET"), 1)

    def test_expired_entry_is_cancelled_and_journaled(self):
        _, pending, _ = self.positions.open(_candidate(), self.book(), self.portfolio)
        pending.deadline_ms = sessions.now_ms() - 1      # force the deadline

        self.positions.advance_pending()

        self.assertFalse(self.positions.has_pending(SYMBOL))
        self.assertNotIn(SYMBOL, self.venue.positions)
        self.assertEqual(self.venue.count_open(SYMBOL), 0,
                         "the unfilled entry must not be left resting")
        self.assertIn(SetupState.EXPIRED, [state for _, state in self.journal.setups])

    def test_second_entry_on_the_same_symbol_is_refused_while_one_rests(self):
        self.positions.open(_candidate(), self.book(), self.portfolio)
        managed, pending, rejection = self.positions.open(
            _candidate(), self.book(), self.portfolio)

        self.assertIsNone(managed)
        self.assertIsNone(pending)
        self.assertIsNotNone(rejection, "must not stack two entries on one symbol")

    def test_session_boundary_cancels_a_resting_entry(self):
        self.positions.open(_candidate(), self.book(), self.portfolio)
        self.positions.on_session_boundary("2026-09-28")

        self.assertFalse(self.positions.has_pending(SYMBOL))
        self.assertEqual(self.venue.count_open(SYMBOL), 0)


class ProtectionInvariantTests(_Base):
    """Every open position has a stop. Including on the failure paths."""

    def _open_filled(self, candidate=None):
        candidate = candidate or _candidate()
        _, pending, _ = self.positions.open(candidate, self.book(), self.portfolio)
        self.venue.fill(client_order_id=candidate.client_order_id, price=100.0)
        opened = self.positions.advance_pending()
        return opened[0] if opened else None

    def test_stop_is_placed_before_the_target(self):
        self._open_filled()
        types = [kwargs.get("type") for name, kwargs in self.venue.calls
                 if name == "new_order"]
        # LIMIT (entry), STOP_MARKET, LIMIT (target)
        self.assertEqual(types[1], "STOP_MARKET",
                         f"stop must be placed before the target, got {types}")

    def test_position_is_closed_when_the_stop_cannot_be_placed(self):
        """The only automatic close in the system, and it must actually happen."""
        candidate = _candidate()
        _, _, _ = self.positions.open(candidate, self.book(), self.portfolio)
        self.venue.fill(client_order_id=candidate.client_order_id, price=100.0)
        # Refuse the stop the way the venue would if it were already through it.
        from exchange.rest import ApiError
        self.venue.fail_next["new_order"] = ApiError(-2021,
                                                     "Order would immediately trigger")

        self.positions.advance_pending()

        self.assertNotIn(SYMBOL, self.venue.positions,
                         "an unprotectable position must be closed, never left naked")
        self.assertFalse(self.positions.has_position(SYMBOL))

    def test_target_failure_leaves_the_position_protected(self):
        """A missing target costs opportunity; it must not cost the stop."""
        candidate = _candidate()
        self.positions.open(candidate, self.book(), self.portfolio)
        self.venue.fill(client_order_id=candidate.client_order_id, price=100.0)

        placed = {"n": 0}
        original = self.venue.new_order
        from exchange.rest import ApiError

        def flaky(**params):
            placed["n"] += 1
            if params.get("type") == "LIMIT" and params.get("reduceOnly"):
                raise ApiError(-4164, "notional too small")
            return original(**params)

        self.venue.new_order = flaky
        self.positions.advance_pending()

        self.assertIn(SYMBOL, self.venue.positions, "position should still be open")
        self.assertTrue(self.positions.has_position(SYMBOL))
        self.assertEqual(self.venue.count_open(SYMBOL, order_type="STOP_MARKET"), 1)

    def test_partial_fill_cancels_the_remainder_then_protects(self):
        candidate = _candidate(qty=2.0)
        self.positions.open(candidate, self.book(), self.portfolio)
        self.venue.fill(client_order_id=candidate.client_order_id,
                        quantity=0.8, price=100.0)
        self.venue.expire(candidate.client_order_id)

        opened = self.positions.advance_pending()

        self.assertEqual(len(opened), 1)
        self.assertAlmostEqual(opened[0].quantity, 0.8, places=6)
        self.assertEqual(self.venue.count_open(SYMBOL, order_type="STOP_MARKET"), 1)
        # The target must cover what actually filled, not what was requested.
        target = [o for o in self.venue.open_orders(SYMBOL)
                  if o["type"] == "LIMIT"][0]
        self.assertAlmostEqual(float(target["origQty"]), 0.8, places=6)

    def test_partial_fill_race_sizes_the_target_off_the_freshest_fill(self):
        """More can fill in the gap between the poll that detects PARTIAL and cancel().

        The stop needs no correction - closePosition covers whatever is actually open -
        but the target is placed with an EXPLICIT quantity. Sizing it off the stale,
        pre-cancel poll would leave a slice of the real position with no take-profit,
        and reconciliation's _replace_missing_targets would never catch it: a target
        order is present, just undersized, so has_live_target reads True regardless.
        """
        candidate = _candidate(qty=2.0)
        self.positions.open(candidate, self.book(), self.portfolio)
        self.venue.fill(client_order_id=candidate.client_order_id,
                        quantity=0.8, price=100.0)
        self.positions._pending[SYMBOL].deadline_ms = sessions.now_ms() - 1

        original_cancel = self.venue.cancel_order

        def cancel_after_late_fill(symbol, order_id=None, client_order_id=None):
            # Simulates more of the order filling before the cancel actually lands.
            self.venue.fill(client_order_id=candidate.client_order_id,
                            quantity=0.5, price=100.0)
            return original_cancel(symbol, order_id=order_id,
                                   client_order_id=client_order_id)

        self.venue.cancel_order = cancel_after_late_fill

        opened = self.positions.advance_pending()

        self.assertEqual(len(opened), 1)
        self.assertAlmostEqual(opened[0].quantity, 1.3, places=6,
                               msg="must reflect the fill that landed during cancel, "
                                   "not the stale pre-cancel poll (0.8)")
        target = [o for o in self.venue.open_orders(SYMBOL)
                  if o["type"] == "LIMIT"][0]
        self.assertAlmostEqual(float(target["origQty"]), 1.3, places=6,
                               msg="the target must cover the freshest fill, not "
                                   "leave 0.5 of the position without a take-profit")


class ReconcileDoesNotDuplicateTests(_Base):
    """The duplicate-leg bug: replacing one exit order must not re-place the other."""

    def _open_filled(self):
        candidate = _candidate()
        self.positions.open(candidate, self.book(), self.portfolio)
        self.venue.fill(client_order_id=candidate.client_order_id, price=100.0)
        self.positions.advance_pending()
        return candidate

    def test_replacing_a_missing_target_does_not_add_a_second_stop(self):
        self._open_filled()
        # The target vanishes (filled elsewhere, or cancelled manually).
        for order in self.venue.open_orders(SYMBOL):
            if order["type"] == "LIMIT":
                self.venue.cancel_order(SYMBOL, order_id=order["orderId"])

        self.positions.reconcile()

        self.assertEqual(self.venue.count_open(SYMBOL, order_type="STOP_MARKET"), 1,
                         "reconcile must not place a second stop")
        self.assertEqual(self.venue.count_open(SYMBOL, order_type="LIMIT",
                                               reduce_only=True), 1)

    def test_replacing_a_missing_stop_does_not_add_a_second_target(self):
        self._open_filled()
        for order in self.venue.open_orders(SYMBOL):
            if order["type"] == "STOP_MARKET":
                self.venue.cancel_order(SYMBOL, order_id=order["orderId"])

        self.positions.reconcile()

        self.assertEqual(self.venue.count_open(SYMBOL, order_type="STOP_MARKET"), 1)
        self.assertEqual(self.venue.count_open(SYMBOL, order_type="LIMIT",
                                               reduce_only=True), 1,
                         "reconcile must not place a second target")

    def test_a_flat_position_has_its_survivor_order_cancelled(self):
        """No OCO on this venue, so the survivor must be cancelled explicitly."""
        self._open_filled()
        # The stop triggers and closes the position; the target survives.
        self.venue.trigger_stop(SYMBOL, price=99.0)
        self.assertNotIn(SYMBOL, self.venue.positions)
        self.assertGreater(self.venue.count_open(SYMBOL, order_type="LIMIT"), 0)

        self.positions.reconcile()

        self.assertEqual(self.venue.count_open(SYMBOL), 0,
                         "the surviving target must be cancelled once flat")
        self.assertFalse(self.positions.has_position(SYMBOL))

    def test_unmanaged_position_is_never_touched(self):
        """Closing someone else's position is worse than leaving it."""
        self.venue.positions[SYMBOL] = 5.0
        self.venue.entry_prices[SYMBOL] = 100.0

        self.positions.reconcile()

        self.assertEqual(self.venue.positions.get(SYMBOL), 5.0,
                         "an unrecognised position must be reported, not closed")

    def test_openorders_failure_does_not_trigger_a_close_or_a_duplicate(self):
        """A transient fetch error must not look like a missing stop.

        The old code returned [] on a failed fetch, which reads as "no stop" - so
        reconcile responded to a network blip by placing a SECOND stop, and would close
        at market if that placement also failed. Asserting only that the position
        survived would not catch that, so this counts orders placed after the break.
        """
        self._open_filled()
        before = sum(1 for name, _ in self.venue.calls if name == "new_order")

        def broken(symbol=None):
            raise RuntimeError("network")

        self.venue.open_orders = broken
        self.positions.reconcile()

        after = sum(1 for name, _ in self.venue.calls if name == "new_order")
        self.assertIn(SYMBOL, self.venue.positions,
                      "a network error must never cause a close-at-market")
        self.assertEqual(after, before,
                         "no order may be placed while the venue cannot be queried")


class SettlementTests(_Base):
    """R must be measured, and never fabricated."""

    def _open_filled(self):
        candidate = _candidate()
        self.positions.open(candidate, self.book(), self.portfolio)
        self.venue.fill(client_order_id=candidate.client_order_id, price=100.0)
        self.positions.advance_pending()

    def test_stop_out_settles_as_a_loss(self):
        self._open_filled()
        self.venue.trigger_stop(SYMBOL, price=99.0)
        self.positions.reconcile()

        self.assertEqual(len(self.journal.closes), 1)
        row = self.journal.closes[0]
        self.assertEqual(row["exit_reason"], "STOP")
        self.assertLess(row["net_r"], 0.0)

    def test_unknown_exit_is_not_recorded_as_a_win(self):
        """The optimistic-default bug: no exit fill must never mean 'hit target'."""
        self._open_filled()
        # Position disappears with no exit trade recorded at all.
        self.venue.positions.pop(SYMBOL, None)
        self.venue.trades = [t for t in self.venue.trades if t["side"] != "SELL"]

        self.positions.reconcile()

        self.assertEqual(len(self.journal.closes), 1)
        row = self.journal.closes[0]
        self.assertEqual(row["exit_reason"], "UNKNOWN")
        self.assertIsNone(row["net_r"],
                         "an unreadable exit must attribute no R at all")


class OrderHygieneTests(_Base):
    """Things the venue enforces that are easy to get wrong in formatting."""

    def test_every_order_carries_a_client_order_id(self):
        candidate = _candidate()
        self.positions.open(candidate, self.book(), self.portfolio)
        self.venue.fill(client_order_id=candidate.client_order_id, price=100.0)
        self.positions.advance_pending()
        # FakeVenue raises AssertionError if any order lacks one; reaching here is
        # the assertion. Confirm we actually placed the three we expect.
        orders = [kwargs for name, kwargs in self.venue.calls if name == "new_order"]
        self.assertEqual(len(orders), 3)
        self.assertTrue(all(o.get("newClientOrderId") for o in orders))

    def test_post_only_rejection_is_a_normal_skip(self):
        """GTX rejection means the market got there first - a race, not a fault."""
        self.venue.ask = 99.0        # a BUY at 100 would cross, so GTX is rejected
        original = config.ENTRY_TIME_IN_FORCE
        config.ENTRY_TIME_IN_FORCE = "GTX"
        try:
            managed, pending, rejection = self.positions.open(
                _candidate(), self.book(), self.portfolio)
        finally:
            config.ENTRY_TIME_IN_FORCE = original

        self.assertIsNone(managed)
        self.assertIsNone(pending)
        self.assertIsNotNone(rejection)
        self.assertIn("POST_ONLY", rejection.detail)
        self.assertNotIn(SYMBOL, self.venue.positions)

    def test_prices_are_placed_on_the_tick_lattice(self):
        """A price off the lattice is -1111 at the venue; the fake enforces it."""
        candidate = _candidate(entry=100.0123456, stop=98.98765, target=102.00001)
        self.positions.open(candidate, self.book(), self.portfolio)
        self.venue.fill(client_order_id=candidate.client_order_id, price=100.0)
        # Raises ApiError(-1111) inside the fake if any price is mis-rounded.
        opened = self.positions.advance_pending()
        self.assertEqual(len(opened), 1)

    def test_stop_is_rounded_away_from_entry_and_target_toward_it(self):
        candidate = _candidate(direction="BUY", entry=100.0,
                               stop=98.987, target=102.013)
        self.positions.open(candidate, self.book(), self.portfolio)
        self.venue.fill(client_order_id=candidate.client_order_id, price=100.0)
        self.positions.advance_pending()

        orders = self.venue.open_orders(SYMBOL)
        stop = [o for o in orders if o["type"] == "STOP_MARKET"][0]
        target = [o for o in orders if o["type"] == "LIMIT"][0]

        # Long: stop rounds DOWN (further away), target rounds DOWN (nearer entry).
        self.assertLessEqual(float(stop["stopPrice"]), 98.987)
        self.assertLessEqual(float(target["price"]), 102.013)


class RiskBudgetThroughTheLivePathTests(_Base):
    """A stop-out must cost RISK_PCT after the router has improved the entry.

    The unit test for sizing asserts this in isolation, on a candidate whose entry
    never moves. In the live path the router improves the entry toward the passive
    side - which is TOWARD the stop - so the risk distance shrinks after sizing and the
    position risks less than intended. Conservative, but it makes the invariant the
    whole sizing model rests on untrue, and only an integrated test can see it.
    """

    def _loss_at_stop(self, candidate):
        return candidate.risk_distance * candidate.quantity

    def test_risk_budget_holds_when_the_entry_improves(self):
        # Reference entry 100.0, but the bid has fallen to 99.50: a passive buy is
        # placed there, which halves the distance to a stop at 99.00.
        self.venue.bid, self.venue.ask = 99.50, 99.51
        candidate = _candidate(direction="BUY", entry=100.0, stop=99.0, target=102.0)

        rejection = risk.size(candidate, self.portfolio, self.spec)
        self.assertIsNone(rejection)
        budget = self.portfolio.equity * config.RISK_PCT

        self.positions.open(candidate, self.book(), self.portfolio)

        self.assertLess(candidate.entry_price, 100.0, "the entry should have improved")
        self.assertAlmostEqual(self._loss_at_stop(candidate), budget,
                               delta=budget * 0.05,
                               msg=(f"entry improved to {candidate.entry_price} so risk "
                                    f"distance is {candidate.risk_distance}; qty must be "
                                    f"re-sized to keep the loss at the budget"))

    def test_stop_is_not_moved_by_an_improved_entry(self):
        """A better entry improves R; it must not move the invalidation point."""
        self.venue.bid, self.venue.ask = 99.50, 99.51
        candidate = _candidate(direction="BUY", entry=100.0, stop=99.0, target=102.0)
        risk.size(candidate, self.portfolio, self.spec)

        self.positions.open(candidate, self.book(), self.portfolio)

        self.assertEqual(candidate.stop_price, 99.0,
                         "the stop is anchored to a profile level, not to the entry")

    def test_entry_is_never_worse_than_the_reference(self):
        """The router's one invariant, in both directions."""
        # Market moved AGAINST a buy: bid above the reference. Must not chase up.
        self.venue.bid, self.venue.ask = 100.50, 100.51
        buy = _candidate(direction="BUY", entry=100.0, stop=99.0, target=102.0)
        risk.size(buy, self.portfolio, self.spec)
        self.positions.open(buy, self.book(), self.portfolio)
        self.assertLessEqual(buy.entry_price, 100.0)

        # Market moved against a sell: ask below the reference. Must not chase down.
        self.venue = FakeVenue(self.spec, mark_price=100.0, bid=99.49, ask=99.50)
        self.router = OrderRouter(self.venue, self.catalog, self.journal)
        self.positions = PositionManager(self.venue, self.catalog, self.router,
                                         self.journal)
        sell = _candidate(direction="SELL", entry=100.0, stop=101.0, target=98.0)
        risk.size(sell, self.portfolio, self.spec)
        self.positions.open(sell, self.book(), self.portfolio)
        self.assertGreaterEqual(sell.entry_price, 100.0)


class NotionalEquityTests(unittest.TestCase):
    """A notional equity must enable observation and never size a real order."""

    class _NoAccount:
        def account(self):
            raise RuntimeError("signed request without credentials")

        def position_risk(self, symbol=None):
            raise RuntimeError("signed request without credentials")

    def test_observation_run_gets_a_usable_equity(self):
        original = config.TRADING_ENABLED
        config.TRADING_ENABLED = False
        try:
            state = risk.portfolio_from_exchange(self._NoAccount())
        finally:
            config.TRADING_ENABLED = original

        self.assertGreater(state.equity, 0.0,
                           "with equity 0 every candidate is refused as 'equity is "
                           "zero' and the observation run records nothing usable")
        self.assertTrue(state.notional_equity)

    def test_live_trading_without_account_access_refuses_to_size(self):
        """The guard that matters: notional equity must never size real risk."""
        original = config.TRADING_ENABLED
        config.TRADING_ENABLED = True
        try:
            state = risk.portfolio_from_exchange(self._NoAccount())
        finally:
            config.TRADING_ENABLED = original

        self.assertEqual(state.equity, 0.0,
                         "not knowing the balance must prevent trading, not invent one")
        self.assertFalse(state.notional_equity)

        candidate = _candidate()
        rejection = risk.size(candidate, state, FakeSpec())
        self.assertIsNotNone(rejection, "sizing must be refused on unknown equity")


class StaleEntryRecoveryTests(_Base):
    """A resting entry that outlived its process is the worst loose end there is.

    Nothing else cleans it up: the orphan-order pass only cancels reduceOnly and
    closePosition orders, and an entry is neither. Left alone it fills later into a
    position with no stop that the bot cannot claim - and the orphan-position pass then
    correctly refuses to touch it. The end state is an unprotected position, open
    indefinitely, by design.
    """

    def test_untracked_entry_order_is_cancelled_by_reconcile(self):
        candidate = _candidate()
        self.positions.open(candidate, self.book(), self.portfolio)
        self.assertEqual(self.venue.count_open(SYMBOL), 1)

        # Simulate losing the in-memory tracking, as a restart would.
        self.positions._pending.clear()
        self.positions.reconcile()

        self.assertEqual(self.venue.count_open(SYMBOL), 0,
                         "an untracked entry order must be cancelled, not left to fill")

    def test_a_tracked_entry_order_is_left_alone(self):
        candidate = _candidate()
        self.positions.open(candidate, self.book(), self.portfolio)

        self.positions.reconcile()

        self.assertEqual(self.venue.count_open(SYMBOL), 1,
                         "an entry we are still tracking must survive reconcile")
        self.assertTrue(self.positions.has_pending(SYMBOL))

    def test_recovery_cancels_our_entry_and_never_a_manual_order(self):
        from ops import runtime

        candidate = _candidate()
        self.positions.open(candidate, self.book(), self.portfolio)

        # An order the operator placed by hand: no tag, so not ours.
        self.venue.new_order(symbol=SYMBOL, side="BUY", type="LIMIT",
                             timeInForce="GTC", price="95.00", quantity="1",
                             newClientOrderId="my-own-order")
        self.assertEqual(self.venue.count_open(SYMBOL), 2)

        cancelled = runtime.cancel_stale_entries(self.venue, self.positions)

        self.assertEqual(cancelled, 1)
        remaining = self.venue.open_orders(SYMBOL)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["clientOrderId"], "my-own-order",
                         "a manual order must never be cancelled by recovery")

    def test_exit_orders_are_not_mistaken_for_entries(self):
        """The stop and target must survive recovery - they protect a live position."""
        from ops import runtime

        candidate = _candidate()
        self.positions.open(candidate, self.book(), self.portfolio)
        self.venue.fill(client_order_id=candidate.client_order_id, price=100.0)
        self.positions.advance_pending()
        self.assertEqual(self.venue.count_open(SYMBOL), 2)   # stop + target

        cancelled = runtime.cancel_stale_entries(self.venue, self.positions)

        self.assertEqual(cancelled, 0)
        self.assertEqual(self.venue.count_open(SYMBOL), 2,
                         "protection orders must survive recovery")


class OrderIdentityTests(unittest.TestCase):
    """Ownership is read from the client order id, because local state is what a
    crash loses."""

    def test_our_orders_are_identifiable_by_role(self):
        from execution import router
        entry = router.client_order_id(router.ROLE_ENTRY, "BTCUSDT")
        stop = router.client_order_id(router.ROLE_STOP, "BTCUSDT")

        self.assertTrue(router.is_ours(entry))
        self.assertEqual(router.order_role(entry), router.ROLE_ENTRY)
        self.assertEqual(router.order_role(stop), router.ROLE_STOP)
        self.assertLessEqual(len(entry), 36, "venue limit is 36 characters")

    def test_foreign_ids_are_not_ours(self):
        from execution import router
        for foreign in ("", None, "my-own-order", "web_12345", "android_abc"):
            self.assertFalse(router.is_ours(foreign), foreign)
            self.assertEqual(router.order_role(foreign), "")

    def test_ids_are_unique(self):
        from execution import router
        ids = {router.client_order_id(router.ROLE_ENTRY, "BTCUSDT")
               for _ in range(2000)}
        self.assertEqual(len(ids), 2000, "a collision would break idempotent recovery")


if __name__ == "__main__":
    unittest.main()
