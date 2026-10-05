"""Validity gates. Each is a pure function returning a Rejection or None.

TWO CLASSES OF GATE, AND THE DISTINCTION IS LOAD-BEARING.

MECHANICAL gates say the measurement or the arithmetic cannot be trusted: a
profile built from too few candles, a level that moves when you re-bin it, a stop
narrower than the cost of trading. Being wrong about one of these is a bug. They
default ON.

OPINION gates say something about the market: that reverting against a one-sided
profile is worse, that taker flow opposing the trade matters, that higher-timeframe
value should agree. Being wrong about one of these is a hypothesis. They default
OFF and are switched on only by a Phase 3 ablation result.

Mixing the two is how a system accumulates unvalidated beliefs that look like
safety checks. The `profiles.py` table makes the split visible at a glance.

WHY MECHANICAL GATES START PERMISSIVE. A gate set too loose costs some bad trades,
which Phase 2 measures and prices. A gate set too tight produces a FALSE NULL - the
setup never fires where it works, and Phase 1 concludes "no edge" about a filter
rather than about the market. The second failure is invisible: nothing signals that
a measurement was strangled rather than informative. So thresholds start permissive
and tighten on evidence.

Every gate takes (candidate, ctx) and returns Rejection | None. No gate mutates
anything, so they can be run in any order, individually disabled, and evaluated in
research without an execution path.
"""
import config
import sessions
from exchange import filters
from setups.base import RejectReason, reject


# ------------------------------------------------------- profile quality (ON)

def profile_thin(candidate, ctx):
    """The histogram is not a distribution: too few candles, bins, or volume."""
    profile = ctx.prior_profile
    if profile is None:
        return reject(RejectReason.NO_PRIOR_PROFILE, candidate.setup, ctx.symbol)

    if profile.candle_count < config.PROFILE_MIN_CANDLES:
        return reject(RejectReason.PROFILE_THIN, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      detail=(f"{profile.candle_count} candles < "
                              f"{config.PROFILE_MIN_CANDLES}"))
    if profile.bin_count < config.PROFILE_MIN_OCCUPIED_BINS:
        return reject(RejectReason.PROFILE_THIN, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      detail=(f"{profile.bin_count} occupied bins < "
                              f"{config.PROFILE_MIN_OCCUPIED_BINS}"))
    if profile.total_quote_volume < config.PROFILE_MIN_QUOTE_VOLUME:
        return reject(RejectReason.PROFILE_THIN, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      detail=(f"quote volume {profile.total_quote_volume:.0f} < "
                              f"{config.PROFILE_MIN_QUOTE_VOLUME:.0f}"))
    return None


def poc_unstable(candidate, ctx):
    """The POC moves more than one bin when the bin width changes by +/-33%.

    A level that shifts under re-binning is an artifact of the grid, not a
    participation peak. Applied only to setups that stake a stop on the POC.
    """
    stability = ctx.prior_stability
    if stability is None:
        return None
    if not stability.poc_stable:
        return reject(RejectReason.POC_UNSTABLE, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      level_price=candidate.level_price,
                      detail=(f"poc moves {stability.poc_shift_bins:.2f} bins under "
                              f"re-binning, max "
                              f"{config.STABILITY_MAX_POC_SHIFT_BINS}"))
    return None


def poc_not_prominent(candidate, ctx):
    """The distribution is too flat to have a meaningful mode.

    Companion to poc_unstable, and necessary because the two failures are
    independent: the argmax of near-uniform noise is perfectly REPRODUCIBLE - it
    passes the stability check - and still means nothing.
    """
    stability = ctx.prior_stability
    if stability is None:
        return None
    if not stability.poc_prominent:
        return reject(RejectReason.POC_NOT_PROMINENT, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      level_price=candidate.level_price,
                      detail=(f"prominence {stability.poc_prominence:.2f} < "
                              f"{config.POC_MIN_PROMINENCE}"))
    return None


def value_bounds_unstable(candidate, ctx):
    """VAH/VAL move more than one bin under re-binning. Gates S2 and S3."""
    stability = ctx.prior_stability
    if stability is None:
        return None
    if not stability.value_bounds_stable:
        return reject(RejectReason.VA_BOUNDS_UNSTABLE, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      level_price=candidate.level_price,
                      detail=(f"vah/val move {stability.vah_shift_bins:.2f}/"
                              f"{stability.val_shift_bins:.2f} bins"))
    return None


def value_area_degenerate(candidate, ctx):
    """The value area is too wide, too narrow, or covers nearly the whole range.

    A VA spanning almost the entire range says nothing - there is no "outside
    value" to reference. One spanning almost none of it means the session was a
    single spike and its bounds are noise.
    """
    levels, profile = ctx.prior_levels, ctx.prior_profile
    if levels is None or profile is None or ctx.atr <= 0:
        return None

    width_atr = levels.value_width / ctx.atr
    if width_atr < config.VA_MIN_WIDTH_ATR:
        return reject(RejectReason.VA_DEGENERATE, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      detail=f"value width {width_atr:.3f} ATR is too narrow")
    if width_atr > config.VA_MAX_WIDTH_ATR:
        return reject(RejectReason.VA_DEGENERATE, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      detail=f"value width {width_atr:.3f} ATR is too wide")
    if profile.range > 0:
        fraction = levels.value_width / profile.range
        if fraction > config.VA_MAX_RANGE_FRACTION:
            return reject(RejectReason.VA_DEGENERATE, candidate.setup, ctx.symbol,
                          direction=candidate.direction,
                          detail=(f"value covers {fraction:.3f} of range - no "
                                  f"meaningful outside-value region"))
    return None


def developing_immature(candidate, ctx):
    """The current session has not built enough profile to be read yet.

    Only relevant to setups that reference the DEVELOPING profile - S2 and S3 read
    the current session's excursions and POC migration. S1 reads only the frozen
    prior profile and is unaffected.
    """
    maturity = ctx.maturity or {}
    if not maturity:
        return None
    if not maturity.get("mature", False):
        return reject(RejectReason.DEVELOPING_IMMATURE, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      detail=(f"elapsed={maturity.get('elapsed_minutes', 0):.0f}m "
                              f"(need {config.DEVELOPING_MIN_ELAPSED_MINUTES}) "
                              f"volume_frac={maturity.get('volume_fraction', 0):.3f} "
                              f"(need {config.DEVELOPING_MIN_VOLUME_FRACTION})"))
    return None


# --------------------------------------------------------- geometry (ON)

def direction_geometry(candidate, ctx):
    """Stop and target must lie on the sides the DIRECTION implies.

        BUY   stop < entry < target
        SELL  target < entry < stop

    WHY THIS HAS TO BE AN EXPLICIT GATE. Every other risk measure in the system is
    built on absolute values - `risk_distance` is `abs(entry - stop)` and reward is
    `abs(target - entry)` - because R is a magnitude. A consequence is that an
    INVERTED level set produces a perfectly healthy-looking positive R and passes
    every existing check: stop_too_tight sees a sensible distance, reward_below_minimum
    sees a sensible ratio, and the venue filters see two valid prices.

    What the venue then does with it is the expensive part. An inverted stop is already
    through its trigger, so it is rejected -2021 AFTER the entry has filled, and the
    position is closed at market by the protection path. An inverted target is worse
    and quieter: a reduceOnly LIMIT on the wrong side of the market is immediately
    fillable, so it closes the position the moment it is placed, for roughly nothing,
    and the trade is journaled as an ordinary small loss rather than as a fault.

    So this is cheap insurance against a sign error anywhere upstream - in a detector,
    in a target chooser, in a future setup - and it fails loudly instead of trading.
    A detector should never produce this; that is exactly why it is worth asserting.
    """
    entry = candidate.entry_price
    stop = candidate.stop_price
    target = candidate.target_price

    if entry <= 0 or stop <= 0 or target <= 0:
        return reject(RejectReason.GEOMETRY_INVALID, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      detail=(f"non-positive price: entry={entry:.8g} "
                              f"stop={stop:.8g} target={target:.8g}"))

    if candidate.direction == "BUY":
        ordered = stop < entry < target
    else:
        ordered = target < entry < stop

    if not ordered:
        return reject(RejectReason.GEOMETRY_INVALID, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      level_price=candidate.level_price,
                      detail=(f"{candidate.direction} requires "
                              f"{'stop<entry<target' if candidate.direction == 'BUY' else 'target<entry<stop'}"
                              f", got stop={stop:.8g} entry={entry:.8g} "
                              f"target={target:.8g}"))
    return None


def stop_too_tight(candidate, ctx):
    """Risk smaller than a multiple of the cost of trading.

    Below this, R is not a meaningful unit: fees and slippage consume the risk
    budget, so a "2R winner" nets far less and a stop-out costs far more than 1R.
    Candidate.round_trip_cost_r expresses exactly this in R terms.
    """
    if candidate.risk_distance <= 0:
        return reject(RejectReason.STOP_TOO_TIGHT, candidate.setup, ctx.symbol,
                      direction=candidate.direction, detail="zero risk distance")

    cost_r = candidate.round_trip_cost_r()
    limit = 1.0 / max(config.STOP_MIN_COST_MULTIPLE, 1e-9)
    if cost_r > limit:
        return reject(RejectReason.STOP_TOO_TIGHT, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      level_price=candidate.level_price,
                      detail=(f"fees are {cost_r:.3f}R - risk must exceed "
                              f"{config.STOP_MIN_COST_MULTIPLE}x trading cost"))
    return None


def stop_too_wide(candidate, ctx):
    """Risk so large that position size collapses toward the notional minimum."""
    if ctx.atr <= 0:
        return None
    risk_atr = candidate.risk_distance / ctx.atr
    if risk_atr > config.STOP_MAX_ATR:
        return reject(RejectReason.STOP_TOO_WIDE, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      level_price=candidate.level_price,
                      detail=f"risk {risk_atr:.3f} ATR > {config.STOP_MAX_ATR}")
    return None


def reward_below_minimum(candidate, ctx):
    """Reward-to-risk beneath the floor, measured NET of fees.

    Gross R is the wrong yardstick here: a 1.3R gross target on a tight stop can be
    below 1.0R net, which is a losing trade dressed as a winning one.
    """
    net = candidate.net_r_at_target()
    if net < config.TARGET_MIN_R:
        return reject(RejectReason.REWARD_BELOW_MINIMUM, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      level_price=candidate.level_price,
                      detail=(f"net {net:.3f}R (gross {candidate.r_multiple:.3f}R "
                              f"minus {candidate.round_trip_cost_r():.3f}R cost) < "
                              f"{config.TARGET_MIN_R}"))
    return None


def venue_filters(candidate, ctx):
    """The venue's own filters, checked BEFORE anything is sent.

    Catching a filter failure locally is strictly better than learning it from an
    API error: the error costs a round trip, a rate-limit weight, and a log line
    that reads as a fault rather than a normal skip. More importantly, a stop order
    refused AFTER the entry filled leaves an unprotected position.
    """
    spec = ctx.spec
    if spec is None:
        return None

    mark = ctx.mark_price or ctx.last_price
    try:
        entry = filters.round_price_passive(candidate.entry_price, spec.tick_size,
                                           candidate.direction)
        filters.check_price(entry, spec)
        filters.check_percent_price(entry, mark, spec, candidate.direction)

        stop = filters.round_stop_price(candidate.stop_price, spec.tick_size,
                                       candidate.position_side)
        filters.check_price(stop, spec)
        filters.check_stop_triggerable(stop, mark, candidate.position_side)

        target = filters.round_target_price(candidate.target_price, spec.tick_size,
                                           candidate.position_side)
        filters.check_price(target, spec)
    except filters.FilterRejection as exc:
        code = (RejectReason.STOP_WOULD_TRIGGER
                if exc.code == "STOP_WOULD_TRIGGER" else RejectReason.FILTER_REJECTED)
        return reject(code, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      level_price=candidate.level_price,
                      detail=f"{exc.code}: {exc.detail}")
    return None


# ------------------------------------------------------ operational (ON)

def funding_window(candidate, ctx):
    """Inside the funding settlement guard.

    Funding concentrates volume into a few minutes and distorts both the developing
    profile and the fill quality of a passive entry. Mechanical rather than an
    opinion - but the window WIDTH is provisional and gets swept in Phase 4.
    """
    if sessions.in_funding_guard(ctx.as_of):
        return reject(RejectReason.FUNDING_WINDOW, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      detail=(f"{sessions.minutes_to_nearest_funding(ctx.as_of):.1f}m "
                              f"from funding, guard "
                              f"{config.FUNDING_GUARD_MINUTES}m"))
    return None


def spread_too_wide(candidate, ctx, spread_bps=None):
    """The live book is too wide for the entry price to mean anything.

    Takes the measurement as an argument rather than fetching it: gates must stay
    pure so research can run them without a network.
    """
    if spread_bps is None:
        return None
    if spread_bps > config.MAX_SPREAD_BPS:
        return reject(RejectReason.SPREAD_TOO_WIDE, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      detail=f"spread {spread_bps:.2f}bps > {config.MAX_SPREAD_BPS}")
    return None


# ------------------------------------------------------- opinion gates (OFF)

def shape_opposed(candidate, ctx):
    """A balance trade against a one-sided profile. OPINION - default OFF.

    Theory supports it: reverting into a D-shape is a balance trade, while
    reverting against a P, b or trend profile is fading a directional auction. But
    the state machine already restricts S1 to D-shapes, so on S2 this is a genuine
    additional hypothesis rather than a restatement, and it must be measured.
    """
    if not config.GATE_SHAPE_OPPOSED_ENABLED:
        return None
    shape = ctx.prior_shape
    if shape is None or shape.balanced:
        return None
    if candidate.setup.startswith("S3"):
        return None                     # continuation WANTS a one-sided auction
    return reject(RejectReason.SHAPE_OPPOSED, candidate.setup, ctx.symbol,
                  direction=candidate.direction,
                  level_price=candidate.level_price,
                  detail=f"shape={shape.label} is one-sided")


def delta_opposed(candidate, ctx):
    """Per-bin taker imbalance at the level opposes the trade. OPINION - OFF.

    Reads the signed flow accumulated in the profile bin the trade is entering at,
    which is a different question from confirm.delta_divergence - that one reads a
    single candle, this reads the whole session's flow at that price.
    """
    if not config.GATE_DELTA_OPPOSED_ENABLED:
        return None
    profile, levels = ctx.prior_profile, ctx.prior_levels
    if profile is None or levels is None:
        return None

    index = profile.bin_index(candidate.level_price)
    delta = profile.delta_at(index)
    volume = profile.volume_at(index)
    if volume <= 0:
        return None

    normalised = delta / volume
    opposed = (normalised > config.GATE_DELTA_OPPOSED_MIN
               if candidate.direction == "SELL"
               else normalised < -config.GATE_DELTA_OPPOSED_MIN)
    if opposed:
        return reject(RejectReason.DELTA_OPPOSED, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      level_price=candidate.level_price,
                      detail=f"bin delta {normalised:+.3f} opposes {candidate.direction}")
    return None


def delta_opposed_multibin(candidate, ctx):
    """Taker imbalance opposes the trade, read over a WINDOW of bins. OPINION - OFF.

    Same question as delta_opposed, widened from the single bin the level falls in
    to config.MULTIBIN_DELTA_WINDOW_BINS on each side - see
    setups.base.multibin_delta_normalized. Not part of finding 18's sweep, which
    tested only the single-bin version; this one is unswept, PLAN item 6.
    """
    if not config.GATE_DELTA_OPPOSED_MULTIBIN_ENABLED:
        return None
    from setups.base import multibin_delta_normalized

    normalised = multibin_delta_normalized(ctx.prior_profile, candidate.level_price)
    if normalised is None:
        return None

    opposed = (normalised > config.GATE_DELTA_OPPOSED_MULTIBIN_MIN
               if candidate.direction == "SELL"
               else normalised < -config.GATE_DELTA_OPPOSED_MULTIBIN_MIN)
    if opposed:
        return reject(RejectReason.DELTA_OPPOSED_MULTIBIN, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      level_price=candidate.level_price,
                      detail=f"multi-bin delta {normalised:+.3f} opposes "
                             f"{candidate.direction}")
    return None


def htf_value_opposed(candidate, ctx):
    """The trade opposes weekly value migration. OPINION - default OFF."""
    if not config.GATE_HTF_VALUE_OPPOSED_ENABLED:
        return None
    migration = ctx.migration
    if migration is None or not migration.directional:
        return None
    opposed = ((migration.label == "HIGHER" and candidate.direction == "SELL")
               or (migration.label == "LOWER" and candidate.direction == "BUY"))
    if opposed:
        return reject(RejectReason.HTF_VALUE_OPPOSED, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      detail=f"value migrating {migration.label}")
    return None


def stop_inside_hvn(candidate, ctx):
    """The stop sits inside a high-volume node. OPINION - default OFF.

    An HVN is where the most business was done, so price rotates there - a stop
    inside one is likely to be reached by ordinary rotation rather than by the
    setup failing. Plausible and untested, hence OFF.
    """
    if not config.GATE_STOP_INSIDE_HVN_ENABLED:
        return None
    levels = ctx.prior_levels
    if levels is None:
        return None
    node = levels.hvn_containing(candidate.stop_price)
    if node is not None:
        return reject(RejectReason.STOP_INSIDE_HVN, candidate.setup, ctx.symbol,
                      direction=candidate.direction,
                      level_price=candidate.level_price,
                      detail=(f"stop {candidate.stop_price:.8g} inside HVN "
                              f"[{node.low_price:.8g},{node.high_price:.8g}]"))
    return None


def target_behind_hvn(candidate, ctx):
    """The target sits beyond an unbroken high-volume shelf. OPINION - OFF.

    The mirror of the above, and the sharpest argument against fixed-R targeting: a
    2R target placed past a dense shelf asks price to cross ground it has every
    reason to stall in.
    """
    if not config.GATE_TARGET_BEHIND_HVN_ENABLED:
        return None
    levels = ctx.prior_levels
    if levels is None:
        return None

    low, high = sorted((candidate.entry_price, candidate.target_price))
    for node in levels.hvns:
        if low < node.center_price < high:
            return reject(RejectReason.TARGET_BEHIND_HVN, candidate.setup, ctx.symbol,
                          direction=candidate.direction,
                          level_price=candidate.level_price,
                          detail=(f"HVN at {node.center_price:.8g} lies between entry "
                                  f"and target"))
    return None
