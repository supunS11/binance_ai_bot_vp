"""Counts only (no outcomes): how the 2026-10-18 fixes change the DEV entry-M trades.

  fix 1  T1 / X at the prior VAH / VAL: cancelled when value was reclaimed (two 30m closes back
         inside, wf_levels.reclaim_time) before the touch.
  fix 2  level trades at the prior VAH / VAL: stop beyond the level as well (wf_trade.level_stop);
         the trade is re-planned with the stored T2 and skipped if T2 < 2R or fees > 1/3 R.
Leg levels (LVN / POC) are not in the stored rows' level prices, so they are reported apart.

  python -m research.wf_fix_count calibration/workflow_v2/ledger_dev_m "calibration/trend_combo/values_s*.csv"
"""
import collections
import csv
import datetime as dt
import glob
import sys

import sessions
from data import store
from research import wf_levels as wl
from research import wf_trade as wt
from setups import workflow as wf


def main(root, values_glob):
    rows = []
    for f in sorted(glob.glob(f"{root}/s*.csv")):
        rows += list(csv.DictReader(open(f, encoding="utf-8")))
    vals = {}
    for f in glob.glob(values_glob):
        for r in csv.DictReader(open(f, encoding="utf-8")):
            vals[(r["symbol"], r["day"])] = r
    cache = store.KlineCache(None, parquet_dir="data_cache_mainnet")
    trades = [r for r in rows if r["control"] == "0" and r["allowed"] == "1" and r["skip"] == ""
              and r["path"] != "R" and r["filled"] == "1" and r["recipe"] == "LEDGER" and r["entry"] == "M"]
    out = collections.Counter()
    examples = []
    for r in trades:
        sym, sid, path, side, kind = r["symbol"], r["session_id"], r["path"], r["side"], r["kind"]
        prev = (dt.date.fromisoformat(sid) - dt.timedelta(days=1)).isoformat()
        pv = vals.get((sym, prev))
        if kind not in ("VAH", "VAL") or pv is None:
            out[(path, "leg level / no prior value: unchanged")] += 1
            continue
        vah, val, atr = float(pv["vah"]), float(pv["val"]), float(pv["atr"])
        candles = cache.load_session(sym, sid)
        if not candles:
            out[(path, "no candles")] += 1
            continue
        start = sessions.session_start_ms_from_id(sid)
        bars30 = wl.bars30(candles)
        touch_ms = start + int(r["touch_min"]) * 60_000
        if path in ("T1", "X"):
            acc = wl.acceptance_times(bars30, vah, val).get("ABOVE" if side == "BUY" else "BELOW")
            rec = wl.reclaim_time(bars30, vah, val, acc)
            if rec is not None and rec <= touch_ms:
                out[(path, "fix1: cancelled (value reclaimed before the touch)")] += 1
                examples.append((sym, sid, path, side))
                continue
        level = vah if kind == "VAH" else val
        em = start + int(r["entry_min"]) * 60_000
        entry = next(c.open for c in candles if c.open_time == em)
        dec = start + int(r["decision_min"]) * 60_000
        upto = [c for c in candles if c.close_time < dec + 60_000]
        span = [c for c in upto if c.open_time >= touch_ms]
        extreme = min(c.low for c in span) if side == "BUY" else max(c.high for c in span)
        a15 = wf.atr15(upto, atr)
        old_stop = wt.stop_price(side, extreme, a15)
        new_stop = wt.level_stop(side, extreme, level, a15)
        if abs(new_stop - old_stop) < 1e-12:
            out[(path, "fix2: stop already beyond the level")] += 1
            continue
        old_risk = abs(entry - old_stop)
        t2 = entry + (1 if side == "BUY" else -1) * float(r["t2_r"]) * old_risk
        skip = wt.plan(side, entry, new_stop, None, t2)
        out[(path, f"fix2: stop moved beyond the level -> {skip or 'still a trade'}")] += 1
    total = len(trades)
    print(f"DEV level trades (T1/T2/T3/X/F, entry M): {total}")
    for (path, what), n in sorted(out.items()):
        print(f"  {path:3s} {what}: {n}")
    gone = sum(n for (p, w), n in out.items() if "cancelled" in w or "t2_under" in w or "too_tight" in w)
    print(f"removed by fix 1 + 2: {gone} of {total} ({gone / total:.0%}); "
          f"R trades (139) unchanged")
    print("fix-1 examples:", examples[:8])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
