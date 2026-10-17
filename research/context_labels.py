"""Higher-timeframe context labels (UP / DOWN / ROTATION) for testing S4-OFR's
direction filter - research only, never imported by the live bot.

Two independent definitions, kept separate on purpose because only one of them is
stated mechanically in the source material:

VALUE MIGRATION (AK Order Flow guide, "Balance and Imbalance", after Dalton) - the
  PRIMARY definition. Imbalance: "Value areas migrate day after day, consistently
  higher or consistently lower." Balance: "Day after day, the value areas overlap."
  The guide's own worked example (days 7-9) calls slightly overlapping, steadily
  higher value areas "migrating higher", so OVERLAPPING_HIGHER counts with HIGHER;
  INSIDE / OUTSIDE / UNCHANGED (value contained, engulfing, or not moving) are
  balance. Labels come from profile/relations.classify_migration, the same function
  the live bot already uses for one-day migration.

SWING STRUCTURE (Trader Dale's daily plan reads "uptrend, downtrend or rotation" on a
  240-minute or Daily chart, but gives no mechanical rule). The conventional
  price-action definition - higher highs AND higher lows = uptrend - is used as the
  comparison. It is NOT stated in the PDFs and is reported as such.

Rotation is a full label, not a missing value: per Dale, "in case there is a rotation,
you can take both long and short trades." None means not enough history to say.
"""

UP, DOWN, ROTATION = "UP", "DOWN", "ROTATION"

_MIGRATING_UP = ("HIGHER", "OVERLAPPING_HIGHER")
_MIGRATING_DOWN = ("LOWER", "OVERLAPPING_LOWER")


def value_migration_context(labels_newest_first, days):
    """UP / DOWN when the last `days` day-over-day migration labels all moved the
    same way, ROTATION otherwise, None when fewer than `days` labels exist."""
    recent = [label for label in labels_newest_first[:days] if label]
    if len(recent) < days:
        return None
    if all(label in _MIGRATING_UP for label in recent):
        return UP
    if all(label in _MIGRATING_DOWN for label in recent):
        return DOWN
    return ROTATION


def swing_points(bars, strength=2):
    """Fractal swing highs/lows: a bar whose high (low) is strictly above (below)
    the `strength` bars on BOTH sides. Returns ([highs], [lows]) as prices, oldest
    first. Only bars with `strength` bars after them can qualify, so a swing is
    never confirmed by a bar that has not closed yet."""
    highs, lows = [], []
    for i in range(strength, len(bars) - strength):
        neighbours = bars[i - strength:i] + bars[i + 1:i + 1 + strength]
        if all(bars[i].high > other.high for other in neighbours):
            highs.append(bars[i].high)
        if all(bars[i].low < other.low for other in neighbours):
            lows.append(bars[i].low)
    return highs, lows


def swing_structure_context(bars, strength=2):
    """UP: last two swing highs AND last two swing lows both higher. DOWN: both
    lower. ROTATION: anything else (mixed, equal). None: fewer than two of either."""
    highs, lows = swing_points(bars, strength)
    if len(highs) < 2 or len(lows) < 2:
        return None
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return UP
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return DOWN
    return ROTATION


def allows(context, side):
    """Dale's rule: uptrend -> longs only, downtrend -> shorts only, rotation ->
    both. An unmeasured context (None) refuses nothing - same as live's NEUTRAL."""
    if context == UP:
        return side == "BUY"
    if context == DOWN:
        return side == "SELL"
    return True
