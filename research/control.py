"""The matched random control: does the LOCATION carry the edge, or not?

THE QUESTION THIS ANSWERS, AND WHY NOTHING ELSE ANSWERS IT.

Suppose a replay reports S1-LVN at +0.08R net, significant, consistent across both time
halves. That is not yet evidence the profile found anything. A trade has several parts,
and only one of them is the setup's actual claim:

    direction   long or short
    geometry    a stop 0.4 ATR away and a target at 2R
    timing      a particular bar in a particular session
    LOCATION    that bar is at a low-volume node between price and the POC

Only the last is what a volume profile contributes. Everything before it is available
for free. If a random bar in the same session, taken in the same direction with the same
stop distance and the same target multiple, earns the same R, then the LVN contributed
nothing and the number came from market drift, from the direction, or from the fact that
a 2R target with a 0.4 ATR stop has a mechanical win rate.

So each real trade gets twins: identical symbol, session, direction and geometry,
entered at a uniformly random confirmation bar in the same session. Everything is held
constant except the thing under test. The measurement that matters is then not "is the
setup profitable" but **"does the setup beat its own twins"** - the LIFT. A setup can be
profitable and worthless, if its twins are equally profitable.

WHY SEVERAL TWINS PER TRADE. One random entry per trade gives a control as noisy as the
treatment, which wastes power precisely where samples are thin. N twins per trade shrink
the control's standard error by sqrt(N) at no cost but CPU, so the comparison is limited
by the real sample rather than by the control.

WHAT THE CONTROL DELIBERATELY SHARES. The fill requirement, the pessimistic intrabar
rule, and the fee model are all identical, because a difference in any of those would
show up as lift that is really a modelling artifact. The control is run through the SAME
simulate() function, not a copy of it.

Run:
    python -m research.control --trades phase1/trades.csv --twins 5
"""
import argparse
import csv
import logging
import os
import random
import time

import config
import sessions
from data import klines as klines_mod
from research import dataset, metrics, replay as replay_mod
from research.historical import HistoricalCache, StaticCatalog

log = logging.getLogger(__name__)


class _Twin:
    """Minimal candidate stand-in: simulate() reads only these fields."""

    def __init__(self, symbol, direction, entry, stop, target, quantity, atr):
        self.symbol = symbol
        self.direction = direction
        self.entry_price = entry
        self.stop_price = stop
        self.target_price = target
        self.quantity = quantity
        self.atr = atr

    @property
    def risk_distance(self):
        return abs(self.entry_price - self.stop_price)

    @property
    def r_multiple(self):
        risk = self.risk_distance
        return (abs(self.target_price - self.entry_price) / risk) if risk > 0 else 0.0

    def round_trip_cost_r(self):
        return metrics.round_trip_cost_r(self.risk_distance, self.entry_price,
                                        self.quantity or 1.0)


def _twin_levels(entry, direction, risk_distance, r_multiple):
    """Same geometry, relocated. Stop and target scale from the twin's own entry."""
    if direction == "BUY":
        return entry - risk_distance, entry + risk_distance * r_multiple
    return entry + risk_distance, entry - risk_distance * r_multiple


def twins_for(cache, row, count, rng):
    """Build `count` relocated twins of one real trade. Returns simulated rows."""
    symbol = row["symbol"]
    session_id = row["session_id"]
    direction = row["direction"]
    risk_distance = float(row["risk_distance"])
    r_multiple = float(row["r_multiple"])
    quantity = float(row["quantity"] or 1.0)
    atr = float(row["atr"] or 0.0)
    if risk_distance <= 0 or r_multiple <= 0:
        return []

    session_start = sessions.session_start_ms_from_id(session_id)
    session_end = session_start + sessions.MS_DAY
    confirm_ms = klines_mod.INTERVAL_MS.get(config.CONFIRM_INTERVAL, 900_000)

    minute = cache.forward_candles(symbol, session_start, session_end)
    if len(minute) < 120:
        return []
    confirm = klines_mod.resample(minute, config.CONFIRM_INTERVAL, "1m")
    # Leave room after the entry for the trade to resolve, and skip the first few bars
    # so a twin is not placed before the session has any structure at all.
    usable = confirm[2:-4]
    if len(usable) < 4:
        return []

    out = []
    for index in range(count):
        bar = rng.choice(usable)
        entry = bar.close
        stop, target = _twin_levels(entry, direction, risk_distance, r_multiple)
        twin = _Twin(symbol, direction, entry, stop, target, quantity, atr)

        forward = cache.forward_candles(
            symbol, bar.close_time + 1,
            bar.close_time + replay_mod.MAX_HOLD_BARS * confirm_ms
            + config.ENTRY_TIMEOUT_SECONDS * 1000 + 2 * confirm_ms)
        # Match the real trade's own fill mechanics: a zone-watch entry fills at market
        # (see research/zone_watch_replay.py), everything else fills on a trade-through
        # of a resting price. A twin must share whichever one produced its treatment row,
        # or a fill-mechanics difference would show up as lift that isn't really there.
        outcome = replay_mod.simulate(twin, forward,
                                      market_fill=row.get("entry_mode") == "MARKET")

        out.append({
            "symbol": symbol,
            "qv_rank": row.get("qv_rank"),
            "session_id": session_id,
            "setup": row["setup"],
            "direction": direction,
            "decision_ts": bar.open_time,
            "decision_index": None,
            "entry_price": entry,
            "stop_price": stop,
            "target_price": target,
            "quantity": quantity,
            "risk_distance": round(risk_distance, 10),
            "r_multiple": round(r_multiple, 5),
            "cost_r": round(twin.round_trip_cost_r(), 5),
            "atr": atr,
            "value_width": row.get("value_width"),
            "prior_shape": row.get("prior_shape"),
            "auction_state": row.get("auction_state"),
            "open_relationship": row.get("open_relationship"),
            "target_kind": "control",
            "confirmations": "",
            "confirmation_count": 0,
            "poc_prominence": row.get("poc_prominence"),
            "va_range_ratio": row.get("va_range_ratio"),
            "acceptance_ratio": None,
            "excursion_candles": row.get("excursion_candles"),
            # Session/level properties, not entry-timing ones - the twin shares the
            # real trade's session, direction and traded level (POC/VAH/VAL), so these
            # are identical for both. Copied through the same way prior_shape already
            # is, so Phase 3's attribution can stratify controls exactly like treatment.
            "value_migration": row.get("value_migration"),
            "bin_delta_normalized": row.get("bin_delta_normalized"),
            "poor_at_extreme": row.get("poor_at_extreme"),
            "excess_at_extreme": row.get("excess_at_extreme"),
            "is_control": 1,
            "control_of": f"{symbol}:{session_id}:{row['setup']}:{index}",
            **outcome,
        })
    return out


def build(trades_path, root=None, twins=5, seed=20260927):
    root = root or config.PARQUET_DIR
    specs = dataset.load_specs(root)
    if not specs:
        raise SystemExit("no corpus - run research.dataset first")
    cache = HistoricalCache(root=root, strict=False)

    with open(trades_path, "r", encoding="utf-8", newline="") as handle:
        real = [row for row in csv.DictReader(handle)
                if int(row.get("is_control") or 0) == 0]
    if not real:
        raise SystemExit(f"no real trades in {trades_path}")

    # A FIXED SEED, so the control is reproducible. An unseeded control makes every
    # rerun disagree slightly, and then a lift that moved cannot be distinguished from
    # a lift that was never there.
    rng = random.Random(seed)

    out = []
    started = time.time()
    last_symbol = None
    for index, row in enumerate(real, start=1):
        if row["symbol"] != last_symbol:
            if last_symbol:
                cache.release(last_symbol)
            last_symbol = row["symbol"]
        out.extend(twins_for(cache, row, twins, rng))
        if index % 250 == 0:
            log.info("controls: %d/%d real trades -> %d twins (%.0fs)",
                     index, len(real), len(out), time.time() - started)

    log.info("controls: %d twins for %d real trades", len(out), len(real))
    return real, out


# ------------------------------------------------------------------ reporting

def report(real, controls):
    """Treatment vs control, per setup. The LIFT is the finding, not the level."""
    print(f"\n{'=' * 78}")
    print(f"MATCHED RANDOM CONTROL   {len(real)} real / {len(controls)} twins")
    print(f"{'=' * 78}")
    print("Same symbol, session, direction and geometry; entry bar chosen at random.")
    print("The question is not whether a setup is profitable - it is whether the")
    print("setup beats its own twins. A profitable setup with equally profitable")
    print("twins has contributed nothing.\n")

    def resolved(rows):
        out = []
        for row in rows:
            value = row.get("net_r")
            if value in (None, "", "None"):
                continue
            out.append(dict(row, net_r=float(value)))
        return out

    real_ok = resolved(real)
    control_ok = resolved(controls)

    by_setup_real = metrics.group_by(real_ok, "setup")
    by_setup_control = metrics.group_by(control_ok, "setup")

    print(f"{'setup':<12} {'n':>5} {'treatment':>11} {'n_ctl':>6} {'control':>11} "
          f"{'lift':>10} {'verdict':>22}")
    print("-" * 82)

    verdicts = {}
    for setup in sorted(set(by_setup_real) | set(by_setup_control)):
        treatment = metrics.summarise_r(by_setup_real.get(setup, []))
        control = metrics.summarise_r(by_setup_control.get(setup, []))
        if not treatment.get("n") or not control.get("n"):
            print(f"{setup:<12} {treatment.get('n', 0):>5} "
                  f"{'-':>11} {control.get('n', 0):>6} {'-':>11} {'-':>10} "
                  f"{'insufficient data':>22}")
            verdicts[setup] = "INSUFFICIENT"
            continue

        lift = treatment["mean_r"] - control["mean_r"]
        # The lift's own interval: independent samples, so the standard errors add in
        # quadrature. Reporting the lift without an interval invites reading a noisy
        # difference as a finding.
        se = (treatment_se(treatment) ** 2 + treatment_se(control) ** 2) ** 0.5
        low, high = lift - 1.96 * se, lift + 1.96 * se
        beats = low > 0

        if beats:
            verdict = "BEATS CONTROL"
        elif high < 0:
            verdict = "WORSE than control"
        else:
            verdict = "no separation"
        verdicts[setup] = verdict

        print(f"{setup:<12} {treatment['n']:>5} {treatment['mean_r']:>+11.4f} "
              f"{control['n']:>6} {control['mean_r']:>+11.4f} "
              f"{lift:>+10.4f} {verdict:>22}")
        print(f"{'':<12} {'':>5} {'':>11} {'':>6} {'':>11} "
              f"  95% CI [{low:+.4f}, {high:+.4f}]")

    print(f"\n{'-' * 78}\nWIN RATE, treatment vs control\n{'-' * 78}")
    for setup in sorted(by_setup_real):
        treatment = metrics.summarise_r(by_setup_real[setup])
        control = metrics.summarise_r(by_setup_control.get(setup, []))
        if not control.get("n"):
            continue
        print(f"  {setup:<12} {treatment['win_rate']*100:>5.1f}% "
              f"[{treatment['win_low']*100:.1f}-{treatment['win_high']*100:.1f}]  vs "
              f"control {control['win_rate']*100:>5.1f}% "
              f"[{control['win_low']*100:.1f}-{control['win_high']*100:.1f}]")

    return verdicts


def treatment_se(summary):
    """Standard error of a summarise_r result.

    Reads the exported `se` rather than dividing the interval width by a hard-coded 1.96,
    which silently returns the wrong number for a summary taken at a corrected z. Falls
    back to the old derivation using the summary's OWN z, so an older row still works.

    Note this is now the CLUSTER-ROBUST standard error, so the lift test below inherits
    the same-day correlation correction without doing anything: a lift that only looked
    real because two thousand trades were counted as independent will no longer clear its
    interval.
    """
    if not summary.get("n") or summary["n"] <= 1:
        return 0.0
    if summary.get("se") is not None:
        return float(summary["se"])
    return (summary["r_high"] - summary["mean_r"]) / float(summary.get("z") or 1.96)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Matched random control for Phase 1.")
    parser.add_argument("--trades", default="phase1/trades.csv")
    parser.add_argument("--root", default=None)
    parser.add_argument("--twins", type=int, default=5)
    parser.add_argument("--out", default=None,
                        help="where to write controls.csv (default beside --trades)")
    parser.add_argument("--seed", type=int, default=20260927)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)sZ %(levelname)s %(message)s")
    logging.Formatter.converter = time.gmtime

    real, controls = build(args.trades, root=args.root, twins=args.twins,
                           seed=args.seed)
    out_dir = args.out or os.path.dirname(args.trades) or "."
    path = os.path.join(out_dir, "controls.csv")
    replay_mod.write_csv(path, controls)
    report(real, controls)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
