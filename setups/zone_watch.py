"""In-zone watch for S4-OFR (ZONE_WATCH_ENABLED).

Redesigned around how a manual order-flow trader actually reads a level, rather than
around counting how many named signals agree. Two conditions are HARD GATES - both must
hold, in order, for a trade to exist. Everything else is a MODIFIER, recorded for later
sizing or target work but never itself a reason to enter or refuse:

  gate 1  FIRST-TEST ABSORPTION. Does the zone hold on its first test: a high-volume bar
          there, aggression against the reversal, no follow-through. This is the one
          thing a trader watches for at the level itself.
  gate 2  STRUCTURAL REVERSAL. A close that breaks the high (BUY) or low (SELL) of the
          whole base built since absorption appeared - not merely past one bar's high.
          The base keeps extending every minute the watch holds, so the level this must
          clear only ever gets more demanding, never less.

  modifier  CVD divergence on the leg INTO the zone (approach_leg below), read once when
            absorption appears, since it is a fixed property of how price arrived.
  modifier  STACKED footprint imbalance overlapping this zone, and RESTING large orders
            defending it, both read over the whole base once gate 2 fires - "repeated
            defense", not a single instant. UNAVAILABLE (no recorder coverage) never
            counts as present.
  modifier  LEVEL SIGNIFICANCE: the zone's distance from the separate weekly composite's
            POC, in ATR (weekly_poc_distance_atr below) - every zone here is already a
            prior SESSION level (see orderflow_reversal's module docstring); this asks
            whether it is ALSO close to a level the auction has agreed on across more
            than one session, rather than a one-day print. Recorded, not gated, same as
            the other modifiers above, pending validation against real outcomes.

From the first touch of a zone, every closed 1-minute candle is checked until one of two
things happens: a close beyond the zone on the side the trade is against (the watch ends
with no trade), or both gates pass (entry at market). A watch has no time limit beyond
that, or the session boundary.
"""
import json
import logging
from dataclasses import dataclass, field

import config
from data import feed_reader
from data import klines as klines_mod
from profile import bias as bias_mod
from profile.va_hvn import touch_count
from setups import orderflow_reversal as ofr
from setups.base import (MARKET_ENTRY, Candidate, RejectReason, SetupState, reject,
                         target_candidates, vwap_zscore, weekly_and_vwap_both_oppose,
                         weekly_poc_distance_atr)

log = logging.getLogger(__name__)

WATCHING = "WATCHING"
ABSORBED = "ABSORBED"

END_WRONG_WAY = "WRONG_WAY_CLOSE"
END_ENTERED = "ENTERED"
END_STOP_TOO_TIGHT = "STOP_TOO_TIGHT"


@dataclass
class _Watch:
    zone: ofr.Zone
    index: int
    side: str
    visits: int
    started_ms: int
    state: str = WATCHING
    absorb_index: int = None
    cluster_price: float = 0.0
    trigger: float = 0.0
    confirmed_ms: int = None
    approach_cvd: bool = False
    approach_persistence: int = 0
    too_far_logged: bool = False


@dataclass
class _SymbolState:
    session_id: str
    seen_ms: int = None
    active: dict = field(default_factory=dict)
    done: set = field(default_factory=set)


class ZoneWatch:
    def __init__(self, store=None):
        self._symbols = {}
        self._store = store

    def restore(self):
        """Reload persisted watches. Stale sessions are discarded by observe(), not here."""
        if self._store is None:
            return
        for symbol, payload in self._store.load_zone_watches().items():
            self._symbols[symbol] = _state_from_json(payload)
        if self._symbols:
            log.info("restored zone watches for %d symbol(s)", len(self._symbols))

    def observe(self, ctx):
        """Advance every watch on the closed minutes since the last call.

        Returns (candidate, rejections). At most one candidate per call, and only from a
        minute the watch has not already seen.
        """
        if ctx.prior_levels is None or ctx.prior_profile is None or ctx.atr <= 0:
            return None, []
        closed = [c for c in ctx.session_candles if c.close_time <= ctx.as_of]
        if not closed:
            return None, []

        state = self._symbols.get(ctx.symbol)
        if state is None or state.session_id != ctx.session_id:
            state = _SymbolState(session_id=ctx.session_id)
            self._symbols[ctx.symbol] = state

        if state.seen_ms is None:
            fresh = [len(closed) - 1]
        else:
            fresh = [i for i, c in enumerate(closed) if c.close_time > state.seen_ms]
        state.seen_ms = closed[-1].close_time

        zones = ofr._zones(ctx.prior_levels, ctx.atr)
        rejections = []
        entry = None
        for index in fresh:
            candidate = self._advance(state, closed[:index + 1], zones, ctx, rejections)
            if candidate is not None and entry is None:
                entry = candidate
        self._save(ctx.symbol, state)
        return entry, rejections

    def _save(self, symbol, state):
        """Persist before any entry is sent, so a restart cannot re-enter a taken zone."""
        if self._store is not None:
            self._store.save_zone_watch(symbol, _state_to_json(state))

    def _advance(self, state, closed, zones, ctx, rejections):
        last = closed[-1]
        entry = None
        for index, zone in enumerate(zones):
            watch = state.active.get(index)
            if watch is not None:
                candidate = self._step(state, watch, closed, ctx, rejections)
                if candidate is not None and entry is None:
                    entry = candidate
                continue
            if index in state.done or not ofr._touches(last, zone):
                continue
            self._start(state, index, zone, closed, ctx)
        return entry

    def _start(self, state, index, zone, closed, ctx):
        visits = touch_count(closed, zone.low, zone.high)
        if config.OFR_VISIT_RULE == "first" and visits != 1:
            return
        if len(closed) < 2:
            return
        previous = closed[-2].close
        if previous > zone.high:
            side = "BUY"
        elif previous < zone.low:
            side = "SELL"
        else:
            return
        if config.BIAS_FILTER_ENABLED:
            label, _strength = bias_mod.session_bias(ctx)
            if (label == "BULL" and side == "SELL") or (label == "BEAR" and side == "BUY"):
                return
        # RECORDED ONLY, never a gate - see orderflow_reversal.approach_persistence.
        # The direction call just above reads a single bar's close; this measures how
        # settled that read actually was, for later investigation, not to refuse here.
        persistence = ofr.approach_persistence(closed, zone, side)
        state.active[index] = _Watch(zone=zone, index=index, side=side, visits=visits,
                                     started_ms=closed[-1].open_time,
                                     approach_persistence=persistence)
        log.info("%s zone watch started: %s %s %s", ctx.symbol, zone.kind, side,
                 round(zone.center, 8))

    def _step(self, state, watch, closed, ctx, rejections):
        last = closed[-1]
        zone, side = watch.zone, watch.side
        if (side == "BUY" and last.close < zone.low) or \
                (side == "SELL" and last.close > zone.high):
            self._end(state, watch, END_WRONG_WAY, ctx)
            return None

        bars = klines_mod.resample(closed, config.OFR_BAR_INTERVAL, "1m")
        if len(bars) < config.OFR_MIN_BARS:
            return None

        if watch.absorb_index is None:
            self._gate1(watch, bars)
            if watch.absorb_index is None:
                return None

        # GATE 2. The base keeps extending every minute it holds, so this only ever gets
        # more demanding to clear, never less - there is no stale trigger to chase.
        watch.trigger = ofr.structure_break(bars, zone, side, watch.absorb_index)
        moved = last.close > watch.trigger if side == "BUY" else last.close < watch.trigger
        if not moved:
            return None
        return self._enter(state, watch, closed, ctx, rejections)

    def _gate1(self, watch, bars):
        """First-test absorption: does the zone hold? The approach leg into the zone is
        read here too, once - a fixed property of how price arrived, not something later
        bars at the zone can change."""
        zone, side = watch.zone, watch.side
        absorb = ofr._absorption(bars, zone, side)
        if absorb is None:
            return
        watch.absorb_index = absorb
        watch.state = ABSORBED
        bar = bars[absorb]
        watch.cluster_price = bar.low if side == "BUY" else bar.high
        watch.confirmed_ms = bar.close_time
        leg = ofr.approach_leg(bars[:absorb + 1], zone, config.OFR_APPROACH_MAX_BARS)
        watch.approach_cvd = len(leg) >= 4 and ofr._cvd_divergence(leg, side)

    def _enter(self, state, watch, closed, ctx, rejections):
        zone, side = watch.zone, watch.side
        last = closed[-1]

        plan_bars = klines_mod.resample(closed, config.OFR_PLAN_ATR_INTERVAL, "1m")
        if len(plan_bars) <= ofr.PLAN_ATR_MIN_BARS:
            return None
        atr15 = klines_mod.atr(plan_bars)
        if atr15 <= 0:
            return None

        entry = last.close
        away = entry - zone.high if side == "BUY" else zone.low - entry
        if away > config.OFR_ENTRY_MAX_DISTANCE_ATR * atr15:
            if not watch.too_far_logged:
                watch.too_far_logged = True
                rejections.append(reject(RejectReason.ENTRY_TOO_FAR, ofr.SETUP, ctx.symbol,
                                         direction=side, level_price=zone.center))
            return None

        buffer = config.OFR_STOP_BUFFER_ATR * atr15
        if side == "BUY":
            stop = min(zone.low, watch.cluster_price) - buffer
        else:
            stop = max(zone.high, watch.cluster_price) + buffer
        if (side == "BUY" and stop >= entry) or (side == "SELL" and stop <= entry):
            rejections.append(reject(RejectReason.STOP_TOO_TIGHT, ofr.SETUP, ctx.symbol,
                                     direction=side, level_price=zone.center))
            self._end(state, watch, END_STOP_TOO_TIGHT, ctx)
            return None

        stacked, resting = _defense_modifiers(ctx, zone, side, watch.confirmed_ms, ctx.as_of)
        modifiers = [name for name, value in
                    (("CVD", watch.approach_cvd), ("STACKED", stacked), ("RESTING", resting))
                    if value is True]
        signal_states = {
            "CVD": 1 if watch.approach_cvd else 0,
            "STACKED": ofr.UNAVAILABLE if stacked is None else int(stacked),
            "RESTING": ofr.UNAVAILABLE if resting is None else int(resting),
        }

        levels = ctx.prior_levels
        prior_extreme = ctx.prior_profile.high if side == "BUY" else ctx.prior_profile.low
        (tp1_kind, tp1), (tp2_kind, tp2) = ofr._target_ladder(
            entry, side, abs(entry - stop), levels, levels.hvns, prior_extreme)

        # LEVEL SIGNIFICANCE, recorded only (same "measure before gate" discipline as
        # CVD/STACKED/RESTING above). zone.center is always a PRIOR SESSION level
        # already (see orderflow_reversal's module docstring) - this asks whether
        # that level is ALSO close to the separate weekly composite's POC, i.e.
        # tested across more than one session rather than a one-day print. Reuses
        # the same helper and attribute name S1-POC/S2-VAR already record this
        # under, so research/replay.py needs no change to start seeing it for
        # S4-OFR too.
        weekly_poc_distance = weekly_poc_distance_atr(ctx.weekly_levels, zone.center, ctx.atr)

        # Same discipline, today's own session instead of the weekly composite -
        # mean-reversion read (below today's VWAP) rather than the trend read
        # above. Reuses S1-POC/S2-VAR's existing helper/attribute name.
        dev_vwap_z = vwap_zscore(ctx.dev_levels, entry)

        # A stronger, separate veto than the composite below - see
        # weekly_and_vwap_both_oppose's own docstring for the validation. Checked
        # here (not only folded into session_bias) because the full composite does
        # not catch this population on its own: the other four, older votes can
        # outvote these two even when they agree with each other.
        if config.DOUBLE_VOTE_VETO_ENABLED and weekly_and_vwap_both_oppose(
                weekly_poc_distance, dev_vwap_z, side):
            rejections.append(reject(RejectReason.WEEKLY_VWAP_OPPOSED, ofr.SETUP,
                                     ctx.symbol, direction=side, level_price=zone.center))
            return None

        bias_label, bias_strength = bias_mod.session_bias(ctx)

        # Re-check here, not only at watch-start: the base can run long enough for the
        # composite read to drift to clearly opposed by the time gate 2 actually fires.
        # The watch itself is NOT ended - gate 2 keeps re-testing on later bars, same as
        # any other bar where it fails to move, so a bias that clears later can still
        # trade the same break.
        if config.BIAS_FILTER_ENABLED and (
                (bias_label == "BULL" and side == "SELL") or
                (bias_label == "BEAR" and side == "BUY")):
            rejections.append(reject(RejectReason.BIAS_OPPOSED, ofr.SETUP, ctx.symbol,
                                     direction=side, level_price=zone.center))
            return None

        self._end(state, watch, END_ENTERED, ctx)
        return Candidate(
            setup=ofr.SETUP,
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
                "entry_mode": MARKET_ENTRY,
                "target_kind": tp1_kind,
                "tp2_price": tp2,
                "tp2_kind": tp2_kind,
                "atr15": atr15,
                "ofr_tier": None,
                "ofr_signals": "+".join(modifiers),
                "ofr_signal_states": ";".join(f"{k}={v}" for k, v in sorted(signal_states.items())),
                "ofr_conviction": len(modifiers),
                "ofr_zone_kind": zone.kind,
                "ofr_first_test": 1 if watch.visits == 1 else 0,
                "ofr_visit_number": watch.visits,
                "ofr_absorb_bars_before": None,
                "approach_persistence": watch.approach_persistence,
                "watch_started_ms": watch.started_ms,
                "watch_confirmed_ms": watch.confirmed_ms,
                "day_bias": bias_label,
                "bias_strength": bias_strength,
                "dev_shape_label": ctx.dev_shape.label if ctx.dev_shape is not None else None,
                "weekly_poc_distance_atr": weekly_poc_distance,
                # Deliberately NOT "vwap_zscore_at_level" - S1-POC/S2-VAR's existing
                # field under that name reads the PRIOR (frozen, static) session's
                # VWAP, a different measurement from this one (today's own live,
                # developing VWAP). Reusing the name would silently conflate two
                # different things under one column in any cross-setup analysis.
                "dev_vwap_zscore_at_entry": dev_vwap_z,
                "target_candidates": target_candidates(
                    entry, side, levels, naked_pocs=ctx.naked_pocs, hvns=levels.hvns,
                    prior_extreme=prior_extreme),
            },
        )

    @staticmethod
    def _end(state, watch, reason, ctx):
        state.active.pop(watch.index, None)
        if reason in (END_ENTERED, END_STOP_TOO_TIGHT):
            state.done.add(watch.index)
        log.info("%s zone watch ended: %s %s (%s)", ctx.symbol, watch.zone.kind,
                 watch.side, reason)


def _defense_modifiers(ctx, zone, side, start_ms, end_ms):
    """STACKED and RESTING over the whole base, from the absorption bar to now - repeated
    defense of this zone, not a single instant. (True, False) when the recorder covered
    the window, None for whichever one it did not; None never counts as present."""
    if not config.OFR_USE_RECORDED_FLOW:
        return None, None
    tick = float(getattr(ctx.spec, "tick_size", 0) or 0)

    stacked = None
    levels = feed_reader.trade_levels(config.FEED_OUT_DIR, ctx.symbol, start_ms, end_ms)
    if levels is not None and tick > 0:
        stacked = feed_reader.stacked_imbalance(
            levels, side, tick, config.OFR_STACK_RATIO, config.OFR_STACK_MIN_LEVELS,
            zone.low, zone.high)

    resting = None
    samples = feed_reader.depth_samples(config.FEED_OUT_DIR, ctx.symbol, start_ms, end_ms)
    if samples is not None:
        resting = feed_reader.resting_large_orders(
            samples, side, zone.low, zone.high, config.OFR_RESTING_SIZE_MULT,
            config.OFR_RESTING_PERSIST)

    return stacked, resting


def _state_to_json(state):
    return json.dumps({
        "session_id": state.session_id,
        "seen_ms": state.seen_ms,
        "done": sorted(state.done),
        "active": [
            {
                "index": watch.index,
                "zone": [watch.zone.kind, watch.zone.low, watch.zone.high],
                "side": watch.side,
                "visits": watch.visits,
                "started_ms": watch.started_ms,
                "state": watch.state,
                "absorb_index": watch.absorb_index,
                "cluster_price": watch.cluster_price,
                "trigger": watch.trigger,
                "confirmed_ms": watch.confirmed_ms,
                "approach_cvd": watch.approach_cvd,
                "approach_persistence": watch.approach_persistence,
                "too_far_logged": watch.too_far_logged,
            }
            for watch in state.active.values()
        ],
    })


def _state_from_json(payload):
    data = json.loads(payload)
    state = _SymbolState(session_id=data["session_id"], seen_ms=data["seen_ms"],
                         done=set(data["done"]))
    for row in data["active"]:
        kind, low, high = row["zone"]
        watch = _Watch(zone=ofr.Zone(kind, low, high), index=row["index"], side=row["side"],
                       visits=row["visits"], started_ms=row["started_ms"],
                       state=row["state"], absorb_index=row["absorb_index"],
                       cluster_price=row["cluster_price"], trigger=row["trigger"],
                       confirmed_ms=row["confirmed_ms"], approach_cvd=row["approach_cvd"],
                       # .get(), not row[...]: a watch persisted by a process running
                       # before this field existed must still restore cleanly.
                       approach_persistence=row.get("approach_persistence", 0),
                       too_far_logged=row["too_far_logged"])
        state.active[watch.index] = watch
    return state
