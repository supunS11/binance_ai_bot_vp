import random
import unittest

from data.klines import Candle
from profile import builder

MIN = 60_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % (24 * 60 * MIN))


def _candles(count, seed):
    rng = random.Random(seed)
    out, price = [], 100.0
    for i in range(count):
        open_ = price
        close = max(1.0, price + rng.uniform(-0.4, 0.4))
        high, low = max(open_, close) + rng.uniform(0, 0.2), min(open_, close) - rng.uniform(0, 0.2)
        volume = rng.uniform(1, 50)
        taker = volume * rng.uniform(0.2, 0.8)
        out.append(Candle(open_time=T0 + i * MIN, open=open_, high=high, low=low, close=close,
                          volume=volume, close_time=T0 + i * MIN + MIN - 1,
                          quote_volume=volume * close, trades=rng.randint(1, 30),
                          taker_buy_base=taker, taker_buy_quote=taker * close))
        price = close
    return out


def _snapshot(profile):
    return (profile.as_of, profile.volume, profile.delta, profile.quote, profile.trades,
            profile.high, profile.low, profile.open, profile.close, profile.total_volume,
            profile.total_quote_volume, profile.total_trades, profile.candle_count)


def _full(candles, end, as_of, bin_size):
    builder._INCREMENTAL.clear()
    return _snapshot(builder.profile_as_of("TESTUSDT", candles, T0, end, as_of, bin_size))


class IncrementalParityTests(unittest.TestCase):
    def setUp(self):
        builder._INCREMENTAL.clear()

    def test_stepping_through_a_session_matches_a_full_rebuild_exactly(self):
        candles = _candles(240, seed=7)
        bin_size = 0.25
        for step in range(15, 241, 15):
            as_of = candles[step - 1].close_time
            expected = _full(candles, T0 + 240 * MIN, as_of, bin_size)
            actual = _snapshot(builder.profile_as_of("TESTUSDT", candles, T0, T0 + 240 * MIN,
                                                     as_of, bin_size))
            self.assertEqual(actual, expected, f"diverged at step {step}")

    def test_a_different_candle_set_with_the_same_timestamps_is_not_reused(self):
        first = _candles(60, seed=1)
        second = _candles(60, seed=2)
        as_of = first[-1].close_time
        builder.profile_as_of("TESTUSDT", first, T0, T0 + 60 * MIN, as_of, 0.25)
        actual = _snapshot(builder.profile_as_of("TESTUSDT", second, T0, T0 + 60 * MIN,
                                                 second[-1].close_time, 0.25))
        self.assertEqual(actual, _full(second, T0 + 60 * MIN, second[-1].close_time, 0.25))

    def test_returned_profiles_are_not_aliased_by_later_extension(self):
        candles = _candles(40, seed=3)
        early = builder.profile_as_of("TESTUSDT", candles, T0, T0 + 40 * MIN,
                                      candles[19].close_time, 0.25)
        before = dict(early.volume)
        builder.profile_as_of("TESTUSDT", candles, T0, T0 + 40 * MIN,
                              candles[-1].close_time, 0.25)
        self.assertEqual(early.volume, before)


if __name__ == "__main__":
    unittest.main()
