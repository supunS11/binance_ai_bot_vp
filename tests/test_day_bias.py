import unittest
from types import SimpleNamespace

from profile.shape import day_bias


def _shape(label, migration=None):
    return SimpleNamespace(label=label, intra_poc_migration_atr=migration)


class DayBiasMappingTests(unittest.TestCase):
    def test_no_shape_means_no_bias(self):
        self.assertIsNone(day_bias(None))

    def test_p_is_bull_and_b_is_bear(self):
        self.assertEqual(day_bias(_shape("P")), "BULL")
        self.assertEqual(day_bias(_shape("b")), "BEAR")

    def test_d_is_neutral_whatever_the_migration(self):
        self.assertEqual(day_bias(_shape("D", migration=2.0)), "NEUTRAL")

    def test_trend_and_double_distribution_follow_the_migration(self):
        self.assertEqual(day_bias(_shape("trend", migration=0.8)), "BULL")
        self.assertEqual(day_bias(_shape("B", migration=-0.8)), "BEAR")

    def test_unmeasured_or_zero_migration_is_neutral_not_guessed(self):
        self.assertEqual(day_bias(_shape("trend", migration=None)), "NEUTRAL")
        self.assertEqual(day_bias(_shape("B", migration=0.0)), "NEUTRAL")


if __name__ == "__main__":
    unittest.main()
