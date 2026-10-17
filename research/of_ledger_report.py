"""Stage report for the accumulated order-flow read (research/of_ledger) on DEV watches.
Counts and behaviour only - no trade outcome exists in the input.

  python -m research.of_ledger_report "calibration/workflow_v2/ledger_dev/s*.csv" \
      [--bookdepth <dir of SYMBOL_YYYY-MM-DD.csv>]
"""
import argparse
import collections
import csv
import datetime as dt
import glob
import math
import os

import numpy as np

STAGES = ("attempt_dt", "absorption_dt", "exhaustion_dt", "failure_dt", "response_dt")


def load(pattern):
    rows = []
    for f in sorted(glob.glob(pattern)):
        with open(f, encoding="utf-8") as h:
            rows += [r for r in csv.DictReader(h) if r["recipe"] == "LEDGER"]
    return rows


def share(n, d):
    return f"{n:6d} ({n / d:5.1%})" if d else f"{n:6d}"


def wilson(k, n):
    if n == 0:
        return (math.nan, math.nan)
    p, z = k / n, 1.96
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return c - h, c + h


def funnel(rows, title):
    n = len(rows)
    nd = sum(r["of_outcome"] == "NO_DATA" for r in rows)
    ok = [r for r in rows if r["of_outcome"] != "NO_DATA"]
    m = len(ok)
    has = lambda k: sum(1 for r in ok if r.get(k) not in ("", None))   # noqa: E731
    out = collections.Counter((r["of_outcome"], r["of_path"]) for r in ok)
    print(f"  {title}: interactions {n}, no data {nd}, read {m}")
    print(f"    attempt (unusual selling into the level) {share(has('attempt_dt'), m)}")
    print(f"    absorption read                          {share(has('absorption_dt'), m)}")
    print(f"    exhaustion read                          {share(has('exhaustion_dt'), m)}")
    print(f"    any failure reading                      {share(has('failure_dt'), m)}")
    print(f"    CONFIRM (response after a failure)       {share(sum(v for (o, _), v in out.items() if o == 'CONFIRM'), m)}"
          f"   ABS {out[('CONFIRM', 'ABS')]} / EXH {out[('CONFIRM', 'EXH')]}")
    print(f"    VETO acceptance / break                  {out[('VETO', 'ACCEPT')]} / {out[('VETO', 'BREAK')]}")
    print(f"    NO_SIGNAL                                {share(out[('NO_SIGNAL', '')], m)}")
    return ok


def rate(rows, key):
    ok = [r for r in rows if r["of_outcome"] != "NO_DATA"]
    k = sum(1 for r in ok if r.get(key) not in ("", None))
    return k, len(ok)


def depth_series(path):
    """{percentage: (times[], notional[])} from a Binance bookDepth CSV."""
    out = collections.defaultdict(lambda: ([], []))
    with open(path, encoding="utf-8") as h:
        for r in csv.DictReader(h):
            t = dt.datetime.fromisoformat(r["timestamp"]).replace(tzinfo=dt.timezone.utc).timestamp()
            out[r["percentage"]][0].append(t)
            out[r["percentage"]][1].append(float(r["notional"]))
    return {k: (np.array(a), np.array(b)) for k, (a, b) in out.items()}


def depth_check(rows, root):
    """On rows with an attempt and an archived bookDepth file: did resting size on the
    defending side within 0.2% persist from just before the touch through the attack?"""
    res = collections.Counter()
    cache = {}
    for r in rows:
        if r.get("attempt_dt") in ("", None) or r["control"] != "0":
            continue
        f = os.path.join(root, f"{r['symbol']}_{r['session_id']}.csv")
        if not os.path.exists(f):
            continue
        if f not in cache:
            cache[f] = depth_series(f)
        ser = cache[f].get("-0.20" if r["side"] == "BUY" else "0.20")
        if ser is None:
            continue
        times, notional = ser
        day0 = dt.datetime.fromisoformat(r["session_id"]).replace(tzinfo=dt.timezone.utc).timestamp()
        t_touch = day0 + int(r["touch_min"]) * 60
        end_dt = r.get("failure_dt") or r.get("attempt_dt")
        t_end = t_touch + (int(end_dt) + 1) * 60
        before = notional[times < t_touch]
        during = notional[(times >= t_touch) & (times <= t_end)]
        if before.size == 0 or during.size == 0:
            continue
        kept = float(np.median(during)) >= float(before[-1])
        kind = "absorption" if r.get("absorption_dt") not in ("", None) else "no absorption"
        res[(kind, kept)] += 1
    if not res:
        print("  no bookDepth overlap")
        return
    for kind in ("absorption", "no absorption"):
        k, n = res[(kind, True)], res[(kind, True)] + res[(kind, False)]
        lo, hi = wilson(k, n)
        print(f"  {kind:14s}: resting size within 0.2% persisted in {k}/{n} = "
              f"{(k / n if n else float('nan')):.1%} [{lo:.1%}, {hi:.1%}]")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("pattern")
    p.add_argument("--bookdepth", default=None)
    a = p.parse_args(argv)
    rows = load(a.pattern)
    real = [r for r in rows if r["control"] == "0" and r["allowed"] == "1"]
    ctrl = [r for r in rows if r["control"] == "1" and r["allowed"] == "1"]
    print("== 1. Stage funnel: real planned levels vs random-offset control levels ==")
    funnel(real, "REAL levels")
    funnel(ctrl, "CONTROL levels")

    print("\n== 2. Per path (real levels) ==")
    for path in ("T1", "T2", "T3", "X", "F", "FC"):
        funnel([r for r in real if r["path"] == path], path)
    funnel([r for r in real if r["path"] == "X" and r["kind"] in ("LVN", "POC")],
           "X (breakout-leg LVN/POC)")

    print("\n== 3. Do real levels attract more absorption than random levels? (rate, 95% Wilson) ==")
    for key, name in (("attempt_dt", "attempt"), ("absorption_dt", "absorption"),
                      ("exhaustion_dt", "exhaustion"), ("response_dt", "confirm")):
        k1, n1 = rate(real, key)
        k2, n2 = rate(ctrl, key)
        l1, h1 = wilson(k1, n1)
        l2, h2 = wilson(k2, n2)
        print(f"  {name:11s} real {k1 / n1:6.1%} [{l1:.1%},{h1:.1%}] n={n1}   "
              f"control {k2 / n2:6.1%} [{l2:.1%},{h2:.1%}] n={n2}")

    print("\n== 3b. Composite confluence: do levels on a composite level hold more liquidity? ==")
    print("  (control rows carry their parent level's tag; FC levels are excluded)")
    for tag, name in (("1", "CONFLUENT"), ("0", "not confluent")):
        rr = [r for r in real if r.get("conf") == tag]
        cc = [r for r in ctrl if r.get("conf") == tag]
        for key, label in (("absorption_dt", "absorption"), ("exhaustion_dt", "exhaustion"),
                           ("response_dt", "confirm")):
            k1, n1 = rate(rr, key)
            k2, n2 = rate(cc, key)
            if n1 and n2:
                l1, h1 = wilson(k1, n1)
                l2, h2 = wilson(k2, n2)
                print(f"  {name:14s} {label:10s} real {k1 / n1:6.1%} [{l1:.1%},{h1:.1%}] n={n1}   "
                      f"control {k2 / n2:6.1%} [{l2:.1%},{h2:.1%}] n={n2}")
        ok1 = [r for r in rr if r["of_outcome"] != "NO_DATA"]
        ok2 = [r for r in cc if r["of_outcome"] != "NO_DATA"]
        if ok1 and ok2:
            for veto in ("BREAK", "ACCEPT"):
                v1 = sum(r["of_outcome"] == "VETO" and r["of_path"] == veto for r in ok1) / len(ok1)
                v2 = sum(r["of_outcome"] == "VETO" and r["of_path"] == veto for r in ok2) / len(ok2)
                print(f"  {name:14s} veto {veto:6s} real {v1:6.1%}   control {v2:6.1%}")

    print("\n== 3c. Middle-timeframe alignment on trend days (real levels) ==")
    trend = [r for r in real if r.get("align")]
    for al in ("ALIGNED", "RANGE", "CONFLICT"):
        funnel([r for r in trend if r["align"] == al], al)

    print("\n== 4. What the confirmations rest on (real levels) ==")
    conf = [r for r in real if r["of_outcome"] == "CONFIRM"]
    if conf:
        st = sum(int(r["stack"] or 0) for r in conf)
        print(f"  confirmations {len(conf)}: via absorption {sum(r['of_path'] == 'ABS' for r in conf)}, "
              f"via exhaustion {sum(r['of_path'] == 'EXH' for r in conf)}")
        print(f"  stacked buying imbalances in the response footprint: {share(st, len(conf))}")
        sh = [float(r["low_rows_share"]) for r in conf if r["low_rows_share"] not in ("", None)]
        if sh:
            print(f"  share of the attack's selling in the lowest 3 rows: median {np.median(sh):.1%}, "
                  f"p25 {np.percentile(sh, 25):.1%}, p75 {np.percentile(sh, 75):.1%}")
        dts = [int(r["response_dt"]) for r in conf]
        print(f"  minutes from touch to confirmation: median {np.median(dts):.0f}, "
              f"p25 {np.percentile(dts, 25):.0f}, p75 {np.percentile(dts, 75):.0f}")
        gap = [int(r["response_dt"]) - int(r["failure_dt"]) for r in conf]
        print(f"  minutes from failure reading to response: median {np.median(gap):.0f}")
        dp = collections.Counter(r.get("dpoc") or "?" for r in conf)
        print(f"  developing POC vs prior value at the touch: {dict(dp)}")
    if a.bookdepth:
        print("\n== 5. Order-book check (archived bookDepth, sample of symbol-days) ==")
        depth_check(rows, a.bookdepth)


if __name__ == "__main__":
    main()
