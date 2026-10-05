"""Acceptance: has price outside value been ACCEPTED, or merely visited?

THIS IS THE DISCRIMINATOR THE WHOLE SYSTEM TURNS ON. S2 (value-area reversion)
and S3 (breakout continuation) trade the same location in opposite directions.
They are not two signals to rank against each other - they are the two branches of
this single question, and answering it correctly is worth more than any entry
refinement in either setup.

ACCEPTANCE IS TIME AND VOLUME, NEVER DISTANCE. This is the central claim, and it
comes straight from what a volume profile measures. Price is an advertisement;
volume is the validation. A move that travels a long way on thin volume has
advertised a price nobody wanted - a single-print spike, which reverts. A move
that transacts substantial business outside old value has found willing
counterparties there - that is price discovery, and old value is now irrelevant.
Distance cannot tell these apart, and every breakout rule written in ATR alone
fails on exactly this.

So two conditions, both required:

  TIME     ACCEPT_MIN_CANDLES consecutive closes beyond the value bound. A wick
           outside proves nothing; a close is the market agreeing at that price
           for the length of a bar.
  VOLUME   the RATE of business outside value, relative to this session's own
           normal rate, at least ACCEPT_MIN_VOLUME_RATE_RATIO.

And a symmetric rejection test with a LOWER ceiling
(REJECT_MAX_VOLUME_RATE_RATIO), which is what arms S2. The gap between the two
thresholds is deliberate: between them the answer is PENDING and neither setup
fires. Overlapping thresholds would let both branches trigger on the same data,
which is precisely the ambiguity this module exists to remove.

WHY A RATE RATIO AND NOT A FRACTION - a bug worth recording, because the first
version was meaningless rather than merely imprecise. It measured "what fraction of
the excursion window's volume transacted outside the bound". But the excursion window
IS the run of candles that closed outside, so almost all of its volume is outside by
construction: the measurement returned ~1.000 on every excursion, thin or heavy
alike. The rejection threshold was unreachable and the acceptance threshold was
trivially satisfied, so the S2/S3 discriminator - the thing the whole state machine
rests on - did not discriminate at all.

The quantity that actually separates a spike from discovery is HOW MUCH BUSINESS PER
UNIT TIME happened out there, compared with how much this symbol normally does in
this session:

    outside_rate  = volume transacted beyond the bound / candles in the excursion
    session_rate  = total session volume / total session candles
    ratio         = outside_rate / session_rate

A spike advertises prices at a fraction of normal volume (ratio well below 1); real
acceptance transacts at or above normal (ratio at or above 1). The ratio is
scale-free, needs no reference to ATR or to the symbol's size, and compares the
session against itself rather than against a global constant.

HOW OUTSIDE VOLUME IS MEASURED. A candle straddling the bound has some of its
volume above and some below. Rather than counting a straddling candle wholly
in or out, its volume is PRORATED by the fraction of its high-low range that
lies beyond the bound - the same uniform-spreading assumption the profile builder
uses, so the two cannot disagree about the same candles.
"""
import logging
from dataclasses import dataclass

import config

log = logging.getLogger(__name__)

PENDING = "PENDING"
ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
INSIDE = "INSIDE"

# A FAILED AUCTION: real business was transacted outside value - acceptance-level
# volume - and then price was reclaimed back inside anyway. This is a genuinely
# distinct auction event, not a middling case, and conflating it with PENDING was
# actively misleading (a live session read rate_ratio=1.216 while being reported as
# "between the thresholds", when in fact it was above both).
#
# In auction theory a failed auction is a stronger reversal signal than an ordinary
# thin-spike rejection: participants committed at the new prices and were then proven
# wrong, so they become trapped supply or demand. It is NOT traded in v1 - the
# failed-auction reversal is deliberately out of scope until the four core setups are
# measured - but it is CLASSIFIED and JOURNALED so Phase 1 can measure how often it
# occurs and whether it deserves its own setup.
FAILED_AUCTION = "FAILED_AUCTION"

# NO INDEPENDENT BASELINE EXISTS, so the ratio cannot be computed at all. Distinct
# from PENDING, which means "measured, and the answer is ambiguous" - this means "not
# measured". Conflating the two would be the worst available option: with no baseline
# the ratio computes to 0.0, which reads as the thinnest possible excursion and would
# arm S2 on a session that has told us nothing. Found in Phase 0 calibration; see
# _baseline_rate for the arithmetic.
NO_BASELINE = "NO_BASELINE"


@dataclass
class Acceptance:
    """The answer, plus every input that produced it - the journal records all."""
    verdict: str                     # PENDING | ACCEPTED | REJECTED | INSIDE
    side: str                        # "ABOVE" | "BELOW" | "NONE"

    excursion_start_ts: int = 0
    excursion_extreme: float = 0.0
    excursion_distance: float = 0.0
    excursion_distance_atr: float = 0.0

    candles_in_window: int = 0
    consecutive_closes_outside: int = 0
    volume_outside: float = 0.0
    volume_total: float = 0.0
    # Share of the SESSION's volume that transacted outside. Recorded because it is
    # informative, but NOT the discriminator - see the module docstring.
    volume_outside_fraction: float = 0.0
    outside_rate: float = 0.0        # outside volume per excursion candle
    session_rate: float = 0.0        # the BASELINE rate - see baseline_source
    volume_rate_ratio: float = 0.0   # THE discriminator: outside_rate / session_rate

    # Which independent reference the denominator came from: "SESSION" (this session's
    # pre-excursion candles), "PRIOR_SESSION" (yesterday's rate, used when this session
    # has not yet traded enough inside value), or "NONE" (no reference; not tradeable).
    # Recorded because the two working sources are not equivalent - a prior-session
    # baseline ignores today's regime - so Phase 1 must be able to separate them.
    baseline_source: str = "NONE"

    returned_inside: bool = False    # the latest candle closed back within value
    return_ts: int = 0

    @property
    def accepted(self):
        return self.verdict == ACCEPTED

    @property
    def rejected(self):
        return self.verdict == REJECTED

    def as_row(self):
        return {
            "acceptance_verdict": self.verdict,
            "acceptance_side": self.side,
            "excursion_distance_atr": round(self.excursion_distance_atr, 4),
            "consecutive_closes_outside": self.consecutive_closes_outside,
            "volume_outside_fraction": round(self.volume_outside_fraction, 4),
            "volume_rate_ratio": round(self.volume_rate_ratio, 4),
            "outside_rate": round(self.outside_rate, 6),
            "session_rate": round(self.session_rate, 6),
            "baseline_source": self.baseline_source,
            "returned_inside": self.returned_inside,
        }


def _volume_outside(candle, bound, side):
    """Portion of one candle's volume that transacted beyond `bound`.

    Prorated by range overlap, matching profile/builder._spread_candle. A candle
    entirely beyond the bound contributes all its volume; one entirely inside
    contributes none; one straddling contributes the fraction of its range that
    lies outside. A zero-range candle is all-or-nothing on which side its single
    price fell.
    """
    low, high = candle.low, candle.high
    if high < low:
        low, high = high, low
    span = high - low

    if span <= 0:
        if side == "ABOVE":
            return candle.volume if low > bound else 0.0
        return candle.volume if high < bound else 0.0

    if side == "ABOVE":
        outside = max(0.0, high - max(low, bound))
    else:
        outside = max(0.0, min(high, bound) - low)

    return candle.volume * min(1.0, outside / span)


def _current_side(price, levels):
    if price > levels.vah:
        return "ABOVE"
    if price < levels.val:
        return "BELOW"
    return "NONE"


def _session_rate(candles):
    """Mean volume per candle across a set of candles.

    Comparing the session against itself is what keeps the measure scale-free: no
    reference to ATR, to notional size, or to any cross-symbol constant, so the same
    threshold means the same thing on BTCUSDT and on a thin alt.
    """
    if not candles:
        return 0.0
    return sum(candle.volume for candle in candles) / len(candles)


def _baseline_rate(candles, window_start_index, prior_rate, min_candles):
    """The ratio's DENOMINATOR, computed from candles OUTSIDE the excursion.

    THE DENOMINATOR MUST NOT CONTAIN THE NUMERATOR. This is the second and subtler
    form of a bug already fixed once in this module, found in Phase 0 calibration on
    real data, and it was quietly corrupting every measurement rather than only the
    obvious cases.

    The first version averaged over the WHOLE session, excursion included. Write the
    session as M excursion candles at rate r_out and N-M in-value candles at r_in:

        session_rate = (M*r_out + (N-M)*r_in) / N
        ratio        = r_out / session_rate

    As M approaches N the denominator approaches r_out and the ratio approaches 1.0
    NO MATTER WHAT r_out ACTUALLY IS. So an excursion dilutes its own reference in
    proportion to its own length - which biases hardest on the long excursions S3
    exists to trade, and collapses completely in the case that turned up in the
    calibration sweep: a session that OPENS outside yesterday's value and never trades
    back inside has M == N, giving ratio == 1.000 exactly, on 22% of all events. Every
    one of those cleared ACCEPT_MIN_VOLUME_RATE_RATIO and armed S3 on a number
    containing no information whatsoever. And because a session opening outside value
    is precisely when S3 becomes eligible, the degenerate case was concentrated in the
    population the setup trades.

    So the baseline is drawn from this session's candles BEFORE the excursion began.
    When too few of those exist to average, the prior session's rate is used - an
    independent estimate, always available (a frozen profile is a precondition for
    any of this), and honestly labelled so the two populations can be separated in
    analysis. When neither exists there is no reference at all, and that must be a
    refusal rather than a guess: returning 0.0 here makes the ratio 0.0, which reads
    as "extremely thin" and would arm S2 instead. The caller turns it into a distinct
    NO_BASELINE verdict.

    Returns (rate, source).
    """
    baseline = candles[:window_start_index]
    if len(baseline) >= min_candles:
        rate = _session_rate(baseline)
        if rate > 0:
            return rate, "SESSION"
    if prior_rate and prior_rate > 0:
        return float(prior_rate), "PRIOR_SESSION"
    return 0.0, "NONE"


def _verdict(consecutive, ratio, baseline_source, min_candles,
             min_rate_ratio, max_rate_ratio, allow_rejected,
             discriminator=None, min_outside=None, max_outside=None):
    """The verdict, from whichever quantity config says decides it.

    `allow_rejected` CARRIES A STRATEGY DECISION, not a detail. While price is STILL
    OUTSIDE value the excursion is in progress, and an in-progress excursion cannot be
    called rejected - it can only be not-yet-accepted. Rejection is CONFIRMED by price
    closing back inside value, which is a different code path and is S2's actual trigger.

    So the live path passes False and the ended-excursion path passes True. Letting the
    live path return REJECTED would arm S2 on a move that has not returned yet, turning
    "fade a confirmed rejection" into "catch a falling knife" - a different strategy
    wearing the same name, and the kind of change that would never show up as a test
    failure because both versions produce trades.

    ONE PLACE, so the live path, the ended-excursion path and the research sweep cannot
    drift apart - they did before, which is how an inert discriminator survived three
    separate bug fixes without anyone comparing the two branches.

    DURATION (default). Acceptance is a claim about TIME: price is accepted when it trades
    outside value long enough for value to develop out there. Measured on 14,268 excursions
    with one independent read each, the continuation rate rises monotonically with duration
    across every band - 28.4% below an hour to 73.1% past eight - and duration holds
    AUC 0.61 with excursion distance held fixed. It needs no denominator at all, which also
    removes this module's entire recurring failure mode: there is no baseline for a
    numerator to contaminate.

    VOLUME_RATE. The original, kept as an ablation arm rather than deleted. The same
    control puts it at ~0.50 - it is a proxy for distance and adds nothing to it. A
    Phase 1 run already exists under this setting, which makes it the control for the
    change rather than a branch nobody can test.
    """
    discriminator = (config.ACCEPT_DISCRIMINATOR if discriminator is None
                     else discriminator)
    if discriminator == "duration":
        min_outside = (config.ACCEPT_MIN_CANDLES_OUTSIDE if min_outside is None
                       else min_outside)
        max_outside = (config.REJECT_MAX_CANDLES_OUTSIDE if max_outside is None
                       else max_outside)
        # The floor stays: below min_candles the excursion is too short to describe at all,
        # whatever its duration threshold says.
        if consecutive < min_candles:
            return PENDING
        if consecutive >= min_outside:
            return ACCEPTED
        if allow_rejected and consecutive <= max_outside:
            return REJECTED
        return PENDING

    # volume_rate: the baseline matters here, because the ratio has a denominator.
    if baseline_source == "NONE":
        return NO_BASELINE
    if consecutive >= min_candles and ratio >= min_rate_ratio:
        return ACCEPTED
    if allow_rejected and consecutive >= min_candles and ratio <= max_rate_ratio:
        return REJECTED
    return PENDING


def evaluate(candles, levels, atr, min_candles=None,
             min_rate_ratio=None, max_rate_ratio=None,
             prior_rate=None, min_baseline_candles=None):
    """Classify the current excursion beyond `levels`' value area.

    `candles` are confirmation-timeframe candles in ascending time order, closed
    only. The window examined is the CURRENT excursion: it starts at the first
    candle after the last one that closed inside value, so the measurement always
    describes the move in progress rather than an average over the session.

    `prior_rate` is the previous session's volume per confirmation candle, used as the
    baseline when this session has not yet traded enough candles inside value to
    supply one. See _baseline_rate - passing it is strongly recommended, because
    without it a session that opens outside value cannot be measured at all.

    Stateless by design. The same candles and levels always produce the same
    verdict, in live and in replay, with no accumulated state to diverge.
    """
    min_candles = config.ACCEPT_MIN_CANDLES if min_candles is None else min_candles
    min_rate_ratio = (config.ACCEPT_MIN_VOLUME_RATE_RATIO
                      if min_rate_ratio is None else min_rate_ratio)
    max_rate_ratio = (config.REJECT_MAX_VOLUME_RATE_RATIO
                      if max_rate_ratio is None else max_rate_ratio)
    min_baseline_candles = (config.ACCEPT_MIN_BASELINE_CANDLES
                            if min_baseline_candles is None else min_baseline_candles)
    atr = float(atr) if atr and atr > 0 else 0.0

    if not candles or levels is None:
        return Acceptance(verdict=INSIDE, side="NONE")

    latest = candles[-1]

    # Find where the current excursion began: walk back to the most recent candle
    # that CLOSED inside value. Everything after it is the excursion window.
    start_index = 0
    for index in range(len(candles) - 1, -1, -1):
        if _current_side(candles[index].close, levels) == "NONE":
            start_index = index + 1
            break

    window = candles[start_index:]
    session_rate, baseline_source = _baseline_rate(
        candles, start_index, prior_rate, min_baseline_candles)

    # The latest candle closed inside value. Either nothing is happening, or a
    # move just returned - and a return is what S2 waits for, so it is reported
    # explicitly rather than as an absence.
    if _current_side(latest.close, levels) == "NONE":
        prior = candles[:-1]
        prior_side = "NONE"
        for candle in reversed(prior):
            side = _current_side(candle.close, levels)
            if side != "NONE":
                prior_side = side
                break

        if prior_side == "NONE":
            return Acceptance(verdict=INSIDE, side="NONE",
                              candles_in_window=len(window),
                              session_rate=session_rate,
                              baseline_source=baseline_source)

        # Re-measure the excursion that just ended, so the reversion decision is
        # made against the same numbers the breakout decision would have used. The
        # baseline is recomputed inside, against that excursion's own start index.
        ended = _measure_ended_excursion(prior, levels, prior_side, atr,
                                        prior_rate, min_baseline_candles)
        ended.returned_inside = True
        ended.return_ts = latest.open_time

        # No independent reference means no measurement - but ONLY for the ratio, which
        # has a denominator to contaminate. Duration has none, so under that
        # discriminator a missing volume baseline does not impair the measurement that
        # actually decides, and refusing here would throw away a sound reading.
        if (config.ACCEPT_DISCRIMINATOR != "duration"
                and ended.baseline_source == "NONE"):
            ended.verdict = NO_BASELINE
            return ended

        # Three distinct outcomes, not two. Price has returned inside value, so this is
        # never a live breakout - but WHY it returned matters:
        #   brief / thin out there -> REJECTED: a spike nobody transacted at. S2's case.
        #   long / heavy out there -> FAILED_AUCTION: real business was done out there and
        #                             then reclaimed. A different, stronger event, and not
        #                             traded in v1.
        #   in between             -> PENDING: genuinely ambiguous.
        #
        # Duration reads this split more naturally than volume rate does: an excursion
        # that held outside value for four hours and then came back IS a failed auction,
        # while a three-candle spike that came back is a rejection. Same three categories,
        # measured by the quantity Phase 0 found to carry the information.
        if config.ACCEPT_DISCRIMINATOR == "duration":
            outside = ended.consecutive_closes_outside
            if outside <= config.REJECT_MAX_CANDLES_OUTSIDE:
                ended.verdict = REJECTED
            elif outside >= config.ACCEPT_MIN_CANDLES_OUTSIDE:
                ended.verdict = FAILED_AUCTION
            else:
                ended.verdict = PENDING
            return ended

        if ended.volume_rate_ratio <= max_rate_ratio:
            ended.verdict = REJECTED
        elif ended.volume_rate_ratio >= min_rate_ratio:
            ended.verdict = FAILED_AUCTION
        else:
            ended.verdict = PENDING
        return ended

    side = _current_side(latest.close, levels)
    bound = levels.vah if side == "ABOVE" else levels.val

    consecutive = 0
    for candle in reversed(window):
        if _current_side(candle.close, levels) == side:
            consecutive += 1
        else:
            break

    volume_outside = sum(_volume_outside(candle, bound, side) for candle in window)
    session_total = sum(candle.volume for candle in candles)
    outside_rate = (volume_outside / len(window)) if window else 0.0
    ratio = (outside_rate / session_rate) if session_rate > 0 else 0.0
    fraction = (volume_outside / session_total) if session_total > 0 else 0.0

    if side == "ABOVE":
        extreme = max(candle.high for candle in window)
        distance = extreme - levels.vah
    else:
        extreme = min(candle.low for candle in window)
        distance = levels.val - extreme

    # allow_rejected=False: this excursion is still in progress. See _verdict.
    verdict = _verdict(consecutive, ratio, baseline_source, min_candles,
                       min_rate_ratio, max_rate_ratio, allow_rejected=False)

    return Acceptance(
        verdict=verdict,
        side=side,
        baseline_source=baseline_source,
        excursion_start_ts=window[0].open_time if window else latest.open_time,
        excursion_extreme=extreme,
        excursion_distance=distance,
        excursion_distance_atr=(distance / atr) if atr > 0 else 0.0,
        candles_in_window=len(window),
        consecutive_closes_outside=consecutive,
        volume_outside=volume_outside,
        volume_total=session_total,
        volume_outside_fraction=fraction,
        outside_rate=outside_rate,
        session_rate=session_rate,
        volume_rate_ratio=ratio,
    )


def _measure_ended_excursion(candles, levels, side, atr, prior_rate=None,
                             min_baseline_candles=None):
    """Measure the excursion that ended just before a return inside value.

    Symmetry with the live path is the point: S2's rejection verdict and S3's
    acceptance verdict must be computed from identical arithmetic, or the mutual
    exclusivity the state machine relies on is not real.

    THE WINDOW IS THE CONTIGUOUS OUTSIDE RUN, found by walking back from the LAST
    candle that closed outside value. An earlier version reused the live path's
    "everything after the last inside close" rule, which is wrong here for a subtle
    reason: by the time this function is called, price has ALREADY returned inside, so
    the most recent inside close is the return itself. The window then became the
    post-return candles rather than the excursion, and every measurement taken from it
    described the wrong candles - producing a NEGATIVE excursion distance, since the
    minimum low of a window sitting inside value is above the lower bound.
    """
    bound = levels.vah if side == "ABOVE" else levels.val

    # Last candle that closed on the excursion's side.
    end_index = None
    for index in range(len(candles) - 1, -1, -1):
        if _current_side(candles[index].close, levels) == side:
            end_index = index
            break

    if end_index is None:
        return Acceptance(verdict=INSIDE, side="NONE")

    # Back to the start of that contiguous run.
    start_index = end_index
    while start_index > 0 and \
            _current_side(candles[start_index - 1].close, levels) == side:
        start_index -= 1

    window = candles[start_index:end_index + 1]

    # Baseline from the candles BEFORE this excursion, never from the excursion
    # itself - the same independence requirement as the live path. See _baseline_rate.
    min_baseline_candles = (config.ACCEPT_MIN_BASELINE_CANDLES
                            if min_baseline_candles is None else min_baseline_candles)
    session_rate, baseline_source = _baseline_rate(
        candles, start_index, prior_rate, min_baseline_candles)

    consecutive = 0
    for candle in reversed(window):
        if _current_side(candle.close, levels) == side:
            consecutive += 1
        else:
            break

    volume_outside = sum(_volume_outside(candle, bound, side) for candle in window)
    session_total = sum(candle.volume for candle in candles)
    outside_rate = (volume_outside / len(window)) if window else 0.0
    ratio = (outside_rate / session_rate) if session_rate > 0 else 0.0
    fraction = (volume_outside / session_total) if session_total > 0 else 0.0

    if side == "ABOVE":
        extreme = max(candle.high for candle in window)
        distance = extreme - levels.vah
    else:
        extreme = min(candle.low for candle in window)
        distance = levels.val - extreme

    return Acceptance(
        verdict=PENDING,
        side=side,
        baseline_source=baseline_source,
        excursion_start_ts=window[0].open_time,
        excursion_extreme=extreme,
        excursion_distance=distance,
        excursion_distance_atr=(distance / atr) if atr > 0 else 0.0,
        candles_in_window=len(window),
        consecutive_closes_outside=consecutive,
        volume_outside=volume_outside,
        volume_total=session_total,
        volume_outside_fraction=fraction,
        outside_rate=outside_rate,
        session_rate=session_rate,
        volume_rate_ratio=ratio,
    )
