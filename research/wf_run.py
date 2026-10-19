"""Workflow v2 runner - research only, never imported by the live bot.
Design and pre-registration: calibration/workflow_v2/design_plan_DRAFT.md.

For every symbol-session with validated footprints:
  state     wf_state (Turtle 55/20 + 10-day composite value; veto = prior-day THIN against).
  watches   wf_levels for EVERY pseudo-state (TREND_UP, TREND_DOWN, BALANCE), tagged
            allowed = (pseudo == real state, and no veto for trend paths). allowed rows are
            the workflow; the others are the L1 'wrong state' control. Each allowed watch also
            gets 3 random-offset copies of its zone (0.3-1.5 daily ATR, same side and time
            window) - the L2 'random level' control.
  order flow --of ledger (THE WORKFLOW since 2026-10-16, = live S6-WF): the accumulated read
            research/of_ledger, entry M (market at the next minute's open), stop beyond the
            interaction low, + C0 (no order flow, L3 control).
            --of recipes (superseded): wf_orderflow bar recipes per path (T1/T2/T3/X: DEF_INI
            primary, EXH_INI secondary; F: FA_DEF_INI primary, DEF_INI secondary) + C0.
  R         80% rule (and its R_CTRL matched control), no order flow.
  trades    wf_trade: entries A / B (C0: limit at the zone edge, R: market), path stops and
            targets, skips. BASE rows (baseline 1): for every traded allowed real primary-entry
            row, a seeded random minute of the same session, same side, market entry, the same
            risk distance and the same target distance in R.
Order flow is read on a two-day flow (wf_orderflow.TwoDayFlow: previous day + this one), so
early touches keep their 120-minute reference; touches after minute 1379 are recorded with skip
'window' (the 60-minute window must end inside the day's footprint).

MODES
  dev   counts only: no trade outcome, gross R, or direction field is computed or written
        (the plan's blind build stage).
  full  everything (only for the pre-registered holdout run, after the freeze).

Run:
  PARQUET_DIR=data_cache_mainnet python -m research.wf_run --mode dev \
      --fp data_footprint_crypto,data_footprint_fresh --symbols-file <list> \
      --values "calibration/trend_combo/values_s*.csv" --daily data_cache_daily_long \
      --shard-index 0 --shard-count 6 --out <csv>
"""
import argparse
import bisect
import csv
import datetime as dt
import logging
import os
import random
import time

import numpy as np

import config
import sessions
from data import klines as klines_mod
from research import confirm_study as cs
from research import daily_shapes, dataset
from research import impulse_leg as il
from research import of_ledger
from research import of_rules
from research import wf_composite as wc
from research import trend_combo as tc
from research import wf_levels as wl
from research import wf_orderflow as wo
from research import wf_state as ws
from research import wf_trade as wt
from research.footprint import load_day
from research.historical import HistoricalCache, StaticCatalog
from research.zone_watch_replay import _shard
from scanner import Scanner
from setups import orderflow_reversal as ofr

log = logging.getLogger(__name__)

FIRST_MIN, LAST_MIN = 0, 1440 - 1 - of_rules.WINDOW_MIN   # window must end inside the day
LEG_HISTORY_DAYS = 10
TARGET_LOOKBACK_DAYS = 10
CONTROLS, OFFSET_MIN, OFFSET_MAX = 3, 0.3, 1.5
RECIPES = {"T1": ("DEF_INI", "EXH_INI"), "T2": ("DEF_INI", "EXH_INI"), "T3": ("DEF_INI", "EXH_INI"),
           "X": ("DEF_INI", "EXH_INI"), "F": ("FA_DEF_INI", "DEF_INI")}
PRIMARY_RECIPE = {"T1": "DEF_INI", "T2": "DEF_INI", "T3": "DEF_INI", "X": "DEF_INI", "F": "FA_DEF_INI"}
DEV_FIELDS = ["symbol", "session_id", "week", "state", "veto", "prior_shape", "mid", "align", "conf",
              "dpoc", "pseudo", "allowed",
              "control", "path", "side", "kind", "touch_min", "recipe", "of_outcome", "of_path",
              "fa", "entry", "filled", "skip", "decision_min", "entry_min", "risk_atr", "t2_r"]
LEDGER_FIELDS = ["symbol", "session_id", "week", "state", "veto", "prior_shape", "mid", "align",
                 "conf", "dpoc", "pseudo", "allowed",
                 "control", "path", "side", "kind", "touch_min", "recipe", "of_outcome", "of_path",
                 "skip", "decision_min", "attempt_dt", "absorption_dt", "exhaustion_dt", "failure_dt",
                 "response_dt", "stack", "low_rows_share", "entry", "filled", "entry_min",
                 "risk_atr", "t2_r"]
OF_ENGINE = "recipes"          # "ledger" = research/of_ledger read only (no entries, no trades)
REAL_ONLY = False              # only the real state's watches (+ their L2 controls)
FULL_FIELDS = DEV_FIELDS + ["fee_kind", "cost_unit", "outcome", "gross", "minutes", "t1_hit", "d15"]


# ------------------------------------------------------------------ per-session context

class Ctx:
    def __init__(self, symbol, sid, candles, allc, atr, levels, prof, tick, b15_hist, values,
                 fp_root):
        self.symbol, self.sid, self.atr, self.levels, self.prof = symbol, sid, atr, levels, prof
        self.tick, self.b15_hist, self.values, self.fp_root = tick, b15_hist, values, fp_root
        self.candles, self.allc = candles, allc
        self.start = sessions.session_start_ms_from_id(sid)
        self.arr = tuple(np.array([getattr(c, k) for c in allc]) for k in ("open", "high", "low", "close"))
        self.minute_of = [(c.open_time - self.start) // 60_000 for c in candles]
        self.index_of = {m: i for i, m in enumerate(self.minute_of)}
        self.bars15 = klines_mod.resample(candles, "15m", "1m")
        self.t15 = [b.close_time for b in self.bars15]
        self.comp = None                      # the 10-session composite (wf_composite)

    def atr15(self, i):
        t = self.candles[i].close_time
        plan = self.bars15[:bisect.bisect_right(self.t15, t)]
        a = klines_mod.atr(plan) if len(plan) > ofr.PLAN_ATR_MIN_BARS else 0.0
        return a if a > 0 else 0.1 * self.atr

    def bins_for(self, t0, t1):
        return il.footprint_bins(lambda s, d: load_day(self.fp_root, s, d), self.symbol, t0, t1)

    def t_candidates(self, i_dec):
        """Target candidates known at decision index i_dec (T / X paths)."""
        now = self.candles[i_dec].close_time
        seen_hi = max(c_.high for c_ in self.candles[:i_dec + 1])
        seen_lo = min(c_.low for c_ in self.candles[:i_dec + 1])
        prior = [r for r in self.values if r["day"] < self.sid][-TARGET_LOOKBACK_DAYS:]
        hist = [b for b in self.b15_hist if b.close_time < now
                and b.open_time >= self.start - TARGET_LOOKBACK_DAYS * sessions.MS_DAY]
        return wt.target_candidates(self.levels, self.prof, prior, seen_lo, seen_hi, hist,
                                    self.atr, wc.composite_levels(self.comp))


# ------------------------------------------------------------------ trades

def _trade(ctx, side, path, entry_kind, f, entry, stop, t1, t2, mode, fee_kind):
    out = {"entry": entry_kind, "filled": 1, "entry_min": ctx.minute_of[f] if f < len(ctx.minute_of) else "",
           "risk_atr": round(abs(entry - stop) / ctx.atr, 4)}
    skip = wt.plan(side, entry, stop, t1, t2)
    if skip:
        return {**out, "skip": skip}
    risk = abs(entry - stop)
    out["t2_r"] = round(abs(t2 - entry) / risk, 3)
    if mode == "full":
        r = wt.run_bracket(ctx.arr, f, side, entry, stop, t2)
        out.update(fee_kind=fee_kind, cost_unit=round(entry / risk, 4), outcome=r["outcome"],
                   gross=r["gross"], minutes=r.get("minutes", ""),
                   t1_hit=wt.t1_reached(ctx.arr, f, side, entry, stop, t1))
    return {**out, "skip": ""}


def _targets(ctx, path, side, entry, risk, i_dec):
    if path == "FC":
        return wc.value_watch_targets(ctx.comp, side)
    if path in ("F", "R"):
        lv = ctx.levels
        t2 = lv.vah if side == "BUY" else lv.val
        return lv.poc_price, t2
    return wt.pick_targets(side, entry, risk, ctx.t_candidates(i_dec))


def confirm_trades(ctx, w, res, i_dec, mode):
    """Entry A and B rows for one CONFIRM."""
    side, path = w["side"], w["path"]
    extreme = float(np.min(ctx.arr[2][w["i"]:i_dec + 1])) if side == "BUY" \
        else float(np.max(ctx.arr[1][w["i"]:i_dec + 1]))
    stop = (wt.level_stop(side, extreme, w["level"], ctx.atr15(i_dec)) if config.WF_STOP_BEYOND_LEVEL
            else wt.stop_price(side, extreme, ctx.atr15(i_dec)))
    rows = []
    a = wt.entry_a(ctx.arr, i_dec, side, res["ini_high"], ctx.tick)
    if a is None:
        rows.append({"entry": "A", "filled": 0, "skip": "a_not_triggered"})
    else:
        f, e = a
        t1, t2 = _targets(ctx, path, side, e, abs(e - stop), i_dec)
        rows.append(_trade(ctx, side, path, "A", f, e, stop, t1, t2, mode, "taker"))
    if res["stack_row"] is None:
        rows.append({"entry": "B", "filled": 0, "skip": "no_stack_origin"})
    else:
        rs = ctx.row_size
        limit = res["stack_row"] * rs if side == "BUY" else (res["stack_row"] + 1) * rs
        last = ctx.index_of.get(ctx.minute_of[w["i"]] + of_rules.WINDOW_MIN, len(ctx.candles) - 1)
        b = wt.entry_b(ctx.arr, i_dec, side, limit, ctx.tick, last)
        if b is None:
            rows.append({"entry": "B", "filled": 0, "skip": "b_not_filled"})
        else:
            f, e = b
            t1, t2 = _targets(ctx, path, side, e, abs(e - stop), i_dec)
            rows.append(_trade(ctx, side, path, "B", f, e, stop, t1, t2, mode, "maker"))
    return rows


def c0_trade(ctx, w, mode):
    """L3 control: limit at the zone edge on the touch, stop beyond the zone."""
    side, i = w["side"], w["i"]
    lo, hi = w["zone"]
    entry = hi if side == "BUY" else lo
    stop = wt.stop_price(side, lo if side == "BUY" else hi, ctx.atr15(i))
    t1, t2 = _targets(ctx, w["path"], side, entry, abs(entry - stop), i)
    return _trade(ctx, side, w["path"], "C0", i, entry, stop, t1, t2, mode, "maker")


# ------------------------------------------------------------------ session

def session_rows(scanner, cache, symbol, sid, fp_root, state_of, values, tick, daily_long, mode):
    raw = load_day(fp_root, symbol, sid)
    if raw is None:
        return []
    start = sessions.session_start_ms_from_id(sid)
    end = start + sessions.MS_DAY
    cache.set_as_of(start)
    cache.clear_frozen()
    bundle, _ = scanner.frozen_bundle(symbol, sid, start)
    if bundle is None or bundle.levels is None or not bundle.atr or bundle.atr <= 0:
        return []
    hist = cache.minute_candles(symbol, start - LEG_HISTORY_DAYS * sessions.MS_DAY, start)
    allc = cache.forward_candles(symbol, start, end + 1440 * 60_000)
    candles = [c for c in allc if c.open_time < end]
    if len(candles) < 60:
        return []
    b15_hist = klines_mod.resample(hist + candles, "15m", "1m")
    ctx = Ctx(symbol, sid, candles, allc, bundle.atr, bundle.levels, bundle.profile, tick,
              b15_hist, values, fp_root)
    ctx.row_size = float(np.ravel(raw["row_size"])[0])
    ctx.comp = wc.composite(symbol, hist, start, bundle.bin_size, bundle.atr)
    mid = wc.mid_label([r for r in values if r["day"] < sid], sid)
    o = np.full(1440, np.nan); h = o.copy(); l = o.copy(); c = o.copy()
    for i, m in enumerate(ctx.minute_of):
        if 0 <= m < 1440:
            o[m], h[m], l[m], c[m] = candles[i].open, candles[i].high, candles[i].low, candles[i].close
    prev_sid = sessions.session_id(start - sessions.MS_DAY)
    prev_raw = load_day(fp_root, symbol, prev_sid)
    po = np.full(1440, np.nan); ph = po.copy(); pl = po.copy(); pc = po.copy()
    for cc in hist:
        m = (cc.open_time - (start - sessions.MS_DAY)) // 60_000
        if 0 <= m < 1440:
            po[m], ph[m], pl[m], pc[m] = cc.open, cc.high, cc.low, cc.close
    flows = {s: wo.TwoDayFlow(prev_raw, (po, ph, pl, pc), raw, (o, h, l, c), s) for s in ("BUY", "SELL")}
    state = state_of.get(sid, ws.UNCLEAR)
    shape = daily_shapes.v2_label(bundle.profile, bundle.levels)
    veto = ws.veto_day(state, shape)
    base = {"symbol": symbol, "session_id": sid, "week": str(dt.date.fromisoformat(sid).isocalendar()[:2]),
            "state": state, "veto": int(veto), "prior_shape": shape or "", "mid": mid or ""}
    b30 = wl.bars30(candles)
    rng = random.Random(f"{symbol}|{sid}")
    watches = []
    for pseudo in (ws.TREND_UP, ws.TREND_DOWN, ws.BALANCE):
        allowed = int(pseudo == state and not (veto and pseudo != ws.BALANCE))
        if REAL_ONLY and not allowed:
            continue
        vw = wl.value_watches(pseudo, False, bundle.levels, bundle.atr, candles, b30)
        lw, _ = wl.leg_watches(pseudo, False, bundle.atr, b15_hist, candles, ctx.bins_for)
        cw = wl.composite_watches(pseudo, ctx.comp, bundle.atr, candles, b30,
                                  wc.fc_sides(pseudo, wc.alignment(pseudo, mid)))
        xw = wl.breakout_leg_watches(pseudo, bundle.levels, bundle.atr, b15_hist, candles, b30,
                                     ctx.bins_for)
        for w in wl.drop_duplicate_fc(vw + lw + cw + xw):
            watches.append((pseudo, allowed, 0, w))
            if allowed:
                for _ in range(CONTROLS):
                    off = rng.uniform(OFFSET_MIN, OFFSET_MAX) * bundle.atr * rng.choice((-1, 1))
                    z = (w["zone"][0] + off, w["zone"][1] + off)
                    i = wl.first_touch(candles, z, w["side"], w["from_ms"])
                    if i is None or (w["until_ms"] is not None and candles[i].open_time >= w["until_ms"]):
                        continue
                    watches.append((pseudo, allowed, 1, {**w, "zone": z, "level": w["level"] + off, "i": i,
                                                         "conf_parent": wc.watch_confluence(w, ctx.comp, ctx.atr)}))
    rows = []
    for pseudo, allowed, control, w in watches:
        t0 = ctx.minute_of[w["i"]]
        wb = {**base, "pseudo": pseudo, "allowed": allowed, "control": control, "path": w["path"],
              "side": w["side"], "kind": w["kind"], "touch_min": t0,
              "align": wc.alignment(pseudo, mid), "conf": w.get("conf_parent", wc.watch_confluence(w, ctx.comp, ctx.atr))}
        if allowed and control == 0:
            wb["dpoc"] = wc.dpoc_position(symbol, candles[:w["i"] + 1], start, bundle.bin_size,
                                          bundle.levels)
        if not FIRST_MIN <= t0 <= LAST_MIN:
            rows.append({**wb, "skip": "window"})
            continue
        if OF_ENGINE == "ledger":
            rows.append({**wb, "recipe": "C0", **c0_trade(ctx, w, mode)})
            rows.append({**wb, **ledger_trade_row(ctx, flows[w["side"]], w, t0, mode)})
            continue
        rows.append({**wb, "recipe": "C0", **c0_trade(ctx, w, mode)})
        for recipe in RECIPES[w["path"]]:
            res = wo.evaluate(flows[w["side"]], w["zone"][0], w["zone"][1], t0 + wo.DAY, recipe)
            if res["minute"] is not None:
                res["minute"] -= wo.DAY
            rb = {**wb, "recipe": recipe, "of_outcome": res["outcome"], "of_path": res["path"] or "",
                  "fa": int(res["fa"])}
            if res["outcome"] != "CONFIRM" or res["minute"] not in ctx.index_of:
                rows.append(rb)
                continue
            i_dec = ctx.index_of[res["minute"]]
            rb["decision_min"] = res["minute"]
            if mode == "full":
                rb["d15"] = wt.direction_d15(ctx.arr, i_dec, w["side"], ctx.atr)
            for tr in confirm_trades(ctx, w, res, i_dec, mode):
                rows.append({**rb, **tr})
    rows += eighty_rows(ctx, base, state, b30, mode)
    if mode == "full":
        rows += baseline_rows(ctx, base, rows, rng)
    return rows


def ledger_row(flow, w, t0):
    """The accumulated order-flow read (research/of_ledger) for one watch."""
    r = of_ledger.evaluate(flow, w["zone"][0], w["zone"][1], t0 + wo.DAY)
    s = r["stages"]
    T = t0 + wo.DAY
    out = {"recipe": "LEDGER", "of_outcome": r["outcome"], "of_path": r["path"] or ""}
    if r["minute"] is not None:
        out["decision_min"] = r["minute"] - wo.DAY
    for k in ("attempt", "absorption", "exhaustion", "failure", "response"):
        if k in s:
            out[k + "_dt"] = s[k] - T
    ev = r["evidence"]
    if "stack" in ev:
        out["stack"] = ev["stack"]
        out["low_rows_share"] = "" if ev["low_rows_share"] is None else round(ev["low_rows_share"], 4)
    return out


def ledger_trade_row(ctx, flow, w, t0, mode):
    """The ledger read for one watch plus, on a CONFIRM, the workflow's trade: entry M (market
    at the next minute's open), stop beyond the interaction low, the path's targets."""
    out = ledger_row(flow, w, t0)
    if out["of_outcome"] != "CONFIRM" or out.get("decision_min") not in ctx.index_of:
        return out
    i_dec = ctx.index_of[out["decision_min"]]
    if mode == "full":
        out["d15"] = wt.direction_d15(ctx.arr, i_dec, w["side"], ctx.atr)
    side = w["side"]
    extreme = float(np.min(ctx.arr[2][w["i"]:i_dec + 1])) if side == "BUY" \
        else float(np.max(ctx.arr[1][w["i"]:i_dec + 1]))
    stop = (wt.level_stop(side, extreme, w["level"], ctx.atr15(i_dec)) if config.WF_STOP_BEYOND_LEVEL
            else wt.stop_price(side, extreme, ctx.atr15(i_dec)))
    m = wt.entry_m(ctx.arr, i_dec)
    if m is None:
        return {**out, "entry": "M", "filled": 0, "skip": "no_bar"}
    f, e = m
    t1, t2 = _targets(ctx, w["path"], side, e, abs(e - stop), i_dec)
    return {**out, **_trade(ctx, side, w["path"], "M", f, e, stop, t1, t2, mode, "taker")}


def eighty_rows(ctx, base, state, b30, mode):
    r = wl.eighty_rule(ctx.candles, b30, ctx.levels.vah, ctx.levels.val)
    if r is None:
        return []
    kind, side, t, ext = r
    i_sig = max(k for k, c_ in enumerate(ctx.candles) if c_.close_time < t)
    row = {**base, "pseudo": ws.BALANCE, "allowed": int(state == ws.BALANCE),
           "control": 0 if kind == "R" else 2, "path": "R", "side": side, "kind": kind,
           "touch_min": ctx.minute_of[i_sig], "recipe": "NONE", "decision_min": ctx.minute_of[i_sig]}
    f = i_sig + 1
    if f >= len(ctx.candles):
        return [{**row, "skip": "no_bar"}]
    entry = float(ctx.arr[0][f])
    stop = wt.stop_price(side, ext, ctx.atr15(i_sig))
    t1, t2 = _targets(ctx, "R", side, entry, abs(entry - stop), i_sig)
    if mode == "full":
        row["d15"] = wt.direction_d15(ctx.arr, i_sig, side, ctx.atr)
    return [{**row, **_trade(ctx, side, "R", "R", f, entry, stop, t1, t2, mode, "taker")}]


def baseline_rows(ctx, base, rows, rng):
    """Baseline 1: random minute, same side, same risk and target distance (full mode only)."""
    out = []
    for r in rows:
        primary = (r.get("recipe") == "LEDGER" and r.get("entry") == "M") if OF_ENGINE == "ledger" \
            else (r.get("entry") == "A" and r.get("recipe") == PRIMARY_RECIPE.get(r["path"]))
        if not (r.get("allowed") == 1 and r.get("control") == 0 and r.get("skip") == "" and primary):
            continue
        m = rng.randint(FIRST_MIN, LAST_MIN)
        if m not in ctx.index_of or ctx.index_of[m] + 1 >= len(ctx.candles):
            continue
        f = ctx.index_of[m] + 1
        entry = float(ctx.arr[0][f])
        risk = r["risk_atr"] * ctx.atr
        sgn = 1 if r["side"] == "BUY" else -1
        stop, t2 = entry - sgn * risk, entry + sgn * r["t2_r"] * risk
        res = wt.run_bracket(ctx.arr, f, r["side"], entry, stop, t2)
        out.append({**base, "pseudo": r["pseudo"], "allowed": 1, "control": 3, "path": r["path"],
                    "side": r["side"], "kind": "BASE", "touch_min": m, "recipe": "BASE",
                    "entry": "M", "filled": 1, "skip": "", "entry_min": m + 1,
                    "risk_atr": r["risk_atr"], "t2_r": r["t2_r"], "fee_kind": "taker",
                    "cost_unit": round(entry / risk, 4), "outcome": res["outcome"],
                    "gross": res["gross"], "minutes": res.get("minutes", "")})
    return out


# ------------------------------------------------------------------ driver

def run(args):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "mainnet" not in config.PARQUET_DIR:
        raise SystemExit("refusing to run: PARQUET_DIR must be a mainnet-verified cache")
    for name in cs.SHAPE_FLAGS:
        setattr(config, name, False)
    root = config.PARQUET_DIR
    catalog = StaticCatalog(dataset.load_specs(root))
    fp_roots = args.fp.split(",")
    values_by = tc.load_values(args.values)
    wanted = [s.strip() for s in open(args.symbols_file, encoding="utf-8") if s.strip()]
    todo = []
    for s in wanted:
        fp = next((d for d in fp_roots if os.path.isdir(os.path.join(d, s))), None)
        if fp and catalog.get(s) and s in values_by:
            todo.append({"symbol": s, "fp": fp})
        else:
            log.warning("skipping %s (footprints / spec / values missing)", s)
    todo = _shard(todo, args.shard_index, args.shard_count)
    global OF_ENGINE, REAL_ONLY
    OF_ENGINE, REAL_ONLY = args.of, args.real_only
    extra = FULL_FIELDS[len(DEV_FIELDS):] if args.mode == "full" else []
    fields = (LEDGER_FIELDS if OF_ENGINE == "ledger" else DEV_FIELDS) + extra
    started = time.time()
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for k, e in enumerate(todo, 1):
            symbol = e["symbol"]
            daily = ws.load_daily_long(args.daily, symbol)
            if daily is None:
                log.warning("%s: no long daily history", symbol)
                continue
            state_of = ws.states_for_symbol(daily, values_by[symbol])
            tick = float(catalog.get(symbol).tick_size)
            cache = HistoricalCache(root=root, strict=False)
            scanner = Scanner(rest=None, catalog=catalog, cache=cache, journal=None)
            n = 0
            for sid in cache.available_sessions(symbol)[1:]:
                if args.first and sid < args.first or args.last and sid > args.last:
                    continue
                try:
                    rows = session_rows(scanner, cache, symbol, sid, e["fp"], state_of,
                                        values_by[symbol], tick, daily, args.mode)
                except Exception as exc:                    # noqa: BLE001
                    log.warning("%s %s failed: %s", symbol, sid, exc)
                    continue
                w.writerows(rows)
                n += len(rows)
            fh.flush()
            cache.release()
            log.info("[%d/%d] %-14s %d rows %.0fs", k, len(todo), symbol, n, time.time() - started)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--mode", choices=("dev", "full"), required=True)
    p.add_argument("--fp", required=True)
    p.add_argument("--symbols-file", required=True)
    p.add_argument("--values", required=True)
    p.add_argument("--daily", default="data_cache_daily_long")
    p.add_argument("--first", default=None)
    p.add_argument("--last", default=None)
    p.add_argument("--shard-index", type=int, default=0)
    p.add_argument("--shard-count", type=int, default=1)
    p.add_argument("--out", required=True)
    p.add_argument("--of", choices=("recipes", "ledger"), default="recipes")
    p.add_argument("--real-only", action="store_true")
    run(p.parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
