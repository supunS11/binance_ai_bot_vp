"""The journal, end to end. Another path that had never executed.

`trade_journal` had zero rows: no trade had ever been opened, closed or settled, so
every SQL statement in this module was unverified. Two of them carry real consequences:

  stats_today() FEEDS THE DAILY LOSS LIMIT AND THE LOSS STREAK. It is read from the
      database rather than from memory precisely so a restart cannot reset a risk
      control - which means a bug here silently removes the control instead of
      crashing.

  record_trade_close() MATCHES AN OPEN ROW BY SYMBOL. If the match is wrong, a close
      lands on the wrong trade, and every R statistic downstream is computed from
      mismatched pairs.
"""
import os
import tempfile
import unittest

import sessions
from execution.positions import ManagedPosition
from journal.sinks import Journal


def _managed(symbol="TESTUSDT", setup="S1-POC", direction="BUY",
             entry=100.0, stop=99.0, target=102.0, quantity=1.0):
    return ManagedPosition(
        symbol=symbol, setup=setup, direction=direction,
        session_id=sessions.session_id(sessions.now_ms()),
        entry_price=entry, stop_price=stop, target_price=target,
        quantity=quantity, opened_at=sessions.now_ms(),
    )


class _Candidate:
    """Minimal candidate for record_trade_open."""
    def __init__(self, managed):
        self.setup = managed.setup
        self.symbol = managed.symbol
        self.direction = managed.direction
        self.session_id = managed.session_id
        self.entry_price = managed.entry_price
        self.stop_price = managed.stop_price
        self.target_price = managed.target_price
        self.quantity = managed.quantity
        self.level_price = managed.entry_price
        self.level_kind = "POC"
        self.atr = 1.0
        self.as_of = managed.opened_at
        self.state = "FILLED"
        self.notional = managed.entry_price * managed.quantity
        self.risk_amount = 25.0
        self.attributes = {"target_kind": "fixed_r"}
        self.profile_snapshot = {"shape": "D"}
        self.client_order_id = "vpeTEST0000000000"
        self.confirmations = []

    @property
    def risk_distance(self):
        return abs(self.entry_price - self.stop_price)

    @property
    def r_multiple(self):
        return abs(self.target_price - self.entry_price) / self.risk_distance

    def round_trip_cost_r(self):
        return 0.02

    def net_r_at_target(self):
        return self.r_multiple - 0.02

    def as_row(self):
        return {"setup": self.setup, "symbol": self.symbol}


class JournalRoundTripTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(handle)
        os.unlink(self.path)
        self.journal = Journal(self.path)

    def tearDown(self):
        try:
            self.journal.close()
        except Exception:                             # noqa: BLE001
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def _open_and_close(self, net_r, symbol="TESTUSDT", reason="STOP", closed_at=None):
        managed = _managed(symbol=symbol)
        closed_at = closed_at if closed_at is not None else sessions.now_ms()
        self.journal.record_trade_open(managed, _Candidate(managed))
        self.journal.record_trade_close(managed, {
            "exit_price": 99.0, "exit_reason": reason,
            "gross_r": net_r, "net_r": net_r, "commission": 0.02,
            "pnl": None if net_r is None else net_r * 25.0,
            "closed_at": closed_at,
            "bars_held": 1.0,
        })
        return managed

    def test_open_then_close_round_trips(self):
        self._open_and_close(-1.02)
        stats = self.journal.stats_today()
        self.assertAlmostEqual(stats["realised_r_today"], -1.02, places=6)
        self.assertEqual(stats["consecutive_losses"], 1)

    def test_daily_realised_r_accumulates(self):
        for value in (-1.0, -1.0, 2.0):
            self._open_and_close(value)
        stats = self.journal.stats_today()
        self.assertAlmostEqual(stats["realised_r_today"], 0.0, places=6)

    def test_loss_streak_breaks_on_a_win(self):
        for value in (-1.0, -1.0, 1.5):
            self._open_and_close(value)
        self.assertEqual(self.journal.stats_today()["consecutive_losses"], 0)

    def test_loss_streak_counts_consecutive_losses(self):
        for value in (1.5, -1.0, -1.0, -1.0):
            self._open_and_close(value)
        self.assertEqual(self.journal.stats_today()["consecutive_losses"], 3)

    def test_last_loss_closed_at_is_the_most_recent_losing_trades_timestamp(self):
        """risk.py's automatic cooldown release (2026-10-10) measures from this -
        it must be the NEWEST loss in the streak, not the oldest or an average."""
        self._open_and_close(-1.0, closed_at=1_000)
        self._open_and_close(-1.0, closed_at=2_000)
        self._open_and_close(-1.0, closed_at=3_000)
        self.assertEqual(self.journal.stats_today()["last_loss_closed_at"], 3_000)

    def test_last_loss_closed_at_is_none_without_a_live_streak(self):
        self._open_and_close(-1.0, closed_at=1_000)
        self._open_and_close(1.5, closed_at=2_000)
        stats = self.journal.stats_today()
        self.assertEqual(stats["consecutive_losses"], 0)
        self.assertIsNone(stats["last_loss_closed_at"])

    def test_an_unknown_exit_does_not_reset_the_loss_streak(self):
        """The risk-control flaw: NULL net_r read as a non-loss.

        `(net_r or 0.0) < 0` turns NULL into 0.0, which breaks the streak and resets the
        consecutive-loss counter - inside the one control meant to stop a bad day
        compounding. Three losses, an unreadable exit, then more losses would never
        reach CONSECUTIVE_LOSS_LIMIT.
        """
        self._open_and_close(-1.0)
        self._open_and_close(-1.0)
        self._open_and_close(None, reason="UNKNOWN")      # exit unreadable
        self._open_and_close(-1.0)

        stats = self.journal.stats_today()
        self.assertEqual(stats["consecutive_losses"], 3,
                         "an unknown outcome must not reset the loss streak")

    def test_unknown_exit_contributes_no_r_to_the_daily_total(self):
        self._open_and_close(-1.0)
        self._open_and_close(None, reason="UNKNOWN")
        stats = self.journal.stats_today()
        self.assertAlmostEqual(stats["realised_r_today"], -1.0, places=6,
                               msg="a NULL R must not be counted as zero profit "
                                   "nor crash the sum")

    def test_close_matches_the_right_open_row_per_symbol(self):
        """A close landing on the wrong trade corrupts every downstream statistic."""
        first = _managed(symbol="AAAUSDT")
        second = _managed(symbol="BBBUSDT")
        self.journal.record_trade_open(first, _Candidate(first))
        self.journal.record_trade_open(second, _Candidate(second))

        self.journal.record_trade_close(second, {
            "exit_price": 102.0, "exit_reason": "TARGET", "gross_r": 2.0,
            "net_r": 1.98, "commission": 0.02, "pnl": 49.5,
            "closed_at": sessions.now_ms(), "bars_held": 2.0,
        })

        stats = self.journal.stats_today()
        self.assertAlmostEqual(stats["realised_r_today"], 1.98, places=6,
                               msg="only BBBUSDT should have closed")

    def test_rejects_are_recorded_without_an_allowlist(self):
        """Any reason string must persist - a reason list always falls behind."""
        from setups.base import RejectReason, reject
        for reason in list(RejectReason)[:12]:
            self.journal.record_reject(
                reject(reason, "S1-POC", "TESTUSDT", direction="BUY",
                       detail="test"),
                session_id=sessions.session_id(sessions.now_ms()))
        tally = self.journal.reject_tally()
        self.assertGreaterEqual(len(tally), 1)

    def test_stats_survive_a_reopen(self):
        """The whole point of reading limits from disk: a restart must not reset them."""
        self._open_and_close(-1.0)
        self._open_and_close(-1.0)
        self.journal.close()

        reopened = Journal(self.path)
        try:
            stats = reopened.stats_today()
            self.assertAlmostEqual(stats["realised_r_today"], -2.0, places=6)
            self.assertEqual(stats["consecutive_losses"], 2,
                             "a restart must not reset the loss streak")
        finally:
            reopened.close()
            self.journal = reopened


if __name__ == "__main__":
    unittest.main(verbosity=2)
