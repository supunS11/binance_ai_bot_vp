"""Order-flow confirmation study inside the redesigned workflow - research only.

PRE-REGISTERED 2026-10-13, written before any output of this script was seen. Runs
ONLY on data_cache_mainnet (the original cache holds testnet candles for
2026-05-30..2026-09-26 - see research/rebuild_mainnet_cache.py).

WORKFLOW (the user's design, components held behind flags in the live code):
  1. 1D trend       research/context_labels.swing_structure_context on the last 90
                    daily candles before the session (UP / DOWN / ROTATION / None)
  2. VP shape       the shape direction detector with the held shape bundle ON:
                    day_bias(prior shape) (PB_VA_CLOSE_LOCATION + TREND_CLOSE_LOCATION),
                    and for a B prior the B-zone read (classify_b_zone) at the touch
  3. zone           S4-OFR's zones (ofr._zones) - first touch sets the side, live's rule
  4. confirmation   the methods below, watched from the touch exactly like
                    setups/zone_watch.py (ends on a wrong-way 1m close, may restart on
                    a later touch)

  ALIGNED = the trend allows the side (UP -> BUY only, DOWN -> SELL only, ROTATION or
  unknown -> both) AND the shape detector does not oppose it. This is the population
  that decides.

CONFIRMATION METHODS (each fires at most once per zone per session, at the close of
the 1m bar where it is first true):
  C0     none - the touch itself (reference)
  ABS_BRK   CURRENT live: absorption (gate 1) then structure break (gate 2), live's
            0.5 x ATR15 distance cap (confirm_lab.watch_entries E1, validated vs live)
  ABS       Dale: absorption alone (E2)
  ABS_AGG   Dale's best: absorption then a 5m bar of aggressive orders on our side
            holding the zone (E3)
  AGG       Dale #3 alone: a completed 5m bar at/near the zone with delta_frac >=
            OFR_ABSORB_DELTA_FRAC on our side, closing back on the approach side of the
            zone's far edge
  CVD       Dale #4: CVD divergence over the last OFR_CVD_LOOKBACK_BARS 5m bars
            (ofr._cvd_divergence) while the latest bar touches the zone
  PIN       price-action rejection (Dale's PA confirmation): a completed 5m bar touching
            the zone, wick on the far side >= 50% of its range, close in the third of
            the range nearest the approach side, volume >= the 20-bar median
  FAIL      failed auction / excess (AK Ch.7): a 1m close beyond the far edge, then
            within 30 minutes a 1m close back beyond the near edge, never more than
            0.30 daily ATR beyond the far edge in between
  Not testable on this data: big resting limit orders (needs order-book history) and
  footprint stacked imbalances (needs trades by price for 240 days).

OUTCOME - direction only, independent of the later entry/SL/TP stage. From the close of
the confirming 1m bar (price P), on the following bars: does price reach P + D in the
trade's direction before P - D against it? D = 0.15 and 0.30 daily ATR. A bar reaching
both is AMBIGUOUS, neither within 1440 minutes NONE (both excluded). Random direction
gives ~50%.

CONTROL. Each real zone gets 3 control zones of identical width at the real centre
+ U([-1.5,-0.3] U [0.3,1.5]) daily ATR, seeded per symbol-session, run through the
identical workflow and confirmation code (same day's trend and shape labels).

WINDOWS. Held-out = sessions before 2026-06-15 (decides); discovery = from 2026-06-15.

PASS for a confirmation method M (held-out, ALIGNED touches, D = 0.15):
  (a) M at real zones beats C0 at real zones by >= 3 percentage points, 95% cluster
      (symbol-session) bootstrap CI > 0 - the confirmation adds information; AND
  (b) M at real zones beats M at control zones, CI > 0 - it works BECAUSE it is at a
      zone, which is the reason the workflow waits for one.
Reported for every method, both D, both windows, aligned and all touches, by side.

Run:
    PARQUET_DIR=data_cache_mainnet python -m research.confirm_study --shard-index 0 \
        --shard-count 4 --out calibration/confirm_study/shard0.csv
    python -m research.confirm_study --analyse "calibration/confirm_study/shard*.csv"
"""
import argparse
import csv
import glob
import logging
import random
import time
from types import SimpleNamespace

import numpy as np

import config
import sessions
from data import klines as klines_mod
from profile import shape as shape_mod
from research import confirm_lab
from research import context_labels as cl
from research import dataset
from research.confirm_lab import _closed_upto
from research.historical import HistoricalCache, StaticCatalog
from research.zone_watch_replay import _shard
from scanner import Scanner
from setups import orderflow_reversal as ofr

log = logging.getLogger(__name__)

SPLIT = "2026-06-15"
DISTANCES = (0.15, 0.30)
CONTROLS = 3
OFFSET_MIN, OFFSET_MAX = 0.3, 1.5
MAX_MINUTES = 1440
SWING_LOOKBACK = 90
FAIL_WINDOW = 30
FAIL_MAX_ATR = 0.30
SHAPE_FLAGS = ("PB_VA_CLOSE_LOCATION_ENABLED", "TREND_CLOSE_LOCATION_BIAS_ENABLED",
               "B_ZONE_BIAS_ENABLED")
METHODS = ("C0", "ABS_BRK", "ABS", "ABS_AGG", "AGG", "CVD", "PIN", "FAIL")
LAB_NAMES = {"E0": "C0", "E1": "ABS_BRK", "E2": "ABS", "E3": "ABS_AGG"}

FIELDS = ["symbol", "session_id", "control", "zone_id", "zone_kind", "side", "trend",
          "shape_bias", "aligned", "method", "minute", "dist_atr"] + \
         [f"d{int(d * 100)}" for d in DISTANCES]


# ------------------------------------------------------------------ components 1-2

def trend_label(daily, start):
    bars = [d for d in daily if d.close_time < start][-SWING_LOOKBACK:]
    return cl.swing_structure_context(bars)


def shape_bias(prior_shape, session_candles, upto):
    """The held shape bundle's direction read at minute `upto` (inclusive)."""
    for name in SHAPE_FLAGS:
        setattr(config, name, True)
    try:
        if prior_shape is not None and prior_shape.label == "B":
            seen = session_candles[:upto + 1]
            dev = SimpleNamespace(open=seen[0].open, high=max(c.high for c in seen),
                                  low=min(c.low for c in seen), volume={0: 1.0})
            read = shape_mod.classify_b_zone(prior_shape, dev, seen[-1].close)
            if read is None:
                return None
            return {"BULL": "BULL", "BULL_REVERT": "BULL", "BEAR": "BEAR",
                    "BEAR_REVERT": "BEAR"}.get(read.bias, "NEUTRAL")
        return shape_mod.day_bias(prior_shape)
    finally:
        for name in SHAPE_FLAGS:
            setattr(config, name, False)


def aligned(trend, bias, side):
    if not cl.allows(trend, side):
        return False
    return not ((bias == "BULL" and side == "SELL") or (bias == "BEAR" and side == "BUY"))


# ------------------------------------------------------------------ confirmations

def _bar_minute(close_times_1m, bar_close):
    """Index of the 1m candle whose close is the 5m bar's close."""
    import bisect
    return bisect.bisect_left(close_times_1m, bar_close)


def extra_confirmations(candles, zone, bars5, atr):
    """{method: (1m index, side)} for AGG, CVD and PIN (watched like zone_watch: from a
    touch until a wrong-way 1m close, restarting on a later touch) and FAIL."""
    out = {}
    t5 = [b.close_time for b in bars5]
    t1 = [c.close_time for c in candles]
    frac = config.OFR_ABSORB_DELTA_FRAC
    side = None
    seen5 = 0
    for i in range(1, len(candles)):
        last = candles[i]
        if side is None:
            if not ofr._touches(last, zone):
                continue
            prev = candles[i - 1].close
            side = "BUY" if prev > zone.high else "SELL" if prev < zone.low else None
            if side is not None:
                seen5 = len(_closed_upto(bars5, t5, last.close_time))
            continue
        if (side == "BUY" and last.close < zone.low) or (side == "SELL" and last.close > zone.high):
            side = None
            continue
        bars = _closed_upto(bars5, t5, last.close_time)
        if len(bars) <= seen5 or bars[-1].close_time != last.close_time:
            continue                     # act only when a 5m bar has just completed
        bar = bars[-1]
        near = bar.low <= zone.high + 0.5 * (zone.high - zone.low) and \
            bar.high >= zone.low - 0.5 * (zone.high - zone.low)
        d = ofr._delta_frac(bar)
        if "AGG" not in out and near:
            holds = bar.close >= zone.low if side == "BUY" else bar.close <= zone.high
            if holds and ((side == "BUY" and d >= frac) or (side == "SELL" and d <= -frac)):
                out["AGG"] = (i, side)
        if "CVD" not in out and ofr._touches(bar, zone) and \
                len(bars) >= 4 and ofr._cvd_divergence(bars, side):
            out["CVD"] = (i, side)
        if "PIN" not in out and ofr._touches(bar, zone):
            rng = bar.high - bar.low
            recent = sorted(b.volume for b in bars[-21:-1])
            median = recent[len(recent) // 2] if recent else 0.0
            if rng > 0 and bar.volume >= median:
                if side == "BUY":
                    wick = min(bar.open, bar.close) - bar.low
                    good = bar.close >= bar.high - rng / 3.0
                else:
                    wick = bar.high - max(bar.open, bar.close)
                    good = bar.close <= bar.low + rng / 3.0
                if wick >= 0.5 * rng and good:
                    out["PIN"] = (i, side)
        if all(k in out for k in ("AGG", "CVD", "PIN")):
            break
    fail = failed_auction(candles, zone, atr)
    if fail is not None:
        out["FAIL"] = fail
    return out


def failed_auction(candles, zone, atr):
    """(1m index, side) of AK's failed auction at the zone's first touch, or None."""
    side, touch = None, None
    for i in range(1, len(candles)):
        if ofr._touches(candles[i], zone):
            prev = candles[i - 1].close
            side = "BUY" if prev > zone.high else "SELL" if prev < zone.low else None
            touch = i
            break
    if side is None:
        return None
    limit = FAIL_MAX_ATR * atr
    beyond_at = None
    for i in range(touch, len(candles)):
        c = candles[i]
        if beyond_at is None:
            if (side == "BUY" and c.close < zone.low) or (side == "SELL" and c.close > zone.high):
                beyond_at = i
            continue
        if (side == "BUY" and c.low < zone.low - limit) or \
                (side == "SELL" and c.high > zone.high + limit):
            return None
        if i - beyond_at > FAIL_WINDOW:
            return None
        if (side == "BUY" and c.close > zone.high) or (side == "SELL" and c.close < zone.low):
            return i, side
    return None


# ------------------------------------------------------------------ outcome

def direction_outcome(highs, lows, i, side, price, dist):
    """CORRECT / WRONG / AMBIGUOUS / NONE from bar i+1 on (module docstring)."""
    end = min(len(highs), i + 1 + MAX_MINUTES)
    h, l = highs[i + 1:end], lows[i + 1:end]
    if h.size == 0:
        return "NONE"
    up, down = h >= price + dist, l <= price - dist
    good, bad = (up, down) if side == "BUY" else (down, up)
    g = int(np.argmax(good)) if good.any() else None
    b = int(np.argmax(bad)) if bad.any() else None
    if g is None and b is None:
        return "NONE"
    if g is None:
        return "WRONG"
    if b is None:
        return "CORRECT"
    if g == b:
        return "AMBIGUOUS"
    return "CORRECT" if g < b else "WRONG"


# ------------------------------------------------------------------ dataset

def session_rows(scanner, cache, symbol, session_id, daily):
    start = sessions.session_start_ms_from_id(session_id)
    end = start + sessions.MS_DAY
    cache.set_as_of(start)
    cache.clear_frozen()
    bundle, _ = scanner.frozen_bundle(symbol, session_id, start)
    if bundle is None or bundle.levels is None or not bundle.atr or bundle.atr <= 0:
        return []
    atr = bundle.atr
    allc = cache.forward_candles(symbol, start, end + MAX_MINUTES * 60_000)
    candles = [c for c in allc if c.open_time < end]
    if len(candles) < 60:
        return []
    highs = np.array([c.high for c in allc])
    lows = np.array([c.low for c in allc])
    bars5 = klines_mod.resample(candles, config.OFR_BAR_INTERVAL, "1m")
    bars15 = klines_mod.resample(candles, config.OFR_PLAN_ATR_INTERVAL, "1m")
    trend = trend_label(daily, start)

    zones = ofr._zones(bundle.levels, atr)
    rng = random.Random(f"{symbol}|{session_id}")
    variants = []
    for zone_id, zone in enumerate(zones):
        variants.append((0, zone_id, zone))
        width = zone.high - zone.low
        for _ in range(CONTROLS):
            offset = rng.uniform(OFFSET_MIN, OFFSET_MAX) * atr * rng.choice((-1, 1))
            centre = zone.center + offset
            variants.append((1, zone_id, ofr.Zone(zone.kind, centre - width / 2,
                                                  centre + width / 2)))
    rows = []
    for control, zone_id, zone in variants:
        events = {LAB_NAMES[k]: (i, side) for k, (i, side, _c)
                  in confirm_lab.watch_entries(candles, zone, atr, bars5, bars15).items()}
        if "C0" not in events:
            continue                                  # never actionably touched
        events.update(extra_confirmations(candles, zone, bars5, atr))
        touch_i, touch_side = events["C0"]
        bias = shape_bias(bundle.shape, candles, touch_i)
        for method, (i, side) in events.items():
            price = candles[i].close
            edge = zone.high if side == "BUY" else zone.low
            row = {"symbol": symbol, "session_id": session_id, "control": control,
                   "zone_id": zone_id, "zone_kind": zone.kind, "side": side,
                   "trend": trend, "shape_bias": bias,
                   "aligned": int(aligned(trend, bias, side)), "method": method,
                   "minute": i, "dist_atr": round(abs(price - edge) / atr, 4)}
            for d in DISTANCES:
                row[f"d{int(d * 100)}"] = direction_outcome(highs, lows, i, side, price, d * atr)
            rows.append(row)
    return rows


def run(args):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "mainnet" not in config.PARQUET_DIR:
        raise SystemExit("refusing to run: PARQUET_DIR must be the mainnet-verified cache")
    for name in SHAPE_FLAGS:
        setattr(config, name, False)
    root = config.PARQUET_DIR
    catalog = StaticCatalog(dataset.load_specs(root))
    entries = [r for r in dataset.load_universe(root) if catalog.get(r["symbol"])]
    entries = _shard(entries, args.shard_index, args.shard_count)
    started = time.time()
    with open(args.out, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for index, entry in enumerate(entries, start=1):
            symbol = entry["symbol"]
            cache = HistoricalCache(root=root, strict=False)
            scanner = Scanner(rest=None, catalog=catalog, cache=cache, journal=None)
            daily = cache.load_daily(symbol)
            count = 0
            for session_id in cache.available_sessions(symbol)[1:]:
                try:
                    rows = session_rows(scanner, cache, symbol, session_id, daily)
                except Exception as exc:                     # noqa: BLE001
                    log.warning("%s %s failed: %s", symbol, session_id, exc)
                    continue
                writer.writerows(rows)
                count += len(rows)
            handle.flush()
            cache.release()
            log.info("[%d/%d] %-14s %d rows %.0fs", index, len(entries), symbol, count,
                     time.time() - started)


# ------------------------------------------------------------------ analysis

def _rate_diff(a, b, col, iterations=2000, seed=9):
    """Correct-rate difference a - b with a 95% CI, resampling symbol-sessions."""
    def per_cluster(df):
        ok = df[df[col].isin(["CORRECT", "WRONG"])]
        key = ok.symbol + "|" + ok.session_id
        return (ok[col] == "CORRECT").groupby(key).agg(["sum", "count"])
    ga, gb = per_cluster(a), per_cluster(b)
    if ga.empty or gb.empty:
        return None
    keys = sorted(set(ga.index) | set(gb.index))
    ga, gb = ga.reindex(keys, fill_value=0), gb.reindex(keys, fill_value=0)
    sa, na = ga["sum"].to_numpy(float), ga["count"].to_numpy(float)
    sb, nb = gb["sum"].to_numpy(float), gb["count"].to_numpy(float)
    point = sa.sum() / na.sum() - sb.sum() / nb.sum()
    rng = np.random.default_rng(seed)
    n = len(keys)
    boots = []
    for _ in range(iterations):
        p = rng.integers(0, n, n)
        if na[p].sum() and nb[p].sum():
            boots.append(sa[p].sum() / na[p].sum() - sb[p].sum() / nb[p].sum())
    return (int(na.sum()), sa.sum() / na.sum(), int(nb.sum()), sb.sum() / nb.sum(), point,
            float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5)))


def _fmt(res):
    if res is None:
        return "n/a"
    n, ra, nb, rb, d, lo, hi = res
    return f"n={n:5d} {ra:6.1%} vs {rb:6.1%} (n={nb:5d}) diff {d:+6.1%} [{lo:+6.1%}, {hi:+6.1%}]"


def analyse(pattern):
    import pandas as pd
    df = pd.concat([pd.read_csv(p) for p in sorted(glob.glob(pattern))], ignore_index=True)
    out = [f"rows {len(df)}, symbols {df.symbol.nunique()}, sessions "
           f"{df.session_id.min()}..{df.session_id.max()}"]
    real0 = df[(df.control == 0) & (df.method == "C0")]
    out.append(f"real first touches {len(real0)}; aligned {real0.aligned.mean():.1%}; trend "
               f"{real0.trend.fillna('None').value_counts().to_dict()}; shape "
               f"{real0.shape_bias.fillna('None').value_counts().to_dict()}")
    verdicts = {}
    for window, part in (("HELD-OUT (< %s) - DECIDES" % SPLIT, df[df.session_id < SPLIT]),
                         ("DISCOVERY (>= %s) - reported" % SPLIT, df[df.session_id >= SPLIT])):
        for population, pop in (("ALIGNED", part[part.aligned == 1]), ("ALL touches", part)):
            out.append(f"\n=== {window} | {population} ===")
            real, ctrl = pop[pop.control == 0], pop[pop.control == 1]
            for d in DISTANCES:
                col = f"d{int(d * 100)}"
                out.append(f"-- D = {d} daily ATR: share of confirmations where price went "
                           f"D our way before D against --")
                base = real[real.method == "C0"]
                ok =base[base[col].isin(["CORRECT", "WRONG"])]
                out.append(f"  C0 (touch, no confirmation) at real zones: n={len(ok)} "
                           f"{(ok[col] == 'CORRECT').mean():.1%}")
                for m in METHODS[1:]:
                    rm, cm = real[real.method == m], ctrl[ctrl.method == m]
                    vs_c0 = _rate_diff(rm, base, col)
                    vs_ctrl = _rate_diff(rm, cm, col)
                    flag = ""
                    if window.startswith("HELD") and population == "ALIGNED" and d == DISTANCES[0]:
                        passed = bool(vs_c0 and vs_ctrl and vs_c0[4] >= 0.03 and vs_c0[5] > 0
                                      and vs_ctrl[5] > 0)
                        verdicts[m] = passed
                        flag = "PASS" if passed else "fail"
                    out.append(f"  {m:8s} vs C0   {_fmt(vs_c0)} {flag}")
                    out.append(f"  {'':8s} vs ctrl {_fmt(vs_ctrl)}")
                    if d == DISTANCES[0]:
                        for side in ("BUY", "SELL"):
                            r = _rate_diff(rm[rm.side == side], base[base.side == side], col,
                                           iterations=300)
                            out.append(f"      {side:4s} vs C0 {_fmt(r)}")
    out.append("\nPRE-REGISTERED VERDICT (held-out, aligned, D=0.15: beats C0 by >= 3pp with "
               "CI > 0 AND beats its own random-level controls with CI > 0):")
    for m, ok in verdicts.items():
        out.append(f"  {m:8s} {'PASS' if ok else 'FAIL'}")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--analyse")
    args = parser.parse_args(argv)
    if args.analyse:
        report = analyse(args.analyse)
        print(report)
        with open("calibration/confirm_study/report.txt", "w", encoding="utf-8") as h:
            h.write(report + "\n")
        return 0
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
