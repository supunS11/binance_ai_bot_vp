"""Exchange filter arithmetic: rounding prices and quantities so the venue
accepts them, and so that rounding never works against us.

WHY DECIMAL AND NOT FLOAT. Binance rejects a price or quantity whose precision
exceeds the symbol's tickSize/stepSize with error -1111 ("Precision is over the
maximum defined for this asset"). Float arithmetic reliably produces exactly
that: 0.1 + 0.2 == 0.30000000000000004, and round(2.675, 2) == 2.67. A tickSize
of 0.001 on a price like 1.115 is enough to trip it. Every quantisation in this
module therefore goes through Decimal with an explicit rounding direction, and
the result is returned as a Decimal-backed string for the wire plus a float for
arithmetic.

WHY DIRECTION MATTERS MORE THAN PRECISION. Rounding to the nearest tick is the
obvious choice and it is wrong in three of the four places it is used:

  entry     round so the order stays PASSIVE - down for a buy, up for a sell.
            Rounding a buy up can cross the spread and turn a maker fill into a
            taker fill, or get a post-only order rejected outright.
  stop      round AWAY from entry. A stop rounded toward entry is a tighter stop
            than the one the setup asked for, which silently increases position
            size and makes realised R larger than intended in both directions.
  target    round TOWARD entry. A target rounded away is marginally less likely
            to fill, and an unfilled target on a mean-reversion trade usually
            means giving the move back.
  quantity  always round DOWN. Rounding up exceeds the risk budget, which is the
            one error that compounds.

Each of those is a deliberate asymmetry, not a convention, so each has its own
function rather than a shared `round_to_tick(x)` with a mode argument that a
caller can forget to pass.
"""
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext


def _dec(value):
    """Decimal from anything, via str so float artefacts do not survive.

    Decimal(0.1) is 0.1000000000000000055511151231257827, while
    Decimal(str(0.1)) is exactly 0.1. Always the latter.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _quantise(value, step, rounding):
    """Snap value onto the lattice defined by step, in the given direction."""
    value, step = _dec(value), _dec(step)
    if step <= 0:
        return value
    with localcontext() as ctx:
        ctx.prec = 34
        return (value / step).to_integral_value(rounding=rounding) * step


def _format(value, step):
    """Wire format: fixed-point, with exactly the step's decimal places.

    Binance accepts a plain decimal string. Scientific notation - which
    str(Decimal('1E-8')) produces - is rejected, so exponents are normalised
    away here rather than at each call site.
    """
    value, step = _dec(value), _dec(step)
    places = max(0, -step.as_tuple().exponent)
    return f"{value:.{places}f}"


# ------------------------------------------------------------------- prices

def round_price_passive(price, tick_size, side):
    """Quantise an ENTRY price so the order stays on the passive side.

    BUY  -> down (bid side: a lower price never crosses)
    SELL -> up   (ask side: a higher price never crosses)

    This is what keeps a post-only (GTX) entry from being rejected for crossing,
    and is the difference between paying maker and taker on the entry leg.
    """
    rounding = ROUND_DOWN if side.upper() == "BUY" else ROUND_UP
    return _quantise(price, tick_size, rounding)


def round_stop_price(stop_price, tick_size, position_side):
    """Quantise a STOP so it can only end up wider, never tighter.

    A LONG's stop sits below entry, so rounding DOWN moves it further away; a
    SHORT's sits above, so rounding UP does. Tightening a stop by a tick is not
    cosmetic: position size is computed from the stop distance, so a tighter
    stop means a larger position than the risk budget authorised.
    """
    rounding = ROUND_DOWN if position_side.upper() == "LONG" else ROUND_UP
    return _quantise(stop_price, tick_size, rounding)


def round_target_price(target_price, tick_size, position_side):
    """Quantise a TARGET so it can only end up nearer, never further.

    A LONG's target is above entry, so rounding DOWN brings it closer and
    marginally more fillable; a SHORT's is below, so rounding UP does. The cost
    is a fraction of a tick of profit; the benefit is not missing the exit.
    """
    rounding = ROUND_DOWN if position_side.upper() == "LONG" else ROUND_UP
    return _quantise(target_price, tick_size, rounding)


# --------------------------------------------------------------- quantities

def round_quantity(quantity, step_size):
    """Quantise a quantity DOWN, always.

    Rounding up would place more risk than the sizing calculation authorised.
    On a small stepSize the give-up is negligible; on a coarse one this can zero
    the order out entirely, which the notional check below is there to catch.
    """
    return _quantise(quantity, step_size, ROUND_DOWN)


# ------------------------------------------------------------------ validity

class FilterRejection(Exception):
    """An order that the venue's own filters would refuse.

    Raised before anything is sent. Catching a rejection locally is strictly
    better than learning it from an API error: the error costs a round trip, a
    rate-limit weight, and a log line that looks like a fault rather than a
    normal skip.
    """

    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def check_price(price, spec):
    """PRICE_FILTER: within [minPrice, maxPrice] and on the tick lattice."""
    price = _dec(price)
    if spec.min_price and price < _dec(spec.min_price):
        raise FilterRejection("PRICE_BELOW_MIN",
                              f"{price} < {spec.min_price}")
    if spec.max_price and price > _dec(spec.max_price):
        raise FilterRejection("PRICE_ABOVE_MAX",
                              f"{price} > {spec.max_price}")
    if _dec(spec.tick_size) > 0:
        remainder = (price / _dec(spec.tick_size)) % 1
        if remainder != 0:
            raise FilterRejection("PRICE_OFF_TICK",
                                  f"{price} not a multiple of {spec.tick_size}")
    return True


def check_quantity(quantity, spec, market_order=False):
    """LOT_SIZE, or MARKET_LOT_SIZE when the order is a market order.

    The two filters are genuinely different - MARKET_LOT_SIZE usually caps
    maxQty far lower than LOT_SIZE, because a market order consumes the book.
    Using LOT_SIZE for a market order is a real and easy mistake.
    """
    quantity = _dec(quantity)
    min_qty = _dec(spec.market_min_qty if market_order else spec.min_qty)
    max_qty = _dec(spec.market_max_qty if market_order else spec.max_qty)
    step = _dec(spec.market_step_size if market_order else spec.step_size)

    if quantity <= 0:
        raise FilterRejection("QTY_ZERO", "quantity rounded to zero")
    if min_qty > 0 and quantity < min_qty:
        raise FilterRejection("QTY_BELOW_MIN", f"{quantity} < {min_qty}")
    if max_qty > 0 and quantity > max_qty:
        raise FilterRejection("QTY_ABOVE_MAX", f"{quantity} > {max_qty}")
    if step > 0 and (quantity / step) % 1 != 0:
        raise FilterRejection("QTY_OFF_STEP",
                              f"{quantity} not a multiple of {step}")
    return True


def check_notional(price, quantity, spec):
    """MIN_NOTIONAL: price * quantity must clear the floor.

    This is the filter that most often bites after a correct rounding pass - a
    quantity rounded down to stepSize can fall under the notional minimum even
    though both the price and the quantity are individually valid.
    """
    notional = _dec(price) * _dec(quantity)
    if spec.min_notional and notional < _dec(spec.min_notional):
        raise FilterRejection("NOTIONAL_BELOW_MIN",
                              f"{notional} < {spec.min_notional}")
    return True


def check_percent_price(price, mark_price, spec, side):
    """PERCENT_PRICE: a limit price must sit inside a band around the mark.

    Binance bounds how far from the mark a resting order may be placed. A
    far-away entry on a level the profile found yesterday can legitimately fall
    outside that band, in which case the order is refused with -4131/-2010 and
    the correct response is to skip the setup, not to move the level.
    """
    price, mark_price = _dec(price), _dec(mark_price)
    if mark_price <= 0:
        return True
    up = _dec(spec.multiplier_up) if spec.multiplier_up else None
    down = _dec(spec.multiplier_down) if spec.multiplier_down else None
    if side.upper() == "BUY" and down is not None and price < mark_price * down:
        raise FilterRejection("PRICE_BELOW_PERCENT_BAND",
                              f"{price} < mark {mark_price} * {down}")
    if side.upper() == "SELL" and up is not None and price > mark_price * up:
        raise FilterRejection("PRICE_ABOVE_PERCENT_BAND",
                              f"{price} > mark {mark_price} * {up}")
    return True


def check_stop_triggerable(stop_price, mark_price, position_side):
    """Refuse a stop that the venue would trigger the instant it is accepted.

    Binance returns -2021 "Order would immediately trigger" when a stop sits on
    the already-satisfied side of the trigger price. For a LONG the stop is a
    sell below the mark, so a stop at or above the mark fires at once; for a
    SHORT the mirror. Catching it here matters because the failure happens
    AFTER the entry has filled - the alternative is an open position whose
    protective order was silently refused.
    """
    stop_price, mark_price = _dec(stop_price), _dec(mark_price)
    if mark_price <= 0:
        return True
    if position_side.upper() == "LONG" and stop_price >= mark_price:
        raise FilterRejection(
            "STOP_WOULD_TRIGGER",
            f"long stop {stop_price} >= mark {mark_price}")
    if position_side.upper() == "SHORT" and stop_price <= mark_price:
        raise FilterRejection(
            "STOP_WOULD_TRIGGER",
            f"short stop {stop_price} <= mark {mark_price}")
    return True


# -------------------------------------------------------------- wire helpers

def price_str(price, spec):
    return _format(price, spec.tick_size)


def quantity_str(quantity, spec):
    return _format(quantity, spec.step_size)


def ticks(count, spec):
    """`count` ticks as a Decimal, for buffer arithmetic that must stay exact."""
    return _dec(count) * _dec(spec.tick_size)
