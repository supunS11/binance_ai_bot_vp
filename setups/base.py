"""Shared setup types: the Candidate, its lifecycle, and the typed rejection.

WHY REJECTION IS A TYPE AND NOT A LOG CALL. Every detector and every gate in this
system returns either a Candidate or a Rejection - there is no path that declines
a trade by returning None and writing a log line. The consequence is that the
journal layer receives every refusal as structured data, and a reason added in six
months is observable the day it is written.

The specific failure this avoids: a reject journal driven by a hand-maintained
list of "reasons we record". Such a list always falls behind the gates themselves,
and the reasons it omits become invisible - not under-reported, but absent, so
nobody notices they are missing. The journal here writes whatever RejectReason it
is handed and has no allowlist. tests/test_reject_coverage.py asserts every enum
member is reachable.

TARGETS. Two modes, both implemented, because the choice is an open empirical
question and not a matter of taste. `fixed_r` tests the simple claim - a fixed
multiple of risk - as stated. `structural` uses the levels the profile actually
produced, which is the mode auction theory argues for: a fixed 2R target will
regularly sit inside a high-volume shelf that price has no reason to cross, while
the opposite value edge or an untested POC is somewhere the auction has unfinished
business. `fixed_r` is the default so the simple version is measured honestly
first.
"""
from dataclasses import dataclass, field
from enum import Enum

import config


class SetupState(str, Enum):
    """A candidate's lifecycle. Every transition is journaled.

    INVALIDATED is deliberately distinct from REJECTED: rejected means a gate
    refused it at birth, invalidated means it was legitimately armed and the
    market then removed the premise - a breakout pullback that failed to hold, a
    level that price closed decisively through. Conflating them would hide the
    difference between "we filtered this out" and "the setup died", and only the
    second tells you anything about the market.
    """
    ARMED = "ARMED"                # context is right, waiting for price
    PENDING = "PENDING"            # price is at the level, awaiting confirmation
    TRIGGERED = "TRIGGERED"        # confirmed, order being placed
    WORKING = "WORKING"            # entry order resting on the book
    FILLED = "FILLED"              # position open
    INVALIDATED = "INVALIDATED"    # premise removed by the market
    REJECTED = "REJECTED"          # refused by a gate
    EXPIRED = "EXPIRED"            # session boundary or entry timeout
    CLOSED = "CLOSED"              # position finished


class RejectReason(str, Enum):
    """Every way this system can decline to trade.

    Grouped by what they say about the world, because the groups get analysed
    differently: DATA/PROFILE reasons describe measurement quality and their rate
    is a health metric, CONTEXT reasons are the state machine working as intended
    and their rate is expected to be high, and GEOMETRY reasons indicate the
    setup's own arithmetic did not survive contact with the venue's filters.
    """
    # --- data quality -----------------------------------------------------
    NO_DATA = "NO_DATA"
    INSUFFICIENT_CANDLES = "INSUFFICIENT_CANDLES"
    DATA_GAP = "DATA_GAP"
    NO_PRIOR_PROFILE = "NO_PRIOR_PROFILE"

    # --- profile quality --------------------------------------------------
    PROFILE_THIN = "PROFILE_THIN"
    PROFILE_NO_LEVELS = "PROFILE_NO_LEVELS"
    POC_UNSTABLE = "POC_UNSTABLE"
    POC_NOT_PROMINENT = "POC_NOT_PROMINENT"
    VA_BOUNDS_UNSTABLE = "VA_BOUNDS_UNSTABLE"
    VA_DEGENERATE = "VA_DEGENERATE"
    LVN_NOT_QUALIFIED = "LVN_NOT_QUALIFIED"
    DEVELOPING_IMMATURE = "DEVELOPING_IMMATURE"

    # --- auction context (the state machine working as designed) -----------
    SETUP_DISABLED = "SETUP_DISABLED"
    OPEN_RELATIONSHIP_WRONG = "OPEN_RELATIONSHIP_WRONG"
    SHAPE_NOT_ELIGIBLE = "SHAPE_NOT_ELIGIBLE"
    SHAPE_OPPOSED = "SHAPE_OPPOSED"
    PRICE_NOT_AT_LEVEL = "PRICE_NOT_AT_LEVEL"
    NO_EXCURSION = "NO_EXCURSION"
    EXCURSION_TOO_SMALL = "EXCURSION_TOO_SMALL"
    ACCEPTANCE_PENDING = "ACCEPTANCE_PENDING"
    ACCEPTANCE_CONTRADICTS = "ACCEPTANCE_CONTRADICTS"
    # Acceptance-level volume outside value, then reclaimed. A real auction event,
    # stronger than a thin-spike rejection, and deliberately out of v1 scope - but
    # classified and journaled so its frequency can be measured.
    FAILED_AUCTION_NOT_TRADED = "FAILED_AUCTION_NOT_TRADED"
    # The discriminator could not be MEASURED - no volume baseline independent of the
    # excursion itself. Distinct from ACCEPTANCE_PENDING, which means it was measured
    # and the answer was ambiguous. Kept separate because the two say different things
    # about the market: PENDING is a real middling auction, this is missing data.
    ACCEPTANCE_NOT_MEASURABLE = "ACCEPTANCE_NOT_MEASURABLE"
    NOT_CONFIRMED = "NOT_CONFIRMED"
    PULLBACK_TOO_DEEP = "PULLBACK_TOO_DEEP"
    NO_CONTINUATION = "NO_CONTINUATION"
    DELTA_OPPOSED = "DELTA_OPPOSED"
    DELTA_OPPOSED_MULTIBIN = "DELTA_OPPOSED_MULTIBIN"
    HTF_VALUE_OPPOSED = "HTF_VALUE_OPPOSED"

    # --- arbitration ------------------------------------------------------
    SUPPRESSED_BY_ARBITER = "SUPPRESSED_BY_ARBITER"
    SESSION_SETUP_LIMIT = "SESSION_SETUP_LIMIT"
    ZONE_CLAIMED_BY_S4 = "ZONE_CLAIMED_BY_S4"
    ALREADY_IN_POSITION = "ALREADY_IN_POSITION"

    # --- geometry and venue filters ---------------------------------------
    # Stop or target on the wrong side of entry for the stated direction. Should be
    # impossible from a correct detector, which is exactly why it is asserted: every
    # other risk measure uses absolute distances, so an inverted level set produces a
    # healthy-looking positive R and passes every other gate.
    GEOMETRY_INVALID = "GEOMETRY_INVALID"
    STOP_TOO_TIGHT = "STOP_TOO_TIGHT"
    STOP_TOO_WIDE = "STOP_TOO_WIDE"
    STOP_INSIDE_HVN = "STOP_INSIDE_HVN"
    STOP_WOULD_TRIGGER = "STOP_WOULD_TRIGGER"
    TARGET_TOO_CLOSE = "TARGET_TOO_CLOSE"
    TARGET_BEHIND_HVN = "TARGET_BEHIND_HVN"
    REWARD_BELOW_MINIMUM = "REWARD_BELOW_MINIMUM"
    FILTER_REJECTED = "FILTER_REJECTED"
    NOTIONAL_TOO_SMALL = "NOTIONAL_TOO_SMALL"
    QTY_ZERO = "QTY_ZERO"

    # --- risk and operations ----------------------------------------------
    RISK_LIMIT_POSITIONS = "RISK_LIMIT_POSITIONS"
    RISK_LIMIT_DIRECTION = "RISK_LIMIT_DIRECTION"
    RISK_LIMIT_DAILY_LOSS = "RISK_LIMIT_DAILY_LOSS"
    RISK_LIMIT_CONSECUTIVE = "RISK_LIMIT_CONSECUTIVE"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    NOT_FIRST_TEST = "NOT_FIRST_TEST"
    NO_ORDER_FLOW_ZONE = "NO_ORDER_FLOW_ZONE"
    NO_ORDER_FLOW_SIGNAL = "NO_ORDER_FLOW_SIGNAL"
    NO_FOLLOW_THROUGH = "NO_FOLLOW_THROUGH"
    TIER_NOT_MET = "TIER_NOT_MET"
    INVALIDATED = "INVALIDATED"
    ENTRY_TOO_FAR = "ENTRY_TOO_FAR"
    BIAS_OPPOSED = "BIAS_OPPOSED"
    ENTRY_NOT_PASSIVE = "ENTRY_NOT_PASSIVE"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    FUNDING_WINDOW = "FUNDING_WINDOW"
    TRADING_DISABLED = "TRADING_DISABLED"
    KILL_SWITCH = "KILL_SWITCH"
    EXCHANGE_ERROR = "EXCHANGE_ERROR"


@dataclass
class Rejection:
    """A refusal, with enough context to analyse it without re-running anything."""
    reason: RejectReason
    setup: str
    symbol: str
    detail: str = ""
    direction: str = ""
    level_price: float = 0.0
    context: dict = field(default_factory=dict)

    @property
    def is_rejection(self):
        return True

    def as_row(self):
        return {
            "reason": str(self.reason.value),
            "setup": self.setup,
            "symbol": self.symbol,
            "direction": self.direction,
            "detail": self.detail,
            "level_price": self.level_price,
            **{f"ctx_{key}": value for key, value in self.context.items()},
        }


def reject(reason, setup, symbol, detail="", direction="", level_price=0.0, **context):
    """Construct a Rejection. The only way a detector or gate says no."""
    return Rejection(reason=reason, setup=setup, symbol=symbol, detail=detail,
                     direction=direction, level_price=level_price, context=context)


MARKET_ENTRY = "MARKET"


@dataclass
class Candidate:
    """A tradeable setup: what to trade, where, and every input that justified it.

    `attributes` carries the measured features - excursion size, confirmation
    detail, shape, stability, delta - and is written to the journal verbatim. It
    is the substrate every later phase is computed from, which is why it holds raw
    measurements rather than booleans: a stored threshold comparison cannot be
    re-swept, a stored value can.
    """
    setup: str
    symbol: str
    direction: str                  # "BUY" | "SELL"
    state: SetupState = SetupState.ARMED

    # Geometry, all in price terms.
    level_price: float = 0.0        # the profile level being traded
    level_kind: str = ""            # "POC" | "LVN" | "VAH" | "VAL"
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0

    # Context for the journal and for research.
    session_id: str = ""
    as_of: int = 0
    atr: float = 0.0
    attributes: dict = field(default_factory=dict)
    profile_snapshot: dict = field(default_factory=dict)

    # Filled in by risk.py once equity is known.
    quantity: float = 0.0
    notional: float = 0.0
    risk_amount: float = 0.0

    # Filled in by execution.
    client_order_id: str = ""
    entry_order_id: int = 0
    stop_order_id: int = 0
    target_order_id: int = 0
    filled_price: float = 0.0
    filled_qty: float = 0.0

    @property
    def is_rejection(self):
        return False

    @property
    def position_side(self):
        return "LONG" if self.direction == "BUY" else "SHORT"

    @property
    def risk_distance(self):
        """Price distance from entry to stop - the R unit for this trade."""
        return abs(self.entry_price - self.stop_price)

    @property
    def reward_distance(self):
        return abs(self.target_price - self.entry_price)

    @property
    def r_multiple(self):
        risk = self.risk_distance
        return (self.reward_distance / risk) if risk > 0 else 0.0

    @property
    def risk_pct_of_price(self):
        """Stop distance as a fraction of entry - how fee cost compares to risk."""
        return (self.risk_distance / self.entry_price) if self.entry_price > 0 else 0.0

    def round_trip_cost_r(self, maker=None, taker=None):
        """Fees expressed in R, which is the only way they are comparable.

        Taker is assumed on BOTH legs even when the entry is posted, because the
        stop always exits at market and an entry that needs a market fallback
        pays taker too. Overstating cost slightly is the safe direction: it makes
        every measured edge conservative rather than flattering.
        """
        maker = config.FEE_MAKER if maker is None else maker
        taker = config.FEE_TAKER if taker is None else taker
        round_trip = float(taker) * 2.0
        risk_fraction = self.risk_pct_of_price
        return (round_trip / risk_fraction) if risk_fraction > 0 else float("inf")

    def net_r_at_target(self):
        """R actually collected if the target fills - gross R minus fee cost."""
        return self.r_multiple - self.round_trip_cost_r()

    def as_row(self):
        return {
            "setup": self.setup,
            "symbol": self.symbol,
            "direction": self.direction,
            "state": self.state.value,
            "session_id": self.session_id,
            "as_of": self.as_of,
            "level_kind": self.level_kind,
            "level_price": self.level_price,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "risk_distance": self.risk_distance,
            "reward_distance": self.reward_distance,
            "r_multiple": round(self.r_multiple, 4),
            "risk_pct_of_price": round(self.risk_pct_of_price, 6),
            "cost_r": round(self.round_trip_cost_r(), 4),
            "net_r_at_target": round(self.net_r_at_target(), 4),
            "atr": self.atr,
            "quantity": self.quantity,
            "notional": self.notional,
            **{f"attr_{key}": value for key, value in self.attributes.items()},
        }


# ------------------------------------------------------------------- flow

def multibin_delta_normalized(profile, level_price, window_bins=None):
    """Normalised taker imbalance (delta/volume, -1..+1) over a WINDOW of bins.

    `gates.checks.delta_opposed` (finding 18: no rescue) reads flow from the single
    bin the level falls in. That is a narrow read - real order flow around a level
    spreads over several ticks, not one bin - so this widens the same question to
    `window_bins` on each side without changing what it measures: signed volume over
    total volume, still bounded to [-1, 1].

    Summing volume and delta separately BEFORE dividing (rather than averaging each
    bin's own normalised ratio) is deliberate: it weights by how much actually
    traded in each bin, so a wide, thin bin cannot swing the result as much as a
    bin where real size changed hands.

    Returns None when the window carries no volume at all - "no flow measured" is a
    different answer from "flow measured as exactly neutral" and callers should not
    conflate them, matching how the single-bin version already returns None on an
    empty bin.
    """
    if profile is None:
        return None
    window = config.MULTIBIN_DELTA_WINDOW_BINS if window_bins is None else window_bins
    center = profile.bin_index(level_price)

    volume = 0.0
    delta = 0.0
    for index in range(center - window, center + window + 1):
        volume += profile.volume_at(index)
        delta += profile.delta_at(index)

    if volume <= 0:
        return None
    return delta / volume


# --------------------------------------------------------------------- vwap

def vwap_zscore(levels, price):
    """How many VWAP standard deviations `price` sits from the prior session's VWAP.

    `profile/levels.py` already builds the session VWAP and its 1sd/2sd bands for
    every profile - used today only as the POC tie-breaker, never exposed as a
    measurement. This is the "measure before gate" version of the same investigation
    finding 18 ran for delta: record the quantity unconditionally on every candidate
    so a real distribution exists before anyone proposes a threshold to gate on.

    Standardising by sigma (rather than reporting the raw distance) makes the number
    comparable across symbols and sessions of very different volatility, exactly why
    ATR is used to normalise every other distance in this project - this is the same
    idea applied to VWAP's own dispersion instead of ATR.

    Returns None when there is no level to compare against or the session's VWAP
    band collapsed to zero width (nothing to standardise by), rather than a
    division that would silently read as "exactly at VWAP".
    """
    if levels is None or levels.vwap is None:
        return None
    sigma = levels.vwap_upper_1sd - levels.vwap
    if sigma <= 0:
        return None
    return (price - levels.vwap) / sigma


# ------------------------------------------------------------------- weekly

def weekly_poc_distance_atr(weekly_levels, price, atr):
    """Signed distance from `price` to the previous calendar week's POC, in ATR.

    PLAN item 21's own "measure before gate" companion, same pattern as
    `vwap_zscore`: `ctx.weekly_levels` now exists (scanner.frozen_weekly_bundle),
    so this is the cheapest genuinely-independent HTF reading to record first -
    signed rather than absolute, like `dev_poc_migrated` elsewhere, so direction
    can be read back out of stored rows without re-deriving it.

    Returns None when there is no weekly bundle (a fresh listing's normal cold
    start, not an error) or no ATR to normalise by.
    """
    if weekly_levels is None or not atr or atr <= 0:
        return None
    return (price - weekly_levels.poc_price) / atr


# --------------------------------------------------------------- targeting

def fixed_r_target(entry_price, stop_price, direction, r_multiple=None):
    """Target at a fixed multiple of the stop distance."""
    r_multiple = config.TARGET_FIXED_R if r_multiple is None else r_multiple
    risk = abs(float(entry_price) - float(stop_price))
    if risk <= 0:
        return 0.0
    if direction == "BUY":
        return float(entry_price) + risk * float(r_multiple)
    return float(entry_price) - risk * float(r_multiple)


def structural_target(entry_price, direction, levels, naked_pocs=None,
                      exclude_prices=(), stop_price=None, min_r=None,
                      hvns=None, prior_extreme=None, prefer_near=False):
    """The nearest profile level in the trade's favour that is worth aiming at.

    Preference order, strongest magnet first: an untested (naked) POC, then this
    session's POC, then the far value-area edge. The reasoning is auction-based -
    a naked POC is unfinished business and the strongest attractor the profile
    offers, while the POC is where the most business was done and therefore where
    a returning move is most likely to reach.

    PLAN item 17, both opt-in and both None by default so the proven candidate
    set above is unchanged unless a caller explicitly asks for more:
    - `hvns`: high-volume nodes (`profile.levels.Node`, `.kind == "HVN"`) from the
      same profile the POC/VAH/VAL came from. Each node's `peak_price` - its own
      local density peak - is added as a candidate, same auction-based reasoning
      as the POC itself: a shelf where significant business was done.
    - `prior_extreme`: a single price, already resolved by the caller to the
      correct side (the prior session's high for a BUY, low for a SELL) - the
      full traded range, not the value area's 70% boundary. A different, more
      aggressive magnet than VAH/VAL.
    These are NOT validated yet - see CALIBRATION.md for the comparison against
    the current, already-proven candidate set before this becomes a default.

    PLAN item 25 - `prefer_near`, also opt-in, False by default: CALIBRATION.md's
    diagnosis found win rate falls monotonically as target distance grows
    (44.8% at 1.2-1.5R down to 7.8% at 3R+), and that the naked POC - this
    function's OWN top preference - ends up chosen on 63-66% of S2-VAR trades at
    an average 2.83R, winning only ~22%, while the session's own POC/VAH/VAL
    (1.4-1.5R average) win 38-49% of the time. `prefer_near=True` tests the
    direct fix: rank the SAME-SESSION levels (`levels`'s POC/VAH-VAL) first,
    and only fall through to the far set (naked POCs, HVNs, prior extreme) when
    nothing near clears `min_r`. False reproduces the exact existing ranking
    (one flat list, nearest-worthwhile-wins regardless of which group a level
    came from).

    Levels at or behind the entry are skipped: a "target" the trade has already
    passed is not a target.

    NEAREST, BUT ONLY AMONG LEVELS THAT CLEAR THE MINIMUM R. An earlier version took
    the nearest viable level unconditionally, which quietly biased the whole Phase 2
    comparison of targeting modes. A level can sit a few ticks beyond the entry, giving
    a target of perhaps 0.2R; `reward_below_minimum` then rejects the candidate
    entirely, even though a further level with a perfectly good structural reason was
    available in the same list. The setup therefore did not merely get a worse target -
    it DISAPPEARED, and specifically on the sessions where a near level existed. Since
    that is not a random subset, structural mode would have been measured on a
    different population from fixed-R mode, and the comparison would have been
    unfair rather than just noisy.

    So the nearest level is still preferred - closest is likeliest to be reached - but
    only among those actually worth trading. `stop_price` is required to judge that;
    without it the old unconditional behaviour applies.
    """
    entry_price = float(entry_price)
    min_r = config.TARGET_MIN_R if min_r is None else float(min_r)
    risk = abs(entry_price - float(stop_price)) if stop_price else 0.0
    excluded = {round(float(price), 12) for price in exclude_prices}

    def _viable(candidates):
        out = []
        for kind, price in candidates:
            if round(price, 12) in excluded:
                continue
            if direction == "BUY" and price > entry_price:
                out.append((kind, price))
            elif direction == "SELL" and price < entry_price:
                out.append((kind, price))
        return sorted(out, key=lambda pair: abs(pair[1] - entry_price))

    def _pick(by_distance):
        """Nearest level that clears min_r, else the furthest available (an
        honest reading of the best this candidate set had), else None."""
        if not by_distance:
            return None
        if risk > 0:
            worthwhile = [pair for pair in by_distance
                         if abs(pair[1] - entry_price) / risk >= min_r]
            if worthwhile:
                return worthwhile[0]
            return by_distance[-1]
        return by_distance[0]

    near_candidates = []
    if levels is not None:
        near_candidates.append(("POC", float(levels.poc_price)))
        near_candidates.append(("VAH" if direction == "BUY" else "VAL",
                                float(levels.vah if direction == "BUY" else levels.val)))

    far_candidates = []
    for price in (naked_pocs or []):
        far_candidates.append(("nPOC", float(price)))
    for node in (hvns or []):
        far_candidates.append(("HVN", float(node.peak_price)))
    if prior_extreme is not None:
        far_candidates.append(("priorExtreme", float(prior_extreme)))

    if prefer_near:
        near_viable = _viable(near_candidates)
        far_viable = _viable(far_candidates)

        if risk > 0:
            near_worthwhile = [pair for pair in near_viable
                               if abs(pair[1] - entry_price) / risk >= min_r]
            if near_worthwhile:
                return near_worthwhile[0][1], near_worthwhile[0][0]
            far_worthwhile = [pair for pair in far_viable
                              if abs(pair[1] - entry_price) / risk >= min_r]
            if far_worthwhile:
                return far_worthwhile[0][1], far_worthwhile[0][0]
            # Nothing anywhere clears the floor - the same honest "furthest
            # available" fallback as the non-preferred path, just computed
            # across both groups together rather than one flat list.
            combined = near_viable + far_viable
            if not combined:
                return None, ""
            kind, price = sorted(combined,
                                 key=lambda pair: abs(pair[1] - entry_price))[-1]
            return price, kind

        # No stop_price given: same "nearest within whichever group is tried
        # first" shape the non-preferred path uses when risk is unknown.
        if near_viable:
            return near_viable[0][1], near_viable[0][0]
        if far_viable:
            return far_viable[0][1], far_viable[0][0]
        return None, ""

    by_distance = _viable(near_candidates + far_candidates)
    choice = _pick(by_distance)
    if choice is None:
        return None, ""
    return choice[1], choice[0]


def target_candidates(entry_price, direction, levels, naked_pocs=None, hvns=None,
                      prior_extreme=None):
    """Every candidate target on the trade's side of entry, recorded for counterfactual
    analysis. Ignores the item 17/25 flags on purpose: the point is the full menu, not
    the subset the live rule happens to use. No decision reads this field."""
    entry_price = float(entry_price)
    found = []
    if levels is not None:
        found.append(("POC", float(levels.poc_price)))
        found.append(("VAH" if direction == "BUY" else "VAL",
                      float(levels.vah if direction == "BUY" else levels.val)))
    for price in (naked_pocs or []):
        found.append(("nPOC", float(price)))
    for node in (hvns or []):
        found.append(("HVN", float(node.peak_price)))
    if prior_extreme is not None:
        found.append(("priorExtreme", float(prior_extreme)))
    side = [(kind, price) for kind, price in found
            if (price > entry_price if direction == "BUY" else price < entry_price)]
    return ";".join(f"{kind}:{price!r}" for kind, price in side)


def choose_target(entry_price, stop_price, direction, levels,
                  naked_pocs=None, mode=None, hvns=None, prior_extreme=None,
                  prefer_near=False):
    """Resolve the configured target mode into a price plus its provenance.

    Structural mode falls back to fixed R when the profile offers nothing in the
    trade's favour, and the fallback is REPORTED rather than silent - otherwise a
    Phase 2 comparison of the two modes would be contaminated by an unknown
    number of fixed-R trades labelled structural.

    `hvns`/`prior_extreme`: PLAN item 17's opt-in enrichment, passed straight
    through to `structural_target`. None (the default) reproduces today's
    already-proven candidate set exactly.

    `prefer_near`: PLAN item 25's opt-in reordering, same pass-through, False
    by default.
    """
    mode = config.TARGET_MODE if mode is None else mode

    if mode == "structural":
        price, kind = structural_target(entry_price, direction, levels,
                                        naked_pocs=naked_pocs,
                                        stop_price=stop_price,
                                        hvns=hvns, prior_extreme=prior_extreme,
                                        prefer_near=prefer_near)
        if price is not None:
            return price, f"structural:{kind}"
        return (fixed_r_target(entry_price, stop_price, direction),
                "fixed_r:fallback")

    return fixed_r_target(entry_price, stop_price, direction), "fixed_r"
