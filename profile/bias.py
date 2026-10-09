"""Session-level directional bias, combined from everything a manual trader reads before
ever looking at order flow: the previous day's profile shape, value migration between
sessions, where today opened relative to yesterday's value, today's own developing shape
once the session is mature, where price sits against the previous calendar week's
composite POC, and where price sits against today's own live VWAP. Order flow confirms a
trade at a zone; it does not set the day's bias - see research/CALIBRATION notes from the
zone-watch redesign.

Each of the six casts at most one vote, BULL or BEAR; an unmeasured or neutral read casts
none. The label is whichever side has more votes, NEUTRAL on a tie or no votes. `strength`
is how many of the six voted, for use as a conviction modifier - not as a gate.

Two of the six - weekly POC and today's VWAP - read oppositely by design, not by
oversight: the weekly vote is TREND-FOLLOWING (above the week's centre of business
supports more upside), the VWAP vote is MEAN-REVERTING (below today's own average
supports a bounce back up to it) - the same two readings a manual trader holds at
once on different timeframes, validated independently of each other below.
"""
from profile import shape as shape_mod


def _migration_vote(migration):
    if migration is None or not migration.directional:
        return None
    return "BULL" if migration.poc_shift_atr > 0 else "BEAR"


def _open_vote(open_relationship):
    if open_relationship is None or open_relationship.label == "INSIDE_VALUE":
        return None
    return "BULL" if open_relationship.side == "ABOVE" else "BEAR"


def _dev_vote(dev_shape, maturity):
    if dev_shape is None or not maturity or not maturity.get("mature"):
        return None
    # require_close_location=False: today's own session has not closed yet, so
    # there is no settled close to confirm against - the close-location
    # validation in day_bias() was measured against COMPLETED sessions only.
    return shape_mod.day_bias(dev_shape, require_close_location=False)


def _weekly_vote(weekly_levels, price, atr):
    """BULL if price sits above the previous calendar week's composite POC, BEAR
    if below - independent of the other four votes, which only ever look one
    session back and so cannot distinguish a genuine reversal from a bounce
    inside a larger multi-day move still centred well away from price.

    Validated 2026-10-09 against 280 real S4-OFR trades (calibration/
    approach_persistence_merged_with_weekly.csv): aligned with the weekly POC
    38.4% win / +0.122R vs. counter to it 29.3% win / -0.180R (AUC 0.58, 95% CI
    [0.51, 0.65] - excludes 0.5), holding separately for BUY (39.0% vs 31.9%) and
    SELL (37.5% vs 26.2%), and not explained by the other four votes together
    (bias_strength AUC 0.51, null, on the same aligned/counter split).

    None when there is no weekly bundle yet (a fresh listing's normal cold
    start), no ATR to normalise by, or price sits exactly on the POC.
    """
    if weekly_levels is None or not atr or atr <= 0:
        return None
    if price == weekly_levels.poc_price:
        return None
    return "BULL" if price > weekly_levels.poc_price else "BEAR"


def _dev_vwap_vote(dev_levels, price):
    """BULL if price is trading BELOW today's own live VWAP (bought at a discount
    to the session so far, supporting a reversion back up), BEAR if above (sold
    at a premium, supporting a reversion back down) - the opposite sign
    convention from _weekly_vote on purpose, see session_bias's module note:
    this is a mean-reversion read of the CURRENT session, not a trend read of a
    completed one, which is exactly how a manual trader holds both at once.

    Validated 2026-10-09 against 560 real S4-OFR trades (pooled pre/post-fix
    replay corpus, calibration/pooled_with_dev_vwap.csv) after an earlier,
    smaller (n=280) pass came back short of significant: aligned 38.3% win /
    +0.044R vs. counter 29.5% win / -0.097R (AUC 0.556, 95% CI [0.506, 0.607] -
    excludes 0.5 at this sample size). Confound-checked against _weekly_vote
    (AUC 0.489, null - genuinely independent) with a large joint effect: both
    aligned 42.3% win / +0.194R vs. both counter 23.8% win / -0.261R.

    None when there is no developing VWAP yet (the session has no candles, or
    its band has collapsed to zero width) or price sits exactly on it.
    """
    if dev_levels is None or dev_levels.vwap is None:
        return None
    sigma = dev_levels.vwap_upper_1sd - dev_levels.vwap
    if sigma <= 0 or price == dev_levels.vwap:
        return None
    return "BULL" if price < dev_levels.vwap else "BEAR"


def session_bias(ctx):
    """(label, strength): BULL/BEAR/NEUTRAL and how many of the six signals agree."""
    votes = [
        shape_mod.day_bias(ctx.prior_shape),
        _migration_vote(ctx.migration),
        _open_vote(ctx.open_relationship),
        _dev_vote(ctx.dev_shape, ctx.maturity),
        _weekly_vote(ctx.weekly_levels, ctx.last_price, ctx.atr),
        _dev_vwap_vote(ctx.dev_levels, ctx.last_price),
    ]
    votes = [vote for vote in votes if vote in ("BULL", "BEAR")]
    if not votes:
        return "NEUTRAL", 0
    bulls, bears = votes.count("BULL"), votes.count("BEAR")
    if bulls == bears:
        return "NEUTRAL", max(bulls, bears)
    return ("BULL" if bulls > bears else "BEAR"), max(bulls, bears)
