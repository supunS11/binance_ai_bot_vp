"""Profile construction: candles in, volume-at-price histogram out.

Three decisions in this module determine the quality of everything downstream,
so each is stated explicitly rather than left as an implementation detail.

1. THE BIN LATTICE IS ABSOLUTE. bin_index(p) = floor(p / bin_size), anchored at
   zero. Not anchored at the session low, not at the current price, not at the
   window's first trade. This sounds pedantic and is not: if the grid origin or
   width depends on where price happens to be when the profile is computed, then
   the same set of trades produces different bins at different times, and a POC
   that moves when nothing happened cannot be traded. An absolute lattice makes
   binning a pure function of the trades.

2. BIN WIDTH IS ATR-NORMALISED WITH A TICK FLOOR, not a fixed row count. Fixed
   row counts - what charting platforms default to - make bin width a function of
   session range, so a wide day and a quiet day are measured on different rulers
   and no threshold can mean the same thing on both. Normalising by daily ATR
   makes "0.3 ATR from the POC" comparable across symbols and days. The tick
   floor exists because a bin finer than the venue's own price increment is
   fiction: it creates bins that no trade can ever land in, which shows up as a
   spuriously spiky histogram.

3. VOLUME IS SPREAD ACROSS EACH CANDLE'S RANGE, not dumped at its close. A 1m
   candle has a high and a low, and its volume genuinely transacted across that
   span. Assigning all of it to the close produces a histogram of closes rather
   than of traded volume - visibly spikier, with a POC that jumps between
   adjacent bins on re-computation. Uniform spreading weighted by bin overlap is
   the standard approximation and, at 1m granularity, the residual error inside
   any one candle is small against the session range.

   INTRA_CANDLE_DISTRIBUTION="close" exists so the difference can be measured in
   Phase 4. It is not a supported live setting.

Signed taker delta is accumulated with exactly the same spreading, giving a
per-bin buy/sell imbalance for free - see data/klines.Candle.delta_base.
"""
import copy
import logging
import math
from dataclasses import dataclass, field

import config

log = logging.getLogger(__name__)


def compute_bin_size(daily_atr, tick_size, atr_fraction=None):
    """Bin width for a symbol: max(tick_size, round_to_tick(ATR * fraction)).

    Snapped to the tick lattice so bin edges are themselves representable
    prices. An un-snapped bin width would place edges between valid prices,
    which makes a level like VAH unquotable without a second rounding step.
    """
    atr_fraction = (config.BIN_ATR_FRACTION if atr_fraction is None
                    else atr_fraction)
    tick = float(tick_size) if tick_size else 0.0

    raw = float(daily_atr) * float(atr_fraction)
    if tick > 0:
        snapped = math.floor(raw / tick) * tick
        return max(tick, snapped)
    return max(raw, 1e-12)


@dataclass
class Profile:
    """A volume-at-price histogram over one window, plus its own provenance.

    `as_of` is part of the identity, not metadata: a developing profile computed
    at 06:00 and the same window computed at 18:00 are different objects and must
    never be confused. Every level derived from this profile is only valid for
    decisions taken at or after `as_of`.
    """
    symbol: str
    window_start: int
    window_end: int
    as_of: int
    bin_size: float

    # bin_index -> accumulated value. Sparse dicts, because a session touches a
    # narrow band of an absolute lattice that spans all of price space.
    volume: dict = field(default_factory=dict)       # base asset
    delta: dict = field(default_factory=dict)        # signed taker flow, base
    quote: dict = field(default_factory=dict)        # quote asset
    trades: dict = field(default_factory=dict)       # trade count

    # Session bounds as traded, not as binned.
    high: float = 0.0
    low: float = 0.0
    open: float = 0.0
    close: float = 0.0

    total_volume: float = 0.0
    total_quote_volume: float = 0.0
    total_trades: int = 0
    candle_count: int = 0
    coverage: float = 0.0
    source_interval: str = ""
    distribution: str = ""

    # --------------------------------------------------------- bin geometry

    def bin_index(self, price):
        return int(math.floor(float(price) / self.bin_size))

    def bin_low(self, index):
        return index * self.bin_size

    def bin_high(self, index):
        return (index + 1) * self.bin_size

    def bin_center(self, index):
        return (index + 0.5) * self.bin_size

    @property
    def occupied_bins(self):
        """Bin indices carrying volume, ascending. The histogram's support."""
        return sorted(index for index, vol in self.volume.items() if vol > 0)

    @property
    def bin_count(self):
        return len(self.occupied_bins)

    @property
    def range(self):
        return self.high - self.low

    def volume_at(self, index):
        return self.volume.get(index, 0.0)

    def delta_at(self, index):
        return self.delta.get(index, 0.0)

    def histogram(self):
        """(index, volume) pairs over the CONTIGUOUS span, zeros included.

        Contiguity matters for every algorithm that walks outward from the POC
        or looks for a valley: a sparse dict would silently skip empty bins, and
        an empty bin is exactly what a low-volume node is made of.
        """
        indices = self.occupied_bins
        if not indices:
            return []
        return [(index, self.volume.get(index, 0.0))
                for index in range(indices[0], indices[-1] + 1)]

    def volume_between(self, low_price, high_price):
        """Volume transacted within a price band - the acceptance measurement.

        Partial bins are prorated by overlap rather than counted whole, because
        the band edges are real prices (a value-area bound) that will rarely
        coincide with a bin edge.
        """
        if high_price < low_price:
            low_price, high_price = high_price, low_price
        total = 0.0
        for index, vol in self.volume.items():
            if vol <= 0:
                continue
            bin_lo, bin_hi = self.bin_low(index), self.bin_high(index)
            overlap = min(bin_hi, high_price) - max(bin_lo, low_price)
            if overlap <= 0:
                continue
            total += vol * (overlap / self.bin_size)
        return total

    def describe(self):
        return (f"{self.symbol} [{self.window_start}..{self.window_end}) "
                f"as_of={self.as_of} bins={self.bin_count} "
                f"bin_size={self.bin_size:.10g} vol={self.total_volume:.4g}")


def _spread_candle(profile, candle):
    """Distribute one candle's volume across the bins its range overlaps.

    Weight per bin is the fraction of the candle's high-low span that falls
    inside that bin. A zero-range candle - every trade at one price, common on
    illiquid alts - is assigned wholly to its own bin, which is correct rather
    than a special case.
    """
    low, high = candle.low, candle.high
    if high < low:
        low, high = high, low

    span = high - low
    first = profile.bin_index(low)
    last = profile.bin_index(high)

    if span <= 0 or first == last:
        weights = {first: 1.0}
    else:
        weights = {}
        for index in range(first, last + 1):
            overlap = min(profile.bin_high(index), high) - max(profile.bin_low(index), low)
            if overlap > 0:
                weights[index] = overlap / span
        if not weights:
            weights = {first: 1.0}
        else:
            # Renormalise: float division can leave the sum a hair off 1.0, and
            # over 1440 candles that drift would misstate total volume.
            scale = sum(weights.values())
            weights = {index: weight / scale for index, weight in weights.items()}

    return weights


def _assign_at_close(profile, candle):
    return {profile.bin_index(candle.close): 1.0}


def build(symbol, candles, window_start, window_end, as_of, bin_size,
          source_interval=None, distribution=None, coverage=1.0):
    """Accumulate candles into a Profile.

    Candles outside [window_start, window_end) are ignored, and - the contract
    that prevents lookahead - so is any candle whose close_time exceeds `as_of`.
    That check lives here rather than in callers so there is exactly one place it
    can be forgotten, and profile_as_of() below is the only intended entry point.
    """
    distribution = (config.INTRA_CANDLE_DISTRIBUTION if distribution is None
                    else distribution)
    profile = Profile(
        symbol=symbol.upper(),
        window_start=int(window_start),
        window_end=int(window_end),
        as_of=int(as_of),
        bin_size=float(bin_size),
        source_interval=source_interval or config.PROFILE_SOURCE_INTERVAL,
        distribution=distribution,
        coverage=coverage,
    )

    usable = [
        candle for candle in candles
        if window_start <= candle.open_time < window_end
        and candle.close_time <= as_of
    ]
    key = (symbol.upper(), int(window_start), int(window_end), float(bin_size),
           source_interval or config.PROFILE_SOURCE_INTERVAL, distribution, coverage)
    profile, consumed = _resume(key, usable, as_of)
    if profile is None:
        profile, consumed = _fresh(symbol, window_start, window_end, as_of, bin_size,
                                   source_interval, distribution, coverage), 0
    if not usable:
        return profile

    spread = _assign_at_close if distribution == "close" else _spread_candle

    for position in range(consumed, len(usable)):
        candle = usable[position]
        if position == 0:
            profile.open = candle.open
            profile.high = candle.high
            profile.low = candle.low
        else:
            profile.high = max(profile.high, candle.high)
            profile.low = min(profile.low, candle.low)
        profile.close = candle.close

        weights = spread(profile, candle)
        for index, weight in weights.items():
            if weight <= 0:
                continue
            profile.volume[index] = profile.volume.get(index, 0.0) + candle.volume * weight
            profile.quote[index] = profile.quote.get(index, 0.0) + candle.quote_volume * weight
            profile.delta[index] = profile.delta.get(index, 0.0) + candle.delta_base * weight
            profile.trades[index] = profile.trades.get(index, 0.0) + candle.trades * weight

        profile.total_volume += candle.volume
        profile.total_quote_volume += candle.quote_volume
        profile.total_trades += candle.trades

    profile.candle_count = len(usable)
    _remember(key, profile, usable)

    if profile.bin_count > config.BIN_MAX_COUNT:
        log.warning("%s profile has %s bins (cap %s) - bin_size %.10g may be "
                    "too fine for this symbol's range",
                    symbol, profile.bin_count, config.BIN_MAX_COUNT,
                    profile.bin_size)

    return profile


# THE INCREMENTAL CACHE. A developing profile is rebuilt at every decision step, and a full
# rebuild costs the whole session's candles each time, so a day's replay is quadratic. The
# cache keeps the last accumulated profile per (symbol, window, bin geometry, settings) and
# folds in only the candles that closed since. Candles are folded in the same order a full
# build uses, so the result is bit-identical to a rebuild; a test asserts that. A cached
# state is used only when the candles still begin with the ones already folded in.
_INCREMENTAL = {}
_INCREMENTAL_LIMIT = 512


def _fresh(symbol, window_start, window_end, as_of, bin_size, source_interval,
           distribution, coverage):
    return Profile(
        symbol=symbol.upper(),
        window_start=int(window_start),
        window_end=int(window_end),
        as_of=int(as_of),
        bin_size=float(bin_size),
        source_interval=source_interval or config.PROFILE_SOURCE_INTERVAL,
        distribution=distribution,
        coverage=coverage,
    )


def _clone(profile, as_of):
    clone = copy.copy(profile)
    clone.as_of = int(as_of)
    clone.volume = dict(profile.volume)
    clone.delta = dict(profile.delta)
    clone.quote = dict(profile.quote)
    clone.trades = dict(profile.trades)
    return clone


def _prefix_digest(candles):
    return hash(tuple((c.open_time, c.close_time, c.open, c.high, c.low, c.close,
                       c.volume, c.quote_volume, c.trades, c.taker_buy_base)
                      for c in candles))


def _resume(key, usable, as_of):
    """A copy of the cached profile holding the first `count` usable candles, plus that
    count, or (None, 0) when the candles no longer begin with the ones already folded in."""
    cached = _INCREMENTAL.get(key)
    if cached is None:
        return None, 0
    count = cached["count"]
    if not (0 < count <= len(usable)):
        return None, 0
    if _prefix_digest(usable[:count]) != cached["digest"]:
        return None, 0
    return _clone(cached["profile"], as_of), count


def _remember(key, profile, usable):
    if len(_INCREMENTAL) >= _INCREMENTAL_LIMIT:
        _INCREMENTAL.clear()
    _INCREMENTAL[key] = {"profile": _clone(profile, profile.as_of), "count": len(usable),
                         "digest": _prefix_digest(usable)}


def profile_as_of(symbol, candles, window_start, window_end, as_of, bin_size,
                  source_interval=None, distribution=None, coverage=1.0):
    """THE accessor. Uses only candles closed at or before `as_of`.

    Live code passes the latest closed-candle time; replay passes its simulated
    clock. Because there is no second path into the builder that skips the
    filter, lookahead is not a discipline anyone has to remember - it is simply
    unavailable. tests/test_profile_asof.py asserts the contract directly.
    """
    return build(symbol, candles, window_start, window_end, as_of, bin_size,
                 source_interval=source_interval, distribution=distribution,
                 coverage=coverage)
