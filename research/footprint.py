"""Real footprint data from Binance's raw trades archive - research only.

Every trade (price, quantity, time, aggressor side) from data.binance.vision's RAW
trades archive is put
into a 1-minute footprint: for each minute and each price row, the volume bought by
aggressive buyers (lifting the ask) and sold by aggressive sellers (hitting the bid).
Coarser bars (5m, 30m) and coarser rows are exact sums of these.

ROWS. Row size per symbol-day = max(tick, tick-rounded 0.005 x daily ATR), with ATR from
daily candles closed BEFORE that day (no look-ahead); row index = floor(price / row).

VALIDATION (every minute of every day, against the mainnet 1m klines of the research
cache, which were themselves verified against fapi.binance.com):
  volume       sum of quantities            == kline volume
  buy volume   sum where buyer is aggressor == kline taker_buy_base
  high / low   max / min trade price        == kline high / low
  trades       sum(last_id - first_id + 1)  == kline trade count
A day is VALID only if every minute matches (relative tolerance 1e-6 on volumes).

SOURCE CHANGED 2026-10-13 from aggTrades to raw trades: the aggTrades archive omits a few
trades on most days from ~April 2026 (0.006-0.13% of volume; e.g. ONEUSDT 2026-04-07
trade 322037544), so those days could never match the klines exactly. The raw trades
archive matched exactly on every day tried.

Storage: <out>/<SYMBOL>/<YYYY-MM-DD>.npz with minute (0..1439), row, buy, sell (base
units), row_size; validation summary in <out>/validation.csv.

Run:
    python -m research.footprint --symbols XAUUSDT --cache data_cache_xau_mainnet \
        --first 2025-12-12 --last 2026-10-11 --out data_footprint
"""
import argparse
import bisect
import csv
import io
import logging
import math
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ProcessPoolExecutor

import numpy as np

import sessions
from data import klines as klines_mod
from research.historical import HistoricalCache

log = logging.getLogger(__name__)

MONTHLY = "https://data.binance.vision/data/futures/um/monthly/aggTrades/{s}/{s}-aggTrades-{m}.zip"
DAILY = "https://data.binance.vision/data/futures/um/daily/aggTrades/{s}/{s}-aggTrades-{d}.zip"
RAW_MONTHLY = "https://data.binance.vision/data/futures/um/monthly/trades/{s}/{s}-trades-{m}.zip"
ROW_ATR = 0.005
DOWNLOAD_TIMEOUT_S = 120
TMP = os.path.join("calibration", "footprint_tmp")


def _download(url, path):
    # A complete archive left by an interrupted run is reused: downloads land in ".part" and
    # are renamed only once finished, so a file at `path` that opens as a zip is whole.
    if os.path.exists(path) and zipfile.is_zipfile(path):
        return path
    url = urllib.parse.quote(url, safe=":/")
    for attempt in range(4):
        try:
            urllib.request.urlretrieve(url, path + ".part")
            os.replace(path + ".part", path)
            return path
        except urllib.error.HTTPError:
            raise
        except Exception:                                 # noqa: BLE001 - network hiccup
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"download failed: {url}")


def _trades(path):
    """(time_ms, price, qty, buyer_is_aggressor, n_trades) per aggTrade, in file order."""
    with zipfile.ZipFile(path) as archive:
        with archive.open(archive.namelist()[0]) as raw:
            for line in io.TextIOWrapper(raw, encoding="utf-8"):
                p = line.split(",")
                if not p[0][:1].isdigit():
                    continue
                yield (int(p[5]), float(p[1]), float(p[2]),
                       p[6].strip().lower() == "false", int(p[4]) - int(p[3]) + 1)


RAW_DAILY = "https://data.binance.vision/data/futures/um/daily/trades/{s}/{s}-trades-{d}.zip"


def _raw_trades(path):
    """Same tuples as _trades, from the RAW trades archive (one row per trade). Found
    2026-10-13: the aggTrades archive omits a few trades on some days (ONEUSDT 2026-04-07
    trade 322037544, 1,427,917 ONE, is in the trades archive and in the kline but not in
    aggTrades), so days that fail validation are rebuilt from this source."""
    with zipfile.ZipFile(path) as archive:
        with archive.open(archive.namelist()[0]) as raw:
            for line in io.TextIOWrapper(raw, encoding="utf-8"):
                p = line.split(",")
                if not p[0][:1].isdigit():
                    continue
                yield int(p[4]), float(p[1]), float(p[2]), p[5].strip().lower() == "false", 1


CHUNK_ROWS = 2_000_000
RAW_COLS = ["id", "price", "qty", "quote_qty", "time", "is_buyer_maker"]


def _raw_trade_chunks(path):
    """The raw trades archive as (time_ms, price, qty, buyer_is_aggressor) numpy arrays, in
    chunks of CHUNK_ROWS, in file order (added 2026-10-15 for speed; the per-trade Python
    loop of _raw_trades was CPU-bound). Prices and quantities use round-trip float parsing,
    so every value equals Python's float() of the same text, as in _raw_trades."""
    import pandas as pd
    with zipfile.ZipFile(path) as archive:
        name = archive.namelist()[0]
        with archive.open(name) as raw:
            has_header = not raw.readline()[:1].isdigit()
        with archive.open(name) as raw:
            reader = pd.read_csv(raw, header=0 if has_header else None, names=RAW_COLS,
                                 usecols=["price", "qty", "time", "is_buyer_maker"],
                                 dtype={"price": np.float64, "qty": np.float64, "time": np.int64,
                                        "is_buyer_maker": str},
                                 float_precision="round_trip", chunksize=CHUNK_ROWS)
            for chunk in reader:
                yield (chunk["time"].to_numpy(), chunk["price"].to_numpy(), chunk["qty"].to_numpy(),
                       chunk["is_buyer_maker"].str.strip().str.lower().to_numpy() == "false")


def _add_arrays(b, ts, price, qty, buyer):
    """Vectorised DayBuilder.add over one chunk of one day's trades: the same minute / row
    arithmetic and the same per-cell and per-minute aggregates (sums can differ from the
    sequential loop only in the last floating-point bits)."""
    m = (ts - b.start) // 60_000
    r = np.floor(price / b.rsize + 1e-9).astype(np.int64)
    rmin = int(r.min())
    key = (r - rmin) * 2048 + m
    uniq, inv = np.unique(key, return_inverse=True)
    bq = np.bincount(inv, weights=np.where(buyer, qty, 0.0))
    sq = np.bincount(inv, weights=np.where(buyer, 0.0, qty))
    for k, bv, sv in zip(uniq.tolist(), bq.tolist(), sq.tolist()):
        cell = b.cells.setdefault((k % 2048, k // 2048 + rmin), [0.0, 0.0])
        cell[0] += bv
        cell[1] += sv
    um, mi = np.unique(m, return_inverse=True)
    vol = np.bincount(mi, weights=qty)
    buy = np.bincount(mi, weights=np.where(buyer, qty, 0.0))
    cnt = np.bincount(mi)
    hi = np.full(um.size, -np.inf); lo = np.full(um.size, np.inf)
    np.maximum.at(hi, mi, price)
    np.minimum.at(lo, mi, price)
    for j, mm in enumerate(um.tolist()):
        s = b.minute.get(mm)
        if s is None:
            b.minute[mm] = [vol[j], buy[j], hi[j], lo[j], int(cnt[j])]
        else:
            s[0] += vol[j]
            s[1] += buy[j]
            s[2] = max(s[2], hi[j])
            s[3] = min(s[3], lo[j])
            s[4] += int(cnt[j])


def repair(out, cache_root, symbols=None):
    """Rebuild every day that failed validation from the raw trades archive; a day is
    saved only if it then passes the same validation. Appends to repair.csv."""
    rows = list(csv.DictReader(open(os.path.join(out, "validation.csv"), encoding="utf-8")))
    failed = [(r["symbol"], r["day"]) for r in rows if int(r["mismatches"]) > 0
              and (symbols is None or r["symbol"] in symbols)]
    os.makedirs(TMP, exist_ok=True)
    results = []
    caches = {}
    for symbol, sid in failed:
        cache = caches.setdefault(symbol, HistoricalCache(root=cache_root, strict=False))
        start = sessions.session_start_ms_from_id(sid)
        rs = row_size(cache.load_daily(symbol), start, _tick(cache_root, symbol))
        path = os.path.join(TMP, f"{symbol}-trades-{sid}.zip")
        try:
            _download(RAW_DAILY.format(s=symbol, d=sid), path)
        except urllib.error.HTTPError:
            results.append((symbol, sid, 0, -1, "no raw trades archive", rs, 0))
            continue
        b = DayBuilder(start, rs)
        for t in _raw_trades(path):
            if start <= t[0] < start + sessions.MS_DAY:
                b.add(*t)
        os.remove(path)
        checked, bad, first = b.validate(cache.minute_candles(symbol, start, start + sessions.MS_DAY))
        if bad == 0:
            b.save(os.path.join(out, symbol, f"{sid}.npz"))
        results.append((symbol, sid, checked, bad, first[:200], rs, b.shifts))
    with open(os.path.join(out, "repair.csv"), "a", encoding="utf-8", newline="") as h:
        csv.writer(h).writerows(results)
    return results


def row_size(daily, day_start, tick):
    k = bisect.bisect_left([d.close_time for d in daily], day_start)
    atr = klines_mod.daily_atr(daily[:k]) if k >= 15 else 0.0
    if atr <= 0:
        return None
    raw = atr * ROW_ATR
    return max(tick, math.floor(raw / tick) * tick) if tick > 0 else raw


class DayBuilder:
    def __init__(self, day_start, rsize):
        self.start, self.rsize = day_start, rsize
        self.cells = {}                       # (minute, row) -> [buy, sell]
        self.minute = {}                      # minute -> [vol, buy, high, low, trades]

    def add(self, ts, price, qty, buyer_aggr, n):
        m = (ts - self.start) // 60_000
        r = int(math.floor(price / self.rsize + 1e-9))
        cell = self.cells.setdefault((m, r), [0.0, 0.0])
        cell[0 if buyer_aggr else 1] += qty
        s = self.minute.get(m)
        if s is None:
            self.minute[m] = [qty, qty if buyer_aggr else 0.0, price, price, n]
        else:
            s[0] += qty
            s[1] += qty if buyer_aggr else 0.0
            s[2] = max(s[2], price)
            s[3] = min(s[3], price)
            s[4] += n

    def save(self, path):
        keys = sorted(self.cells)
        np.savez_compressed(
            path, minute=np.array([k[0] for k in keys], dtype=np.int16),
            row=np.array([k[1] for k in keys], dtype=np.int64),
            buy=np.array([self.cells[k][0] for k in keys], dtype=np.float64),
            sell=np.array([self.cells[k][1] for k in keys], dtype=np.float64),
            row_size=np.array([self.rsize]))

    def validate(self, candles):
        """(minutes checked, unexplained mismatches, description, boundary shifts).

        Binance's kline occasionally puts a trade stamped in the last milliseconds of a
        minute into the next minute (found 2026-10-13: LSKUSDT 2026-03-01 m122/m123, one
        trade). Such a shift is accepted only when the day totals match exactly and the
        two minutes are adjacent with exactly offsetting volume, buy volume and count."""
        bad, first = 0, ""
        seen = set()
        deltas = {}
        for c in candles:
            m = (c.open_time - self.start) // 60_000
            s = self.minute.get(m, [0.0, 0.0, None, None, 0])
            deltas[m] = (s[0] - c.volume, s[1] - c.taker_buy_base, s[4] - c.trades)
        day_ok = (abs(sum(d[0] for d in deltas.values())) <= 1e-6 * max(1.0, sum(c.volume for c in candles))
                  and abs(sum(d[1] for d in deltas.values())) <= 1e-6 * max(1.0, sum(c.volume for c in candles))
                  and sum(d[2] for d in deltas.values()) == 0)
        shifted = set()
        for m, d in deltas.items():
            if m in shifted or d[2] == 0 and abs(d[0]) < 1e-9:
                continue
            nxt = deltas.get(m + 1)
            if day_ok and nxt and d[2] == -nxt[2] and abs(d[0] + nxt[0]) <= 1e-6 * max(1.0, abs(d[0])) \
                    and abs(d[1] + nxt[1]) <= 1e-6 * max(1.0, abs(d[0])):
                shifted |= {m, m + 1}
        self.shifts = len(shifted) // 2
        for c in candles:
            m = (c.open_time - self.start) // 60_000
            seen.add(m)
            s = self.minute.get(m)
            if s is None:
                ok = c.volume == 0 and c.trades == 0
                desc = "no trades but kline volume"
            else:
                tol = 1e-6 * max(1.0, c.volume)
                ok = (abs(s[0] - c.volume) <= tol and abs(s[1] - c.taker_buy_base) <= tol
                      and s[2] == c.high and s[3] == c.low and s[4] == c.trades)
                desc = (f"m{m} vol {s[0]} vs {c.volume}, buy {s[1]} vs {c.taker_buy_base}, "
                        f"hl {s[2]}/{s[3]} vs {c.high}/{c.low}, n {s[4]} vs {c.trades}")
            if not ok and m in shifted and s is not None and s[2] <= c.high + 1e-12 \
                    and s[3] >= c.low - 1e-12:
                continue                      # explained boundary shift (docstring)
            if not ok:
                bad += 1
                first = first or desc
        extra = [m for m in self.minute if m not in seen]
        if extra:
            bad += len(extra)
            first = first or f"trades in minutes with no kline: {extra[:3]}"
        return len(candles), bad, first


def process_symbol_safe(args):
    """process_symbol, but one symbol's unexpected failure is logged and skipped instead of
    aborting every other symbol in the pool (added 2026-10-14 for the 74-symbol fresh set)."""
    try:
        return process_symbol(args)
    except Exception as exc:                              # noqa: BLE001
        logging.getLogger(__name__).error("%s failed: %r", args[0], exc)
        return []


def process_symbol(args):
    symbol, cache_root, first, last, out = args
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.makedirs(TMP, exist_ok=True)
    os.makedirs(os.path.join(out, symbol), exist_ok=True)
    cache = HistoricalCache(root=cache_root, strict=False)
    daily = cache.load_daily(symbol)
    spec_tick = _tick(cache_root, symbol)
    d0 = sessions.session_start_ms_from_id(first)
    d1 = sessions.session_start_ms_from_id(last)
    days = []
    t = d0
    while t <= d1:
        days.append(sessions.session_id(t))
        t += sessions.MS_DAY
    wanted = set(days)
    months = sorted({d[:7] for d in days})
    current = sessions.session_id(int(time.time() * 1000))[:7]
    # 2026-10-15: urlretrieve has no timeout, and a stalled connection hung three workers
    # for hours. A stall now raises, and _download's retry loop reconnects.
    socket.setdefaulttimeout(DOWNLOAD_TIMEOUT_S)
    results = []
    for month in months:
        month_days = [d for d in days if d[:7] == month]
        if all(os.path.exists(os.path.join(out, symbol, f"{d}.npz")) for d in month_days):
            # Every day of this month was built AND passed validation in an interrupted run
            # (a day file is written only when validation finds 0 mismatches), so the month
            # is not downloaded again. Its validation rows are restated, marked as reused.
            for d in month_days:
                results.append((symbol, d, 1440, 0, "reused: validated in an interrupted run", "", 0))
            log.info("%s %s: %d days reused", symbol, month, len(month_days))
            continue
        sources = []
        if month < current:
            try:
                sources = [_download(RAW_MONTHLY.format(s=symbol, m=month),
                                     os.path.join(TMP, f"{symbol}-trades-{month}.zip"))]
            except urllib.error.HTTPError:
                sources = []
        if not sources:
            for d in (x for x in days if x[:7] == month):
                try:
                    sources.append(_download(RAW_DAILY.format(s=symbol, d=d),
                                             os.path.join(TMP, f"{symbol}-trades-{d}.zip")))
                except urllib.error.HTTPError:
                    pass
        builders = {}
        for path in sources:
            for ts, price, qty, buyer in _raw_trade_chunks(path):
                day = (ts // sessions.MS_DAY) * sessions.MS_DAY
                for start in np.unique(day).tolist():
                    sid = sessions.session_id(start)
                    if sid not in wanted:
                        continue
                    b = builders.get(sid)
                    if b is None:
                        rs = row_size(daily, start, spec_tick)
                        if rs is None:
                            builders[sid] = False
                            continue
                        b = builders[sid] = DayBuilder(start, rs)
                    if b:
                        sel = day == start
                        _add_arrays(b, ts[sel], price[sel], qty[sel], buyer[sel])
            os.remove(path)
        for sid, b in sorted(builders.items()):
            if not b:
                continue
            start = sessions.session_start_ms_from_id(sid)
            candles = cache.minute_candles(symbol, start, start + sessions.MS_DAY)
            checked, bad, first_bad = b.validate(candles)
            if bad == 0:
                b.save(os.path.join(out, symbol, f"{sid}.npz"))
            results.append((symbol, sid, checked, bad, first_bad[:200], b.rsize, b.shifts))
        log.info("%s %s: %d days built, %d with mismatches", symbol, month,
                 sum(1 for r in results if r[1][:7] == month),
                 sum(1 for r in results if r[1][:7] == month and r[3]))
        cache.release()
    return results


def _tick(cache_root, symbol):
    with open(os.path.join(cache_root, "specs.csv"), encoding="utf-8") as h:
        for row in csv.DictReader(h):
            if row["symbol"] == symbol:
                return float(row["tick_size"])
    return 0.0


def load_day(out, symbol, sid):
    path = os.path.join(out, symbol, f"{sid}.npz")
    if not os.path.exists(path):
        return None
    z = np.load(path)
    return {k: z[k] for k in z.files}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", required=True, help="comma list")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--first", required=True)
    parser.add_argument("--last", required=True)
    parser.add_argument("--out", default="data_footprint")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    symbols = [s for s in args.symbols.split(",") if s]
    jobs = [(s, args.cache, args.first, args.last, args.out) for s in symbols]
    path = os.path.join(args.out, "validation.csv")
    new = not os.path.exists(path)
    os.makedirs(args.out, exist_ok=True)
    with ProcessPoolExecutor(args.workers) as ex, open(path, "a", encoding="utf-8",
                                                       newline="") as h:
        w = csv.writer(h)
        if new:
            w.writerow(["symbol", "day", "minutes", "mismatches", "first_mismatch", "row_size",
                        "boundary_shifts"])
        for res in ex.map(process_symbol_safe, jobs):
            w.writerows(res)
            h.flush()
            log.info("%s done: %d days, %d invalid", res[0][0] if res else "?", len(res),
                     sum(1 for r in res if r[3]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
