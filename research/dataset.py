"""The one-time bulk fetch: build the local history every later phase reads.

Phases 0-2 all read the same corpus. Fetching it once and reading it from disk
afterwards is not only about politeness to the venue - it is what makes results
COMPARABLE. A harness that refetches per run measures a slightly different universe
each time (ranks move, new symbols list, old ones delist), so a calibration and the
validation that follows it would be run against different populations without anyone
choosing that.

THE UNIVERSE IS FETCHED ONCE AND ITS RANKS ARE FROZEN TO A FILE. A second ranked
fetch later re-ranks by *current* volume, so an in-sample/held-out split built from
two separate fetches silently overlaps - the contamination is invisible and fatal.
`universe.csv` is therefore written once and read thereafter; `--refresh-universe`
exists but says what it invalidates.

WHAT IS STORED, AND WHY EACH PIECE
    1m/<SYMBOL>/<session>.csv   one UTC day of 1m klines per file. Day-sized because
                                the session IS the profile period, so a replay reads
                                whole units and never a partial file.
    1d/<SYMBOL>/daily.csv       the daily series, for ATR and typical volume.
    specs.csv                   tick/step/notional filters AS AT FETCH TIME. Using
                                today's filters for a session six months old would
                                round replayed prices onto a lattice that did not
                                exist then.
    universe.csv                symbol, qv_rank, quote_volume, fetched_at.

RESUMABLE BY DESIGN. Every session file is written once and skipped if present, so
an interrupted run resumes where it stopped. This is a several-hour job at full size
and an unresumable version of it would be unusable.

COST. One 1m day is 1440 candles = one request at weight 10 (limit>1000). So
600 symbols x 365 days is ~219,000 requests and ~2.19M weight. The venue allows
2400 weight/minute, which puts the floor at about 15 hours. Start narrow: the
defaults here fetch 120 symbols x 120 days (~14,400 requests, ~40 minutes), which is
enough to calibrate every threshold in CALIBRATION.md, and widen only if a
distribution looks unstable.
"""
import argparse
import csv
import logging
import os
import time
from datetime import datetime

import config
import sessions
from data import klines as klines_mod
from exchange import symbols as symbols_mod
from exchange.rest import RestClient

log = logging.getLogger(__name__)

SPEC_FIELDS = [
    "symbol", "base_asset", "quote_asset", "status", "contract_type",
    "tick_size", "min_price", "max_price",
    "step_size", "min_qty", "max_qty",
    "market_step_size", "market_min_qty", "market_max_qty",
    "min_notional", "multiplier_up", "multiplier_down",
    "max_num_orders", "max_num_algo_orders",
    "price_precision", "quantity_precision",
]


# --------------------------------------------------------------------- paths

def _session_path(root, symbol, session_id, interval="1m"):
    directory = os.path.join(root, interval, symbol.upper())
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"{session_id}.csv")


def _write_candles(path, candles):
    """Write via a temp file then rename, so an interrupted run leaves no partial.

    A half-written CSV is worse than a missing one: the resume logic skips files that
    exist, so a truncated day would be silently treated as complete and the session
    would carry a profile built from half its volume.
    """
    temporary = path + ".part"
    with open(temporary, "w", encoding="utf-8", newline="") as handle:
        handle.write("open_time,open,high,low,close,volume,close_time,"
                     "quote_volume,trades,taker_buy_base,taker_buy_quote\n")
        for candle in candles:
            handle.write(
                f"{candle.open_time},{candle.open},{candle.high},{candle.low},"
                f"{candle.close},{candle.volume},{candle.close_time},"
                f"{candle.quote_volume},{candle.trades},{candle.taker_buy_base},"
                f"{candle.taker_buy_quote}\n")
    os.replace(temporary, path)


# ------------------------------------------------------------------ universe

def universe_path(root):
    return os.path.join(root, "universe.csv")


def load_universe(root):
    path = universe_path(root)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return [
            {"symbol": row["symbol"], "qv_rank": int(row["qv_rank"]),
             "quote_volume": float(row["quote_volume"])}
            for row in csv.DictReader(handle)
        ]


def save_universe(root, rows):
    os.makedirs(root, exist_ok=True)
    stamp = sessions.now_ms()
    with open(universe_path(root), "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "qv_rank", "quote_volume", "fetched_at"])
        for row in rows:
            writer.writerow([row["symbol"], row["qv_rank"],
                             row["quote_volume"], stamp])


def resolve_universe(rest, catalog, root, size, refresh=False):
    """The frozen ranked universe, fetched only if absent or explicitly refreshed."""
    existing = load_universe(root)
    if existing and not refresh:
        log.info("universe: reusing %d frozen ranks from %s",
                 len(existing), universe_path(root))
        return existing[:size] if size else existing

    catalog.refresh(force=True)
    tickers = rest.ticker_24hr()
    rows = symbols_mod.rank_by_quote_volume(tickers, catalog,
                                            size or config.UNIVERSE_SIZE)
    save_universe(root, rows)
    log.info("universe: froze %d ranked symbols", len(rows))
    return rows


# --------------------------------------------------------------------- specs

def specs_path(root):
    return os.path.join(root, "specs.csv")


def save_specs(root, specs):
    os.makedirs(root, exist_ok=True)
    with open(specs_path(root), "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SPEC_FIELDS)
        writer.writeheader()
        for spec in specs:
            writer.writerow({name: getattr(spec, name) for name in SPEC_FIELDS})


def load_specs(root):
    """Read persisted specs back into SymbolSpec objects."""
    path = specs_path(root)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            kwargs = dict(row)
            for name in ("max_num_orders", "max_num_algo_orders",
                         "price_precision", "quantity_precision"):
                kwargs[name] = int(float(kwargs.get(name) or 0))
            out.append(symbols_mod.SymbolSpec(**kwargs))
    return out


# ------------------------------------------------------------------- fetching

def fetch_symbol(rest, root, symbol, session_ids, throttle=0.0):
    """Fetch and persist every missing session for one symbol.

    Returns (written, skipped, empty). `empty` counts sessions the venue returned
    nothing for - normal before a symbol listed, and the reason a missing file is not
    treated as an error by the sweep.
    """
    written = skipped = empty = 0
    for session_id in session_ids:
        path = _session_path(root, symbol, session_id)
        if os.path.exists(path):
            skipped += 1
            continue

        start = sessions.session_start_ms_from_id(session_id)
        end = start + sessions.MS_DAY
        try:
            candles = klines_mod.fetch(rest, symbol, "1m", start, end)
        except Exception as exc:                       # noqa: BLE001
            log.error("%s %s fetch failed: %s", symbol, session_id, exc)
            continue

        if not candles:
            empty += 1
            continue

        _write_candles(path, candles)
        written += 1
        if throttle:
            time.sleep(throttle)
    return written, skipped, empty


def fetch_daily(rest, root, symbol, days, end_as_of=None):
    """Fetch and persist the daily series. Always rewritten - it has a live tail.

    Must take `end_as_of` and use it exactly like `session_id_range` does. Left
    at `now()` regardless of the caller's window, a second/third historical corpus
    (built via `--end-date`) gets 1m session candles anchored correctly in the past
    but daily candles anchored at the REAL present - so the scanner's `daily_candles`
    lookup finds nothing to check ATR/history-length against for any session old
    enough that "now" and the window's own end have drifted apart. This is exactly
    how `data_cache_window3` (2025-10-02..2026-01-29) produced zero trades: its
    daily.csv covered 2026-04-22..2026-09-28, entirely disjoint from its own session
    range. `data_cache_window2` had the same bug but partially masked - "now" at the
    time it was first fetched happened to fall near its own tail end, so only the
    early ~2/3 of its days silently lost their daily history, not all of them.
    """
    path = _session_path(root, symbol, "daily", interval="1d")
    end = end_as_of if end_as_of is not None else sessions.now_ms()
    start = sessions.session_start_ms(end) - int(days + 40) * sessions.MS_DAY
    try:
        candles = klines_mod.fetch(rest, symbol, "1d", start, end)
    except Exception as exc:                           # noqa: BLE001
        log.error("%s daily fetch failed: %s", symbol, exc)
        return 0
    if not candles:
        return 0
    _write_candles(path, candles)
    return len(candles)


def _write_funding(path, records):
    """Write funding-rate records via temp+rename, mirroring `_write_candles`."""
    temporary = path + ".part"
    with open(temporary, "w", encoding="utf-8", newline="") as handle:
        handle.write("funding_time,funding_rate,mark_price\n")
        for record in records:
            handle.write(f"{record['fundingTime']},{record['fundingRate']},"
                         f"{record.get('markPrice', '')}\n")
    os.replace(temporary, path)


def fetch_funding(rest, root, symbol, days, end_as_of=None):
    """Fetch and persist funding-rate history. Always rewritten - it has a live tail,
    same reasoning as `fetch_daily`, and takes `end_as_of` the identical way for the
    identical reason (CALIBRATION.md finding on `fetch_daily`'s `end_as_of` bug).

    UNLIKE open interest, funding rate carries no documented retention ceiling -
    verified empirically against the live endpoint back to a symbol's perpetual
    listing date - so a single request cannot be assumed to reach far enough back
    the way it can for `fetch_daily`'s ATR history. Paginated forward in
    `limit`-sized pages instead: the venue returns oldest-first when `startTime` is
    given, so each page's last `fundingTime` becomes the next page's `startTime`.
    """
    path = _session_path(root, symbol, "funding", interval="funding")
    end = end_as_of if end_as_of is not None else sessions.now_ms()
    start = sessions.session_start_ms(end) - int(days + 40) * sessions.MS_DAY

    records = []
    cursor = start
    while cursor < end:
        try:
            page = rest.funding_rate_history(symbol, start_ms=cursor, end_ms=end,
                                              limit=1000)
        except Exception as exc:                           # noqa: BLE001
            log.error("%s funding fetch failed: %s", symbol, exc)
            return 0
        if not page:
            break
        records.extend(page)
        last_time = page[-1]["fundingTime"]
        if len(page) < 1000 or last_time <= cursor:
            break
        cursor = last_time + 1
    if not records:
        return 0
    _write_funding(path, records)
    return len(records)


def session_id_range(days, end_as_of=None):
    """The `days` most recent COMPLETE UTC sessions, oldest first.

    The current session is excluded: it is still forming, so its file would be
    written partial and then skipped as complete for ever after.
    """
    end_as_of = sessions.now_ms() if end_as_of is None else end_as_of
    today_start = sessions.session_start_ms(end_as_of)
    return [sessions.session_id(today_start - (offset + 1) * sessions.MS_DAY)
            for offset in reversed(range(int(days)))]


def build(root=None, symbols_wanted=None, days=120, universe_size=120,
          refresh_universe=False, throttle=0.0, rest=None, end_as_of=None,
          with_funding=False):
    """Fetch the corpus. Idempotent: rerunning only fills what is missing.

    `end_as_of` (ms epoch) shifts the fetched window into the past - for building a
    second, independent historical corpus ending before an existing one, rather than
    the default "N sessions ending yesterday". Left None, behaviour is unchanged.
    Always pass a different `root` alongside it: this still resolves the universe by
    CURRENT (fetch-time) volume ranking (`--symbols` with the frozen list from the
    first corpus's own universe.csv is the honest way to keep the same population),
    and `specs.csv` still records filters AS AT FETCH TIME rather than as at
    `end_as_of` - both are the existing corpus's own limitations, not new ones.

    `with_funding` additionally fetches funding-rate history per symbol. Opt-in and
    defaulted False so every existing caller/test of `build()` is unaffected - this
    is a diagnostic-only addition (see CALIBRATION.md), not part of the routine corpus
    fetch yet.
    """
    root = root or config.PARQUET_DIR
    os.makedirs(root, exist_ok=True)

    rest = rest or RestClient()
    catalog = symbols_mod.SymbolCatalog(rest)
    catalog.refresh(force=True)

    if symbols_wanted:
        universe = [{"symbol": name.upper(), "qv_rank": index, "quote_volume": 0.0}
                    for index, name in enumerate(symbols_wanted, start=1)]
    else:
        universe = resolve_universe(rest, catalog, root, universe_size,
                                    refresh=refresh_universe)

    specs = [catalog.get(row["symbol"]) for row in universe]
    specs = [spec for spec in specs if spec is not None]
    save_specs(root, specs)
    log.info("specs: persisted %d", len(specs))

    session_ids = session_id_range(days, end_as_of=end_as_of)
    log.info("fetching %d symbols x %d sessions (%s .. %s)",
             len(universe), len(session_ids), session_ids[0], session_ids[-1])

    totals = {"written": 0, "skipped": 0, "empty": 0, "symbols": 0}
    started = time.time()

    for index, row in enumerate(universe, start=1):
        symbol = row["symbol"]
        fetch_daily(rest, root, symbol, days, end_as_of=end_as_of)
        if with_funding:
            fetch_funding(rest, root, symbol, days, end_as_of=end_as_of)
        written, skipped, empty = fetch_symbol(rest, root, symbol, session_ids,
                                               throttle=throttle)
        totals["written"] += written
        totals["skipped"] += skipped
        totals["empty"] += empty
        totals["symbols"] += 1

        elapsed = time.time() - started
        rate = index / elapsed if elapsed > 0 else 0.0
        remaining = (len(universe) - index) / rate if rate > 0 else 0.0
        log.info("[%d/%d] %-14s +%-4d ~%-4d o%-4d | %.0fm elapsed, ~%.0fm left",
                 index, len(universe), symbol, written, skipped, empty,
                 elapsed / 60.0, remaining / 60.0)

    log.info("done: %(symbols)d symbols, %(written)d written, %(skipped)d already "
             "present, %(empty)d empty", totals)
    return totals


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fetch the research corpus.")
    parser.add_argument("--root", default=None, help="cache root (default PARQUET_DIR)")
    parser.add_argument("--days", type=int, default=120,
                        help="complete UTC sessions to fetch (default 120)")
    parser.add_argument("--universe-size", type=int, default=120,
                        help="top-N by 24h quote volume (default 120)")
    parser.add_argument("--symbols", default=None,
                        help="comma-separated symbols instead of the ranked universe")
    parser.add_argument("--refresh-universe", action="store_true",
                        help="re-rank and overwrite universe.csv (INVALIDATES any "
                             "in-sample/held-out split already made from it)")
    parser.add_argument("--throttle", type=float, default=0.0,
                        help="extra seconds between requests")
    parser.add_argument("--end-date", default=None, metavar="YYYY-MM-DD",
                        help="fetch the `days` sessions ending BEFORE this UTC date, "
                             "instead of ending yesterday - for a second, independent "
                             "historical window. Always pair with a different --root.")
    parser.add_argument("--with-funding", action="store_true",
                        help="also fetch funding-rate history per symbol "
                             "(diagnostic - see CALIBRATION.md)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
    )
    logging.Formatter.converter = time.gmtime

    wanted = ([name.strip() for name in args.symbols.split(",") if name.strip()]
              if args.symbols else None)
    end_as_of = (sessions.to_ms(datetime.strptime(args.end_date, "%Y-%m-%d"))
                 if args.end_date else None)
    build(root=args.root, symbols_wanted=wanted, days=args.days,
          universe_size=args.universe_size,
          refresh_universe=args.refresh_universe, throttle=args.throttle,
          end_as_of=end_as_of, with_funding=args.with_funding)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
