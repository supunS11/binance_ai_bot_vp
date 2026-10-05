"""S1-LVN: reject from a low-volume node, target the point of control.

THE VARIANT AUCTION THEORY ACTUALLY PREDICTS. It shares conditions 1 and 2 with
S1-POC exactly - prior session opened away from value, balanced profile - and then
swaps the roles of the two level types:

  S1-POC   enters AT the POC (maximum acceptance), expecting rejection there,
           and stops just beyond it.
  S1-LVN   enters at the nearest LOW-volume node between price and the POC,
           expecting rejection there, stops just beyond that thin zone, and
           TARGETS the POC.

The reasoning is the definition of the levels themselves. A low-volume node is
price the auction passed through quickly because one side was absent - so it is
where a reaction is mechanically likely, and where a stop is cheap because price
either rejects immediately or traverses fast with nothing to stop it. The POC is
maximum acceptance, which makes it a magnet and therefore a natural objective. The
sibling setup uses the magnet as an entry and the dense area as a stop location;
this one uses the thin area as an entry and the magnet as a target.

Two consequences worth noting because they will show up in the numbers:

  1. The stop here is usually TIGHTER than S1-POC's - an LVN is by definition a
     narrow band - so the same 2R target is a smaller price move, and fee cost in
     R terms is HIGHER. base.Candidate.round_trip_cost_r makes that visible, and
     the STOP_TOO_TIGHT gate is what stops it becoming a trade whose risk unit is
     smaller than the cost of trading.
  2. The target is fixed by structure rather than by an R multiple, so R varies
     trade to trade. That is the honest version of this setup, and TARGET_MIN_R
     is what declines the ones where the magnet is too close to be worth it.
"""
import config
from setups.base import Candidate, RejectReason, SetupState, reject

SETUP = "S1-LVN"


def detect(ctx):
    """Return a Candidate, or a Rejection naming the earliest failed condition."""
    if not config.S1_LVN_ENABLED:
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

    # --- conditions 1-2: identical to S1-POC, deliberately -----------------
    # Kept as duplicated checks rather than shared code so the two setups can be
    # tuned independently later without one silently changing the other. The
    # Phase 1 comparison is only valid while these agree, and tests assert it.
    open_rel = ctx.open_relationship
    if open_rel is None or not open_rel.outside_value:
        return reject(RejectReason.OPEN_RELATIONSHIP_WRONG, SETUP, ctx.symbol,
                      detail=f"open={getattr(open_rel, 'label', 'unknown')}",
                      level_price=poc)
    min_distance = levels.value_width * config.S1_MIN_OPEN_DISTANCE_VA_FRACTION
    if open_rel.distance_from_value < min_distance:
        return reject(RejectReason.OPEN_RELATIONSHIP_WRONG, SETUP, ctx.symbol,
                      detail=(f"open {open_rel.distance_from_value:.8g} outside value, "
                              f"need {min_distance:.8g}"),
                      level_price=poc)
    if open_rel.outside_range:
        return reject(RejectReason.OPEN_RELATIONSHIP_WRONG, SETUP, ctx.symbol,
                      detail="opened outside prior RANGE - imbalance, not balance",
                      level_price=poc)
    if ctx.prior_shape.label not in config.S1_REQUIRED_SHAPES:
        return reject(RejectReason.SHAPE_NOT_ELIGIBLE, SETUP, ctx.symbol,
                      detail=f"shape={ctx.prior_shape.label}", level_price=poc)

    # --- condition 3: a qualifying LVN stands between price and the POC ----
    # No LVN means the path to the magnet is unobstructed. That is a valid
    # answer, not a failure: there is no rejection level to trade, so this setup
    # simply does not exist on this session.
    node = levels.nearest_lvn_between(open_rel.open_price, poc)
    if node is None:
        return reject(RejectReason.LVN_NOT_QUALIFIED, SETUP, ctx.symbol,
                      level_price=poc,
                      detail="no qualifying LVN between open and POC")

    if node.width_bins < config.LVN_MIN_WIDTH_BINS:
        return reject(RejectReason.LVN_NOT_QUALIFIED, SETUP, ctx.symbol,
                      level_price=node.center_price,
                      detail=(f"lvn width {node.width_bins} bins < "
                              f"{config.LVN_MIN_WIDTH_BINS}"))

    # The magnet must be far enough away to be worth aiming at. A target one
    # tick past the entry is arithmetically a target and economically noise.
    #
    # Denominated in VALUE WIDTH, not ATR, and this one is forced rather than merely
    # preferable: the LVN and the POC both sit inside the value area, so their
    # separation cannot exceed the value width. An ATR threshold larger than the value
    # width (0.14 ATR on BTCUSDT) is unsatisfiable by construction.
    poc_distance = abs(poc - node.center_price)
    min_poc_distance = levels.value_width * config.S1_LVN_MIN_POC_DISTANCE_VA_FRACTION
    if poc_distance < min_poc_distance:
        return reject(RejectReason.TARGET_TOO_CLOSE, SETUP, ctx.symbol,
                      level_price=node.center_price,
                      detail=(f"poc only {poc_distance:.8g} from lvn, need "
                              f"{min_poc_distance:.8g} "
                              f"({config.S1_LVN_MIN_POC_DISTANCE_VA_FRACTION} of "
                              f"value width)"))
    poc_distance_atr = poc_distance / ctx.atr

    # Direction opposes the approach: price travelling from the open toward the
    # POC meets the LVN on the way, and the trade is that the thin area holds.
    approach_from = "BELOW" if open_rel.open_price < poc else "ABOVE"
    direction = "SELL" if approach_from == "BELOW" else "BUY"

    # --- condition 4: price is at the LVN zone ----------------------------
    tolerance = ctx.atr * config.S1_LVN_TOUCH_TOLERANCE_ATR
    candle = ctx.confirm_candle
    touched = (candle.low - tolerance) <= node.high_price and \
              (candle.high + tolerance) >= node.low_price
    if not touched:
        return reject(RejectReason.PRICE_NOT_AT_LEVEL, SETUP, ctx.symbol,
                      direction=direction, level_price=node.center_price,
                      detail=(f"candle [{candle.low:.8g},{candle.high:.8g}] missed "
                              f"lvn [{node.low_price:.8g},{node.high_price:.8g}]"))

    # --- condition 5: rejection confirms ----------------------------------
    # Confirmations are evaluated against the zone EDGE price travelled to, not
    # the zone centre: that edge is where the reaction would occur, and using the
    # centre would measure a pierce depth from a price inside the thin area.
    level_for_confirm = node.low_price if direction == "SELL" else node.high_price

    from confirm import collect, summarise
    confirmations = collect(list(ctx.confirm_candles), level_for_confirm,
                            direction, ctx.atr)
    summary = summarise(confirmations, minimum=config.S1_MIN_CONFIRMATIONS)
    if not summary["confirmed"]:
        return reject(RejectReason.NOT_CONFIRMED, SETUP, ctx.symbol,
                      direction=direction, level_price=node.center_price,
                      detail=f"{summary['count']}/{summary['required']} confirmations",
                      confirmations=summary["names"])

    # --- condition 6: stop just beyond the thin zone ----------------------
    # Beyond the FAR edge of the node, not its centre. Price inside a low-volume
    # area is still inside the reason for the trade; only a move clean through it
    # says the thin area failed to hold.
    tick = float(ctx.spec.tick_size) if ctx.spec else 0.0
    buffer = max(ctx.atr * config.S1_LVN_STOP_BUFFER_ATR,
                 tick * config.S1_STOP_BUFFER_TICKS)
    if direction == "SELL":
        stop_price = node.high_price + buffer
    else:
        stop_price = node.low_price - buffer

    entry_price = ctx.reference_entry
    if entry_price <= 0:
        return reject(RejectReason.NO_DATA, SETUP, ctx.symbol, direction=direction,
                      detail="no reference entry price")

    if direction == "SELL" and stop_price <= entry_price:
        return reject(RejectReason.STOP_TOO_TIGHT, SETUP, ctx.symbol,
                      direction=direction, level_price=node.center_price,
                      detail=f"stop {stop_price:.8g} <= entry {entry_price:.8g}")
    if direction == "BUY" and stop_price >= entry_price:
        return reject(RejectReason.STOP_TOO_TIGHT, SETUP, ctx.symbol,
                      direction=direction, level_price=node.center_price,
                      detail=f"stop {stop_price:.8g} >= entry {entry_price:.8g}")

    # --- condition 7: the POC is the target -------------------------------
    # Structural by construction. The fixed-R equivalent is recorded alongside so
    # Phase 2 can compare the two targeting modes on identical trades rather than
    # on two different populations.
    target_price = poc
    risk = abs(entry_price - stop_price)
    implied_r = (abs(target_price - entry_price) / risk) if risk > 0 else 0.0

    if direction == "SELL" and target_price >= entry_price:
        return reject(RejectReason.TARGET_TOO_CLOSE, SETUP, ctx.symbol,
                      direction=direction, level_price=node.center_price,
                      detail="poc is not below a short entry")
    if direction == "BUY" and target_price <= entry_price:
        return reject(RejectReason.TARGET_TOO_CLOSE, SETUP, ctx.symbol,
                      direction=direction, level_price=node.center_price,
                      detail="poc is not above a long entry")

    if implied_r < config.TARGET_MIN_R:
        return reject(RejectReason.REWARD_BELOW_MINIMUM, SETUP, ctx.symbol,
                      direction=direction, level_price=node.center_price,
                      detail=(f"poc target is only {implied_r:.2f}R, "
                              f"need {config.TARGET_MIN_R}"))

    return Candidate(
        setup=SETUP,
        symbol=ctx.symbol,
        direction=direction,
        state=SetupState.TRIGGERED,
        level_price=node.center_price,
        level_kind="LVN",
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        session_id=ctx.session_id,
        as_of=ctx.as_of,
        atr=ctx.atr,
        profile_snapshot=ctx.profile_row(),
        attributes={
            "target_kind": "structural:POC",
            "approach_from": approach_from,
            "open_distance_atr": open_rel.distance_atr,
            "lvn_low": node.low_price,
            "lvn_high": node.high_price,
            "lvn_width_bins": node.width_bins,
            "lvn_volume_pct_of_poc": node.volume_pct_of_poc,
            "poc_distance_atr": poc_distance_atr,
            "stop_buffer_atr": buffer / ctx.atr,
            "implied_r": implied_r,
            "confirmations": ",".join(summary["names"]),
            "confirmation_count": summary["count"],
            "confirmation_strength": summary["strength"],
            "value_width_atr": levels.value_width / ctx.atr,
            "poc_prominence": levels.poc_prominence,
            "shape": ctx.prior_shape.label,
        },
    )
