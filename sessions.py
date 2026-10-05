"""The session calendar. Everything here is UTC, deliberately and without an
option to change it.

WHY UTC IS NOT A PREFERENCE. Binance USD-M exposes no timezone setting: every
timestamp on every REST and WebSocket payload is Unix milliseconds in UTC, 1d
klines open at exactly 00:00:00.000 UTC, and funding settles at 00/08/16 UTC.
So the exchange session IS the UTC day - the boundary is the venue's own, which
means any level derived from it can be independently verified against a 1d
kline rather than trusted.

The second reason matters more for research than for trading. A calendar
anchored to local clock time - London, New York, a broker's server time - shifts
by an hour twice a year, in each region, on DIFFERENT dates. That silently
re-defines every level twice a year and makes any backtest spanning the change
incomparable with itself. UTC has no such discontinuity, ever.

Regional windows still exist here, but ONLY as tags recorded on candidates for
later measurement. They overlap (London 07-16 and New York 12-21 share four
hours), so they cannot partition volume and therefore cannot define a profile
period. Treating them as features rather than periods is the distinction that
keeps them honest.
"""
from datetime import datetime, timedelta, timezone

import config

MS_MINUTE = 60_000
MS_HOUR = 3_600_000
MS_DAY = 86_400_000


# --------------------------------------------------------------- conversions

def to_ms(dt):
    return int(dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp() * 1000)


def to_utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# ------------------------------------------------------------ daily sessions

def session_start_ms(ts_ms):
    """Open of the UTC day containing ts_ms - identical to the 1d kline open.

    Floor division on the epoch is exact here because the Unix epoch itself
    begins at 00:00:00 UTC and UTC has no offset changes, so every day boundary
    is an exact multiple of MS_DAY. This would NOT be safe for a local-time
    calendar, which is the whole argument of this module.
    """
    return (int(ts_ms) // MS_DAY) * MS_DAY


def session_end_ms(ts_ms):
    """Exclusive end of the session containing ts_ms."""
    return session_start_ms(ts_ms) + MS_DAY


def session_id(ts_ms):
    """Stable human-readable id, e.g. '2026-09-27'. Used as a journal key."""
    return to_utc(session_start_ms(ts_ms)).strftime("%Y-%m-%d")


def session_start_ms_from_id(session_id_str):
    """Inverse of session_id: '2026-09-27' -> that session's open in epoch ms.

    Exact, and exact only because the calendar is UTC. A local-clock calendar has
    two 01:30s on one day of the year and none on another, so a date string there
    does not identify a unique instant - the round trip
    session_id(session_start_ms_from_id(x)) == x would fail twice a year, silently,
    on whichever days the region moved its clocks.

    Round-tripping through the id rather than keeping raw milliseconds is what lets
    the research corpus be addressed by filename.
    """
    parsed = datetime.strptime(session_id_str, "%Y-%m-%d").replace(
        tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def current_session(ts_ms=None):
    """(start, end) of the session in progress. The DEVELOPING profile window."""
    ts_ms = now_ms() if ts_ms is None else ts_ms
    start = session_start_ms(ts_ms)
    return start, start + MS_DAY


def previous_session(ts_ms=None):
    """(start, end) of the last COMPLETED session.

    This is the window whose profile gets frozen and supplies the reference
    levels - POC, VAH, VAL - for the whole of the current session.
    """
    ts_ms = now_ms() if ts_ms is None else ts_ms
    start = session_start_ms(ts_ms) - MS_DAY
    return start, start + MS_DAY


def nth_previous_session(ts_ms, n):
    """(start, end) of the session n days before the one containing ts_ms.

    n=1 is previous_session. Used by the composite builder and by the naked-POC
    registry, which needs to walk backwards through frozen profiles.
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    start = session_start_ms(ts_ms) - n * MS_DAY
    return start, start + MS_DAY


def session_elapsed_minutes(ts_ms=None):
    ts_ms = now_ms() if ts_ms is None else ts_ms
    return (ts_ms - session_start_ms(ts_ms)) / MS_MINUTE


def session_elapsed_fraction(ts_ms=None):
    return min(1.0, max(0.0, session_elapsed_minutes(ts_ms) / 1440.0))


# ----------------------------------------------------------- weekly composite

def week_start_ms(ts_ms):
    """Monday 00:00 UTC of the week containing ts_ms.

    Chosen over Sunday because Binance's own weekly kline uses Monday, so the
    same verifiability argument as the daily boundary applies.
    """
    day_start = session_start_ms(ts_ms)
    weekday = to_utc(day_start).weekday()        # Monday == 0
    return day_start - weekday * MS_DAY


def current_week(ts_ms=None):
    ts_ms = now_ms() if ts_ms is None else ts_ms
    start = week_start_ms(ts_ms)
    return start, start + 7 * MS_DAY


def previous_week(ts_ms=None):
    ts_ms = now_ms() if ts_ms is None else ts_ms
    start = week_start_ms(ts_ms) - 7 * MS_DAY
    return start, start + 7 * MS_DAY


# --------------------------------------------------------------- funding guard

def _funding_boundaries_around(ts_ms):
    """Every funding instant within a day either side of ts_ms.

    Computed as a window rather than a modulo so a guard spanning midnight (the
    00:00 event) is handled by the same code path as any other.
    """
    day = session_start_ms(ts_ms)
    out = []
    for offset_days in (-1, 0, 1):
        base = day + offset_days * MS_DAY
        for hour in config.FUNDING_HOURS_UTC:
            out.append(base + hour * MS_HOUR)
    return out


def minutes_to_nearest_funding(ts_ms=None):
    ts_ms = now_ms() if ts_ms is None else ts_ms
    nearest = min(_funding_boundaries_around(ts_ms), key=lambda b: abs(b - ts_ms))
    return abs(nearest - ts_ms) / MS_MINUTE


def in_funding_guard(ts_ms=None):
    """True inside +/- FUNDING_GUARD_MINUTES of a funding settlement.

    Funding events concentrate volume into a few minutes and distort both the
    developing profile and the fill quality of a passive entry. Suppressing
    entries there is a mechanical precaution, not a market opinion - but the
    window width is still PROVISIONAL and gets swept in Phase 4.
    """
    return minutes_to_nearest_funding(ts_ms) <= config.FUNDING_GUARD_MINUTES


# ------------------------------------------------------- regional tags (features)

def _parse_window(spec):
    start_s, end_s = spec.split("-")
    sh, sm = (int(part) for part in start_s.split(":"))
    eh, em = (int(part) for part in end_s.split(":"))
    return sh * 60 + sm, eh * 60 + em


def _tag_windows():
    return {
        "ASIA": _parse_window(config.SESSION_TAG_ASIA),
        "LONDON": _parse_window(config.SESSION_TAG_LONDON),
        "NEWYORK": _parse_window(config.SESSION_TAG_NEWYORK),
    }


def session_tags(ts_ms=None):
    """Every regional window containing ts_ms - a list, because they overlap.

    Returning a list rather than a single label is the point: 13:00 UTC is
    genuinely inside both London and New York, and collapsing that to one name
    would invent information. Recorded on candidates as `origin_session`.
    """
    ts_ms = now_ms() if ts_ms is None else ts_ms
    minute_of_day = (ts_ms - session_start_ms(ts_ms)) // MS_MINUTE
    tags = []
    for name, (start, end) in _tag_windows().items():
        if start <= minute_of_day < end:
            tags.append(name)
    return tags or ["OFF_HOURS"]


# --------------------------------------------------------- developing maturity

def developing_maturity(ts_ms, session_quote_volume, typical_quote_volume):
    """How much of a profile the current session has actually built yet.

    An immature developing profile has an unstable POC by construction - with
    twenty minutes of data the argmax is noise - so setups that read the CURRENT
    session are gated on this rather than on elapsed time alone. Volume is the
    better of the two measures because a quiet morning builds less of a profile
    than a busy one of the same length, so both floors must pass.

    Returns a dict rather than a bool so the journal records WHY a setup was
    held back, not merely that it was.
    """
    elapsed = session_elapsed_minutes(ts_ms)
    volume_fraction = (
        (session_quote_volume / typical_quote_volume)
        if typical_quote_volume and typical_quote_volume > 0 else 0.0
    )
    return {
        "elapsed_minutes": elapsed,
        "elapsed_fraction": session_elapsed_fraction(ts_ms),
        "volume_fraction": volume_fraction,
        "elapsed_ok": elapsed >= config.DEVELOPING_MIN_ELAPSED_MINUTES,
        "volume_ok": volume_fraction >= config.DEVELOPING_MIN_VOLUME_FRACTION,
        "mature": (
            elapsed >= config.DEVELOPING_MIN_ELAPSED_MINUTES
            and volume_fraction >= config.DEVELOPING_MIN_VOLUME_FRACTION
        ),
    }


# ------------------------------------------------------------------- boundaries

def crossed_session_boundary(previous_ts_ms, current_ts_ms):
    """True when a session rolled over between two observations.

    The runtime uses this to freeze the completed profile, cancel pending
    candidates (their reference profile is no longer current) and reset the
    per-symbol state machines - while leaving open positions alone, since their
    stop and target were derived from the profile that was current at entry.
    """
    return session_start_ms(previous_ts_ms) != session_start_ms(current_ts_ms)


def describe(ts_ms=None):
    """One-line session context for logs and heartbeats."""
    ts_ms = now_ms() if ts_ms is None else ts_ms
    prev_start, _ = previous_session(ts_ms)
    return (
        f"session={session_id(ts_ms)} "
        f"elapsed={session_elapsed_minutes(ts_ms):.0f}m "
        f"prev={session_id(prev_start)} "
        f"tags={'+'.join(session_tags(ts_ms))} "
        f"funding_in={minutes_to_nearest_funding(ts_ms):.0f}m"
        f"{' GUARD' if in_funding_guard(ts_ms) else ''}"
    )
