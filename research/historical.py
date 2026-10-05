"""A disk-backed cache that makes the live Scanner replayable, unchanged.

THE WHOLE POINT OF THIS MODULE is that research must call the SAME
`Scanner.build_context` that live calls. A second, research-only implementation of
the scan would be the easiest possible way to produce results that do not transfer:
the two copies drift, and the backtest measures a system that was never deployed.
So the seam is placed at the DATA layer instead. `Scanner` talks to its cache
through six methods; this class implements those six from local CSV files and a
simulated clock, and the scanner cannot tell the difference.

THE ONE THING THAT MUST NOT BE GOT WRONG: TRUNCATION.

In live, future candles do not exist - the venue simply has not produced them yet,
so `minute_candles` physically cannot return them. Reading the same call from a
completed session on disk, it returns the WHOLE day, including candles after the
simulated clock. The profile builder would still filter them (`profile_as_of` is
honest), but three things in `build_context` read the candle list directly rather
than through the builder:

    last_price       = session_candles[-1].close      -> the day's FINAL close
    confirm_candles  = resample(session_candles, ...)  -> 15m bars from the future
    session_candles  = tuple(session_candles)          -> handed to every setup

Each of those is a lookahead leak, and none of them would raise an error or look
wrong in a result - they would just quietly produce an excellent backtest. So
truncation happens HERE, at the boundary where the data enters, reproducing the
physical fact that live enjoys for free. `set_as_of()` moves the clock and every
read after it is bounded by `close_time <= as_of`.

That is also why this class does not subclass KlineCache. Inheriting would mean a
method added to the live cache later arrives here untruncated and silently
un-audited. Six explicit methods fail loudly instead.

DAILY CANDLES ARE BOUNDED THE SAME WAY, and it matters more than it looks: daily
ATR sets the bin width, so leaking one future daily candle changes the bin lattice
and therefore every level in the replay.
"""
import logging
import os
import threading

import config
import sessions
from data import klines as klines_mod

log = logging.getLogger(__name__)


class MissingData(Exception):
    """Requested history is not on disk. Raised rather than returning empty.

    An empty return would be indistinguishable from "this symbol genuinely did not
    trade", which would silently drop sessions from a calibration sample and bias it
    toward whatever the fetcher happened to complete.
    """


class HistoricalCache:
    """Reads persisted sessions from disk, bounded by a simulated clock.

    Implements the cache interface `Scanner` depends on:
    `minute_candles`, `daily_candles`, `frozen`, `set_frozen`, `persist_session`,
    `trim`.
    """

    def __init__(self, root=None, as_of=None, strict=True):
        self._root = root or config.PARQUET_DIR
        self._as_of = as_of
        self._strict = strict
        self._lock = threading.RLock()
        self._sessions = {}     # (symbol, session_id) -> [Candle] (full, untruncated)
        self._daily = {}        # symbol -> [Candle] (full, untruncated)
        self._frozen = {}       # symbol -> (session_id, bundle)
        self.misses = []        # (symbol, session_id) pairs that were not on disk

    # ------------------------------------------------------------------ clock

    def set_as_of(self, as_of):
        """Move the simulated clock. Every subsequent read is bounded by it."""
        self._as_of = int(as_of)

    @property
    def as_of(self):
        return self._as_of

    def _bound(self, candles):
        """The truncation. See the module docstring - this is the load-bearing line."""
        if self._as_of is None:
            return list(candles)
        return [c for c in candles if c.close_time <= self._as_of]

    # ------------------------------------------------------------------ paths

    def _path(self, symbol, interval, session_id):
        return os.path.join(self._root, interval, symbol.upper(),
                            f"{session_id}.csv")

    def has_session(self, symbol, session_id, interval="1m"):
        return os.path.exists(self._path(symbol, interval, session_id))

    def available_sessions(self, symbol, interval="1m"):
        """Session ids on disk for this symbol, sorted ascending."""
        directory = os.path.join(self._root, interval, symbol.upper())
        if not os.path.isdir(directory):
            return []
        out = [name[:-4] for name in os.listdir(directory) if name.endswith(".csv")]
        out.sort()
        return out

    def available_symbols(self, interval="1m"):
        directory = os.path.join(self._root, interval)
        if not os.path.isdir(directory):
            return []
        return sorted(name for name in os.listdir(directory)
                      if os.path.isdir(os.path.join(directory, name)))

    # -------------------------------------------------------------- 1m access

    def _load(self, symbol, session_id, interval="1m"):
        key = (symbol.upper(), session_id, interval)
        with self._lock:
            if key in self._sessions:
                return self._sessions[key]

        path = self._path(symbol, interval, session_id)
        if not os.path.exists(path):
            with self._lock:
                self.misses.append((symbol.upper(), session_id))
            if self._strict:
                raise MissingData(f"{symbol} {session_id} {interval} not cached")
            return []

        out = []
        with open(path, "r", encoding="utf-8") as handle:
            next(handle, None)
            for line in handle:
                parts = line.rstrip("\n").split(",")
                if len(parts) < 11:
                    continue
                try:
                    out.append(klines_mod.Candle(
                        open_time=int(parts[0]), open=float(parts[1]),
                        high=float(parts[2]), low=float(parts[3]),
                        close=float(parts[4]), volume=float(parts[5]),
                        close_time=int(parts[6]), quote_volume=float(parts[7]),
                        trades=int(float(parts[8])),
                        taker_buy_base=float(parts[9]),
                        taker_buy_quote=float(parts[10]),
                    ))
                except ValueError:
                    continue
        out.sort(key=lambda c: c.open_time)
        with self._lock:
            self._sessions[key] = out
        return out

    def minute_candles(self, symbol, start_ms, end_ms, force=False):
        """Closed 1m candles in [start_ms, end_ms), bounded by the simulated clock.

        Spans the session files the window touches, so a caller asking for a window
        that straddles midnight gets a contiguous series rather than one day's worth.
        """
        del force                       # no fetching here; signature parity only
        start_ms, end_ms = int(start_ms), int(end_ms)
        out = []
        cursor = sessions.session_start_ms(start_ms)
        while cursor < end_ms:
            session_id = sessions.session_id(cursor)
            out.extend(self._load(symbol, session_id))
            cursor += sessions.MS_DAY
        window = [c for c in out if start_ms <= c.open_time < end_ms]
        return self._bound(window)

    def forward_candles(self, symbol, from_ms, to_ms):
        """Candles AFTER the decision point, for measuring what happened next.

        DELIBERATELY BYPASSES THE as_of BOUND, and is named so that cannot happen by
        accident. Measuring a trade's outcome legitimately requires data the decision
        did not have - that is what an outcome IS. The danger is only ever using it to
        DECIDE, so it is a separate method with an unmistakable name rather than a flag
        on minute_candles, and nothing in the live package can reach it.

        Everything the decision may see goes through minute_candles, which truncates.
        """
        from_ms, to_ms = int(from_ms), int(to_ms)
        out = []
        cursor = sessions.session_start_ms(from_ms)
        while cursor < to_ms:
            out.extend(self._load(symbol, sessions.session_id(cursor)))
            cursor += sessions.MS_DAY
        return [c for c in out if from_ms <= c.open_time < to_ms]

    # ----------------------------------------------------------- daily access

    def load_daily(self, symbol):
        """The full persisted daily series (untruncated). For the fetcher's use."""
        symbol = symbol.upper()
        with self._lock:
            if symbol in self._daily:
                return self._daily[symbol]
        candles = self._load(symbol, "daily", interval="1d")
        with self._lock:
            self._daily[symbol] = candles
        return candles

    def daily_candles(self, symbol, days=None):
        """Daily candles bounded by the simulated clock.

        The bound is not cosmetic: daily ATR sets bin width, so one leaked future
        daily candle shifts the bin lattice and with it every level in the replay.
        """
        candles = self._bound(self.load_daily(symbol))
        if days:
            wanted = max(int(days), 30)
            return candles[-wanted:]
        return candles

    # --------------------------------------------------------------- frozen

    def frozen(self, symbol, session_id):
        with self._lock:
            cached = self._frozen.get(symbol.upper())
        if cached and cached[0] == session_id:
            return cached[1]
        return None

    def set_frozen(self, symbol, session_id, bundle):
        with self._lock:
            self._frozen[symbol.upper()] = (session_id, bundle)

    def clear_frozen(self):
        with self._lock:
            self._frozen.clear()

    # ------------------------------------------------- no-ops for parity

    def persist_session(self, symbol, session_id, candles, interval="1m"):
        """No-op: the data came from disk, writing it back would be a round trip."""
        return None

    def trim(self, symbol, keep_from_ms):
        """No-op in replay.

        The live cache trims to bound memory; here the sessions dict is the working
        set and dropping it would force a re-read of the same file on the next bar.
        """
        return None

    def release(self, symbol=None):
        """Drop loaded candle series to bound memory across a long sweep."""
        with self._lock:
            if symbol is None:
                self._sessions.clear()
                self._daily.clear()
                return
            symbol = symbol.upper()
            for key in [k for k in self._sessions if k[0] == symbol]:
                del self._sessions[key]
            self._daily.pop(symbol, None)

    def stats(self):
        with self._lock:
            return {
                "sessions_loaded": len(self._sessions),
                "daily_loaded": len(self._daily),
                "misses": len(self.misses),
            }


class StaticCatalog:
    """A SymbolCatalog stand-in backed by persisted specs.

    Replay needs tick_size and step_size to round exactly as live would, but must
    not depend on a network call - and must not use TODAY's filters for a session
    six months ago if the venue has since changed them. Specs are persisted
    alongside the klines by the fetcher and read back here.
    """

    def __init__(self, specs):
        self._specs = {spec.symbol.upper(): spec for spec in specs}

    def get(self, symbol):
        return self._specs.get(symbol.upper())

    def require(self, symbol):
        spec = self.get(symbol)
        if spec is None:
            raise KeyError(f"no persisted spec for {symbol}")
        return spec

    def refresh(self, force=False):
        return None

    def __len__(self):
        return len(self._specs)

    @property
    def symbols(self):
        return sorted(self._specs)
