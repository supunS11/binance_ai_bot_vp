"""S4-OFR: order-flow confirmed reversals at high-volume zones (the order-flow plan).

Zones are the previous session's VAH, VAL and POC, plus its HVN zones. A reversal is taken
only on the zone's FIRST visit this session; a second visit is ignored outright.

Signals are computed on 5-minute bars built from closed 1-minute candles:
  ABS   absorption: a high-volume bar at the zone with aggression against the reversal, where
        price fails to follow through against it
  CVD   CVD divergence: price makes a more extreme point while cumulative delta does not
  FLIP  delta flip: the follow-through bar's aggression supports the reversal, the bar before
        opposed it
  STACKED  footprint stacked imbalance - UNAVAILABLE: needs trade-level data by price
  RESTING  resting large orders - UNAVAILABLE: needs a live depth feed

Tiers follow the plan. A single signal never trades, except the plan's exception: clean
absorption with a strong immediate follow-through on a very high-quality zone.

Entry, stop and target follow the entry/TP/SL plan, with distances in 15m ATR. The entry is
a passive limit at the zone's near edge plus a buffer, or the signal candle's low when that is
higher. The target is the first level in the plan's priority ladder that clears OFR_TP_MIN_R.
"""
from dataclasses import dataclass

import config
from data import feed_reader
from data import klines as klines_mod
from profile import shape as shape_mod
from profile.va_hvn import touch_count
from setups.base import (Candidate, RejectReason, SetupState, reject,
                         target_candidates)

SETUP = "S4-OFR"
UNAVAILABLE = "UNAVAILABLE"
PLAN_ATR_MIN_BARS = 15


@dataclass
class Zone:
    kind: str
    low: float
    high: float

    @property
    def center(self):
        return (self.low + self.high) / 2.0


def _zones(levels, atr):
    tol = config.OFR_TOUCH_TOL_ATR * atr
    zones = [Zone("POC", levels.poc_price - tol, levels.poc_price + tol),
             Zone("VAH", levels.vah - tol, levels.vah + tol),
             Zone("VAL", levels.val - tol, levels.val + tol)]
    for node in levels.hvns:
        if node.is_poc:
            continue
        zones.append(Zone("HVN", node.low_price - tol, node.high_price + tol))
    return zones


def _delta_frac(bar):
    return (2.0 * bar.taker_buy_base - bar.volume) / bar.volume if bar.volume > 0 else 0.0


def _touches(bar, zone):
    return bar.low <= zone.high and bar.high >= zone.low


def _absorption(bars, zone, side):
    """Index of the latest bar at the zone showing absorption, or None."""
    recent = sorted(bar.volume for bar in bars[-21:-1])
    median = recent[len(recent) // 2] if recent else 0.0
    if median <= 0:
        return None
    frac = config.OFR_ABSORB_DELTA_FRAC
    for index in range(len(bars) - 4, len(bars) - 1):
        bar = bars[index]
        if not _touches(bar, zone) or bar.volume < config.OFR_ABSORB_VOLUME_MULT * median:
            continue
        delta = _delta_frac(bar)
        if side == "BUY" and delta <= -frac and bar.close >= bar.open:
            return index
        if side == "SELL" and delta >= frac and bar.close <= bar.open:
            return index
    return None


def _cvd_divergence(bars, side):
    window = bars[-config.OFR_CVD_LOOKBACK_BARS:]
    if len(window) < 4:
        return False
    cum, cvd = 0.0, []
    for bar in window:
        cum += 2.0 * bar.taker_buy_base - bar.volume
        cvd.append(cum)
    half = len(window) // 2
    first, second = range(half), range(half, len(window))
    if side == "BUY":
        a = min(first, key=lambda i: window[i].low)
        b = min(second, key=lambda i: window[i].low)
        return window[b].low < window[a].low and cvd[b] > cvd[a]
    a = max(first, key=lambda i: window[i].high)
    b = max(second, key=lambda i: window[i].high)
    return window[b].high > window[a].high and cvd[b] < cvd[a]


def _flip(bars, side):
    frac = config.OFR_FLIP_DELTA_FRAC
    last, prev = _delta_frac(bars[-1]), _delta_frac(bars[-2])
    if side == "BUY":
        return last >= frac and prev <= -frac
    return last <= -frac and prev >= frac


def _follow_through(bar, zone, side, atr, margin_atr):
    margin = margin_atr * atr
    supportive = _delta_frac(bar) >= 0 if side == "BUY" else _delta_frac(bar) <= 0
    if not supportive:
        return False
    if side == "BUY":
        return bar.close >= zone.high + margin
    return bar.close <= zone.low - margin


def _invalidated(bars, zone, side, atr):
    margin = config.OFR_FOLLOW_ATR * atr
    last_two = bars[-2:]
    if side == "BUY":
        beyond = all(bar.close < zone.low - margin for bar in last_two)
        selling = _delta_frac(bars[-1]) <= -config.OFR_FLIP_DELTA_FRAC
        return beyond and selling
    beyond = all(bar.close > zone.high + margin for bar in last_two)
    buying = _delta_frac(bars[-1]) >= config.OFR_FLIP_DELTA_FRAC
    return beyond and buying


def _recorded_flow(ctx, zone, side):
    """STACKED and RESTING from the feed recorder. True or False when the recorder covered the
    window, None when it did not - and None is UNAVAILABLE, never a quiet market."""
    if not config.OFR_USE_RECORDED_FLOW:
        return {}
    flow = {"STACKED": None, "RESTING": None}
    end = ctx.as_of
    start = end - config.OFR_FLOW_WINDOW_MINUTES * 60_000
    tick = float(getattr(ctx.spec, "tick_size", 0) or 0)
    levels = feed_reader.trade_levels(config.FEED_OUT_DIR, ctx.symbol, start, end)
    if levels is not None and tick > 0:
        flow["STACKED"] = feed_reader.stacked_imbalance(
            levels, side, tick, config.OFR_STACK_RATIO, config.OFR_STACK_MIN_LEVELS)
    samples = feed_reader.depth_samples(config.FEED_OUT_DIR, ctx.symbol, start, end)
    if samples is not None:
        flow["RESTING"] = feed_reader.resting_large_orders(
            samples, side, zone.low, zone.high, config.OFR_RESTING_SIZE_MULT,
            config.OFR_RESTING_PERSIST)
    return flow


def classify_tier(present, zone_kind, first_test, dev_supported, exceptional_follow):
    """Plan tiers. `present` is the set of signal names that fired (UNAVAILABLE never does)."""
    supporting = present & {"FLIP", "STACKED", "RESTING"}
    if {"ABS", "CVD"} <= present or {"ABS", "STACKED", "FLIP"} <= present \
            or {"CVD", "STACKED", "RESTING"} <= present:
        return "T1"
    if (({"ABS", "CVD"} & present) and supporting) \
            or ({"STACKED", "FLIP", "RESTING"} <= present and not ({"ABS", "CVD"} & present)):
        return "T2"
    if len(supporting) >= 2 and zone_kind in ("VAH", "VAL") and first_test and dev_supported:
        return "T3"
    if present == {"ABS"} and exceptional_follow and zone_kind in ("POC", "VAH", "VAL") \
            and first_test:
        return "X"
    return None


def detect(ctx):
    if not config.S4_OFR_ENABLED:
        return reject(RejectReason.SETUP_DISABLED, SETUP, ctx.symbol)
    if ctx.prior_levels is None or ctx.prior_profile is None:
        return reject(RejectReason.NO_PRIOR_PROFILE, SETUP, ctx.symbol)
    if ctx.atr <= 0:
        return reject(RejectReason.NO_DATA, SETUP, ctx.symbol, detail="atr<=0")

    closed = [c for c in ctx.session_candles if c.close_time <= ctx.as_of]
    bars = klines_mod.resample(closed, config.OFR_BAR_INTERVAL, "1m")
    if len(bars) < config.OFR_MIN_BARS:
        return reject(RejectReason.INSUFFICIENT_CANDLES, SETUP, ctx.symbol)
    plan_bars = klines_mod.resample(closed, config.OFR_PLAN_ATR_INTERVAL, "1m")
    if len(plan_bars) <= PLAN_ATR_MIN_BARS:
        return reject(RejectReason.INSUFFICIENT_CANDLES, SETUP, ctx.symbol,
                      detail="plan ATR needs more complete bars")
    atr15 = klines_mod.atr(plan_bars)
    if atr15 <= 0:
        return reject(RejectReason.NO_DATA, SETUP, ctx.symbol, detail="plan atr<=0")

    last = bars[-1]
    arrived = [z for z in _zones(ctx.prior_levels, ctx.atr)
               if any(_touches(bar, z) for bar in bars[-4:])]
    if not arrived:
        return reject(RejectReason.NO_ORDER_FLOW_ZONE, SETUP, ctx.symbol,
                      detail="no zone touched in the last three bars")

    rejected = []
    for zone in arrived:
        visits = touch_count(closed, zone.low, zone.high)
        first_test = visits == 1
        if config.OFR_VISIT_RULE == "first" and not first_test:
            rejected.append("NOT_FIRST_TEST")
            continue
        side = "BUY" if zone.center < last.close else "SELL"

        bias = shape_mod.day_bias(ctx.prior_shape)
        if config.BIAS_FILTER_ENABLED and (
                (bias == "BULL" and side == "SELL") or (bias == "BEAR" and side == "BUY")):
            rejected.append("BIAS_OPPOSED")
            continue

        if _invalidated(bars, zone, side, ctx.atr):
            rejected.append("INVALIDATED")
            continue

        absorb_index = _absorption(bars, zone, side)
        present = set()
        if absorb_index is not None:
            present.add("ABS")
        if _cvd_divergence(bars, side):
            present.add("CVD")
        if _flip(bars, side):
            present.add("FLIP")

        flow = _recorded_flow(ctx, zone, side)
        present |= {name for name, value in flow.items() if value}

        follow = _follow_through(last, zone, side, ctx.atr, config.OFR_FOLLOW_ATR)
        exceptional = _follow_through(last, zone, side, ctx.atr,
                                      config.OFR_EXCEPTIONAL_FOLLOW_ATR)
        if not follow:
            rejected.append("NO_FOLLOW_THROUGH")
            continue
        if not present:
            rejected.append("NO_ORDER_FLOW_SIGNAL")
            continue

        dev = ctx.dev_levels
        dev_supported = (dev is not None and
                         abs(dev.poc_price - zone.center) <= config.OFR_DEV_SUPPORT_ATR * ctx.atr)
        tier = classify_tier(present, zone.kind, first_test, dev_supported, exceptional)
        if tier is None:
            rejected.append("TIER_NOT_MET")
            continue

        result = _candidate(ctx, zone, side, bars, absorb_index, present, tier, atr15,
                            first_test, visits, flow)
        if result.is_rejection:
            rejected.append(result.reason.value)
            continue
        return result

    reason = (RejectReason.NOT_FIRST_TEST if "NOT_FIRST_TEST" in rejected
              else RejectReason.TIER_NOT_MET)
    return reject(reason, SETUP, ctx.symbol, detail=",".join(sorted(set(rejected))))


def _target_ladder(entry, side, risk, levels, naked_pocs, hvns, prior_extreme):
    """The plan's target priority list: the first level on the trade's side that clears
    OFR_TP_MIN_R. Returns ((kind, price) for TP1, (kind, price) for TP2 or (None, None))."""
    def clears(price):
        if price is None or price != price:
            return False
        ahead = price > entry if side == "BUY" else price < entry
        return ahead and abs(price - entry) / risk >= config.OFR_TP_MIN_R

    def nearest(prices):
        ok = sorted((p for p in prices if clears(p)), key=lambda p: abs(p - entry))
        return ok[0] if ok else None

    opposite_va = levels.vah if side == "BUY" else levels.val
    stages = [
        ("HVN", nearest([node.peak_price for node in hvns if not node.is_poc])),
        ("POC", levels.poc_price if clears(levels.poc_price) else None),
        ("VAH" if side == "BUY" else "VAL", opposite_va if clears(opposite_va) else None),
        ("nPOC", nearest(naked_pocs or [])),
        ("priorExtreme", prior_extreme if clears(prior_extreme) else None),
    ]
    picks = [(kind, price) for kind, price in stages if price is not None]
    fallback = entry + risk * config.OFR_TP_FALLBACK_R if side == "BUY" \
        else entry - risk * config.OFR_TP_FALLBACK_R
    picks.append(("fixed2R", fallback))

    tp1 = picks[0]
    beyond = [pick for pick in picks[1:]
              if (pick[1] > tp1[1] if side == "BUY" else pick[1] < tp1[1])]
    tp2 = beyond[0] if beyond else (None, None)
    return tp1, tp2


def _candidate(ctx, zone, side, bars, absorb_index, present, tier, atr15, first_test, visits,
               flow):
    last = bars[-1]
    cluster = bars[absorb_index] if absorb_index is not None else last
    entry_buffer = config.OFR_ENTRY_BUFFER_ATR * atr15
    stop_buffer = config.OFR_STOP_BUFFER_ATR * atr15

    if side == "BUY":
        entry = max(zone.low + entry_buffer, cluster.low)
        away = last.close - zone.high
    else:
        entry = min(zone.high - entry_buffer, cluster.high)
        away = zone.low - last.close
    if away > config.OFR_ENTRY_MAX_DISTANCE_ATR * atr15:
        return reject(RejectReason.ENTRY_TOO_FAR, SETUP, ctx.symbol, direction=side,
                      level_price=zone.center)
    if (side == "BUY" and entry >= last.close) or (side == "SELL" and entry <= last.close):
        return reject(RejectReason.ENTRY_NOT_PASSIVE, SETUP, ctx.symbol, direction=side,
                      level_price=zone.center)

    if side == "BUY":
        stop = min(zone.low, cluster.low) - stop_buffer
    else:
        stop = max(zone.high, cluster.high) + stop_buffer
    if (side == "BUY" and stop >= entry) or (side == "SELL" and stop <= entry):
        return reject(RejectReason.STOP_TOO_TIGHT, SETUP, ctx.symbol, direction=side)
    risk = abs(entry - stop)

    levels = ctx.prior_levels
    prior_extreme = ctx.prior_profile.high if side == "BUY" else ctx.prior_profile.low
    (tp1_kind, tp1), (tp2_kind, tp2) = _target_ladder(
        entry, side, risk, levels, ctx.naked_pocs, levels.hvns, prior_extreme)

    signals = {name: (name in present) for name in ("ABS", "CVD", "FLIP")}
    for name in ("STACKED", "RESTING"):
        value = flow.get(name)
        signals[name] = UNAVAILABLE if value is None else value
    signal_text = "+".join(sorted(present))

    return Candidate(
        setup=SETUP,
        symbol=ctx.symbol,
        direction=side,
        state=SetupState.TRIGGERED,
        level_price=zone.center,
        level_kind=zone.kind,
        entry_price=entry,
        stop_price=stop,
        target_price=tp1,
        session_id=ctx.session_id,
        as_of=ctx.as_of,
        atr=ctx.atr,
        profile_snapshot=ctx.profile_row(),
        attributes={
            "target_kind": tp1_kind,
            "tp2_price": tp2,
            "tp2_kind": tp2_kind,
            "atr15": atr15,
            "ofr_tier": tier,
            "ofr_signals": signal_text,
            "ofr_zone_kind": zone.kind,
            "ofr_first_test": 1 if first_test else 0,
            "ofr_visit_number": visits,
            "ofr_absorb_bars_before": (len(bars) - 1 - absorb_index) if absorb_index is not None
            else None,
            "ofr_signal_states": ";".join(f"{k}={v}" for k, v in sorted(signals.items())),
            "day_bias": shape_mod.day_bias(ctx.prior_shape),
            "dev_shape_label": ctx.dev_shape.label if ctx.dev_shape is not None else None,
            "target_candidates": target_candidates(
                entry, side, levels, naked_pocs=ctx.naked_pocs, hvns=levels.hvns,
                prior_extreme=prior_extreme),
        },
    )
