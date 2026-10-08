"""A fake USDⓈ-M venue, strict about the semantics that actually bite.

WHY THIS EXISTS. The execution stack is the only part of the system that had never
run when it was first written - no order had ever been placed, not even on testnet -
so every belief encoded in it was a belief about the API rather than an observation of
it. Unit tests using permissive mocks cannot find that class of error, because a
permissive mock agrees with whatever the code does.

So this fake ENFORCES rather than accepts. It refuses what Binance refuses, with the
same error codes, and it models the three mechanics that produce real bugs:

  GTX IS POST-ONLY. A buy at or above the ask is rejected -5022, not filled. This is
      the normal path for a passive entry that the market has already reached, and the
      router must treat it as a race rather than an error.

  closePosition IS NOT A QUANTITY. A STOP_MARKET + closePosition order closes whatever
      is open when it triggers, so it must remain valid when the position size changes
      under it - which is what makes it correct under partial fills.

  reduceOnly CANNOT OPEN OR INCREASE. A reduceOnly order with no position behind it is
      rejected -2022, which is what makes an orphaned target a real hazard rather than
      a tidiness issue.

It also enforces the filter lattice (tick size, step size, min notional), because a
price or quantity formatted with the wrong precision is rejected -1111 by the venue
and that is the single easiest mistake to make in this layer.

What it deliberately does NOT model: matching against a real book, funding, liquidation,
margin. Those need testnet; this covers order lifecycle and bookkeeping.
"""
from decimal import Decimal

from exchange.rest import ApiError


def _is_multiple(value, step):
    if step <= 0:
        return True
    quotient = (Decimal(str(value)) / Decimal(str(step))).normalize()
    return quotient == quotient.to_integral_value()


class FakeVenue:
    """Order book of record for one account. Duck-types the RestClient surface."""

    def __init__(self, spec, mark_price=100.0, bid=None, ask=None):
        self.spec = spec
        self.mark_price = mark_price
        self.bid = bid if bid is not None else mark_price - 0.01
        self.ask = ask if ask is not None else mark_price + 0.01

        self.orders = {}             # order_id -> order dict
        self.positions = {}          # symbol -> signed amount
        self.entry_prices = {}       # symbol -> average entry
        self.trades = []             # userTrades rows
        self.calls = []              # every method call, for assertions
        self._next_id = 1000
        self.leverage = {}
        self.margin_type = {}
        self.fail_next = {}          # method name -> ApiError to raise once

    # --------------------------------------------------------------- helpers

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))
        error = self.fail_next.pop(name, None)
        if error is not None:
            raise error

    def _new_id(self):
        self._next_id += 1
        return self._next_id

    def _check_filters(self, price, quantity, is_market=False):
        tick = float(self.spec.tick_size)
        step = float(self.spec.market_step_size if is_market else self.spec.step_size)
        if price is not None and price > 0 and not _is_multiple(price, tick):
            raise ApiError(-1111, f"price {price} not a multiple of {tick}")
        if quantity is not None and quantity > 0 and not _is_multiple(quantity, step):
            raise ApiError(-1111, f"quantity {quantity} not a multiple of {step}")
        if price and quantity and price * quantity < float(self.spec.min_notional):
            raise ApiError(-4164, "order notional below minimum")

    def count_open(self, symbol, order_type=None, reduce_only=None):
        total = 0
        for order in self.orders.values():
            if order["symbol"] != symbol or order["status"] != "NEW":
                continue
            if order_type and order["type"] != order_type:
                continue
            if reduce_only is not None:
                is_reduce = (order.get("reduceOnly") in (True, "true")
                             or order.get("closePosition") in (True, "true"))
                if is_reduce != reduce_only:
                    continue
            total += 1
        return total

    # ------------------------------------------------------- account plumbing

    def set_leverage(self, symbol, leverage):
        self._record("set_leverage", symbol=symbol, leverage=leverage)
        self.leverage[symbol] = leverage
        return {"leverage": leverage}

    def set_margin_type(self, symbol, margin_type):
        self._record("set_margin_type", symbol=symbol, margin_type=margin_type)
        self.margin_type[symbol] = margin_type
        return {"code": 200}

    def book_ticker(self, symbol):
        self._record("book_ticker", symbol=symbol)
        return {"symbol": symbol, "bidPrice": str(self.bid), "askPrice": str(self.ask)}

    def position_risk(self, symbol=None):
        self._record("position_risk", symbol=symbol)
        out = []
        for name, amount in self.positions.items():
            if symbol and name != symbol:
                continue
            out.append({
                "symbol": name,
                "positionAmt": str(amount),
                "entryPrice": str(self.entry_prices.get(name, 0.0)),
                "unRealizedProfit": "0",
                "liquidationPrice": "0",
            })
        return out

    def user_trades(self, symbol, start_ms=None, limit=500):
        self._record("user_trades", symbol=symbol)
        return [t for t in self.trades if t["symbol"] == symbol]

    # ----------------------------------------------------------------- orders

    def new_order(self, **params):
        self._record("new_order", **params)
        symbol = params["symbol"]
        side = params["side"]
        order_type = params["type"]
        quantity = float(params.get("quantity") or 0.0)
        price = float(params.get("price") or 0.0)
        stop_price = float(params.get("stopPrice") or 0.0)
        close_position = params.get("closePosition") in (True, "true")
        reduce_only = params.get("reduceOnly") in (True, "true")

        if not params.get("newClientOrderId"):
            raise AssertionError("every order must carry a newClientOrderId")

        self._check_filters(price or stop_price or None,
                            None if close_position else quantity,
                            is_market=(order_type == "MARKET"))

        position = self.positions.get(symbol, 0.0)

        # reduceOnly / closePosition cannot open or increase a position.
        if (reduce_only or close_position) and position == 0:
            raise ApiError(-2022, "ReduceOnly Order is rejected")

        # A stop that would trigger the moment it is placed.
        if order_type == "STOP_MARKET" and stop_price > 0:
            if position > 0 and stop_price >= self.mark_price:
                raise ApiError(-2021, "Order would immediately trigger")
            if position < 0 and stop_price <= self.mark_price:
                raise ApiError(-2021, "Order would immediately trigger")

        order_id = self._new_id()
        order = {
            "orderId": order_id,
            "clientOrderId": params["newClientOrderId"],
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "price": str(price),
            "stopPrice": str(stop_price),
            "origQty": str(quantity),
            "executedQty": "0",
            "avgPrice": "0",
            "status": "NEW",
            "reduceOnly": reduce_only,
            "closePosition": close_position,
            "timeInForce": params.get("timeInForce", "GTC"),
        }

        if order_type == "MARKET":
            self.orders[order_id] = order
            self._fill(order_id, quantity, self.mark_price)
            return self.orders[order_id]

        if order_type == "LIMIT" and order["timeInForce"] == "GTX":
            # Post-only: reject rather than cross.
            if side == "BUY" and price >= self.ask:
                raise ApiError(-5022, "Post Only order will be rejected")
            if side == "SELL" and price <= self.bid:
                raise ApiError(-5022, "Post Only order will be rejected")

        self.orders[order_id] = order
        return order

    def query_order(self, symbol, order_id=None, client_order_id=None):
        self._record("query_order", symbol=symbol, order_id=order_id,
                     client_order_id=client_order_id)
        order = self._find(symbol, order_id, client_order_id)
        if order is None:
            raise ApiError(-2013, "Order does not exist")
        return order

    def cancel_order(self, symbol, order_id=None, client_order_id=None):
        self._record("cancel_order", symbol=symbol, order_id=order_id,
                     client_order_id=client_order_id)
        order = self._find(symbol, order_id, client_order_id)
        if order is None:
            raise ApiError(-2011, "Unknown order sent")
        if order["status"] != "NEW":
            raise ApiError(-2011, "Unknown order sent")
        order["status"] = "CANCELED"
        return order

    def cancel_all_orders(self, symbol):
        self._record("cancel_all_orders", symbol=symbol)
        for order in self.orders.values():
            if order["symbol"] == symbol and order["status"] == "NEW":
                order["status"] = "CANCELED"
        return {"code": 200}

    def open_orders(self, symbol=None):
        self._record("open_orders", symbol=symbol)
        return [dict(order) for order in self.orders.values()
                if order["status"] == "NEW" and not order.get("_algo")
                and (symbol is None or order["symbol"] == symbol)]

    # ------------------------------------------------------------ algo orders
    #
    # Modelled as the SAME underlying order (shared id space, same _find/_fill/
    # cancel_order machinery - trigger_stop and count_open keep working unchanged),
    # tagged "_algo" so open_orders() excludes it and open_algo_orders() is the only
    # way to see it - the real separation this fake exists to catch bugs against.

    def new_algo_order(self, **params):
        self._record("new_algo_order", **params)
        if params.get("algoType", "CONDITIONAL") != "CONDITIONAL":
            raise AssertionError(f"unmodelled algoType {params.get('algoType')}")
        if not params.get("clientAlgoId"):
            raise AssertionError("every algo order must carry a clientAlgoId")

        symbol = params["symbol"]
        side = params["side"]
        order_type = params["type"]
        quantity = float(params.get("quantity") or 0.0)
        trigger_price = float(params.get("triggerPrice") or 0.0)
        close_position = params.get("closePosition") in (True, "true")
        reduce_only = params.get("reduceOnly") in (True, "true")

        self._check_filters(trigger_price or None,
                            None if close_position else quantity, is_market=False)

        position = self.positions.get(symbol, 0.0)
        if (reduce_only or close_position) and position == 0:
            raise ApiError(-2022, "ReduceOnly Order is rejected")

        if order_type == "STOP_MARKET" and trigger_price > 0:
            if position > 0 and trigger_price >= self.mark_price:
                raise ApiError(-2021, "Order would immediately trigger")
            if position < 0 and trigger_price <= self.mark_price:
                raise ApiError(-2021, "Order would immediately trigger")

        order_id = self._new_id()
        client_id = params["clientAlgoId"]
        order = {
            "orderId": order_id, "algoId": order_id,
            "clientOrderId": client_id, "clientAlgoId": client_id,
            "symbol": symbol, "side": side, "type": order_type,
            "price": str(params.get("price") or 0.0),
            "stopPrice": str(trigger_price), "triggerPrice": str(trigger_price),
            "origQty": str(quantity), "executedQty": "0", "avgPrice": "0",
            "status": "NEW", "algoStatus": "NEW",
            "reduceOnly": reduce_only, "closePosition": close_position,
            "timeInForce": params.get("timeInForce", "GTC"),
            "_algo": True,
        }
        self.orders[order_id] = order
        return order

    def cancel_algo_order(self, symbol, algo_id=None, client_algo_id=None):
        self._record("cancel_algo_order", symbol=symbol, algo_id=algo_id,
                     client_algo_id=client_algo_id)
        order = self._find(symbol, algo_id, client_algo_id)
        if order is None:
            raise ApiError(-2011, "Unknown order sent")
        if order["status"] != "NEW":
            raise ApiError(-2011, "Unknown order sent")
        order["status"] = "CANCELED"
        order["algoStatus"] = "CANCELED"
        return order

    def cancel_all_algo_orders(self, symbol):
        self._record("cancel_all_algo_orders", symbol=symbol)
        for order in self.orders.values():
            if order.get("_algo") and order["symbol"] == symbol \
                    and order["status"] == "NEW":
                order["status"] = "CANCELED"
                order["algoStatus"] = "CANCELED"
        return {"code": 200}

    def open_algo_orders(self, symbol=None):
        self._record("open_algo_orders", symbol=symbol)
        return [dict(order) for order in self.orders.values()
                if order.get("_algo") and order["status"] == "NEW"
                and (symbol is None or order["symbol"] == symbol)]

    def _find(self, symbol, order_id, client_order_id):
        for order in self.orders.values():
            if order["symbol"] != symbol:
                continue
            if order_id and order["orderId"] == int(order_id):
                return order
            if client_order_id and order["clientOrderId"] == client_order_id:
                return order
        return None

    # ---------------------------------------------------- simulation controls

    def _fill(self, order_id, quantity, price):
        """Fill part or all of an order and move the position."""
        order = self.orders[order_id]
        filled = float(order["executedQty"]) + quantity
        order["executedQty"] = str(filled)
        order["avgPrice"] = str(price)
        order["status"] = ("FILLED" if filled >= float(order["origQty"]) - 1e-12
                           else "PARTIALLY_FILLED")

        symbol = order["symbol"]
        signed = quantity if order["side"] == "BUY" else -quantity
        before = self.positions.get(symbol, 0.0)
        after = before + signed
        if abs(after) < 1e-12:
            self.positions.pop(symbol, None)
            self.entry_prices.pop(symbol, None)
        else:
            self.positions[symbol] = after
            if before == 0:
                self.entry_prices[symbol] = price

        self.trades.append({
            "symbol": symbol, "side": order["side"], "qty": str(quantity),
            "price": str(price), "commission": str(quantity * price * 0.0002),
            "orderId": order_id,
        })

    def fill(self, client_order_id=None, order_id=None, quantity=None, price=None):
        """Test hook: fill a resting order."""
        for oid, order in self.orders.items():
            if ((client_order_id and order["clientOrderId"] == client_order_id)
                    or (order_id and oid == order_id)):
                remaining = float(order["origQty"]) - float(order["executedQty"])
                amount = remaining if quantity is None else quantity
                fill_price = price if price is not None else float(order["price"])
                self._fill(oid, amount, fill_price)
                return order
        raise AssertionError("no such order to fill")

    def trigger_stop(self, symbol, price):
        """Test hook: trigger a resting STOP_MARKET, closing the position."""
        for oid, order in self.orders.items():
            if (order["symbol"] == symbol and order["type"] == "STOP_MARKET"
                    and order["status"] == "NEW"):
                amount = abs(self.positions.get(symbol, 0.0))
                if amount <= 0:
                    return None
                order["origQty"] = str(amount)
                self._fill(oid, amount, price)
                return order
        return None

    def expire(self, client_order_id):
        """Test hook: the venue expired an order."""
        for order in self.orders.values():
            if order["clientOrderId"] == client_order_id:
                order["status"] = "EXPIRED"
                return order
        raise AssertionError("no such order to expire")


class FakeCatalog:
    def __init__(self, spec):
        self._spec = spec

    def get(self, symbol):
        return self._spec

    def require(self, symbol):
        return self._spec

    def refresh(self, force=False):
        return None
