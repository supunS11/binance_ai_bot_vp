"""S6-WF live plumbing: recorder reader, footprint arrays and minute validation, the
'state for today' helpers against the research labels, the shared planning functions, the
persisted state, and the live engine's freshness / restart bookkeeping. Rule equivalence with
the research runner on real sessions is checked separately by research/wf_live_replay.py."""
import gzip
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import config
import sessions
from data import footprint_live as fpl
from data.klines import Candle
from research import wf_state as ws
from research import wf_trade as wt

_patchers = [patch.object(config, "WF_ENABLED", False),
             patch.object(config, "WF_TRADING_ENABLED", False),
             patch.object(config, "WF_MTF_ALIGNMENT", False)]


def setUpModule():
    for p in _patchers:
        p.start()


def tearDownModule():
    for p in _patchers:
        p.stop()


DAY0 = sessions.session_start_ms_from_id("2026-05-01")


def candle(m, o, h, l, c, v, start=DAY0):
    t = start + m * 60_000
    return Candle(open_time=t, open=o, high=h, low=l, close=c, volume=v, close_time=t + 59_999,
                  quote_volume=v * c, trades=1, taker_buy_base=v / 2, taker_buy_quote=v * c / 2)


def gz_member(obj):
    return gzip.compress((json.dumps(obj) + "\n").encode())


class RecorderReaderTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.path = os.path.join(self.root, "trades", "XUSDT", "2026-05-01.jsonl.gz")
        os.makedirs(os.path.dirname(self.path))

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_incremental_members_and_partial_tail(self):
        with open(self.path, "wb") as h:
            h.write(gz_member({"m": DAY0, "lv": {"10.0": [1.0, 2.0]}}))
            h.write(gz_member({"m": DAY0 + 60_000, "lv": {"10.5": [3.0, 0.0]}}))
        r = fpl.RecorderTrades(self.root)
        minutes, gaps = r.day("XUSDT", "2026-05-01")
        self.assertEqual(minutes, {0: {"10.0": [1.0, 2.0]}, 1: {"10.5": [3.0, 0.0]}})
        third = gz_member({"m": DAY0 + 120_000, "lv": {"11.0": [0.5, 0.5]}})
        with open(self.path, "ab") as h:
            h.write(third[:10])                       # a member still being written
        minutes, _ = r.day("XUSDT", "2026-05-01")
        self.assertNotIn(2, minutes)
        with open(self.path, "ab") as h:
            h.write(third[10:])
            h.write(gz_member({"gap_from": DAY0 + 300_000, "gap_to": DAY0 + 420_000}))
        minutes, gaps = r.day("XUSDT", "2026-05-01")
        self.assertEqual(minutes[2], {"11.0": [0.5, 0.5]})
        self.assertEqual(minutes[0], {"10.0": [1.0, 2.0]})       # nothing counted twice
        self.assertEqual(gaps, [(DAY0 + 300_000, DAY0 + 420_000)])

    def test_corrupt_member_is_skipped(self):
        with open(self.path, "wb") as h:
            h.write(gz_member({"m": DAY0, "lv": {"10.0": [1.0, 0.0]}}))
            h.write(b"\x1f\x8b\x08garbage-not-deflate")
            h.write(gz_member({"m": DAY0 + 60_000, "lv": {"10.0": [0.0, 1.0]}}))
        minutes, _ = fpl.RecorderTrades(self.root).day("XUSDT", "2026-05-01")
        self.assertEqual(sorted(minutes), [0, 1])

    def test_missing_file_is_none(self):
        self.assertIsNone(fpl.RecorderTrades(self.root).day("XUSDT", "2026-05-02"))


class FootprintArraysTest(unittest.TestCase):
    def test_rows_match_research_grid(self):
        raw = fpl.day_raw({0: {"10.00": [1.0, 0.0], "10.04": [0.0, 2.0]},
                           3: {"9.99": [1.0, 1.0]}}, 0.05)
        # research/footprint.py: row = floor(price / row_size + 1e-9)
        self.assertEqual(list(raw["minute"]), [0, 3])
        self.assertEqual(list(raw["row"]), [200, 199])
        self.assertEqual(list(raw["buy"]), [1.0, 1.0])
        self.assertEqual(list(raw["sell"]), [2.0, 1.0])
        self.assertEqual(float(raw["row_size"][0]), 0.05)

    def test_upto_excludes_later_minutes(self):
        raw = fpl.day_raw({0: {"1": [1, 0]}, 5: {"1": [1, 0]}}, 0.1, upto=4)
        self.assertEqual(list(raw["minute"]), [0])

    def test_invalid_minutes(self):
        minutes = {0: {"10": [1.0, 1.0]}, 1: {"10": [1.0, 0.0]}, 4: {"10": [1.0, 0.0]}}
        candles = [candle(0, 10, 10, 10, 10, 2.0), candle(1, 10, 10, 10, 10, 5.0),
                   candle(2, 10, 10, 10, 10, 0.0), candle(3, 10, 10, 10, 10, 3.0),
                   candle(4, 10, 10, 10, 10, 1.0)]
        bad = fpl.invalid_minutes(minutes, [], candles, DAY0, 5)
        # 1: total mismatch; 2: no trades and no volume = valid; 3: volume, no record;
        # 5: no candle at all
        self.assertEqual(bad, [1, 3, 5])
        bad = fpl.invalid_minutes(minutes, [(DAY0 + 4 * 60_000, DAY0 + 4 * 60_000 + 10)], candles,
                                  DAY0, 4)
        self.assertEqual(bad, [1, 3, 4])

    def test_ohlc_arrays(self):
        o, h, l, c = fpl.ohlc_arrays([candle(2, 1, 3, 0.5, 2, 1)], DAY0, upto=10)
        self.assertEqual((o[2], h[2], l[2], c[2]), (1, 3, 0.5, 2))
        self.assertTrue(np.isnan(c[1]) and np.isnan(c[3]))


class StateForTodayTest(unittest.TestCase):
    def test_value_before_matches_value_by_day(self):
        rng = np.random.default_rng(3)
        days = [sessions.session_id(DAY0 + k * sessions.MS_DAY) for k in range(40)]
        poc = 100 + np.cumsum(rng.normal(0.3, 1.0, 40))
        rows = [{"day": d, "poc": float(p), "atr": 2.0} for d, p in zip(days, poc)]
        ref = ws.value_by_day(rows)
        for j in range(len(rows)):
            self.assertEqual(ws.value_before(rows[:j], rows[j]["day"]), ref[rows[j]["day"]], j)
        holed = rows[:30] + rows[31:]                    # a missing day breaks contiguity
        self.assertIsNone(ws.value_before(holed[:35], holed[35]["day"]))

    def test_turtle_before_matches_turtle_by_day(self):
        rng = np.random.default_rng(4)
        c = 100 + np.cumsum(rng.normal(0, 1.5, 300))
        h, lo = c + 1, c - 1
        days = [str(k) for k in range(300)]
        ref = ws.turtle_by_day((days, h, lo, c))
        for k in (60, 120, 299):
            self.assertEqual(ws.turtle_before(h[:k], lo[:k], c[:k]), ref[str(k)])


class SharedPlanningTest(unittest.TestCase):
    def test_entry_m_is_next_open(self):
        o = np.array([1.0, 2.0, np.nan])
        arr = (o, o, o, o)
        self.assertEqual(wt.entry_m(arr, 0), (1, 2.0))
        self.assertIsNone(wt.entry_m(arr, 1))               # no next bar
        self.assertIsNone(wt.entry_m(arr, 2))

    def test_target_candidates_naked_pocs(self):
        lv = SimpleNamespace(vah=105, val=95, poc_price=100, hvns=[])
        prof = SimpleNamespace(high=108, low=92)
        prior = [{"poc": 120, "high": 121, "low": 119},      # revisited by the next day
                 {"poc": 90, "high": 125, "low": 118},       # naked
                 {"poc": 99, "high": 124, "low": 117}]       # inside today's range
        c = wt.target_candidates(lv, prof, prior, 98, 102, [], 2.0)
        self.assertIn(90, c)
        self.assertNotIn(120, c)
        self.assertNotIn(99, c)


class WorkflowStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_round_trip_and_zone_watch_untouched(self):
        from ops.runtime import StateStore
        store = StateStore(os.path.join(self.dir, "s.sqlite"))
        store.save_workflow_state("xusdt", json.dumps({"session_id": "2026-05-01",
                                                       "done": {"T1|VAH|BUY|1": "VETO_BREAK"}}))
        self.assertEqual(json.loads(store.load_workflow_state()["XUSDT"])["done"],
                         {"T1|VAH|BUY|1": "VETO_BREAK"})
        self.assertEqual(store.load_zone_watches(), {})
        store.close()


class _NoData:
    """A provider whose state is never computable: observe() must stay quiet."""
    recorder = None

    def daily(self, symbol, start):
        return []

    def values(self, symbol, sid, daily, tick):
        return []

    def hist15(self, symbol, a, b):
        return []


class WorkflowEngineTest(unittest.TestCase):
    def test_unclear_state_trades_nothing(self):
        from setups import workflow as wfm
        engine = wfm.Workflow(_NoData())
        engine._data.recorder = SimpleNamespace(drop_before=lambda sid: None)
        lv = SimpleNamespace(vah=105, val=95, poc_price=100, hvns=[])
        prof = SimpleNamespace(high=108, low=92, low_price=92, volume={}, bin_center=lambda i: 0,
                               open=100, close=100)
        candles = tuple(candle(m, 100, 100, 100, 100, 1) for m in range(5))
        ctx = SimpleNamespace(symbol="XUSDT", spec=SimpleNamespace(tick_size=0.01),
                              session_id="2026-05-01", as_of=candles[-1].close_time,
                              prior_levels=lv, prior_profile=prof, atr=2.0,
                              session_candles=candles, profile_row=lambda: {})
        with patch.object(wfm.ws, "prior_shape_label", return_value=None):
            cand, rej = engine.observe(ctx)
        self.assertIsNone(cand)
        self.assertEqual(rej, [])
        self.assertEqual(engine._days["XUSDT"].state, ws.UNCLEAR)

    def test_restore_marks_levels_done(self):
        from setups import workflow as wfm
        store = SimpleNamespace(load_workflow_state=lambda: {
            "XUSDT": json.dumps({"session_id": "2026-05-01", "done": {"R": "SIGNAL"}})})
        engine = wfm.Workflow(_NoData(), store)
        engine.restore()
        self.assertEqual(engine._done_for("XUSDT", "2026-05-01"), {"R": "SIGNAL"})
        self.assertEqual(engine._done_for("XUSDT", "2026-05-02"), {})   # a new session starts clean

    def test_gap_rules(self):
        from setups import workflow as wfm
        T = 100 + wfm.wo.DAY
        self.assertEqual(wfm.Workflow._gap(set(), 100, 130), "")
        ref = set(range(T - 30, T - 30 + wfm.REF_INVALID_MAX))
        self.assertEqual(wfm.Workflow._gap(ref, 100, 130), "")
        self.assertIn("reference", wfm.Workflow._gap(ref | {T - 1}, 100, 130))
        self.assertIn("inside", wfm.Workflow._gap({T + 5}, 100, 130))
        self.assertEqual(wfm.Workflow._gap({T + 40}, 100, 130), "")       # not read yet


class AlignmentSwitchTest(unittest.TestCase):
    def _day(self, state, align, veto=False):
        from setups import workflow as wfm
        return wfm._Day(symbol="X", sid="2026-05-01", start=DAY0, state=state, turtle=None,
                        value=None, veto=veto, shape=None, levels=None, prof=None, atr=1.0,
                        tick=0.01, daily=[], prior_values=[], hist15=[], rs_today=0.1,
                        rs_prev=0.1, align=align)

    def test_off_trades_every_trend_day(self):
        from research import wf_composite as wc
        for align in (wc.ALIGNED, wc.RANGE, wc.CONFLICT, ""):
            d = self._day(ws.TREND_UP, align)
            self.assertTrue(d.trades_today and d.trend_paths, align)
        self.assertFalse(self._day(ws.TREND_UP, wc.ALIGNED, veto=True).trades_today)

    def test_on_applies_playbook_5a(self):
        from research import wf_composite as wc
        with patch.object(config, "WF_MTF_ALIGNMENT", True):
            aligned, rng, conflict = (self._day(ws.TREND_UP, a) for a in
                                      (wc.ALIGNED, wc.RANGE, wc.CONFLICT))
            self.assertTrue(aligned.trades_today and aligned.trend_paths)
            self.assertTrue(rng.trades_today)
            self.assertFalse(rng.trend_paths)                 # only the FC lower edge
            self.assertFalse(conflict.trades_today)
            self.assertTrue(self._day(ws.BALANCE, "").trades_today)


class ObservationSwitchTest(unittest.TestCase):
    def test_workflow_needs_its_own_switch(self):
        import main
        wf = SimpleNamespace(setup="S6-WF")
        s4 = SimpleNamespace(setup="S4-OFR")
        with patch.object(config, "TRADING_ENABLED", True):
            self.assertTrue(main.observation_only(wf, True))          # WF_TRADING_ENABLED off
            self.assertFalse(main.observation_only(s4, True))         # other setups unaffected
            self.assertTrue(main.observation_only(s4, False))         # not reconciled
            with patch.object(config, "WF_TRADING_ENABLED", True):
                self.assertFalse(main.observation_only(wf, True))
        with patch.object(config, "TRADING_ENABLED", False), \
                patch.object(config, "WF_TRADING_ENABLED", True):
            self.assertTrue(main.observation_only(wf, True))          # both switches needed


class CycleAlignmentTest(unittest.TestCase):
    def test_workflow_aligns_cycles_to_minute_closes(self):
        import main
        with patch.object(config, "ZONE_WATCH_ENABLED", False), \
                patch.object(config, "WF_ENABLED", True):
            self.assertAlmostEqual(main._seconds_until_next_cycle(0, now=125.0),
                                   180 + config.ZONE_WATCH_CLOSE_DELAY_SECONDS - 125.0)


class S6OnlyTest(unittest.TestCase):
    """Deployment with S4-OFR stopped: ZONE_WATCH_ENABLED=false and S4_OFR_ENABLED=false."""

    def test_s4_off_yields_no_s4_candidate(self):
        from setups import orderflow_reversal
        ctx = SimpleNamespace(symbol="BTCUSDT")
        with patch.object(config, "S4_OFR_ENABLED", False):
            self.assertTrue(orderflow_reversal.detect(ctx).is_rejection)

    def test_workflow_candidate_passes_when_s4_silent(self):
        import main
        wf = SimpleNamespace(setup="S6-WF")
        bot = SimpleNamespace(workflow=SimpleNamespace(observe=lambda ctx: (wf, [])))
        candidate, rejections = main.VolumeProfileBot._workflow_step(
            bot, SimpleNamespace(symbol="BTCUSDT"), None, ["s4 refusal"])
        self.assertIs(candidate, wf)
        self.assertEqual(rejections, ["s4 refusal"])


if __name__ == "__main__":
    unittest.main()
