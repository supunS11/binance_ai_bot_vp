"""Coverage for profile/shape.py's excess/poor extreme classification.

CALIBRATION.md finding 24: the old hardcoded poor_ceiling (0.40 * poc_volume) sat
above the 99th percentile of the real distribution - measured across 13,901 profiled
sessions, poor_high/poor_low fired on only 0.3%/0.5% of them. Replaced with
config.POOR_EXTREME_MAX_BIN_VOLUME_PCT (default 0.15, matching EXCESS_MAX_BIN_VOLUME_PCT),
which yields 3.3%/4.0% - a real testable population. These tests exist to prove the
fix is actually WIRED to config, not just declared: the same profile must classify
differently under the old value and the new one.
"""
import unittest
from unittest.mock import patch

import config
from profile import builder, levels as levels_mod, shape as shape_mod


def _profile_with_edge_volume(edge_volume, bin_size=1.0):
    """Nine contiguous bins, POC at the centre, one controlled top-edge bin.

    Bins 0-8: a thin bottom edge (bin 0) that stays "excess" throughout, a tall
    POC at bin 4, and a top edge (bin 8) set to whatever `edge_volume` asks for -
    the value under test.
    """
    profile = builder.Profile(
        symbol="TESTUSDT", window_start=0, window_end=86_400_000,
        as_of=86_400_000, bin_size=bin_size,
        # Bins 6-7 stay thin (<=15% of the POC) in every case, so only the edge
        # bin (8) decides whether the last-3-bins run counts as excess.
        volume={0: 5.0, 1: 8.0, 2: 30.0, 3: 60.0, 4: 100.0,
               5: 60.0, 6: 10.0, 7: 8.0, 8: float(edge_volume)},
    )
    profile.high = profile.bin_high(8)
    profile.low = profile.bin_low(0)
    profile.open = profile.bin_center(4)
    profile.close = profile.bin_center(4)
    profile.candle_count = 100
    profile.total_volume = sum(profile.volume.values())
    profile.total_quote_volume = profile.total_volume
    return profile


class PoorExtremeThresholdTests(unittest.TestCase):
    def test_poor_high_fires_at_a_realistic_volume_the_old_hardcoded_ceiling_missed(self):
        # Edge bin at 20% of the POC's volume (100) - not thin enough to be excess,
        # but far below the old hardcoded 0.40 ceiling (40).
        profile = _profile_with_edge_volume(edge_volume=20.0)
        levels = levels_mod.compute(profile)
        shape = shape_mod.classify(profile, levels)

        self.assertFalse(shape.excess_high, "20% of POC volume is not a thin tail")
        self.assertTrue(shape.poor_high,
                        "20% of POC volume clears the measured 0.15 ceiling")

    def test_poor_high_is_read_from_config_not_hardcoded(self):
        """The same profile must classify differently under the old 0.40 value.

        This is the test that actually catches a regression to the hardcoded
        constant: asserting only the new default's behaviour would still pass if
        someone reintroduced `0.40` as a literal, since 0.15 happens to be this
        module's own default too.
        """
        profile = _profile_with_edge_volume(edge_volume=20.0)
        levels = levels_mod.compute(profile)

        with patch.object(config, "POOR_EXTREME_MAX_BIN_VOLUME_PCT", 0.40):
            shape_old_ceiling = shape_mod.classify(profile, levels)
        shape_new_ceiling = shape_mod.classify(profile, levels)

        self.assertFalse(shape_old_ceiling.poor_high,
                         "old 0.40 ceiling: 20% of POC volume does not clear it")
        self.assertTrue(shape_new_ceiling.poor_high,
                       "measured 0.15 ceiling: 20% of POC volume does clear it")

    def test_thin_edge_is_excess_not_poor(self):
        profile = _profile_with_edge_volume(edge_volume=5.0)
        levels = levels_mod.compute(profile)
        shape = shape_mod.classify(profile, levels)

        self.assertTrue(shape.excess_high)
        self.assertFalse(shape.poor_high)

    def test_poor_low_uses_the_same_config_value(self):
        profile = builder.Profile(
            symbol="TESTUSDT", window_start=0, window_end=86_400_000,
            as_of=86_400_000, bin_size=1.0,
            volume={0: 25.0, 1: 8.0, 2: 30.0, 3: 60.0, 4: 100.0,
                   5: 60.0, 6: 30.0, 7: 8.0, 8: 5.0},
        )
        profile.high = profile.bin_high(8)
        profile.low = profile.bin_low(0)
        profile.candle_count = 100
        profile.total_volume = sum(profile.volume.values())
        profile.total_quote_volume = profile.total_volume
        levels = levels_mod.compute(profile)
        shape = shape_mod.classify(profile, levels)

        self.assertFalse(shape.excess_low, "25% of POC volume is not a thin tail")
        self.assertTrue(shape.poor_low,
                        "25% of POC volume clears the measured 0.15 ceiling")


class BimodalFractionTests(unittest.TestCase):
    """second_mode_fraction/valley_fraction must carry the real ratio the B
    threshold compares against - not just the pass/fail bimodal bool - so a
    calibration sweep can re-test SHAPE_BIMODAL_MIN_SECOND_MODE/_MAX_VALLEY from
    stored rows without rebuilding every profile."""

    def _two_peak_profile(self, second_peak_volume, valley_volume):
        # POC at bin 4 (volume 100). The exclusion zone (abs(index - poc) < 2)
        # covers bins 3-5, so bin 6 is the nearest bin eligible to be the "second
        # peak" - which makes bin 5 the ONLY bin strictly between POC and it, i.e.
        # the entire valley search. Bins 0-2 and 7 are fixed low "noise" bins, well
        # below either volume this is ever called with, so they can never win the
        # best-non-POC-bin race or distort the valley minimum.
        profile = builder.Profile(
            symbol="TESTUSDT", window_start=0, window_end=86_400_000,
            as_of=86_400_000, bin_size=1.0,
            volume={0: 2.0, 1: 3.0, 2: 4.0, 3: 60.0, 4: 100.0,
                   5: float(valley_volume), 6: float(second_peak_volume), 7: 5.0},
        )
        profile.high = profile.bin_high(7)
        profile.low = profile.bin_low(0)
        profile.open = profile.bin_center(4)
        profile.close = profile.bin_center(4)
        profile.candle_count = 100
        profile.total_volume = sum(profile.volume.values())
        profile.total_quote_volume = profile.total_volume
        return profile

    def test_a_qualifying_split_records_both_real_fractions(self):
        profile = self._two_peak_profile(second_peak_volume=70.0, valley_volume=20.0)
        levels = levels_mod.compute(profile)
        shape = shape_mod.classify(profile, levels)

        self.assertTrue(shape.bimodal)
        self.assertEqual(shape.label, "B")
        self.assertAlmostEqual(shape.second_mode_fraction, 0.70, places=6)
        self.assertAlmostEqual(shape.valley_fraction, 0.20, places=6)

    def test_a_second_peak_below_threshold_still_records_its_real_fraction(self):
        """Sub-threshold on purpose: SHAPE_BIMODAL_MIN_SECOND_MODE defaults to 0.60,
        so a 40% second peak fails today's cutoff - but the sweep this exists for
        needs the real 0.40, not a flag that the cutoff was not cleared."""
        profile = self._two_peak_profile(second_peak_volume=40.0, valley_volume=20.0)
        levels = levels_mod.compute(profile)
        shape = shape_mod.classify(profile, levels)

        self.assertFalse(shape.bimodal)
        self.assertAlmostEqual(shape.second_mode_fraction, 0.40, places=6)
        self.assertEqual(shape.valley_fraction, 0.0,
                        "a valley is never located once the second mode itself "
                        "already failed")

    def test_too_few_bins_to_measure_records_zero_for_both(self):
        """len(histogram) < 5 is _bimodality's own "nothing to measure" floor -
        the genuine zero case, distinct from a second peak that was found and
        simply measured small."""
        profile = builder.Profile(
            symbol="TESTUSDT", window_start=0, window_end=86_400_000,
            as_of=86_400_000, bin_size=1.0,
            volume={0: 60.0, 1: 100.0, 2: 60.0},
        )
        profile.high = profile.bin_high(2)
        profile.low = profile.bin_low(0)
        profile.open = profile.bin_center(1)
        profile.close = profile.bin_center(1)
        profile.candle_count = 100
        profile.total_volume = sum(profile.volume.values())
        profile.total_quote_volume = profile.total_volume
        levels = levels_mod.compute(profile)
        shape = shape_mod.classify(profile, levels)

        self.assertFalse(shape.bimodal)
        self.assertEqual(shape.second_mode_fraction, 0.0)
        self.assertEqual(shape.valley_fraction, 0.0)


class CloseLocationTests(unittest.TestCase):
    """close_location is day_bias()'s close-location confirmation for P/b - see
    its docstring for the validated numbers this is calibrated from. Covered at
    classify() level (not just the day_bias mock in test_day_bias.py) so a
    change to the actual (close - low) / range computation is caught here."""

    def _profile_with_close(self, close, bin_size=1.0):
        # Ten contiguous bins, same low-volume-everywhere shape as elsewhere in
        # this file - close_location depends only on high/low/close, not on the
        # volume histogram, so a flat profile isolates it cleanly.
        profile = builder.Profile(
            symbol="TESTUSDT", window_start=0, window_end=86_400_000,
            as_of=86_400_000, bin_size=bin_size,
            volume={i: 10.0 for i in range(10)},
        )
        profile.high = profile.bin_high(9)
        profile.low = profile.bin_low(0)
        profile.open = profile.bin_center(4)
        profile.close = close
        profile.candle_count = 100
        profile.total_volume = sum(profile.volume.values())
        profile.total_quote_volume = profile.total_volume
        return profile

    def test_close_at_the_high_is_one(self):
        profile = self._profile_with_close(close=10.0)  # range is [0, 10)
        levels = levels_mod.compute(profile)
        shape = shape_mod.classify(profile, levels)
        self.assertAlmostEqual(shape.close_location, 1.0, places=6)

    def test_close_at_the_low_is_zero(self):
        profile = self._profile_with_close(close=0.0)
        levels = levels_mod.compute(profile)
        shape = shape_mod.classify(profile, levels)
        self.assertAlmostEqual(shape.close_location, 0.0, places=6)

    def test_close_three_quarters_up_the_range(self):
        profile = self._profile_with_close(close=7.5)  # range is [0, 10)
        levels = levels_mod.compute(profile)
        shape = shape_mod.classify(profile, levels)
        self.assertAlmostEqual(shape.close_location, 0.75, places=6)

    def test_zero_range_is_none_not_a_divide_by_zero(self):
        profile = self._profile_with_close(close=5.0)
        profile.high = profile.low  # degenerate: a session with no range at all
        levels = levels_mod.compute(profile)
        shape = shape_mod.classify(profile, levels)
        self.assertIsNone(shape.close_location)


if __name__ == "__main__":
    unittest.main()
