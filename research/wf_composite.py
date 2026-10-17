"""Workflow v2 composite profile, middle-timeframe label, confluence and developing-POC position.
Shared with the live S6-WF setup (setups/workflow.py), so live and research run the same rules.
Design: calibration/workflow_v2/mtf_profiles_recommendation.md.

COMPOSITE   one volume profile of the COMPOSITE_SESSIONS completed sessions before today, built
            from 1m candles on today's bin lattice with the live builder - the same construction
            as the prior-session profile (scanner.frozen_bundle), only over ten days: cVAH, cVAL,
            cPOC (70% value area) and its HVN nodes (profile/va_hvn, stability-filtered like the
            daily ones). Fixed for the whole day, so it never repaints.
MID         the trend's health over the last MID_SESSIONS completed sessions: their day-over-day
            value migrations (profile/relations.classify_migration via
            trend_combo.migration_scores, +1 higher / overlapping-higher, -1 lower /
            overlapping-lower, 0 otherwise). MID_UP: at least 2 of 3 up and none down; MID_DOWN:
            the mirror; MID_ROTATION otherwise; None when the days are not contiguous.
ALIGNMENT   playbook 5A: 1D trend vs MID. ALIGNED (same direction), RANGE (mid rotation),
            CONFLICT (mid against the trend). BALANCE days have no alignment ("").
CONFLUENCE  a zone lies within one zone width of a composite level (VAH, VAL, POC or an HVN
            peak): the zones overlap.
DPOC        where today's developing POC sits at the touch, against the prior value area:
            ABOVE / INSIDE / BELOW.
"""
import types

import sessions
from profile import builder
from profile import levels as levels_mod
from profile import va_hvn
from research import trend_combo as tc
from research import wf_state as ws

COMPOSITE_SESSIONS = 10
MID_SESSIONS = 3
MID_UP, MID_DOWN, MID_ROT = "MID_UP", "MID_DOWN", "MID_ROTATION"
ALIGNED, RANGE, CONFLICT = "ALIGNED", "RANGE", "CONFLICT"


def composite(symbol, candles, session_start, bin_size, atr):
    """The composite of the COMPOSITE_SESSIONS sessions before session_start from their 1m
    candles (others are ignored), or None. Returns a namespace: vah, val, poc, high, low, hvns
    (list of (low, high, peak) prices, the POC node excluded), coverage."""
    start = session_start - COMPOSITE_SESSIONS * sessions.MS_DAY
    end = session_start - 1
    window = [c for c in candles if start <= c.open_time and c.close_time <= end]
    if bin_size <= 0 or atr <= 0 or len(window) < 60 * COMPOSITE_SESSIONS:
        return None
    from data import klines as klines_mod
    from scanner import _rebuilt_profiles
    coverage = klines_mod.coverage(window, "1m", start, end)
    prof = builder.profile_as_of(symbol, window, start, end, end, bin_size, coverage=coverage)
    lv = levels_mod.compute(prof)
    if lv is None:
        return None
    perturbed = _rebuilt_profiles(symbol, window, start, end, end, bin_size)
    nodes = va_hvn.detect(prof, lv, atr, perturbed=perturbed)
    return types.SimpleNamespace(
        vah=lv.vah, val=lv.val, poc=lv.poc_price, high=prof.high, low=prof.low,
        hvns=[(n.low_price, n.high_price, n.peak_price) for n in nodes if not n.is_poc],
        coverage=coverage)


def composite_levels(comp):
    """Every composite price a zone can coincide with, or a target can be: VAH, VAL, POC, HVN
    peaks."""
    if comp is None:
        return []
    return [comp.vah, comp.val, comp.poc] + [p for _, _, p in comp.hvns]


def mid_label(prior, sid):
    """MID label for day sid from its completed days' value rows (sorted, every day < sid)."""
    rows = prior[-(MID_SESSIONS + 1):]
    if len(rows) < MID_SESSIONS + 1:
        return None
    first = sessions.session_start_ms_from_id(rows[0]["day"])
    if sessions.session_start_ms_from_id(sid) - first != (MID_SESSIONS + 1) * sessions.MS_DAY:
        return None
    scores = tc.migration_scores(rows)[1:]
    up, down = sum(1 for s in scores if s > 0), sum(1 for s in scores if s < 0)
    if up >= 2 and down == 0:
        return MID_UP
    if down >= 2 and up == 0:
        return MID_DOWN
    return MID_ROT


def alignment(state, mid):
    """Playbook 5A alignment of the 1D state with the middle-timeframe label."""
    if state not in (ws.TREND_UP, ws.TREND_DOWN) or mid is None:
        return ""
    with_trend = MID_UP if state == ws.TREND_UP else MID_DOWN
    against = MID_DOWN if state == ws.TREND_UP else MID_UP
    return ALIGNED if mid == with_trend else CONFLICT if mid == against else RANGE


def fc_sides(state, align):
    """Sides of the composite-edge path FC: both on a BALANCE day; on a trend day whose middle
    timeframe is a range, only the with-trend lower edge (playbook 5A: "1D up + 4H range: only
    buy the lower edge (VAL / range low)"); none otherwise."""
    if state == ws.BALANCE:
        return ("BUY", "SELL")
    if align == RANGE:
        return ("BUY",) if state == ws.TREND_UP else ("SELL",)
    return ()


def confluent(zone, comp, atr):
    """1 when the zone overlaps a composite level's zone (one zone width), else 0."""
    from research import wf_levels as wl
    width = 2 * wl.ZONE_ATR * atr
    lo, hi = zone
    return int(any(lo - width / 2 <= p <= hi + width / 2 for p in composite_levels(comp)))


def watch_confluence(watch, comp, atr):
    """confluent() for a watch; '' for FC watches, whose level IS a composite level."""
    return "" if watch["path"] == "FC" else confluent(watch["zone"], comp, atr)


def dpoc_position(symbol, candles, session_start, bin_size, levels):
    """ABOVE / INSIDE / BELOW: today's developing POC (from `candles`, the session's closed 1m
    candles up to the touch) against the prior value area; '' when it cannot be built."""
    if not candles or bin_size <= 0:
        return ""
    end = session_start + sessions.MS_DAY - 1
    prof = builder.profile_as_of(symbol, candles, session_start, end, candles[-1].close_time,
                                 bin_size)
    lv = levels_mod.compute(prof) if prof.volume else None
    if lv is None:
        return ""
    return "ABOVE" if lv.poc_price > levels.vah else "BELOW" if lv.poc_price < levels.val else "INSIDE"


def value_watch_targets(comp, side):
    """(T1, T2) of the composite fade path FC: T1 = cPOC, T2 = the opposite composite edge."""
    return comp.poc, (comp.vah if side == "BUY" else comp.val)

