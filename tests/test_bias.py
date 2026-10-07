import unittest
from types import SimpleNamespace


from profile.bias import session_bias


def _shape(label, migration=None):
    return SimpleNamespace(label=label, intra_poc_migration_atr=migration)


def _migration(label, poc_shift_atr=0.0):
    return SimpleNamespace(label=label, poc_shift_atr=poc_shift_atr,
                           directional=label in ("HIGHER", "LOWER"))


def _open(label, side=""):
    return SimpleNamespace(label=label, side=side)


def _ctx(prior_shape=None, migration=None, open_relationship=None, dev_shape=None,
        mature=False):
    return SimpleNamespace(
        prior_shape=prior_shape, migration=migration, open_relationship=open_relationship,
        dev_shape=dev_shape, maturity={"mature": mature},
    )


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


if __name__ == "__main__":
    unittest.main()
