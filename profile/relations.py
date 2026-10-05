"""Session-to-session relationships: the open relationship, value migration and
range extension.

THE OPEN RELATIONSHIP IS THE STATE SWITCH. It answers "how far from yesterday's
agreement does today begin?", and its answer decides which setups are eligible
for the whole session. Three classes, not two:

  INSIDE_VALUE                 opened within agreed value. Balance is the working
                               hypothesis; reversion setups are live.
  OUTSIDE_VALUE_INSIDE_RANGE   opened beyond value but still inside yesterday's
                               traded range. Ordinary - price left value but not
                               the auction's known territory.
  OUTSIDE_RANGE                opened beyond the entire prior range. A much
                               stronger and rarer statement: the auction has
                               moved somewhere it did not trade at all yesterday.

Collapsing the last two into a single "outside" loses the distinction that
matters most. Being outside value happens constantly; being outside the whole
prior range is the auction announcing imbalance, and offering a mean-reversion
trade there is fading a market that has already left.

WHY "OPEN" IS MEANINGFUL ON A PERPETUAL AT ALL. A 24/7 contract has no genuine
open - 00:00 UTC is an accounting boundary, not a re-opening auction. But
precisely because there is no gap, the prior session's CLOSE and the new
session's OPEN are the same price. So this classification carries the same
information either way, and it can be computed the instant the boundary passes
rather than waiting for an opening range to form.

What does NOT transfer from the equity-futures literature is anything depending
on a true auction restart: initial balance as a statement about the day's
character, open-drive, gap rules. Initial balance is still computed below because
it is nearly free and range extension needs it, but it is recorded as a FEATURE
for later measurement, never used as a premise. That prior is explicit rather
than assumed away.
"""
from dataclasses import dataclass

import config


@dataclass
class OpenRelationship:
    label: str
    open_price: float
    distance_from_value: float       # absolute price distance outside the VA, 0 if inside
    distance_atr: float              # the same, in ATR - the comparable measure
    side: str                        # "ABOVE" | "BELOW" | "INSIDE"

    @property
    def inside_value(self):
        return self.label == "INSIDE_VALUE"

    @property
    def outside_range(self):
        return self.label == "OUTSIDE_RANGE"

    @property
    def outside_value(self):
        """True for both outside classes - the S1 precondition."""
        return self.label != "INSIDE_VALUE"

    def as_row(self):
        return {
            "open_relationship": self.label,
            "open_side": self.side,
            "open_distance_atr": round(self.distance_atr, 4),
        }


@dataclass
class ValueMigration:
    label: str
    poc_shift: float                 # today's POC minus yesterday's, in price
    poc_shift_atr: float
    vah_shift_atr: float
    val_shift_atr: float
    overlap_fraction: float          # how much the two value areas share

    @property
    def directional(self):
        """Value moving one way with little overlap - the cleanest trend read."""
        return self.label in ("HIGHER", "LOWER")

    def as_row(self):
        return {
            "value_migration": self.label,
            "poc_shift_atr": round(self.poc_shift_atr, 4),
            "value_overlap": round(self.overlap_fraction, 4),
        }


def classify_open(open_price, prior_levels, prior_profile, atr):
    """Where a session began relative to the prior session's value and range."""
    open_price = float(open_price)
    atr = float(atr) if atr and atr > 0 else 0.0

    if prior_levels.val <= open_price <= prior_levels.vah:
        return OpenRelationship("INSIDE_VALUE", open_price, 0.0, 0.0, "INSIDE")

    if open_price > prior_levels.vah:
        side = "ABOVE"
        distance = open_price - prior_levels.vah
        beyond_range = open_price > prior_profile.high
    else:
        side = "BELOW"
        distance = prior_levels.val - open_price
        beyond_range = open_price < prior_profile.low

    label = "OUTSIDE_RANGE" if beyond_range else "OUTSIDE_VALUE_INSIDE_RANGE"
    return OpenRelationship(
        label=label,
        open_price=open_price,
        distance_from_value=distance,
        distance_atr=(distance / atr) if atr > 0 else 0.0,
        side=side,
    )


def _overlap_fraction(low_a, high_a, low_b, high_b):
    """Shared fraction of two price bands, relative to the narrower one.

    Relative to the NARROWER band deliberately: a tight value area sitting wholly
    inside a wide one is fully contained - overlap 1.0 - which is the honest
    reading. Normalising by the wider band would report that as partial overlap
    and hide the containment relationship the classifier needs.
    """
    overlap = min(high_a, high_b) - max(low_a, low_b)
    if overlap <= 0:
        return 0.0
    narrower = min(high_a - low_a, high_b - low_b)
    if narrower <= 0:
        return 0.0
    return min(1.0, overlap / narrower)


def classify_migration(current_levels, prior_levels, atr):
    """How agreement itself moved between two sessions.

    Sustained one-directional migration with low overlap is the cleanest
    definition of trend the profile offers - cleaner than any price-based trend
    read, because it is a statement about where business was actually done rather
    than about where price travelled.
    """
    atr = float(atr) if atr and atr > 0 else 0.0

    poc_shift = current_levels.poc_price - prior_levels.poc_price
    vah_shift = current_levels.vah - prior_levels.vah
    val_shift = current_levels.val - prior_levels.val
    overlap = _overlap_fraction(current_levels.val, current_levels.vah,
                               prior_levels.val, prior_levels.vah)

    def in_atr(value):
        return (value / atr) if atr > 0 else 0.0

    # INSIDE and OUTSIDE are checked first: a contained or engulfing value area
    # is a statement about conviction (or its absence) that the directional
    # labels would misreport as a small shift.
    if (current_levels.val >= prior_levels.val
            and current_levels.vah <= prior_levels.vah):
        label = "INSIDE"
    elif (current_levels.val <= prior_levels.val
            and current_levels.vah >= prior_levels.vah):
        label = "OUTSIDE"
    elif overlap <= 0.0:
        label = "HIGHER" if poc_shift > 0 else "LOWER"
    elif abs(in_atr(poc_shift)) < 0.10:
        label = "UNCHANGED"
    elif poc_shift > 0:
        label = "OVERLAPPING_HIGHER"
    else:
        label = "OVERLAPPING_LOWER"

    return ValueMigration(
        label=label,
        poc_shift=poc_shift,
        poc_shift_atr=in_atr(poc_shift),
        vah_shift_atr=in_atr(vah_shift),
        val_shift_atr=in_atr(val_shift),
        overlap_fraction=overlap,
    )


def initial_balance(candles, session_start, minutes=60):
    """High/low of the session's first `minutes` - a FEATURE, not a premise.

    On a contract that never stops trading this has no auction meaning: there is
    no opening rotation establishing a day's parameters, because the previous
    session's participants never left. It is computed because range extension is
    defined against it and both are nearly free, and it is recorded so Phase 3 can
    test whether it carries anything on a perpetual. The explicit prior is that it
    does not.
    """
    window_end = int(session_start) + int(minutes) * 60_000
    window = [candle for candle in candles
              if session_start <= candle.open_time < window_end]
    if not window:
        return None
    return {
        "ib_high": max(candle.high for candle in window),
        "ib_low": min(candle.low for candle in window),
        "ib_minutes": minutes,
        "ib_candles": len(window),
    }


def range_extension(profile, ib):
    """How far the session pushed beyond its initial balance, per side, in IB widths."""
    if not ib:
        return {"extension_up": 0.0, "extension_down": 0.0, "extended": None}
    width = ib["ib_high"] - ib["ib_low"]
    if width <= 0:
        return {"extension_up": 0.0, "extension_down": 0.0, "extended": None}

    up = max(0.0, profile.high - ib["ib_high"]) / width
    down = max(0.0, ib["ib_low"] - profile.low) / width
    extended = None
    if up > 0.10 or down > 0.10:
        extended = "UP" if up > down else "DOWN" if down > up else "BOTH"
    return {"extension_up": up, "extension_down": down, "extended": extended}


def describe(open_rel, migration, shape):
    """One-line auction context for logs and the heartbeat."""
    parts = [f"open={open_rel.label}"]
    if migration is not None:
        parts.append(f"value={migration.label}")
    if shape is not None:
        parts.append(f"shape={shape.label}({shape.auction_state})")
    return " ".join(parts)
