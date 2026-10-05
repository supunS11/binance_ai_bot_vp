"""PLAN section 5: how price reacts at the HVN zones the system identifies. Research only.

For each session in the window, the prior session's HVN zones are rebuilt through the live
scanner path. Each zone's first visit this session is then measured on 1-minute candles over the
following hour:

  REJECTED  price returns to the side it came from by MOVE_ATR x ATR past the zone edge
  BROKE     price leaves the zone through the far side by MOVE_ATR x ATR
  NEITHER   neither happens within the hour

Results are split by whether the zone is the POC and by whether it is the first visit or a
later one, so the plan's threshold questions can be answered from the same output.

Usage: python -m research.hvn_reactions --root data_cache --start 2025-10-03 --end 2025-10-10
"""
import argparse
import collections
import datetime as dt
import os
import sys

import config
import sessions
from research import dataset
from research.historical import HistoricalCache, StaticCatalog
from scanner import Scanner

MINUTES = 60
MOVE_ATR = 0.10  # daily ATR is far too wide for a one-hour window; 0.5 gave NEITHER on every visit
MS_DAY = 86_400_000


def _day_ms(text):
    day = dt.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(day.timestamp() * 1000)


def _episodes(candles, node):
    """Yield (index, approach) for each visit to the zone, approach being 'BELOW' or 'ABOVE'."""
    inside = False
    previous = None
    for index, candle in enumerate(candles):
        touching = candle.low <= node.high_price and candle.high >= node.low_price
        if touching and not inside and previous is not None:
            approach = "BELOW" if previous.close < node.low_price else "ABOVE"
            yield index, approach
        inside = touching
        previous = candle


def _outcome(candles, start, node, approach, atr):
    margin = MOVE_ATR * atr
    window = candles[start + 1:start + 1 + MINUTES]
    for candle in window:
        if approach == "BELOW":
            if candle.high >= node.high_price + margin:
                return "BROKE"
            if candle.close <= node.low_price - margin:
                return "REJECTED"
        else:
            if candle.low <= node.low_price - margin:
                return "BROKE"
            if candle.close >= node.high_price + margin:
                return "REJECTED"
    return "NEITHER"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="data_cache")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbols", type=int, default=0)
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    specs = dataset.load_specs(root)
    cache = HistoricalCache(root=root, strict=False)
    catalog = StaticCatalog(specs)
    scanner = Scanner(rest=None, catalog=catalog, cache=cache, journal=None)
    symbols = [spec.symbol for spec in specs]
    if args.symbols:
        symbols = symbols[:args.symbols]

    tally = collections.Counter()
    first_day, last_day = _day_ms(args.start), _day_ms(args.end)
    for symbol in symbols:
        for day_start in range(first_day, last_day + MS_DAY, MS_DAY):
            session_id = sessions.session_id(day_start)
            cur_start, cur_end = sessions.current_session(day_start)
            candles = cache.minute_candles(symbol, cur_start, cur_end)
            if not candles:
                continue
            bundle, _reason = scanner.frozen_bundle(symbol, session_id, day_start)
            if bundle is None:
                continue
            for node in bundle.levels.hvns:
                visits = list(_episodes(candles, node))
                for number, (index, approach) in enumerate(visits):
                    outcome = _outcome(candles, index, node, approach, bundle.atr)
                    key = ("POC" if node.is_poc else "OTHER",
                           "FIRST" if number == 0 else "RETEST", outcome)
                    tally[key] += 1

    lines = [f"window {args.start} .. {args.end}, symbols {len(symbols)}"]
    groups = sorted({(k[0], k[1]) for k in tally})
    for zone_kind, visit_kind in groups:
        total = sum(v for k, v in tally.items() if k[0] == zone_kind and k[1] == visit_kind)
        parts = []
        for outcome in ("REJECTED", "BROKE", "NEITHER"):
            count = tally.get((zone_kind, visit_kind, outcome), 0)
            parts.append(f"{outcome}={count} ({100.0 * count / total:.1f}%)")
        lines.append(f"  {zone_kind:<5} {visit_kind:<6} n={total:<6} " + "  ".join(parts))
    print("\n".join(lines))
    return lines


if __name__ == "__main__":
    main(sys.argv[1:])
