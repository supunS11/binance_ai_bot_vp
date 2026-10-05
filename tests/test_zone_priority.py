import unittest
from types import SimpleNamespace
from unittest.mock import patch

import config
import state_machine
from setups.base import Candidate, RejectReason, SetupState, reject

ATR = 1.0


def _candidate(setup, level):
    return Candidate(setup=setup, symbol="TESTUSDT", direction="SELL", state=SetupState.TRIGGERED,
                     level_price=level, level_kind="VAH", entry_price=level,
                     stop_price=level + 0.5, target_price=level - 1.0, session_id="2026-01-01",
                     as_of=0, atr=ATR)


def _ctx():
    return SimpleNamespace(symbol="TESTUSDT", session_id="2026-01-01",
                           open_relationship=SimpleNamespace(label="INSIDE_VALUE"))


class _Stubs:
    def __init__(self, s2_level, s4_level):
        self.s2_level = s2_level
        self.s4_level = s4_level

    def s2(self, ctx):
        if self.s2_level is None:
            return reject(RejectReason.PRICE_NOT_AT_LEVEL, "S2-VAR", ctx.symbol)
        return _candidate("S2-VAR", self.s2_level)

    def s4(self, ctx):
        if self.s4_level is None:
            return reject(RejectReason.NO_ORDER_FLOW_ZONE, "S4-OFR", ctx.symbol)
        return _candidate("S4-OFR", self.s4_level)

    def s3(self, ctx):
        return reject(RejectReason.NO_EXCURSION, "S3-BRK", ctx.symbol)


def _run(machine, stubs, research_mode=False):
    stubs_map = {"S2-VAR": stubs.s2, "S4-OFR": stubs.s4, "S3-BRK": stubs.s3}
    with patch.dict(state_machine.DETECTORS, stubs_map):
        return machine.evaluate(_ctx(), research_mode=research_mode)


class SharedZonePriorityTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(config, "ACTIVE_SETUPS", ["S2-VAR", "S4-OFR"])
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_same_tick_s4_wins_a_shared_zone(self):
        machine = state_machine.StateMachine()
        winner, rejections = _run(machine, _Stubs(s2_level=101.0, s4_level=101.0 + 0.5 * config.OFR_TOUCH_TOL_ATR))
        self.assertEqual(winner.setup, "S4-OFR")
        claimed = [r for r in rejections if r.reason == RejectReason.ZONE_CLAIMED_BY_S4]
        self.assertEqual([r.setup for r in claimed], ["S2-VAR"])

    def test_s2_is_blocked_on_a_zone_s4_already_traded(self):
        machine = state_machine.StateMachine()
        _run(machine, _Stubs(s2_level=None, s4_level=None))
        machine.record_taken("TESTUSDT", "2026-01-01", "S4-OFR", 101.0)
        winner, rejections = _run(machine, _Stubs(s2_level=101.0, s4_level=None))
        self.assertIsNone(winner)
        self.assertIn(RejectReason.ZONE_CLAIMED_BY_S4, [r.reason for r in rejections])

    def test_s2_on_a_distant_zone_is_unaffected(self):
        machine = state_machine.StateMachine()
        machine.record_taken("TESTUSDT", "2026-01-01", "S4-OFR", 101.0)
        winner, _ = _run(machine, _Stubs(s2_level=95.0, s4_level=None))
        self.assertEqual(winner.setup, "S2-VAR")

    def test_s2_trade_does_not_block_a_later_s4_on_the_same_zone(self):
        machine = state_machine.StateMachine()
        machine.record_taken("TESTUSDT", "2026-01-01", "S2-VAR", 101.0)
        winner, _ = _run(machine, _Stubs(s2_level=None, s4_level=101.0))
        self.assertEqual(winner.setup, "S4-OFR")

    def test_research_mode_measures_each_setup_independently(self):
        machine = state_machine.StateMachine()
        machine.record_taken("TESTUSDT", "2026-01-01", "S4-OFR", 101.0)
        candidates, _ = _run(machine, _Stubs(s2_level=101.0, s4_level=101.0), research_mode=True)
        self.assertEqual(sorted(c.setup for c in candidates), ["S2-VAR", "S4-OFR"])


class ActiveSetupsTests(unittest.TestCase):
    def test_default_active_setup_is_s4_only(self):
        self.assertEqual(config.ACTIVE_SETUPS, ["S4-OFR"])

    def test_inactive_setup_is_rejected_before_its_detector_runs(self):
        machine = state_machine.StateMachine()
        with patch.object(config, "ACTIVE_SETUPS", ["S4-OFR"]):
            winner, rejections = _run(machine, _Stubs(s2_level=101.0, s4_level=None))
        self.assertIsNone(winner)
        s2 = [r for r in rejections if r.setup == "S2-VAR"]
        self.assertEqual([r.reason for r in s2], [RejectReason.SETUP_DISABLED])


if __name__ == "__main__":
    unittest.main()
