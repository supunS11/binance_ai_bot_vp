"""Live footprints from the order-flow recorder's trade files (ops/feed_recorder.py).

The recorder writes, per symbol and UTC day, one gzip member per line:
    {"m": minute_ms, "lv": {price: [taker_buy_qty, taker_sell_qty]}}   one line per minute
    {"gap_from": ms, "gap_to": ms}                                      a socket outage
This module turns them into the per-day footprint arrays the research code reads
(research/footprint.py's stored format: minute 0..1439, row, buy, sell, row_size), on the same
row grid: row = floor(price / row_size + 1e-9), row_size = research.footprint.row_size (0.005
daily ATR, rounded down to the tick).

VALIDATION, the live counterpart of research/footprint.py's (which accepts a day only when every
minute matches the exchange's 1m kline). A recorder minute is VALID when its total traded
quantity matches the 1m candle's volume within VOLUME_TOL; a minute with no record is valid only
when the candle traded nothing. A gap marker, a recorder that was not running, or a dropped
message therefore shows up as an INVALID minute - never as a quiet market.

The files are read incrementally: each gzip member is complete on its own, so the reader keeps
the byte offset of the last complete member and decompresses only what was appended since. A
member still being written is left for the next call.
"""
import json
import logging
import math
import os
import threading
import zlib

import numpy as np

import sessions

log = logging.getLogger(__name__)

MINUTE_MS = 60_000
DAY_MIN = 1440
VOLUME_TOL = 1e-3            # relative; the recorder rounds to 8 decimals
SHIFT_TOL = 1e-6             # a boundary shift must offset exactly (research/footprint.py)
GZIP_MAGIC = b"\x1f\x8b\x08"


class _DayFile:
    def __init__(self):
        self.offset = 0
        self.minutes = {}        # minute index -> {price_str: [buy, sell]}
        self.gaps = []           # (from_ms, to_ms)


class RecorderTrades:
    """Incremental reader of <root>/trades/<SYMBOL>/<YYYY-MM-DD>.jsonl.gz."""

    def __init__(self, root):
        self.root = root
        self._files = {}
        self._lock = threading.Lock()

    def path(self, symbol, sid):
        return os.path.join(self.root, "trades", symbol.upper(), f"{sid}.jsonl.gz")

    def day(self, symbol, sid):
        """(minutes {index: {price: [buy, sell]}}, gaps) recorded so far for the day, or None
        when the recorder has no file for it."""
        path = self.path(symbol, sid)
        if not os.path.exists(path):
            return None
        key = (symbol.upper(), sid)
        with self._lock:
            state = self._files.setdefault(key, _DayFile())
            self._read_new(path, state, sessions.session_start_ms_from_id(sid))
            return dict(state.minutes), list(state.gaps)

    def drop_before(self, sid):
        """Forget days older than sid (memory bound)."""
        with self._lock:
            for key in [k for k in self._files if k[1] < sid]:
                del self._files[key]

    @staticmethod
    def _read_new(path, state, day_start):
        try:
            with open(path, "rb") as handle:
                handle.seek(state.offset)
                buf = handle.read()
        except OSError as exc:
            log.warning("recorder file %s unreadable: %s", path, exc)
            return
        pos = 0
        while pos < len(buf):
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                text = d.decompress(buf[pos:])
            except zlib.error:
                # A corrupt member (a crash mid-write): resume at the next member header.
                nxt = buf.find(GZIP_MAGIC, pos + 1)
                if nxt < 0:
                    break
                pos = nxt
                continue
            if not d.eof:
                break                        # the member is still being written
            pos = len(buf) - len(d.unused_data)
            for line in text.decode("utf-8", "replace").splitlines():
                _apply(line, state, day_start)
        state.offset += pos


def _apply(line, state, day_start):
    try:
        rec = json.loads(line)
    except ValueError:
        return
    if "gap_from" in rec:
        state.gaps.append((int(rec["gap_from"]), int(rec["gap_to"])))
        return
    if "m" not in rec:
        return
    m = (int(rec["m"]) - day_start) // MINUTE_MS
    if not 0 <= m < DAY_MIN:
        return
    cells = state.minutes.setdefault(m, {})
    for price, (buy, sell) in rec.get("lv", {}).items():
        cell = cells.setdefault(price, [0.0, 0.0])
        cell[0] += float(buy)
        cell[1] += float(sell)


def day_raw(minutes, row_size, upto=DAY_MIN - 1):
    """Footprint arrays (research/footprint.py format) from RecorderTrades.day minutes, for
    minutes 0..upto, on the row grid of row_size."""
    cells = {}
    for m, levels in minutes.items():
        if m > upto:
            continue
        for price, (buy, sell) in levels.items():
            r = int(math.floor(float(price) / row_size + 1e-9))
            c = cells.setdefault((m, r), [0.0, 0.0])
            c[0] += buy
            c[1] += sell
    keys = sorted(cells)
    return {"minute": np.array([k[0] for k in keys], dtype=np.int64),
            "row": np.array([k[1] for k in keys], dtype=np.int64),
            "buy": np.array([cells[k][0] for k in keys], dtype=float),
            "sell": np.array([cells[k][1] for k in keys], dtype=float),
            "row_size": np.array([row_size])}


def invalid_minutes(minutes, gaps, candles, day_start, upto):
    """Sorted minute indexes 0..upto of the day that cannot be trusted: no 1m candle, a
    recorder total that does not match the candle's volume, or inside a gap marker.

    BOUNDARY SHIFTS, as research/footprint.py's validate accepts them: Binance's kline
    sometimes counts a trade stamped in the last milliseconds of a minute in the next minute.
    Two adjacent mismatched minutes whose differences offset exactly (the same quantity too
    much in one, too little in the other) are a shift, not missing data, and stay valid.
    Measured on the VPS 2026-10-18: nearly all live mismatches were such pairs."""
    vol = {}
    for c in candles:
        m = (c.open_time - day_start) // MINUTE_MS
        if 0 <= m <= upto:
            vol[m] = float(c.volume)
    bad = set()
    diff = {}
    for m in range(upto + 1):
        if m not in vol:
            bad.add(m)
            continue
        got = sum(b + s for b, s in minutes.get(m, {}).values())
        want = vol[m]
        if abs(got - want) > VOLUME_TOL * max(want, 1e-12) and not (want == 0 and got == 0):
            bad.add(m)
            diff[m] = got - want
    for m in sorted(diff):
        n = m + 1
        if m in bad and n in bad and n in diff \
                and abs(diff[m] + diff[n]) <= SHIFT_TOL * max(1.0, abs(diff[m])):
            bad -= {m, n}
    for a, z in gaps:
        lo = max(0, (a - day_start) // MINUTE_MS)
        hi = min(upto, (z - day_start) // MINUTE_MS)
        bad.update(range(int(lo), int(hi) + 1))
    return sorted(bad)


def ohlc_arrays(candles, day_start, upto=DAY_MIN - 1):
    """(open, high, low, close) arrays of length 1440, NaN where no candle (or after upto)."""
    o = np.full(DAY_MIN, np.nan)
    h, lo, c = o.copy(), o.copy(), o.copy()
    for k in candles:
        m = (k.open_time - day_start) // MINUTE_MS
        if 0 <= m <= upto:
            o[m], h[m], lo[m], c[m] = k.open, k.high, k.low, k.close
    return o, h, lo, c
