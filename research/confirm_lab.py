"""Entry-confirmation x stop x target x management grid for S4-OFR zones - research
only, never imported by the live bot.

For every eligible session (one with a row in the level-touch dataset), every S4-OFR
zone is watched minute by minute exactly like setups/zone_watch.py (watch starts on a
touch with the side set by the previous 1m close, ends on a wrong-way 1m close, may
restart on a later touch, 5m bars, the bot's own ofr._absorption and
ofr.structure_break). Each ENTRY variant takes at most one trade per zone per session:

  E0  no confirmation - limit fill at the zone edge on the first actionable touch
  E1  CURRENT - gate 1 absorption, then gate 2 structure break (close beyond the
      base), market entry, max 0.5 x ATR15 from the zone (live's own check)
  E2  Dale: enter as soon as absorption is identified (Order Flow book p.79-80)
  E3  Dale's best combination: absorption, then a completed 5m bar of aggressive
      orders on OUR side (delta_frac >= OFR_ABSORB_DELTA_FRAC) still holding the zone
      (book p.87)

Market entries fill at the next 1m open (same as research/zone_watch_replay).

STOP methods (Dale, Order Flow book p.107-108; VP book p.151):
  Sst   CURRENT structural: beyond min(zone, absorption bar extreme) + 0.2 x ATR15
  Szn   beyond the S/R zone's own edge + 0.05 x daily ATR
  Slv   in the low-volume node (LVN) behind the level (its centre); skipped if none
        within 0.5 daily ATR of the zone
  S10/S15/S20  fixed 10/15/20% of daily ATR from entry (Dale: "10-20% of average
        daily volatility")

TARGET methods:
  R15/R20/R25  fixed 1.5R / 2R / 2.5R
  V15/V20  Dale's volume-based TP: just before (0.05 x daily ATR) the nearest
        heavy-volume level ahead (HVN peak, POC, opposite value-area edge, prior
        session extreme) that is at least 1.5R / 2R away; no such level -> no trade
  CUR  live's ladder: nearest of HVN/POC/VA edge/prior extreme >= 1.2R, else 2R

MANAGEMENT: M0 none; MBE stop to entry once +1R is reached (Dale moves to the
reaction point; break-even is the conservative, unambiguous version).

Every trade also obeys live's stop_too_tight rule (fees > 1/3 R -> not taken).
Fees: taker both sides. A bar reaching stop and target together counts as a stop.

Run (one process per shard):
    python -m research.confirm_lab --shard-index 0 --shard-count 4 --out calibration/confirm/shard0.csv
"""
import argparse
import csv
import logging
import time

import numpy as np
import pandas as pd

import config
import sessions
from data import klines as klines_mod
from research import dataset
from research.historical import HistoricalCache, StaticCatalog
from research.zone_watch_replay import _shard
from scanner import Scanner
from setups import orderflow_reversal as ofr

log = logging.getLogger(__name__)

MINUTE = 60_000
HOLD_MINUTES = 1440
ENTRIES = ("E0", "E1", "E2", "E3")
STOPS = ("Sst", "Szn", "Slv", "S10", "S15", "S20")
TARGETS = ("R15", "R20", "R25", "V15", "V20", "CUR")
MGMT = ("M0", "MBE")
FIELDS = ["symbol", "session_id", "zone_kind", "direction", "entry_variant", "entry_ts",
          "entry", "atr", "atr15", "stop_variant", "target_variant", "mgmt", "risk_atr",
          "reward_r", "outcome", "gross_r", "net_r", "minutes"]


# ------------------------------------------------------------------ outcomes

def simulate(highs, lows, closes, side, entry, stop, target, breakeven):
    """(outcome, gross_r, minutes) for one bracket on forward 1m arrays that start at
    the fill minute. Stop wins ties. Break-even: once +1R is reached, the stop moves to
    entry from the NEXT bar on."""
    risk = abs(entry - stop)
    sgn = 1.0 if side == "BUY" else -1.0
    fav = (highs - entry) if side == "BUY" else (entry - lows)
    adv = (entry - lows) if side == "BUY" else (highs - entry)
    hit_stop = adv >= risk
    hit_tp = fav >= abs(target - entry)
    n = len(highs)
    i_stop = int(np.argmax(hit_stop)) if hit_stop.any() else n
    i_tp = int(np.argmax(hit_tp)) if hit_tp.any() else n
    i_be_exit = n
    if breakeven:
        reach = fav >= risk
        if reach.any():
            i_reach = int(np.argmax(reach))
            back = adv[i_reach + 1:] >= 0.0
            if back.any():
                i_be_exit = i_reach + 1 + int(np.argmax(back))
    first = min(i_stop, i_tp, i_be_exit)
    if first == n:
        if n == 0:
            return "UNRESOLVED", 0.0, 0
        return "UNRESOLVED", ((closes[-1] - entry) * sgn) / risk, n
    if first == i_stop:
        return "STOP", -1.0, first
    if first == i_be_exit and i_be_exit < i_tp:
        return "BREAKEVEN", 0.0, first
    return "TARGET", abs(target - entry) / risk, first


# ------------------------------------------------------------------ stops/targets

def stop_price(variant, side, entry, zone, cluster, atr, atr15, lvns):
    sgn = -1 if side == "BUY" else 1
    if variant == "Sst":
        if side == "BUY":
            return min(zone.low, cluster) - config.OFR_STOP_BUFFER_ATR * atr15
        return max(zone.high, cluster) + config.OFR_STOP_BUFFER_ATR * atr15
    if variant == "Szn":
        edge = zone.low if side == "BUY" else zone.high
        return edge + sgn * 0.05 * atr
    if variant == "Slv":
        if side == "BUY":
            behind = [n for n in lvns if n.high_price < zone.low
                      and zone.low - n.center_price <= 0.5 * atr]
            return max(behind, key=lambda n: n.center_price).center_price if behind else None
        behind = [n for n in lvns if n.low_price > zone.high
                  and n.center_price - zone.high <= 0.5 * atr]
        return min(behind, key=lambda n: n.center_price).center_price if behind else None
    frac = {"S10": 0.10, "S15": 0.15, "S20": 0.20}[variant]
    return entry + sgn * frac * atr


def target_price(variant, side, entry, risk, levels, prior_extreme, atr):
    sgn = 1 if side == "BUY" else -1
    if variant in ("R15", "R20", "R25"):
        return entry + sgn * {"R15": 1.5, "R20": 2.0, "R25": 2.5}[variant] * risk
    if variant == "CUR":
        tp, _ = ofr._target_ladder(entry, side, risk, levels, levels.hvns, prior_extreme)
        return tp[1]
    min_r = 1.5 if variant == "V15" else 2.0
    opposite = levels.vah if side == "BUY" else levels.val
    raw = [n.peak_price for n in levels.hvns if not n.is_poc]
    raw += [levels.poc_price, opposite, prior_extreme]
    best = None
    for level in raw:
        if level is None or level != level:
            continue
        tp = level - sgn * 0.05 * atr                       # "just before" the level
        dist = (tp - entry) * sgn
        if dist > 0 and dist / risk >= min_r and (best is None or dist < (best - entry) * sgn):
            best = tp
    return best


# ------------------------------------------------------------------ entries

def _closed_upto(bars, close_times, ts):
    """The precomputed completed bars whose close is at or before `ts` - identical to
    resampling candles[:i+1], since resample only ever returns completed buckets."""
    import bisect
    return bars[:bisect.bisect_right(close_times, ts)]


def watch_entries(candles, zone, atr, bars5=None, bars15=None):
    """Replay the zone watch on one zone for one session. Returns {variant: (index of
    the 1m bar whose CLOSE triggered, side, cluster_price)} - E0 is the touch itself."""
    out = {}
    state = None                              # None idle, else dict
    if bars5 is None:
        bars5 = klines_mod.resample(candles, config.OFR_BAR_INTERVAL, "1m")
    if bars15 is None:
        bars15 = klines_mod.resample(candles, config.OFR_PLAN_ATR_INTERVAL, "1m")
    t5 = [b.close_time for b in bars5]
    t15 = [b.close_time for b in bars15]
    for i in range(1, len(candles)):
        last = candles[i]
        if state is None:
            if not ofr._touches(last, zone):
                continue
            prev = candles[i - 1].close
            side = "BUY" if prev > zone.high else "SELL" if prev < zone.low else None
            if side is None:
                continue
            out.setdefault("E0", (i, side, None))
            state = {"side": side, "absorb": None, "cluster": None}
            continue        # ZoneWatch starts on the touch minute, steps from the next
        side = state["side"]
        if (side == "BUY" and last.close < zone.low) or (side == "SELL" and last.close > zone.high):
            state = None
            continue
        bars = _closed_upto(bars5, t5, last.close_time)
        if len(bars) < config.OFR_MIN_BARS:
            continue
        if state["absorb"] is None:
            absorb = ofr._absorption(bars, zone, side)
            if absorb is None:
                continue
            state["absorb"] = absorb
            bar = bars[absorb]
            state["cluster"] = bar.low if side == "BUY" else bar.high
            state["absorb_ts"] = bar.close_time
            out.setdefault("E2", (i, side, state["cluster"]))
        if "E3" not in out:
            frac = config.OFR_ABSORB_DELTA_FRAC
            for b in bars[state["absorb"] + 1:]:        # completed 5m bars only
                d = ofr._delta_frac(b)
                holds = b.close >= zone.low if side == "BUY" else b.close <= zone.high
                if holds and ((side == "BUY" and d >= frac) or (side == "SELL" and d <= -frac)):
                    out["E3"] = (i, side, state["cluster"])
                    break
        if "E1" not in out:
            trigger = ofr.structure_break(bars, zone, side, state["absorb"])
            moved = last.close > trigger if side == "BUY" else last.close < trigger
            if moved:
                plan = _closed_upto(bars15, t15, last.close_time)
                atr15 = klines_mod.atr(plan) if len(plan) > ofr.PLAN_ATR_MIN_BARS else 0
                away = (last.close - zone.high) if side == "BUY" else (zone.low - last.close)
                if atr15 > 0 and away <= config.OFR_ENTRY_MAX_DISTANCE_ATR * atr15:
                    out["E1"] = (i, side, state["cluster"])
        if all(v in out for v in ("E1", "E2", "E3")):
            break
    return out


def session_rows(scanner, cache, symbol, session_id):
    start = sessions.session_start_ms_from_id(session_id)
    cache.set_as_of(start)
    cache.clear_frozen()
    bundle, _ = scanner.frozen_bundle(symbol, session_id, start)
    if bundle is None or bundle.levels is None or not bundle.atr or bundle.atr <= 0:
        return []
    levels, atr = bundle.levels, bundle.atr
    allc = cache.forward_candles(symbol, start, start + sessions.MS_DAY + HOLD_MINUTES * MINUTE)
    candles = [c for c in allc if c.open_time < start + sessions.MS_DAY]
    if len(candles) < 60:
        return []
    highs = np.array([c.high for c in allc]); lows = np.array([c.low for c in allc])
    closes = np.array([c.close for c in allc]); opens = np.array([c.open for c in allc])
    rows = []
    bars5 = klines_mod.resample(candles, config.OFR_BAR_INTERVAL, "1m")
    bars15 = klines_mod.resample(candles, config.OFR_PLAN_ATR_INTERVAL, "1m")
    t15 = [b.close_time for b in bars15]
    for zone in ofr._zones(levels, atr):
        for variant, (i, side, cluster) in watch_entries(candles, zone, atr, bars5, bars15).items():
            plan = _closed_upto(bars15, t15, candles[i].close_time)
            atr15 = klines_mod.atr(plan) if len(plan) > ofr.PLAN_ATR_MIN_BARS else 0.0
            if variant == "E0":
                entry = zone.high if side == "BUY" else zone.low
                f0 = i                                       # filled on the touch bar
                cluster = entry
            else:
                if i + 1 >= len(allc):
                    continue
                entry = opens[i + 1]
                f0 = i + 1
            if atr15 <= 0:
                atr15 = 0.1 * atr
            f1 = min(len(allc), f0 + HOLD_MINUTES)
            h, l, c = highs[f0:f1], lows[f0:f1], closes[f0:f1]
            if variant == "E0":                              # touch bar: stop only
                h = h.copy(); h[0] = entry if side == "BUY" else h[0]
                l = l.copy(); l[0] = entry if side == "SELL" else l[0]
            prior_extreme = bundle.profile.high if side == "BUY" else bundle.profile.low
            for s_var in STOPS:
                stop = stop_price(s_var, side, entry, zone, cluster, atr, atr15, levels.lvns)
                if stop is None or (side == "BUY" and stop >= entry) or (side == "SELL" and stop <= entry):
                    continue
                risk = abs(entry - stop)
                cost_r = 2 * config.FEE_TAKER * entry / risk
                if cost_r > 1.0 / config.STOP_MIN_COST_MULTIPLE:
                    continue
                for t_var in TARGETS:
                    tp = target_price(t_var, side, entry, risk, levels, prior_extreme, atr)
                    if tp is None:
                        continue
                    reward = abs(tp - entry) / risk
                    for m in MGMT:
                        outcome, gross, minutes = simulate(h, l, c, side, entry, stop, tp, m == "MBE")
                        rows.append({
                            "symbol": symbol, "session_id": session_id,
                            "zone_kind": zone.kind, "direction": side,
                            "entry_variant": variant, "entry_ts": candles[i].close_time,
                            "entry": round(entry, 10), "atr": atr, "atr15": atr15,
                            "stop_variant": s_var, "target_variant": t_var, "mgmt": m,
                            "risk_atr": round(risk / atr, 4), "reward_r": round(reward, 3),
                            "outcome": outcome, "gross_r": round(gross, 4),
                            "net_r": round(gross - cost_r, 4), "minutes": minutes})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--touches", default="calibration/touch/touch_shard*.csv")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--symbols", type=int, default=None)
    parser.add_argument("--first", default=None)
    parser.add_argument("--last", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(message)s")

    import glob
    eligible = pd.concat([pd.read_csv(p, usecols=["symbol", "session_id"])
                          for p in glob.glob(args.touches)]).drop_duplicates()
    by_symbol = eligible.groupby("symbol").session_id.apply(sorted).to_dict()
    root = config.PARQUET_DIR
    catalog = StaticCatalog(dataset.load_specs(root))
    entries = [r for r in dataset.load_universe(root)
               if catalog.get(r["symbol"]) and r["symbol"] in by_symbol]
    if args.symbols:
        entries = entries[:args.symbols]
    entries = _shard(entries, args.shard_index, args.shard_count)
    started = time.time()
    with open(args.out, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for index, entry in enumerate(entries, start=1):
            symbol = entry["symbol"]
            cache = HistoricalCache(root=root, strict=False)
            scanner = Scanner(rest=None, catalog=catalog, cache=cache, journal=None)
            count = 0
            for session_id in by_symbol[symbol]:
                if (args.first and session_id < args.first) or (args.last and session_id > args.last):
                    continue
                try:
                    rows = session_rows(scanner, cache, symbol, session_id)
                except Exception as exc:                        # noqa: BLE001
                    log.warning("%s %s failed: %s", symbol, session_id, exc)
                    continue
                writer.writerows(rows)
                count += len(rows)
                cache.clear_frozen()
            handle.flush()
            cache.release()
            log.info("[%d/%d] %-14s %d rows %.0fs", index, len(entries), symbol, count,
                     time.time() - started)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
