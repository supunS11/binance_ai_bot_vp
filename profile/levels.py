"""Derived levels: POC, value area, HVN/LVN, VWAP bands.

THE VALUE AREA ALGORITHM IS WRITTEN OUT IN FULL because the obvious
implementation and the standard one disagree, and the difference is not
academic. Expanding one bin at a time from the POC - the intuitive approach -
produces measurably different bounds than the Market Profile convention, which
compares the sum of the next TWO bins above against the next TWO below and takes
the larger side whole. On a histogram with a single large bin just above the POC
and two moderate ones below, the two methods choose opposite directions.

The paired form is used here for two reasons: it is what every standard platform
implements, so a human eyeballing a chart to sanity-check the bot sees the same
VAH and VAL it does; and it is less sensitive to a single outlier bin, which on a
thin alt-coin session is usually a liquidation print rather than genuine value.

TIE-BREAKS ARE DETERMINISTIC AND SPECIFIED. Two bins with identical volume, or
two sides with identical pair sums, must resolve the same way in live and in
replay or the two will disagree on a level and every reconciliation check
becomes noise. Floating-point sums make exact ties rarer than they look but not
impossible, and "rare" is the worst kind of nondeterminism.
"""
import math
from dataclasses import dataclass, field

import config


@dataclass
class Node:
    """A contiguous run of bins forming a volume peak (HVN) or valley (LVN)."""
    kind: str                 # "HVN" | "LVN"
    low_bin: int
    high_bin: int
    low_price: float
    high_price: float
    peak_bin: int
    peak_price: float
    volume: float             # total across the run
    volume_pct_of_poc: float  # peak bin volume / POC bin volume
    peak_volume: float = 0.0  # HVN zones from profile/va_hvn.py: volume of the peak bin
    is_poc: bool = False      # HVN zones from profile/va_hvn.py: the zone holds the POC

    @property
    def width_bins(self):
        return self.high_bin - self.low_bin + 1

    @property
    def center_price(self):
        return (self.low_price + self.high_price) / 2.0

    def contains(self, price):
        return self.low_price <= price <= self.high_price


@dataclass
class Levels:
    poc_bin: int
    poc_price: float          # bin CENTRE - the tradeable representative price
    poc_volume: float
    poc_prominence: float

    vah: float                # upper edge of the highest included bin
    val: float                # lower edge of the lowest included bin
    vah_bin: int
    val_bin: int
    value_volume: float
    value_fraction: float     # realised fraction of total volume inside the VA

    vwap: float
    vwap_upper_1sd: float
    vwap_lower_1sd: float
    vwap_upper_2sd: float
    vwap_lower_2sd: float

    delta_poc_bin: int = 0
    delta_poc_price: float = 0.0

    hvns: list = field(default_factory=list)
    lvns: list = field(default_factory=list)

    @property
    def value_width(self):
        return self.vah - self.val

    def position_of(self, price):
        """Where a price sits relative to value - the state machine's input."""
        if price > self.vah:
            return "ABOVE_VALUE"
        if price < self.val:
            return "BELOW_VALUE"
        return "INSIDE_VALUE"

    def nearest_lvn_between(self, price, target_price):
        """The closest qualifying LVN strictly between two prices.

        Used by S1-LVN to find its entry: the thin area standing between current
        price and the POC magnet. Returns None when the path is unobstructed,
        which is itself a valid answer - it means there is no rejection level to
        trade and the setup does not exist.
        """
        low, high = sorted((float(price), float(target_price)))
        candidates = [
            node for node in self.lvns
            if low < node.center_price < high
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda node: abs(node.center_price - price))

    def nearest_lvn_beyond(self, price, direction, outer_bound=None):
        """The closest LVN strictly on one side of `price` - a stop reference.

        `direction`: "ABOVE" or "BELOW", the side to search. `outer_bound`, when
        given, excludes anything past it (e.g. the prior session's own high/low) -
        without one a thin area far outside the session's traded range could be
        found and used as a stop reference for a level that has no real bearing on
        this session's structure.

        Returns None when nothing qualifies, which is itself a valid answer: the
        caller falls back to the value-area edge or a pure volatility floor rather
        than inventing a level that is not there.
        """
        price = float(price)
        candidates = [node for node in self.lvns
                     if (node.center_price > price if direction == "ABOVE"
                         else node.center_price < price)]
        if outer_bound is not None:
            outer_bound = float(outer_bound)
            candidates = [node for node in candidates
                         if (node.center_price <= outer_bound if direction == "ABOVE"
                             else node.center_price >= outer_bound)]
        if not candidates:
            return None
        return min(candidates, key=lambda node: abs(node.center_price - price))

    def hvn_containing(self, price):
        for node in self.hvns:
            if node.contains(price):
                return node
        return None


def _poc_bin(profile, vwap_hint):
    """Highest-volume bin, with a specified tie-break.

    Ties resolve to the bin nearest the session VWAP, then to the lower bin.
    Nearest-VWAP is not arbitrary: when two bins genuinely tie, the one closer to
    the volume-weighted mean is the better representative of where business was
    done, and it keeps the chosen POC inside the value area rather than at an
    edge.
    """
    best_index = None
    best_volume = -1.0
    for index, volume in profile.volume.items():
        if volume <= 0:
            continue
        if volume > best_volume:
            best_index, best_volume = index, volume
            continue
        if volume == best_volume and best_index is not None:
            current = abs(profile.bin_center(index) - vwap_hint)
            incumbent = abs(profile.bin_center(best_index) - vwap_hint)
            if current < incumbent or (current == incumbent and index < best_index):
                best_index = index
    return best_index, max(best_volume, 0.0)


def _vwap_and_bands(profile):
    """Volume-weighted mean price and its standard-deviation bands.

    Computed from the PROFILE rather than from candle typical prices, so VWAP and
    the POC are derived from one histogram and cannot disagree about the same
    session. A candle-based VWAP would be marginally more precise and would
    introduce a second source of truth, which is the worse trade.
    """
    total = sum(volume for volume in profile.volume.values() if volume > 0)
    if total <= 0:
        return 0.0, 0.0
    mean = sum(profile.bin_center(index) * volume
               for index, volume in profile.volume.items() if volume > 0) / total
    variance = sum(volume * (profile.bin_center(index) - mean) ** 2
                   for index, volume in profile.volume.items() if volume > 0) / total
    return mean, math.sqrt(max(variance, 0.0))


def _value_area(profile, poc_bin, value_area_pct):
    """Expand outward from the POC by BIN PAIRS until the target is covered.

    Returns (low_bin, high_bin, covered_volume). The loop terminates either on
    reaching the target or on exhausting the histogram, so a profile whose total
    is concentrated in fewer bins than the target implies still yields bounds.
    """
    histogram = dict(profile.histogram())
    if not histogram:
        return poc_bin, poc_bin, 0.0

    indices = sorted(histogram)
    lowest, highest = indices[0], indices[-1]
    total = sum(histogram.values())
    target = total * float(value_area_pct)

    low = high = poc_bin
    covered = histogram.get(poc_bin, 0.0)

    def pair_above(cursor):
        return (histogram.get(cursor + 1, 0.0), histogram.get(cursor + 2, 0.0))

    def pair_below(cursor):
        return (histogram.get(cursor - 1, 0.0), histogram.get(cursor - 2, 0.0))

    while covered < target and (low > lowest or high < highest):
        can_go_up = high < highest
        can_go_down = low > lowest

        up_sum = sum(pair_above(high)) if can_go_up else -1.0
        down_sum = sum(pair_below(low)) if can_go_down else -1.0

        # Tie -> extend DOWNWARD. Specified so live and replay agree; the choice
        # itself is conventional and its effect is at most one bin.
        if can_go_up and (not can_go_down or up_sum > down_sum):
            for _ in range(2):
                if high >= highest:
                    break
                high += 1
                covered += histogram.get(high, 0.0)
        elif can_go_down:
            for _ in range(2):
                if low <= lowest:
                    break
                low -= 1
                covered += histogram.get(low, 0.0)
        else:
            break

    return low, high, covered


def _find_nodes(profile, poc_volume):
    """Locate HVN peaks and LVN valleys across the contiguous histogram.

    A node is a RUN of bins, not a single bin: an LVN one bin wide on a thin
    symbol is usually a gap in the tick grid rather than a real absence of
    interest, which is why LVN_MIN_WIDTH_BINS exists and defaults above one.

    HVN and LVN are defined by depth relative to the POC bin rather than by local
    curvature, because curvature on a noisy histogram finds dozens of meaningless
    inflections while the auction meaning of these levels is about ABSOLUTE
    participation: an HVN is somewhere business was genuinely done, an LVN is
    somewhere it was not.
    """
    histogram = profile.histogram()
    if not histogram or poc_volume <= 0:
        return [], []

    hvn_floor = poc_volume * config.HVN_PCT
    lvn_ceiling = poc_volume * config.LVN_PCT

    hvns, lvns = [], []

    def flush(run, kind, out):
        if not run:
            return
        indices = [index for index, _ in run]
        volumes = [volume for _, volume in run]
        if kind == "LVN" and len(indices) < config.LVN_MIN_WIDTH_BINS:
            return
        peak_at = (indices[volumes.index(max(volumes))] if kind == "HVN"
                   else indices[volumes.index(min(volumes))])
        out.append(Node(
            kind=kind,
            low_bin=indices[0],
            high_bin=indices[-1],
            low_price=profile.bin_low(indices[0]),
            high_price=profile.bin_high(indices[-1]),
            peak_bin=peak_at,
            peak_price=profile.bin_center(peak_at),
            volume=sum(volumes),
            volume_pct_of_poc=(max(volumes) if kind == "HVN" else min(volumes)) / poc_volume,
        ))

    current_hvn, current_lvn = [], []
    for index, volume in histogram:
        if volume >= hvn_floor:
            current_hvn.append((index, volume))
        else:
            flush(current_hvn, "HVN", hvns)
            current_hvn = []

        if volume <= lvn_ceiling:
            current_lvn.append((index, volume))
        else:
            flush(current_lvn, "LVN", lvns)
            current_lvn = []

    flush(current_hvn, "HVN", hvns)
    flush(current_lvn, "LVN", lvns)

    return hvns, lvns


def compute(profile, value_area_pct=None):
    """Full level set for a profile, or None when the histogram is empty."""
    value_area_pct = (config.VALUE_AREA_PCT if value_area_pct is None
                      else value_area_pct)
    if not profile.volume:
        return None

    vwap, sigma = _vwap_and_bands(profile)
    poc_bin, poc_volume = _poc_bin(profile, vwap)
    if poc_bin is None:
        return None

    low_bin, high_bin, covered = _value_area(profile, poc_bin, value_area_pct)
    total = sum(v for v in profile.volume.values() if v > 0)

    occupied = [v for v in profile.volume.values() if v > 0]
    mean_bin_volume = (sum(occupied) / len(occupied)) if occupied else 0.0
    prominence = (poc_volume / mean_bin_volume) if mean_bin_volume > 0 else 0.0

    delta_bin = poc_bin
    if profile.delta:
        delta_bin = max(profile.delta.items(), key=lambda kv: abs(kv[1]))[0]

    hvns, lvns = _find_nodes(profile, poc_volume)

    return Levels(
        poc_bin=poc_bin,
        poc_price=profile.bin_center(poc_bin),
        poc_volume=poc_volume,
        poc_prominence=prominence,
        vah=profile.bin_high(high_bin),
        val=profile.bin_low(low_bin),
        vah_bin=high_bin,
        val_bin=low_bin,
        value_volume=covered,
        value_fraction=(covered / total) if total > 0 else 0.0,
        vwap=vwap,
        vwap_upper_1sd=vwap + sigma,
        vwap_lower_1sd=vwap - sigma,
        vwap_upper_2sd=vwap + 2 * sigma,
        vwap_lower_2sd=vwap - 2 * sigma,
        delta_poc_bin=delta_bin,
        delta_poc_price=profile.bin_center(delta_bin),
        hvns=hvns,
        lvns=lvns,
    )
