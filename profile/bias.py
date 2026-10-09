"""Session-level directional bias, combined from everything a manual trader reads before
ever looking at order flow: the previous day's profile shape, value migration between
sessions, where today opened relative to yesterday's value, and today's own developing
shape once the session is mature. Order flow confirms a trade at a zone; it does not set
the day's bias - see research/CALIBRATION notes from the zone-watch redesign.

Each of the four casts at most one vote, BULL or BEAR; an unmeasured or neutral read casts
none. The label is whichever side has more votes, NEUTRAL on a tie or no votes. `strength`
is how many of the four voted, for use as a conviction modifier - not as a gate.
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


def session_bias(ctx):
    """(label, strength): BULL/BEAR/NEUTRAL and how many of the four signals agree."""
    votes = [
        shape_mod.day_bias(ctx.prior_shape),
        _migration_vote(ctx.migration),
        _open_vote(ctx.open_relationship),
        _dev_vote(ctx.dev_shape, ctx.maturity),
    ]
    votes = [vote for vote in votes if vote in ("BULL", "BEAR")]
    if not votes:
        return "NEUTRAL", 0
    bulls, bears = votes.count("BULL"), votes.count("BEAR")
    if bulls == bears:
        return "NEUTRAL", max(bulls, bears)
    return ("BULL" if bulls > bears else "BEAR"), max(bulls, bears)
