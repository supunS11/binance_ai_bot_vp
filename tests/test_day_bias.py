import unittest
from types import SimpleNamespace

from profile.shape import day_bias


def _shape(label, migration=None, close_location=None):
    return SimpleNamespace(label=label, intra_poc_migration_atr=migration,
                           close_location=close_location)


class DayBiasMappingTests(unittest.TestCase):
    def test_no_shape_means_no_bias(self):
        self.assertIsNone(day_bias(None))

    def test_confirmed_p_is_bull_and_confirmed_b_is_bear(self):
        self.assertEqual(day_bias(_shape("P", close_location=0.8)), "BULL")
        self.assertEqual(day_bias(_shape("b", close_location=0.2)), "BEAR")

    def test_d_is_neutral_whatever_the_migration(self):
        self.assertEqual(day_bias(_shape("D", migration=2.0)), "NEUTRAL")

    def test_trend_and_double_distribution_follow_the_migration(self):
        self.assertEqual(day_bias(_shape("trend", migration=0.8)), "BULL")
        self.assertEqual(day_bias(_shape("B", migration=-0.8)), "BEAR")

    def test_unmeasured_or_zero_migration_is_neutral_not_guessed(self):
        self.assertEqual(day_bias(_shape("trend", migration=None)), "NEUTRAL")
        self.assertEqual(day_bias(_shape("B", migration=0.0)), "NEUTRAL")


class DayBiasCloseLocationTests(unittest.TestCase):
    """2026-10-09: P/b only confirmed when the session's own close held what it
    took - see day_bias's docstring for the validated numbers this is from."""

    def test_p_closing_in_the_lower_half_is_unconfirmed_not_bull(self):
        self.assertEqual(day_bias(_shape("P", close_location=0.3)), "NEUTRAL")

    def test_b_closing_in_the_upper_half_is_unconfirmed_not_bear(self):
        self.assertEqual(day_bias(_shape("b", close_location=0.7)), "NEUTRAL")

    def test_missing_close_location_is_unconfirmed_not_guessed(self):
        self.assertEqual(day_bias(_shape("P", close_location=None)), "NEUTRAL")
        self.assertEqual(day_bias(_shape("b", close_location=None)), "NEUTRAL")

    def test_exactly_half_is_not_confirmed_either_side(self):
        # The boundary belongs to neither side - P needs STRICTLY above half,
        # b STRICTLY below, so a dead-center close confirms nothing.
        self.assertEqual(day_bias(_shape("P", close_location=0.5)), "NEUTRAL")
        self.assertEqual(day_bias(_shape("b", close_location=0.5)), "NEUTRAL")

    def test_require_close_location_false_restores_the_unconditional_vote(self):
        """The escape hatch for a still-developing session's shape (see
        profile/bias.py's _dev_vote) - not independently validated, used
        deliberately only where the completed-session condition cannot apply."""
        self.assertEqual(
            day_bias(_shape("P", close_location=0.1), require_close_location=False),
            "BULL")
        self.assertEqual(
            day_bias(_shape("b", close_location=0.9), require_close_location=False),
            "BEAR")


if __name__ == "__main__":
    unittest.main()
