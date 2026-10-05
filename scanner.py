"""The scan: from a symbol to a fully assembled SetupContext.

This is where the profile engine, the session calendar and the cache meet. One
function does the whole job for one symbol, and it is the same function the research
harness calls - which is what guarantees live and replay read identical inputs.

THE ORDER OF WORK, and why each step is where it is:

  1. DAILY CANDLES FIRST, because ATR is the unit everything else is measured in and
     bin width depends on it. A symbol with no usable daily history cannot be scanned
     at all, so this is the cheapest possible rejection.

  2. THE FROZEN PRIOR PROFILE, computed ONCE per symbol per session and cached.
     Yesterday's 1m candles are immutable once 00:00 UTC passes, so recomputing them
     every cycle would be pure waste - and the frozen/developing distinction is what
     keeps the reference levels stable for the whole day.

  3. THE DEVELOPING PROFILE, recomputed each cycle from the current session's
     candles, which is the only part that legitimately changes.

  4. CONFIRMATION CANDLES BY LOCAL AGGREGATION from the same 1m series, never by a
     separate fetch. One fewer request, and it makes a disagreement between the
     profile and the confirmation candles impossible rather than merely unlikely.

EVERYTHING IS as_of THE LAST CLOSED 1m CANDLE. Not wall-clock, not the last trade.
That single choice is what makes the whole pipeline replayable: the research harness
passes a simulated clock into the same call and gets the same answer.
"""
import logging
from dataclasses import dataclass

import config
import sessions
from data import klines as klines_mod
from profile import (builder, composite, levels as levels_mod, relations, va_hvn,
                     shape as shape_mod, stability as stability_mod)
from setups.context import SetupContext

log = logging.getLogger(__name__)


@dataclass
class WeeklyBundle:
    """The most recently COMPLETED calendar week's composite profile.

    Frozen for the whole current week once built - Monday-Sunday is complete the
    moment a new week starts, so recomputing it every cycle would be the same
    waste `FrozenBundle` exists to avoid at the daily grain.
    """
    profile: object
    levels: object


@dataclass
class FrozenBundle:
    """The previous session's profile and everything derived from it.

    Cached for the whole session because none of it can change. Grouped into one
    object so the cache holds a single entry per symbol-session rather than five
    that could fall out of step with each other.
    """
    profile: object
    levels: object
    shape: object
    stability: object
    bin_size: float
    atr: float
    typical_quote_volume: float


def _rebuilt_profiles(symbol, candles, start, end, as_of, bin_size):
    rebuilt_profiles = []
    for multiplier in config.STABILITY_BIN_MULTIPLIERS:
        size = bin_size * float(multiplier)
        rebuilt = builder.profile_as_of(symbol, candles, start, end, as_of, size, coverage=1.0)
        if not rebuilt.volume:
            continue
        rebuilt_levels = levels_mod.compute(rebuilt)
        if rebuilt_levels is not None:
            rebuilt_profiles.append((rebuilt, rebuilt_levels, size))
    return rebuilt_profiles


class Scanner:
    def __init__(self, rest, catalog, cache, journal=None):
        self._rest = rest
        self._catalog = catalog
        self._cache = cache
        self._journal = journal
        # One registry for the process lifetime. It accumulates each session's true POC
        # as frozen_bundle computes it, so the cost is zero and the coverage grows with
        # uptime. Keyed inside by symbol; `_poc_loaded` tracks which symbols have had
        # their history pulled from the journal, so that read happens once per symbol
        # rather than once per cycle.
        self._naked_pocs = composite.NakedPocRegistry()
        self._poc_loaded = set()
        # symbol -> (week_start_ms, WeeklyBundle). Same reasoning as the frozen-
        # daily cache: a completed calendar week never changes.
        self._weekly = {}

    # ------------------------------------------------------------ frozen side

    def frozen_bundle(self, symbol, session_id, as_of):
        """Build (or reuse) the frozen previous-session bundle.

        Returns (bundle, reason) - reason is a short string when the symbol cannot be
        scanned, so the caller can journal a specific refusal instead of a generic
        skip. Distinguishing "no daily history" from "profile has no levels" matters:
        the first is a new listing, the second is a broken session.
        """
        cached = self._cache.frozen(symbol, session_id)
        if cached is not None:
            return cached, None

        daily = self._cache.daily_candles(symbol)
        if len(daily) < 5:
            return None, "INSUFFICIENT_DAILY_HISTORY"

        atr = klines_mod.daily_atr(daily)
        if atr <= 0:
            return None, "ATR_ZERO"

        spec = self._catalog.get(symbol)
        if spec is None:
            return None, "UNKNOWN_SYMBOL"

        bin_size = builder.compute_bin_size(atr, spec.tick_size)
        if bin_size <= 0:
            return None, "BIN_SIZE_ZERO"

        prev_start, prev_end = sessions.previous_session(as_of)
        candles = self._cache.minute_candles(symbol, prev_start, prev_end)
        if not candles:
            return None, "NO_PRIOR_CANDLES"

        coverage = klines_mod.coverage(candles, "1m", prev_start, prev_end)

        # as_of is the session END here, not the caller's clock: the previous session
        # is complete by definition, so there is nothing to withhold.
        profile = builder.profile_as_of(symbol, candles, prev_start, prev_end,
                                       prev_end, bin_size, coverage=coverage)
        levels = levels_mod.compute(profile)
        if levels is None:
            return None, "NO_LEVELS"

        migration = shape_mod.intra_session_poc_migration(
            builder, symbol, candles, prev_start, prev_end, prev_end, bin_size, atr)
        shape = shape_mod.classify(profile, levels,
                                  intra_poc_migration_atr=migration)
        stability = stability_mod.assess(symbol, candles, prev_start, prev_end,
                                        prev_end, bin_size, levels)

        if config.HVN_METHOD == "va_plan":
            perturbed = _rebuilt_profiles(symbol, candles, prev_start, prev_end, prev_end,
                                          bin_size)
            levels.hvns = va_hvn.detect(profile, levels, atr, perturbed=perturbed)

        bundle = FrozenBundle(
            profile=profile, levels=levels, shape=shape, stability=stability,
            bin_size=bin_size, atr=atr,
            typical_quote_volume=klines_mod.typical_session_volume(daily),
        )
        self._cache.set_frozen(symbol, session_id, bundle)

        # THE PRIOR SESSION'S TRUE POC, recorded here because here is the one place it
        # is computed. Every other route to it would mean rebuilding a profile from 1m
        # candles that have already been discarded by persist_session below.
        self._naked_pocs.record_bundle(symbol, sessions.session_id(prev_start),
                                       profile, levels)

        if self._journal:
            self._journal.record_profile(profile, levels, shape,
                                         session_id=sessions.session_id(prev_start))

        # The raw prior candles are no longer needed once the profile exists.
        self._cache.persist_session(symbol, sessions.session_id(prev_start), candles)
        self._cache.trim(symbol, sessions.session_start_ms(as_of))

        return bundle, None

    # ---------------------------------------------------------------- weekly

    def frozen_weekly_bundle(self, symbol, as_of, bin_size):
        """Build (or reuse) the previous calendar week's composite profile.

        PLAN item 21. `SetupContext.weekly_levels` and the calendar-week helpers
        (`sessions.week_start_ms`/`previous_week`) were both already in the
        codebase, built for exactly this and never finished - not a new design,
        a completion of one already started. This is a genuine higher-timeframe
        composite, distinct from `ctx.migration` (which is only yesterday-vs-
        today and already measured as weak, findings 18/28): Monday 00:00 UTC
        through Sunday 23:59:59 UTC, aggregated on the SAME bin lattice as the
        daily profile (the caller's `bin_size`) so its POC/VAH/VAL sit on prices
        directly comparable to `prior_levels`' own.

        Optional context, never a scan-blocking requirement the way the daily
        bundle is - returns None (not a rejection reason) when the week has no
        usable candles, which is simply a fresh listing's normal cold start.
        """
        week_start, week_end = sessions.previous_week(as_of)
        symbol = symbol.upper()

        cached = self._weekly.get(symbol)
        if cached is not None and cached[0] == week_start:
            return cached[1]

        candles = self._cache.minute_candles(symbol, week_start, week_end)
        if not candles:
            return None

        coverage = klines_mod.coverage(candles, "1m", week_start, week_end)
        profile = builder.profile_as_of(symbol, candles, week_start, week_end,
                                       week_end, bin_size, coverage=coverage)
        levels = levels_mod.compute(profile)
        if levels is None:
            return None

        bundle = WeeklyBundle(profile=profile, levels=levels)
        self._weekly[symbol] = (week_start, bundle)
        return bundle

    # ---------------------------------------------------------------- context

    def build_context(self, symbol, as_of=None, mark_price=0.0):
        """Assemble the SetupContext for one symbol. Returns (ctx, reason)."""
        symbol = symbol.upper()
        as_of = sessions.now_ms() if as_of is None else as_of
        session_id = sessions.session_id(as_of)

        spec = self._catalog.get(symbol)
        if spec is None:
            return None, "UNKNOWN_SYMBOL"

        bundle, reason = self.frozen_bundle(symbol, session_id, as_of)
        if bundle is None:
            return None, reason

        cur_start, cur_end = sessions.current_session(as_of)
        session_candles = self._cache.minute_candles(symbol, cur_start, cur_end)
        if not session_candles:
            return None, "NO_SESSION_CANDLES"

        # THE as_of BOUNDARY. Every profile below is computed to the last CLOSED 1m
        # candle, never to wall-clock, so a partially formed bar can never influence
        # a decision. This is the line that makes the pipeline replayable.
        last_closed = session_candles[-1].close_time
        effective_as_of = min(as_of, last_closed)

        dev_profile = builder.profile_as_of(
            symbol, session_candles, cur_start, cur_end, effective_as_of,
            bundle.bin_size,
        )
        dev_levels = levels_mod.compute(dev_profile)
        if dev_levels is not None and config.HVN_METHOD == "va_plan":
            elapsed_ms = effective_as_of - cur_start
            if elapsed_ms >= config.DEV_HVN_MIN_MINUTES * 60_000:
                perturbed = _rebuilt_profiles(symbol, session_candles, cur_start, cur_end,
                                              effective_as_of, bundle.bin_size)
                dev_levels.hvns = va_hvn.detect(dev_profile, dev_levels, bundle.atr,
                                                perturbed=perturbed)
            else:
                dev_levels.hvns = []
        dev_migration = shape_mod.intra_session_poc_migration(
            builder, symbol, session_candles, cur_start, cur_end,
            effective_as_of, bundle.bin_size, bundle.atr)
        dev_shape = (shape_mod.classify(dev_profile, dev_levels,
                                       intra_poc_migration_atr=dev_migration)
                     if dev_levels is not None else None)

        # The open relationship uses the session's OPEN price - the first 1m candle's
        # open. On a perpetual there is no gap, so this equals the prior session's
        # close, and it is the unambiguous record of where the session began relative
        # to yesterday's value.
        open_price = session_candles[0].open
        open_relationship = relations.classify_open(
            open_price, bundle.levels, bundle.profile, bundle.atr)

        migration = (relations.classify_migration(dev_levels, bundle.levels,
                                                 bundle.atr)
                     if dev_levels is not None else None)

        weekly_bundle = self.frozen_weekly_bundle(symbol, as_of, bundle.bin_size)
        weekly_levels = weekly_bundle.levels if weekly_bundle is not None else None

        confirm_candles = klines_mod.resample(session_candles,
                                             config.CONFIRM_INTERVAL, "1m")
        if not confirm_candles:
            return None, "NO_CONFIRM_CANDLES"

        maturity = sessions.developing_maturity(
            effective_as_of,
            klines_mod.session_quote_volume(session_candles),
            bundle.typical_quote_volume,
        )

        closed_session = [c for c in session_candles if c.close_time <= effective_as_of]
        hvn_tests = tuple(
            va_hvn.touch_count(closed_session, node.low_price, node.high_price)
            for node in bundle.levels.hvns)

        ctx = SetupContext(
            symbol=symbol,
            spec=spec,
            as_of=effective_as_of,
            session_id=session_id,
            prior_profile=bundle.profile,
            prior_levels=bundle.levels,
            prior_shape=bundle.shape,
            prior_stability=bundle.stability,
            dev_profile=dev_profile,
            dev_levels=dev_levels,
            dev_shape=dev_shape,
            maturity=maturity,
            hvn_tests=hvn_tests,
            open_relationship=open_relationship,
            migration=migration,
            atr=bundle.atr,
            confirm_candles=tuple(confirm_candles),
            session_candles=tuple(session_candles),
            last_price=session_candles[-1].close,
            mark_price=mark_price or session_candles[-1].close,
            naked_pocs=tuple(self.naked_pocs(symbol, session_id,
                                             session_candles=session_candles)),
            weekly_levels=weekly_levels,
        )
        return ctx, None

    # ------------------------------------------------------------ naked POCs

    def naked_pocs(self, symbol, session_id, session_candles=None):
        """Prior session POCs that price has not traded through since.

        Unfinished business, and the strongest non-arbitrary target the profile offers:
        a POC that was never revisited is a price the auction agreed on and then left,
        which tends to attract price back.

        These are now TRUE POCs - see profile/composite.py. The previous implementation
        approximated each session's POC by its typical price, (H+L+C)/3, because the
        exact value needs that session's 1m candles. But it does not need a fetch: the
        POC is computed in frozen_bundle once per symbol per session and recorded there,
        so the registry is populated as a side effect of work already being done. A
        typical price is an average of three extremes and a POC is a volume mode; on a
        trend session - the session whose abandoned POC is most worth targeting - the
        two can sit most of the range apart.

        The current session's traded range is passed in so a level price has ALREADY
        passed through today is not reported as naked. Those extremes come from
        `session_candles`, which is as_of-bounded, so this uses no future information.
        """
        current_high = current_low = None
        if session_candles:
            current_high = max(candle.high for candle in session_candles)
            current_low = min(candle.low for candle in session_candles)

        symbol = symbol.upper()
        # Pull this symbol's POC history from the journal once. Live, the registry also
        # fills itself as frozen_bundle runs, but that only covers sessions seen since
        # start-up - after a restart the journal is what makes yesterday's POCs
        # available immediately instead of ten days later.
        if self._journal is not None and symbol not in self._poc_loaded:
            self._poc_loaded.add(symbol)
            self._naked_pocs.load_from_journal(self._journal, symbol)

        return self._naked_pocs.prices(
            symbol, session_id,
            daily_candles=self._cache.daily_candles(symbol),
            current_high=current_high, current_low=current_low)

    def naked_poc_coverage(self, symbol):
        """How much of the lookback window has a recorded POC, in [0, 1].

        An empty naked-POC list is ambiguous - it means either "price revisited every
        prior POC" or "this deployment has no history yet" - and those are opposite
        facts. Exposed so the journal can record which one it was.
        """
        return self._naked_pocs.coverage(symbol)

    # --------------------------------------------------------------- universe

    def trade_universe(self):
        """Symbols eligible for live trading: the liquid head of the ranking.

        Research runs wide and trading runs narrow - statistics need breadth, money
        needs depth. Symbols below the volume floor would fail PROFILE_THIN anyway, so
        excluding them here saves the fetch rather than changing the outcome.
        """
        try:
            tickers = self._rest.ticker_24hr()
        except Exception as exc:                  # noqa: BLE001
            log.error("ticker fetch failed: %s", exc)
            return []

        from exchange.symbols import rank_by_quote_volume
        ranked = rank_by_quote_volume(tickers, self._catalog,
                                     config.TRADE_UNIVERSE_SIZE)
        return [row for row in ranked
                if row["quote_volume"] >= config.TRADE_MIN_24H_QUOTE_VOLUME]

    def research_universe(self, size=None):
        """The wide ranking, with qv_rank recorded for the in/out-of-sample split.

        ONE fetch, ranks recorded, split by rank afterwards. Launching a second ranked
        fetch later re-ranks by CURRENT volume, so the two sets overlap and the
        held-out sample is silently contaminated.
        """
        try:
            tickers = self._rest.ticker_24hr()
        except Exception as exc:                  # noqa: BLE001
            log.error("ticker fetch failed: %s", exc)
            return []
        from exchange.symbols import rank_by_quote_volume
        return rank_by_quote_volume(tickers, self._catalog,
                                   size or config.UNIVERSE_SIZE)
