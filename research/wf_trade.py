"""Workflow v2 stages 4-5: entry, stop, targets, simulation. Design:
calibration/workflow_v2/design_plan_DRAFT.md (Stages 4-5). The pure planning functions (entry_m,
stop_price, target_candidates, pick_targets, plan) are SHARED with the live workflow
(setups/workflow.py), so research and live plan a trade with the same code; the simulation
(run_bracket, t1_reached, direction_d15) is research only and imports its helpers lazily.

All arrays are 1m forward arrays (open/high/low/close) indexed like the session's candles,
continuing past the session end for the holding period. `dec` = index of the 1m bar whose
close is the decision point. BUY orientation in the comments.

ENTRIES
  M  (accumulated order-flow read, research/of_ledger - the workflow's entry since 2026-10-16)
     market order at the open of the minute after the confirmation minute. The confirmation is
     itself the evidence (failure of the attack, then unusual buying WITH a price result and a
     close back above the zone); waiting for a further bar break would re-introduce a candle
     rule, and a resting limit for a retest fills mainly when the buyers LOSE the initiative
     the confirmation just read (adverse selection). Taker fee + slippage.
  A  (playbook "confirmation entry") stop order 1 tick above the INI bar's high, valid for the
     NEXT 5m bar only (playbook: "enter when the next bar breaks its high"). Fill price =
     max(trigger, that minute's open) (a gap fills worse). Taker fee + slippage.
  B  (playbook "retest of the imbalance origin") limit at the lowest price of the stacked
     imbalance, valid until the order-flow window ends (touch + 60 min). Filled only when
     price trades THROUGH it by one tick; fill = the limit. Maker fee, no slippage.
  R  (80% rule) market order at the next 1m open after the signal close.
  C0 (no order flow - the L3 control) limit at the zone edge on the touch bar.
STOPS (where the idea is wrong, + 0.2 x ATR15 = live OFR_STOP_BUFFER_ATR)
  T, X, F  beyond the lowest low from the touch to the decision (the hold / failed-auction
           extreme; for M: the interaction low the failure reading was made at - trading
           below it means the defence did not hold).  R  beyond the session's extreme outside
           value.  C0  beyond the zone.
TARGETS (single exit at T2; T1 only reported - user decision D5)
  T, X  candidates beyond the entry: prior-day high, VAH, POC, VAL, low, prior-day HVN
        centres, naked daily POCs of the last 10 sessions, 15m swing highs/lows of the last 10
        sessions. T1 = the nearest; T2 = the nearest at >= 2R. None at >= 2R -> skip.
        + the 10-session composite's VAH / VAL / POC / HVN peaks (2026-10-17).
  F, R  T1 = prior POC, T2 = the opposite value edge; skip when T2 < 2R.
  FC    T1 = composite POC, T2 = the opposite composite value edge; skip when T2 < 2R.
SKIPS  stop on the wrong side; fees > 1/3 of the risk (live STOP_MIN_COST_MULTIPLE); no T2.
SIMULATION  confirm_lab.simulate (stop wins a same-minute tie, 1440-minute horizon, no
  management). On the fill minute only adverse movement counts (the intrabar order is
  unknown). Costs are applied in the analysis from `cost_unit` = entry / risk.
"""
import numpy as np

import config
from research import impulse_leg as il
from research import trend_combo as tc

STOP_BUFFER_ATR15 = 0.20
MIN_R_T2 = 2.0
A_VALID_MIN = 5
NAKED_POC_SESSIONS = 10


def entry_m(arr, dec):
    """(fill index, fill price) of a market order sent at the close of bar `dec`: the next
    bar's open, or None when there is no next bar."""
    o = arr[0]
    f = dec + 1
    if f >= len(o) or not np.isfinite(o[f]):
        return None
    return f, float(o[f])


def target_candidates(levels, prof, prior_values, seen_low, seen_high, hist15, atr,
                      composite_levels=()):
    """Path T / X target candidates known at the decision (see TARGETS above).
    levels / prof: the prior day's levels and profile; prior_values: daily value rows (dicts
    with poc, high, low) of the last NAKED_POC_SESSIONS completed days, oldest first;
    seen_low / seen_high: this session's range up to the decision; hist15: closed 15m bars of
    the last 10 sessions up to the decision; composite_levels: the 10-session composite's VAH,
    VAL, POC and HVN peaks (wf_composite.composite_levels; playbook Step 5: T2 = the next
    higher-timeframe level)."""
    c = [levels.vah, levels.val, levels.poc_price, prof.high, prof.low] + list(composite_levels)
    c += [(n.low_price + n.high_price) / 2 for n in levels.hvns]
    for k, r in enumerate(prior_values):                     # naked POCs
        p = r["poc"]
        later = prior_values[k + 1:]
        if any(x["low"] <= p <= x["high"] for x in later) or seen_low <= p <= seen_high:
            continue
        c.append(p)
    if hist15:
        piv = tc.zigzag_pivots([il._Close(b.close) for b in hist15], [atr] * len(hist15),
                               il.SWING_ATR)
        for _, bi, _, kind in piv:
            c.append(hist15[bi].high if kind == "H" else hist15[bi].low)
    return c


def entry_a(arr, dec, side, ini_extreme, tick):
    o, h, l, _ = arr
    trig = ini_extreme + tick if side == "BUY" else ini_extreme - tick
    for j in range(dec + 1, min(dec + 1 + A_VALID_MIN, len(h))):
        if side == "BUY" and h[j] >= trig:
            return j, max(trig, o[j])
        if side == "SELL" and l[j] <= trig:
            return j, min(trig, o[j])
    return None


def entry_b(arr, dec, side, limit, tick, last_index):
    _, h, l, _ = arr
    for j in range(dec + 1, min(last_index + 1, len(h))):
        if side == "BUY" and l[j] < limit - tick:
            return j, limit
        if side == "SELL" and h[j] > limit + tick:
            return j, limit
    return None


def stop_price(side, extreme, atr15):
    return extreme - STOP_BUFFER_ATR15 * atr15 if side == "BUY" else extreme + STOP_BUFFER_ATR15 * atr15


def level_stop(side, extreme, level, atr15):
    """Stop for a level trade (T, X, F, FC): beyond the interaction extreme AND beyond the level
    itself. A stop between the entry and the level is hit by an ordinary test of the level -
    the very event the trade sells into (added 2026-10-18, BASEDUSDT: stop 0.048965 under the
    VAL 0.04914, stopped at 0.04903 without the level being tested)."""
    beyond = min(extreme, level) if side == "BUY" else max(extreme, level)
    return stop_price(side, beyond, atr15)


def pick_targets(side, entry, risk, candidates):
    """(T1, T2) from candidate prices: T1 nearest beyond entry, T2 nearest at >= 2R."""
    sgn = 1 if side == "BUY" else -1
    beyond = sorted({p for p in candidates if p is not None and sgn * (p - entry) > 0},
                    key=lambda p: sgn * (p - entry))
    t1 = beyond[0] if beyond else None
    t2 = next((p for p in beyond if sgn * (p - entry) >= MIN_R_T2 * risk), None)
    return t1, t2


def plan(side, entry, stop, t1, t2, fee=config.FEE_TAKER):
    """Skip reason or '' for a planned trade."""
    if (side == "BUY" and stop >= entry) or (side == "SELL" and stop <= entry):
        return "stop_side"
    risk = abs(entry - stop)
    if 2 * fee * entry / risk > 1.0 / config.STOP_MIN_COST_MULTIPLE:
        return "stop_too_tight"
    if t2 is None:
        return "no_t2"
    sgn = 1 if side == "BUY" else -1
    if sgn * (t2 - entry) < MIN_R_T2 * risk:
        return "t2_under_2r"
    return ""


def run_bracket(arr, f0, side, entry, stop, target):
    """Outcome fields for a fill at index f0 (only adverse movement on the fill minute)."""
    from research import confirm_lab
    _, h, l, c = arr
    f1 = min(len(h), f0 + confirm_lab.HOLD_MINUTES)
    hh, ll = h[f0:f1].copy(), l[f0:f1].copy()
    if len(hh) == 0:
        return {"outcome": "NO_DATA", "gross": 0.0}
    if side == "BUY":
        hh[0] = min(hh[0], entry)
    else:
        ll[0] = max(ll[0], entry)
    outcome, gross, minutes = confirm_lab.simulate(hh, ll, c[f0:f1], side, entry, stop, target, False)
    return {"outcome": outcome, "gross": round(float(gross), 4), "minutes": int(minutes)}


def t1_reached(arr, f0, side, entry, stop, t1):
    """Whether T1 was reached before the stop (reported only; does not change the trade)."""
    if t1 is None:
        return ""
    r = run_bracket(arr, f0, side, entry, stop, t1)
    return int(r["outcome"] == "TARGET")


def direction_d15(arr, i, side, atr):
    _, h, l, c = arr
    from research import confirm_study as cs
    return cs.direction_outcome(np.asarray(h), np.asarray(l), i, side, c[i], 0.15 * atr)
