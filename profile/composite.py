"""Naked POCs: prices the auction agreed on, left, and has not revisited.

A POC is the price at which the most volume transacted in a session - the auction's
own statement about where value was. When a later session never trades through that
price, the business there is unfinished, and unfinished business is the strongest
NON-ARBITRARY target a volume profile offers. Every other target in this system is
either a fixed multiple of risk (an opinion about payoff, not about the market) or a
level from the CURRENT profile (which is where price already is). A naked POC is a
level price has to travel to reach.

WHY THIS MODULE EXISTS AT ALL - the approximation it replaces was actively misleading.
`scanner.naked_pocs` estimated each prior session's POC by its TYPICAL PRICE, (H+L+C)/3.
A POC is a volume MODE; a typical price is a geometric average of three extremes. On a
balanced session they land close together, which is what made the approximation look
reasonable. On a trend session - exactly the session whose POC is most worth targeting,
because value migrated and left a shelf behind - they can sit most of the range apart,
and the estimate then names a price at which comparatively little traded.

That mattered beyond accuracy. `TARGET_MODE=structural` is the measured challenger to
`fixed_r` in Phase 2, and it draws its targets from this list. Feeding it invented
levels would have handicapped it in the comparison and produced the same unfair result
as the `structural_target` self-rejection bug: a mode losing a head-to-head because of
its inputs rather than its thesis.

WHERE THE DATA COMES FROM, AND WHY IT IS FREE. Computing a session's true POC needs
that session's 1m candles, and re-fetching ten sessions per symbol per cycle would be
~1,200 REST calls a minute on the trade universe - unaffordable. But nothing needs to
be re-fetched: `Scanner.frozen_bundle` already builds the previous session's complete
profile and levels once per symbol per day, and already writes `poc_price`, `high` and
`low` to `profile_snapshots`. A closed session's POC is immutable, so the registry is a
READ over data the system was already producing. Live it fills in as the bot runs;
replay populates it from the corpus, which has every session on disk.

The honest consequence of that design is a cold start: a fresh deployment has no prior
snapshots and so no naked POCs, and the list fills over the following days. That is
recorded rather than papered over - `coverage()` reports it - because the alternative is
falling back to the approximation this module exists to remove, and a target drawn from
a fabricated level is worse than no target. Setups that find no naked POC fall back to
their structural levels, which is a documented path, not a failure.
"""
import logging
from dataclasses import dataclass

import sessions

log = logging.getLogger(__name__)

# How many prior sessions to consider. Ten sessions is two trading weeks: far enough
# back that a genuine shelf is still relevant, near enough that the level has not been
# overtaken by a regime change. A POC from six weeks ago is archaeology.
DEFAULT_LOOKBACK_SESSIONS = 10

# How many to hand a setup. The list is newest-first, so a cap keeps the nearest and
# most recent unfinished business rather than an arbitrary subset.
DEFAULT_LIMIT = 5


@dataclass(frozen=True)
class SessionPoc:
    """One session's POC, with the extremes needed to test LATER sessions against it."""
    symbol: str
    session_id: str
    poc_price: float
    high: float
    low: float

    @property
    def session_start_ms(self):
        return sessions.session_start_ms_from_id(self.session_id)


@dataclass(frozen=True)
class NakedPoc:
    """A POC price that has survived every later session's range."""
    price: float
    session_id: str
    sessions_ago: int
    # Distance is left to the caller: it depends on the current price, which this
    # module deliberately does not know. Mixing "is this level naked" with "is this
    # level near" is how a registry turns into a setup.

    def __float__(self):
        return float(self.price)


class NakedPocRegistry:
    """Per-symbol store of prior session POCs, and the nakedness test over them.

    Not a cache of a computation - a record of history. Entries are only ever added,
    because a closed session's POC cannot change. What DOES change is whether a POC is
    still naked, and that is recomputed on every read rather than stored, since it
    depends on all the sessions since.
    """

    def __init__(self, lookback_sessions=DEFAULT_LOOKBACK_SESSIONS):
        self._by_symbol = {}          # symbol -> {session_id: SessionPoc}
        self._lookback = int(lookback_sessions)

    # ------------------------------------------------------------- population

    def record(self, symbol, session_id, poc_price, high, low):
        """Add one session's POC. Idempotent - re-recording the same session is a no-op.

        Silently ignores a non-positive POC or an inverted range rather than storing a
        level that would later be proposed as a target.
        """
        if not poc_price or poc_price <= 0:
            return False
        if high is None or low is None or high < low:
            return False
        symbol = symbol.upper()
        entry = SessionPoc(symbol=symbol, session_id=str(session_id),
                           poc_price=float(poc_price),
                           high=float(high), low=float(low))
        self._by_symbol.setdefault(symbol, {})[entry.session_id] = entry
        return True

    def record_bundle(self, symbol, session_id, profile, levels):
        """Record from the objects `frozen_bundle` already has in hand.

        This is the live population path, and it costs nothing: the profile and levels
        were built for the setups regardless.
        """
        if levels is None or profile is None:
            return False
        return self.record(symbol, session_id, levels.poc_price,
                           profile.high, profile.low)

    def load_from_journal(self, journal, symbol, limit=None):
        """Populate from `profile_snapshots`, which the live bot writes every day.

        Reads the newest rows first so a long history does not have to be scanned. A
        journal that cannot be read leaves the registry empty rather than raising: a
        missing target hint must never stop a scan.
        """
        limit = int(limit or self._lookback * 3)
        try:
            rows = journal.recent_profile_pocs(symbol, limit=limit)
        except Exception as exc:                          # noqa: BLE001
            log.debug("%s naked-POC load failed: %s", symbol, exc)
            return 0
        added = 0
        for row in rows or []:
            if self.record(symbol, row["session_id"], row["poc_price"],
                           row["high"], row["low"]):
                added += 1
        return added

    # ------------------------------------------------------------------ reads

    def sessions_known(self, symbol):
        return len(self._by_symbol.get(symbol.upper(), {}))

    def coverage(self, symbol):
        """How much of the lookback window is actually populated, in [0, 1].

        Exists so a caller can tell "no naked POCs because price revisited them all"
        from "no naked POCs because this deployment is two days old". Those are
        opposite facts and an empty list alone cannot distinguish them.
        """
        if self._lookback <= 0:
            return 0.0
        return min(1.0, self.sessions_known(symbol) / float(self._lookback))

    def naked(self, symbol, before_session_id, daily_candles=None,
              current_high=None, current_low=None, limit=DEFAULT_LIMIT):
        """Naked POCs for `symbol`, newest first, strictly before `before_session_id`.

        ANTI-LOOKAHEAD, and there are three separate ways it could leak here:

          1. The current session's own POC is not a prior POC. Sessions at or after
             `before_session_id` are excluded outright.
          2. A POC is tested only against sessions AFTER it formed and BEFORE the
             current one. A later session's range is future information relative to an
             earlier POC but past information relative to now, which is exactly what
             makes this test legal.
          3. The current session may legitimately have traded through a level already,
             and ignoring that would report a level as naked after price passed it. So
             `current_high`/`current_low` are accepted - but they must be computed from
             candles up to the decision time, which is the caller's job, and they are
             OPTIONAL so a caller that cannot honestly supply them simply omits them.

        `daily_candles` supplies the later sessions' ranges. Daily bars are the right
        granularity: a POC that lies anywhere inside a later session's high-low has been
        traded through, and whether it was touched for one minute or held for six hours
        does not change that it is no longer unfinished business. Recorded sessions are
        used as a fallback when daily bars are unavailable, so the test still works in
        replay against a corpus.
        """
        symbol = symbol.upper()
        known = self._by_symbol.get(symbol)
        if not known:
            return []

        boundary = str(before_session_id)
        # Session ids are ISO dates, so a string comparison IS a chronological one -
        # the same property sessions.py relies on. Kept explicit because it is only
        # true for zero-padded ISO, and a change of id format would break it silently.
        candidates = sorted((entry for entry in known.values()
                             if entry.session_id < boundary),
                            key=lambda entry: entry.session_id, reverse=True)
        candidates = candidates[:self._lookback]
        if not candidates:
            return []

        ranges = self._later_ranges(symbol, daily_candles, boundary)

        out = []
        for index, entry in enumerate(candidates):
            price = entry.poc_price
            if self._traded_through(price, entry.session_id, boundary, ranges):
                continue
            if (current_high is not None and current_low is not None
                    and current_low <= price <= current_high):
                continue
            out.append(NakedPoc(price=price, session_id=entry.session_id,
                                sessions_ago=index + 1))
            if len(out) >= limit:
                break
        return out

    def prices(self, *args, **kwargs):
        """`naked()` as bare floats, for callers that only want target candidates."""
        return [item.price for item in self.naked(*args, **kwargs)]

    # -------------------------------------------------------------- internals

    def _later_ranges(self, symbol, daily_candles, boundary):
        """(session_id, low, high) for every session strictly before the boundary.

        Daily candles preferred; recorded POC sessions as a fallback so replay works
        against a corpus with no daily series loaded.
        """
        ranges = []
        for candle in daily_candles or []:
            session_id = sessions.session_id(candle.open_time)
            if session_id >= boundary:
                continue          # the current session is handled by current_high/low
            ranges.append((session_id, float(candle.low), float(candle.high)))
        if ranges:
            return ranges
        for entry in self._by_symbol.get(symbol, {}).values():
            if entry.session_id < boundary:
                ranges.append((entry.session_id, entry.low, entry.high))
        return ranges

    @staticmethod
    def _traded_through(price, formed_session_id, boundary, ranges):
        """Did any session strictly after `formed_session_id` contain `price`?

        Strictly after: the POC's own session obviously contains its own POC, and
        counting that would make every POC instantly non-naked. That is the
        bound-derived-from-the-measure mistake this project has met repeatedly, so the
        comparison is `>` and never `>=`.
        """
        for session_id, low, high in ranges:
            if session_id <= formed_session_id or session_id >= boundary:
                continue
            if low <= price <= high:
                return True
        return False


# NO SEPARATE REPLAY PATH IS NEEDED, and that is worth stating because writing one was
# the obvious first move. `frozen_bundle` records each session's POC as it computes it,
# and replay walks a symbol's sessions in ascending order (`available_sessions` sorts,
# `replay_symbol` iterates in order), so processing session N records session N-1's POC
# and the registry warms up exactly as it does live. A corpus-scanning builder would have
# duplicated that for no gain, and it would have given replay a fully populated registry
# from its first session - which live never has. Letting both warm up the same way keeps
# the early-session coverage gap identical in research and production instead of
# flattering the replay.
