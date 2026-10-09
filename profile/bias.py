"""Session-level directional bias, combined from everything a manual trader reads before
ever looking at order flow: the previous day's profile shape, value migration between
sessions, where today opened relative to yesterday's value, today's own developing shape
once the session is mature, and where price sits against the previous calendar week's
composite POC. Order flow confirms a trade at a zone; it does not set the day's bias -
see research/CALIBRATION notes from the zone-watch redesign.

Each of the five casts at most one vote, BULL or BEAR; an unmeasured or neutral read casts
none. The label is whichever side has more votes, NEUTRAL on a tie or no votes. `strength`
is how many of the five voted, for use as a conviction modifier - not as a gate.
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


def session_bias(ctx):
    """(label, strength): BULL/BEAR/NEUTRAL and how many of the five signals agree."""
    votes = [
        shape_mod.day_bias(ctx.prior_shape),
        _migration_vote(ctx.migration),
        _open_vote(ctx.open_relationship),
        _dev_vote(ctx.dev_shape, ctx.maturity),
        _weekly_vote(ctx.weekly_levels, ctx.last_price, ctx.atr),
    ]
    votes = [vote for vote in votes if vote in ("BULL", "BEAR")]
    if not votes:
        return "NEUTRAL", 0
    bulls, bears = votes.count("BULL"), votes.count("BEAR")
    if bulls == bears:
        return "NEUTRAL", max(bulls, bears)
    return ("BULL" if bulls > bears else "BEAR"), max(bulls, bears)
