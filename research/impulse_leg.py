"""Impulse-leg LVN / POC pullback levels (workflow v2, path T2/T3) - shared with the
live S6-WF setup (setups/workflow.py), so live and research run the same rules. Design and justification: calibration/workflow_v2/design_plan_DRAFT.md
(Stage 2b).

DEFINITION (uptrend; downtrend is the mirror image)
  1. Swings: causal zig-zag on 15m CLOSES (trend_combo.zigzag_pivots), reversal SWING_ATR x
     daily ATR. Closes, not wicks, so a single spike cannot make a leg.
  2. Only the latest swing in the trend direction is used (no fallback to older legs).
  3. Leg end = the first close within END_ATR of the swing's extreme close (sideways trading at
     the top is a new balance, not part of the impulse). Leg start = the lowest close in the
     MAX_BARS bars before the end. It is an impulse when start -> extreme >= K_ATR x daily ATR
     (1.5 ATR within 24h is ~ the 90th percentile of daily moves on the DEV data).
  4. The leg's top must be within FRESH_DAYS sessions.
  5. ARMED (levels become usable) at the first close FINISH_ATR back from the extreme.
     CANCELLED once a close goes beyond the leg start (the move is fully undone).
  6. Profile: exact traded volume at price (validated footprints) over the leg's bars, ROWS
     equal rows from leg low to leg high, 3-row smoothing (shape_v2.rows_profile).
  7. POC = busiest row. LVN = a valley in the middle 80% of the rows whose volume is
     <= VALLEY x the busiest row on EACH side (a prominence test) AND <= LVN_MAX_OF_POC x the
     POC row (common practitioner floor); the deepest valley is kept.
  8. A level is used from arming until its first touch (zone +/- ZONE_ATR x daily ATR) and only
     on the pullback side of price.
Everything uses closed bars only; `legs_asof` sees nothing after `now_ms`.
"""
import numpy as np

import sessions
from research.shape_v2 import rows_profile
from research.trend_combo import zigzag_pivots

SWING_ATR = 1.0
K_ATR = 1.5
END_ATR = 0.25
FINISH_ATR = 0.25
MAX_BARS = 96
FRESH_DAYS = 5
ROWS = 30
VALLEY = 0.5
LVN_MAX_OF_POC = 0.4
ZONE_ATR = 0.10


class _Close:
    __slots__ = ("high", "low")

    def __init__(self, c):
        self.high = self.low = c


def find_leg(b15, atr, side, now_ms):
    """Latest impulse leg from CLOSED 15m bars (all close_time < now_ms). side 'BUY' (uptrend
    leg, pullback buys) or 'SELL'. Returns a dict or a status string."""
    bars = [b for b in b15 if b.close_time < now_ms]
    if atr <= 0 or len(bars) < 3:
        return "no_data"
    closes = [b.close for b in bars]
    sign = 1 if side == "BUY" else -1
    start_kind = "L" if side == "BUY" else "H"
    piv = zigzag_pivots([_Close(c) for c in closes], [atr] * len(closes), SWING_ATR)
    starts = [p for p in piv if p[3] == start_kind]
    if not starts:
        return "no_swing"
    s0 = starts[-1][1]
    nxt = [q for q in piv if q[1] > s0 and q[3] != start_kind]
    last = nxt[0][1] if nxt else len(closes) - 1
    if last <= s0:
        return "no_impulse"
    ext = max(range(s0 + 1, last + 1), key=lambda x: sign * closes[x])
    if bars[ext].close_time < now_ms - FRESH_DAYS * sessions.MS_DAY:
        return "stale"
    end = next(x for x in range(s0 + 1, ext + 1)
               if sign * closes[x] >= sign * closes[ext] - END_ATR * atr)
    start = min(range(max(s0, end - MAX_BARS), end), key=lambda x: sign * closes[x])
    if sign * (closes[ext] - closes[start]) < K_ATR * atr:
        return "no_impulse"
    after = closes[ext + 1:]
    if any(sign * (c - closes[start]) < 0 for c in after):
        return "cancelled"
    armed = next((ext + 1 + j for j, c in enumerate(after)
                  if sign * (closes[ext] - c) >= FINISH_ATR * atr), None)
    if armed is None:
        return "not_finished"
    return {"start": start, "end": end, "ext": ext, "armed": armed,
            "t0": bars[start].open_time, "t1": bars[end].close_time + 1,
            "armed_ms": bars[armed].close_time + 1,
            "low": min(b.low for b in bars[start:end + 1]),
            "high": max(b.high for b in bars[start:end + 1])}


def find_lvn(smooth):
    """(row, valley/side ratio) of the deepest qualifying valley, or None."""
    smooth = np.asarray(smooth, dtype=float)
    peak = smooth.max()
    best = None
    lo, hi = int(0.1 * len(smooth)), int(0.9 * len(smooth)) - 1
    for i in range(max(1, lo), min(len(smooth) - 1, hi + 1)):
        if not (smooth[i] <= smooth[i - 1] and smooth[i] <= smooth[i + 1]):
            continue
        side = min(smooth[:i].max(), smooth[i + 1:].max())
        if side <= 0 or smooth[i] > LVN_MAX_OF_POC * peak:
            continue
        ratio = smooth[i] / side
        if ratio <= VALLEY and (best is None or ratio < best[1]):
            best = (i, ratio)
    return best


def leg_levels(leg, price_bins, atr):
    """POC / LVN prices and zones from {price: volume} traded during the leg."""
    smooth = rows_profile(price_bins, leg["low"], leg["high"], ROWS)
    if smooth is None:
        return None
    h = (leg["high"] - leg["low"]) / ROWS
    out = {"smooth": smooth, "poc": leg["low"] + (int(np.argmax(smooth)) + 0.5) * h, "lvn": None}
    lv = find_lvn(smooth)
    if lv:
        out["lvn"] = leg["low"] + (lv[0] + 0.5) * h
        out["lvn_ratio"] = float(lv[1])
    for k in ("poc", "lvn"):
        if out[k] is not None:
            out[k + "_zone"] = (out[k] - ZONE_ATR * atr, out[k] + ZONE_ATR * atr)
    return out


def footprint_bins(load_day, symbol, t0, t1):
    """{price: volume} from per-day footprint files over [t0, t1); None if a day is missing.
    load_day(symbol, session_id) -> dict(minute,row,buy,sell,row_size) or None."""
    bins = {}
    s = sessions.session_start_ms(t0)
    while s < t1:
        z = load_day(symbol, sessions.session_id(s))
        if z is None:
            return None
        m0, m1 = (t0 - s) // 60_000, (t1 - s) // 60_000
        sel = (z["minute"] >= m0) & (z["minute"] < m1)
        rs = float(np.ravel(z["row_size"])[0])
        for r, v in zip(z["row"][sel], (z["buy"] + z["sell"])[sel]):
            p = (int(r) + 0.5) * rs
            bins[p] = bins.get(p, 0.0) + float(v)
        s += sessions.MS_DAY
    return bins
