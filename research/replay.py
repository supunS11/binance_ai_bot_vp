"""Phase 1/2: walk history through the LIVE pipeline and measure what happened.

The scanner, the state machine, the four detectors and the gate profiles are imported
from the trading package, not reimplemented. The seam is the data layer
(research/historical.py), so a replayed decision is the same computation a live
decision would have been, on the same inputs.

TWO MODELLING CHOICES DECIDE WHETHER THE OUTPUT IS WORTH ANYTHING.

1. INTRABAR AMBIGUITY IS RESOLVED AGAINST US, ALWAYS.

   When a 1m candle's range contains BOTH the stop and the target, the candle does not
   say which came first. A backtest that assumes the target is not optimistic by a few
   percent - it is systematically wrong in exactly the cases that decide the result,
   because a bar wide enough to contain both is a volatile bar, and volatile bars
   cluster where the trade was going badly. Every such bar is therefore scored as a
   STOP, and the count is reported (`ambiguous_bars`) so the size of the assumption is
   visible rather than buried.

   The honest alternative is sub-minute data, which is not available at this history
   depth. So the pessimistic rule stands, and any edge that only appears under the
   optimistic rule is not an edge.

2. THE ENTRY MUST ACTUALLY BE FILLABLE.

   Entries are posted GTX at or better than the reference price, so a replayed entry
   fills only if price subsequently TRADES THROUGH that price. Assuming every candidate
   fills at its reference is the second classic way to manufacture an edge: the trades
   that would never have filled are disproportionately the ones where price ran away in
   our favour, i.e. the winners. Unfilled candidates are recorded as EXPIRED and
   excluded from R statistics, and the fill rate is reported.

WHAT IS DELIBERATELY NOT MODELLED: funding (recorded separately as a cost, not applied
per trade), liquidation (irrelevant at 0.25% risk and 5x), queue position (a GTX order
at a level is assumed to fill if price trades through it - optimistic, and noted).

Run:
    python -m research.replay --symbols 40 --out phase1
    python -m research.replay --setup S3-BRK --verbose
"""
import argparse
import csv
import logging
import os
import time

import config
import risk
import sessions
from data import klines as klines_mod
from gates import profiles as gate_profiles
from research import dataset, metrics
from research.historical import HistoricalCache, StaticCatalog
from scanner import Scanner
from setups import orderflow_reversal
from state_machine import StateMachine

log = logging.getLogger(__name__)

# How long a replayed entry may wait to be filled, and how long a filled trade may
# run before being marked unresolved. Both in CONFIRM_INTERVAL candles.
MAX_HOLD_BARS = 96              # 24 hours at 15m

TRADE_FIELDS = [
    "symbol", "qv_rank", "session_id", "setup", "direction",
    "decision_ts", "decision_index",
    "entry_price", "stop_price", "target_price", "quantity",
    "risk_distance", "r_multiple", "cost_r",
    "outcome", "exit_price", "exit_ts", "bars_to_exit", "bars_to_fill",
    "gross_r", "net_r", "mfe_r", "mae_r", "ambiguous_bars",
    "atr", "value_width", "prior_shape", "auction_state",
    "open_relationship", "target_kind", "tp2_price", "tp2_kind", "atr15",
    "target_candidates", "entry_mode",
    "stop_mode", "stop_reference",
    "confirmations", "confirmation_count",
    "poc_prominence", "va_range_ratio", "acceptance_ratio", "excursion_candles",
    "value_migration", "bin_delta_normalized", "multi_bin_delta_normalized",
    "vwap_zscore_at_level", "weekly_poc_distance_atr",
    "poor_at_extreme", "excess_at_extreme",
    "stop_inside_hvn", "target_behind_hvn",
    "hvn_entry_in_zone", "hvn_nearest_atr", "hvn_confluence", "hvn_first_test",
    "ofr_tier", "ofr_signals", "ofr_zone_kind", "ofr_first_test",
    "ofr_absorb_bars_before", "ofr_signal_states", "day_bias", "dev_shape_label",
    "ofr_visit_number", "ofr_conviction", "bias_strength",
    "is_control", "control_of",
]

# EVERY FIELD'S TYPE, DECLARED ONCE, because CSV loses all of them and the reader has to
# put them back. merge.py used to carry its own hand-written lists of which columns were
# numeric; they had drifted, leaving `exit_price`, `exit_ts` and `decision_index` as
# strings in the merged dataset. Nothing did arithmetic on those three yet, so the drift
# was invisible - and that is the problem, because the lists would have had to be updated
# by hand every time a field was added here, forever, with nothing checking.
#
# Anything not listed is a string. A test asserts this mapping covers TRADE_FIELDS exactly,
# so adding a field without classifying it fails loudly instead of silently arriving as
# text in a Phase 2 comparison.
TRADE_FIELD_TYPES = {
    "qv_rank": int, "decision_ts": int, "decision_index": int,
    "entry_price": float, "stop_price": float, "target_price": float,
    "quantity": float, "risk_distance": float, "r_multiple": float, "cost_r": float,
    "exit_price": float, "exit_ts": int,
    "bars_to_exit": int, "bars_to_fill": int, "ambiguous_bars": int,
    "gross_r": float, "net_r": float, "mfe_r": float, "mae_r": float,
    "atr": float, "value_width": float,
    "confirmation_count": int,
    "poc_prominence": float, "va_range_ratio": float, "acceptance_ratio": float,
    "excursion_candles": int,
    "bin_delta_normalized": float, "multi_bin_delta_normalized": float,
    "vwap_zscore_at_level": float, "weekly_poc_distance_atr": float,
    "poor_at_extreme": int, "excess_at_extreme": int,
    "stop_inside_hvn": int, "target_behind_hvn": int,
    "hvn_entry_in_zone": int, "hvn_nearest_atr": float, "hvn_confluence": int,
    "hvn_first_test": int,
    "ofr_first_test": int, "ofr_absorb_bars_before": int, "ofr_visit_number": int,
    "ofr_conviction": int, "bias_strength": int,
    "tp2_price": float, "atr15": float,
    "is_control": int,
}


def coerce_row(row):
    """Restore a CSV row's types in place, keeping blanks as None.

    A BLANK MUST NOT BECOME 0.0. An unfilled entry has no net_r, and reading it as
    break-even would mix "this trade never happened" into the mean of trades that did -
    which flatters a setup in exact proportion to how often its entries go unfilled, and
    unfilled candidates are disproportionately the winners (see the fill-requirement note
    in this module's docstring). The same applies to an int: bars_to_exit of None is not
    bars_to_exit of 0.

    "None" as a literal string is handled too: csv writes Python's None as an empty field,
    but a value that was stringified before reaching the writer arrives as the four
    characters instead, and float("None") raises.
    """
    for key, caster in TRADE_FIELD_TYPES.items():
        value = row.get(key)
        if value in (None, "", "None"):
            row[key] = None
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            continue                      # already coerced; merging is idempotent
        # float() first even for ints: CSV may hold "3.0" for an integer column, and
        # int("3.0") raises while int(float("3.0")) does not.
        row[key] = caster(float(value))
    return row


# --------------------------------------------------------------- simulation

def simulate(candidate, forward, spec=None, market_fill=False):
    """First-touch outcome for one candidate against forward 1m candles.

    With `market_fill`, the entry is taken at the first forward candle's open and needs no
    trade-through: a market order fills at the next minute, not at a level.

    Returns a dict. `outcome` is one of:
        EXPIRED      never filled inside the fill window
        TARGET       target touched first
        STOP         stop touched first (INCLUDING every ambiguous bar)
        UNRESOLVED   still open at MAX_HOLD_BARS - excluded from R statistics, because
                     scoring it at the horizon price invents an exit the system would
                     not have taken
    """
    direction = candidate.direction
    entry = float(candidate.entry_price)
    if market_fill and forward:
        entry = float(forward[0].open)
    stop = float(candidate.stop_price)
    target = float(candidate.target_price)
    risk_distance = abs(entry - stop)

    blank = {
        "outcome": "EXPIRED", "exit_price": None, "exit_ts": None,
        "bars_to_exit": None, "bars_to_fill": None, "gross_r": None,
        "net_r": None, "mfe_r": None, "mae_r": None, "ambiguous_bars": 0,
    }
    if risk_distance <= 0 or not forward:
        return blank

    step = klines_mod.INTERVAL_MS["1m"]
    confirm_ms = klines_mod.INTERVAL_MS.get(config.CONFIRM_INTERVAL, 900_000)
    fill_deadline_ms = forward[0].open_time + config.ENTRY_TIMEOUT_SECONDS * 1000
    hold_deadline_ms = None

    filled_at = None
    if market_fill:
        filled_at = 0
        hold_deadline_ms = forward[0].open_time + MAX_HOLD_BARS * confirm_ms
    for index, candle in enumerate(forward):
        if filled_at is not None:
            break
        if candle.open_time >= fill_deadline_ms:
            return blank
        # A passive entry fills only if price TRADES THROUGH it. A buy resting at
        # `entry` needs the low to reach it; a sell needs the high.
        reached = (candle.low <= entry) if direction == "BUY" else (candle.high >= entry)
        if reached:
            filled_at = index
            hold_deadline_ms = candle.open_time + MAX_HOLD_BARS * confirm_ms
            break

    if filled_at is None:
        return blank

    mfe = mae = 0.0
    ambiguous = 0

    for index in range(filled_at, len(forward)):
        candle = forward[index]
        if candle.open_time > hold_deadline_ms:
            return {
                "outcome": "UNRESOLVED", "exit_price": None, "exit_ts": None,
                "bars_to_exit": None, "bars_to_fill": filled_at,
                "gross_r": None, "net_r": None,
                "mfe_r": round(mfe / risk_distance, 5),
                "mae_r": round(mae / risk_distance, 5),
                "ambiguous_bars": ambiguous,
            }

        if direction == "BUY":
            favourable = candle.high - entry
            adverse = entry - candle.low
            hit_stop = candle.low <= stop
            hit_target = candle.high >= target
        else:
            favourable = entry - candle.low
            adverse = candle.high - entry
            hit_stop = candle.high >= stop
            hit_target = candle.low <= target

        mfe = max(mfe, favourable)
        mae = max(mae, adverse)

        if hit_stop and hit_target:
            # THE PESSIMISTIC RULE. The candle contains both levels and cannot say
            # which came first, so it is scored as a loss. See the module docstring -
            # assuming the target here is the standard way a backtest lies.
            ambiguous += 1
            hit_target = False

        if hit_stop or hit_target:
            exit_price = stop if hit_stop else target
            gross = ((exit_price - entry) if direction == "BUY"
                     else (entry - exit_price)) / risk_distance
            cost = metrics.round_trip_cost_r(risk_distance, entry,
                                             candidate.quantity or 1.0)
            return {
                "outcome": "STOP" if hit_stop else "TARGET",
                "exit_price": exit_price,
                "exit_ts": candle.close_time,
                "bars_to_exit": index - filled_at,
                "bars_to_fill": filled_at,
                "gross_r": round(gross, 5),
                "net_r": round(gross - cost, 5),
                "mfe_r": round(mfe / risk_distance, 5),
                "mae_r": round(mae / risk_distance, 5),
                "ambiguous_bars": ambiguous,
            }

    return {
        "outcome": "UNRESOLVED", "exit_price": None, "exit_ts": None,
        "bars_to_exit": None, "bars_to_fill": filled_at,
        "gross_r": None, "net_r": None,
        "mfe_r": round(mfe / risk_distance, 5),
        "mae_r": round(mae / risk_distance, 5),
        "ambiguous_bars": ambiguous,
    }


# ------------------------------------------------------------------- replay

def _bool_to_int(value):
    """None stays None (unmeasured); True/False become 1/0.

    csv.DictWriter stringifies a raw bool as the WORD "True"/"False", and
    coerce_row's float(value) then raises on it - every other boolean-shaped
    field in this schema (is_control) is written as an int for the same reason.
    """
    return None if value is None else int(bool(value))


def _row(candidate, ctx, entry, outcome, qv_rank, is_control=False, control_of=""):
    attributes = candidate.attributes or {}
    snapshot = candidate.profile_snapshot or {}
    return {
        "symbol": candidate.symbol,
        "qv_rank": qv_rank,
        "session_id": candidate.session_id,
        "setup": candidate.setup,
        "direction": candidate.direction,
        "decision_ts": candidate.as_of,
        "decision_index": None,
        "entry_price": candidate.entry_price,
        "stop_price": candidate.stop_price,
        "target_price": candidate.target_price,
        "quantity": candidate.quantity,
        "risk_distance": round(candidate.risk_distance, 10),
        "r_multiple": round(candidate.r_multiple, 5),
        "cost_r": round(candidate.round_trip_cost_r(), 5),
        "atr": candidate.atr,
        "value_width": snapshot.get("prior_value_width"),
        "prior_shape": snapshot.get("shape"),
        "auction_state": snapshot.get("auction_state"),
        "open_relationship": snapshot.get("open_relationship"),
        "target_kind": attributes.get("target_kind"),
        "tp2_price": attributes.get("tp2_price"),
        "tp2_kind": attributes.get("tp2_kind"),
        "atr15": attributes.get("atr15"),
        "target_candidates": attributes.get("target_candidates"),
        "hvn_entry_in_zone": attributes.get("hvn_entry_in_zone"),
        "hvn_nearest_atr": attributes.get("hvn_nearest_atr"),
        "hvn_confluence": attributes.get("hvn_confluence"),
        "hvn_first_test": attributes.get("hvn_first_test"),
        "ofr_tier": attributes.get("ofr_tier"),
        "ofr_signals": attributes.get("ofr_signals"),
        "ofr_zone_kind": attributes.get("ofr_zone_kind"),
        "ofr_first_test": attributes.get("ofr_first_test"),
        "ofr_absorb_bars_before": attributes.get("ofr_absorb_bars_before"),
        "ofr_signal_states": attributes.get("ofr_signal_states"),
        "ofr_visit_number": attributes.get("ofr_visit_number"),
        "ofr_conviction": attributes.get("ofr_conviction"),
        "day_bias": attributes.get("day_bias"),
        "bias_strength": attributes.get("bias_strength"),
        "dev_shape_label": attributes.get("dev_shape_label"),
        # Blank for every setup except S3-BRK, which is the only one with more than one
        # entry rule right now. Present unconditionally, like baseline_source and
        # target_kind, so a future setup with its own mode switch has somewhere to
        # record it without another schema change.
        "entry_mode": attributes.get("entry_mode"),
        "stop_mode": attributes.get("stop_mode"),
        "stop_reference": attributes.get("stop_reference"),
        "confirmations": attributes.get("confirmations"),
        "confirmation_count": attributes.get("confirmation_count"),
        "poc_prominence": snapshot.get("poc_prominence"),
        "va_range_ratio": snapshot.get("va_range_ratio"),
        "acceptance_ratio": attributes.get("volume_rate_ratio"),
        # How many candles the excursion ran before this candidate fired - the raw
        # measurement behind the "quick vs. slow rejection" proxy for a poor extreme
        # (S2_REQUIRE_POOR_EXTREME's own population was too thin to test - finding
        # 18). Recorded now, unconditionally, so a future gate on this can be
        # calibrated against a real distribution instead of a guessed threshold.
        "excursion_candles": attributes.get("excursion_candles"),
        # Session-over-session value migration (profile.relations.classify_migration,
        # folded into profile_snapshot by SetupContext.profile_row) - available for
        # every setup unconditionally, since it costs nothing beyond what the context
        # already computes. Despite gates.checks.htf_value_opposed's docstring calling
        # this "weekly", it reads ctx.migration, which is PRIOR-SESSION-vs-CURRENT, not
        # the separate weekly composite (ctx.weekly_levels) - recorded as measured here.
        "value_migration": snapshot.get("value_migration"),
        "bin_delta_normalized": attributes.get("bin_delta_normalized"),
        "multi_bin_delta_normalized": attributes.get("multi_bin_delta_normalized"),
        # PLAN follow-up from CALIBRATION.md finding 32: distance of the traded
        # level from the prior session's VWAP, in VWAP sigmas. Computed already
        # (profile/levels.py) but never persisted before now - same "measure
        # before gate" pattern as the two delta fields above.
        "vwap_zscore_at_level": attributes.get("vwap_zscore_at_level"),
        # PLAN item 21: signed distance to the previous calendar week's composite
        # POC, in ATR. `ctx.weekly_levels` is new (scanner.frozen_weekly_bundle) -
        # None until a symbol has at least one completed prior calendar week.
        "weekly_poc_distance_atr": attributes.get("weekly_poc_distance_atr"),
        "poor_at_extreme": _bool_to_int(attributes.get("poor_at_extreme")),
        "excess_at_extreme": _bool_to_int(attributes.get("excess_at_extreme")),
        "stop_inside_hvn": _bool_to_int(attributes.get("stop_inside_hvn")),
        "target_behind_hvn": _bool_to_int(attributes.get("target_behind_hvn")),
        "is_control": int(is_control),
        "control_of": control_of,
        **outcome,
    }


S4_PRECHECK_MINUTES = 30


def _s4_zone_near(scanner, cache, symbol, session_id, step):
    """Whether price has touched any S4 zone in the last S4_PRECHECK_MINUTES.

    Exact for S4: the detector rejects a minute as NO_ORDER_FLOW_ZONE unless a zone was
    touched in its last four 5-minute bars, which lie inside this wider window. A minute
    with no touch here cannot produce a candidate, so the expensive context build can be
    skipped for it. Any doubt (no bundle, no candles) keeps the minute for the full path.
    """
    bundle, _reason = scanner.frozen_bundle(symbol, session_id, step)
    if bundle is None:
        return True
    recent = [c for c in cache.minute_candles(symbol, step - S4_PRECHECK_MINUTES * 60_000, step)
              if c.close_time <= step]
    if not recent:
        return True
    zones = orderflow_reversal._zones(bundle.levels, bundle.atr)
    return any(c.low <= z.high and c.high >= z.low for c in recent for z in zones)


def replay_symbol(scanner, cache, catalog, symbol, qv_rank, session_ids,
                  portfolio, apply_gates=True, only_setup=None, step_ms=None):
    """Replay every session for one symbol. Returns (trade rows, reject tally)."""
    rows = []
    rejects = {}
    confirm_ms = klines_mod.INTERVAL_MS.get(config.CONFIRM_INTERVAL, 900_000)
    decision_ms = step_ms or confirm_ms
    spec = catalog.get(symbol)
    if spec is None:
        return rows, rejects

    for session_id in session_ids:
        session_start = sessions.session_start_ms_from_id(session_id)
        session_end = session_start + sessions.MS_DAY

        # A fresh state machine per symbol-session: the session cap and the
        # once-per-session semantics are per symbol-session in live too.
        machine = StateMachine()
        taken = set()

        step = session_start + decision_ms
        while step <= session_end:
            cache.set_as_of(step)
            if only_setup == "S4-OFR" and not _s4_zone_near(scanner, cache, symbol,
                                                           session_id, step):
                rejects["NO_ORDER_FLOW_ZONE"] = rejects.get("NO_ORDER_FLOW_ZONE", 0) + 1
                step += decision_ms
                continue
            try:
                ctx, reason = scanner.build_context(symbol, as_of=step)
            except Exception as exc:                    # noqa: BLE001
                log.debug("%s %s build_context: %s", symbol, session_id, exc)
                rejects["EXCEPTION"] = rejects.get("EXCEPTION", 0) + 1
                step += decision_ms
                continue

            if ctx is None:
                rejects[reason] = rejects.get(reason, 0) + 1
                step += decision_ms
                continue

            candidates, rejections = machine.evaluate(ctx, research_mode=True)
            for rejection in rejections:
                key = rejection.reason.value
                rejects[key] = rejects.get(key, 0) + 1

            for candidate in (candidates or []):
                if only_setup and candidate.setup != only_setup:
                    continue
                # ONE CANDIDATE PER SETUP PER SESSION. A setup whose conditions hold
                # for ten consecutive bars would otherwise contribute ten near-identical
                # trades and dominate the sample with one market event.
                if candidate.setup in taken:
                    continue

                if apply_gates:
                    rejection = gate_profiles.evaluate(candidate, ctx)
                    if rejection is not None:
                        key = f"GATE:{rejection.reason.value}"
                        rejects[key] = rejects.get(key, 0) + 1
                        continue

                rejection = risk.size(candidate, portfolio, spec)
                if rejection is not None:
                    key = f"SIZE:{rejection.reason.value}"
                    rejects[key] = rejects.get(key, 0) + 1
                    continue

                taken.add(candidate.setup)
                forward = cache.forward_candles(
                    symbol, candidate.as_of + 1,
                    candidate.as_of + MAX_HOLD_BARS * confirm_ms
                    + config.ENTRY_TIMEOUT_SECONDS * 1000 + 2 * confirm_ms)
                outcome = simulate(candidate, forward, spec)
                rows.append(_row(candidate, ctx, candidate.entry_price, outcome,
                                 qv_rank))

            step += decision_ms

        cache.clear_frozen()
    cache.release(symbol)
    return rows, rejects


_LIVE_ENABLE_FLAGS = ("S1_POC_ENABLED", "S1_LVN_ENABLED", "S2_ENABLED", "S3_ENABLED")


def run(root=None, limit_symbols=None, limit_sessions=None, apply_gates=True,
        only_setup=None, equity=10_000.0, shard=None, shards=1,
        force_enable_setups=True, step_minutes=None):
    """Replay history through the live pipeline.

    `force_enable_setups=True` (the default) makes every setup evaluable regardless of
    its S*_ENABLED shipped default. THIS MUST STAY THE DEFAULT, or research quietly goes
    blind the moment a setup fails its phase gate and gets disabled for live safety.

    CALIBRATION.md finding 15: Phase 1 + the matched-random control found no setup beats
    a random entry at the same location, and shipped every S*_ENABLED default as False.
    That is a statement about whether to TRADE a setup, decided by risk.check_limits and
    main.py's live path - it is not a statement about whether the setup's DETECTION LOGIC
    should be measurable. A disabled setup is exactly the one a re-run, a fix, or Phase 3's
    ablation most needs to see. Without this override, `S*_ENABLED=False` would make
    every setup's `detect()` return SETUP_DISABLED unconditionally, and every future
    replay - including the one that would prove a fix worked - would silently produce
    zero candidates for it.

    Pass `force_enable_setups=False` only to deliberately reproduce what the LIVE bot
    would do with today's shipped flags - a live-fidelity check, not ordinary research.
    """
    if force_enable_setups:
        for flag in _LIVE_ENABLE_FLAGS:
            setattr(config, flag, True)

    root = root or config.PARQUET_DIR
    specs = dataset.load_specs(root)
    if not specs:
        raise SystemExit("no corpus - run research.dataset first")

    cache = HistoricalCache(root=root, strict=False)
    catalog = StaticCatalog(specs)
    universe = dataset.load_universe(root) or [
        {"symbol": s.symbol, "qv_rank": i, "quote_volume": 0.0}
        for i, s in enumerate(specs, start=1)]

    scanner = Scanner(rest=None, catalog=catalog, cache=cache, journal=None)
    portfolio = risk.PortfolioState(equity=equity, available_balance=equity)

    entries = [row for row in universe if catalog.get(row["symbol"])]
    if limit_symbols:
        entries = entries[:limit_symbols]

    # SHARD BY STRIDE, not by contiguous block. The universe is ranked by volume, so
    # contiguous blocks would give one worker all the liquid symbols and another all the
    # thin ones - unequal runtimes, and a partial result biased by liquidity if a worker
    # fails. A stride interleaves them.
    if shards and shards > 1 and shard is not None:
        entries = entries[shard::shards]
        log.info("shard %d/%d: %d symbols", shard, shards, len(entries))

    all_rows, all_rejects = [], {}
    started = time.time()

    for index, entry in enumerate(entries, start=1):
        symbol = entry["symbol"]
        available = cache.available_sessions(symbol)
        session_ids = available[1:]
        if limit_sessions:
            session_ids = session_ids[-limit_sessions:]
        if not session_ids:
            continue

        rows, rejects = replay_symbol(scanner, cache, catalog, symbol,
                                      entry["qv_rank"], session_ids, portfolio,
                                      apply_gates=apply_gates,
                                      only_setup=only_setup,
                                      step_ms=step_minutes * 60_000 if step_minutes else None)
        all_rows.extend(rows)
        for key, count in rejects.items():
            all_rejects[key] = all_rejects.get(key, 0) + count

        elapsed = time.time() - started
        log.info("[%d/%d] %-14s %d trades (%d total) %.0fs",
                 index, len(entries), symbol, len(rows), len(all_rows), elapsed)

    return all_rows, all_rejects


# ------------------------------------------------------------------ reporting

def report(rows, rejects=None, label="PHASE 1/2"):
    print(f"\n{'=' * 78}\n{label}   {len(rows)} candidates\n{'=' * 78}")
    if not rows:
        print("no candidates produced")
        if rejects:
            _print_rejects(rejects)
        return

    filled = [r for r in rows if r["outcome"] in ("TARGET", "STOP")]
    expired = [r for r in rows if r["outcome"] == "EXPIRED"]
    unresolved = [r for r in rows if r["outcome"] == "UNRESOLVED"]
    ambiguous = sum(r["ambiguous_bars"] or 0 for r in rows)

    print(f"\nfill rate        {len(filled) + len(unresolved)}/{len(rows)} "
          f"({(len(filled) + len(unresolved)) / len(rows) * 100:.1f}%)   "
          f"expired {len(expired)}")
    print(f"resolved         {len(filled)}   unresolved {len(unresolved)} "
          f"(excluded from R)")
    print(f"ambiguous bars   {ambiguous}  <- every one scored as a STOP "
          f"(pessimistic; see module docstring)")

    if not filled:
        print("\nno resolved trades - nothing to measure")
        if rejects:
            _print_rejects(rejects)
        return

    by_setup = metrics.group_by(filled, "setup")

    print(f"\n{'-' * 78}\nNET R BY SETUP (net of fees and modelled slippage)\n{'-' * 78}")
    print(f"  intervals are CLUSTER-ROBUST, clustered on UTC session: same-day trades")
    print(f"  across the alt complex are one market move, not independent evidence.")
    print(f"  g = clusters.  * = lower bound clears zero.  ~ = would have been starred")
    print(f"  under the independence assumption, but is not once correlation is paid for.")
    print()
    print(f"{'setup':<26}{'n':<7}{'g':<6}{'mean R':<14}{'95% CI':<22}"
          f"{'win':<8}{'payoff':<8}{'totalR'}")
    summaries = {}
    for setup in sorted(by_setup):
        summaries[setup] = metrics.summarise_r(by_setup[setup])
        print(metrics.format_r_summary(setup, summaries[setup]))
    print(metrics.format_r_summary("ALL", metrics.summarise_r(filled)))

    # WHAT THE NAIVE INTERVAL WAS CLAIMING, stated as a number. A large inflation factor
    # means the nominal trade count was mostly repetition of the same few market days.
    inflations = [summary["se_inflation"] for summary in summaries.values()
                  if summary.get("n")]
    if inflations:
        print(f"\n  clustering widened the standard errors by "
              f"{min(inflations):.2f}x to {max(inflations):.2f}x")
        demoted = [setup for setup, summary in summaries.items()
                   if summary.get("significant_naive") and not summary.get("significant")]
        if demoted:
            print(f"  NO LONGER SIGNIFICANT once same-day correlation is paid for: "
                  f"{', '.join(sorted(demoted))}")

    # MULTIPLICITY. This report runs one test per setup, plus one per direction within
    # each setup, plus the split halves. At a nominal 95% each, the chance that AT LEAST
    # ONE fires by chance is far above 5%: with sixteen independent tests it is about 56%.
    # A single star among many comparisons is therefore not evidence, and the number of
    # comparisons has to be printed next to the stars or it will be forgotten.
    comparisons = len(by_setup) * 3 + len(by_setup)
    family_risk = 1.0 - (0.95 ** max(comparisons, 1))
    strict_z = metrics.bonferroni_z(len(by_setup))
    print(f"\n  MULTIPLICITY: ~{comparisons} comparisons in this report. "
          f"P(at least one false star) ~= {family_risk*100:.0f}%")
    print(f"  Primary gate is the per-setup pooled mean ({len(by_setup)} tests), so the "
          f"corrected bar is z={strict_z:.2f}, not 1.96:")
    for setup in sorted(by_setup):
        strict = metrics.summarise_r(by_setup[setup], z=strict_z)
        verdict = "CLEARS corrected bar" if strict["significant"] else "does not clear"
        print(f"    {setup:<24} corrected CI "
              f"[{strict['r_low']:>+7.4f},{strict['r_high']:>+7.4f}]  {verdict}")
    print(f"  Replication and the matched-random control are still the real tests - a "
          f"corrected\n  p-value is a weaker guarantee than a result that holds in both "
          f"halves and beats its twin.")

    print(f"\n{'-' * 78}\nSPLIT-HALF BY TIME (does it hold in both halves?)\n{'-' * 78}")
    for setup in sorted(by_setup):
        group = [dict(r, _t=r["decision_ts"]) for r in by_setup[setup]]
        split = metrics.split_half(group, "_t", lambda r: r["net_r"])
        if split.get("consistent") is None or "first" not in split:
            print(f"  {setup:<24} {split.get('reason', 'n/a')}")
            continue
        first, second = split["first"], split["second"]
        flag = "consistent" if split["consistent"] else "FLIPS"
        print(f"  {setup:<24} early {first['mean']:+.4f} (n={first['n']})  "
              f"late {second['mean']:+.4f} (n={second['n']})  -> {flag}")

    print(f"\n{'-' * 78}\nDIRECTION BREAKDOWN\n{'-' * 78}")
    for setup in sorted(by_setup):
        for direction in ("BUY", "SELL"):
            group = [r for r in by_setup[setup] if r["direction"] == direction]
            if group:
                print(metrics.format_r_summary(f"  {setup} {direction}",
                                               metrics.summarise_r(group)))

    if rejects:
        _print_rejects(rejects)


def _print_rejects(rejects, top=18):
    print(f"\n{'-' * 78}\nREJECT FUNNEL (top {top})\n{'-' * 78}")
    total = sum(rejects.values())
    for key in sorted(rejects, key=lambda k: -rejects[k])[:top]:
        print(f"  {key:<42} {rejects[key]:>9d}  {rejects[key]/total*100:>5.1f}%")


def write_csv(path, rows):
    if not rows:
        return None
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRADE_FIELDS,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Replay history through the live pipeline.")
    parser.add_argument("--root", default=None)
    parser.add_argument("--out", default="phase1")
    parser.add_argument("--symbols", type=int, default=None,
                        help="limit to the first N symbols by rank")
    parser.add_argument("--sessions", type=int, default=None,
                        help="limit to the most recent N sessions per symbol")
    parser.add_argument("--setup", default=None, help="replay one setup only")
    parser.add_argument("--step-minutes", type=int, default=None,
                        help="evaluate every N minutes instead of CONFIRM_INTERVAL "
                             "(holding and fill windows are unchanged)")
    parser.add_argument("--s3-entry-mode", default=None, choices=("confirmed", "accepted"),
                        help="override config.S3_ENTRY_MODE for this run - "
                             "'confirmed' (default behaviour) or 'accepted' "
                             "(CALIBRATION.md finding 15's experiment). Left alone "
                             "unless passed, so an ordinary run is unaffected.")
    parser.add_argument("--s1-stop-mode", default=None, choices=("buffer", "lvn"),
                        help="override config.S1_STOP_MODE for this run - 'buffer' "
                             "(default, fixed ATR/tick buffer past the POC) or 'lvn' "
                             "(stop past the nearest thin area - CALIBRATION.md's "
                             "POC-rotation diagnosis). Left alone unless passed.")
    parser.add_argument("--s2-stop-mode", default=None, choices=("extreme", "lvn"),
                        help="override config.S2_STOP_MODE for this run - 'extreme' "
                             "(default, buffer past the excursion's own extreme) or "
                             "'lvn' (stop past a thin area further out, falling back "
                             "to 'extreme' behaviour when none qualifies). Left alone "
                             "unless passed.")
    parser.add_argument("--target-mode", default=None, choices=("fixed_r", "structural"),
                        help="override config.TARGET_MODE for this run - 'fixed_r' "
                             "(default, tests the source material's stated 2R claim) "
                             "or 'structural' (POC/opposite value edge/naked POC - the "
                             "measured challenger, see setups/base.py). Left alone "
                             "unless passed.")
    parser.add_argument("--confirm-interval", default=None,
                        help="override config.CONFIRM_INTERVAL for this run (e.g. "
                             "'5m', '1h') - PLAN item 7. Also rescales every "
                             "candle-count threshold that reads it (ACCEPT_MIN_CANDLES, "
                             "ACCEPT_MIN_CANDLES_OUTSIDE, REJECT_MAX_CANDLES_OUTSIDE, "
                             "ACCEPT_MIN_BASELINE_CANDLES, CONFIRM_CONSECUTIVE_CANDLES) "
                             "to the SAME real-world duration at the new interval, so "
                             "the ablation tests interval granularity rather than "
                             "silently redefining what each threshold means. Left "
                             "alone unless passed.")
    parser.add_argument("--target-include-hvn", action="store_true",
                        help="override config.TARGET_INCLUDE_HVN to True for this run - "
                             "PLAN item 17, adds each HVN's peak price to "
                             "structural_target()'s candidate set. Left alone (False) "
                             "unless passed.")
    parser.add_argument("--target-include-prior-extreme", action="store_true",
                        help="override config.TARGET_INCLUDE_PRIOR_EXTREME to True for "
                             "this run - PLAN item 17, adds the prior session's opposite "
                             "extreme (high for a BUY, low for a SELL) to "
                             "structural_target()'s candidate set. Left alone (False) "
                             "unless passed.")
    parser.add_argument("--target-prefer-near", action="store_true",
                        help="override config.TARGET_PREFER_NEAR_LEVEL to True for "
                             "this run - PLAN item 25, ranks the session's own "
                             "POC/VAH-VAL first and only falls through to naked "
                             "POCs/HVNs/prior extreme when nothing near clears "
                             "TARGET_MIN_R. Left alone (False) unless passed.")
    parser.add_argument("--no-gates", action="store_true",
                        help="skip gates - measures the raw detector population")
    parser.add_argument("--respect-live-flags", action="store_true",
                        help="do NOT force-enable setups disabled by S*_ENABLED - "
                             "reproduces what the live bot would trade today. Ordinary "
                             "research wants the default (force-enabled).")
    parser.add_argument("--shard", type=int, default=None,
                        help="this worker's index, 0-based")
    parser.add_argument("--shards", type=int, default=1,
                        help="total worker count; symbols are strided, not blocked")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)sZ %(levelname)s %(message)s")
    logging.Formatter.converter = time.gmtime

    if args.s3_entry_mode is not None:
        config.S3_ENTRY_MODE = args.s3_entry_mode
        log.info("S3_ENTRY_MODE overridden to %r for this run", args.s3_entry_mode)
    if args.target_mode is not None:
        config.TARGET_MODE = args.target_mode
        log.info("TARGET_MODE overridden to %r for this run", args.target_mode)
    if args.s1_stop_mode is not None:
        config.S1_STOP_MODE = args.s1_stop_mode
        log.info("S1_STOP_MODE overridden to %r for this run", args.s1_stop_mode)
    if args.s2_stop_mode is not None:
        config.S2_STOP_MODE = args.s2_stop_mode
        log.info("S2_STOP_MODE overridden to %r for this run", args.s2_stop_mode)
    if args.target_include_hvn:
        config.TARGET_INCLUDE_HVN = True
        log.info("TARGET_INCLUDE_HVN overridden to True for this run")
    if args.target_include_prior_extreme:
        config.TARGET_INCLUDE_PRIOR_EXTREME = True
        log.info("TARGET_INCLUDE_PRIOR_EXTREME overridden to True for this run")
    if args.target_prefer_near:
        config.TARGET_PREFER_NEAR_LEVEL = True
        log.info("TARGET_PREFER_NEAR_LEVEL overridden to True for this run")
    if args.confirm_interval is not None:
        config.CONFIRM_INTERVAL = args.confirm_interval
        config.CONFIRM_INTERVAL_MINUTES = config.interval_minutes(args.confirm_interval)
        config.ACCEPT_MIN_CANDLES = config.candles_for_minutes(45)
        config.ACCEPT_MIN_CANDLES_OUTSIDE = config.candles_for_minutes(195)
        config.REJECT_MAX_CANDLES_OUTSIDE = config.candles_for_minutes(75)
        config.ACCEPT_MIN_BASELINE_CANDLES = config.candles_for_minutes(90)
        config.CONFIRM_CONSECUTIVE_CANDLES = config.candles_for_minutes(30)
        log.info("CONFIRM_INTERVAL overridden to %r (%d min/candle) for this run - "
                 "candle-count thresholds rescaled to %d/%d/%d/%d/%d "
                 "(accept_min/accept_outside/reject_outside/baseline/consecutive)",
                 args.confirm_interval, config.CONFIRM_INTERVAL_MINUTES,
                 config.ACCEPT_MIN_CANDLES, config.ACCEPT_MIN_CANDLES_OUTSIDE,
                 config.REJECT_MAX_CANDLES_OUTSIDE, config.ACCEPT_MIN_BASELINE_CANDLES,
                 config.CONFIRM_CONSECUTIVE_CANDLES)

    rows, rejects = run(root=args.root, limit_symbols=args.symbols,
                        limit_sessions=args.sessions,
                        apply_gates=not args.no_gates, only_setup=args.setup,
                        shard=args.shard, shards=args.shards,
                        force_enable_setups=not args.respect_live_flags,
                        step_minutes=args.step_minutes)

    name = ("trades.csv" if args.shard is None
            else f"trades_shard{args.shard}.csv")
    path = write_csv(os.path.join(args.out, name), rows)

    if args.shard is not None:
        # A shard writes its reject tally too, so the merged funnel is complete.
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, f"rejects_shard{args.shard}.csv"),
                  "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["reason", "count"])
            for key in sorted(rejects):
                writer.writerow([key, rejects[key]])
        print(f"shard {args.shard}: {len(rows)} trades -> {path}")
        return 0

    report(rows, rejects)
    if path:
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
