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


if __name__ == "__main__":
    unittest.main()
