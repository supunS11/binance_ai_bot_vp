"""High-volume nodes (HVNs) inside the value area (VAL..VAH), per the value-area HVN plan.

Selected by config.HVN_METHOD == "va_plan". The "current" method (profile/levels._find_nodes)
is retained behind the same switch for comparison.

Steps, in the plan's order:
  1. Restrict to the value-area bins (val_bin..vah_bin inclusive).
  2. Candidate peaks: strict local maxima (volume greater than both immediate neighbours),
     clearing the POC-relative and value-area-mean floors, with at least VA_HVN_MIN_RUN_BINS
     consecutive bins at or above the POC-relative floor, and prominence at least
     VA_HVN_MIN_PROMINENCE of the peak.
  3. Grow each peak into a zone outward, stopping at the flanking trough (where volume starts
     to rise) or when volume falls below VA_HVN_GROW_FRACTION of the peak.
  4. Cap zone width at VA_HVN_MAX_WIDTH_ATR x ATR, and merge zones that touch or overlap.
  5. Rank by peak volume, then prominence, then narrower width. Keep the POC plus the top
     VA_HVN_MAX_COUNT others.
  6. If rebuilt profiles are supplied, a non-POC peak survives only if every rebuild shows a
     local maximum within VA_HVN_STABILITY_TOL_BINS nominal bins of it.
"""
from profile.levels import Node
import config


def _is_local_max(profile, index):
    vol = profile.volume_at(index)
    return vol > profile.volume_at(index - 1) and vol > profile.volume_at(index + 1)


def _prominence(profile, peak, va_lo, va_hi):
    peak_vol = profile.volume_at(peak)
    if peak_vol <= 0:
        return 0.0
    troughs = []
    for step in (-1, 1):
        low_seen = peak_vol
        index = peak + step
        while va_lo <= index <= va_hi:
            vol = profile.volume_at(index)
            if vol > peak_vol:
                break
            low_seen = min(low_seen, vol)
            index += step
        troughs.append(low_seen)
    return (peak_vol - max(troughs)) / peak_vol


def _run_length(profile, peak, floor, va_lo, va_hi):
    lo = hi = peak
    while lo - 1 >= va_lo and profile.volume_at(lo - 1) >= floor:
        lo -= 1
    while hi + 1 <= va_hi and profile.volume_at(hi + 1) >= floor:
        hi += 1
    return hi - lo + 1


def _grow(profile, peak, va_lo, va_hi, grow_floor):
    lo = hi = peak
    while lo - 1 >= va_lo:
        nxt = profile.volume_at(lo - 1)
        if nxt < grow_floor or nxt > profile.volume_at(lo):
            break
        lo -= 1
    while hi + 1 <= va_hi:
        nxt = profile.volume_at(hi + 1)
        if nxt < grow_floor or nxt > profile.volume_at(hi):
            break
        hi += 1
    return lo, hi


def _zone(profile, peak, lo, hi, prominence):
    return {
        "lo": lo, "hi": hi, "peak": peak,
        "peak_volume": profile.volume_at(peak),
        "volume": sum(profile.volume_at(i) for i in range(lo, hi + 1)),
        "prominence": prominence,
    }


def _cap(profile, zone, max_bins):
    lo, hi, peak = zone["lo"], zone["hi"], zone["peak"]
    while hi - lo + 1 > max_bins and (lo != peak or hi != peak):
        if lo != peak and (hi == peak or profile.volume_at(lo) <= profile.volume_at(hi)):
            lo += 1
        else:
            hi -= 1
    return _zone(profile, peak, lo, hi, zone["prominence"])


def _merge(profile, zones):
    merged = []
    for zone in sorted(zones, key=lambda z: z["lo"]):
        if merged and zone["lo"] <= merged[-1]["hi"] + 1:
            prev = merged[-1]
            keep = prev if (prev["peak_volume"], prev["prominence"]) >= (
                zone["peak_volume"], zone["prominence"]) else zone
            merged[-1] = _zone(profile, keep["peak"], min(prev["lo"], zone["lo"]),
                               max(prev["hi"], zone["hi"]), keep["prominence"])
        else:
            merged.append(zone)
    return merged


def _rank_key(zone):
    return (zone["peak_volume"], zone["prominence"], -(zone["hi"] - zone["lo"]))


def _select(profile, levels, atr):
    va_lo, va_hi = levels.val_bin, levels.vah_bin
    if va_hi < va_lo or levels.poc_volume <= 0:
        return []
    bins = range(va_lo, va_hi + 1)
    mean_va = sum(profile.volume_at(i) for i in bins) / len(bins)
    poc_floor = levels.poc_volume * config.VA_HVN_MIN_POC_FRACTION
    mean_floor = mean_va * config.VA_HVN_MIN_MEAN_MULTIPLE
    max_bins = (max(1, int(config.VA_HVN_MAX_WIDTH_ATR * atr / profile.bin_size))
                if atr > 0 else 10 ** 9)

    candidates = []
    for index in bins:
        vol = profile.volume_at(index)
        if vol < poc_floor or vol < mean_floor or not _is_local_max(profile, index):
            continue
        if _run_length(profile, index, poc_floor, va_lo, va_hi) < config.VA_HVN_MIN_RUN_BINS:
            continue
        prominence = _prominence(profile, index, va_lo, va_hi)
        if prominence < config.VA_HVN_MIN_PROMINENCE:
            continue
        lo, hi = _grow(profile, index, va_lo, va_hi, vol * config.VA_HVN_GROW_FRACTION)
        candidates.append(_cap(profile, _zone(profile, index, lo, hi, prominence), max_bins))

    zones = _merge(profile, candidates)
    poc = levels.poc_bin
    poc_zone = next((z for z in zones if z["lo"] <= poc <= z["hi"]), None)
    if poc_zone is None:
        lo, hi = _grow(profile, poc, va_lo, va_hi,
                       profile.volume_at(poc) * config.VA_HVN_GROW_FRACTION)
        poc_zone = _cap(profile, _zone(profile, poc, lo, hi,
                                       _prominence(profile, poc, va_lo, va_hi)), max_bins)
    others = [z for z in zones if z is not poc_zone and not (z["lo"] <= poc <= z["hi"])]
    others.sort(key=_rank_key, reverse=True)
    return [poc_zone] + others[:config.VA_HVN_MAX_COUNT]


def _as_node(profile, levels, zone):
    poc_volume = levels.poc_volume
    return Node(
        kind="HVN",
        low_bin=zone["lo"], high_bin=zone["hi"],
        low_price=profile.bin_low(zone["lo"]), high_price=profile.bin_high(zone["hi"]),
        peak_bin=zone["peak"], peak_price=profile.bin_center(zone["peak"]),
        volume=zone["volume"],
        volume_pct_of_poc=zone["peak_volume"] / poc_volume if poc_volume > 0 else 0.0,
        peak_volume=zone["peak_volume"],
        is_poc=zone["peak"] == levels.poc_bin,
    )


def _local_max_prices(profile, levels):
    va_lo, va_hi = levels.val_bin, levels.vah_bin
    return [profile.bin_center(i) for i in range(va_lo, va_hi + 1)
            if _is_local_max(profile, i)]


def detect(profile, levels, atr, perturbed=()):
    """Return the selected HVN zones as Node objects, POC first.

    `perturbed`: (profile, levels, bin_size) triples rebuilt at the stability multipliers.
    Empty means no stability filter is applied.
    """
    selected = _select(profile, levels, atr)
    nodes = [_as_node(profile, levels, z) for z in selected]
    if not perturbed:
        return nodes

    tol = config.VA_HVN_STABILITY_TOL_BINS * profile.bin_size
    rebuilt_maxima = [_local_max_prices(p_profile, p_levels)
                      for p_profile, p_levels, _size in perturbed]
    stable = []
    for node in nodes:
        if node.is_poc or all(
                any(abs(node.peak_price - price) <= tol for price in maxima)
                for maxima in rebuilt_maxima):
            stable.append(node)
    return stable


def touch_count(candles, low, high):
    """Separate visits to the band [low, high] by closed candles, in time order.

    A visit starts on a candle that trades into the band from outside. A candle that
    stays inside, or a return after leaving, is not a new visit. Zero means the zone
    has not been tested yet in the window supplied.
    """
    visits = 0
    inside = False
    for candle in candles:
        touching = candle.low <= high and candle.high >= low
        if touching and not inside:
            visits += 1
        inside = touching
    return visits


def usage(entry_price, direction, atr, levels, dev_levels, nodes, tests):
    """Plan section 4 read-outs for one entry. Recorded for measurement, never gated.

    entry_in_zone: entry sits in an HVN zone, allowing a quarter-ATR tolerance.
    nearest_atr:   distance to the nearest HVN on the trade's target side, in ATR.
    confluence:    reference levels (previous VAH/VAL/POC, VWAP, developing POC) within a
                   quarter ATR of that nearest HVN's peak.
    first_test:    1 if that zone has not been visited yet this session, 0 if it has.
    """
    tol = 0.25 * atr if atr > 0 else 0.0
    entry_in_zone = any(n.low_price - tol <= entry_price <= n.high_price + tol
                        for n in nodes)
    beyond = [(i, n) for i, n in enumerate(nodes)
              if (n.peak_price > entry_price if direction == "BUY" else n.peak_price < entry_price)]
    if not beyond or atr <= 0:
        return {"entry_in_zone": int(entry_in_zone), "nearest_atr": None,
                "confluence": None, "first_test": None}
    index, nearest = min(beyond, key=lambda pair: abs(pair[1].peak_price - entry_price))
    refs = [levels.vah, levels.val, levels.poc_price, levels.vwap]
    if dev_levels is not None:
        refs.append(dev_levels.poc_price)
    confluence = sum(1 for ref in refs if abs(ref - nearest.peak_price) <= tol)
    first_test = None
    if tests and index < len(tests):
        first_test = int(tests[index] == 0)
    return {"entry_in_zone": int(entry_in_zone),
            "nearest_atr": abs(nearest.peak_price - entry_price) / atr,
            "confluence": confluence, "first_test": first_test}
