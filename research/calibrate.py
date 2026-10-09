"""Phase 0: replace every argued threshold with an observed one.

Two sweeps, because the thresholds in config.py are two different kinds of thing.

SWEEP A - PROFILE DISTRIBUTIONS (one row per symbol-session)
    Builds each completed session's profile through the LIVE Scanner and records
    every raw measurement: elongation, POC position, migration, prominence, stability
    shifts, value width, occupied bins. These thresholds are descriptive - they select
    a population ("the most elongated fifth of sessions"), so the number to write in
    config is that distribution's percentile. Nothing here needs an outcome.

SWEEP B - ACCEPTANCE, LABELLED BY OUTCOME (one row per excursion)
    The acceptance thresholds are NOT descriptive and a percentile is the wrong
    instrument for them. `ACCEPT_MIN_VOLUME_RATE_RATIO` makes a claim about the
    world - that heavy volume outside value predicts CONTINUATION while thin volume
    predicts REVERSION - so it has to be calibrated against what price actually did
    next. Every excursion is therefore recorded at its decision point together with
    its forward outcome, which turns threshold choice into a measurable question:
    where does the ratio best separate the two populations, and does it separate them
    at all?

    That second half matters more than the first. If reverted and continued
    excursions have the SAME ratio distribution, then no threshold anywhere makes S2
    and S3 different bets, and the finding is that the discriminator does not
    discriminate on real data - which Phase 1 would otherwise spend a month
    discovering as two simultaneous nulls.

THE DECISION POINT IS THE MOMENT THE SYSTEM WOULD HAVE HAD TO CHOOSE: the candle at
which the excursion first reaches ACCEPT_MIN_CANDLES consecutive closes outside
value. Measuring at the excursion's extreme instead would be lookahead - the extreme
is only knowable afterwards, and calibrating on it produces thresholds that cannot be
applied live.

SWEEP C - SHAPE BIAS vs NEXT SESSION (one row per symbol-session)
    day_bias(shape) - P/b's POC-position skew, trend's elongation/migration -
    makes a directional claim BIAS_FILTER_ENABLED gates real S4-OFR trades on
    (at watch-start, and since today's fix, re-checked at entry too). Sweeps A
    and B do not test it: A is the shape's own distribution, with no outcome;
    B calibrates a different threshold entirely. This sweep tests the claim
    itself against the FOLLOWING session's own price action, decoupled from
    whether S4-OFR happened to trade it - see sweep_shape_bias's docstring.

Run:
    python -m research.calibrate --sweep profile
    python -m research.calibrate --sweep acceptance
    python -m research.calibrate --sweep shape_bias
    python -m research.calibrate --sweep both --out calib
"""
import argparse
import csv
import logging
import os
import time

import config
import sessions
from data import klines as klines_mod
from profile import builder, levels as levels_mod, relations, shape as shape_mod
import acceptance as acceptance_mod
from research import dataset, metrics
from research.historical import HistoricalCache, StaticCatalog
from scanner import Scanner

log = logging.getLogger(__name__)


# --------------------------------------------------------------------- setup

def open_corpus(root=None, strict=False):
    """Load the persisted corpus: (cache, catalog, universe)."""
    root = root or config.PARQUET_DIR
    specs = dataset.load_specs(root)
    if not specs:
        raise SystemExit(
            f"no specs.csv under {root!r} - run research.dataset first:\n"
            f"    python -m research.dataset --days 120 --universe-size 120")
    cache = HistoricalCache(root=root, strict=strict)
    catalog = StaticCatalog(specs)
    universe = dataset.load_universe(root)
    if not universe:
        universe = [{"symbol": spec.symbol, "qv_rank": index, "quote_volume": 0.0}
                    for index, spec in enumerate(specs, start=1)]
    return cache, catalog, universe


def corpus_sessions(cache, symbol):
    """Session ids available for a symbol, EXCLUDING the oldest.

    The oldest has no predecessor on disk, and every measurement here is either of a
    session or of a session against its predecessor's value area.
    """
    available = cache.available_sessions(symbol)
    return available[1:] if len(available) > 1 else []


# ------------------------------------------------------- sweep A: profiles

PROFILE_FIELDS = [
    "symbol", "qv_rank", "session_id", "session_start_ms",
    "candles", "coverage", "quote_volume", "trades",
    "atr", "bin_size", "occupied_bins", "value_fraction",
    "session_range", "value_width", "value_width_atr", "va_range_ratio",
    "poc_position", "poc_prominence", "intra_poc_migration_atr",
    "shape", "auction_state", "bimodal",
    "excess_high", "excess_low", "poor_high", "poor_low", "single_prints",
    "hvn_count", "lvn_count",
    "poc_shift_bins", "vah_shift_bins", "val_shift_bins",
    "poc_price", "vah", "val",
]


def profile_row(scanner, cache, symbol, qv_rank, session_id):
    """Measure one completed session exactly as live would, at the next session's open.

    The clock is set to the following session's open, which is when live actually
    computes this profile as its frozen reference - so ATR, bin width and every level
    match what the bot would have used, rather than what a tidier
    hindsight computation would produce.
    """
    session_start = sessions.session_start_ms_from_id(session_id)
    next_start = session_start + sessions.MS_DAY

    cache.set_as_of(next_start)
    cache.clear_frozen()

    bundle, reason = scanner.frozen_bundle(symbol, sessions.session_id(next_start),
                                           next_start)
    if bundle is None:
        return None, reason

    profile, levels = bundle.profile, bundle.levels
    shape, stability = bundle.shape, bundle.stability
    if shape is None:
        return None, "NO_SHAPE"

    atr = bundle.atr
    return {
        "symbol": symbol,
        "qv_rank": qv_rank,
        "session_id": session_id,
        "session_start_ms": session_start,
        "candles": profile.candle_count,
        "coverage": round(profile.coverage, 4),
        "quote_volume": round(profile.total_quote_volume, 2),
        "trades": profile.total_trades,
        "atr": atr,
        "bin_size": bundle.bin_size,
        "occupied_bins": len(profile.volume),
        "value_fraction": round(levels.value_fraction, 4),
        "session_range": profile.range,
        "value_width": levels.value_width,
        "value_width_atr": (levels.value_width / atr) if atr > 0 else None,
        "va_range_ratio": round(shape.va_range_ratio, 4),
        "poc_position": round(shape.poc_position, 4),
        "poc_prominence": round(levels.poc_prominence, 4),
        "intra_poc_migration_atr": (
            round(shape.intra_poc_migration_atr, 4)
            if shape.intra_poc_migration_atr is not None else None),
        "shape": shape.label,
        "auction_state": shape.auction_state,
        "bimodal": int(shape.bimodal),
        "excess_high": int(shape.excess_high),
        "excess_low": int(shape.excess_low),
        "poor_high": int(shape.poor_high),
        "poor_low": int(shape.poor_low),
        "single_prints": len(shape.single_print_bins),
        "hvn_count": len(levels.hvns),
        "lvn_count": len(levels.lvns),
        "poc_shift_bins": round(stability.poc_shift_bins, 4),
        "vah_shift_bins": round(stability.vah_shift_bins, 4),
        "val_shift_bins": round(stability.val_shift_bins, 4),
        "poc_price": levels.poc_price,
        "vah": levels.vah,
        "val": levels.val,
    }, None


def sweep_profiles(cache, catalog, universe, limit_symbols=None):
    """Sweep every symbol-session in the corpus. Returns (rows, refusals)."""
    scanner = Scanner(rest=None, catalog=catalog, cache=cache, journal=None)
    rows, refusals = [], {}
    symbols = [row for row in universe if catalog.get(row["symbol"])]
    if limit_symbols:
        symbols = symbols[:limit_symbols]

    started = time.time()
    for index, entry in enumerate(symbols, start=1):
        symbol = entry["symbol"]
        session_ids = corpus_sessions(cache, symbol)
        for session_id in session_ids:
            try:
                row, reason = profile_row(scanner, cache, symbol,
                                          entry["qv_rank"], session_id)
            except Exception as exc:                        # noqa: BLE001
                log.debug("%s %s failed: %s", symbol, session_id, exc)
                refusals["EXCEPTION"] = refusals.get("EXCEPTION", 0) + 1
                continue
            if row is None:
                refusals[reason] = refusals.get(reason, 0) + 1
                continue
            rows.append(row)
        cache.release(symbol)
        if index % 10 == 0 or index == len(symbols):
            log.info("profiles: %d/%d symbols, %d rows, %.0fs",
                     index, len(symbols), len(rows), time.time() - started)
    return rows, refusals


# ----------------------------------------------- sweep B: acceptance events

ACCEPTANCE_FIELDS = [
    "symbol", "qv_rank", "session_id", "side",
    # excursion_id groups rows belonging to ONE run, which only matters in
    # --every-candle mode where a run contributes many rows. In the default mode it is
    # a run counter and every row is its own excursion.
    "excursion_id", "candles_into_run",
    "decision_ts", "decision_index", "consecutive_closes",
    "volume_rate_ratio", "outside_rate", "session_rate", "baseline_source",
    "volume_outside_fraction",
    "excursion_distance_atr", "excursion_distance_va",
    "atr", "value_width", "prior_shape",
    "decision_price", "vah", "val",
    # Forward outcome, measured AFTER the decision candle.
    "bars_forward", "returned_inside", "bars_to_return",
    "max_continuation_va", "max_adverse_va", "close_at_horizon_va",
    "outcome",
]

# Horizon for the forward outcome, in confirmation candles. At the 15m default this
# is 6 hours - long enough for a genuine break to extend, short enough that the
# outcome still belongs to the same session rather than to the next day's auction.
DEFAULT_HORIZON = 24


def _excursion_events(confirm_candles, prior_levels, atr, value_width,
                      horizon=DEFAULT_HORIZON, min_candles=None, prior_rate=0.0,
                      every_candle=False):
    """Every excursion beyond prior value, measured at its decision point.

    Walks the session forward. An excursion is a contiguous run of closes on one side
    of the prior value area; its DECISION POINT is the candle at which that run first
    reaches `min_candles` closes, because that is the first bar on which the live
    system could have acted. One event per excursion - re-measuring the same run on
    every later candle would count one move many times and weight the sample toward
    long excursions.

    `every_candle=True` deliberately lifts that restriction, for one specific question
    the default sampling CANNOT answer. Firing once per run pins
    `consecutive_closes` to exactly `min_candles` in every row, so the corpus measures
    acceptance only at the earliest and noisiest instant it can be measured. But
    acceptance in Market Profile is a claim about TIME: value is supposed to BUILD
    outside the area, and the live scanner re-evaluates a long-running excursion on
    every cycle rather than only on its third candle. So the default corpus is both a
    weaker test of the idea than the idea deserves and a narrower population than live
    trading actually reaches.

    Rows from this mode are NOT independent - one excursion contributes many, and long
    excursions contribute most - so `excursion_id` is emitted to group them, and any
    pooled statistic over this mode must cluster on it or weight by it. It exists to
    answer "does the measurement improve with more candles", not to re-estimate any
    base rate.
    """
    min_candles = (config.ACCEPT_MIN_CANDLES if min_candles is None else min_candles)
    events = []
    run_side, run_length = "NONE", 0
    fired = False
    run_serial = 0

    for index, candle in enumerate(confirm_candles):
        side = acceptance_mod._current_side(candle.close, prior_levels)

        if side != run_side:
            run_side, run_length, fired = side, 0, False
            if side != "NONE":
                run_serial += 1
        run_length += 1 if side != "NONE" else 0

        if side == "NONE" or run_length < min_candles:
            continue
        if fired and not every_candle:
            continue

        # The decision bar. Measure with the SAME function the live path uses, fed
        # only candles up to and including this one.
        window = confirm_candles[:index + 1]
        verdict = acceptance_mod.evaluate(window, prior_levels, atr,
                                         prior_rate=prior_rate)
        if verdict.side != side:
            continue
        fired = True

        forward = confirm_candles[index + 1:index + 1 + horizon]
        outcome = _forward_outcome(candle, forward, prior_levels, side, value_width)
        events.append({
            "side": side,
            "excursion_id": run_serial,
            "candles_into_run": run_length,
            "decision_ts": candle.open_time,
            "decision_index": index,
            "consecutive_closes": verdict.consecutive_closes_outside,
            "volume_rate_ratio": round(verdict.volume_rate_ratio, 5),
            "outside_rate": round(verdict.outside_rate, 6),
            "session_rate": round(verdict.session_rate, 6),
            "baseline_source": verdict.baseline_source,
            "volume_outside_fraction": round(verdict.volume_outside_fraction, 5),
            "excursion_distance_atr": round(verdict.excursion_distance_atr, 5),
            "excursion_distance_va": (
                round(verdict.excursion_distance / value_width, 5)
                if value_width > 0 else None),
            "decision_price": candle.close,
            **outcome,
        })
    return events


def _forward_outcome(decision_candle, forward, prior_levels, side, value_width):
    """What price did after the decision bar, in value-width units.

    Denominated in VALUE WIDTH rather than ATR for the reason CALIBRATION.md finding 5
    records: this is a distance between profile levels, and daily ATR is a 14-day
    average that can differ from one session's value width by ~10x.

    `outcome` collapses it to a three-way label for threshold selection:
        CONTINUED  extended at least half a value width further away, without first
                   closing back inside value
        REVERTED   closed back inside the value area
        NEITHER    did neither inside the horizon - a real third case, and lumping it
                   into either bucket would overstate whichever one it joined
    """
    entry = decision_candle.close
    empty = {
        "bars_forward": 0, "returned_inside": 0, "bars_to_return": None,
        "max_continuation_va": None, "max_adverse_va": None,
        "close_at_horizon_va": None, "outcome": "NO_FORWARD_DATA",
    }
    if not forward or value_width <= 0:
        return empty

    sign = 1.0 if side == "ABOVE" else -1.0
    best_continuation = 0.0
    worst_adverse = 0.0
    bars_to_return = None

    for offset, candle in enumerate(forward, start=1):
        favourable = (candle.high - entry) if side == "ABOVE" else (entry - candle.low)
        adverse = (entry - candle.low) if side == "ABOVE" else (candle.high - entry)
        best_continuation = max(best_continuation, favourable)
        worst_adverse = max(worst_adverse, adverse)
        if (bars_to_return is None
                and acceptance_mod._current_side(candle.close, prior_levels) == "NONE"):
            bars_to_return = offset

    continuation_va = best_continuation / value_width
    adverse_va = worst_adverse / value_width
    horizon_close = forward[-1].close
    close_va = sign * (horizon_close - entry) / value_width

    # Order matters: a return inside value is checked FIRST, because a move that came
    # back into value and only then extended is not a clean continuation - it is the
    # ambiguous case S3 exists to avoid, and counting it as CONTINUED would flatter
    # the breakout thesis.
    if bars_to_return is not None:
        outcome = "REVERTED"
    elif continuation_va >= 0.5:
        outcome = "CONTINUED"
    else:
        outcome = "NEITHER"

    return {
        "bars_forward": len(forward),
        "returned_inside": int(bars_to_return is not None),
        "bars_to_return": bars_to_return,
        "max_continuation_va": round(continuation_va, 5),
        "max_adverse_va": round(adverse_va, 5),
        "close_at_horizon_va": round(close_va, 5),
        "outcome": outcome,
    }


def acceptance_rows(scanner, cache, symbol, qv_rank, session_id,
                    horizon=DEFAULT_HORIZON, every_candle=False):
    """Excursion events for one developing session against the prior value area."""
    session_start = sessions.session_start_ms_from_id(session_id)
    session_end = session_start + sessions.MS_DAY

    # Prior profile as live would have it: computed at THIS session's open.
    cache.set_as_of(session_start)
    cache.clear_frozen()
    bundle, reason = scanner.frozen_bundle(symbol, session_id, session_start)
    if bundle is None:
        return [], reason
    if bundle.shape is None:
        return [], "NO_SHAPE"

    # The developing session, whole - the forward outcome legitimately needs candles
    # after each decision point, and each event's own measurement is bounded to its
    # decision bar inside _excursion_events.
    cache.set_as_of(session_end)
    minute_candles = cache.minute_candles(symbol, session_start, session_end)
    if len(minute_candles) < config.PROFILE_MIN_CANDLES:
        return [], "THIN_DEVELOPING_SESSION"

    confirm = klines_mod.resample(minute_candles, config.CONFIRM_INTERVAL, "1m")
    if len(confirm) < config.ACCEPT_MIN_CANDLES + 2:
        return [], "NO_CONFIRM_CANDLES"

    value_width = bundle.levels.value_width

    # The fallback baseline, computed exactly as SetupContext.prior_confirm_rate does,
    # so replay and live feed the discriminator the same denominator.
    confirm_ms = klines_mod.INTERVAL_MS.get(config.CONFIRM_INTERVAL, 900_000)
    prior_rate = 0.0
    if bundle.profile.candle_count and bundle.profile.total_volume > 0:
        source_ms = klines_mod.INTERVAL_MS.get(
            bundle.profile.source_interval or "1m", 60_000)
        prior_rate = (bundle.profile.total_volume / bundle.profile.candle_count
                      * (confirm_ms / source_ms))

    events = _excursion_events(confirm, bundle.levels, bundle.atr, value_width,
                               horizon=horizon, prior_rate=prior_rate,
                               every_candle=every_candle)

    out = []
    for event in events:
        out.append({
            "symbol": symbol, "qv_rank": qv_rank, "session_id": session_id,
            "atr": bundle.atr, "value_width": value_width,
            "prior_shape": bundle.shape.label,
            "vah": bundle.levels.vah, "val": bundle.levels.val,
            **event,
        })
    return out, None


def sweep_acceptance(cache, catalog, universe, limit_symbols=None,
                     horizon=DEFAULT_HORIZON, every_candle=False):
    scanner = Scanner(rest=None, catalog=catalog, cache=cache, journal=None)
    rows, refusals = [], {}
    symbols = [row for row in universe if catalog.get(row["symbol"])]
    if limit_symbols:
        symbols = symbols[:limit_symbols]

    started = time.time()
    for index, entry in enumerate(symbols, start=1):
        symbol = entry["symbol"]
        for session_id in corpus_sessions(cache, symbol):
            try:
                events, reason = acceptance_rows(scanner, cache, symbol,
                                                 entry["qv_rank"], session_id,
                                                 horizon=horizon,
                                                 every_candle=every_candle)
            except Exception as exc:                        # noqa: BLE001
                log.debug("%s %s failed: %s", symbol, session_id, exc)
                refusals["EXCEPTION"] = refusals.get("EXCEPTION", 0) + 1
                continue
            if reason:
                refusals[reason] = refusals.get(reason, 0) + 1
                continue
            rows.extend(events)
        cache.release(symbol)
        if index % 10 == 0 or index == len(symbols):
            log.info("acceptance: %d/%d symbols, %d events, %.0fs",
                     index, len(symbols), len(rows), time.time() - started)
    return rows, refusals


# ------------------------------------------------------------------- output

def write_csv(path, rows, fields):
    if not rows:
        return None
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def report_profiles(rows):
    """Print the distributions, each next to the threshold it should set."""
    if not rows:
        print("no profile rows")
        return

    print(f"\n{'='*78}\nPROFILE DISTRIBUTIONS   n={len(rows)} symbol-sessions")
    print(f"{'='*78}")

    def column(name):
        return [row[name] for row in rows if row.get(name) is not None]

    order = [
        ("va_range_ratio", "SHAPE_TREND_MAX_VA_RANGE_RATIO"),
        ("poc_position", "SHAPE_P/B_*_POC_POSITION"),
        ("intra_poc_migration_atr", "SHAPE_TREND_MIN_POC_MIGRATION_ATR"),
        ("poc_prominence", "POC_MIN_PROMINENCE"),
        ("value_width_atr", "VA_MIN/MAX_WIDTH_ATR"),
        ("occupied_bins", "PROFILE_MIN_OCCUPIED_BINS"),
        ("candles", "PROFILE_MIN_CANDLES"),
        ("poc_shift_bins", "STABILITY_MAX_POC_SHIFT_BINS"),
        ("vah_shift_bins", "STABILITY_MAX_VA_SHIFT_BINS"),
        ("val_shift_bins", "STABILITY_MAX_VA_SHIFT_BINS"),
        ("value_fraction", "(realised value area, target VALUE_AREA_PCT)"),
    ]
    print()
    for name, governs in order:
        row = metrics.describe(column(name), label=name)
        print(metrics.format_description(row))
        print(f"{'':<34} -> {governs}")

    # Absolute migration is what the threshold actually tests (it uses abs()).
    absolute = [abs(value) for value in column("intra_poc_migration_atr")]
    print(metrics.format_description(
        metrics.describe(absolute, label="|intra_poc_migration_atr|")))
    print(f"{'':<34} -> SHAPE_TREND_MIN_POC_MIGRATION_ATR (tested on abs)")

    counts = {}
    for row in rows:
        counts[row["shape"]] = counts.get(row["shape"], 0) + 1
    print(f"\nSHAPE MIX at current thresholds")
    for label in sorted(counts, key=lambda key: -counts[key]):
        share = counts[label] / len(rows) * 100
        print(f"  {label:<8} {counts[label]:>6d}  {share:>5.1f}%")

    balance = sum(1 for row in rows if row["auction_state"] == "BALANCE")
    print(f"  {'BALANCE':<8} {balance:>6d}  {balance/len(rows)*100:>5.1f}%  "
          f"(mean-reversion setups eligible)")

    print(f"\nva_range_ratio distribution (the state classifier's axis)")
    print(metrics.text_histogram(column("va_range_ratio"), bins=24))


def _report_directional(rows):
    """The scale-free directional test, which is the one that settles S2 vs S3.

    Every other label here mixes two questions that have to be separated:

        "did price MOVE 0.5 units"      - overwhelmingly a VOLATILITY question
        "did price move AWAY or BACK"   - the DIRECTIONAL question, and the only one
                                          that decides which setup has a thesis

    The label used here is: among excursions where EXACTLY ONE of two SYMMETRIC
    thresholds was reached, was it the favourable one? Under directional neutrality
    that is 50% at every threshold in every unit and in every subgroup, because both
    thresholds carry the unit and it cancels. Excursions that hit both or neither are
    dropped: they carry no directional information, and scoring "neither" as a failure
    to continue is what made the earlier race labels read as reversion edges.

    Reported at several thresholds in BOTH units on purpose. The two units disagree -
    in value widths the population reverts, in ATR it continues - and that is not a
    contradiction to be resolved by picking one. A value area is typically ~0.34 ATR
    wide, so +/-0.5 VA is a SMALL move that resolves inside the value area's pull,
    while +/-0.5 ATR is ~1.5 value widths and resolves outside it. Small moves revert
    and large moves continue; the crossover is a property of the market, and showing
    both is what makes it visible instead of letting the choice of unit decide the
    verdict silently.
    """
    usable = [row for row in rows
              if row.get("max_continuation_va") is not None
              and row.get("max_adverse_va") is not None
              and row.get("value_width") and row.get("atr")
              and row.get("bars_forward")]
    if len(usable) < 500:
        return

    print(f"\nDIRECTIONAL TEST  (scale-free: exactly one of two symmetric thresholds)")
    print(f"  50.0% = no directional content. Both units, several thresholds.")
    print(f"  {'threshold':<14} {'n':>7}  {'away from value (95% CI)':<26} verdict")

    for unit, scale_of in (("VA", lambda row: 1.0),
                           ("ATR", lambda row: row["value_width"] / row["atr"])):
        for threshold in (0.25, 0.5, 0.75, 1.0):
            away, resolved = 0, 0
            for row in usable:
                scale = scale_of(row)
                favourable = row["max_continuation_va"] * scale >= threshold
                adverse = row["max_adverse_va"] * scale >= threshold
                if favourable == adverse:
                    continue
                resolved += 1
                away += 1 if favourable else 0
            if resolved < 200:
                continue
            point, low, high = metrics.wilson_interval(away, resolved)
            if low > 0.5:
                verdict = "CONTINUATION"
            elif high < 0.5:
                verdict = "REVERSION"
            else:
                verdict = "flat"
            print(f"  +/-{threshold:.2f} {unit:<8} {resolved:>7}  "
                  f"{point*100:>5.1f}% [{low*100:>4.1f},{high*100:>4.1f}]"
                  f"{'':<8} {verdict}")
        print()

    # And the question the thresholds exist for: does the ratio MOVE that number?
    # If the bands are flat, no cut point on the ratio separates the two setups, and
    # the correct action is to stop gating on it rather than to pick the best-looking
    # band.
    print(f"  does the ratio shift it? (+/-0.50 VA label)")
    resolved_rows = []
    for row in usable:
        favourable = row["max_continuation_va"] >= 0.5
        adverse = row["max_adverse_va"] >= 0.5
        if favourable == adverse:
            continue
        resolved_rows.append((row["volume_rate_ratio"], favourable))
    for low, high in ((0.0, 0.45), (0.45, 0.80), (0.80, 1.20),
                      (1.20, 2.00), (2.00, float("inf"))):
        group = [flag for ratio, flag in resolved_rows if low <= ratio < high]
        if len(group) < 200:
            continue
        point, lo, hi = metrics.wilson_interval(sum(group), len(group))
        top = "inf" if high == float("inf") else f"{high:.2f}"
        print(f"    ratio [{low:.2f},{top:<4}) {len(group):>7}  "
              f"{point*100:>5.1f}% [{lo*100:>4.1f},{hi*100:>4.1f}]")
    if resolved_rows:
        point, lo, hi = metrics.wilson_interval(
            sum(1 for _, flag in resolved_rows if flag), len(resolved_rows))
        print(f"    {'ALL':<18} {len(resolved_rows):>7}  "
              f"{point*100:>5.1f}% [{lo*100:>4.1f},{hi*100:>4.1f}]")
        separation = metrics.auc(
            [ratio for ratio, flag in resolved_rows if flag],
            [ratio for ratio, flag in resolved_rows if not flag])
        if separation:
            print(f"    ratio -> away    AUC={separation['auc']:.4f} "
                  f"[{separation['low']:.4f},{separation['high']:.4f}]   "
                  f"{metrics.auc_verdict(separation)}")


def report_acceptance(rows):
    """The outcome-labelled view: does the ratio separate the two populations?"""
    if not rows:
        print("no acceptance events")
        return

    print(f"\n{'='*78}\nACCEPTANCE EVENTS   n={len(rows)} excursions")
    print(f"{'='*78}")

    ratios = [row["volume_rate_ratio"] for row in rows]
    print()
    print(metrics.format_description(
        metrics.describe(ratios, label="volume_rate_ratio (all)")))

    # The baseline source matters: a PRIOR_SESSION denominator ignores today's regime,
    # so if it dominates, the ratio is comparing across days rather than within one.
    by_source = metrics.group_by(rows, "baseline_source")
    print(f"\nBASELINE SOURCE (the ratio's denominator)")
    for label in sorted(by_source, key=lambda key: -len(by_source[key])):
        group = by_source[label]
        print(f"  {label:<16} {len(group):>6d}  {len(group)/len(rows)*100:>5.1f}%")
    exact_one = sum(1 for row in rows if abs(row["volume_rate_ratio"] - 1.0) < 1e-9)
    print(f"  ratio exactly 1.000: {exact_one} "
          f"({exact_one/len(rows)*100:.1f}%)  <- must stay near zero; a cluster here "
          f"means the denominator contains the numerator again")

    by_outcome = metrics.group_by(rows, "outcome")
    print(f"\nOUTCOME MIX (horizon {rows[0]['bars_forward']} confirm candles)")
    for label in sorted(by_outcome, key=lambda key: -len(by_outcome[key])):
        group = by_outcome[label]
        print(f"  {label:<16} {len(group):>6d}  {len(group)/len(rows)*100:>5.1f}%")

    print(f"\nTHE QUESTION PHASE 0 EXISTS TO ANSWER:")
    print(f"  does volume_rate_ratio differ between REVERTED and CONTINUED?")
    print()
    for label in ("REVERTED", "CONTINUED", "NEITHER"):
        group = by_outcome.get(label, [])
        if not group:
            continue
        print(metrics.format_description(metrics.describe(
            [row["volume_rate_ratio"] for row in group],
            label=f"  ratio | {label}")))

    # RANK SEPARATION, NOT A DIFFERENCE OF MEANS. The first version of this report
    # compared the two means and declared "NO separation" - on a quantity whose mean
    # is ~64 with sd ~4212 against a p95 of ~3.6, where a few near-zero denominators
    # own the mean and its standard error outright. The ordering is what a threshold
    # actually acts on, so the ordering is what gets tested.
    contrast = [row for row in rows if row["outcome"] in ("CONTINUED", "REVERTED")]
    separation = metrics.auc(
        [row["volume_rate_ratio"] for row in contrast if row["outcome"] == "CONTINUED"],
        [row["volume_rate_ratio"] for row in contrast if row["outcome"] == "REVERTED"])
    if separation:
        print(f"\n  rank separation CONTINUED vs REVERTED (tie-corrected AUC; "
              f"0.500 = no information)")
        print(f"    AUC={separation['auc']:.4f} "
              f"[{separation['low']:.4f},{separation['high']:.4f}]   "
              f"n_cont={separation['n_pos']} n_rev={separation['n_neg']}   "
              f"{metrics.auc_verdict(separation)}")

    # THE ATTRIBUTION TEST. This label is asymmetric by construction: REVERTED means
    # "closed back inside value", which is near-automatic 0.1 value widths outside the
    # edge and hard at 1.5, so excursion distance predicts the contrast PARTLY BY
    # DEFINITION. Any apparent power in the ratio has to be shown to survive holding
    # distance fixed, or it is distance wearing the ratio's name.
    if len(contrast) >= 400:
        label_of = lambda row: row["outcome"] == "CONTINUED"        # noqa: E731
        ratio_of = lambda row: row["volume_rate_ratio"]             # noqa: E731
        dist_of = lambda row: row["excursion_distance_va"]          # noqa: E731
        distance_alone = metrics.auc(
            [dist_of(row) for row in contrast if label_of(row)],
            [dist_of(row) for row in contrast if not label_of(row)])
        ratio_within, _ = metrics.auc_within(contrast, dist_of, ratio_of, label_of)
        dist_within, _ = metrics.auc_within(contrast, ratio_of, dist_of, label_of)
        print(f"\n  ATTRIBUTION - is the ratio its own signal, or distance's proxy?")
        if distance_alone:
            print(f"    excursion_distance_va alone            "
                  f"AUC={distance_alone['auc']:.4f} "
                  f"[{distance_alone['low']:.4f},{distance_alone['high']:.4f}]  "
                  f"{metrics.auc_verdict(distance_alone)}")
        if ratio_within:
            print(f"    ratio    within distance quintiles     "
                  f"AUC={ratio_within['auc']:.4f} "
                  f"[{ratio_within['low']:.4f},{ratio_within['high']:.4f}]  "
                  f"{metrics.auc_verdict(ratio_within)}")
        if dist_within:
            print(f"    distance within ratio    quintiles     "
                  f"AUC={dist_within['auc']:.4f} "
                  f"[{dist_within['low']:.4f},{dist_within['high']:.4f}]  "
                  f"{metrics.auc_verdict(dist_within)}")
        if ratio_within and dist_within:
            if ratio_within["high"] < 0.52 and dist_within["low"] > 0.55:
                print(f"    -> the ratio is a PROXY for distance. Its thresholds are "
                      f"redundant with the\n       distance thresholds the setups "
                      f"already carry, and should not be set from this.")
            elif ratio_within["low"] > 0.52:
                print(f"    -> the ratio survives the control and carries its own "
                      f"information.")

    _report_directional(rows)

    print(f"\nSWEEP: continuation rate by ratio band")
    print(f"  {'band':<16} {'n':>6} {'continued':>10} {'reverted':>10} {'lift':>8}")
    base = (len(by_outcome.get("CONTINUED", [])) / len(rows)) if rows else 0.0
    bands = [(0.0, 0.3), (0.3, 0.45), (0.45, 0.6), (0.6, 0.8),
             (0.8, 1.0), (1.0, 1.5), (1.5, 99.0)]
    for low, high in bands:
        group = [row for row in rows if low <= row["volume_rate_ratio"] < high]
        if not group:
            continue
        continued_n = sum(1 for row in group if row["outcome"] == "CONTINUED")
        reverted_n = sum(1 for row in group if row["outcome"] == "REVERTED")
        rate = continued_n / len(group)
        print(f"  [{low:.2f},{high:<5.2f}) {len(group):>6d} "
              f"{continued_n/len(group)*100:>9.1f}% {reverted_n/len(group)*100:>9.1f}% "
              f"{(rate - base)*100:>+7.1f}pp")
    print(f"  baseline continuation rate: {base*100:.1f}%")

    print(f"\nratio distribution")
    print(metrics.text_histogram(ratios, bins=24, low=0.0,
                                 high=metrics.percentile(ratios, 0.98)))


# ------------------------------------- sweep C: shape's bias vs next session
#
# Sweeps A and B leave a gap: A measures shape's raw DISTRIBUTION (poc_position,
# va_range_ratio, ...) and needs no outcome by its own docstring; B calibrates the
# acceptance ratio against a forward outcome. Nothing calibrates SHAPE_P_MIN_POC_
# POSITION / SHAPE_B_MAX_POC_POSITION / SHAPE_TREND_* against one - and those
# thresholds now gate real trades through day_bias() -> BIAS_FILTER_ENABLED, at
# both watch-start and (as of today's fix) entry. config.py's own comment on the
# P/b cutoffs says they were left uncalibrated specifically because "nothing trades
# on the P-vs-b distinction" - no longer true.
#
# THE OUTCOME IS DECOUPLED FROM S4-OFR ON PURPOSE. day_bias() is the claim under
# test, not whether a live trade agreed with it - conflating the two is exactly
# the mistake the P-shape investigation already corrected (bad trades were blamed
# on the shape when the bug was in the gating, not the classification). So every
# outcome below is read directly from price/profile over the FOLLOWING session -
# never filtered by zone touches, absorption, or anything else S4-OFR's entry logic
# does.
#
# THREE INDEPENDENT OPERATIONALISATIONS OF "DID THE BIAS PLAY OUT", reported side
# by side rather than one silently chosen - a literature review done on this sweep's
# first (open-to-close-only) output flagged that as the least theory-aligned of the
# three:
#   agrees_with_bias             value migration: did the next session's OWN value
#                                 area move in the predicted direction and fail to
#                                 overlap with today's - the PRIMARY label, closest
#                                 to "did the auction accept the new location."
#   agrees_with_bias_return      next-session open-to-close return, in ATR - the
#                                 original, simplest, least circular measure. Kept
#                                 as a named secondary column, not replaced.
#   agrees_with_bias_new_extreme did the next session clear today's own high (BULL)
#                                 or low (BEAR) - genuine follow-through. None when
#                                 neither or both extremes cleared, same "exactly one
#                                 of two symmetric thresholds" discipline
#                                 _report_directional already uses below.
#
# UTC CALENDAR-DAY SESSIONS ARE AN ACCOUNTING BOUNDARY ON A 24/7 PERPETUAL, not a
# genuine auction boundary - profile/relations.py's own docstring already makes this
# point for the open-relationship read, and it applies here too. Treat any threshold
# this sweep finds as calibrated against a structurally noisier feature than the
# same technique would be on an instrument with real session opens/closes, and
# prefer a larger, more consistent effect before trusting it for that reason alone.

SHAPE_BIAS_FIELDS = [
    "symbol", "qv_rank", "session_id", "session_start_ms", "atr",
    "shape", "auction_state", "day_bias",
    "poc_position", "va_range_ratio", "intra_poc_migration_atr",
    "bimodal", "second_mode_fraction", "valley_fraction",
    "excess_high", "excess_low", "poor_high", "poor_low",
    "prior_migration_label", "trend_aligned",
    "close_location", "close_location_valid",
    "next_session_id", "next_return_atr", "next_value_migration",
    "next_higher_high", "next_lower_low",
    "agrees_with_bias", "agrees_with_bias_return", "agrees_with_bias_new_extreme",
]

# STAGE 3 - does professional Market-Profile practice's two conditions on a P/b
# read, researched 2026-10-09, actually separate agreement from disagreement here?
#
#   1. TREND CONTEXT: the literature reads P/b as short-covering / long-liquidation
#      - a temporary, weaker force - when the shape runs COUNTER to the move that
#      preceded it (e.g. a P-day capping a prior decline), vs genuine initiative
#      participation - durable - when the shape CONTINUES that move (a P-day
#      extending a prior advance). day_bias() currently makes no such distinction.
#      Operationalised as this session's OWN value-area migration relative to the
#      session immediately before it (the same classify_migration() the NEXT-session
#      outcome already uses, just one relation earlier) - ALIGNED when that prior
#      move agrees with the shape's naive bias, COUNTER when it does not.
#   2. CLOSE LOCATION: the literature also treats the read as unconfirmed unless
#      price closed on the appropriate side of its OWN day's range (a P-day cited as
#      needing a close above 50% of its own range to have "held" what it took).
#      Mirrored for b at below 50% - a deliberate simplification of the b-specific
#      "closed back toward/above the open" phrasing in favour of one symmetric,
#      directly comparable cutoff for both shapes; see _close_location_valid.
#
# Both are recorded here and reported as stratifiers on the SAME primary outcome
# already computed above - nothing about day_bias() or BIAS_FILTER_ENABLED changes
# from this. If agreement is materially higher in the ALIGNED/VALID subset than the
# COUNTER/INVALID one, that is evidence day_bias() should fold these in as modifiers;
# if not, the literature's conditions do not transfer to this venue/timeframe and the
# search for what is wrong with P/b continues elsewhere.


def _trend_alignment(shape_label, prior_migration_label):
    """ALIGNED (1): the shape continues the move that preceded it - the literature's
    "genuine initiative participation" case. COUNTER (0): the shape runs against the
    prior move - "short covering" (P) / "long liquidation" (b), the case the
    literature treats as temporary and weaker. None when not P/b or the prior
    relation does not resolve (first corpus session, insufficient daily history)."""
    if shape_label not in ("P", "b") or not prior_migration_label:
        return None
    moved_up = prior_migration_label in _MIGRATION_AGREES_BULL
    moved_down = prior_migration_label in _MIGRATION_AGREES_BEAR
    if not moved_up and not moved_down:
        return None
    aligned = moved_up if shape_label == "P" else moved_down
    return int(aligned)


def _close_location_valid(shape_label, close_location):
    """Did price close on the side of its own range the shape's bias needs to have
    "held" - see the STAGE 3 note above. None when not P/b or unresolvable."""
    if shape_label not in ("P", "b") or close_location is None:
        return None
    return int(close_location > 0.5 if shape_label == "P" else close_location < 0.5)

_MIGRATION_AGREES_BULL = ("HIGHER", "OVERLAPPING_HIGHER")
_MIGRATION_AGREES_BEAR = ("LOWER", "OVERLAPPING_LOWER")


def _agreement_from_migration(bias, migration_label):
    """PRIMARY outcome: BULL/BEAR vs whether the next session's own value area
    moved in that direction and failed to overlap - see the module note above.

    None for a NEUTRAL bias, an unmeasured migration, or a label that makes no
    directional claim either way (INSIDE/OUTSIDE/UNCHANGED) - never coerced.
    """
    if bias not in ("BULL", "BEAR") or not migration_label:
        return None
    agrees_bull = migration_label in _MIGRATION_AGREES_BULL
    agrees_bear = migration_label in _MIGRATION_AGREES_BEAR
    if not agrees_bull and not agrees_bear:
        return None
    return int(agrees_bull == (bias == "BULL"))


def _agreement_from_return(bias, next_return_atr):
    """SECONDARY outcome: the original, simplest measure - raw next-session
    open-to-close return. None for a NEUTRAL bias or an exactly-flat return."""
    if bias not in ("BULL", "BEAR") or not next_return_atr:
        return None
    return int((next_return_atr > 0) == (bias == "BULL"))


def _agreement_from_new_extreme(bias, higher_high, lower_low):
    """SECONDARY outcome: genuine follow-through - did the next session clear
    today's own high/low. Both or neither cleared is dropped as carrying no
    directional information, the same "exactly one of two symmetric thresholds"
    rule _report_directional already applies to the acceptance sweep below."""
    if bias not in ("BULL", "BEAR") or higher_high == lower_low:
        return None
    return int(higher_high == (bias == "BULL"))


def shape_bias_row(scanner, cache, symbol, qv_rank, session_id):
    """One row: THIS session's shape-derived day_bias(), tested against what price
    and the next session's own profile actually did - three independent agreement
    columns, see the module note above for what each means and why none of them is
    coerced when its own claim does not resolve.
    """
    session_start = sessions.session_start_ms_from_id(session_id)
    next_start = session_start + sessions.MS_DAY
    next_end = next_start + sessions.MS_DAY

    # The session immediately BEFORE session_id, as live would have had it at
    # session_id's own open - the same frozen_bundle(symbol, session_id, as_of)
    # call acceptance_rows already uses for exactly this relation. Fetched first,
    # at the earliest as_of this function uses, to keep the cache's forward-only
    # discipline the rest of this function (and acceptance_rows) already follows.
    # Best-effort: STAGE 3's trend-context stratifier degrades to None, not a
    # refused row, when this is unavailable (e.g. the corpus's first session).
    cache.set_as_of(session_start)
    cache.clear_frozen()
    prior_bundle, _ = scanner.frozen_bundle(symbol, session_id, session_start)

    cache.set_as_of(next_start)
    cache.clear_frozen()
    bundle, reason = scanner.frozen_bundle(symbol, sessions.session_id(next_start),
                                           next_start)
    if bundle is None:
        return None, reason
    shape = bundle.shape
    if shape is None:
        return None, "NO_SHAPE"

    bias = shape_mod.day_bias(shape)
    atr = bundle.atr

    prior_migration_label = None
    if prior_bundle is not None and prior_bundle.levels is not None:
        prior_migration_label = relations.classify_migration(
            bundle.levels, prior_bundle.levels, atr).label
    trend_aligned = _trend_alignment(shape.label, prior_migration_label)
    # shape.close_location is the SAME field day_bias() now gates P/b on - using
    # it here rather than recomputing from bundle.profile means this sweep and
    # production can never silently drift apart.
    close_location = shape.close_location
    close_location_valid = _close_location_valid(shape.label, close_location)

    # THE FOLLOWING SESSION'S OWN TRADING - this is what makes the row a forward
    # test rather than a restatement of today. set_as_of is moved to its close so
    # minute_candles can return the whole thing; nothing about THIS session's own
    # measurement above depended on it.
    cache.set_as_of(next_end)
    next_candles = cache.minute_candles(symbol, next_start, next_end)
    if len(next_candles) < config.PROFILE_MIN_CANDLES:
        return None, "THIN_NEXT_SESSION"

    next_return_atr = ((next_candles[-1].close - next_candles[0].open) / atr
                       if atr > 0 else None)
    higher_high = max(c.high for c in next_candles) > bundle.profile.high
    lower_low = min(c.low for c in next_candles) < bundle.profile.low

    next_profile = builder.profile_as_of(
        symbol, next_candles, next_start, next_end, next_end, bundle.bin_size)
    next_levels = levels_mod.compute(next_profile)
    next_migration_label = (
        relations.classify_migration(next_levels, bundle.levels, atr).label
        if next_levels is not None else None)

    return {
        "symbol": symbol, "qv_rank": qv_rank, "session_id": session_id,
        "session_start_ms": session_start, "atr": atr,
        "shape": shape.label, "auction_state": shape.auction_state,
        "day_bias": bias,
        "poc_position": round(shape.poc_position, 4),
        "va_range_ratio": round(shape.va_range_ratio, 4),
        "intra_poc_migration_atr": (
            round(shape.intra_poc_migration_atr, 4)
            if shape.intra_poc_migration_atr is not None else None),
        "bimodal": int(shape.bimodal),
        "second_mode_fraction": round(shape.second_mode_fraction, 4),
        "valley_fraction": round(shape.valley_fraction, 4),
        "excess_high": int(shape.excess_high), "excess_low": int(shape.excess_low),
        "poor_high": int(shape.poor_high), "poor_low": int(shape.poor_low),
        "prior_migration_label": prior_migration_label,
        "trend_aligned": trend_aligned,
        "close_location": (round(close_location, 4)
                           if close_location is not None else None),
        "close_location_valid": close_location_valid,
        "next_session_id": sessions.session_id(next_start),
        "next_return_atr": (round(next_return_atr, 5)
                            if next_return_atr is not None else None),
        "next_value_migration": next_migration_label,
        "next_higher_high": int(higher_high), "next_lower_low": int(lower_low),
        "agrees_with_bias": _agreement_from_migration(bias, next_migration_label),
        "agrees_with_bias_return": _agreement_from_return(bias, next_return_atr),
        "agrees_with_bias_new_extreme": _agreement_from_new_extreme(
            bias, higher_high, lower_low),
    }, None


def sweep_shape_bias(cache, catalog, universe, limit_symbols=None):
    scanner = Scanner(rest=None, catalog=catalog, cache=cache, journal=None)
    rows, refusals = [], {}
    symbols = [row for row in universe if catalog.get(row["symbol"])]
    if limit_symbols:
        symbols = symbols[:limit_symbols]

    started = time.time()
    for index, entry in enumerate(symbols, start=1):
        symbol = entry["symbol"]
        for session_id in corpus_sessions(cache, symbol):
            try:
                row, reason = shape_bias_row(scanner, cache, symbol,
                                             entry["qv_rank"], session_id)
            except Exception as exc:                        # noqa: BLE001
                log.debug("%s %s failed: %s", symbol, session_id, exc)
                refusals["EXCEPTION"] = refusals.get("EXCEPTION", 0) + 1
                continue
            if row is None:
                refusals[reason] = refusals.get(reason, 0) + 1
                continue
            rows.append(row)
        cache.release(symbol)
        if index % 10 == 0 or index == len(symbols):
            log.info("shape_bias: %d/%d symbols, %d rows, %.0fs",
                     index, len(symbols), len(rows), time.time() - started)
    return rows, refusals


def _directional_auc(rows, measure_key):
    """Does `measure_key` rank-separate sessions whose NEXT return was positive
    from ones whose next return was negative?

    Deliberately NOT restricted to rows already labelled P or b - that would only
    show how well the axis orders days AFTER today's cutoff already selected them,
    which cannot say whether 0.65/0.35 is the right place to cut. This looks at the
    axis's full range against the only ground truth that matters (what price
    actually did next), the same posture as report_acceptance's rank-separation
    test for volume_rate_ratio.
    """
    usable = [row for row in rows
             if row.get(measure_key) is not None and row.get("next_return_atr")]
    positive = [row[measure_key] for row in usable if row["next_return_atr"] > 0]
    negative = [row[measure_key] for row in usable if row["next_return_atr"] < 0]
    return metrics.auc(positive, negative)


def _threshold_grid(rows, measure_key, thresholds, side):
    """Agreement rate if `measure_key >= threshold` (side="P") or
    `measure_key <= threshold` (side="b") were used as the ENTIRE bias call,
    at each candidate cutoff - the same grid-sweep instrument config.py's own
    comments already used for the bimodal thresholds (0.60-0.90 x 0.15-0.35),
    applied here to the P/b axis with an actual outcome attached.
    """
    usable = [row for row in rows
             if row.get(measure_key) is not None and row.get("next_return_atr")]
    print(f"\n  {measure_key} as the WHOLE bias call ({side}-side, "
         f">= cutoff -> BULL)" if side == "P" else
         f"\n  {measure_key} as the WHOLE bias call ({side}-side, <= cutoff -> BEAR)")
    print(f"  {'cutoff':<8} {'n':>6}  {'agree with next-session direction (95% CI)'}")
    for cutoff in thresholds:
        if side == "P":
            selected = [row for row in usable if row[measure_key] >= cutoff]
            predicted_bull = True
        else:
            selected = [row for row in usable if row[measure_key] <= cutoff]
            predicted_bull = False
        if len(selected) < 30:
            print(f"  {cutoff:<8.2f} {len(selected):>6d}  (too few rows)")
            continue
        agree = sum(1 for row in selected
                   if (row["next_return_atr"] > 0) == predicted_bull)
        point, lo, hi = metrics.wilson_interval(agree, len(selected))
        print(f"  {cutoff:<8.2f} {len(selected):>6d}  "
             f"{point*100:>5.1f}% [{lo*100:>4.1f},{hi*100:>4.1f}]")


def _agreement_summary(rows, key, title):
    """Agreement rate for one outcome column, three ways: the naive Wilson interval
    (treats every row as independent, which overstates precision - consecutive
    sessions on one symbol are not independent evidence, nor are different symbols
    on the same calendar day), and the same rate clustered by symbol and by session
    - metrics.clustered_mean_interval already exists for exactly this reasoning.
    Printing all three makes the gap between naive and clustered itself the honest
    answer to "how much of that precision was real."

    Returns the resolved subset (for callers that stratify it further), or None.
    """
    resolved = [row for row in rows if row.get(key) is not None]
    print(f"\n{title}  (resolved {len(resolved)}/{len(rows)})")
    if not resolved:
        return None
    point, lo, hi = metrics.wilson_interval(
        sum(row[key] for row in resolved), len(resolved))
    by_symbol = metrics.clustered_mean_interval(
        resolved, lambda r: r[key], lambda r: r["symbol"])
    by_session = metrics.clustered_mean_interval(
        resolved, lambda r: r[key], lambda r: r["session_id"])
    print(f"  naive Wilson           {point*100:>5.1f}% [{lo*100:>4.1f},{hi*100:>4.1f}]")
    print(f"  clustered by symbol    {by_symbol['mean']*100:>5.1f}% "
         f"[{by_symbol['low']*100:>4.1f},{by_symbol['high']*100:>4.1f}]  "
         f"({by_symbol['clusters']} symbols)")
    print(f"  clustered by session   {by_session['mean']*100:>5.1f}% "
         f"[{by_session['low']*100:>4.1f},{by_session['high']*100:>4.1f}]  "
         f"({by_session['clusters']} sessions)")
    return resolved


def _stratify_by_extreme(rows, key, excess_key, poor_key, label):
    """Does the extreme's own completeness (excess = clean rejection, poor =
    unfinished, tends to be revisited) change whether the bias played out?
    Literature treats these as material modifiers to a P/b read, not decoration -
    this answers whether day_bias() SHOULD fold them in, before any code does."""
    groups = {
        "excess": [r for r in rows if r[excess_key] and not r[poor_key]],
        "poor": [r for r in rows if r[poor_key] and not r[excess_key]],
        "neither": [r for r in rows if not r[excess_key] and not r[poor_key]],
    }
    for name, group in groups.items():
        if len(group) < 15:
            print(f"    {label} | {name:<8} n={len(group):>4d}  (too few)")
            continue
        point, lo, hi = metrics.wilson_interval(
            sum(row[key] for row in group), len(group))
        print(f"    {label} | {name:<8} n={len(group):>4d}  "
             f"{point*100:>5.1f}% [{lo*100:>4.1f},{hi*100:>4.1f}]")


def _stratify_by_flag(rows, key, flag_key, group_labels, label):
    """Agreement rate for one outcome column, split by a single binary modifier -
    same instrument as _stratify_by_extreme, for a two-way rather than three-way
    split. `group_labels` is (name for flag==0, name for flag==1)."""
    groups = {
        group_labels[0]: [r for r in rows if r.get(flag_key) == 0],
        group_labels[1]: [r for r in rows if r.get(flag_key) == 1],
    }
    for name, group in groups.items():
        if len(group) < 15:
            print(f"    {label} | {name:<10} n={len(group):>4d}  (too few)")
            continue
        point, lo, hi = metrics.wilson_interval(
            sum(row[key] for row in group), len(group))
        print(f"    {label} | {name:<10} n={len(group):>4d}  "
             f"{point*100:>5.1f}% [{lo*100:>4.1f},{hi*100:>4.1f}]")


def _joint_stratification(rows, key, close_key, trend_key, label):
    """2x2: does close-location validity add information ON TOP OF trend context,
    or does one subsume the other? Each of the 4 cells pairs one close-location
    state with one trend-context state."""
    cells = [
        ("INVALID", 0, "COUNTER", 0), ("INVALID", 0, "ALIGNED", 1),
        ("VALID", 1, "COUNTER", 0), ("VALID", 1, "ALIGNED", 1),
    ]
    for close_name, close_val, trend_name, trend_val in cells:
        group = [r for r in rows
                 if r.get(close_key) == close_val and r.get(trend_key) == trend_val]
        if len(group) < 15:
            print(f"    {label} | close={close_name:<8} trend={trend_name:<8} "
                 f"n={len(group):>4d}  (too few)")
            continue
        point, lo, hi = metrics.wilson_interval(
            sum(row[key] for row in group), len(group))
        print(f"    {label} | close={close_name:<8} trend={trend_name:<8} "
             f"n={len(group):>4d}  {point*100:>5.1f}% [{lo*100:>4.1f},{hi*100:>4.1f}]")


def _confound_check(rows, flag_key, measure_keys, label):
    """Does close_location_valid just restate a measurement already in the row -
    i.e. are VALID sessions simply the more elongated/migrated/extreme-POC ones
    that would have scored well as B/trend anyway - rather than adding anything
    new? Rank-separation (AUC) between the VALID and INVALID groups on each
    candidate, the same instrument report_acceptance's ATTRIBUTION TEST already
    uses for the same question about a different pair of measures. 0.500 = the
    two groups look the same on this axis, i.e. NOT a confound on this measure."""
    print(f"\n  CONFOUND CHECK ({label}): is close_location_valid just restating")
    print(f"  poc_position / va_range_ratio / migration, rather than new information?")
    for measure_key in measure_keys:
        def value_of(row):
            raw = row[measure_key]
            return abs(raw) if measure_key == "intra_poc_migration_atr" else raw
        valid_vals = [value_of(r) for r in rows
                     if r.get(flag_key) == 1 and r.get(measure_key) is not None]
        invalid_vals = [value_of(r) for r in rows
                        if r.get(flag_key) == 0 and r.get(measure_key) is not None]
        result = metrics.auc(valid_vals, invalid_vals)
        if result:
            print(f"    {measure_key:<26} AUC={result['auc']:.4f} "
                 f"[{result['low']:.4f},{result['high']:.4f}]  "
                 f"n={result['n_pos']}+{result['n_neg']}   {metrics.auc_verdict(result)}")


def _outcomes_agree_with_each_other(rows, pairs):
    print(f"\nDO THE THREE OUTCOME DEFINITIONS AGREE WITH EACH OTHER?")
    print(f"  (independent of day_bias - just whether they tell the same story)")
    for key_a, key_b, label in pairs:
        both = [row for row in rows
               if row.get(key_a) is not None and row.get(key_b) is not None]
        if len(both) < 20:
            print(f"  {label:<32} n={len(both):>5d}  (too few)")
            continue
        match = sum(1 for row in both if row[key_a] == row[key_b])
        print(f"  {label:<32} n={len(both):>5d}  agree: {match/len(both)*100:.1f}%")


def report_shape_bias(rows):
    """Does day_bias() - and the thresholds it is built from - predict anything?"""
    if not rows:
        print("no shape-bias rows")
        return

    print(f"\n{'='*78}\nSHAPE BIAS vs NEXT SESSION   n={len(rows)} symbol-sessions")
    print(f"{'='*78}")
    print("Three independent readings of \"did the bias play out\" - value migration")
    print("(PRIMARY), next-session return, and a new-extreme clear - shown side by")
    print("side rather than one silently chosen. Decoupled from S4-OFR's own entry")
    print("mechanics on purpose - see the module note above shape_bias_row.\n")
    print("UTC calendar-day sessions are an accounting boundary on this venue, not a")
    print("genuine auction boundary - treat any threshold found here as calibrated")
    print("against a structurally noisier feature than on an instrument with real")
    print("session opens/closes, and prefer a larger, more consistent effect.")

    by_shape = metrics.group_by(rows, "shape")
    print("\nSHAPE MIX in this sample")
    for label in sorted(by_shape, key=lambda key: -len(by_shape[key])):
        print(f"  {label:<8} {len(by_shape[label]):>6d}")

    primary = _agreement_summary(rows, "agrees_with_bias",
                                 "PRIMARY OUTCOME: next session's value migration")
    _agreement_summary(rows, "agrees_with_bias_return",
                       "SECONDARY: next-session open-to-close return")
    _agreement_summary(rows, "agrees_with_bias_new_extreme",
                       "SECONDARY: next session clears today's own high/low")

    _outcomes_agree_with_each_other(rows, [
        ("agrees_with_bias", "agrees_with_bias_return", "migration vs return"),
        ("agrees_with_bias", "agrees_with_bias_new_extreme", "migration vs new extreme"),
        ("agrees_with_bias_return", "agrees_with_bias_new_extreme",
         "return vs new extreme"),
    ])

    if primary:
        print(f"\nBY SHAPE LABEL (primary outcome - does P's bias agree more/less "
             f"than b's?)")
        by_label = metrics.group_by(primary, "shape")
        for label in sorted(by_label, key=lambda key: -len(by_label[key])):
            group = by_label[label]
            if len(group) < 20:
                continue
            point, lo, hi = metrics.wilson_interval(
                sum(row["agrees_with_bias"] for row in group), len(group))
            print(f"    {label:<8} n={len(group):>5d}  "
                 f"{point*100:>5.1f}% [{lo*100:>4.1f},{hi*100:>4.1f}]")

        print(f"\nEXCESS/POOR STRATIFICATION (primary outcome) - a clean rejection and")
        print(f"  an unfinished auction get the identical bias vote today; should they?")
        p_rows = [row for row in primary if row["shape"] == "P"]
        _stratify_by_extreme(p_rows, "agrees_with_bias", "excess_high", "poor_high",
                             "P (high)")
        b_rows = [row for row in primary if row["shape"] == "b"]
        _stratify_by_extreme(b_rows, "agrees_with_bias", "excess_low", "poor_low",
                             "b (low)")

        print(f"\nTREND-CONTEXT STRATIFICATION (primary outcome) - professional")
        print(f"  practice reads P/b as short-covering/long-liquidation (weaker,")
        print(f"  COUNTER-trend) vs genuine continuation (ALIGNED) - see STAGE 3")
        print(f"  note above shape_bias_row. day_bias() currently makes no such")
        print(f"  distinction - does the data say it should?")
        _stratify_by_flag(p_rows, "agrees_with_bias", "trend_aligned",
                          ("COUNTER", "ALIGNED"), "P")
        _stratify_by_flag(b_rows, "agrees_with_bias", "trend_aligned",
                          ("COUNTER", "ALIGNED"), "b")

        print(f"\nCLOSE-LOCATION VALIDITY STRATIFICATION (primary outcome) - the")
        print(f"  literature treats the read as unconfirmed unless price closed on")
        print(f"  the appropriate side of its OWN day's range - an unconditional")
        print(f"  P/b vote skips this filter entirely.")
        _stratify_by_flag(p_rows, "agrees_with_bias", "close_location_valid",
                          ("INVALID", "VALID"), "P")
        _stratify_by_flag(b_rows, "agrees_with_bias", "close_location_valid",
                          ("INVALID", "VALID"), "b")

        print(f"\nJOINT STRATIFICATION (primary outcome) - does close-location add")
        print(f"  anything ON TOP OF trend context, or does one subsume the other?")
        _joint_stratification(p_rows, "agrees_with_bias", "close_location_valid",
                              "trend_aligned", "P")
        _joint_stratification(b_rows, "agrees_with_bias", "close_location_valid",
                              "trend_aligned", "b")

        _confound_check(p_rows, "close_location_valid",
                        ("poc_position", "va_range_ratio", "intra_poc_migration_atr"),
                        "P")
        _confound_check(b_rows, "close_location_valid",
                        ("poc_position", "va_range_ratio", "intra_poc_migration_atr"),
                        "b")

    print(f"\nRANK SEPARATION (full range, not just rows already labelled P/b/trend;")
    print(f"  0.500 = the axis carries no directional information at all)")
    for key in ("poc_position", "va_range_ratio"):
        result = _directional_auc(rows, key)
        if result:
            print(f"  {key:<24} AUC={result['auc']:.4f} "
                 f"[{result['low']:.4f},{result['high']:.4f}]  n={result['n_pos']}+"
                 f"{result['n_neg']}   {metrics.auc_verdict(result)}")
    abs_migration_rows = [
        {**row, "_abs_migration": abs(row["intra_poc_migration_atr"])}
        for row in rows if row.get("intra_poc_migration_atr") is not None]
    if abs_migration_rows:
        result = _directional_auc(abs_migration_rows, "_abs_migration")
        if result:
            print(f"  {'|intra_poc_migration_atr|':<24} AUC={result['auc']:.4f} "
                 f"[{result['low']:.4f},{result['high']:.4f}]  n={result['n_pos']}+"
                 f"{result['n_neg']}   {metrics.auc_verdict(result)}")

    print(f"\nTHRESHOLD GRID - where (if anywhere) does a cutoff actually separate")
    print(f"  agreement from disagreement, vs. the current 0.65/0.35?")
    _threshold_grid(rows, "poc_position",
                    (0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85), side="P")
    _threshold_grid(rows, "poc_position",
                    (0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15), side="b")


# --------------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(description="Phase 0 calibration sweeps.")
    parser.add_argument("--sweep",
                        choices=("profile", "acceptance", "shape_bias", "both"),
                        default="both")
    parser.add_argument("--root", default=None, help="corpus root (PARQUET_DIR)")
    parser.add_argument("--out", default="calibration", help="output directory")
    parser.add_argument("--symbols", type=int, default=None,
                        help="limit to the first N symbols by rank (for a quick look)")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON,
                        help="forward outcome horizon in confirm candles")
    parser.add_argument("--every-candle", action="store_true",
                        help="record an acceptance event on EVERY candle of an "
                             "excursion, not only the first that qualifies. Answers "
                             "whether the measurement sharpens as value builds - the "
                             "default sampling pins consecutive_closes to "
                             "ACCEPT_MIN_CANDLES and cannot see it. Rows are "
                             "clustered by excursion_id and are not independent; "
                             "writes acceptance_every.csv so the primary corpus is "
                             "left intact.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)sZ %(levelname)s %(message)s")
    logging.Formatter.converter = time.gmtime

    cache, catalog, universe = open_corpus(args.root)
    log.info("corpus: %d specs, %d ranked symbols, %d symbols with 1m data",
             len(catalog), len(universe), len(cache.available_symbols()))

    if args.sweep in ("profile", "both"):
        rows, refusals = sweep_profiles(cache, catalog, universe, args.symbols)
        path = write_csv(os.path.join(args.out, "profiles.csv"), rows,
                         PROFILE_FIELDS)
        report_profiles(rows)
        if refusals:
            print(f"\nrefused sessions: "
                  f"{', '.join(f'{k}={v}' for k, v in sorted(refusals.items()))}")
        if path:
            print(f"\nwrote {path}")

    if args.sweep in ("acceptance", "both"):
        cache.release()
        rows, refusals = sweep_acceptance(cache, catalog, universe, args.symbols,
                                          horizon=args.horizon,
                                          every_candle=args.every_candle)
        name = "acceptance_every.csv" if args.every_candle else "acceptance.csv"
        path = write_csv(os.path.join(args.out, name), rows, ACCEPTANCE_FIELDS)
        report_acceptance(rows)
        if refusals:
            print(f"\nrefused sessions: "
                  f"{', '.join(f'{k}={v}' for k, v in sorted(refusals.items()))}")
        if path:
            print(f"\nwrote {path}")

    if args.sweep == "shape_bias":
        cache.release()
        rows, refusals = sweep_shape_bias(cache, catalog, universe, args.symbols)
        path = write_csv(os.path.join(args.out, "shape_bias.csv"), rows,
                         SHAPE_BIAS_FIELDS)
        report_shape_bias(rows)
        if refusals:
            print(f"\nrefused sessions: "
                  f"{', '.join(f'{k}={v}' for k, v in sorted(refusals.items()))}")
        if path:
            print(f"\nwrote {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
