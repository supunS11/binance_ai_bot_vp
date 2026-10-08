"""Order placement: entry, then protection, with real venue semantics.

ENTRY PRICING - THE REFERENCE-AND-IMPROVE RULE.

Every setup produces a REFERENCE entry price: the close of the candle that
completed the setup. That is the first price at which the setup demonstrably
existed, so it is honest for both live and replay - using the level itself would
backdate the entry to before the confirming evidence arrived.

The router then converts that reference into an executable PASSIVE price, under one
invariant: THE PLACED PRICE IS NEVER WORSE THAN THE REFERENCE.

    BUY   price = min(reference, best_bid)      lower is better, never above ref
    SELL  price = max(reference, best_ask)      higher is better, never below ref

So if the market has moved in our favour since the candle closed, we take the
better price. If it has moved against us, we rest at the reference and may not
fill - which is the correct outcome, because filling at a worse price than the
setup justified changes the trade's risk/reward without changing its premise.
ENTRY_TIMEOUT_SECONDS then expires it. A missed fill costs nothing; a bad fill
costs real R on a tight stop.

GTX IS POST-ONLY. Binance rejects a GTX order outright (-5022) rather than
crossing, which is exactly the desired behaviour: it guarantees maker fees and
guarantees we never accidentally pay the spread. -5022 is therefore a NORMAL
outcome, not an error - it means the market came to us before the order landed.

PROTECTION ORDERING - STOP FIRST, ALWAYS.

After a fill, the stop is placed BEFORE the target. If target placement fails the
position is still protected; if the order were reversed, a stop failure would leave
an unprotected position. This is the single most consequential ordering decision in
the module.

WHY THE TWO EXIT ORDERS HAVE DIFFERENT TYPES.

    STOP: STOP_MARKET with closePosition=true.
          It MUST exit, so it accepts taker cost and slippage. closePosition
          closes whatever is actually open, which makes it correct under partial
          fills without any quantity bookkeeping.

    TARGET: LIMIT with reduceOnly=true, resting at the target.
          It is a level the profile chose, so resting there earns maker fees and
          fills AT the target rather than through it. That also makes realised R
          match replayed R, which a market target would not.

NO NATIVE OCO. Binance USD-M has no OCO for this pair, and the venue's auto-cancel
of close-all orders on a flat position is not something to rely on. positions.py
explicitly cancels the survivor when it observes the position is flat.
"""
import logging
import time
import uuid

import config
from exchange import filters
from exchange.rest import ApiError
from setups.base import MARKET_ENTRY

log = logging.getLogger(__name__)

# Binance allows 36 characters for newClientOrderId.
_ID_MAX = 36

# EVERY ORDER THIS SYSTEM PLACES CARRIES THIS TAG, and that is what makes it possible
# to tell our orders from anything else in the account on a cold start. Without it,
# recovery cannot distinguish a resting entry this bot left behind from an order the
# operator placed by hand - and the two need opposite treatment: cancel ours, never
# touch theirs.
ORDER_TAG = "vp"

ROLE_ENTRY = "e"
ROLE_STOP = "s"
ROLE_TARGET = "t"
ROLE_CLOSE = "x"


def client_order_id(role, symbol):
    """Short tagged unique id, used for idempotent placement and recovery.

    IDEMPOTENCY IS THE WHOLE POINT. A transport failure leaves the outcome genuinely
    unknown - the order may have been accepted. The only safe recovery is to query
    by an id we chose BEFORE sending. Placing a second order blind is how a position
    ends up doubled.

    Layout: <tag><role><symbol stem><random>, e.g. "vpeBTC1a2b3c4d5e".
    """
    stem = (f"{ORDER_TAG}{role}{symbol.replace('USDT', '')[:6]}"
            f"{uuid.uuid4().hex[:10]}")
    return stem[:_ID_MAX]


def is_ours(client_id):
    """Was this order placed by this system? Read from the id, not from local state.

    Local state is exactly what a crash loses, so ownership has to be recoverable from
    the venue's own record of the order.
    """
    return bool(client_id) and str(client_id).startswith(ORDER_TAG)


def order_role(client_id):
    """ROLE_* for one of our orders, or "" when it is not ours."""
    if not is_ours(client_id):
        return ""
    text = str(client_id)
    return text[len(ORDER_TAG):len(ORDER_TAG) + 1]


def spread_bps(book):
    """Book spread in basis points, for the spread gate."""
    try:
        bid = float(book.get("bidPrice") or 0.0)
        ask = float(book.get("askPrice") or 0.0)
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return ((ask - bid) / mid) * 10_000.0 if mid > 0 else None


def passive_entry_price(reference, book, direction, spec, offset_ticks=None):
    """Reference price improved toward the passive side, never worsened.

    Returns a Decimal already quantised to the tick lattice in the direction that
    keeps the order passive - down for a buy, up for a sell - so the result can go
    straight onto the wire.
    """
    offset_ticks = (config.ENTRY_OFFSET_TICKS if offset_ticks is None
                    else offset_ticks)
    reference = float(reference)

    try:
        bid = float(book.get("bidPrice") or 0.0)
        ask = float(book.get("askPrice") or 0.0)
    except (TypeError, ValueError):
        bid = ask = 0.0

    if direction == "BUY":
        price = min(reference, bid) if bid > 0 else reference
        if offset_ticks:
            price -= offset_ticks * float(spec.tick_size)
    else:
        price = max(reference, ask) if ask > 0 else reference
        if offset_ticks:
            price += offset_ticks * float(spec.tick_size)

    return filters.round_price_passive(price, spec.tick_size, direction)


class OrderRouter:
    """Places and cancels orders. Holds no trading state of its own.

    All state lives in positions.py and the journal, so a router instance is
    disposable and a restart loses nothing.
    """

    def __init__(self, rest, catalog, journal=None):
        self._rest = rest
        self._catalog = catalog
        self._journal = journal

    # ------------------------------------------------------------ preparation

    def prepare_symbol(self, symbol):
        """Set margin type and leverage before the first order on a symbol.

        Both are idempotent at the venue: -4046 means the margin type is already
        what we asked for, which rest.set_margin_type already treats as success.
        Leverage must be set before the order, because a position opened at the
        wrong leverage cannot have it changed while open.
        """
        try:
            self._rest.set_margin_type(symbol, config.MARGIN_TYPE)
        except ApiError as exc:
            log.warning("%s margin type: %s", symbol, exc)
        try:
            self._rest.set_leverage(symbol, config.LEVERAGE)
        except ApiError as exc:
            log.error("%s leverage %sx refused: %s", symbol, config.LEVERAGE, exc)
            return False
        return True

    # ----------------------------------------------------------------- entry

    @staticmethod
    def resolve_entry_price(candidate, book, spec):
        """Fix the final entry price on the candidate BEFORE it is sized.

        WHY THIS IS A SEPARATE STEP. Improving the entry moves it TOWARD the stop - a
        buy improves downward and its stop sits below - so the risk distance shrinks
        after the improvement. Sizing from the pre-improvement price therefore puts on a
        position that risks LESS than RISK_PCT, by however much the entry improved.

        That is conservative rather than dangerous, but it quietly breaks the invariant
        the whole sizing model rests on - "a stop-out costs RISK_PCT of equity by
        construction" - and a unit test asserts that invariant while the integrated
        path violated it. Resolving the price first, then sizing, makes the two agree.

        The STOP is deliberately not recomputed: it is anchored to a profile level, not
        to the entry, so a better entry improves R rather than moving the point at
        which the idea is wrong.
        """
        if candidate.attributes.get("entry_mode") == MARKET_ENTRY:
            return candidate.entry_price
        price = passive_entry_price(candidate.entry_price, book,
                                   candidate.direction, spec)
        candidate.entry_price = float(price)
        return price

    def place_entry(self, candidate, book):
        """Post the entry order. Returns (order, None) or (None, reason).

        A MARKET entry (zone watch) is sent at the current price, so its reference price is
        not improved against the book and its quantity uses the MARKET lot-size filter.

        Expects `candidate.entry_price` to already be the resolved passive price and
        `candidate.quantity` to have been sized from it - see resolve_entry_price.
        Calling this without that ordering still works, but sizes against the
        reference price rather than the placed one.
        """
        spec = self._catalog.require(candidate.symbol)
        market = candidate.attributes.get("entry_mode") == MARKET_ENTRY

        if market:
            price = candidate.entry_price
            quantity = filters.round_quantity(candidate.quantity, spec.market_step_size)
        else:
            price = passive_entry_price(candidate.entry_price, book,
                                       candidate.direction, spec)
            quantity = filters.round_quantity(candidate.quantity, spec.step_size)

        try:
            if not market:
                filters.check_price(price, spec)
            filters.check_quantity(quantity, spec, market_order=market)
            filters.check_notional(price, quantity, spec)
        except filters.FilterRejection as exc:
            return None, f"{exc.code}: {exc.detail}"

        candidate.entry_price = float(price)
        candidate.client_order_id = client_order_id(ROLE_ENTRY, candidate.symbol)

        params = {
            "symbol": candidate.symbol,
            "side": candidate.direction,
            "type": "MARKET" if market else config.ENTRY_ORDER_TYPE,
            "quantity": filters.quantity_str(quantity, spec),
            "newClientOrderId": candidate.client_order_id,
        }
        if not market and config.ENTRY_ORDER_TYPE == "LIMIT":
            params["price"] = filters.price_str(price, spec)
            params["timeInForce"] = config.ENTRY_TIME_IN_FORCE

        try:
            order = self._rest.new_order(**params)
        except ApiError as exc:
            # -5022: GTX would have crossed. The market reached our price before the
            # order landed - a normal race, not a fault, and not retried because the
            # setup's reference price is no longer available.
            if exc.code == -5022:
                return None, "POST_ONLY_WOULD_CROSS"
            # Unknown outcome: the order may exist. Recover by client id rather
            # than by placing another.
            if exc.code in (None, -1001, -1007):
                recovered = self._recover(candidate.symbol, candidate.client_order_id)
                if recovered is not None:
                    return recovered, None
            return None, f"{exc.code}: {exc.message}"

        candidate.entry_order_id = int(order.get("orderId") or 0)
        return order, None

    def _recover(self, symbol, client_id):
        """Query an order by client id after an ambiguous failure."""
        try:
            return self._rest.query_order(symbol, client_order_id=client_id)
        except ApiError as exc:
            if exc.code == -2013:          # does not exist: the order never landed
                return None
            log.error("%s recovery query failed: %s", symbol, exc)
            return None

    # ------------------------------------------------------------ protection

    def place_stop(self, candidate):
        """Place the protective stop alone. Returns (order, error).

        SEPARATE FROM THE TARGET ON PURPOSE. Reconciliation frequently needs exactly
        one of the two legs - a position whose target vanished still has its stop, and
        vice versa. Placing both through one call in that situation adds a DUPLICATE of
        the leg that was already alive: two closePosition stops on one position, or two
        reduceOnly targets, which burn MAX_NUM_ALGO_ORDERS slots and leave an orphan
        behind whichever one triggers first.

        A stop the venue would trigger immediately (-2021) is the dangerous case: the
        position is already open and already past its invalidation, so the caller must
        close at market rather than retry or leave it naked.

        GOES THROUGH THE ALGO ORDER API, NOT new_order(). Binance migrated every
        conditional type (STOP_MARKET included) off /fapi/v1/order onto a separate
        service effective 2025-12-09; placing it the old way is refused outright with
        -4120 ("Order type not supported for this endpoint"). See new_algo_order() in
        exchange/rest.py for the field renames this carries (stopPrice -> triggerPrice,
        orderId -> algoId).
        """
        spec = self._catalog.require(candidate.symbol)
        exit_side = "SELL" if candidate.direction == "BUY" else "BUY"

        stop_price = filters.round_stop_price(candidate.stop_price, spec.tick_size,
                                             candidate.position_side)
        try:
            filters.check_price(stop_price, spec)
            order = self._rest.new_algo_order(
                symbol=candidate.symbol,
                side=exit_side,
                type="STOP_MARKET",
                triggerPrice=filters.price_str(stop_price, spec),
                # closePosition closes whatever is actually open, so a partial entry
                # fill needs no quantity bookkeeping and cannot be under-protected.
                closePosition="true",
                workingType=config.STOP_WORKING_TYPE,
                priceProtect="TRUE" if config.STOP_PRICE_PROTECT else "FALSE",
                clientAlgoId=client_order_id(ROLE_STOP, candidate.symbol),
            )
        except filters.FilterRejection as exc:
            return None, f"stop {exc.code}: {exc.detail}"
        except ApiError as exc:
            if exc.code == -2021:
                return None, "STOP_WOULD_TRIGGER_CLOSE_NOW"
            return None, f"stop {exc.code}: {exc.message}"

        candidate.stop_order_id = int(order.get("algoId") or 0)
        return order, None

    def place_target(self, candidate, quantity):
        """Place the resting take-profit alone. Returns (order, error).

        A resting reduceOnly LIMIT at the level the profile chose: maker fees, and it
        fills AT the target rather than through it, which is also what makes realised R
        comparable with replayed R.

        Failure is not escalated. A missing target costs opportunity; a missing stop
        costs capital. The caller keeps the position and retries next cycle.
        """
        spec = self._catalog.require(candidate.symbol)
        exit_side = "SELL" if candidate.direction == "BUY" else "BUY"

        target_price = filters.round_target_price(candidate.target_price,
                                                 spec.tick_size,
                                                 candidate.position_side)
        try:
            filters.check_price(target_price, spec)
            rounded = filters.round_quantity(quantity, spec.step_size)
            filters.check_quantity(rounded, spec)
            order = self._rest.new_order(
                symbol=candidate.symbol,
                side=exit_side,
                type="LIMIT",
                timeInForce="GTC",
                price=filters.price_str(target_price, spec),
                quantity=filters.quantity_str(rounded, spec),
                reduceOnly="true",
                newClientOrderId=client_order_id(ROLE_TARGET, candidate.symbol),
            )
        except filters.FilterRejection as exc:
            log.warning("%s target not placed (%s)", candidate.symbol, exc.detail)
            return None, f"target {exc.code}: {exc.detail}"
        except ApiError as exc:
            log.warning("%s target not placed (%s)", candidate.symbol, exc.message)
            return None, f"target {exc.code}: {exc.message}"

        candidate.target_order_id = int(order.get("orderId") or 0)
        return order, None

    def place_protection(self, candidate, filled_qty, filled_price=None):
        """Place the stop, THEN the target. Returns (stop, target, error).

        STOP FIRST is the invariant. If the target fails the position is still
        protected and the target is retried next cycle; the reverse ordering would risk
        an unprotected position.

        Used only when opening, where neither leg exists yet. Reconciliation calls
        place_stop / place_target directly - see place_stop on why.
        """
        del filled_price                      # kept for call-site readability
        stop_order, error = self.place_stop(candidate)
        if stop_order is None:
            return None, None, error

        target_order, _ = self.place_target(candidate, filled_qty)
        return stop_order, target_order, None

    # --------------------------------------------------------------- closing

    def close_at_market(self, symbol, reason=""):
        """Emergency exit. Used when a position exists that cannot be protected.

        closePosition on a MARKET order is not accepted by the venue, so the
        position size is read back from positionRisk and closed with a reduceOnly
        market order for exactly that amount.
        """
        try:
            rows = self._rest.position_risk(symbol) or []
        except ApiError as exc:
            log.error("%s cannot read position to close: %s", symbol, exc)
            return None

        amount = 0.0
        for row in rows:
            if (row.get("symbol") or "").upper() == symbol.upper():
                amount = float(row.get("positionAmt") or 0.0)
                break
        if amount == 0:
            return None

        spec = self._catalog.require(symbol)
        quantity = filters.round_quantity(abs(amount), spec.market_step_size)
        if quantity <= 0:
            return None

        log.warning("%s closing at market (%s), qty %s", symbol, reason, quantity)
        try:
            return self._rest.new_order(
                symbol=symbol,
                side="SELL" if amount > 0 else "BUY",
                type="MARKET",
                quantity=filters.quantity_str(quantity, spec),
                reduceOnly="true",
                newClientOrderId=client_order_id(ROLE_CLOSE, symbol),
            )
        except ApiError as exc:
            log.error("%s market close failed: %s", symbol, exc)
            return None

    def cancel(self, symbol, order_id=None, client_id=None):
        """Cancel one order. A gone order (-2011) is success, not failure."""
        try:
            return self._rest.cancel_order(symbol, order_id=order_id,
                                           client_order_id=client_id)
        except ApiError as exc:
            if exc.code in (-2011, -2013):
                return {"status": "ALREADY_GONE"}
            log.error("%s cancel failed: %s", symbol, exc)
            return None

    def cancel_algo(self, symbol, algo_id=None, client_algo_id=None):
        """Cancel one algo (conditional) order - the stop. Separate from cancel()
        because it is a different order book at the venue; see place_stop()."""
        try:
            return self._rest.cancel_algo_order(symbol, algo_id=algo_id,
                                                client_algo_id=client_algo_id)
        except ApiError as exc:
            if exc.code in (-2011, -2013):
                return {"status": "ALREADY_GONE"}
            log.error("%s algo cancel failed: %s", symbol, exc)
            return None

    def cancel_all(self, symbol):
        """Sweep both order books - a resting stop lives in the algo one, never the
        classic one, so cancel_all_orders() alone would leave it behind."""
        try:
            result = self._rest.cancel_all_orders(symbol)
        except ApiError as exc:
            log.error("%s cancel-all failed: %s", symbol, exc)
            result = None
        try:
            self._rest.cancel_all_algo_orders(symbol)
        except ApiError as exc:
            log.error("%s algo cancel-all failed: %s", symbol, exc)
        return result

    # ------------------------------------------------------------ fill state

    def order_state(self, symbol, order_id=None, client_id=None):
        """Normalised view of one order: status, filled quantity, average price."""
        try:
            order = self._rest.query_order(symbol, order_id=order_id,
                                           client_order_id=client_id)
        except ApiError as exc:
            if exc.code == -2013:
                return {"status": "GONE", "filled": 0.0, "avg_price": 0.0}
            log.error("%s order query failed: %s", symbol, exc)
            return None

        filled = float(order.get("executedQty") or 0.0)
        return {
            "status": order.get("status"),
            "filled": filled,
            "avg_price": float(order.get("avgPrice") or 0.0),
            "orig_qty": float(order.get("origQty") or 0.0),
            "order_id": int(order.get("orderId") or 0),
            "raw": order,
        }

    def wait_for_fill(self, candidate, timeout_seconds=None, poll_seconds=3.0):
        """Poll an entry order until filled, expired, or timed out.

        Returns ("FILLED"|"EXPIRED"|"PARTIAL"|"TIMEOUT", state).

        A PARTIAL fill at timeout is kept rather than cancelled: the position exists
        and must be protected, and closePosition-based protection covers whatever
        size actually filled. Cancelling the remainder is the caller's job.
        """
        timeout_seconds = (config.ENTRY_TIMEOUT_SECONDS if timeout_seconds is None
                           else timeout_seconds)

        # Zero means "check once and hand off" - a fully asynchronous entry path, which
        # is also what keeps the test suite from sleeping through every open().
        if timeout_seconds <= 0:
            state = self.order_state(candidate.symbol,
                                     order_id=candidate.entry_order_id or None,
                                     client_id=candidate.client_order_id or None)
            if state and state["status"] == "FILLED":
                candidate.filled_price = state["avg_price"] or candidate.entry_price
                candidate.filled_qty = state["filled"]
                return "FILLED", state
            if state and state["filled"] > 0:
                candidate.filled_price = state["avg_price"]
                candidate.filled_qty = state["filled"]
                return "PARTIAL", state
            return "TIMEOUT", state

        deadline = time.time() + float(timeout_seconds)

        while time.time() < deadline:
            state = self.order_state(candidate.symbol,
                                     order_id=candidate.entry_order_id or None,
                                     client_id=candidate.client_order_id or None)
            if state is None:
                time.sleep(poll_seconds)
                continue

            status = state["status"]
            if status == "FILLED":
                candidate.filled_price = state["avg_price"] or candidate.entry_price
                candidate.filled_qty = state["filled"]
                return "FILLED", state
            if status in ("CANCELED", "EXPIRED", "REJECTED", "GONE"):
                if state["filled"] > 0:
                    candidate.filled_price = state["avg_price"]
                    candidate.filled_qty = state["filled"]
                    return "PARTIAL", state
                return "EXPIRED", state

            time.sleep(poll_seconds)

        final = self.order_state(candidate.symbol,
                                 order_id=candidate.entry_order_id or None,
                                 client_id=candidate.client_order_id or None)
        if final and final["filled"] > 0:
            candidate.filled_price = final["avg_price"]
            candidate.filled_qty = final["filled"]
            return "PARTIAL", final
        return "TIMEOUT", final
