import unittest
from types import SimpleNamespace

from research.context_labels import (DOWN, ROTATION, UP, allows, swing_points,
                                     swing_structure_context, value_migration_context)


def _bars(points):
    """(high, low) pairs -> bar-like objects."""
    return [SimpleNamespace(high=h, low=l) for h, l in points]


class ValueMigrationContextTests(unittest.TestCase):
    def test_consistent_higher_including_overlapping_is_up(self):
        labels = ["HIGHER", "OVERLAPPING_HIGHER", "HIGHER"]
        self.assertEqual(value_migration_context(labels, 3), UP)

    def test_consistent_lower_is_down(self):
        labels = ["OVERLAPPING_LOWER", "LOWER", "LOWER"]
        self.assertEqual(value_migration_context(labels, 3), DOWN)

    def test_any_overlap_type_or_mixed_is_rotation(self):
        for odd in ("INSIDE", "OUTSIDE", "UNCHANGED", "LOWER"):
            self.assertEqual(value_migration_context(["HIGHER", odd, "HIGHER"], 3), ROTATION)

    def test_only_the_most_recent_days_count(self):
        self.assertEqual(value_migration_context(["HIGHER", "HIGHER", "LOWER"], 2), UP)

    def test_too_little_history_is_none_not_rotation(self):
        self.assertIsNone(value_migration_context(["HIGHER", "HIGHER"], 3))
        self.assertIsNone(value_migration_context(["HIGHER", None, "HIGHER"], 3))


class SwingStructureTests(unittest.TestCase):
    def test_swing_needs_strength_bars_on_both_sides(self):
        bars = _bars([(1, 0), (2, 1), (5, 4), (2, 1), (1, 0)])
        self.assertEqual(swing_points(bars, 2), ([5], []))

    def test_unconfirmed_last_bars_are_never_swings(self):
        bars = _bars([(1, 0.5), (2, 1), (3, 2), (4, 3), (9, 8)])
        self.assertEqual(swing_points(bars, 2), ([], []))

    def test_higher_highs_and_higher_lows_is_up(self):
        # low 1 -> high 5 -> low 3 -> high 7 -> low 4 (then two bars to confirm)
        pts = [(3, 2), (2, 1.5), (1.5, 1), (2, 1.6), (3, 2.5),
               (5, 4), (4, 3.5), (3.6, 3), (4, 3.4), (5, 4.5),
               (7, 6), (6, 5), (5, 4), (6, 4.5), (6.5, 5)]
        self.assertEqual(swing_structure_context(_bars(pts), 2), UP)

    def test_mirror_is_down(self):
        pts = [(-l, -h) for h, l in [(3, 2), (2, 1.5), (1.5, 1), (2, 1.6), (3, 2.5),
                                     (5, 4), (4, 3.5), (3.6, 3), (4, 3.4), (5, 4.5),
                                     (7, 6), (6, 5), (5, 4), (6, 4.5), (6.5, 5)]]
        self.assertEqual(swing_structure_context(_bars(pts), 2), DOWN)

    def test_higher_high_with_lower_low_is_rotation(self):
        # highs 5 -> 7 (higher) but the last swing low 0.5 undercuts the prior 3.
        pts = [(3, 2), (2, 1.5), (1.5, 1), (2, 1.6), (3, 2.5),
               (5, 4), (4, 3.5), (3.6, 3), (4, 3.4), (5, 4.5),
               (7, 6), (6, 5), (5, 0.5), (6, 4.5), (6.5, 5)]
        self.assertEqual(swing_structure_context(_bars(pts), 2), ROTATION)

    def test_too_few_swings_is_none(self):
        self.assertIsNone(swing_structure_context(_bars([(1, 0), (2, 1), (1, 0)]), 2))


class AllowsTests(unittest.TestCase):
    def test_dales_rule(self):
        self.assertTrue(allows(UP, "BUY"))
        self.assertFalse(allows(UP, "SELL"))
        self.assertTrue(allows(DOWN, "SELL"))
        self.assertFalse(allows(DOWN, "BUY"))
        self.assertTrue(allows(ROTATION, "BUY") and allows(ROTATION, "SELL"))
        self.assertTrue(allows(None, "BUY") and allows(None, "SELL"))


if __name__ == "__main__":
    unittest.main()
