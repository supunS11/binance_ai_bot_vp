import unittest
from unittest.mock import patch

import config
from profile import va_hvn


class _Profile:
    bin_size = 1.0

    def __init__(self, volumes):
        self._volumes = volumes

    def volume_at(self, index):
        return float(self._volumes.get(index, 0.0))

    def bin_low(self, index):
        return float(index)

    def bin_high(self, index):
        return float(index + 1)

    def bin_center(self, index):
        return index + 0.5


class _Levels:
    def __init__(self, poc_bin, poc_volume, val_bin, vah_bin):
        self.poc_bin = poc_bin
        self.poc_volume = poc_volume
        self.val_bin = val_bin
        self.vah_bin = vah_bin
        self.poc_price = poc_bin + 0.5
        self.vah = self.val = self.vwap = float("nan")


def _va_profile(overrides, base=10.0, va=(10, 30)):
    volumes = {i: base for i in range(va[0], va[1] + 1)}
    volumes.update(overrides)
    return _Profile(volumes)


def _levels_for(profile, va=(10, 30)):
    poc_bin = max(range(va[0], va[1] + 1), key=profile.volume_at)
    return _Levels(poc_bin, profile.volume_at(poc_bin), va[0], va[1])


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.profile = _va_profile({
            18: 20, 19: 60, 20: 100, 21: 60, 22: 20,
            24: 10, 25: 65, 26: 80, 27: 65, 28: 10,
        })
        self.levels = _levels_for(self.profile)

    def test_poc_is_always_first_and_secondary_peak_is_kept(self):
        nodes = va_hvn.detect(self.profile, self.levels, atr=10.0)
        self.assertEqual(nodes[0].peak_bin, 20)
        self.assertIn(26, [n.peak_bin for n in nodes])

    def test_peak_outside_value_area_is_ignored(self):
        profile = _va_profile({20: 100, 35: 95}, va=(10, 30))
        levels = _levels_for(profile)
        nodes = va_hvn.detect(profile, levels, atr=10.0)
        self.assertNotIn(35, [n.peak_bin for n in nodes])

    def test_flat_shoulder_without_a_dip_is_rejected(self):
        flat = {i: 74.0 for i in range(10, 31)}
        flat.update({20: 100.0, 26: 80.0})
        profile = _Profile(flat)
        levels = _Levels(20, 100.0, 10, 30)
        nodes = va_hvn.detect(profile, levels, atr=100.0)
        self.assertEqual([n.peak_bin for n in nodes], [20])

    def test_max_count_keeps_the_strongest_secondary(self):
        profile = _va_profile({12: 65, 13: 70, 14: 65, 20: 100, 25: 65, 26: 80, 27: 65})
        levels = _levels_for(profile)
        with patch.object(config, "VA_HVN_MAX_COUNT", 1):
            nodes = va_hvn.detect(profile, levels, atr=10.0)
        self.assertEqual([n.peak_bin for n in nodes], [20, 26])

    def test_zone_width_is_capped_around_the_peak(self):
        profile = _va_profile({20: 100, 22: 70, 23: 75, 24: 80, 25: 75, 26: 70})
        levels = _levels_for(profile)
        with patch.object(config, "VA_HVN_MAX_WIDTH_ATR", 1.0):
            nodes = va_hvn.detect(profile, levels, atr=2.0)
        secondary = [n for n in nodes if n.peak_bin == 24]
        self.assertTrue(secondary)
        self.assertLessEqual(secondary[0].high_bin - secondary[0].low_bin + 1, 2)


class StabilityTests(unittest.TestCase):
    def setUp(self):
        self.profile = _va_profile({20: 100, 25: 65, 26: 80, 27: 65})
        self.levels = _levels_for(self.profile)

    def test_peak_that_does_not_persist_under_rebuild_is_dropped(self):
        rebuilt_without = _va_profile({20: 100, 12: 65, 13: 80, 14: 65})
        perturbed = [(rebuilt_without, _levels_for(rebuilt_without), 0.75)]
        nodes = va_hvn.detect(self.profile, self.levels, atr=10.0, perturbed=perturbed)
        self.assertEqual([n.peak_bin for n in nodes], [20])

    def test_peak_that_persists_under_rebuild_is_kept(self):
        rebuilt_with = _va_profile({20: 100, 25: 65, 26: 80, 27: 65})
        perturbed = [(rebuilt_with, _levels_for(rebuilt_with), 0.75)]
        nodes = va_hvn.detect(self.profile, self.levels, atr=10.0, perturbed=perturbed)
        self.assertIn(26, [n.peak_bin for n in nodes])

    def test_no_perturbations_applies_no_stability_filter(self):
        nodes = va_hvn.detect(self.profile, self.levels, atr=10.0, perturbed=())
        self.assertIn(26, [n.peak_bin for n in nodes])


class PlanRuleTests(unittest.TestCase):
    def test_plateau_is_not_a_local_maximum(self):
        profile = _va_profile({19: 80, 20: 80, 21: 80, 22: 80, 23: 80, 24: 80, 25: 80,
                               26: 80, 27: 80, 28: 80})
        levels = _Levels(20, 80.0, 10, 30)
        nodes = va_hvn.detect(profile, levels, atr=100.0)
        self.assertEqual([n.peak_bin for n in nodes], [20])

    def test_single_bin_spike_fails_the_two_bin_width_rule(self):
        profile = _va_profile({20: 100, 26: 80})
        levels = _levels_for(profile)
        nodes = va_hvn.detect(profile, levels, atr=10.0)
        self.assertNotIn(26, [n.peak_bin for n in nodes])

    def test_growth_stops_at_the_flanking_trough(self):
        profile = _va_profile({20: 100, 21: 90, 22: 70, 23: 40, 24: 55, 25: 60, 26: 65})
        zone = va_hvn._grow(profile, 20, 10, 30, 50.0)
        self.assertEqual(zone, (20, 22))

    def test_ranking_uses_peak_volume_then_prominence(self):
        profile = _va_profile({20: 100, 12: 65, 13: 70, 14: 65, 25: 65, 26: 80, 27: 65})
        levels = _levels_for(profile)
        with patch.object(config, "VA_HVN_MAX_COUNT", 1):
            nodes = va_hvn.detect(profile, levels, atr=10.0)
        self.assertEqual([n.peak_bin for n in nodes], [20, 26])

    def test_nodes_carry_peak_volume_and_poc_flag(self):
        profile = _va_profile({18: 20, 19: 60, 20: 100, 21: 60, 22: 20,
                               24: 10, 25: 65, 26: 80, 27: 65, 28: 10})
        levels = _levels_for(profile)
        nodes = va_hvn.detect(profile, levels, atr=10.0)
        self.assertTrue(nodes[0].is_poc)
        self.assertEqual(nodes[0].peak_volume, 100.0)
        self.assertTrue(all(not n.is_poc for n in nodes[1:]))


class DefaultsTests(unittest.TestCase):
    def test_method_defaults_to_the_plan_mechanism(self):
        self.assertEqual(config.HVN_METHOD, "va_plan")

    def test_plan_thresholds_are_the_recommended_starting_points(self):
        self.assertEqual(config.VA_HVN_MIN_POC_FRACTION, 0.60)
        self.assertEqual(config.VA_HVN_MIN_MEAN_MULTIPLE, 1.75)
        self.assertEqual(config.VA_HVN_GROW_FRACTION, 0.50)
        self.assertEqual(config.VA_HVN_MAX_WIDTH_ATR, 1.0)
        self.assertEqual(config.VA_HVN_MAX_COUNT, 3)


if __name__ == "__main__":
    unittest.main()


class _Candle:
    def __init__(self, low, high):
        self.low = low
        self.high = high


class TouchAndUsageTests(unittest.TestCase):
    def test_touch_count_counts_separate_visits_not_bars_inside(self):
        candles = [_Candle(0, 1), _Candle(9, 11), _Candle(10, 10.5), _Candle(0, 1),
                   _Candle(9.5, 12), _Candle(0, 1)]
        self.assertEqual(va_hvn.touch_count(candles, 10.0, 11.0), 2)

    def test_touch_count_is_zero_when_zone_never_visited(self):
        self.assertEqual(va_hvn.touch_count([_Candle(0, 1)], 10.0, 11.0), 0)

    def test_usage_reports_entry_in_zone_and_first_test(self):
        profile = _va_profile({18: 20, 19: 60, 20: 100, 21: 60, 22: 20,
                               24: 10, 25: 65, 26: 80, 27: 65, 28: 10})
        levels = _levels_for(profile)
        nodes = va_hvn.detect(profile, levels, atr=10.0)
        poc = next(n for n in nodes if n.is_poc)
        entry = poc.peak_price - 0.5
        read = va_hvn.usage(entry, "BUY", 10.0, levels, None, nodes, tests=(0,) * len(nodes))
        self.assertEqual(read["entry_in_zone"], 1)
        self.assertEqual(read["first_test"], 1)

    def test_usage_is_none_safe_without_a_target_side_node(self):
        profile = _va_profile({20: 100})
        levels = _levels_for(profile)
        nodes = va_hvn.detect(profile, levels, atr=10.0)
        read = va_hvn.usage(levels.poc_price + 5.0, "BUY", 10.0, levels, None, nodes, ())
        self.assertIsNone(read["nearest_atr"])
