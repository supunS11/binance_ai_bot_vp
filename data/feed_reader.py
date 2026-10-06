"""Reads the feed recorder's output and answers the STACKED and RESTING questions for S4.

Coverage is the first question, and it is answered before any signal is read. A window the
recorder did not observe - before it started, or across a gap marker - is not quiet. It is
unknown, so the signal is returned as None (UNAVAILABLE), never False.

Trades are per-minute price levels. A minute with no trades is simply absent from the file,
so coverage is judged from the recorder's own start time and gap markers, not from counts.
"""
import datetime as dt
import gzip
import json
import os
import statistics

MINUTE_MS = 60_000
DAY_MS = 86_400_000


def _days(start_ms, end_ms):
    day = (start_ms // DAY_MS) * DAY_MS
    while day <= end_ms:
        yield dt.datetime.fromtimestamp(day / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%d")
        day += DAY_MS


def _lines(path):
    if not os.path.exists(path):
        return []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _read(root, kind, symbol, start_ms, end_ms):
    """(records, gaps, earliest_ms) for one symbol and kind, for the days the window touches."""
    records, gaps, earliest = [], [], None
    for day in _days(start_ms, end_ms):
        for record in _lines(os.path.join(root, kind, symbol, f"{day}.jsonl.gz")):
            if "gap_from" in record:
                gaps.append((record["gap_from"], record["gap_to"]))
                continue
            key = record["m"] if kind == "trades" else record["t"]
            earliest = key if earliest is None else min(earliest, key)
            if start_ms <= key < end_ms:
                records.append(record)
    return records, gaps, earliest


def _covered(start_ms, end_ms, gaps, earliest):
    if earliest is None or start_ms < earliest:
        return False
    return not any(g_from < end_ms and g_to > start_ms for g_from, g_to in gaps)


def trade_levels(root, symbol, start_ms, end_ms):
    """Summed (buy, sell) by price over the window, or None when it is not covered."""
    records, gaps, earliest = _read(root, "trades", symbol, start_ms, end_ms)
    if not _covered(start_ms, end_ms, gaps, earliest):
        return None
    totals = {}
    for record in records:
        for price, (buy, sell) in record["lv"].items():
            cell = totals.setdefault(price, [0.0, 0.0])
            cell[0] += buy
            cell[1] += sell
    return totals


def depth_samples(root, symbol, start_ms, end_ms):
    """Depth samples in the window, or None when it is not covered."""
    samples, gaps, earliest = _read(root, "depth", symbol, start_ms, end_ms)
    if not _covered(start_ms, end_ms, gaps, earliest):
        return None
    return samples


def stacked_imbalance(levels, side, tick, ratio, min_levels):
    """True when `min_levels` or more adjacent price levels each show aggression in the
    reversal direction at `ratio` to one. Buy aggression for a BUY, sell aggression for a SELL."""
    if not levels or tick <= 0:
        return False
    run, previous = 0, None
    for price in sorted(levels, key=float):
        p = float(price)
        buy, sell = levels[price]
        dominant = (buy > 0 and buy >= ratio * sell) if side == "BUY" \
            else (sell > 0 and sell >= ratio * buy)
        adjacent = previous is not None and abs(p - previous - tick) <= tick * 1e-6
        run = (run + 1 if adjacent else 1) if dominant else 0
        if run >= min_levels:
            return True
        previous = p
    return False


def resting_large_orders(samples, side, low, high, size_mult, persist):
    """True when, in at least `persist` of the samples, the book carries a level inside the
    zone band that is `size_mult` times the median of the top levels on that side."""
    if not samples:
        return False
    hits = 0
    for sample in samples:
        book = sample["b"] if side == "BUY" else sample["a"]
        quantities = [float(q) for _p, q in book]
        if not quantities:
            continue
        typical = statistics.median(quantities)
        if typical <= 0:
            continue
        inside = [float(q) for p, q in book if low <= float(p) <= high]
        if any(q >= size_mult * typical for q in inside):
            hits += 1
    return hits / len(samples) >= persist
