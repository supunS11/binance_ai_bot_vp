"""Workflow v2 stage 1: daily market state - shared with the live S6-WF setup (setups/workflow.py), so live and research run the same rules.
Design: calibration/workflow_v2/design_plan_DRAFT.md (Stage 1).

  structure  Turtle System 2 (trend_methods.m_turtle 55/20) on daily bars; the label for day D
             is the state at the close of D-1 (trend_methods.day_labels), user's choice.
  value      10-day composite value migration (trend_combo.composite_value_label, n=10,
             d=0.5 daily ATR) over the daily value areas of completed days before D; needs 20
             contiguous calendar days of values (as in trend_combo.build_rows).
  state      both UP -> TREND_UP; both DOWN -> TREND_DOWN; Turtle RANGE and value ROTATION ->
             BALANCE; anything else (incl. missing input) -> UNCLEAR (no trade). Mandatory
             agreement: user decision D2 (2026-10-15).
  veto       prior day shape (shape_v2 FINAL) THIN against the trend -> no path-T trades that
             day (the one consistent shape warning, calibration/trend_shape_v2).
"""
import bisect
import csv
import datetime as dt
import os

import numpy as np

import sessions
from data import klines as klines_mod
from profile import builder
from profile import levels as levels_mod
from research import shape_v2
from research import trend_combo as tc
from research import trend_methods as tm

TREND_UP, TREND_DOWN, BALANCE, UNCLEAR = "TREND_UP", "TREND_DOWN", "BALANCE", "UNCLEAR"
VALUE_N, VALUE_D = 10, 0.5


def load_daily_long(root, symbol):
    """(days, high, low, close) from <root>/1d/<symbol>/daily.csv (data_cache_daily_long)."""
    path = os.path.join(root, "1d", symbol, "daily.csv")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as h:
        rows = list(csv.reader(h))[1:]
    days = [dt.datetime.fromtimestamp(int(r[0]) / 1000, dt.timezone.utc).date().isoformat()
            for r in rows]
    a = np.array([[float(x) for x in r[1:6]] for r in rows])
    return days, a[:, 1], a[:, 2], a[:, 3]


def day_value_row(symbol, sid, candles, daily, tick):
    """One completed day's value row (POC / VAH / VAL on the live bin lattice, daily ATR as of
    the day's start), or None. candles: that day's 1m candles (later ones are ignored); daily:
    1d candles (only those closed before the day are read). Shared with the live workflow."""
    start = sessions.session_start_ms_from_id(sid)
    end = start + sessions.MS_DAY - 1
    k = bisect.bisect_left([d.close_time for d in daily], start)
    if k < 15:
        return None
    atr = klines_mod.daily_atr(daily[:k])
    size = builder.compute_bin_size(atr, tick) if atr > 0 else 0
    candles = [c for c in candles if start <= c.open_time <= end]
    if size <= 0 or len(candles) < 60:
        return None
    coverage = klines_mod.coverage(candles, "1m", start, end)
    prof = builder.profile_as_of(symbol, candles, start, end, end, size, coverage=coverage)
    lv = levels_mod.compute(prof)
    if lv is None:
        return None
    return {"symbol": symbol, "day": sid, "open": candles[0].open,
            "high": max(c.high for c in candles), "low": min(c.low for c in candles),
            "close": candles[-1].close, "atr": atr, "poc": lv.poc_price,
            "vah": lv.vah, "val": lv.val, "coverage": round(coverage, 4)}


def turtle_by_day(daily):
    """{day: UP | DOWN | RANGE | None}; the label for day i uses bars before i only."""
    days, h, lo, c = daily
    labels = tm.day_labels(tm.m_turtle(h, lo, c, 55, 20))
    return dict(zip(days, labels))


def value_by_day(values):
    """{day: UP | DOWN | ROTATION | None} from daily value rows (dicts with day, poc, atr;
    sorted by day), using completed days before each day only."""
    out = {}
    for j, r in enumerate(values):
        ok = j - 2 * VALUE_N >= 0 and (
            sessions.session_start_ms_from_id(values[j]["day"])
            - sessions.session_start_ms_from_id(values[j - 2 * VALUE_N]["day"])
            == 2 * VALUE_N * sessions.MS_DAY)
        out[r["day"]] = tc.composite_value_label(values, j, VALUE_N, VALUE_D) if ok else None
    return out


def combine(turtle, value):
    if turtle == tm.UP and value == tc.UP:
        return TREND_UP
    if turtle == tm.DOWN and value == tc.DOWN:
        return TREND_DOWN
    if turtle == tm.RANGE and value == tc.ROT:
        return BALANCE
    return UNCLEAR


def prior_shape_label(prof, lv):
    """research/shape_v2 FINAL label of a day's profile (the veto's input; also
    daily_shapes.v2_label)."""
    bins = {prof.bin_center(i): v for i, v in prof.volume.items() if v > 0}
    return shape_v2.classify_v2({"bins": bins, "low": prof.low, "high": prof.high,
                                 "open": prof.open, "close": prof.close,
                                 "vah": lv.vah, "val": lv.val}, shape_v2.FINAL)


def veto_day(state, prior_shape):
    """True when the prior day is a thin day against the trend (no path-T trades)."""
    return (state == TREND_UP and prior_shape == "THIN_DOWN") or (
        state == TREND_DOWN and prior_shape == "THIN_UP")


def turtle_before(h, lo, c):
    """Turtle label for the day after the last bar given (= turtle_by_day for that day): the
    state at the close of the last completed daily bar."""
    if len(c) == 0:
        return None
    return tm.m_turtle(list(h), list(lo), list(c), 55, 20)[-1]


def value_before(prior, sid):
    """value_by_day's label for day sid from its completed days' rows (prior, sorted, every
    day < sid): the 2 x VALUE_N rows before sid must be exactly the 2 x VALUE_N calendar days
    before it."""
    if len(prior) < 2 * VALUE_N:
        return None
    first = sessions.session_start_ms_from_id(prior[-2 * VALUE_N]["day"])
    if sessions.session_start_ms_from_id(sid) - first != 2 * VALUE_N * sessions.MS_DAY:
        return None
    return tc.composite_value_label(prior, len(prior), VALUE_N, VALUE_D)


def state_before(h, lo, c, prior, sid):
    """(state, turtle label, value label) for day sid from data completed before it."""
    t, v = turtle_before(h, lo, c), value_before(prior, sid)
    return combine(t, v), t, v


def states_for_symbol(daily, values):
    t, v = turtle_by_day(daily), value_by_day(values)
    return {d: combine(t.get(d), v.get(d)) for d in v}
