"""Live-vs-research equivalence check for S6-WF (setups/workflow.py) - research only.

Drives the LIVE engine minute by minute over historical sessions and compares every decision
with the research runner (research/wf_run.py, ledger mode) on the same data:
  - the recorder's trade files are written from the validated research footprints (one line
    per minute, prices at the row centres, so the live row arithmetic lands on the same rows);
  - daily candles from data_cache_daily_long, 1m / 15m candles from the mainnet cache, through
    the same provider interface as setups.workflow.LiveData;
  - each minute the live engine sees a context holding only the session's candles closed by
    then (the frozen prior-day bundle comes from the same Scanner the research runner uses).
Reported per (symbol, session, path, side, level): research outcome / decision minute vs live,
and for confirmations the stop and target. Any mismatch is a bug in the live plumbing.

    PARQUET_DIR=data_cache_mainnet python -m research.wf_live_replay \
        --pairs SYMBOL:YYYY-MM-DD,... --research <wf_run ledger csv> --out <csv>
"""
import argparse
import csv
import gzip
import json
import logging
import os
import shutil
import tempfile
import types

import numpy as np

import config
import sessions
from data import klines as klines_mod
from research import confirm_study as cs
from research import dataset
from research.footprint import load_day
from research.historical import HistoricalCache, StaticCatalog
from scanner import Scanner
from setups import workflow as wfm

log = logging.getLogger(__name__)
FP_ROOTS = ("data_footprint_crypto", "data_footprint_fresh")
DAYS_BACK = 11                   # the 10-day impulse-leg history + 1


class ReplayData:
    """setups.workflow.LiveData's interface over local research data."""

    def __init__(self, cache, daily_cache, recorder_root):
        self._cache, self._daily = cache, daily_cache
        self.recorder = wfm.fpl.RecorderTrades(recorder_root)
        self._rows = {}

    def daily(self, symbol, session_start):
        return [c for c in self._daily.load_daily(symbol) if c.close_time < session_start]

    def minute_day(self, symbol, sid):
        start = sessions.session_start_ms_from_id(sid)
        return [c for c in self._cache.forward_candles(symbol, start, start + sessions.MS_DAY)
                if c.open_time < start + sessions.MS_DAY]

    def hist15(self, symbol, start_ms, end_ms):
        return klines_mod.resample(self._cache.minute_candles(symbol, start_ms, end_ms), "15m", "1m")

    def values(self, symbol, sid, daily, tick):
        start = sessions.session_start_ms_from_id(sid)
        out = []
        for k in range(wfm.VALUE_DAYS, 0, -1):
            day = sessions.session_id(start - k * sessions.MS_DAY)
            key = (symbol, day)
            if key not in self._rows:
                self._rows[key] = wfm.ws.day_value_row(symbol, day, self.minute_day(symbol, day),
                                                       daily, tick)
            if self._rows[key] is not None:
                out.append(self._rows[key])
        return out


def write_recorder(root, fp_root, symbol, sid):
    """The research footprint of one day as recorder trade lines; False when it is missing."""
    z = load_day(fp_root, symbol, sid)
    if z is None:
        return False
    rs = float(np.ravel(z["row_size"])[0])
    start = sessions.session_start_ms_from_id(sid)
    by_min = {}
    for m, r, b, s in zip(z["minute"], z["row"], z["buy"], z["sell"]):
        by_min.setdefault(int(m), {})[repr((int(r) + 0.5) * rs)] = [float(b), float(s)]
    path = os.path.join(root, "trades", symbol, f"{sid}.jsonl.gz")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for m in sorted(by_min):
            handle.write(json.dumps({"m": start + m * 60_000, "lv": by_min[m]}) + "\n")
    return True


class _Ctx(types.SimpleNamespace):
    def profile_row(self):
        return {}


def replay_session(symbol, sid, catalog, cache, daily_cache, root):
    fp_root = next((d for d in FP_ROOTS if os.path.isdir(os.path.join(d, symbol))), None)
    start = sessions.session_start_ms_from_id(sid)
    for k in range(DAYS_BACK, -1, -1):
        write_recorder(root, fp_root, symbol, sessions.session_id(start - k * sessions.MS_DAY))
    cache.set_as_of(start)
    cache.clear_frozen()
    scanner = Scanner(rest=None, catalog=catalog, cache=cache, journal=None)
    bundle, _ = scanner.frozen_bundle(symbol, sid, start)
    if bundle is None:
        return [], [], None
    candles = [c for c in cache.forward_candles(symbol, start, start + sessions.MS_DAY)
               if c.open_time < start + sessions.MS_DAY]
    engine = wfm.Workflow(ReplayData(cache, daily_cache, root))
    found = []
    for i in range(1, len(candles)):
        ctx = _Ctx(symbol=symbol, spec=catalog.get(symbol), session_id=sid,
                   as_of=candles[i].close_time, prior_levels=bundle.levels,
                   prior_profile=bundle.profile, atr=bundle.atr,
                   session_candles=tuple(candles[:i + 1]))
        cand, _ = engine.observe(ctx)
        if cand is not None:
            found.append(cand)
    done = engine._done.get(symbol, (sid, {}))[1]
    day = engine._days.get(symbol)
    touch = {}
    if day is not None and day.trades_today:
        for w in engine._watches(day, candles):
            touch[wfm.watch_key(w)] = (candles[w["i"]].open_time - start) // 60_000
    return [(k, v, touch.get(k, "")) for k, v in done.items()], found, day


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--pairs", required=True)
    p.add_argument("--research", required=True)
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    for name in cs.SHAPE_FLAGS:
        setattr(config, name, False)
    catalog = StaticCatalog(dataset.load_specs(config.PARQUET_DIR))
    daily_cache = HistoricalCache(root="data_cache_daily_long", strict=False)
    research = {}
    with open(a.research, encoding="utf-8") as h:
        for r in csv.DictReader(h):
            if r["allowed"] == "1" and r["control"] == "0" and r["recipe"] in ("LEDGER", "NONE"):
                research.setdefault((r["symbol"], r["session_id"]), []).append(r)
    root = tempfile.mkdtemp(prefix="wf_replay_")
    out = []
    try:
        for pair in a.pairs.split(","):
            symbol, sid = pair.split(":")
            cache = HistoricalCache(root=config.PARQUET_DIR, strict=False)
            done, cands, day = replay_session(symbol, sid, catalog, cache, daily_cache, root)
            live = {}
            for k, v, t in done:
                p, kind, side = (k.split("|") + ["", "", ""])[:3]
                live[("R", "R") if k == "R" else (p, side, kind, str(t))] = (k, v)
            by_key = {}
            for c in cands:
                a_ = c.attributes
                by_key[("R", "R") if a_["wf_path"] == "R" else
                       (a_["wf_path"], c.direction, c.level_kind, str(a_.get("wf_touch_min")))] = c
            matched = set()
            for r in research.get((symbol, sid), []):
                if r["path"] == "FC" and r["state"] != "BALANCE" and not config.WF_MTF_ALIGNMENT:
                    continue                    # trend-day FC trades only with the 5A switch
                if r["recipe"] == "NONE":
                    if r["kind"] != "R":
                        continue
                    key, res_out = ("R", "R"), "SIGNAL"
                else:
                    key = (r["path"], r["side"], r["kind"], r["touch_min"])
                    res_out = (r["of_outcome"] + ("_" + r["of_path"] if r["of_path"] else "")) \
                        if r["skip"] != "window" else "WINDOW"
                matched.add(key)
                c = by_key.get(key)
                out.append({"symbol": symbol, "session_id": sid, "state": r["state"],
                            "live_state": day.state if day else "",
                            "mid": r.get("mid", ""), "live_mid": (day.mid or "") if day else "",
                            "path": r["path"],
                            "side": r["side"], "kind": r["kind"], "touch_min": r["touch_min"],
                            "research": res_out,
                            "live": live[key][1] if key in live else "NOT_SEEN",
                            "research_decision": r.get("decision_min", ""),
                            "live_decision": c.attributes.get("wf_decision_min", "") if c else "",
                            "research_skip": r.get("skip", ""),
                            "live_candidate": int(c is not None),
                            "research_risk_atr": r.get("risk_atr", ""),
                            "live_risk_atr": round(abs(c.entry_price - c.stop_price) / c.atr, 4) if c else "",
                            "research_t2_r": r.get("t2_r", ""),
                            "live_t2_r": c.attributes.get("wf_t2_r", "") if c else ""})
            for key, (k, v) in live.items():
                if key not in matched:
                    out.append({"symbol": symbol, "session_id": sid, "path": key[0], "kind": k,
                                "touch_min": key[-1], "research": "NOT_IN_RESEARCH", "live": v,
                                "live_state": day.state if day else ""})
            cache.release()
            print(f"{symbol} {sid}: live state {day.state if day else None}, {len(live)} live "
                  f"decisions, {len(cands)} candidates", flush=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    fields = sorted({k for o in out for k in o}, key=lambda k: list(out[0]).index(k) if k in out[0] else 99)
    with open(a.out, "w", encoding="utf-8", newline="") as h:
        w = csv.DictWriter(h, fieldnames=fields)
        w.writeheader()
        w.writerows(out)
    same = sum(1 for o in out if o.get("research") == o.get("live")
               or (o.get("research", "").startswith("CONFIRM") and o.get("live", "").startswith("CONFIRM")))
    print(f"rows {len(out)}, same outcome {same}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
