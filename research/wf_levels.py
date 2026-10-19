"""Workflow v2 stage 2: the session's watches (where and in which direction to look for a
trade) - shared with the live S6-WF setup (setups/workflow.py), so live and research run the same rules. Design:
calibration/workflow_v2/design_plan_DRAFT.md (Stage 2 / 2b).

Prior value = the prior UTC day's profile (the live frozen bundle: VAH, VAL, POC). Zones are
the level +/- ZONE_ATR x daily ATR (the live OFR_TOUCH_TOL_ATR). BUY orientation below.

  acceptance  two consecutive 30m closes beyond a value edge (Dalton: acceptance = time; the
              standard 2 x 30-minute definition, also used by the 80% rule).
  T1  TREND_UP (not a veto day): after acceptance ABOVE the prior VAH, the first return to the
      VAH zone from above -> BUY. TREND_DOWN mirror at the VAL.
  X   BALANCE: the same after acceptance above VAH (BUY) / below VAL (SELL) - a breakout
      switches the session to trend-continuation rules in that direction.
  F   BALANCE: the first touch of the VAL zone from above (BUY) / VAH from below (SELL), only if
      no acceptance outside value happened before that touch.
  T2 / T3  TREND (not a veto day): LVN / POC of the latest impulse leg (research/impulse_leg),
      re-checked at every closed 15m bar; a leg's levels are usable from its arming until
      their first touch, on the pullback side, and are dropped when a newer leg arms.
  R   BALANCE: the session opens outside prior value and then makes two consecutive 30m closes
      inside it -> signal at that close, direction toward the opposite edge (80% rule).
      Matched control R_CTRL: opened outside, at least one 30m close inside, never two in a
      row -> 'signal' at the first inside close.
A touch = a 1m bar trading into the zone after the previous 1m close was on the approach side
(the live zone-watch semantics). Only closed bars are read.
"""
from data import klines as klines_mod
from research import impulse_leg as il
from research import wf_state as ws

ZONE_ATR = 0.10


def zone(level, atr):
    return (level - ZONE_ATR * atr, level + ZONE_ATR * atr)


def first_touch(candles, z, side, from_ms):
    """Index of the first 1m bar opening at/after from_ms that trades into zone z, coming
    from the approach side (BUY: previous close above the zone), or None."""
    lo, hi = z
    for i in range(1, len(candles)):
        c = candles[i]
        if c.open_time < from_ms:
            continue
        prev = candles[i - 1].close
        if side == "BUY" and prev > hi and c.low <= hi:
            return i
        if side == "SELL" and prev < lo and c.high >= lo:
            return i
    return None


def acceptance_times(bars30, vah, val):
    """{'ABOVE': ms, 'BELOW': ms}: close time (+1) of the second of the first two consecutive
    30m closes above VAH / below VAL."""
    out = {}
    for k in range(1, len(bars30)):
        a, b = bars30[k - 1], bars30[k]
        if "ABOVE" not in out and a.close > vah and b.close > vah:
            out["ABOVE"] = b.close_time + 1
        if "BELOW" not in out and a.close < val and b.close < val:
            out["BELOW"] = b.close_time + 1
    return out


def reclaim_time(bars30, vah, val, after_ms):
    """Close time (+1) of the second of the first two consecutive 30m closes back INSIDE
    [val, vah], both closing after after_ms (an acceptance time), or None. Dalton: acceptance
    back inside value undoes the breakout, so the old edge is no longer a with-trend level
    (added 2026-10-18 after the BASEDUSDT T1 loss: the VAL was 'retested' 1% below it, two
    hours after price had closed back above it for four 30m bars)."""
    if after_ms is None:
        return None
    for k in range(1, len(bars30)):
        a, b = bars30[k - 1], bars30[k]
        if a.close_time + 1 <= after_ms:
            continue
        if val <= a.close <= vah and val <= b.close <= vah:
            return b.close_time + 1
    return None


def eighty_rule(candles, bars30, vah, val):
    """(kind, side, signal_ms, session_extreme) for R or R_CTRL, or None."""
    if not candles or not bars30:
        return None
    o = candles[0].open
    if val <= o <= vah:
        return None
    side = "SELL" if o > vah else "BUY"          # opened above -> rotate down to VAL
    inside = [val <= b.close <= vah for b in bars30]
    first_inside = None
    for k, ins in enumerate(inside):
        if ins and first_inside is None:
            first_inside = k
        if k >= 1 and ins and inside[k - 1]:
            t = bars30[k].close_time + 1
            ext = _extreme(candles, t, side)
            return ("R", side, t, ext)
    if first_inside is not None:
        t = bars30[first_inside].close_time + 1
        return ("R_CTRL", side, t, _extreme(candles, t, side))
    return None


def _extreme(candles, until_ms, side):
    seen = [c for c in candles if c.close_time < until_ms]
    return max(c.high for c in seen) if side == "SELL" else min(c.low for c in seen)


def value_watches(state, veto, levels, atr, candles, bars30):
    """T1 / X / F watches for one session. Each: dict(path, side, kind, level, zone, i)."""
    vah, val = levels.vah, levels.val
    acc = acceptance_times(bars30, vah, val)
    out = []

    def add(path, side, kind, level, from_ms):
        # T1 / X: the breakout must still stand at the touch (reclaim_time).
        z = zone(level, atr)
        i = first_touch(candles, z, side, from_ms)
        until = reclaim_time(bars30, vah, val, from_ms)
        if i is not None and (until is None or candles[i].open_time < until):
            out.append({"path": path, "side": side, "kind": kind, "level": level, "zone": z, "i": i,
                        "from_ms": from_ms, "until_ms": until})

    if state == ws.TREND_UP and not veto and "ABOVE" in acc:
        add("T1", "BUY", "VAH", vah, acc["ABOVE"])
    if state == ws.TREND_DOWN and not veto and "BELOW" in acc:
        add("T1", "SELL", "VAL", val, acc["BELOW"])
    if state == ws.BALANCE:
        if "ABOVE" in acc:
            add("X", "BUY", "VAH", vah, acc["ABOVE"])
        if "BELOW" in acc:
            add("X", "SELL", "VAL", val, acc["BELOW"])
        start = candles[0].open_time if candles else 0
        for side, kind, level in (("BUY", "VAL", val), ("SELL", "VAH", vah)):
            z = zone(level, atr)
            i = first_touch(candles, z, side, start)
            if i is None:
                continue
            if any(t <= candles[i].open_time for t in acc.values()):
                continue                              # value already left: not a balance fade
            out.append({"path": "F", "side": side, "kind": kind, "level": level, "zone": z, "i": i,
                        "from_ms": start, "until_ms": min(acc.values(), default=None)})
    return out


def composite_watches(state, comp, atr, candles, bars30, sides=("BUY", "SELL")):
    """FC watches (BALANCE): the first touch of the composite VAL from above (BUY) / composite
    VAH from below (SELL), only before any acceptance (2 x 30m closes) outside the composite
    value area - the multi-day balance's own edges (mtf_profiles_recommendation 3.4)."""
    if comp is None or not candles:
        return []
    acc = acceptance_times(bars30, comp.vah, comp.val)
    start = candles[0].open_time
    out = []
    for side, kind, level in (("BUY", "CVAL", comp.val), ("SELL", "CVAH", comp.vah)):
        if side not in sides:
            continue
        z = zone(level, atr)
        i = first_touch(candles, z, side, start)
        if i is None or any(t <= candles[i].open_time for t in acc.values()):
            continue
        out.append({"path": "FC", "side": side, "kind": kind, "level": level, "zone": z, "i": i,
                    "from_ms": start, "until_ms": min(acc.values(), default=None)})
    return out


def drop_duplicate_fc(watches):
    """An FC watch whose zone overlaps an F watch of the same side is the same touch of the
    same place: keep the F watch only (it is the pre-registered one)."""
    f = [w for w in watches if w["path"] == "F"]
    return [w for w in watches if w["path"] != "FC" or not any(
        g["side"] == w["side"] and g["zone"][0] <= w["zone"][1] and w["zone"][0] <= g["zone"][1]
        for g in f)]


def breakout_leg_watches(state, levels, atr, b15_hist, candles, bars30, bins_for):
    """X-path impulse-leg levels (BALANCE): after acceptance above the prior VAH (below the VAL)
    the day follows trend rules in that direction (playbook Fig. 3: the old VAH AND the
    impulse leg's LVN become support). The latest up-leg's (down-leg's) LVN / POC, armed by
    then and not touched before the acceptance, as X watches of kind LVN / POC."""
    return breakout_leg_touches(
        breakout_leg_armed(state, levels, atr, b15_hist, candles, bars30, bins_for), candles)


def breakout_leg_armed(state, levels, atr, b15_hist, candles, bars30, bins_for):
    """[(side, leg_armed list)] for breakout_leg_watches, armed no earlier than the acceptance."""
    if state != ws.BALANCE or not candles:
        return []
    acc = acceptance_times(bars30, levels.vah, levels.val)
    s_end = candles[-1].close_time + 1
    out = []
    for key, side, pseudo in (("ABOVE", "BUY", ws.TREND_UP), ("BELOW", "SELL", ws.TREND_DOWN)):
        t = acc.get(key)
        if t is None or t >= s_end:
            continue
        armed, _ = leg_armed(pseudo, atr, b15_hist, t, s_end, bins_for)
        # A reclaim of value undoes the breakout: its legs' levels end there too.
        r = reclaim_time(bars30, levels.vah, levels.val, t)
        out.append((side, [{**w, "armed_ms": max(w["armed_ms"], t),
                            "superseded_ms": min((x for x in (w["superseded_ms"], r) if x is not None),
                                                 default=None)} for w in armed]))
    return out


def breakout_leg_touches(armed_by_side, candles):
    return [{**w, "path": "X"} for side, armed in armed_by_side
            for w in leg_touches(armed, candles, side)]


def leg_watches(state, veto, atr, b15_hist, candles, bins_for):
    """T2 (LVN) / T3 (POC) watches. b15_hist: 15m bars of the previous sessions AND this one
    (only bars closed at each check are read). bins_for(t0, t1) -> {price: volume} or None."""
    if veto or state not in (ws.TREND_UP, ws.TREND_DOWN) or not candles:
        return [], {}
    out, stats = leg_armed(state, atr, b15_hist, candles[0].open_time,
                           candles[-1].close_time + 1, bins_for)
    return leg_touches(out, candles, "BUY" if state == ws.TREND_UP else "SELL"), stats


def leg_touches(armed, candles, side):
    """The armed leg levels (leg_armed) that were touched in `candles`, as watches."""
    if not candles:
        return []
    s_start = candles[0].open_time
    final = []
    for w in armed:
        i = first_touch(candles, w["zone"], side, max(w["armed_ms"], s_start))
        if i is None:
            continue
        if w["superseded_ms"] is not None and candles[i].open_time >= w["superseded_ms"]:
            continue
        final.append({k: w[k] for k in ("path", "side", "kind", "level", "zone")}
                     | {"i": i, "from_ms": max(w["armed_ms"], s_start), "until_ms": w["superseded_ms"]})
    return final


def leg_armed(state, atr, b15_hist, s_start, s_end, bins_for):
    """Every leg LVN / POC level armed during [s_start, s_end) or carried into it, with its
    armed_ms and superseded_ms (None while it is the latest leg's). Checks run at s_start and
    at every 15m close inside the window, reading only bars closed by then."""
    side = "BUY" if state == ws.TREND_UP else "SELL"
    sign = 1 if side == "BUY" else -1
    checks = [s_start] + [b.close_time + 1 for b in b15_hist if s_start < b.close_time + 1 <= s_end]
    out, stats, seen = [], {"legs": 0, "no_profile": 0, "wrong_side": 0, "touched_before": 0}, set()
    active = []                                   # watches of the current leg not yet touched
    for now in checks:
        leg = il.find_leg(b15_hist, atr, side, now)
        if not isinstance(leg, dict):
            continue
        key = (leg["t0"], leg["ext"])
        if key in seen:
            continue
        seen.add(key)
        stats["legs"] += 1
        for w in out:                             # a newer leg supersedes every older one
            if w["superseded_ms"] is None:
                w["superseded_ms"] = now
        active = []
        bins = bins_for(leg["t0"], leg["t1"])
        lv = il.leg_levels(leg, bins, atr) if bins else None
        if lv is None:
            stats["no_profile"] += 1
            continue
        arm_close = [b for b in b15_hist if b.close_time + 1 <= leg["armed_ms"]][-1].close
        for path, name in (("T2", "lvn"), ("T3", "poc")):
            if lv.get(name) is None:
                continue
            z = lv[name + "_zone"]
            if sign * (arm_close - (z[1] if side == "BUY" else z[0])) <= 0:
                stats["wrong_side"] += 1
                continue
            active.append({"path": path, "side": side, "kind": name.upper(), "level": lv[name],
                           "zone": z, "armed_ms": leg["armed_ms"], "superseded_ms": None})
        # levels armed before the session: drop them if already touched before it starts
        if leg["armed_ms"] < s_start:
            keep = []
            for w in active:
                if _touched_between(b15_hist, w["zone"], side, leg["armed_ms"], s_start):
                    stats["touched_before"] += 1
                else:
                    keep.append(w)
            active = keep
        out.extend(active)
    return out, stats


def _touched_between(b15, z, side, t0, t1):
    lo, hi = z
    for b in b15:
        if b.open_time >= t0 and b.close_time < t1:
            if (side == "BUY" and b.low <= hi) or (side == "SELL" and b.high >= lo):
                return True
    return False


def bars30(candles):
    return klines_mod.resample(candles, "30m", "1m")
