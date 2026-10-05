"""Kline cache. The difference between fitting inside the rate limit and not.

THE ARITHMETIC THAT FORCES THIS MODULE TO EXIST. A naive scanner refetches, per
symbol per cycle, two UTC days of 1m candles (2880 bars, so two requests at weight
10 each) plus daily candles. At 120 symbols that is ~360 requests and ~2400 weight
per cycle - the ENTIRE per-minute budget, for one cycle, leaving nothing for orders.

Two observations collapse it:

  A CLOSED SESSION NEVER CHANGES. Once 00:00 UTC passes, yesterday's 1m candles are
  immutable, so yesterday's profile is computed ONCE per symbol per session and then
  reused for the whole day. This is also why the frozen profile is a distinct type
  from the developing one.

  THE CURRENT SESSION ONLY GROWS AT THE END. Each cycle needs the handful of new 1m
  candles since the last one, not the whole day. Fetched with a small `limit`, which
  costs weight 1 instead of 10.

Together: ~1 request per symbol per cycle in the steady state, weight ~120/min for a
120-symbol universe. Comfortable.

DISK PERSISTENCE serves a different purpose: research. A replay over 600 symbols x
365 days must not refetch on every run, and the cached parquet files are the same
bytes the live path used, so a replay reads exactly what live saw.
"""
import logging
import os
import threading

import config
import sessions
from data import klines as klines_mod

log = logging.getLogger(__name__)


class KlineCache:
    """Incremental 1m cache plus a daily cache, both keyed by symbol."""

    def __init__(self, rest, parquet_dir=None):
        self._rest = rest
        self._lock = threading.RLock()
        self._minute = {}        # symbol -> {open_time: Candle}
        self._daily = {}         # symbol -> (fetched_session_id, [Candle])
        self._frozen = {}        # symbol -> (session_id, FrozenBundle)
        self._parquet_dir = parquet_dir or config.PARQUET_DIR

    # ---------------------------------------------------------------- 1m data

    def minute_candles(self, symbol, start_ms, end_ms, force=False):
        """Closed 1m candles in [start_ms, end_ms), fetching only what is missing.

        The cache is a dict keyed by open_time, so a re-fetch that overlaps existing
        data is idempotent and out-of-order arrivals cannot duplicate a bar. The
        return is always a fresh sorted list, so a caller mutating it cannot corrupt
        the cache.
        """
        symbol = symbol.upper()
        with self._lock:
            series = self._minute.setdefault(symbol, {})

        have_from = min(series) if series else None
        have_to = max(series) if series else None

        fetch_from = int(start_ms)
        if not force and have_to is not None and have_from is not None:
            if have_from <= start_ms:
                # Contiguous from the requested start: only the tail is missing.
                fetch_from = have_to + klines_mod.INTERVAL_MS["1m"]

        if fetch_from < end_ms:
            try:
                fetched = klines_mod.fetch(self._rest, symbol, "1m",
                                          fetch_from, end_ms)
            except Exception as exc:              # noqa: BLE001
                log.error("%s 1m fetch failed: %s", symbol, exc)
                fetched = []
            with self._lock:
                for candle in fetched:
                    series[candle.open_time] = candle

        with self._lock:
            return [series[key] for key in sorted(series)
                    if start_ms <= key < end_ms]

    def trim(self, symbol, keep_from_ms):
        """Drop cached minutes older than needed, to bound memory.

        Called after the frozen profile for a session has been computed: once that
        exists, the raw candles behind it are no longer required, and holding two days
        of 1m bars for 600 symbols is hundreds of megabytes.
        """
        symbol = symbol.upper()
        with self._lock:
            series = self._minute.get(symbol)
            if not series:
                return
            for key in [k for k in series if k < keep_from_ms]:
                del series[key]

    # ------------------------------------------------------------- daily data

    def daily_candles(self, symbol, days=None):
        """Daily candles for ATR and typical session volume, cached per session.

        Refetched once per UTC day rather than per cycle: a 1d candle only changes
        while it is forming, and the forming one is dropped as unclosed anyway.
        """
        symbol = symbol.upper()
        days = max(int(days or config.BIN_ATR_PERIOD_DAYS * 3), 30)
        today = sessions.session_id(sessions.now_ms())

        with self._lock:
            cached = self._daily.get(symbol)
            if cached and cached[0] == today:
                return list(cached[1])

        end = sessions.now_ms()
        start = sessions.session_start_ms(end) - days * sessions.MS_DAY
        try:
            candles = klines_mod.fetch(self._rest, symbol, "1d", start, end)
        except Exception as exc:                  # noqa: BLE001
            log.error("%s 1d fetch failed: %s", symbol, exc)
            return []

        with self._lock:
            self._daily[symbol] = (today, candles)
        return list(candles)

    # ------------------------------------------------------- frozen profiles

    def frozen(self, symbol, session_id):
        """A previously computed frozen bundle for this session, if any."""
        with self._lock:
            cached = self._frozen.get(symbol.upper())
        if cached and cached[0] == session_id:
            return cached[1]
        return None

    def set_frozen(self, symbol, session_id, bundle):
        """Cache the frozen bundle for the session, replacing any earlier one."""
        with self._lock:
            self._frozen[symbol.upper()] = (session_id, bundle)

    def clear_frozen(self):
        """Drop all frozen bundles - called at the session boundary."""
        with self._lock:
            self._frozen.clear()

    # ------------------------------------------------------ disk persistence

    def _path(self, symbol, interval, session_id):
        directory = os.path.join(self._parquet_dir, interval, symbol.upper())
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, f"{session_id}.csv")

    def persist_session(self, symbol, session_id, candles, interval="1m"):
        """Write one session's candles to disk for research reuse.

        CSV rather than parquet so the cache has no hard dependency on pyarrow - the
        research harness can load it with the standard library. Written once per
        symbol-session and never rewritten, since a closed session is immutable.
        """
        if not candles:
            return None
        path = self._path(symbol, interval, session_id)
        if os.path.exists(path):
            return path
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write("open_time,open,high,low,close,volume,close_time,"
                             "quote_volume,trades,taker_buy_base,taker_buy_quote\n")
                for candle in candles:
                    handle.write(
                        f"{candle.open_time},{candle.open},{candle.high},"
                        f"{candle.low},{candle.close},{candle.volume},"
                        f"{candle.close_time},{candle.quote_volume},"
                        f"{candle.trades},{candle.taker_buy_base},"
                        f"{candle.taker_buy_quote}\n")
        except OSError as exc:
            log.error("%s persist failed: %s", symbol, exc)
            return None
        return path

    def load_session(self, symbol, session_id, interval="1m"):
        """Read a persisted session back, or None when absent."""
        path = self._path(symbol, interval, session_id)
        if not os.path.exists(path):
            return None
        out = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                next(handle, None)
                for line in handle:
                    parts = line.strip().split(",")
                    if len(parts) < 11:
                        continue
                    out.append(klines_mod.Candle(
                        open_time=int(parts[0]), open=float(parts[1]),
                        high=float(parts[2]), low=float(parts[3]),
                        close=float(parts[4]), volume=float(parts[5]),
                        close_time=int(parts[6]), quote_volume=float(parts[7]),
                        trades=int(float(parts[8])),
                        taker_buy_base=float(parts[9]),
                        taker_buy_quote=float(parts[10]),
                    ))
        except (OSError, ValueError) as exc:
            log.error("%s load failed: %s", symbol, exc)
            return None
        return out

    def stats(self):
        with self._lock:
            return {
                "symbols_cached": len(self._minute),
                "minutes_cached": sum(len(series) for series in self._minute.values()),
                "daily_cached": len(self._daily),
                "frozen_cached": len(self._frozen),
            }
