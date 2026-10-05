"""Confirmation vocabulary: the price-action evidence a setup requires at its
level before committing.

WHY CONFIRMATION EXISTS AT ALL. Every setup here is an "if price reaches level X"
bet, and reaching a level is not the same as reacting to it. Without
confirmation, a POC fade is a limit order into a level that price may simply walk
through - and the profile's own logic says the POC is where price LINGERS, so
walking through is common. Confirmation trades a worse entry price for evidence
that the reaction is actually happening.

WHY EACH SIGNAL IS INDEPENDENTLY TOGGLEABLE AND COUNTED. Setups require N of the
available confirmations rather than a fixed combination, so Phase 3 can ablate
each one and find which actually earn their place. A hard-coded conjunction of
four conditions is untestable: when it underperforms you cannot tell which member
is responsible.

Each function returns a Confirmation carrying its own strength, so a caller can
require a count, a specific signal, or a minimum total strength without this
module knowing which policy is in force.

NOTE ON DELTA. Delta divergence is the only non-geometric signal here, and it is
the one genuinely profile-native input of the four: it reads whether aggressive
flow at the level opposes the price move, using taker volume that comes free in
every kline. It is included because it is the cheapest order-flow read available
at full history, and it defaults OFF like every other opinion-bearing component.
"""
from dataclasses import dataclass

import config


@dataclass
class Confirmation:
    name: str
    present: bool
    strength: float = 0.0        # 0..1, comparable across signals
    detail: str = ""

    def __bool__(self):
        return self.present


def wick_rejection(candle, level, direction, atr, min_atr=None):
    """Price pierced the level and closed back on the origin side.

    The clearest single-candle rejection there is: the auction tried the level,
    found no continuation, and gave the ground back within the bar. Requiring a
    MINIMUM pierce depth matters - a one-tick poke is noise, and without a floor
    this fires on almost every candle that touches the level.

    direction is the intended trade direction. A SELL expects price to have
    pierced ABOVE the level and closed back below it.
    """
    min_atr = config.CONFIRM_WICK_MIN_ATR if min_atr is None else min_atr
    atr = float(atr) if atr and atr > 0 else 0.0
    threshold = atr * float(min_atr)

    if direction == "SELL":
        pierce = candle.high - level
        closed_back = candle.close < level
    else:
        pierce = level - candle.low
        closed_back = candle.low < level and candle.close > level

    present = closed_back and pierce >= threshold and pierce > 0
    strength = 0.0
    if present and atr > 0:
        strength = min(1.0, pierce / (atr * 0.5))

    return Confirmation(
        "wick_rejection", present, strength,
        f"pierce={pierce:.6g} threshold={threshold:.6g}",
    )


def engulfing(candles, direction, min_body_ratio=None):
    """The last candle's body covers the previous candle's body, in our direction.

    A standard two-candle reversal read, included because the source material
    names it explicitly. Requiring a body RATIO rather than mere containment
    keeps a large candle following a doji - which engulfs trivially and means
    nothing - from qualifying.
    """
    min_body_ratio = (config.CONFIRM_ENGULF_MIN_BODY_RATIO
                      if min_body_ratio is None else min_body_ratio)
    if len(candles) < 2:
        return Confirmation("engulfing", False, 0.0, "insufficient candles")

    previous, current = candles[-2], candles[-1]
    if previous.body <= 0:
        return Confirmation("engulfing", False, 0.0, "prior body is zero")

    ratio = current.body / previous.body
    covers = (max(current.open, current.close) >= max(previous.open, previous.close)
              and min(current.open, current.close) <= min(previous.open, previous.close))
    right_way = current.bullish if direction == "BUY" else not current.bullish
    opposed_prior = (not previous.bullish) if direction == "BUY" else previous.bullish

    present = covers and right_way and opposed_prior and ratio >= float(min_body_ratio)
    return Confirmation(
        "engulfing", present, min(1.0, ratio / 2.0) if present else 0.0,
        f"body_ratio={ratio:.3f}",
    )


def consecutive_closes(candles, direction, count=None):
    """N closes in a row in the trade's direction - sellers or buyers in control.

    The source material's "multiple red candles forming right after". Weakest of
    the four on its own, and the cheapest to compute; its value is as a
    tie-breaker inside an N-of-M requirement.
    """
    count = config.CONFIRM_CONSECUTIVE_CANDLES if count is None else count
    count = max(1, int(count))
    if len(candles) < count:
        return Confirmation("consecutive_closes", False, 0.0, "insufficient candles")

    recent = candles[-count:]
    if direction == "BUY":
        present = all(candle.bullish for candle in recent)
    else:
        present = all(not candle.bullish for candle in recent)

    return Confirmation(
        "consecutive_closes", present, min(1.0, count / 4.0) if present else 0.0,
        f"count={count}",
    )


def delta_divergence(candles, direction, min_fraction=None):
    """Aggressive flow at the level opposes the price move that produced it.

    The profile-native confirmation. Signed taker flow is exact in every kline
    (2*taker_buy_base - volume), so this reads whether the candle that touched the
    level was driven by aggressive buyers or sellers.

    For a SELL confirmation we want the touch candle to show NET SELLING despite
    having traded up into the level - buyers lifted the offer to get there, and
    then sellers absorbed it. That absorption is the mechanism a rejection is made
    of, and price alone cannot see it.
    """
    min_fraction = (config.CONFIRM_DELTA_DIVERGENCE_MIN
                    if min_fraction is None else min_fraction)
    if not candles:
        return Confirmation("delta_divergence", False, 0.0, "no candles")

    candle = candles[-1]
    if candle.volume <= 0:
        return Confirmation("delta_divergence", False, 0.0, "zero volume")

    normalised = candle.delta_base / candle.volume      # -1 .. +1
    if direction == "SELL":
        present = normalised <= -float(min_fraction)
    else:
        present = normalised >= float(min_fraction)

    return Confirmation(
        "delta_divergence", present, min(1.0, abs(normalised)),
        f"delta_frac={normalised:+.4f}",
    )


def collect(candles, level, direction, atr):
    """Evaluate every confirmation at a level. Returns a list, never filtered.

    Unfired confirmations are returned too, because the journal records which
    evidence was ABSENT as well as which was present - that is what makes the
    Phase 3 ablation possible from stored rows rather than requiring a re-run.
    """
    if not candles:
        return []
    return [
        wick_rejection(candles[-1], level, direction, atr),
        engulfing(candles, direction),
        consecutive_closes(candles, direction),
        delta_divergence(candles, direction),
    ]


def summarise(confirmations, minimum=1):
    """Reduce a confirmation list to a decision plus a recordable summary."""
    present = [c for c in confirmations if c.present]
    return {
        "confirmed": len(present) >= int(minimum),
        "count": len(present),
        "required": int(minimum),
        "names": [c.name for c in present],
        "strength": round(sum(c.strength for c in present), 4),
        "detail": {c.name: {"present": c.present, "detail": c.detail}
                   for c in confirmations},
    }
