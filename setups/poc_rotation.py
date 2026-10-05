"""S1-POC: fade a return to the prior session's point of control.

THE SETUP, as described in the source material: the previous session ended with
price outside the value area; mark the POC; wait for price to travel to it; require
price action rejecting the level; enter against the approach with a stop just
beyond the POC.

WHAT AUCTION THEORY SAYS ABOUT IT, stated here because this detector exists partly
to be measured AGAINST its sibling. The POC is the price of maximum ACCEPTANCE -
where buyers and sellers agreed most. The behaviour that follows from that
definition is ROTATION: price arrives and churns, because willing volume exists on
both sides. A sharp directional rejection is the behaviour of a LOW-volume level,
where one side is absent and there is nothing to transact against.

So "price returns to the POC" is well supported - the POC is a magnet. "And then
reverses cleanly" is the part theory does not supply. Worse, a stop placed just
beyond the POC sits in the single densest, most rotation-prone region of the whole
distribution, which is where a tight stop is least likely to survive.

This is implemented faithfully anyway, because the claim deserves a fair test
rather than a dismissal - and because lvn_rejection.py implements the variant
theory does predict, from the same context and the same confirmations, so Phase 1
can compare them directly. That comparison is the cleanest available test of
whether the profile's thick regions or its thin regions are the tradeable ones.
"""
import config
from profile import va_hvn
from setups.base import (Candidate, RejectReason, SetupState, choose_target,
                         multibin_delta_normalized, reject, target_candidates,
                         vwap_zscore, weekly_poc_distance_atr)

SETUP = "S1-POC"


def _compute_stop(levels, prior_profile, poc, direction, buffer, floor_atr, atr):
    """Where the stop goes: "buffer" (default) or "lvn" (config.S1_STOP_MODE).

    Returns (stop_price, stop_reference) - the reference is recorded on every
    candidate so Phase 1 can see exactly which path each trade took, not just
    which mode was configured for the whole run.
    """
    if config.S1_STOP_MODE != "lvn":
        stop_price = poc + buffer if direction == "SELL" else poc - buffer
        return stop_price, "poc_buffer"

    search_direction = "ABOVE" if direction == "SELL" else "BELOW"
    outer_bound = prior_profile.high if search_direction == "ABOVE" else prior_profile.low
    lvn = levels.nearest_lvn_beyond(poc, search_direction, outer_bound=outer_bound)

    if lvn is not None:
        raw_stop = (lvn.high_price + buffer if direction == "SELL"
                   else lvn.low_price - buffer)
        stop_reference = "lvn"
    else:
        # No thin area between the POC and the session's own recorded range - the
        # value-area edge is the next most structural reference available.
        raw_stop = levels.vah + buffer if direction == "SELL" else levels.val - buffer
        stop_reference = "value_edge_fallback"

    # Volatility floor: a very close LVN must not produce a stop tighter than this.
    # No matching ceiling here deliberately - STOP_TOO_WIDE (checks.stop_too_wide)
    # already rejects a stop this far out on economic grounds, same as it would for
    # any other setup, so a second bound here would just duplicate that gate.
    floor_distance = atr * floor_atr
    if direction == "SELL":
        stop_price = max(raw_stop, poc + floor_distance)
    else:
        stop_price = min(raw_stop, poc - floor_distance)
    return stop_price, stop_reference


def detect(ctx):
    """Return a Candidate, or a Rejection explaining precisely what failed.

    Conditions are checked cheapest-first and most-general-first, so the reject
    reason recorded is the EARLIEST one that applies. That ordering matters for
    analysis: it makes the reject distribution a funnel rather than an arbitrary
    slice of whichever condition happened to be tested first.
    """
    if not config.S1_POC_ENABLED:
        return reject(RejectReason.SETUP_DISABLED, SETUP, ctx.symbol)

    if ctx.prior_profile is None or ctx.prior_levels is None:
        return reject(RejectReason.NO_PRIOR_PROFILE, SETUP, ctx.symbol)
    if ctx.prior_shape is None:
        return reject(RejectReason.PROFILE_NO_LEVELS, SETUP, ctx.symbol)
    if ctx.confirm_candle is None:
        return reject(RejectReason.INSUFFICIENT_CANDLES, SETUP, ctx.symbol)
    if ctx.atr <= 0:
        return reject(RejectReason.NO_DATA, SETUP, ctx.symbol, detail="atr<=0")

    levels = ctx.prior_levels
    poc = levels.poc_price

    # --- condition 1: the session opened away from agreement ---------------
    # Being inside value is S2's precondition, not this one: if price opened
    # within agreement there is no "return to value" move to fade.
    open_rel = ctx.open_relationship
    if open_rel is None or not open_rel.outside_value:
        return reject(RejectReason.OPEN_RELATIONSHIP_WRONG, SETUP, ctx.symbol,
                      detail=f"open={getattr(open_rel, 'label', 'unknown')}",
                      level_price=poc)

    # Scaled by VALUE WIDTH, not ATR: the band between the value edge and the
    # session extreme is a fraction of the value width, so an ATR-denominated
    # floor would make this condition and the outside-range check below nearly
    # mutually exclusive. See config.S1_MIN_OPEN_DISTANCE_VA_FRACTION.
    min_distance = levels.value_width * config.S1_MIN_OPEN_DISTANCE_VA_FRACTION
    if open_rel.distance_from_value < min_distance:
        return reject(RejectReason.OPEN_RELATIONSHIP_WRONG, SETUP, ctx.symbol,
                      detail=(f"open only {open_rel.distance_from_value:.8g} outside "
                              f"value, need {min_distance:.8g} "
                              f"({config.S1_MIN_OPEN_DISTANCE_VA_FRACTION} of "
                              f"value width)"),
                      level_price=poc)

    # Opening beyond the ENTIRE prior range is an imbalance statement, not a
    # stretched balance - the auction has moved somewhere it did not trade at all.
    # Fading back into a value area it has left is the wrong posture there.
    if open_rel.outside_range:
        return reject(RejectReason.OPEN_RELATIONSHIP_WRONG, SETUP, ctx.symbol,
                      detail="opened outside prior RANGE - imbalance, not balance",
                      level_price=poc)

    # --- condition 2: the auction is in balance ----------------------------
    if ctx.prior_shape.label not in config.S1_REQUIRED_SHAPES:
        return reject(RejectReason.SHAPE_NOT_ELIGIBLE, SETUP, ctx.symbol,
                      detail=(f"shape={ctx.prior_shape.label} "
                              f"required={config.S1_REQUIRED_SHAPES}"),
                      level_price=poc)

    # --- condition 3: price has reached the POC ----------------------------
    # Direction opposes the approach. The approach side is taken from where the
    # session OPENED relative to the POC rather than from where price is now:
    # by the time price is at the level it sits on neither side, and the open is
    # the unambiguous record of which way it travelled to get there.
    approach_from = "BELOW" if open_rel.open_price < poc else "ABOVE"
    direction = "SELL" if approach_from == "BELOW" else "BUY"

    tolerance = ctx.atr * config.S1_TOUCH_TOLERANCE_ATR
    candle = ctx.confirm_candle
    touched = (candle.low - tolerance) <= poc <= (candle.high + tolerance)
    if not touched:
        return reject(RejectReason.PRICE_NOT_AT_LEVEL, SETUP, ctx.symbol,
                      direction=direction, level_price=poc,
                      detail=(f"candle [{candle.low:.8g},{candle.high:.8g}] "
                              f"missed poc {poc:.8g} +/- {tolerance:.8g}"))

    # A decisive close through the POC removes the premise: price did not reject
    # the level, it accepted it. Distinguished from a mere touch so the journal
    # separates "not yet" from "no longer".
    beyond = (candle.close - poc) if direction == "SELL" else (poc - candle.close)
    if beyond > ctx.atr * config.S1_INVALIDATE_ATR:
        return reject(RejectReason.PRICE_NOT_AT_LEVEL, SETUP, ctx.symbol,
                      direction=direction, level_price=poc,
                      detail=(f"closed {beyond / ctx.atr:.3f} ATR through the POC "
                              f"- level accepted, not rejected"))

    # --- condition 4: rejection confirms ----------------------------------
    from confirm import collect, summarise
    confirmations = collect(list(ctx.confirm_candles), poc, direction, ctx.atr)
    summary = summarise(confirmations, minimum=config.S1_MIN_CONFIRMATIONS)
    if not summary["confirmed"]:
        return reject(RejectReason.NOT_CONFIRMED, SETUP, ctx.symbol,
                      direction=direction, level_price=poc,
                      detail=f"{summary['count']}/{summary['required']} confirmations",
                      confirmations=summary["names"])

    # --- condition 5: stop, either just beyond the POC or beyond the nearest
    # thin area (config.S1_STOP_MODE) --------------------------------------
    # max() of an ATR buffer and a tick buffer: the ATR term scales with
    # volatility, the tick term keeps the stop off the level itself on a symbol
    # whose ATR is tiny relative to its tick.
    tick = float(ctx.spec.tick_size) if ctx.spec else 0.0
    buffer = max(ctx.atr * config.S1_STOP_BUFFER_ATR,
                 tick * config.S1_STOP_BUFFER_TICKS)
    stop_price, stop_reference = _compute_stop(
        levels, ctx.prior_profile, poc, direction, buffer,
        config.S1_STOP_LVN_FLOOR_ATR, ctx.atr)

    entry_price = ctx.reference_entry
    if entry_price <= 0:
        return reject(RejectReason.NO_DATA, SETUP, ctx.symbol, direction=direction,
                      detail="no reference entry price")

    # The stop must be on the losing side of the entry. When the confirmation
    # candle closed past the level, the buffer can land the stop the wrong side -
    # which would be a trade with inverted risk, not a tight one.
    if direction == "SELL" and stop_price <= entry_price:
        return reject(RejectReason.STOP_TOO_TIGHT, SETUP, ctx.symbol,
                      direction=direction, level_price=poc,
                      detail=f"stop {stop_price:.8g} <= entry {entry_price:.8g}")
    if direction == "BUY" and stop_price >= entry_price:
        return reject(RejectReason.STOP_TOO_TIGHT, SETUP, ctx.symbol,
                      direction=direction, level_price=poc,
                      detail=f"stop {stop_price:.8g} >= entry {entry_price:.8g}")

    # --- condition 6: target ---------------------------------------------
    # PLAN item 17: both opt-in and OFF by default, see config.py.
    hvns = levels.hvns if config.TARGET_INCLUDE_HVN else None
    prior_extreme = (
        (ctx.prior_profile.high if direction == "BUY" else ctx.prior_profile.low)
        if config.TARGET_INCLUDE_PRIOR_EXTREME else None
    )
    target_price, target_kind = choose_target(
        entry_price, stop_price, direction, levels,
        naked_pocs=ctx.naked_pocs, hvns=hvns, prior_extreme=prior_extreme,
        prefer_near=config.TARGET_PREFER_NEAR_LEVEL,
    )

    # Full candidate menu at entry, for the counterfactual target study - recorded
    # regardless of the item 17/25 flags, read by nothing that decides a trade.
    full_prior_extreme = (
        ctx.prior_profile.high if direction == "BUY" else ctx.prior_profile.low
    )
    all_target_candidates = target_candidates(
        entry_price, direction, levels, naked_pocs=ctx.naked_pocs,
        hvns=levels.hvns, prior_extreme=full_prior_extreme,
    )
    hvn_usage = va_hvn.usage(entry_price, direction, ctx.atr, levels, ctx.dev_levels,
                             levels.hvns, ctx.hvn_tests)

    # Normalised per-bin taker imbalance AT THE POC - the same quantity
    # gates.checks.delta_opposed would gate on, recorded unconditionally so Phase 3
    # can sweep the threshold from stored rows instead of re-running the replay per
    # candidate threshold. None when the bin carries no volume (nothing to normalise by).
    poc_bin = ctx.prior_profile.bin_index(poc)
    poc_bin_volume = ctx.prior_profile.volume_at(poc_bin)
    bin_delta_normalized = (
        ctx.prior_profile.delta_at(poc_bin) / poc_bin_volume
        if poc_bin_volume > 0 else None
    )

    # PLAN item 6: the same read, widened to a window of bins around the level -
    # recorded unconditionally so it can be swept from stored rows, same as the
    # single-bin figure above. Not yet part of any Phase 3 sweep.
    multi_bin_delta_normalized = multibin_delta_normalized(ctx.prior_profile, poc)

    # How far the POC itself sits from the prior session's VWAP, in VWAP sigmas -
    # recorded unconditionally, same "measure before gate" pattern as the two
    # delta reads above. Nothing reads this yet; see CALIBRATION.md finding 32.
    vwap_zscore_at_level = vwap_zscore(levels, poc)

    # PLAN item 21: signed distance from the traded level to last week's POC, in
    # ATR - recorded unconditionally, None when no weekly bundle exists yet.
    weekly_poc_distance = weekly_poc_distance_atr(ctx.weekly_levels, poc, ctx.atr)

    # The remaining two opinion gates - gates.checks.stop_inside_hvn and
    # target_behind_hvn - recorded the identical way, unconditionally.
    stop_inside_hvn = levels.hvn_containing(stop_price) is not None
    _lo, _hi = sorted((entry_price, target_price))
    target_behind_hvn = any(_lo < node.center_price < _hi for node in levels.hvns)

    candidate = Candidate(
        setup=SETUP,
        symbol=ctx.symbol,
        direction=direction,
        state=SetupState.TRIGGERED,
        level_price=poc,
        level_kind="POC",
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        session_id=ctx.session_id,
        as_of=ctx.as_of,
        atr=ctx.atr,
        profile_snapshot=ctx.profile_row(),
        attributes={
            "target_kind": target_kind,
            "target_candidates": all_target_candidates,
            "hvn_entry_in_zone": hvn_usage["entry_in_zone"],
            "hvn_nearest_atr": hvn_usage["nearest_atr"],
            "hvn_confluence": hvn_usage["confluence"],
            "hvn_first_test": hvn_usage["first_test"],
            "approach_from": approach_from,
            "open_distance_atr": open_rel.distance_atr,
            "poc_distance_atr": abs(entry_price - poc) / ctx.atr,
            "stop_buffer_atr": buffer / ctx.atr,
            "stop_reference": stop_reference,
            "stop_mode": config.S1_STOP_MODE,
            "confirmations": ",".join(summary["names"]),
            "confirmation_count": summary["count"],
            "confirmation_strength": summary["strength"],
            "value_width_atr": levels.value_width / ctx.atr,
            "poc_prominence": levels.poc_prominence,
            "shape": ctx.prior_shape.label,
            "excess_high": ctx.prior_shape.excess_high,
            "excess_low": ctx.prior_shape.excess_low,
            "bin_delta_normalized": bin_delta_normalized,
            "multi_bin_delta_normalized": multi_bin_delta_normalized,
            "vwap_zscore_at_level": vwap_zscore_at_level,
            "weekly_poc_distance_atr": weekly_poc_distance,
            "stop_inside_hvn": stop_inside_hvn,
            "target_behind_hvn": target_behind_hvn,
            # Recorded so Phase 1 can answer the sibling comparison directly:
            # was there a thin area between price and the POC that S1-LVN would
            # have traded instead?
            "lvn_between": bool(levels.nearest_lvn_between(open_rel.open_price, poc)),
        },
    )
    return candidate
