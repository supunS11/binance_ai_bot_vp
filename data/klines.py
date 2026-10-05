"""Kline retrieval, the Candle type, and the few derived measures the system
needs from raw candles.

The Candle carries SIGNED TAKER FLOW, which is the single most useful thing in a
Binance kline and the reason this system can build an order-flow profile from
plain historical data. Field 9 of every kline is taker_buy_base_volume: the
portion of that candle's volume where the BUYER was the aggressor. So

    taker_buy  = tbbav
    taker_sell = volume - tbbav
    delta      = tbbav - (volume - tbbav) = 2*tbbav - volume

is exact, not estimated, and available for full history. Most order-flow data is
either unavailable historically (book depth) or capped to a few days
(aggregated trades), which makes anything built on it unbackTestable. This is
not.

CLOSED CANDLES ONLY. `fetch` drops the final candle when it is still forming.
An in-progress candle's high, low, close and volume all change, so any level
computed from it repaints - and a backtest that uses it is reading the future.
The drop happens here, once, rather than being each caller's responsibility.
"""
import logging
from dataclasses import dataclass

import config
import sessions

log = logging.getLogger(__name__)

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000,
    "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000,
}


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float          # base asset
    close_time: int
    quote_volume: float    # quote asset - the comparable measure across symbols
    trades: int
    taker_buy_base: float
    taker_buy_quote: float

    @property
    def delta_base(self):
        """Signed taker flow in base units: positive = buyers were aggressive."""
        return 2.0 * self.taker_buy_base - self.volume

    @property
    def delta_quote(self):
        return 2.0 * self.taker_buy_quote - self.quote_volume

    @property
    def range(self):
        return self.high - self.low

    @property
    def body(self):
        return abs(self.close - self.open)

    @property
    def bullish(self):
        return self.close > self.open

    @property
    def upper_wick(self):
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self):
        return min(self.open, self.close) - self.low

    @property
    def typical_price(self):
        return (self.high + self.low + self.close) / 3.0


def parse_kline(row):
    """One raw kline array -> Candle.

    Binance returns every numeric as a string; converting once here means no
    downstream code has to remember to.
    """
    return Candle(
        open_time=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        close_time=int(row[6]),
        quote_volume=float(row[7]),
        trades=int(row[8]),
        taker_buy_base=float(row[9]),
        taker_buy_quote=float(row[10]),
    )


def fetch(rest, symbol, interval, start_ms, end_ms, drop_unclosed=True):
    """Every closed candle in [start_ms, end_ms), paginating as needed.

    Pagination walks forward from the last candle received rather than by a
    computed offset, so a venue-side gap (a maintenance window, a newly listed
    symbol) advances the cursor instead of looping forever on an empty range.
    """
    step = INTERVAL_MS.get(interval)
    if step is None:
        raise ValueError(f"unsupported interval {interval}")

    out = []
    cursor = int(start_ms)
    end_ms = int(end_ms)
    now = sessions.now_ms()
    guard = 0

    while cursor < end_ms:
        guard += 1
        if guard > 500:
            log.warning("%s %s: pagination guard tripped at %s",
                        symbol, interval, cursor)
            break

        rows = rest.klines(symbol, interval, start_ms=cursor,
                           end_ms=end_ms, limit=1500)
        if not rows:
            break

        batch = [parse_kline(row) for row in rows]
        out.extend(batch)

        advanced = batch[-1].open_time + step
        if advanced <= cursor:
            break
        cursor = advanced

        if len(rows) < 1500:
            break

    # De-duplicate: overlapping page boundaries can repeat a candle.
    seen = {}
    for candle in out:
        seen[candle.open_time] = candle
    candles = [seen[key] for key in sorted(seen)]

    if drop_unclosed:
        candles = [c for c in candles if c.close_time < now]

    return [c for c in candles if start_ms <= c.open_time < end_ms]


def resample(candles, target_interval, source_interval="1m"):
    """Aggregate finer candles into coarser ones, aligned to epoch boundaries.

    WHY AGGREGATE LOCALLY INSTEAD OF FETCHING. The confirmation timeframe is derived
    from the same 1m candles the profile is built from, which buys two things: one
    fewer request per symbol per cycle (the difference between fitting inside the
    rate-limit budget and not), and a guarantee that the profile and the
    confirmation candles can never disagree about the same minute.

    Alignment is to the epoch, matching how the venue defines its own buckets, so a
    locally-built 15m candle has the same open_time as the venue's 15m candle. That
    equivalence is worth having: it makes a discrepancy a bug rather than a
    convention difference.

    Only COMPLETE buckets are returned - a partial trailing bucket would repaint as
    more 1m candles arrive, which is the repainting problem the closed-candle rule
    exists to prevent.
    """
    target_ms = INTERVAL_MS.get(target_interval)
    source_ms = INTERVAL_MS.get(source_interval)
    if not target_ms or not source_ms or target_ms < source_ms:
        raise ValueError(f"cannot resample {source_interval} -> {target_interval}")
    if target_ms % source_ms != 0:
        raise ValueError(f"{target_interval} is not a multiple of {source_interval}")

    per_bucket = target_ms // source_ms
    buckets = {}
    for candle in candles:
        key = (candle.open_time // target_ms) * target_ms
        buckets.setdefault(key, []).append(candle)

    out = []
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda c: c.open_time)
        if len(group) < per_bucket:
            continue                      # incomplete bucket: would repaint
        out.append(Candle(
            open_time=key,
            open=group[0].open,
            high=max(c.high for c in group),
            low=min(c.low for c in group),
            close=group[-1].close,
            volume=sum(c.volume for c in group),
            close_time=key + target_ms - 1,
            quote_volume=sum(c.quote_volume for c in group),
            trades=sum(c.trades for c in group),
            taker_buy_base=sum(c.taker_buy_base for c in group),
            taker_buy_quote=sum(c.taker_buy_quote for c in group),
        ))
    return out


def find_gaps(candles, interval):
    """Missing intervals inside an otherwise contiguous series.

    A profile built across a gap is not wrong so much as unlabelled - it covers
    less time than it claims. Callers decide whether that matters; this just
    makes it visible instead of silent.
    """
    step = INTERVAL_MS.get(interval)
    if step is None or len(candles) < 2:
        return []
    gaps = []
    for previous, current in zip(candles, candles[1:]):
        expected = previous.open_time + step
        if current.open_time > expected:
            missing = (current.open_time - expected) // step
            gaps.append({"after": previous.open_time,
                         "before": current.open_time,
                         "missing": int(missing)})
    return gaps


def coverage(candles, interval, start_ms, end_ms):
    """Fraction of the requested window actually covered by candles."""
    step = INTERVAL_MS.get(interval)
    if not step:
        return 0.0
    expected = max(1, (int(end_ms) - int(start_ms)) // step)
    return min(1.0, len(candles) / expected)


# ------------------------------------------------------------------ measures

def true_range(current, previous):
    if previous is None:
        return current.range
    return max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


def atr(candles, period=14):
    """Wilder ATR over the given candles.

    ATR is used throughout this system as a UNIT OF MEASURE, never as a signal -
    it converts "0.3 ATR beyond the level" into a price, and normalises distances
    so a threshold means the same thing on BTCUSDT as on a 40-cent alt. That is
    why an indicator appears at all in a profile-only system (principle P1).
    """
    if len(candles) < 2:
        return candles[0].range if candles else 0.0
    period = max(1, min(int(period), len(candles) - 1))

    ranges = [true_range(candles[i], candles[i - 1])
              for i in range(1, len(candles))]
    value = sum(ranges[:period]) / period
    for tr in ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def daily_atr(daily_candles, period=None):
    """ATR on 1d candles - the reference scale for bin width and stop buffers.

    Daily rather than intraday deliberately: bin width must be stable for a
    whole session, and an intraday ATR recomputed each cycle would shift the bin
    lattice mid-session, changing the levels with no change in the trades.
    """
    period = config.BIN_ATR_PERIOD_DAYS if period is None else period
    return atr(daily_candles, period=period)


def session_quote_volume(candles):
    return sum(candle.quote_volume for candle in candles)


def typical_session_volume(daily_candles, lookback=14):
    """Median recent daily quote volume, for the developing-maturity check.

    Median, not mean: one liquidation cascade or listing pump would otherwise
    raise the bar so high that no ordinary session ever looks mature.
    """
    if not daily_candles:
        return 0.0
    recent = [c.quote_volume for c in daily_candles[-lookback:] if c.quote_volume > 0]
    if not recent:
        return 0.0
    recent.sort()
    middle = len(recent) // 2
    if len(recent) % 2:
        return recent[middle]
    return (recent[middle - 1] + recent[middle]) / 2.0
