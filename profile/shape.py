"""Profile shape classification, excess detection, and single prints.

WHY SHAPE IS THE MOST IMPORTANT CLASSIFIER IN THE SYSTEM. Auction theory gives
two states - balance (two-sided, rotating, mean-reverting) and imbalance
(one-sided discovery, continuation) - and every setup is a bet on which one is
running. Entry rules are cheap and largely interchangeable; getting the state
wrong means taking every trade backwards. So this module, not the setup
detectors, is where the system's accuracy is won or lost.

THE FIVE SHAPES, and what each says about the auction:

  D      Balanced. Volume concentrated centrally, value wide relative to range,
         excess at both extremes. The two-sided auction. MEAN REVERSION.
  P      Rally then balance at the top: a drive up that stopped and rotated.
         Often short-covering exhausting. Not a reversal signal by itself.
  b      Sell-off then balance at the bottom. The liquidation mirror of P.
  B      Two distributions with a valley between. Value has SPLIT - the session
         contains two different auctions and neither POC represents it. The most
         dangerous shape to trade a single level from.
  trend  Thin and elongated, value narrow relative to range. Pure price
         discovery. CONTINUATION ONLY - fading a trend day is the classic way to
         lose on a profile system.

CLASSIFICATION IS QUANTITATIVE. Four measurements decide it: value-area width over
total range (elongation), POC position within the range (skew), a bimodality test
(split value), and intra-session POC migration (travel). No visual pattern
matching, because a threshold can be swept in Phase 4 and an eyeball cannot.

THE FOURTH MEASUREMENT EXISTS BECAUSE THE FIRST THREE ARE NOT ENOUGH, and that was
found by testing rather than by reasoning. A uniform one-directional session -
pure price discovery - spreads volume evenly across its range, so its value area
covers ~70% of that range and its POC sits dead centre. On distribution alone it is
INDISTINGUISHABLE from a balanced session: the trend fixture measured a VA/range of
0.708 and classified as D. The information that separates travel from spread is
time ordering, which a completed histogram has thrown away, so it has to be
recovered explicitly - see intra_session_poc_migration.

D IS THE RESIDUAL, not a threshold. An earlier version gave D its own minimum
elongation ratio, which left a dead zone between that minimum and the trend ceiling
where a central-POC profile matched no branch at all and fell through to `trend`.
Two thresholds on one axis with different outcomes either side always leave such a
gap.

EVERY THRESHOLD HERE IS PROVISIONAL AND CANNOT BE HONESTLY SET FROM SYNTHETIC
DATA. Synthetic fixtures embed an assumption about what real profiles look like,
and that assumption is the thing being calibrated - a Gaussian calibrates POC
prominence usefully and calibrates VA/range badly, because real sessions are
platykurtic where a Gaussian is not. config.py records the reasoning and the
measured reference for each; Phase 0 replaces them with observed percentiles.
"""
from dataclasses import dataclass, field

import config


@dataclass
class Shape:
    label: str                    # "D" | "P" | "b" | "B" | "trend"
    va_range_ratio: float         # value width / total range - elongation
    poc_position: float           # 0.0 at session low, 1.0 at session high
    bimodal: bool
    second_mode_price: float = 0.0
    valley_price: float = 0.0
    # The two continuous fractions SHAPE_BIMODAL_MIN_SECOND_MODE / _MAX_VALLEY
    # actually threshold against - recorded (even when they fail that threshold) so
    # a calibration sweep can re-test those cutoffs from stored rows without
    # rebuilding every profile. 0.0 only when there was nothing to measure at all
    # (too few bins, no POC volume, or no second peak found anywhere outside the
    # POC's own neighbourhood) - valley_fraction specifically stays 0.0 whenever
    # second_mode_fraction itself already failed, since a valley was never located.
    second_mode_fraction: float = 0.0
    valley_fraction: float = 0.0
    intra_poc_migration_atr: float = None   # None = not measured, not zero

    excess_high: bool = False     # long thin tail at the top = real rejection
    excess_low: bool = False
    poor_high: bool = False       # no tail = unfinished = tends to be revisited
    poor_low: bool = False

    single_print_bins: list = field(default_factory=list)

    # Where this session's own close sits in its own range, 0.0=low 1.0=high.
    # None (not 0.0) when there was no range to measure against - see
    # intra_poc_migration_atr's own comment for why the sentinel matters here too.
    # This is day_bias()'s close-location condition for P/b - see its docstring.
    close_location: float = None

    @property
    def balanced(self):
        """Is this a two-sided auction? Gates every mean-reversion setup."""
        return self.label == "D"

    @property
    def one_sided(self):
        """P, b and trend all mean one side dominated the session."""
        return self.label in ("P", "b", "trend")

    @property
    def auction_state(self):
        return "BALANCE" if self.balanced else "IMBALANCE"

    def as_row(self):
        return {
            "shape": self.label,
            "auction_state": self.auction_state,
            "va_range_ratio": round(self.va_range_ratio, 4),
            "poc_position": round(self.poc_position, 4),
            "bimodal": self.bimodal,
            "second_mode_fraction": round(self.second_mode_fraction, 4),
            "valley_fraction": round(self.valley_fraction, 4),
            "intra_poc_migration_atr": (
                round(self.intra_poc_migration_atr, 4)
                if self.intra_poc_migration_atr is not None else None),
            "excess_high": self.excess_high,
            "excess_low": self.excess_low,
            "poor_high": self.poor_high,
            "poor_low": self.poor_low,
            "single_prints": len(self.single_print_bins),
            "close_location": (
                round(self.close_location, 4)
                if self.close_location is not None else None),
        }


def _bimodality(profile, levels):
    """Detect a split distribution: a second mode separated by a real valley.

    Walks the contiguous histogram for the largest peak that is NOT adjacent to
    the POC, then checks the deepest trough between them. Both conditions must
    hold - a second peak with no valley is just a broad shoulder of one
    distribution, and a valley with no second peak is an LVN inside a single
    distribution. Only together do they mean value has split.

    Returns (bimodal, second_mode_price, valley_price, second_mode_fraction,
    valley_fraction) - the two fractions are the continuous values
    SHAPE_BIMODAL_MIN_SECOND_MODE/_MAX_VALLEY actually compare against, surfaced
    (not just their pass/fail) so a calibration sweep can re-test those cutoffs
    without rebuilding every profile.
    """
    histogram = profile.histogram()
    if len(histogram) < 5 or levels.poc_volume <= 0:
        return False, 0.0, 0.0, 0.0, 0.0

    poc_bin = levels.poc_bin
    best_bin, best_volume = None, 0.0

    for index, volume in histogram:
        if abs(index - poc_bin) < 2:
            continue
        if volume > best_volume:
            best_bin, best_volume = index, volume

    if best_bin is None:
        return False, 0.0, 0.0, 0.0, 0.0

    second_mode_fraction = best_volume / levels.poc_volume
    if best_volume < levels.poc_volume * config.SHAPE_BIMODAL_MIN_SECOND_MODE:
        return False, 0.0, 0.0, second_mode_fraction, 0.0

    low, high = sorted((poc_bin, best_bin))
    between = [(index, volume) for index, volume in histogram if low < index < high]
    if not between:
        return False, 0.0, 0.0, second_mode_fraction, 0.0

    valley_bin, valley_volume = min(between, key=lambda pair: pair[1])
    valley_fraction = valley_volume / levels.poc_volume
    if valley_volume > levels.poc_volume * config.SHAPE_BIMODAL_MAX_VALLEY:
        return False, 0.0, 0.0, second_mode_fraction, valley_fraction

    return (True, profile.bin_center(best_bin), profile.bin_center(valley_bin),
           second_mode_fraction, valley_fraction)


def _excess(profile, levels):
    """Excess vs poor extremes at each end of the range.

    EXCESS is a run of very thin bins at an extreme: the auction pushed there,
    found nothing, and was shut off. That is a completed, defended extreme.

    A POOR extreme is the opposite - volume still substantial right up to the
    high or low, meaning the auction was cut short rather than finished. Poor
    extremes tend to be revisited, which makes this the single most useful
    quality filter available to a reversion setup: the same geometry has opposite
    prognosis depending on which of these it ran into.
    """
    histogram = profile.histogram()
    if len(histogram) < config.EXCESS_MIN_BINS * 2 + 1 or levels.poc_volume <= 0:
        return False, False, False, False

    ceiling = levels.poc_volume * config.EXCESS_MAX_BIN_VOLUME_PCT
    window = config.EXCESS_MIN_BINS

    top = histogram[-window:]
    bottom = histogram[:window]

    excess_high = all(volume <= ceiling for _, volume in top)
    excess_low = all(volume <= ceiling for _, volume in bottom)

    # "Poor" is a stronger statement than "not excess": it requires substantial
    # volume at the extreme, not merely the absence of a thin tail. See
    # config.POOR_EXTREME_MAX_BIN_VOLUME_PCT for why this is no longer 0.40.
    poor_ceiling = levels.poc_volume * config.POOR_EXTREME_MAX_BIN_VOLUME_PCT
    poor_high = (not excess_high) and top[-1][1] >= poor_ceiling
    poor_low = (not excess_low) and bottom[0][1] >= poor_ceiling

    return excess_high, excess_low, poor_high, poor_low


def _single_prints(profile, levels):
    """Bins with negligible volume inside the traded range.

    These mark price that was quoted but barely transacted - the signature of
    fast one-sided movement. They overlap with LVNs by construction; the
    distinction kept here is that a single print is about ABSENCE of business
    anywhere in the range, while an LVN is a structural valley used as a level.
    """
    if levels.poc_volume <= 0:
        return []
    ceiling = levels.poc_volume * 0.05
    return [index for index, volume in profile.histogram() if volume <= ceiling]


def intra_session_poc_migration(profile_builder, symbol, candles, window_start,
                                window_end, as_of, bin_size, atr):
    """How far the POC moved between the session's first and second half, in ATR.

    WHY THIS IS NECESSARY, found by testing. VA/range measures how CONCENTRATED
    volume is, not whether the session travelled. A uniform linear ramp from 100 to
    106 - pure price discovery - spreads volume evenly across its range, so its
    value area covers ~70% of that range and its POC sits dead centre. It is
    therefore indistinguishable from a balanced session on distribution alone: the
    test fixture for a trend day classified as D at a ratio of 0.708.

    The information that separates them is TIME ORDERING, which a completed
    histogram has discarded. Splitting the session in half and comparing the two
    POCs restores exactly the missing piece: a balanced auction rotates around one
    price, so both halves agree; a trending auction moves, so they do not.

    This is the standard Market Profile notion of developing value migration,
    applied within a session rather than between them. Cost is two extra profile
    builds, which at 1m granularity is a few milliseconds.

    THE SPLIT IS ON CANDLES THAT TRADED, NOT ON THE WINDOW'S MIDPOINT. Splitting a
    UTC day at 12:00 would mean a session three hours old has an empty second half,
    so the measurement would return None for the entire first half of every day -
    silently disabling this check exactly when a developing profile most needs it.
    Splitting the available candles by count gives equal elapsed time either side
    for uniform 1m bars, and degrades correctly on a partial session.

    Returns None when either half is unusable, so the caller can distinguish
    "no migration" from "not measured".
    """
    if atr is None or atr <= 0:
        return None

    usable = [candle for candle in candles
              if window_start <= candle.open_time < window_end
              and candle.close_time <= as_of]
    # Two POCs need enough bars each to be meaningful rather than merely computable.
    if len(usable) < 20:
        return None

    midpoint = len(usable) // 2
    from profile import levels as levels_mod

    pocs = []
    for part in (usable[:midpoint], usable[midpoint:]):
        half = profile_builder.build(
            symbol, part, part[0].open_time, part[-1].close_time + 1,
            part[-1].close_time, bin_size,
        )
        if not half.volume:
            return None
        half_levels = levels_mod.compute(half)
        if half_levels is None:
            return None
        pocs.append(half_levels.poc_price)

    return (pocs[1] - pocs[0]) / atr


def classify(profile, levels, intra_poc_migration_atr=None):
    """Full shape assessment. Returns None when the profile is empty.

    `intra_poc_migration_atr` is optional but strongly recommended - without it a
    uniform one-directional session is indistinguishable from balance. See
    intra_session_poc_migration above.
    """
    if levels is None or not profile.volume:
        return None

    total_range = profile.range
    va_range_ratio = (levels.value_width / total_range) if total_range > 0 else 1.0
    poc_position = (
        (levels.poc_price - profile.low) / total_range if total_range > 0 else 0.5
    )
    close_location = (
        (profile.close - profile.low) / total_range if total_range > 0 else None
    )

    bimodal, second_mode, valley, second_mode_fraction, valley_fraction = (
        _bimodality(profile, levels))
    excess_high, excess_low, poor_high, poor_low = _excess(profile, levels)

    # ORDER MATTERS, and D IS THE RESIDUAL.
    #
    # B first: a split distribution's POC position and elongation are both
    # meaningless, because they describe an average of two separate auctions.
    # Then trend, which is a statement about the whole session's character.
    # Then the two skew shapes. Anything left - not split, not elongated, POC
    # central - is by definition a balanced two-sided auction, so D is what
    # remains rather than something with its own threshold.
    #
    # Making D the residual is a deliberate correction. An earlier version gated D
    # behind its own minimum elongation ratio, which left a gap between that
    # minimum and the trend ceiling where a central-POC profile matched no branch
    # and fell through to `trend`. Two thresholds on one axis always leave such a
    # gap; a residual cannot.
    migrated = (
        intra_poc_migration_atr is not None
        and abs(intra_poc_migration_atr) >= config.SHAPE_TREND_MIN_POC_MIGRATION_ATR
    )

    if bimodal:
        label = "B"
    elif va_range_ratio <= config.SHAPE_TREND_MAX_VA_RANGE_RATIO:
        label = "trend"
    elif migrated:
        # Value itself moved across the session. This catches the case
        # distribution alone cannot see - a session that travelled evenly, whose
        # histogram is indistinguishable from balance. Checked BEFORE the skew
        # shapes because a migrating auction is a trend regardless of where its
        # aggregate POC happened to land.
        label = "trend"
    elif poc_position >= config.SHAPE_P_MIN_POC_POSITION:
        label = "P"
    elif poc_position <= config.SHAPE_B_MAX_POC_POSITION:
        label = "b"
    else:
        label = "D"

    return Shape(
        label=label,
        va_range_ratio=va_range_ratio,
        poc_position=poc_position,
        bimodal=bimodal,
        intra_poc_migration_atr=intra_poc_migration_atr,
        second_mode_price=second_mode,
        valley_price=valley,
        second_mode_fraction=second_mode_fraction,
        valley_fraction=valley_fraction,
        excess_high=excess_high,
        excess_low=excess_low,
        poor_high=poor_high,
        poor_low=poor_low,
        single_print_bins=_single_prints(profile, levels),
        close_location=close_location,
    )


def day_bias(shape, require_close_location=True):
    """Directional bias from a profile's shape.

    P gives BULL and b gives BEAR - but ONLY when `require_close_location` (the
    default) and the session's own close actually confirmed the move: P needs a
    close in the upper half of its own range, b needs one in the lower half.
    Market-Profile practice treats an unconfirmed P/b as "short covering" /
    "long liquidation" that never held what it took, not as the same signal
    with lower confidence - and that distinction is not cosmetic here. Measured
    2026-10-09 against 14,065 symbol-sessions: an unconfirmed P/b agrees with
    the next session's own value migration only 8.6%/10.7% of the time - far
    WORSE than a coin flip, not merely uninformative - while a confirmed one
    agrees 52.8%/54.2% of the time. Confound-checked against poc_position,
    va_range_ratio and |intra_poc_migration_atr|: none explain the gap, so this
    is not just a restatement of a measurement already folded into `trend`/`B`.
    See research/calibrate.py's STAGE 3 note and `report_shape_bias` for the
    full numbers this is calibrated from.

    An unconfirmed P/b returns NEUTRAL, never the opposite side. The data above
    shows the unconfirmed case is strongly wrong on average, which is tempting
    to read as "trade the inverse" - but that is a separate claim this change
    deliberately does not make without its own direct validation against real
    S4-OFR trades, not just next-session price action.

    Pass `require_close_location=False` only for a shape read off a still-
    DEVELOPING session (see profile/bias.py's `_dev_vote`) - the validation
    above is against a COMPLETED session's own final close, and has not been
    separately tested against an in-progress session's latest price, which is
    a structurally different and untested read.

    D gives NEUTRAL. Trend (thin or elongated) and double distribution days
    take their direction from the intra-session POC migration, the one measure
    here of where value travelled. When that is unmeasured or zero they are
    NEUTRAL, never guessed. None means there is no shape to read.
    """
    if shape is None:
        return None
    if shape.label == "P":
        confirmed = (require_close_location is False or
                     (shape.close_location is not None and shape.close_location > 0.5))
        return "BULL" if confirmed else "NEUTRAL"
    if shape.label == "b":
        confirmed = (require_close_location is False or
                     (shape.close_location is not None and shape.close_location < 0.5))
        return "BEAR" if confirmed else "NEUTRAL"
    if shape.label == "D":
        return "NEUTRAL"
    migration = shape.intra_poc_migration_atr
    if not migration:
        return "NEUTRAL"
    return "BULL" if migration > 0 else "BEAR"
