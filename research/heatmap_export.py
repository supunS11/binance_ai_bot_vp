"""Exports recorded depth + trade data to the compact JSON the liquidity-heatmap viewer reads.

This is a diagnostic tool, not a trading signal: it lets a human look at the same resting-order
and taker-flow data that resting_large_orders()/stacked_imbalance() (data/feed_reader.py) already
read numerically, so a RESTING or STACKED detection on a specific zone touch can be sanity-checked
by eye instead of trusted blind.

Point it at one episode (a zone touch plus the bars around it), not a whole day - a wide window
makes a large, slow-loading JSON file for no diagnostic benefit.

Usage:
    python -m research.heatmap_export --root feed_data --symbol BTCUSDT \\
        --start 2026-10-06T08:00:00 --end 2026-10-06T11:00:00 --out heatmap.json \\
        --zone-low 60250 --zone-high 60310 --poc 60280

Exits with an error if the window is not covered by the recorder (before it started, or across
a gap) - a heatmap built on partial data would look like a real market, which is worse than
refusing outright.
"""
import argparse
import datetime as dt
import json

from data import feed_reader as fr


def _parse_ms(value):
    if value.isdigit():
        return int(value)
    return int(dt.datetime.fromisoformat(value).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def build(root, symbol, start_ms, end_ms, zone=None):
    """Compact JSON payload for the viewer, or raises SystemExit if the window isn't covered."""
    depth = fr.depth_samples(root, symbol, start_ms, end_ms)
    if depth is None:
        raise SystemExit(f"depth not covered for {symbol} in this window - recorder was not "
                         f"running, or there's a gap; check feed_data/depth/{symbol}/")
    trades = fr.trade_minutes(root, symbol, start_ms, end_ms)
    if trades is None:
        raise SystemExit(f"trades not covered for {symbol} in this window - recorder was not "
                         f"running, or there's a gap; check feed_data/trades/{symbol}/")

    return {
        "sample": False,
        "symbol": symbol,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "zone": zone or {},
        "depth": [
            {"t": s["t"],
             "b": [[round(float(p), 8), round(float(q), 8)] for p, q in s["b"]],
             "a": [[round(float(p), 8), round(float(q), 8)] for p, q in s["a"]]}
            for s in depth
        ],
        "trades": [
            {"m": rec["m"],
             "lv": {p: [round(buy, 8), round(sell, 8)] for p, (buy, sell) in rec["lv"].items()}}
            for rec in trades
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default="feed_data")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True, help="ISO timestamp or epoch ms")
    parser.add_argument("--end", required=True, help="ISO timestamp or epoch ms")
    parser.add_argument("--out", required=True)
    parser.add_argument("--zone-low", type=float, default=None)
    parser.add_argument("--zone-high", type=float, default=None)
    parser.add_argument("--poc", type=float, default=None)
    parser.add_argument("--vah", type=float, default=None)
    parser.add_argument("--val", type=float, default=None)
    args = parser.parse_args(argv)

    zone = {k: v for k, v in {
        "low": args.zone_low, "high": args.zone_high,
        "poc": args.poc, "vah": args.vah, "val": args.val,
    }.items() if v is not None}

    payload = build(args.root, args.symbol.upper(), _parse_ms(args.start), _parse_ms(args.end),
                    zone=zone)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    print(f"wrote {args.out}: {len(payload['depth'])} depth samples, "
         f"{len(payload['trades'])} trade minutes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
