"""Test factories: hand-built candles and profiles with known properties.

These exist so setup tests assert against distributions whose POC, value area and
shape are known by construction rather than by running the code under test and
believing whatever it returns. A test that computes its own expectation from the
implementation proves only that the implementation is self-consistent.
"""
from dataclasses import dataclass

from profile import builder, levels as levels_mod, shape as shape_mod

MINUTE = 60_000


@dataclass(frozen=True)
class FakeSpec:
    """Minimal SymbolSpec stand-in. Values chosen to be permissive so a test
    failure means the logic under test failed, not that a filter intervened."""
    symbol: str = "TESTUSDT"
    tick_size: str = "0.01"
    min_price: str = "0.01"
    max_price: str = "1000000"
    step_size: str = "0.001"
    min_qty: str = "0.001"
    max_qty: str = "100000"
    market_step_size: str = "0.001"
    market_min_qty: str = "0.001"
    market_max_qty: str = "10000"
    min_notional: str = "5"
    multiplier_up: str = "5"
    multiplier_down: str = "0.2"
    max_num_orders: int = 200
    max_num_algo_orders: int = 10
    price_precision: int = 2
    quantity_precision: int = 3
    base_asset: str = "TEST"
    quote_asset: str = "USDT"
    status: str = "TRADING"
    contract_type: str = "PERPETUAL"


@dataclass(frozen=True)
class C:
    """Candle with the same surface as data.klines.Candle."""
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float
    trades: int
    taker_buy_base: float
    taker_buy_quote: float

    @property
    def delta_base(self):
        return 2.0 * self.taker_buy_base - self.volume

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


def candle(ts, o, h, l, c, volume=10.0, buy_fraction=0.5):
    """One candle. `buy_fraction` drives signed taker delta: 0.5 is neutral."""
    return C(
        open_time=ts, open=o, high=h, low=l, close=c, volume=volume,
        close_time=ts + MINUTE - 1, quote_volume=volume * c, trades=25,
        taker_buy_base=volume * buy_fraction,
        taker_buy_quote=volume * buy_fraction * c,
    )


def boxy_session(start_ts, center=100.0, half_width=1.0, tail=0.25,
                 core_candles=300, tail_candles=20, volume=10.0, buy_fraction=0.5):
    """A PLATYKURTIC balanced session - the real-world D-shape, not a Gaussian.

    Built as a dense uniform core spanning +/-half_width with short thin tails of
    `tail` beyond it. This is deliberately the shape real balanced sessions take:
    volume spread fairly evenly across an agreed band, with brief rejected
    extremes. A Gaussian with long 3-sigma tails is NOT that shape, and using one
    as the balanced fixture is what hid the classifier's dead zone.

    PRICES OSCILLATE ACROSS THE CORE RATHER THAN RAMPING THROUGH IT. An earlier
    version walked price monotonically from the low edge to the high edge, which
    produces the right HISTOGRAM but the wrong SESSION - it trends, and measured an
    intra-session POC migration of +0.4 ATR against a 0.5 threshold. A balanced
    auction rotates around an agreed price; a fixture that quietly trends is not
    testing balance, and would have made the migration check look near-useless.
    """
    candles = []
    ts = start_ts
    step = (2 * half_width) / max(1, core_candles)

    # Sweep outward from the centre, alternating sides, so both halves of the
    # session cover the same band and the POC does not migrate.
    offsets = []
    for index in range((core_candles + 1) // 2):
        offsets.append(index * step)
        offsets.append(-index * step)
    offsets = offsets[:core_candles]

    for offset in offsets:
        price = center + offset
        candles.append(candle(ts, price, price + step * 0.5,
                              price - step * 0.5, price, volume,
                              buy_fraction=buy_fraction))
        ts += MINUTE
    for index in range(tail_candles):
        offset = tail * (index + 1) / tail_candles
        for price in (center + half_width + offset, center - half_width - offset):
            candles.append(candle(ts, price, price + step * 0.5,
                                  price - step * 0.5, price, volume * 0.08))
            ts += MINUTE
    return candles


def trend_session(start_ts, start_price=100.0, end_price=106.0,
                  candles_count=300, volume=10.0):
    """A one-sided discovery session: value narrow relative to a long travel."""
    candles = []
    ts = start_ts
    step = (end_price - start_price) / max(1, candles_count)
    for index in range(candles_count):
        price = start_price + index * step
        candles.append(candle(ts, price, price + step, price, price + step,
                              volume, buy_fraction=0.62))
        ts += MINUTE
    return candles


def make_profile(symbol, candles, window_start, window_end, bin_size=0.05,
                 as_of=None):
    """Profile + levels + shape for a candle list, as the live path computes them."""
    as_of = candles[-1].close_time if as_of is None else as_of
    profile = builder.profile_as_of(symbol, candles, window_start, window_end,
                                    as_of, bin_size)
    levels = levels_mod.compute(profile)
    shape = shape_mod.classify(profile, levels) if levels else None
    return profile, levels, shape
