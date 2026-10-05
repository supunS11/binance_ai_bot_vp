"""S2-VAR: fade an excursion outside value that was never accepted.

THE BEST-SUPPORTED OF THE FOUR SETUPS. It is a direct statement of the balance
hypothesis: price left an agreed range, found no acceptance outside it, and
returned. The source material's close-back-inside requirement is exactly the right
confirmation, and for a reason worth stating - ACCEPTANCE REQUIRES TIME AND VOLUME.
A wick outside value proves nothing, because a wick is an advertised price nobody
transacted at. A CLOSE back inside says the excursion was rejected by the auction
rather than merely paused.

THE DISCRIMINATOR AGAINST S3. This setup and va_breakout.py trade the same location
in opposite directions, so they cannot both be right. They are resolved by
acceptance.py, not by a priority list: S2 requires business outside value to have run
at a LOW rate relative to this session's own normal
(<= REJECT_MAX_VOLUME_RATE_RATIO), S3 requires a HIGH one
(>= ACCEPT_MIN_VOLUME_RATE_RATIO). The gap between those two thresholds is deliberate
- inside it neither fires, which is what makes their mutual exclusivity real rather
than a tie-break.

TWO THEORY-DRIVEN REFINEMENTS the naive version misses, both implemented and both
defaulting OFF pending Phase 3:

  SHAPE      reverting into a D-shape is a balance trade; reverting against a P or
             b shape is fading a one-sided auction. Same entry, different regime,
             and it should not be the same trade.
  EXCESS     an excursion into an extreme showing EXCESS has already been rejected
             by the auction. One into a POOR extreme - no tail, volume still
             building - is far more likely to extend. Identical geometry, opposite
             prognosis.
"""
import acceptance as acceptance_mod
import config
from profile import va_hvn
from setups.base import (Candidate, RejectReason, SetupState, choose_target,
                         multibin_delta_normalized, reject, target_candidates,
                         vwap_zscore, weekly_poc_distance_atr)

SETUP = "S2-VAR"


def _compute_stop(levels, prior_profile, extreme, direction, buffer, floor_atr, atr):
    """Where the stop goes: "extreme" (default) or "lvn" (config.S2_STOP_MODE).

    Same idea as poc_rotation._compute_stop, adapted to this setup's own anchor:
    the excursion's own extreme rather than the POC. The fallback here is the
    extreme itself - i.e. exactly today's behaviour - so "lvn" can only ever widen
    the stop relative to the default, never tighten it below what was already
    measured.
    """
    if config.S2_STOP_MODE != "lvn":
        stop_price = extreme - buffer if direction == "BUY" else extreme + buffer
        return stop_price, "extreme_buffer"

    search_direction = "ABOVE" if direction == "SELL" else "BELOW"
    outer_bound = (prior_profile.high if search_direction == "ABOVE"
                   else prior_profile.low)
    lvn = levels.nearest_lvn_beyond(extreme, search_direction, outer_bound=outer_bound)

    if lvn is not None:
        raw_stop = (lvn.high_price + buffer if direction == "SELL"
                   else lvn.low_price - buffer)
        stop_reference = "lvn"
    else:
        raw_stop = extreme - buffer if direction == "BUY" else extreme + buffer
        stop_reference = "extreme_buffer_fallback"

    floor_distance = atr * floor_atr
    if direction == "SELL":
        stop_price = max(raw_stop, extreme + floor_distance)
    else:
        stop_price = min(raw_stop, extreme - floor_distance)
    return stop_price, stop_reference


def detect(ctx):
    """Return a Candidate, or a Rejection naming the earliest failed condition."""
    if not config.S2_ENABLED:
        return reject(RejectReason.SETUP_DISABLED, SETUP, ctx.symbol)

    if ctx.prior_profile is None or ctx.prior_levels is None:
        return reject(RejectReason.NO_PRIOR_PROFILE, SETUP, ctx.symbol)
    if ctx.confirm_candle is None:
        return reject(RejectReason.INSUFFICIENT_CANDLES, SETUP, ctx.symbol)
    if ctx.atr <= 0:
        return reject(RejectReason.NO_DATA, SETUP, ctx.symbol, detail="atr<=0")

    levels = ctx.prior_levels

    # --- condition 1: the session opened within agreement ------------------
    open_rel = ctx.open_relationship
    if open_rel is None or not open_rel.inside_value:
        return reject(RejectReason.OPEN_RELATIONSHIP_WRONG, SETUP, ctx.symbol,
                      detail=(f"open={getattr(open_rel, 'label', 'unknown')} "
                              f"- S2 requires INSIDE_VALUE"))

    # --- conditions 2-4: the excursion, measured once ----------------------
    # A single acceptance evaluation supplies the excursion size, the volume
    # fraction outside value, and whether price has closed back inside. Computing
    # these separately would risk S2 and S3 disagreeing about the same move.
    verdict = acceptance_mod.evaluate(list(ctx.confirm_candles), levels, ctx.atr,
                                      prior_rate=ctx.prior_confirm_rate)

    if verdict.verdict == acceptance_mod.INSIDE and not verdict.returned_inside:
        return reject(RejectReason.NO_EXCURSION, SETUP, ctx.symbol,
                      detail="price has not left value this session")

    # No independent volume baseline means the discriminator was not measured at all.
    # Checked BEFORE `rejected` is consulted, because with no baseline the ratio
    # computes to 0.0 - which reads as the thinnest possible excursion and would arm
    # exactly this setup on a session that has told us nothing.
    if verdict.verdict == acceptance_mod.NO_BASELINE:
        return reject(RejectReason.ACCEPTANCE_NOT_MEASURABLE, SETUP, ctx.symbol,
                      detail="no volume baseline independent of the excursion")

    if verdict.side == "NONE":
        return reject(RejectReason.NO_EXCURSION, SETUP, ctx.symbol,
                      detail="no excursion side identified")

    # Direction is toward value: an excursion BELOW the value area is faded long.
    direction = "BUY" if verdict.side == "BELOW" else "SELL"
    bound = levels.val if verdict.side == "BELOW" else levels.vah

    # Scaled by VALUE WIDTH, not ATR. Daily ATR is a 14-day average range while the
    # value area is a fraction of one session's range; on a quiet day they differ by
    # ~10x, so an ATR-denominated floor demands an excursion larger than the value
    # area itself. See config.S2_MIN_EXCURSION_VA_FRACTION.
    min_excursion = levels.value_width * config.S2_MIN_EXCURSION_VA_FRACTION
    if verdict.excursion_distance < min_excursion:
        return reject(RejectReason.EXCURSION_TOO_SMALL, SETUP, ctx.symbol,
                      direction=direction, level_price=bound,
                      detail=(f"excursion {verdict.excursion_distance:.8g} < "
                              f"{min_excursion:.8g} "
                              f"({config.S2_MIN_EXCURSION_VA_FRACTION} of value "
                              f"width) - too small to fade"))

    # --- condition 3: the excursion was NOT accepted -----------------------
    if verdict.accepted:
        return reject(RejectReason.ACCEPTANCE_CONTRADICTS, SETUP, ctx.symbol,
                      direction=direction, level_price=bound,
                      detail=(f"excursion ACCEPTED "
                              f"(rate_ratio={verdict.volume_rate_ratio:.3f}, "
                              f"closes={verdict.consecutive_closes_outside}) "
                              f"- this is S3 territory"),
                      volume_rate_ratio=verdict.volume_rate_ratio)

    # A FAILED AUCTION is not this setup's trade. Real business was transacted outside
    # value and then reclaimed - participants committed at the new prices and were
    # proven wrong, which is a stronger and structurally different event than the thin
    # spike S2 fades. Recorded distinctly so Phase 1 can measure how often it occurs
    # and whether it earns its own setup; deliberately not traded in v1.
    if verdict.verdict == acceptance_mod.FAILED_AUCTION:
        return reject(RejectReason.FAILED_AUCTION_NOT_TRADED, SETUP, ctx.symbol,
                      direction=direction, level_price=bound,
                      detail=(f"failed auction: rate_ratio="
                              f"{verdict.volume_rate_ratio:.3f} is acceptance-level "
                              f"volume that was then reclaimed - out of v1 scope"),
                      volume_rate_ratio=verdict.volume_rate_ratio)

    if not verdict.rejected:
        return reject(RejectReason.ACCEPTANCE_PENDING, SETUP, ctx.symbol,
                      direction=direction, level_price=bound,
                      detail=(f"rate_ratio={verdict.volume_rate_ratio:.3f} sits "
                              f"between reject<={config.REJECT_MAX_VOLUME_RATE_RATIO} "
                              f"and accept>={config.ACCEPT_MIN_VOLUME_RATE_RATIO}"),
                      volume_rate_ratio=verdict.volume_rate_ratio)

    # --- condition 4: a candle has CLOSED back inside value ----------------
    if not verdict.returned_inside:
        return reject(RejectReason.NOT_CONFIRMED, SETUP, ctx.symbol,
                      direction=direction, level_price=bound,
                      detail="no close back inside value yet (wicks do not count)")

    # --- optional quality overlays, default OFF ----------------------------
    if config.S2_REQUIRE_BALANCED_SHAPE and ctx.prior_shape is not None:
        if not ctx.prior_shape.balanced:
            return reject(RejectReason.SHAPE_OPPOSED, SETUP, ctx.symbol,
                          direction=direction, level_price=bound,
                          detail=(f"shape={ctx.prior_shape.label} is one-sided - "
                                  f"reverting against a directional auction"))

    if config.S2_REQUIRE_POOR_EXTREME and ctx.prior_shape is not None:
        # The excursion ran into the prior session's own extreme. If that extreme
        # showed EXCESS it was already defended, which argues for the fade; if it
        # was POOR the auction was cut short there and is likelier to extend.
        excess_at_extreme = (ctx.prior_shape.excess_low if verdict.side == "BELOW"
                             else ctx.prior_shape.excess_high)
        if not excess_at_extreme:
            return reject(RejectReason.SHAPE_OPPOSED, SETUP, ctx.symbol,
                          direction=direction, level_price=bound,
                          detail="no excess at the tested extreme - poor high/low")

    # --- condition 5: stop beyond the excursion extreme, or beyond the nearest
    # thin area further out (config.S2_STOP_MODE) ---------------------------
    tick = float(ctx.spec.tick_size) if ctx.spec else 0.0
    buffer = max(ctx.atr * config.S2_STOP_BUFFER_ATR,
                 tick * config.S1_STOP_BUFFER_TICKS)
    extreme = verdict.excursion_extreme
    stop_price, stop_reference = _compute_stop(levels, ctx.prior_profile, extreme,
                                               direction, buffer,
                                               config.S2_STOP_LVN_FLOOR_ATR, ctx.atr)

    entry_price = ctx.reference_entry
    if entry_price <= 0:
        return reject(RejectReason.NO_DATA, SETUP, ctx.symbol, direction=direction,
                      detail="no reference entry price")

    if direction == "BUY" and stop_price >= entry_price:
        return reject(RejectReason.STOP_TOO_TIGHT, SETUP, ctx.symbol,
                      direction=direction, level_price=bound,
                      detail=f"stop {stop_price:.8g} >= entry {entry_price:.8g}")
    if direction == "SELL" and stop_price <= entry_price:
        return reject(RejectReason.STOP_TOO_TIGHT, SETUP, ctx.symbol,
                      direction=direction, level_price=bound,
                      detail=f"stop {stop_price:.8g} <= entry {entry_price:.8g}")

    # --- condition 6: target ---------------------------------------------
    # Structural mode aims at the POC first - the magnet inside the value area the
    # trade is returning to, which is the move this setup is actually predicting.
    # PLAN item 17: both opt-in and OFF by default, see config.py.
    hvns = levels.hvns if config.TARGET_INCLUDE_HVN else None
    prior_extreme = (
        (ctx.prior_profile.high if direction == "BUY" else ctx.prior_profile.low)
        if config.TARGET_INCLUDE_PRIOR_EXTREME else None
    )
    target_price, target_kind = choose_target(
        entry_price, stop_price, direction, levels, naked_pocs=ctx.naked_pocs,
        hvns=hvns, prior_extreme=prior_extreme,
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

    # Normalised per-bin taker imbalance AT THE TRADED BOUND (VAH/VAL) - the same
    # quantity gates.checks.delta_opposed would gate on, recorded unconditionally so
    # Phase 3 can sweep the threshold from stored rows instead of re-running the
    # replay. None when the bin carries no volume.
    bound_bin = ctx.prior_profile.bin_index(bound)
    bound_bin_volume = ctx.prior_profile.volume_at(bound_bin)
    bin_delta_normalized = (
        ctx.prior_profile.delta_at(bound_bin) / bound_bin_volume
        if bound_bin_volume > 0 else None
    )

    # PLAN item 6: the same read, widened to a window of bins around the level -
    # recorded unconditionally so it can be swept from stored rows, same as the
    # single-bin figure above. Not yet part of any Phase 3 sweep.
    multi_bin_delta_normalized = multibin_delta_normalized(ctx.prior_profile, bound)

    # How far the traded bound (VAH/VAL) sits from the prior session's VWAP, in
    # VWAP sigmas - recorded unconditionally, same "measure before gate" pattern as
    # the two delta reads above. Nothing reads this yet; see CALIBRATION.md finding 32.
    vwap_zscore_at_level = vwap_zscore(levels, bound)

    # PLAN item 21: signed distance from the traded level to last week's POC, in
    # ATR - recorded unconditionally, None when no weekly bundle exists yet.
    weekly_poc_distance = weekly_poc_distance_atr(ctx.weekly_levels, bound, ctx.atr)

    # The remaining two opinion gates - gates.checks.stop_inside_hvn and
    # target_behind_hvn - recorded the identical way, unconditionally.
    stop_inside_hvn = levels.hvn_containing(stop_price) is not None
    _lo, _hi = sorted((entry_price, target_price))
    target_behind_hvn = any(_lo < node.center_price < _hi for node in levels.hvns)

    return Candidate(
        setup=SETUP,
        symbol=ctx.symbol,
        direction=direction,
        state=SetupState.TRIGGERED,
        level_price=bound,
        level_kind="VAL" if verdict.side == "BELOW" else "VAH",
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
            "excursion_side": verdict.side,
            "excursion_distance_atr": verdict.excursion_distance_atr,
            "excursion_extreme": verdict.excursion_extreme,
            "excursion_candles": verdict.candles_in_window,
            "volume_rate_ratio": verdict.volume_rate_ratio,
            "volume_outside_fraction": verdict.volume_outside_fraction,
            "outside_rate": verdict.outside_rate,
            "session_rate": verdict.session_rate,
            "consecutive_closes_outside": verdict.consecutive_closes_outside,
            "stop_buffer_atr": buffer / ctx.atr,
            "stop_reference": stop_reference,
            "stop_mode": config.S2_STOP_MODE,
            "value_width_atr": levels.value_width / ctx.atr,
            "poc_distance_atr": abs(entry_price - levels.poc_price) / ctx.atr,
            "shape": ctx.prior_shape.label if ctx.prior_shape else "",
            "excess_at_extreme": (
                (ctx.prior_shape.excess_low if verdict.side == "BELOW"
                 else ctx.prior_shape.excess_high)
                if ctx.prior_shape else None
            ),
            "poor_at_extreme": (
                (ctx.prior_shape.poor_low if verdict.side == "BELOW"
                 else ctx.prior_shape.poor_high)
                if ctx.prior_shape else None
            ),
            "bin_delta_normalized": bin_delta_normalized,
            "multi_bin_delta_normalized": multi_bin_delta_normalized,
            "vwap_zscore_at_level": vwap_zscore_at_level,
            "weekly_poc_distance_atr": weekly_poc_distance,
            "stop_inside_hvn": stop_inside_hvn,
            "target_behind_hvn": target_behind_hvn,
        },
    )
