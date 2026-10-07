"""Replay of the in-zone watch (setups/zone_watch.py) over history.

Same data path as research/replay.py: the scanner builds each decision's context from the
recorded candles, and the watch sees the closed minutes the live loop would have seen.

A minute is handed to the watch when a zone is near (the S4 pre-check) or when a watch is
already running. Skipping the other minutes loses nothing, because observe() processes every
closed minute it has not yet seen.

A market entry fills at the next minute's open, the earliest a live market order can fill
after its decision. R is measured from that fill price, and there is no trade-through test.

Run:
    python -m research.zone_watch_replay --symbols 20 --sessions 60 --out calibration/zone_watch
"""
import argparse
import csv
import json
import logging
import os
import statistics
import time

import config
import risk
import sessions
from data import klines as klines_mod
from gates import profiles as gate_profiles
from research import replay
from research.historical import HistoricalCache, StaticCatalog
from research import dataset
from scanner import Scanner
from setups import orderflow_reversal as ofr
from setups.zone_watch import ZoneWatch
from state_machine import StateMachine

log = logging.getLogger(__name__)

MINUTE_MS = 60_000


def _watching(watch, symbol):
    state = watch._symbols.get(symbol)
    return state is not None and bool(state.active)


def _timing(candidate, ctx, fill, outcome):
    """Per-trade timing for the late-entry question, kept out of the shared trade schema."""
    atr15 = candidate.attributes["atr15"]
    tol = config.OFR_TOUCH_TOL_ATR * ctx.atr
    sign = 1 if candidate.direction == "BUY" else -1
    edge = candidate.level_price + sign * tol
    decision_distance = sign * (candidate.entry_price - edge) / atr15
    chase = sign * (fill - candidate.entry_price) / atr15
    return {
        "symbol": candidate.symbol,
        "session_id": candidate.session_id,
        "direction": candidate.direction,
        "tier": candidate.attributes["ofr_tier"],
        "visit": candidate.attributes["ofr_visit_number"],
        "lag_minutes": (candidate.as_of - candidate.attributes["watch_confirmed_ms"]) / MINUTE_MS,
        "decision_distance_atr15": round(decision_distance, 4),
        "chase_atr15": round(chase, 4),
        "outcome": outcome["outcome"],
        "net_r": outcome["net_r"],
    }


def replay_symbol(scanner, cache, catalog, symbol, qv_rank, session_ids, portfolio,
                  apply_gates=True):
    rows, rejects, timings = [], {}, []
    spec = catalog.get(symbol)
    if spec is None:
        return rows, rejects, timings

    confirm_ms = klines_mod.INTERVAL_MS.get(config.CONFIRM_INTERVAL, 900_000)

    for session_id in session_ids:
        session_start = sessions.session_start_ms_from_id(session_id)
        session_end = session_start + sessions.MS_DAY

        machine = StateMachine()
        watch = ZoneWatch()
        taken = False

        step = session_start + MINUTE_MS
        while step <= session_end:
            cache.set_as_of(step)
            if not _watching(watch, symbol) and not replay._s4_zone_near(
                    scanner, cache, symbol, session_id, step):
                rejects["NO_ORDER_FLOW_ZONE"] = rejects.get("NO_ORDER_FLOW_ZONE", 0) + 1
                step += MINUTE_MS
                continue
            try:
                ctx, reason = scanner.build_context(symbol, as_of=step)
            except Exception as exc:                    # noqa: BLE001
                log.debug("%s %s build_context: %s", symbol, session_id, exc)
                rejects["EXCEPTION"] = rejects.get("EXCEPTION", 0) + 1
                step += MINUTE_MS
                continue
            if ctx is None:
                rejects[reason] = rejects.get(reason, 0) + 1
                step += MINUTE_MS
                continue

            refusal = machine.admission(ctx, ofr.SETUP)
            if refusal is not None:
                key = refusal.reason.value
                rejects[key] = rejects.get(key, 0) + 1
                step += MINUTE_MS
                continue

            candidate, rejections = watch.observe(ctx)
            for rejection in rejections:
                key = rejection.reason.value
                rejects[key] = rejects.get(key, 0) + 1

            if candidate is not None and not taken:
                if apply_gates:
                    rejection = gate_profiles.evaluate(candidate, ctx)
                    if rejection is not None:
                        key = f"GATE:{rejection.reason.value}"
                        rejects[key] = rejects.get(key, 0) + 1
                        candidate = None
                if candidate is not None:
                    rejection = risk.size(candidate, portfolio, spec)
                    if rejection is not None:
                        key = f"SIZE:{rejection.reason.value}"
                        rejects[key] = rejects.get(key, 0) + 1
                        candidate = None
                if candidate is not None:
                    taken = True
                    machine.record_taken(symbol, session_id, ofr.SETUP, candidate.level_price)
                    forward = cache.forward_candles(
                        symbol, candidate.as_of + 1,
                        candidate.as_of + replay.MAX_HOLD_BARS * confirm_ms
                        + 2 * confirm_ms)
                    outcome = replay.simulate(candidate, forward, spec, market_fill=True)
                    fill = float(forward[0].open) if forward else candidate.entry_price
                    rows.append(replay._row(candidate, ctx, fill, outcome, qv_rank))
                    timings.append(_timing(candidate, ctx, fill, outcome))

            step += MINUTE_MS

        cache.clear_frozen()
    cache.release(symbol)
    return rows, rejects, timings


def _checkpoint_path(out_dir, symbol):
    return os.path.join(out_dir, "checkpoints", f"{symbol}.json")


def _load_checkpoint(out_dir, symbol):
    path = _checkpoint_path(out_dir, symbol)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data["rows"], data["rejects"], data["timings"]


def _save_checkpoint(out_dir, symbol, rows, rejects, timings):
    path = _checkpoint_path(out_dir, symbol)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"rows": rows, "rejects": rejects, "timings": timings}, handle)
    os.replace(tmp, path)        # atomic - a crash mid-write never leaves a half file


def run(root=None, limit_symbols=None, limit_sessions=None, apply_gates=True, equity=10_000.0,
       out_dir=None):
    """`out_dir` checkpoints each symbol's result to disk as it finishes - REQUIRED to
    survive the tool's own background time limits, which kill the process outright with
    nothing to catch it. A symbol whose checkpoint already exists is loaded from disk
    instead of replayed, so a killed run resumes from where it stopped rather than losing
    everything and starting over. Delete the `checkpoints` folder to force a clean rerun -
    there is no staleness check, because nothing else writes there.
    """
    root = root or config.PARQUET_DIR
    specs = dataset.load_specs(root)
    if not specs:
        raise SystemExit("no corpus - run research.dataset first")

    cache = HistoricalCache(root=root, strict=False)
    catalog = StaticCatalog(specs)
    universe = dataset.load_universe(root) or [
        {"symbol": s.symbol, "qv_rank": i, "quote_volume": 0.0}
        for i, s in enumerate(specs, start=1)]
    scanner = Scanner(rest=None, catalog=catalog, cache=cache, journal=None)
    portfolio = risk.PortfolioState(equity=equity, available_balance=equity)

    entries = [row for row in universe if catalog.get(row["symbol"])]
    if limit_symbols:
        entries = entries[:limit_symbols]

    all_rows, all_rejects, all_timings = [], {}, []
    started = time.time()
    for index, entry in enumerate(entries, start=1):
        symbol = entry["symbol"]
        cached = _load_checkpoint(out_dir, symbol) if out_dir else None
        if cached is not None:
            rows, rejects, timings = cached
            log.info("[%d/%d] %-14s %d trades (checkpointed, skipped)", index,
                     len(entries), symbol, len(rows))
        else:
            session_ids = cache.available_sessions(symbol)[1:]
            if limit_sessions:
                session_ids = session_ids[-limit_sessions:]
            if not session_ids:
                continue
            rows, rejects, timings = replay_symbol(scanner, cache, catalog, symbol,
                                                   entry["qv_rank"], session_ids, portfolio,
                                                   apply_gates=apply_gates)
            if out_dir:
                _save_checkpoint(out_dir, symbol, rows, rejects, timings)
            log.info("[%d/%d] %-14s %d trades (%d total) %.0fs", index, len(entries), symbol,
                     len(rows), len(all_rows) + len(rows), time.time() - started)
        all_rows.extend(rows)
        all_timings.extend(timings)
        for key, count in rejects.items():
            all_rejects[key] = all_rejects.get(key, 0) + count
    return all_rows, all_rejects, all_timings


TIMING_FIELDS = ["symbol", "session_id", "direction", "tier", "visit", "lag_minutes",
                 "decision_distance_atr15", "chase_atr15", "outcome", "net_r"]


def _write_timings(path, timings):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TIMING_FIELDS)
        writer.writeheader()
        writer.writerows(timings)


def _bucket_table(title, timings, key, edges):
    print(f"\n{title}")
    for lo, hi in zip(edges, edges[1:]):
        group = [t for t in timings
                 if t["outcome"] in ("TARGET", "STOP") and lo <= t[key] < hi]
        if not group:
            print(f"  [{lo}, {hi})  n=0")
            continue
        values = [float(t["net_r"]) for t in group]
        wins = sum(1 for v in values if v > 0) / len(values)
        print(f"  [{lo:>6}, {hi:>6})  n={len(values):4d}  meanR={statistics.fmean(values):+.3f}"
              f"  win={wins:.1%}")


def _print_timing_buckets(timings):
    _bucket_table("NET R BY CONFIRMATION-TO-ENTRY LAG (minutes)", timings, "lag_minutes",
                  [0, 2, 5, 15, 30, 60, 1e9])
    _bucket_table("NET R BY DISTANCE FROM ZONE EDGE AT DECISION (ATR15)", timings,
                  "decision_distance_atr15", [-1e9, 0, 0.25, 0.5, 0.75, 1.0, 1e9])
    _bucket_table("NET R BY CHASE: FILL vs DECISION PRICE, IN THE DIRECTION OF THE TRADE (ATR15)",
                  timings, "chase_atr15", [-1e9, -0.1, 0.0, 0.1, 0.25, 1e9])


def main(argv=None):
    parser = argparse.ArgumentParser(description="Replay the in-zone watch over history.")
    parser.add_argument("--root", default=None)
    parser.add_argument("--out", default="calibration/zone_watch")
    parser.add_argument("--symbols", type=int, default=None)
    parser.add_argument("--sessions", type=int, default=None)
    parser.add_argument("--max-distance", type=float, default=None,
                        help="override OFR_ENTRY_MAX_DISTANCE_ATR for this run")
    parser.add_argument("--no-gates", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)sZ %(levelname)s %(message)s")
    logging.Formatter.converter = time.gmtime

    config.ZONE_WATCH_ENABLED = True
    if args.max_distance is not None:
        config.OFR_ENTRY_MAX_DISTANCE_ATR = args.max_distance
        log.info("OFR_ENTRY_MAX_DISTANCE_ATR overridden to %s", args.max_distance)

    rows, rejects, timings = run(root=args.root, limit_symbols=args.symbols,
                                 limit_sessions=args.sessions, apply_gates=not args.no_gates,
                                 out_dir=args.out)

    os.makedirs(args.out, exist_ok=True)
    path = replay.write_csv(os.path.join(args.out, "trades.csv"), rows)
    replay.report(rows, rejects, label="ZONE WATCH")
    _write_timings(os.path.join(args.out, "timings.csv"), timings)
    _print_timing_buckets(timings)
    print(f"\nENTRY_TOO_FAR refusals: {rejects.get('ENTRY_TOO_FAR', 0)}")
    if path:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
