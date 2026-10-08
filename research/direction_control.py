"""Direction control: does the SIDE of the call carry information, or not?

THE QUESTION, AND WHY THE EXISTING CONTROL CANNOT ANSWER IT. research/control.py holds
direction fixed and randomizes entry timing - it answers "does this zone's timing beat a
random bar," never "was BUY the right call here rather than SELL." A setup that beats its
own random-timing twins could still be choosing the wrong side more often than the right
one, if the sampled period's own drift pays for the mistake.

THE MIRROR. For each real trade, build its exact opposite: same symbol, same entry
instant, same entry price, same risk distance and same reward multiple, direction
flipped. Nothing else moves - not the moment, not the price, not the stop/target
magnitude, only which way the bet points. This is a PAIRED comparison (one mirror per
real trade, same instant), which is a stronger design than the random-timing twins: the
only thing that can explain a difference between a trade and its own mirror is direction.

If the mirror does as well as, or better than, the real trade, the direction call is not
where the edge was - the opposite side at that exact instant would have earned the same
R. Only if the real side clearly beats its own mirror does this setup's reversal
direction carry real information, rather than riding on geometry or market drift that
would have paid off either way.

Run:
    python -m research.direction_control --trades calibration/zone_watch_v2_full/trades.csv
"""
import argparse
import csv
import logging
import os
import statistics
import time

import config
from data import klines as klines_mod
from research import dataset, replay as replay_mod
from research.control import _Twin, _twin_levels
from research.historical import HistoricalCache, StaticCatalog

log = logging.getLogger(__name__)

_FLIP = {"BUY": "SELL", "SELL": "BUY"}

# A mirror scored on the real trade's own MAX_HOLD_BARS times out disproportionately on
# its best outcomes - see simulate()'s hold_bars docstring. Widened here only, never on
# the real side, so this is a fairer look at the mirror, not a changed live horizon.
MIRROR_HOLD_BARS = replay_mod.MAX_HOLD_BARS * 4

MIRROR_FIELDS = ["symbol", "session_id", "setup", "direction", "mirror_of_direction",
                 "decision_ts", "entry_price", "stop_price", "target_price", "quantity",
                 "risk_distance", "r_multiple", "atr", "ofr_zone_kind", "outcome",
                 "exit_price", "exit_ts", "bars_to_exit", "net_r", "mfe_r", "mae_r"]


def mirror_for(cache, row):
    """The opposite-direction counterfactual of one real trade, at the same instant.

    None when the row cannot be mirrored (no risk geometry, or the forward window has no
    candles) - the same "cannot be measured" cases simulate() itself refuses.
    """
    symbol = row["symbol"]
    direction = row["direction"]
    flipped = _FLIP.get(direction)
    risk_distance = float(row.get("risk_distance") or 0)
    r_multiple = float(row.get("r_multiple") or 0)
    if flipped is None or risk_distance <= 0 or r_multiple <= 0:
        return None

    decision_ts = row.get("decision_ts")
    if decision_ts in (None, ""):
        return None
    decision_ts = int(float(decision_ts))

    confirm_ms = klines_mod.INTERVAL_MS.get(config.CONFIRM_INTERVAL, 900_000)
    forward = cache.forward_candles(
        symbol, decision_ts + 1,
        decision_ts + MIRROR_HOLD_BARS * confirm_ms
        + config.ENTRY_TIMEOUT_SECONDS * 1000 + 2 * confirm_ms)
    if not forward:
        return None

    entry = float(row["entry_price"])
    quantity = float(row.get("quantity") or 1.0)
    atr = float(row.get("atr") or 0.0)
    stop, target = _twin_levels(entry, flipped, risk_distance, r_multiple)
    mirror = _Twin(symbol, flipped, entry, stop, target, quantity, atr)

    # Same fill mechanics the real trade used - a MARKET entry re-fills at forward[0].open
    # for BOTH sides identically, so this never introduces a fill-mechanics difference.
    # The widened hold_bars is the ONLY deliberate difference from the real trade's own
    # simulation - see MIRROR_HOLD_BARS above.
    outcome = replay_mod.simulate(mirror, forward,
                                  market_fill=row.get("entry_mode") == "MARKET",
                                  hold_bars=MIRROR_HOLD_BARS)

    return {
        "symbol": symbol, "session_id": row["session_id"], "setup": row["setup"],
        "direction": flipped, "mirror_of_direction": direction,
        "decision_ts": decision_ts, "entry_price": entry, "stop_price": stop,
        "target_price": target, "quantity": quantity,
        "risk_distance": round(risk_distance, 10), "r_multiple": round(r_multiple, 5),
        "atr": atr, "ofr_zone_kind": row.get("ofr_zone_kind"),
        **outcome,
    }


def build(trades_path, root=None):
    """(real, mirrors) - real is filtered to resolved rows only, mirrors are paired 1:1."""
    root = root or config.PARQUET_DIR
    specs = dataset.load_specs(root)
    if not specs:
        raise SystemExit("no corpus - run research.dataset first")
    cache = HistoricalCache(root=root, strict=False)

    with open(trades_path, "r", encoding="utf-8", newline="") as handle:
        rows = [row for row in csv.DictReader(handle)
               if int(row.get("is_control") or 0) == 0
               and row.get("outcome") in ("STOP", "TARGET")]
    if not rows:
        raise SystemExit(f"no resolved real trades in {trades_path}")

    real, mirrors = [], []
    last_symbol = None
    started = time.time()
    for index, row in enumerate(rows, start=1):
        if row["symbol"] != last_symbol:
            if last_symbol:
                cache.release(last_symbol)
            last_symbol = row["symbol"]
        mirror = mirror_for(cache, row)
        if mirror is None or mirror["outcome"] not in ("STOP", "TARGET"):
            continue                       # paired test needs BOTH sides resolved
        real.append(row)
        mirrors.append(mirror)
        if index % 250 == 0:
            log.info("direction control: %d/%d rows (%.0fs)",
                     index, len(rows), time.time() - started)

    log.info("direction control: %d paired mirrors (of %d resolved real trades)",
             len(mirrors), len(rows))
    return real, mirrors


def write_csv(path, mirrors):
    if not mirrors:
        return None
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MIRROR_FIELDS)
        writer.writeheader()
        for row in mirrors:
            writer.writerow({k: row.get(k) for k in MIRROR_FIELDS})
    return path


def report(real, mirrors):
    """Paired comparison: each real trade against its OWN mirror, same instant.

    Paired, not the independent-sample test research/control.py uses - the mirror shares
    everything with its real trade except direction, so the per-pair DIFFERENCE is what
    carries the signal, and pairing cancels whatever is common to both (the session's own
    drift included) far better than comparing two separate group means would.
    """
    diffs = [float(r["net_r"]) - float(m["net_r"]) for r, m in zip(real, mirrors)]
    n = len(diffs)
    real_mean = statistics.fmean(float(r["net_r"]) for r in real)
    mirror_mean = statistics.fmean(float(m["net_r"]) for m in mirrors)
    real_win = sum(1 for r in real if float(r["net_r"]) > 0) / n
    mirror_win = sum(1 for m in mirrors if float(m["net_r"]) > 0) / n

    mean_diff = statistics.fmean(diffs)
    se = (statistics.stdev(diffs) / (n ** 0.5)) if n > 1 else 0.0
    low, high = mean_diff - 1.96 * se, mean_diff + 1.96 * se
    if low > 0:
        verdict = "REAL SIDE BEATS ITS MIRROR"
    elif high < 0:
        verdict = "MIRROR BEATS THE REAL SIDE"
    else:
        verdict = "no separation"

    real_wins_pair = sum(1 for r, m in zip(real, mirrors)
                        if float(r["net_r"]) > float(m["net_r"]))
    ties_pair = sum(1 for r, m in zip(real, mirrors)
                    if float(r["net_r"]) == float(m["net_r"]))

    print(f"\n{'=' * 78}")
    print(f"DIRECTION CONTROL   {n} paired trades (real vs. its own opposite-side mirror)")
    print(f"{'=' * 78}")
    print("Same symbol, same instant, same entry price, same risk/reward distances;")
    print("only the side is flipped. The question is whether OUR side beat the")
    print("opposite side at that exact moment - not whether either side was profitable.\n")

    print(f"  real side    n={n:4d}  meanR={real_mean:+.4f}  win={real_win:.1%}")
    print(f"  mirror side  n={n:4d}  meanR={mirror_mean:+.4f}  win={mirror_win:.1%}")
    print(f"  paired diff  meanR={mean_diff:+.4f}  95% CI [{low:+.4f}, {high:+.4f}]  "
         f"-> {verdict}")
    print(f"  real side wins the pair outright: {real_wins_pair}/{n} ({real_wins_pair/n:.1%}) "
         f"  (ties: {ties_pair})")

    return verdict


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trades", required=True)
    parser.add_argument("--root", default=None)
    parser.add_argument("--out", default=None,
                        help="where to write mirrors.csv (default beside --trades)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)sZ %(levelname)s %(message)s")
    logging.Formatter.converter = time.gmtime

    real, mirrors = build(args.trades, root=args.root)
    out_dir = args.out or os.path.dirname(args.trades) or "."
    path = write_csv(os.path.join(out_dir, "mirrors.csv"), mirrors)
    report(real, mirrors)
    if path:
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
