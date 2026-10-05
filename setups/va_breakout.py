"""S3-BRK: join a breakout from value that has been accepted.

THE IMBALANCE TRADE. If the auction has entered price discovery, yesterday's value
is irrelevant and pullbacks should be joined rather than faded. The entire
difficulty is telling discovery apart from a failed excursion - which is the same
price action as S2, observed one candle earlier.

ACCEPTANCE IS THE DISCRIMINATOR, AND IT IS NOT DISTANCE. A move that travels far on
thin volume has advertised a price nobody wanted; a move that transacts real
business outside old value has found counterparties there. So condition 2 below is
a volume-and-time test via acceptance.py, and the ATR distance in condition 1 is
only a floor to exclude noise - it is deliberately NOT the thing that decides
whether the breakout is real. Every breakout rule written in ATR alone fails here.

CONTINUATION IS A NEW EXTREME, following the source material's own words: for a
bullish setup "we want to see price forming higher highs". So continuation means a
close BEYOND the pre-pullback extreme, not merely a bounce off the pullback low.
That is stricter and produces fewer trades, and it has the decisive advantage of
being unambiguous - "the pullback held" is a judgement, "price closed above the
prior high" is a fact.

THE FAILURE PATH IS EXPLICIT. The source material's second example shows a breakout
whose pullback does not hold, and the setup is abandoned rather than traded.
PULLBACK_TOO_DEEP is that outcome, journaled. Abandoning a setup is an event worth
recording, not silence - the rate at which breakouts fail their pullback is one of
the more informative things this system can learn.
"""
import acceptance as acceptance_mod
import config
from setups.base import Candidate, RejectReason, SetupState, choose_target, reject

SETUP = "S3-BRK"


def _pullback_structure(window, side):
    """Locate the breakout extreme and the pullback that followed it.

    THE SIGNAL CANDLE IS EXCLUDED FROM THE SEARCH, and that exclusion is the whole
    correctness of this function rather than a detail.

    The first version searched the entire window, signal candle included, which made
    S3 mathematically incapable of firing - verified at 0 of 30,000 random windows.
    Both branches rejected, for opposite reasons:

      the signal candle makes a new extreme (which is precisely what continuation IS)
          -> it becomes the extreme, `after` is empty, the function returns None, and
             the caller reports "no pullback has formed yet"

      the signal candle makes no new extreme
          -> `extreme` is still the max high across a window CONTAINING that candle,
             so extreme >= candle.high >= candle.close, and the caller's test
             `close > extreme` cannot be true

    So the structure must be measured over the candles BEFORE the one being judged.
    `window[:-1]` is the history in which the breakout and its pullback happened;
    `window[-1]` is the candle asked to confirm continuation past them. Splitting the
    two is what turns an unsatisfiable comparison into a real one.

    Returns (extreme, extreme_index, pullback_extreme, pullback_index) indexed against
    the FULL window, or None when no completed breakout-then-pullback exists yet.
    """
    # Three candles of history plus the signal candle: an extreme, at least one
    # pullback candle, and enough room for the pullback not to be the extreme itself.
    if len(window) < 4:
        return None

    history = window[:-1]

    if side == "ABOVE":
        extreme_index = max(range(len(history)), key=lambda i: history[i].high)
        extreme = history[extreme_index].high
    else:
        extreme_index = min(range(len(history)), key=lambda i: history[i].low)
        extreme = history[extreme_index].low

    # The pullback is what happened between the extreme and the signal candle. If the
    # extreme is the last candle of history there is no pullback yet - price is still
    # extending, and there is nothing to join.
    after = history[extreme_index + 1:]
    if not after:
        return None

    if side == "ABOVE":
        pullback_index = min(range(len(after)), key=lambda i: after[i].low)
        pullback_extreme = after[pullback_index].low
    else:
        pullback_index = max(range(len(after)), key=lambda i: after[i].high)
        pullback_extreme = after[pullback_index].high

    return extreme, extreme_index, pullback_extreme, extreme_index + 1 + pullback_index


def _stop_buffer(ctx):
    """The stop buffer, identical in both entry modes.

    Shared as one function specifically so the two modes cannot drift onto different
    buffer formulas by accident - the comparison is only clean if everything except
    the tested variable (entry timing) is held identical.
    """
    tick = float(ctx.spec.tick_size) if ctx.spec else 0.0
    return max(ctx.atr * config.S3_STOP_BUFFER_ATR, tick * config.S1_STOP_BUFFER_TICKS)


def _widen_past_lvn(stop_price, buffer, levels, direction):
    """Move a stop that lands inside a low-volume node to just beyond it.

    Shared between both entry modes for the same reason as `_stop_buffer`: the LVN
    check is about where liquidity is thin, not about which entry mode is in force,
    and it must apply identically to whatever stop reference each mode computes.
    """
    if not config.S3_WIDEN_STOP_PAST_LVN:
        return stop_price, False
    for node in levels.lvns:
        if direction == "BUY" and node.low_price <= stop_price <= node.high_price:
            return node.low_price - buffer, True
        if direction == "SELL" and node.low_price <= stop_price <= node.high_price:
            return node.high_price + buffer, True
    return stop_price, False


def _confirmed_entry(ctx, verdict, levels, bound, direction):
    """S3_ENTRY_MODE="confirmed" (default): wait for a pullback and a new high past it.

    THE MODE THE MATCHED-RANDOM CONTROL MEASURED AS WORSE THAN CHANCE (CALIBRATION.md
    finding 15). Kept exactly as it was validated - unchanged behaviour when
    S3_ENTRY_MODE stays at its default - so it remains the correct control for
    `_accepted_entry` below rather than a moving target.
    """
    start = 0
    for index in range(len(ctx.confirm_candles) - 1, -1, -1):
        if levels.val <= ctx.confirm_candles[index].close <= levels.vah:
            start = index + 1
            break
    window = list(ctx.confirm_candles[start:])

    structure = _pullback_structure(window, verdict.side)
    if structure is None:
        return {"rejection": reject(
            RejectReason.NO_CONTINUATION, SETUP, ctx.symbol,
            direction=direction, level_price=bound,
            detail="no pullback has formed yet - price still extending")}

    extreme, extreme_index, pullback_extreme, pullback_index = structure

    # The pullback must not push far back into the value area. Measured on the
    # pullback's own extreme rather than on closes, because a wick deep into value
    # is already evidence the breakout is not holding.
    max_depth = levels.value_width * config.S3_PULLBACK_MAX_DEPTH
    if verdict.side == "ABOVE":
        limit = levels.vah - max_depth
        too_deep = pullback_extreme < limit
    else:
        limit = levels.val + max_depth
        too_deep = pullback_extreme > limit

    if too_deep:
        return {"rejection": reject(
            RejectReason.PULLBACK_TOO_DEEP, SETUP, ctx.symbol,
            direction=direction, level_price=bound,
            detail=(f"pullback to {pullback_extreme:.8g} breached "
                    f"{limit:.8g} ({config.S3_PULLBACK_MAX_DEPTH:.2f} of "
                    f"value width back inside) - breakout not holding"),
            pullback_extreme=pullback_extreme)}

    # A new extreme beyond the pre-pullback one, following the source material's own
    # words: for a bullish setup "we want to see price forming higher highs".
    candle = ctx.confirm_candles[-1]
    continued = (candle.close > extreme if verdict.side == "ABOVE"
                else candle.close < extreme)
    if not continued:
        return {"rejection": reject(
            RejectReason.NO_CONTINUATION, SETUP, ctx.symbol,
            direction=direction, level_price=bound,
            detail=(f"close {candle.close:.8g} has not exceeded the pre-pullback "
                    f"extreme {extreme:.8g}"))}

    buffer = _stop_buffer(ctx)
    stop_price = (pullback_extreme - buffer if direction == "BUY"
                  else pullback_extreme + buffer)
    stop_price, widened = _widen_past_lvn(stop_price, buffer, levels, direction)

    return {
        "rejection": None,
        "stop_price": stop_price,
        "attributes": {
            "breakout_extreme": extreme,
            "pullback_extreme": pullback_extreme,
            "pullback_depth_va_fraction": (
                abs(bound - pullback_extreme) / levels.value_width
                if levels.value_width > 0 else 0.0
            ),
            "pullback_candles": pullback_index - extreme_index,
            "widened_past_lvn": widened,
            "stop_buffer_atr": buffer / ctx.atr,
            "stop_reference": "pullback_extreme",
        },
    }


def _accepted_entry(ctx, verdict, levels, bound, direction):
    """S3_ENTRY_MODE="accepted": enter the bar acceptance itself first confirms.

    THE EXPERIMENT finding 15's diagnosis motivates, not its result - see
    S3_ENTRY_MODE in config.py. No pullback exists yet in this mode by construction,
    so there is nothing to wait for and nothing to measure a depth or a continuation
    against. The stop instead anchors on the value bound the excursion broke from:
    under acceptance's own logic, price closing back inside value is what falsifies
    the trade (see ACCEPTANCE_CONTRADICTS elsewhere in this file), so the bound is the
    level whose failure the setup is actually betting against, not an arbitrary
    tighter number chosen for its own sake.
    """
    buffer = _stop_buffer(ctx)
    stop_price = bound - buffer if direction == "BUY" else bound + buffer
    stop_price, widened = _widen_past_lvn(stop_price, buffer, levels, direction)

    return {
        "rejection": None,
        "stop_price": stop_price,
        "attributes": {
            "widened_past_lvn": widened,
            "stop_buffer_atr": buffer / ctx.atr,
            "stop_reference": "value_bound",
        },
    }


def detect(ctx):
    """Return a Candidate, or a Rejection naming the earliest failed condition."""
    if not config.S3_ENABLED:
        return reject(RejectReason.SETUP_DISABLED, SETUP, ctx.symbol)

    if ctx.prior_profile is None or ctx.prior_levels is None:
        return reject(RejectReason.NO_PRIOR_PROFILE, SETUP, ctx.symbol)
    if ctx.confirm_candle is None:
        return reject(RejectReason.INSUFFICIENT_CANDLES, SETUP, ctx.symbol)
    if ctx.atr <= 0:
        return reject(RejectReason.NO_DATA, SETUP, ctx.symbol, detail="atr<=0")

    levels = ctx.prior_levels

    # Condition 0: no open-relationship precondition. This is the only setup
    # eligible in every state, which is why it is the one that can be right when
    # the balance hypothesis is wrong.

    # --- condition 2 first: is there an accepted excursion at all? ----------
    # Checked before the distance floor because acceptance is the real question
    # and the distance floor is cheap noise-exclusion. Ordering the funnel this way
    # makes the reject distribution say something about the market rather than
    # about our parameter choices.
    verdict = acceptance_mod.evaluate(list(ctx.confirm_candles), levels, ctx.atr,
                                      prior_rate=ctx.prior_confirm_rate)

    if verdict.side == "NONE":
        return reject(RejectReason.NO_EXCURSION, SETUP, ctx.symbol,
                      detail="price is inside value - nothing to break out of")

    # No baseline, no measurement. This is the setup the bug found in Phase 0 hit
    # hardest: a session opening outside value and never returning made the ratio
    # exactly 1.000 by construction, which cleared the acceptance threshold and armed
    # this setup on a number that contained no information.
    if verdict.verdict == acceptance_mod.NO_BASELINE:
        return reject(RejectReason.ACCEPTANCE_NOT_MEASURABLE, SETUP, ctx.symbol,
                      detail="no volume baseline independent of the excursion")

    direction = "BUY" if verdict.side == "ABOVE" else "SELL"
    bound = levels.vah if verdict.side == "ABOVE" else levels.val

    if verdict.returned_inside:
        return reject(RejectReason.ACCEPTANCE_CONTRADICTS, SETUP, ctx.symbol,
                      direction=direction, level_price=bound,
                      detail="price closed back inside value - this is S2 territory")

    if not verdict.accepted:
        reason = (RejectReason.ACCEPTANCE_PENDING if not verdict.rejected
                  else RejectReason.ACCEPTANCE_CONTRADICTS)
        return reject(reason, SETUP, ctx.symbol,
                      direction=direction, level_price=bound,
                      detail=(f"not accepted: closes={verdict.consecutive_closes_outside}"
                              f"/{config.ACCEPT_MIN_CANDLES} "
                              f"rate_ratio={verdict.volume_rate_ratio:.3f}"
                              f"/{config.ACCEPT_MIN_VOLUME_RATE_RATIO}"),
                      volume_rate_ratio=verdict.volume_rate_ratio)

    # --- condition 1: the break is significant -----------------------------
    # Two floors, whichever is larger. The ATR term scales with volatility; the
    # value-width term scales with how wide agreement was, because a break of 0.5
    # ATR out of a very wide value area is a smaller statement than the same
    # distance out of a tight one.
    min_distance = max(ctx.atr * config.S3_BREAK_MIN_ATR,
                       levels.value_width * config.S3_BREAK_MIN_VA_FRACTION)
    if verdict.excursion_distance < min_distance:
        return reject(RejectReason.EXCURSION_TOO_SMALL, SETUP, ctx.symbol,
                      direction=direction, level_price=bound,
                      detail=(f"break {verdict.excursion_distance:.8g} < required "
                              f"{min_distance:.8g} "
                              f"(atr floor {ctx.atr * config.S3_BREAK_MIN_ATR:.8g}, "
                              f"va floor {levels.value_width * config.S3_BREAK_MIN_VA_FRACTION:.8g})"))

    # --- condition 3: entry mode branches here ------------------------------
    # "confirmed" (default) waits for a pullback and a new high past it - the mode
    # the matched-random control measured as WORSE than a random entry at the same
    # location. "accepted" enters on the bar acceptance itself first confirms,
    # before any pullback exists. See S3_ENTRY_MODE in config.py for the evidence.
    if config.S3_ENTRY_MODE == "accepted":
        outcome = _accepted_entry(ctx, verdict, levels, bound, direction)
    else:
        outcome = _confirmed_entry(ctx, verdict, levels, bound, direction)
    if outcome.get("rejection") is not None:
        return outcome["rejection"]

    stop_price = outcome["stop_price"]
    mode_attributes = outcome["attributes"]

    # The developing profile should be building a new node in the breakout
    # direction - value itself migrating, not just price travelling. This is the
    # profile-native version of "the move is real", and it is the one condition
    # here that a price-only system cannot express. Applied to BOTH entry modes:
    # it needs no pullback structure, only ctx.dev_levels, so it is an independent
    # quality signal rather than part of the timing question S3_ENTRY_MODE tests.
    if config.S3_REQUIRE_DEVELOPING_POC_MIGRATION and ctx.dev_levels is not None:
        migrated = (ctx.dev_levels.poc_price > levels.poc_price
                    if verdict.side == "ABOVE"
                    else ctx.dev_levels.poc_price < levels.poc_price)
        if not migrated:
            return reject(RejectReason.NO_CONTINUATION, SETUP, ctx.symbol,
                          direction=direction, level_price=bound,
                          detail=(f"developing POC {ctx.dev_levels.poc_price:.8g} has "
                                  f"not migrated past prior POC "
                                  f"{levels.poc_price:.8g} - price moved, value "
                                  f"did not"))

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
    # Structural mode aims at the next naked POC in the breakout direction -
    # unfinished business the auction is travelling toward. The prior POC is
    # excluded: the trade is moving away from it, so it is behind the entry.
    target_price, target_kind = choose_target(
        entry_price, stop_price, direction, levels, naked_pocs=ctx.naked_pocs,
    )

    return Candidate(
        setup=SETUP,
        symbol=ctx.symbol,
        direction=direction,
        state=SetupState.TRIGGERED,
        level_price=bound,
        level_kind="VAH" if verdict.side == "ABOVE" else "VAL",
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        session_id=ctx.session_id,
        as_of=ctx.as_of,
        atr=ctx.atr,
        profile_snapshot=ctx.profile_row(),
        attributes={
            "target_kind": target_kind,
            "entry_mode": config.S3_ENTRY_MODE,
            "break_side": verdict.side,
            "break_distance_atr": verdict.excursion_distance_atr,
            "break_distance_va_fraction": (
                verdict.excursion_distance / levels.value_width
                if levels.value_width > 0 else 0.0
            ),
            "volume_rate_ratio": verdict.volume_rate_ratio,
            "volume_outside_fraction": verdict.volume_outside_fraction,
            "outside_rate": verdict.outside_rate,
            "session_rate": verdict.session_rate,
            "consecutive_closes_outside": verdict.consecutive_closes_outside,
            "acceptance_candles": verdict.candles_in_window,
            "value_width_atr": levels.value_width / ctx.atr,
            "dev_poc_migrated": (
                (ctx.dev_levels.poc_price - levels.poc_price) / ctx.atr
                if ctx.dev_levels is not None else None
            ),
            "shape": ctx.prior_shape.label if ctx.prior_shape else "",
            **mode_attributes,
        },
    )
