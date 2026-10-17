"""Workflow v2 DEV firing-rate report (counts only - the dev-mode output has no outcomes).

Answers, per path: how many watches, order-flow results, entries and tradeable trades the
workflow produced on the DEV data, and how many trades the holdout would give if the rate is
similar (scaled by symbol-days). Compared with the pre-registered 300-trade minimum.

  python -m research.wf_counts "calibration/workflow_v2/dev_counts/s*.csv" \
      --holdout-symbol-days N > calibration/workflow_v2/dev_counts/report.txt
"""
import argparse
import collections
import csv
import glob

PRIMARY = {"T1": "DEF_INI", "T2": "DEF_INI", "T3": "DEF_INI", "X": "DEF_INI", "F": "FA_DEF_INI",
           "R": "NONE"}
SECONDARY = {"T1": "EXH_INI", "T2": "EXH_INI", "T3": "EXH_INI", "X": "EXH_INI", "F": "DEF_INI"}
GROUP = {"T1": "T", "T2": "T", "T3": "T", "X": "T", "F": "F", "R": "R"}
MIN_TRADES = 300


def load(pattern):
    rows = []
    for f in sorted(glob.glob(pattern)):
        with open(f, encoding="utf-8") as h:
            rows += list(csv.DictReader(h))
    return rows


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("pattern")
    p.add_argument("--holdout-symbol-days", type=float, default=None)
    p.add_argument("--dev-symbol-days", type=float, required=True,
                   help="symbol-days the DEV run covered (validated footprint days)")
    a = p.parse_args(argv)
    rows = load(a.pattern)
    sessions = {(r["symbol"], r["session_id"]): r["state"] for r in rows}
    sd = len(sessions)
    print(f"DEV symbol-sessions with at least one watch row: {sd}  "
          f"(symbols {len({s for s, _ in sessions})})")
    print("state share of those sessions:", dict(collections.Counter(sessions.values())))
    print(f"DEV symbol-days covered: {a.dev_symbol_days:.0f}")
    scale = (a.holdout_symbol_days / a.dev_symbol_days) if a.holdout_symbol_days else None
    real = [r for r in rows if r["allowed"] == "1" and r["control"] == "0"]

    print("\n== Watches (allowed, real levels) by path: touches, window skips ==")
    w = collections.Counter()
    for r in real:
        if r["recipe"] in ("C0", "NONE") or r["skip"] == "window":
            w[(r["path"], "window" if r["skip"] == "window" else "touch")] += 1
    for path in ("T1", "T2", "T3", "X", "F", "R"):
        print(f"  {path:3s} touches {w[(path, 'touch')]:6d}   outside window {w[(path, 'window')]:5d}")

    print("\n== Order-flow results per path and recipe (allowed, real) ==")
    of = collections.Counter((r["path"], r["recipe"], r["of_outcome"]) for r in real
                             if r["recipe"] not in ("", "C0", "NONE") and r["entry"] in ("", "A"))
    for path in ("T1", "T2", "T3", "X", "F"):
        for rec in (PRIMARY[path], SECONDARY[path]):
            tot = sum(v for (pp, rr, _), v in of.items() if pp == path and rr == rec)
            if not tot:
                continue
            parts = ", ".join(f"{o} {of[(path, rec, o)]} ({of[(path, rec, o)] / tot:.1%})"
                              for o in ("CONFIRM", "CANCEL", "NO_SIGNAL", "NO_DATA") if of[(path, rec, o)])
            print(f"  {path:3s} {rec:11s} n={tot:6d}: {parts}")

    print("\n== Entries and skips (allowed, real; per entry type) ==")
    ent = collections.Counter()
    for r in real:
        if r["entry"]:
            ent[(r["path"], r["recipe"], r["entry"], r["skip"] or "TRADE")] += 1
    for (path, rec, e, s), v in sorted(ent.items()):
        print(f"  {path:3s} {rec:11s} {e:2s} {s:16s} {v:6d}")

    print("\n== Tradeable trades, primary recipe, by group (the pre-registered H-T / H-F / H-R) ==")
    for grp in ("T", "F", "R"):
        for e in (("A", "B") if grp != "R" else ("R",)):
            n = sum(1 for r in real if GROUP.get(r["path"]) == grp and r["recipe"] == PRIMARY[r["path"]]
                    and r["entry"] == e and r["skip"] == "")
            proj = f" -> holdout ~{n * scale:.0f}" if scale else ""
            flag = ""
            if scale:
                flag = "  OK" if n * scale >= MIN_TRADES else f"  BELOW {MIN_TRADES} (no-conclusion risk)"
            print(f"  {grp} entry {e}: DEV {n}{proj}{flag}")
    print("\n== Controls produced (counts) ==")
    ctl = collections.Counter((r["allowed"], r["control"]) for r in rows if r["skip"] == "" and r["entry"])
    names = {("1", "0"): "workflow (real)", ("1", "1"): "L2 random levels", ("0", "0"): "L1 wrong state",
             ("0", "1"): "-", ("1", "2"): "R matched control", ("0", "2"): "R control, wrong state"}
    for k, v in sorted(ctl.items()):
        print(f"  {names.get(k, k)}: {v} tradeable rows (all recipes/entries)")
    c0 = sum(1 for r in real if r["recipe"] == "C0" and r["skip"] == "")
    print(f"  L3 no-order-flow (C0) tradeable rows: {c0}")


if __name__ == "__main__":
    main()
