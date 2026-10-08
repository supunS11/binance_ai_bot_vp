"""Position lifecycle: opening, protecting, reconciling, and closing.

THE INVARIANT THIS MODULE ENFORCES: every open position has a live stop, and every
live order corresponds to an open position. Both halves are checked against the
EXCHANGE each cycle, never against local belief, because local belief is exactly
what goes stale - a fill that arrived during a restart, a manual intervention, an
order the venue cancelled on its own.

THE THREE FAILURE MODES IT EXISTS TO HANDLE.

  ORPHAN POSITION - a position with no stop. The most dangerous state a futures bot
        can be in, and it happens for mundane reasons: the process died between the
        entry fill and the stop placement, or stop placement was refused. Recovery
        is to place the stop; if that is impossible, CLOSE AT MARKET. An
        unprotected position is never left open, and this is the only place in the
        system authorised to close on its own initiative.

  ORPHAN ORDERS - reduceOnly or close-all orders resting against a position that no
        longer exists. Binance USD-M has no OCO for a stop/target pair, so when one
        side fills the other survives. Relying on the venue to auto-cancel close-all
        orders is not safe, so the survivor is cancelled explicitly the moment the
        position is observed flat.

  PARTIAL FILLS - an entry that filled 60% and expired. The position is real and
        must be protected for the size that actually filled. closePosition-based
        stops handle this without quantity bookkeeping; the target is sized to the
        observed fill.

SESSION BOUNDARY SEMANTICS, stated explicitly because they are genuinely ambiguous:

  Open positions SURVIVE. Their stop and target were derived from the profile that
  was current at entry, and that profile does not stop being the reason for the
  trade because a clock rolled over.

  Pending candidates are CANCELLED. Their levels reference a profile that is no
  longer the current reference, so acting on them later would be trading yesterday's
  measurement as though it were today's.
"""
import logging
import time
from dataclasses import dataclass, field

import config
import risk
import sessions
from execution import router as router_mod
from setups.base import RejectReason, SetupState, reject

log = logging.getLogger(__name__)


@dataclass
class ManagedPosition:
    """A position this bot opened, with the reasoning that produced it.

    `stop_price` is retained as the ORIGINAL stop, even after the venue order is
    replaced, because R is defined against the stop the setup chose. Recomputing R
    against a moved stop would make slippage invisible - the one thing it most needs
    to show.
    """
    symbol: str
    setup: str
    direction: str
    session_id: str
    entry_price: float
    stop_price: float
    target_price: float
    quantity: float
    opened_at: int
    stop_order_id: int = 0
    target_order_id: int = 0
    client_order_id: str = ""
    attributes: dict = field(default_factory=dict)

    @property
    def position_side(self):
        return "LONG" if self.direction == "BUY" else "SHORT"


@dataclass
class PendingEntry:
    """An entry order that is resting at the venue, not yet filled.

    WHY THIS TYPE EXISTS. The first version waited synchronously for each entry to
    fill, up to ENTRY_TIMEOUT_SECONDS (900s by default). Because entries are posted
    GTX at the setup's reference price, NOT filling promptly is the ordinary case -
    the router's own contract is that it rests rather than chase. So the ordinary case
    stalled the entire bot for fifteen minutes: reconcile never ran, open positions
    went unmonitored, a flat position kept its survivor order resting, a failed target
    was never retried, and several unfilled entries in one pass multiplied the stall.

    An entry in flight is therefore STATE, advanced once per cycle, exactly like a
    position. The candidate is kept whole because protection placement, journaling and
    R arithmetic all need the levels it carries.
    """
    candidate: object
    placed_at: int
    deadline_ms: int

    @property
    def symbol(self):
        return self.candidate.symbol

    def expired(self, now_ms):
        return now_ms >= self.deadline_ms


class PositionManager:
    """Owns the open-position book, entries in flight, and the reconcile loop."""

    def __init__(self, rest, catalog, router, journal=None):
        self._rest = rest
        self._catalog = catalog
        self._router = router
        self._journal = journal
        self._managed = {}          # symbol -> ManagedPosition
        self._pending = {}          # symbol -> PendingEntry
        self._orders_cache = {}     # symbol -> [order]; per-reconcile only

    # ------------------------------------------------------------------ open

    def open(self, candidate, book, portfolio):
        """Place a gated, sized candidate's entry. Returns (managed, pending, rejection).

        Exactly one of the three is non-None:
            managed   filled inside the fast-fill window and already protected
            pending   resting at the venue; advance_pending() takes it from here
            rejection could not be placed at all

        NEVER BLOCKS FOR THE FULL ENTRY TIMEOUT. A short fast-fill poll catches the
        common case where a passive order fills within seconds, because protecting
        immediately is strictly better than protecting a cycle later. Beyond that the
        order becomes tracked state - see PendingEntry for what the blocking version
        cost.

        The invariant is unchanged: no path here leaves an unprotected position open.
        """
        symbol = candidate.symbol

        if not config.TRADING_ENABLED:
            return None, None, reject(RejectReason.TRADING_DISABLED, candidate.setup,
                                      symbol, direction=candidate.direction,
                                      detail="TRADING_ENABLED is False")

        if symbol in self._pending:
            return None, None, reject(RejectReason.ALREADY_IN_POSITION, candidate.setup,
                                      symbol, direction=candidate.direction,
                                      detail="an entry for this symbol is already resting")

        if not self._router.prepare_symbol(symbol):
            return None, None, reject(RejectReason.EXCHANGE_ERROR, candidate.setup,
                                      symbol, direction=candidate.direction,
                                      detail="could not set leverage/margin type")

        # RESOLVE THE PRICE, THEN RE-SIZE. Improving the entry moves it toward the stop,
        # so the risk distance shrinks and a quantity sized from the reference price
        # would risk less than RISK_PCT. Re-sizing against the price actually being
        # placed is what keeps "a stop-out costs RISK_PCT" true in the live path and not
        # only in the unit test. Skipped when there is no book to improve against, in
        # which case the reference price already is the placed price.
        spec = self._catalog.require(symbol)
        if book:
            self._router.resolve_entry_price(candidate, book, spec)
            rejection = risk.size(candidate, portfolio, spec)
            if rejection is not None:
                return None, None, rejection

        order, error = self._router.place_entry(candidate, book)
        if order is None:
            # POST_ONLY_WOULD_CROSS is a race, not a fault: the market reached our
            # price before the order landed. Recorded as a normal skip.
            return None, None, reject(RejectReason.EXCHANGE_ERROR, candidate.setup,
                                      symbol, direction=candidate.direction,
                                      detail=error or "entry not placed")

        candidate.state = SetupState.WORKING
        if self._journal:
            self._journal.record_setup(candidate)

        # Short poll only. Long enough for an order that is going to fill immediately,
        # short enough that the cycle is not held hostage.
        outcome, _ = self._router.wait_for_fill(
            candidate, timeout_seconds=config.ENTRY_FAST_FILL_SECONDS,
            poll_seconds=config.ENTRY_POLL_SECONDS)

        if outcome in ("FILLED", "PARTIAL"):
            managed, rejection = self._settle_fill(candidate, outcome)
            return managed, None, rejection

        now = sessions.now_ms()
        pending = PendingEntry(
            candidate=candidate,
            placed_at=now,
            deadline_ms=now + config.ENTRY_TIMEOUT_SECONDS * 1000,
        )
        self._pending[symbol] = pending
        log.info("%s %s %s entry resting at %.8g qty=%.10g - %ds to fill",
                 symbol, candidate.setup, candidate.direction,
                 candidate.entry_price, candidate.quantity,
                 config.ENTRY_TIMEOUT_SECONDS)
        return None, pending, None

    def _settle_fill(self, candidate, outcome):
        """Protect a filled entry and promote it to a managed position.

        Returns (ManagedPosition, None) or (None, Rejection). The single place a fill
        becomes a position, so the protection invariant has one implementation whether
        the fill arrived in the fast window or a later cycle.
        """
        symbol = candidate.symbol

        if outcome == "PARTIAL":
            # A real position exists. Cancel the unfilled remainder FIRST, so no
            # further size can arrive while protection is being placed.
            self._router.cancel(symbol, order_id=candidate.entry_order_id or None,
                                client_id=candidate.client_order_id or None)

            # RE-QUERY AFTER THE CANCEL. candidate.filled_qty reflects whichever poll
            # first detected PARTIAL, which happened BEFORE cancel() was sent - more of
            # the order can fill in that gap. The stop needs no correction (closePosition
            # protects whatever is actually open), but the target below is placed with an
            # EXPLICIT quantity: sizing it off the stale, too-low figure would leave a
            # slice of the real position with no take-profit, and reconciliation's
            # _replace_missing_targets never catches this, because a target order IS
            # present - just undersized - so has_live_target reads True.
            state = self._router.order_state(
                symbol, order_id=candidate.entry_order_id or None,
                client_id=candidate.client_order_id or None)
            if state is not None and state["filled"] > 0:
                candidate.filled_qty = state["filled"]
                candidate.filled_price = state["avg_price"] or candidate.filled_price

            log.warning("%s partial fill %.10g of %.10g - protecting the filled size",
                        symbol, candidate.filled_qty, candidate.quantity)

        filled_qty = candidate.filled_qty or candidate.quantity
        filled_price = candidate.filled_price or candidate.entry_price

        stop_order, target_order, protect_error = self._router.place_protection(
            candidate, filled_qty, filled_price)

        if stop_order is None:
            # Could not protect. The position is open and naked, so it is closed
            # immediately - accepting a small realised loss in exchange for removing
            # unbounded risk. This is the only automatic close in the system.
            log.error("%s could not place stop (%s) - closing at market",
                      symbol, protect_error)
            self._router.close_at_market(symbol, reason=protect_error or "no stop")
            candidate.state = SetupState.CLOSED
            if self._journal:
                self._journal.record_setup(candidate)
            return None, reject(RejectReason.EXCHANGE_ERROR, candidate.setup, symbol,
                                direction=candidate.direction,
                                detail=f"unprotectable, closed: {protect_error}")

        managed = ManagedPosition(
            symbol=symbol,
            setup=candidate.setup,
            direction=candidate.direction,
            session_id=candidate.session_id,
            entry_price=filled_price,
            stop_price=candidate.stop_price,
            target_price=candidate.target_price,
            quantity=filled_qty,
            opened_at=sessions.now_ms(),
            stop_order_id=candidate.stop_order_id,
            target_order_id=candidate.target_order_id,
            client_order_id=candidate.client_order_id,
            attributes=dict(candidate.attributes),
        )
        self._managed[symbol] = managed
        self._invalidate_orders(symbol)

        candidate.state = SetupState.FILLED
        if self._journal:
            self._journal.record_setup(candidate)
            self._journal.record_trade_open(managed, candidate)

        log.info("%s %s %s opened qty=%.10g entry=%.8g stop=%.8g target=%.8g "
                 "(%.2fR gross, %.2fR net)",
                 symbol, candidate.setup, candidate.direction, filled_qty,
                 filled_price, candidate.stop_price, candidate.target_price,
                 candidate.r_multiple, candidate.net_r_at_target())
        return managed, None

    # -------------------------------------------------------- entries in flight

    def advance_pending(self):
        """Advance every resting entry by one step. Runs FIRST in every cycle.

        Ordered ahead of everything else because a fill that has not yet been protected
        is the most time-critical state the system can be in - more urgent than a
        missing target, and more urgent than any new opportunity.

        Returns a list of newly opened ManagedPositions so the caller can persist them.
        """
        opened = []
        now = sessions.now_ms()

        for symbol in list(self._pending):
            pending = self._pending[symbol]
            candidate = pending.candidate

            state = self._router.order_state(
                symbol,
                order_id=candidate.entry_order_id or None,
                client_id=candidate.client_order_id or None,
            )
            if state is None:
                continue                  # could not ask; retry next cycle

            status = state["status"]
            filled = state["filled"]

            if status == "FILLED":
                candidate.filled_price = state["avg_price"] or candidate.entry_price
                candidate.filled_qty = filled
                del self._pending[symbol]
                managed, _ = self._settle_fill(candidate, "FILLED")
                if managed is not None:
                    opened.append(managed)
                continue

            if status in ("CANCELED", "EXPIRED", "REJECTED", "GONE"):
                del self._pending[symbol]
                if filled > 0:
                    # Gone, but some size filled: a real position exists.
                    candidate.filled_price = state["avg_price"]
                    candidate.filled_qty = filled
                    managed, _ = self._settle_fill(candidate, "PARTIAL")
                    if managed is not None:
                        opened.append(managed)
                else:
                    self._expire_pending(candidate, "entry order gone without a fill")
                continue

            if pending.expired(now):
                del self._pending[symbol]
                if filled > 0:
                    # Partially filled at the deadline. Keep what filled and protect
                    # it; _settle_fill cancels the remainder before placing anything.
                    candidate.filled_price = state["avg_price"]
                    candidate.filled_qty = filled
                    managed, _ = self._settle_fill(candidate, "PARTIAL")
                    if managed is not None:
                        opened.append(managed)
                else:
                    self._router.cancel(
                        symbol, order_id=candidate.entry_order_id or None,
                        client_id=candidate.client_order_id or None)
                    self._expire_pending(candidate, "entry timed out without a fill")

        return opened

    def _expire_pending(self, candidate, detail):
        """Record an entry that never became a position."""
        candidate.state = SetupState.EXPIRED
        if self._journal:
            self._journal.record_setup(candidate)
            self._journal.record_reject(
                reject(RejectReason.EXCHANGE_ERROR, candidate.setup, candidate.symbol,
                       direction=candidate.direction, detail=detail),
                session_id=candidate.session_id)
        log.info("%s %s entry expired: %s", candidate.symbol, candidate.setup, detail)

    def pending(self):
        return dict(self._pending)

    def has_pending(self, symbol):
        return symbol.upper() in self._pending

    def cancel_pending(self, symbol, reason="cancelled"):
        """Cancel a resting entry and stop tracking it."""
        pending = self._pending.pop(symbol.upper(), None)
        if pending is None:
            return False
        candidate = pending.candidate
        self._router.cancel(candidate.symbol,
                            order_id=candidate.entry_order_id or None,
                            client_id=candidate.client_order_id or None)
        self._expire_pending(candidate, reason)
        return True

    # ----------------------------------------------------------- reconcile

    def reconcile(self):
        """Enforce the invariant against exchange truth. Called every cycle.

        Deliberately does four separate passes rather than one merged loop: each pass
        answers a different question, and merging them makes it easy to handle the
        common case and silently miss an edge case.
        """
        # The cache must start empty: orders placed or cancelled since the last pass
        # would otherwise be invisible for a whole cycle.
        self._invalidate_orders()
        exchange_positions = self._exchange_positions()

        self._close_out_finished(exchange_positions)
        self._protect_orphan_positions(exchange_positions)
        self._cancel_orphan_orders(exchange_positions)
        self._replace_missing_targets(exchange_positions)

        return exchange_positions

    def _exchange_positions(self):
        """symbol -> signed position amount, for non-zero positions only."""
        out = {}
        try:
            for row in self._rest.position_risk() or []:
                amount = float(row.get("positionAmt") or 0.0)
                if amount != 0:
                    out[(row.get("symbol") or "").upper()] = {
                        "amount": amount,
                        "entry_price": float(row.get("entryPrice") or 0.0),
                        "unrealised": float(row.get("unRealizedProfit") or 0.0),
                        "liquidation": float(row.get("liquidationPrice") or 0.0),
                    }
        except Exception as exc:                  # noqa: BLE001
            log.error("positionRisk fetch failed, skipping reconcile: %s", exc)
            return None
        return out

    def _close_out_finished(self, exchange_positions):
        """A managed position that is no longer open has finished - settle it.

        Because there is no OCO, the surviving exit order is cancelled here. Doing it
        the moment the position is observed flat is what prevents a stale reduceOnly
        order from later attaching itself to a NEW position on the same symbol.
        """
        if exchange_positions is None:
            return
        for symbol in list(self._managed):
            if symbol in exchange_positions:
                continue
            managed = self._managed.pop(symbol)
            self._router.cancel_all(symbol)
            self._settle(managed)

    def _protect_orphan_positions(self, exchange_positions):
        """Any open position without a live stop gets one, or gets closed.

        Places the STOP ONLY. Calling place_protection here would add a second target
        to a position that still has the first one - see OrderRouter.place_stop.
        """
        if exchange_positions is None:
            return
        for symbol, info in exchange_positions.items():
            has_stop = self._has_live_stop(symbol)
            if has_stop is None:
                # Could not ask the venue. Do NOTHING: the alternative is to treat a
                # network error as a missing stop and respond by closing the position
                # at market. Retried next cycle.
                log.warning("%s: cannot verify stop this cycle - leaving untouched",
                            symbol)
                continue
            if has_stop:
                continue

            managed = self._managed.get(symbol)
            if managed is None:
                # A position this bot does not recognise. It could be a manual trade
                # or a survivor of a crash before the position was recorded. Never
                # guessed at: closing someone else's position is worse than leaving
                # it, so it is reported loudly and left alone.
                log.error("%s: UNMANAGED position %s with no stop - not touching it, "
                          "manual attention required", symbol, info["amount"])
                continue

            log.error("%s: managed position has no stop - replacing", symbol)
            stop_order, error = self._router.place_stop(_candidate_view(managed))
            if stop_order is None:
                log.error("%s: stop replacement failed (%s) - closing at market",
                          symbol, error)
                self._router.close_at_market(symbol, reason="unprotectable")
            else:
                managed.stop_order_id = int(stop_order.get("algoId") or 0)
                self._invalidate_orders(symbol)

    def _cancel_orphan_orders(self, exchange_positions):
        """reduceOnly / close-all orders with no position behind them.

        Scans BOTH order books - a resting stop lives in the algo one (see
        place_stop()), and an orphaned stop left there is exactly the same hazard as
        an orphaned target left in the classic one.
        """
        if exchange_positions is None:
            return
        try:
            open_orders = list(self._rest.open_orders() or [])
            open_orders += list(self._rest.open_algo_orders() or [])
        except Exception as exc:                  # noqa: BLE001
            log.error("openOrders fetch failed: %s", exc)
            return

        for order in open_orders:
            is_algo = "algoId" in order
            symbol = (order.get("symbol") or "").upper()
            client_id = order.get("clientOrderId") or order.get("clientAlgoId") or ""
            role = router_mod.order_role(client_id)

            # An ENTRY order of ours that we are no longer tracking. This can only
            # happen if tracking was lost - a crash, or a bug - and it is the most
            # dangerous loose end there is, because nothing else would ever cancel it
            # and it fills into a position with no stop that the bot cannot claim.
            # Entries are never algo orders, so this branch only ever matches the
            # classic book - is_algo is irrelevant here.
            if role == router_mod.ROLE_ENTRY and not self.has_pending(symbol):
                log.error("%s: cancelling UNTRACKED entry order %s - it would fill "
                          "into an unmanaged, unprotected position",
                          symbol, client_id)
                self._router.cancel(symbol, order_id=order.get("orderId"))
                self._invalidate_orders(symbol)
                continue

            if symbol in exchange_positions:
                continue
            is_exit = (order.get("reduceOnly") in (True, "true")
                       or order.get("closePosition") in (True, "true"))
            if not is_exit:
                continue
            log.warning("%s: cancelling orphan exit order %s (no position)",
                        symbol, order.get("algoId") or order.get("orderId"))
            if is_algo:
                self._router.cancel_algo(symbol, algo_id=order.get("algoId"))
            else:
                self._router.cancel(symbol, order_id=order.get("orderId"))
            self._invalidate_orders(symbol)

    def _replace_missing_targets(self, exchange_positions):
        """A protected position whose target order vanished gets it back.

        Lower priority than the stop - a missing target costs opportunity, a missing
        stop costs capital - so this runs last and never triggers a close.
        """
        if exchange_positions is None:
            return
        for symbol, info in exchange_positions.items():
            managed = self._managed.get(symbol)
            if managed is None:
                continue
            # None (unknown) is treated as "present" here: a target we cannot verify
            # must not be duplicated. The cost of guessing wrong is a missing target
            # for one cycle, against a duplicate reduceOnly order if we guess the
            # other way.
            if self._has_live_target(symbol) is not False:
                continue
            if self._has_live_stop(symbol) is not True:
                continue        # the stop pass owns this symbol this cycle
            log.info("%s: target order missing - replacing", symbol)
            # TARGET ONLY. place_protection would add a second stop to a position
            # that demonstrably already has one - see OrderRouter.place_stop.
            target, _ = self._router.place_target(_candidate_view(managed),
                                                  abs(info["amount"]))
            if target is not None:
                managed.target_order_id = int(target.get("orderId") or 0)
                self._invalidate_orders(symbol)

    # --------------------------------------------------- open-order inspection

    def _invalidate_orders(self, symbol=None):
        """Drop the per-cycle open-order cache after placing or cancelling."""
        if symbol is None:
            self._orders_cache = {}
        else:
            self._orders_cache.pop(symbol.upper(), None)

    def _open_orders_for(self, symbol):
        """Open orders for one symbol, cached for the duration of a reconcile pass.

        Without the cache the three inspection passes each refetch per symbol, so a
        six-position book costs ~18 requests per cycle for information that cannot
        change while the pass is running. The cache is cleared at the start of every
        reconcile and whenever this module places or cancels something, so it can never
        serve a stale answer across an action.

        Merges in the algo (conditional) order book - the live stop rests there, never
        in open_orders() alone, see OrderRouter.place_stop. Either call failing is
        treated as "cannot verify" as a whole: a stop visible only through the half
        that happened to succeed is not something _has_live_stop can tell apart from
        one that is genuinely missing.
        """
        symbol = symbol.upper()
        if symbol in self._orders_cache:
            return self._orders_cache[symbol]
        try:
            orders = list(self._rest.open_orders(symbol) or [])
            orders += list(self._rest.open_algo_orders(symbol) or [])
        except Exception as exc:                  # noqa: BLE001
            log.error("%s openOrders failed: %s", symbol, exc)
            # NOT cached: a failed fetch must not be remembered as "no orders", which
            # would read as "no stop" and trigger a spurious close-at-market.
            return None
        self._orders_cache[symbol] = orders
        return orders

    def _has_live_stop(self, symbol):
        """True / False / None, where None means the venue could not be asked.

        The three-way return is deliberate. Collapsing an unknown to False would make
        a transient openOrders failure look exactly like a missing stop, and the
        response to a missing stop is to place another one or close the position at
        market - acting destructively on a network error.

        THE STOP'S OWN TYPE FIELD IS NAMED DIFFERENTLY ON EACH BOOK. A classic-book
        order (from open_orders()) carries it as "type"; an algo-book order (from
        open_algo_orders(), where a conditional stop actually rests - see
        OrderRouter.place_stop) carries it as "orderType" instead, confirmed against
        Binance's own Query Algo Order response schema. Checking only "type" reads
        every real algo stop as absent, which made reconcile try to place a SECOND
        one every cycle, get refused with -4130 ("an open stop... already exists"),
        and close the position at market as if it had genuinely failed to protect -
        confirmed live before this fix.
        """
        orders = self._open_orders_for(symbol)
        if orders is None:
            return None
        for order in orders:
            if (order.get("type") or order.get("orderType")) in ("STOP_MARKET", "STOP"):
                return True
        return False

    def _has_live_target(self, symbol):
        orders = self._open_orders_for(symbol)
        if orders is None:
            return None
        for order in orders:
            if order.get("type") in ("LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_MARKET") \
                    and order.get("reduceOnly") in (True, "true"):
                return True
        return False

    # -------------------------------------------------------------- settling

    def _settle(self, managed):
        """Work out how a finished position actually ended, and record it.

        The exit price comes from userTrades rather than from the order, because only
        the fill record carries commission - and R computed without fees overstates
        every result. R is measured against the ORIGINAL stop distance so that a
        stop-out filling beyond the stop records as worse than -1.0R, which is where
        slippage becomes visible.
        """
        symbol = managed.symbol
        try:
            trades = self._rest.user_trades(symbol, start_ms=managed.opened_at)
        except Exception as exc:                  # noqa: BLE001
            log.error("%s userTrades failed, settling without fees: %s", symbol, exc)
            trades = []

        exit_side = "SELL" if managed.direction == "BUY" else "BUY"
        exit_qty = 0.0
        exit_value = 0.0
        commission = 0.0
        for trade in trades:
            commission += float(trade.get("commission") or 0.0)
            if (trade.get("side") or "").upper() != exit_side:
                continue
            quantity = float(trade.get("qty") or 0.0)
            exit_qty += quantity
            exit_value += quantity * float(trade.get("price") or 0.0)

        # NO OPTIMISTIC DEFAULT. An earlier version fell back to managed.target_price
        # when no exit fill could be read, which records a full-win outcome whenever
        # userTrades fails - fabricating the single most favourable result available,
        # in the table Phase 2 measures net R from. An unknown exit is recorded AS
        # unknown: the trade is journaled so it is not lost, with no R attributed.
        if exit_qty <= 0:
            log.error("%s: no exit fills found - recording the close as UNKNOWN "
                      "rather than assuming an outcome", symbol)
            if self._journal:
                self._journal.record_trade_close(managed, {
                    "exit_price": 0.0,
                    "exit_reason": "UNKNOWN",
                    "gross_r": None,
                    "net_r": None,
                    "commission": commission,
                    "pnl": None,
                    "closed_at": sessions.now_ms(),
                    "bars_held": (sessions.now_ms() - managed.opened_at) / 3_600_000,
                })
            return

        exit_price = exit_value / exit_qty

        outcome = risk.realised_r(
            managed.entry_price, exit_price, managed.stop_price,
            managed.direction, exit_qty,
        )

        # Classify against the original levels rather than by sign, so a trade that
        # exited between stop and target is not miscounted as either.
        if managed.direction == "BUY":
            hit_stop = exit_price <= managed.stop_price
            hit_target = exit_price >= managed.target_price
        else:
            hit_stop = exit_price >= managed.stop_price
            hit_target = exit_price <= managed.target_price
        reason = "STOP" if hit_stop else "TARGET" if hit_target else "OTHER"

        log.info("%s %s closed %s exit=%.8g gross=%.3fR net=%.3fR fees=%.4f",
                 symbol, managed.setup, reason, exit_price,
                 outcome["gross_r"], outcome["net_r"], commission or outcome["fees"])

        if self._journal:
            self._journal.record_trade_close(managed, {
                "exit_price": exit_price,
                "exit_reason": reason,
                "gross_r": outcome["gross_r"],
                "net_r": outcome["net_r"],
                "commission": commission or outcome["fees"],
                "pnl": outcome["pnl"],
                "closed_at": sessions.now_ms(),
                "bars_held": (sessions.now_ms() - managed.opened_at) / 3_600_000,
            })

    # ------------------------------------------------------ session boundary

    def on_session_boundary(self, new_session_id):
        """Handle the 00:00 UTC roll.

        Open positions are deliberately untouched unless CLOSE_AT_SESSION_END is on -
        that flag exists only so the carry-across question can be MEASURED rather
        than settled by assumption.
        """
        log.info("session boundary -> %s: %s open position(s) carried, "
                 "%s resting entry(ies) cancelled",
                 new_session_id, len(self._managed), len(self._pending))

        # RESTING ENTRIES ARE CANCELLED, as the module docstring states. Their price
        # and stop came from a profile that is no longer the reference, so filling one
        # after the roll would trade yesterday's measurement as though it were today's.
        # Open positions are the opposite case and are left alone: their levels were
        # correct when the risk was taken.
        for symbol in list(self._pending):
            self.cancel_pending(symbol, reason="session boundary - levels superseded")

        if not config.CLOSE_AT_SESSION_END:
            return

        for symbol in list(self._managed):
            log.info("%s: CLOSE_AT_SESSION_END - closing", symbol)
            self._router.cancel_all(symbol)
            self._router.close_at_market(symbol, reason="session end")

    # ------------------------------------------------------------- accessors

    def managed(self):
        return dict(self._managed)

    def has_position(self, symbol):
        return symbol.upper() in self._managed

    def adopt(self, managed):
        """Restore a position into the book after a restart.

        Called by ops/state.py with rows persisted before the crash, so the manager
        knows a position is its own rather than treating it as unmanaged and refusing
        to touch it.
        """
        self._managed[managed.symbol.upper()] = managed


def _candidate_view(managed):
    """Adapt a ManagedPosition to the shape place_protection expects.

    A tiny shim rather than a shared base class: protection placement needs only
    five fields, and coupling the live-position record to the candidate type would
    make both harder to change.
    """
    from setups.base import Candidate
    return Candidate(
        setup=managed.setup,
        symbol=managed.symbol,
        direction=managed.direction,
        entry_price=managed.entry_price,
        stop_price=managed.stop_price,
        target_price=managed.target_price,
        quantity=managed.quantity,
    )
