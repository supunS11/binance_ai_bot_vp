import unittest
from types import SimpleNamespace


from profile.bias import session_bias


def _shape(label, migration=None, close_location=None):
    # Default close_location CONFIRMS P/b (see day_bias) so existing fixtures
    # calling _shape("P")/_shape("b") keep getting the unconditional vote they
    # were written to expect; pass close_location explicitly to test the
    # unconfirmed case.
    if close_location is None:
        close_location = {"P": 0.8, "b": 0.2}.get(label)
    return SimpleNamespace(label=label, intra_poc_migration_atr=migration,
                           close_location=close_location)


def _migration(label, poc_shift_atr=0.0):
    return SimpleNamespace(label=label, poc_shift_atr=poc_shift_atr,
                           directional=label in ("HIGHER", "LOWER"))


def _open(label, side=""):
    return SimpleNamespace(label=label, side=side)


def _ctx(prior_shape=None, migration=None, open_relationship=None, dev_shape=None,
        mature=False, weekly_levels=None, last_price=0.0, atr=1.0):
    return SimpleNamespace(
        prior_shape=prior_shape, migration=migration, open_relationship=open_relationship,
        dev_shape=dev_shape, maturity={"mature": mature},
        weekly_levels=weekly_levels, last_price=last_price, atr=atr,
    )


def _weekly(poc_price):
    return SimpleNamespace(poc_price=poc_price)


class SessionBiasTests(unittest.TestCase):
    def test_no_signal_at_all_is_neutral_with_zero_strength(self):
        self.assertEqual(session_bias(_ctx()), ("NEUTRAL", 0))

    def test_a_single_vote_sets_the_label(self):
        self.assertEqual(session_bias(_ctx(prior_shape=_shape("P"))), ("BULL", 1))
        self.assertEqual(session_bias(_ctx(prior_shape=_shape("b"))), ("BEAR", 1))

    def test_agreeing_votes_add_strength(self):
        ctx = _ctx(prior_shape=_shape("P"), migration=_migration("HIGHER", 0.5),
                   open_relationship=_open("OUTSIDE_VALUE_INSIDE_RANGE", "ABOVE"))
        self.assertEqual(session_bias(ctx), ("BULL", 3))

    def test_a_tie_is_neutral_but_keeps_the_vote_count(self):
        ctx = _ctx(prior_shape=_shape("P"), migration=_migration("LOWER", -0.5))
        self.assertEqual(session_bias(ctx), ("NEUTRAL", 1))

    def test_an_inside_value_open_casts_no_vote(self):
        ctx = _ctx(open_relationship=_open("INSIDE_VALUE", ""))
        self.assertEqual(session_bias(ctx), ("NEUTRAL", 0))

    def test_a_non_directional_migration_casts_no_vote(self):
        ctx = _ctx(migration=_migration("UNCHANGED", 0.01))
        self.assertEqual(session_bias(ctx), ("NEUTRAL", 0))

    def test_developing_shape_only_votes_once_the_session_is_mature(self):
        immature = _ctx(dev_shape=_shape("P"), mature=False)
        mature = _ctx(dev_shape=_shape("P"), mature=True)
        self.assertEqual(session_bias(immature), ("NEUTRAL", 0))
        self.assertEqual(session_bias(mature), ("BULL", 1))

    def test_four_signals_can_agree(self):
        ctx = _ctx(prior_shape=_shape("P"), migration=_migration("HIGHER", 0.5),
                   open_relationship=_open("OUTSIDE_VALUE_INSIDE_RANGE", "ABOVE"),
                   dev_shape=_shape("P"), mature=True)
        self.assertEqual(session_bias(ctx), ("BULL", 4))

    def test_all_five_signals_can_agree(self):
        ctx = _ctx(prior_shape=_shape("P"), migration=_migration("HIGHER", 0.5),
                   open_relationship=_open("OUTSIDE_VALUE_INSIDE_RANGE", "ABOVE"),
                   dev_shape=_shape("P"), mature=True,
                   weekly_levels=_weekly(90.0), last_price=100.0)
        self.assertEqual(session_bias(ctx), ("BULL", 5))


class WeeklyPocVoteTests(unittest.TestCase):
    """Validated 2026-10-09 against 280 real S4-OFR trades - see _weekly_vote's own
    docstring for the numbers. Independent of the other four votes (confound
    check: bias_strength AUC null on the same aligned/counter split), so it is
    tested on its own here rather than only through the composite."""

    def test_price_above_the_weekly_poc_votes_bull(self):
        ctx = _ctx(weekly_levels=_weekly(90.0), last_price=100.0)
        self.assertEqual(session_bias(ctx), ("BULL", 1))

    def test_price_below_the_weekly_poc_votes_bear(self):
        ctx = _ctx(weekly_levels=_weekly(110.0), last_price=100.0)
        self.assertEqual(session_bias(ctx), ("BEAR", 1))

    def test_price_exactly_on_the_weekly_poc_casts_no_vote(self):
        ctx = _ctx(weekly_levels=_weekly(100.0), last_price=100.0)
        self.assertEqual(session_bias(ctx), ("NEUTRAL", 0))

    def test_no_weekly_bundle_casts_no_vote(self):
        """A fresh listing with no completed prior calendar week - not an error."""
        ctx = _ctx(weekly_levels=None, last_price=100.0)
        self.assertEqual(session_bias(ctx), ("NEUTRAL", 0))

    def test_one_vote_each_way_against_another_signal_is_a_tie(self):
        ctx = _ctx(prior_shape=_shape("b"), weekly_levels=_weekly(90.0), last_price=100.0)
        self.assertEqual(session_bias(ctx), ("NEUTRAL", 1),
                         "one BEAR vs one BULL is a tie, not a win for either side")


if __name__ == "__main__":
    unittest.main()
