"""Daily VP shape of every symbol-day, through the live code path - research only.

For each session D, scanner.frozen_bundle builds the PRIOR day's profile exactly as the
live bot does (bin size from daily ATR + tick size, 70% value area, intra-session POC
migration, bimodality) and profile/shape.classify labels it. One row per SHAPED day
(= D - 1). With --dump-symbols, the shaped day's histogram is also saved so the labels can
be checked by eye (research/shape_gallery in the scratchpad).

Run:
    PARQUET_DIR=data_cache_mainnet python -m research.daily_shapes \
        --out calibration/trend_shape/shapes_s0.csv --shard-index 0 --shard-count 6
"""
import argparse
import csv
import json
import logging
import os
import time

import config
import sessions
from research import confirm_study as cs
from research import dataset
from research.historical import HistoricalCache, StaticCatalog
from research.zone_watch_replay import _shard
from scanner import Scanner

log = logging.getLogger(__name__)

FIELDS = ["symbol", "day", "label", "poc_position", "va_range_ratio", "close_location",
          "va_close_location", "intra_poc_migration_atr", "bimodal", "high", "low", "close",
          "poc", "vah", "val", "atr", "open", "label_v2"]


def v2_label(prof, lv):
    """research/shape_v2 FINAL label of the same histogram (added 2026-10-14). One source with
    the workflow's veto input: wf_state.prior_shape_label."""
    from research import wf_state
    return wf_state.prior_shape_label(prof, lv)


def symbol_shapes(scanner, cache, symbol, dump=None):
    rows = []
    for sid in cache.available_sessions(symbol)[1:]:
        start = sessions.session_start_ms_from_id(sid)
        cache.set_as_of(start)
        cache.clear_frozen()
        bundle, _ = scanner.frozen_bundle(symbol, sid, start)
        if bundle is None or bundle.shape is None or bundle.levels is None:
            continue
        sh, prof, lv = bundle.shape, bundle.profile, bundle.levels
        day = sessions.session_id(start - sessions.MS_DAY)
        rows.append({"symbol": symbol, "day": day, "label": sh.label,
                     "poc_position": round(sh.poc_position, 4),
                     "va_range_ratio": round(sh.va_range_ratio, 4),
                     "close_location": sh.close_location,
                     "va_close_location": sh.va_close_location,
                     "intra_poc_migration_atr": sh.intra_poc_migration_atr,
                     "bimodal": int(sh.bimodal), "high": prof.high, "low": prof.low,
                     "close": prof.close, "poc": lv.poc_price, "vah": lv.vah, "val": lv.val,
                     "atr": bundle.atr, "open": prof.open,
                     "label_v2": v2_label(prof, lv) or "DEGENERATE"})
        if dump is not None:
            vols = {round(prof.bin_center(i), 10): v for i, v in prof.volume.items() if v > 0}
            dump.write(json.dumps({"symbol": symbol, "day": day, "label": sh.label,
                                   "poc": lv.poc_price, "vah": lv.vah, "val": lv.val,
                                   "close": prof.close, "high": prof.high, "low": prof.low,
                                   "bins": vols}) + "\n")
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--dump-symbols", default="", help="comma list: save histograms")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "mainnet" not in config.PARQUET_DIR:
        raise SystemExit("refusing to run: PARQUET_DIR must be a mainnet-verified cache")
    for name in cs.SHAPE_FLAGS:                       # labels only; the held flags stay off
        setattr(config, name, False)
    root = config.PARQUET_DIR
    catalog = StaticCatalog(dataset.load_specs(root))
    entries = [r for r in dataset.load_universe(root) if catalog.get(r["symbol"])]
    entries = _shard(entries, args.shard_index, args.shard_count)
    dump_syms = set(filter(None, args.dump_symbols.split(",")))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    dump = open(os.path.splitext(args.out)[0] + "_hist.jsonl", "w", encoding="utf-8") \
        if dump_syms else None
    started = time.time()
    with open(args.out, "w", encoding="utf-8", newline="") as h:
        w = csv.DictWriter(h, fieldnames=FIELDS)
        w.writeheader()
        for i, entry in enumerate(entries, 1):
            sym = entry["symbol"]
            cache = HistoricalCache(root=root, strict=False)
            scanner = Scanner(rest=None, catalog=catalog, cache=cache, journal=None)
            try:
                rows = symbol_shapes(scanner, cache, sym, dump if sym in dump_syms else None)
            except Exception as exc:                          # noqa: BLE001
                log.warning("%s failed: %s", sym, exc)
                rows = []
            w.writerows(rows)
            h.flush()
            cache.release()
            log.info("[%d/%d] %-14s %d days %.0fs", i, len(entries), sym, len(rows),
                     time.time() - started)
    if dump:
        dump.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
