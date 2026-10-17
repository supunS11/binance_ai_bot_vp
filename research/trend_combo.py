"""Combined 1D trend detection: significant-swing structure + multi-day value migration -
research; composite_value_label / zigzag_pivots are shared with the live S6-WF setup (setups/workflow.py), so live and research run the same rules.

WRITTEN 2026-10-13 BEFORE ANY RESULT OF THIS SCRIPT WAS SEEN. Parameters are chosen on the
DEVELOPMENT period (days < 2026-06-15) only and then reported unchanged on the VALIDATION
period (days >= 2026-06-15).

Every label for day D uses only data closed before D (no look-ahead).

STRUCTURE (Dale: "uptrend, downtrend or rotation" on the Daily chart; the conventional
  Dow definition made mechanical). Swings are SIGNIFICANT swings: a causal zigzag on daily
  bars that confirms a swing high only after price has fallen k x daily ATR from it (and
  vice versa), so highs and lows always alternate and minor wiggles are ignored. With the
  last two confirmed highs H1 < H2-in-time and lows L1, L2, and the latest close c:
    UP        (L2 > L1 and c > H2)                      break above the last high, HL held
              or (H2 > H1 and L2 > L1 and c > L2)       HH + HL, structure intact
    DOWN      the mirror
    ROTATION  anything else (incl. an UP whose last swing low has been broken by the close)
  The break conditions are checked first, so a break of structure relabels at once.

VALUE (AK / Dalton: imbalance = "value areas migrate day after day, consistently higher or
  lower"; balance = "the value areas overlap"). Over the last N completed days: each day's
  migration vs the day before (profile.relations.classify_migration, the live function):
  HIGHER / OVERLAPPING_HIGHER = +1, LOWER / OVERLAPPING_LOWER = -1, INSIDE / OUTSIDE /
  UNCHANGED = 0. VALUE_UP when the sum >= M and the POC has moved >= +P daily ATR over the
  N days; VALUE_DOWN the mirror; otherwise BALANCE.

HINDSIGHT CHART TRUTH (independent of both detectors; what a trader sees on the finished
  chart around day D). Window D-W .. D+W daily closes: net = (close[D+W] - close[D-W]) /
  ATR, efficiency = |net move| / sum of |daily close changes|.
    UP / DOWN  |net| >= 3 ATR and efficiency >= 0.30      (a clear, fairly direct move)
    ROTATION   |net| <= 1.5 ATR                           (went nowhere)
    otherwise  UNCLEAR - excluded from scoring
  W = 10 (a month of daily chart) decides; W = 15 reported for robustness.

  REVISION 2026-10-13 - THE TRUTH ABOVE WAS DROPPED after its first run: on charts it
  labelled plainly trending periods (BTC 65k -> 82k, Mar-May) as rotation, because crypto
  pullbacks are large against ATR. Replaced, after a TRUTH-ONLY chart check of BTC, ETH,
  DOGE, XRP and XAU (before any detector was scored against it), by the HINDSIGHT DOW
  truth (truth_structure_labels): major swings = zigzag at 2.5 daily ATR with pivots at
  their true extremes; breaking legs are trend legs, pullbacks belong to their trend,
  the rest is rotation. 3.0 ATR is reported for robustness. CAVEAT: this truth is itself
  structure-based (Dale reads trend from chart structure), so it favours the structure
  detector by construction; the value detector is judged by what it ADDS.
  VALUE also gained a composite variant (composite_value_label), added at the same time
  because the first value rule labelled almost every day as balance on the charts.

SCORING: balanced accuracy (mean of UP, DOWN and ROTATION recall - so "always ROTATION"
  cannot look good), the rate of OPPOSITE errors (UP when the chart shows DOWN or vice
  versa - the costly mistake), and label changes per 30 days.

DISAGREEMENT RULES compared (none assumed in advance):
  R_STRICT     trend only when both agree; every disagreement -> ROTATION
  R_STRUCT     structure's label; ROTATION only when value OPPOSES it
  R_VALUE      value's label; ROTATION only when structure OPPOSES it
  R_EITHER     trend when either says trend and the other does not oppose; opposing ->
               ROTATION
  R_CELL       each of the 9 (structure x value) cells takes the chart-truth majority of
               that cell in the DEVELOPMENT period, then is scored on VALIDATION

Run:
    python -m research.trend_combo --values "calibration/trend_combo/values_s?.csv" \
        --daily-root data_cache_mainnet --out calibration/trend_combo
"""
import argparse
import bisect
import collections
import csv
import glob
import os
import types

import numpy as np

import sessions
from data import klines as klines_mod
from profile import relations
from research import context_labels as cl
from research.historical import HistoricalCache

SPLIT = "2026-06-15"
UP, DOWN, ROT = "UP", "DOWN", "ROTATION"
K_GRID = (1.5, 2.0, 2.5, 3.0)
N_GRID = (3, 5, 7)
P_GRID = (0.5, 1.0)
TRUTH_NET, TRUTH_FLAT, TRUTH_EFF = 3.0, 1.5, 0.30
TRUTH_K = 2.5
C_N_GRID = (5, 10, 15)
C_D_GRID = (0.5, 1.0, 1.5)


# ------------------------------------------------------------------ structure

def atr_series(daily):
    """atr[i] = daily ATR from bars before i (0 when not enough history)."""
    return [klines_mod.daily_atr(daily[:i]) if i >= 15 else 0.0 for i in range(len(daily))]


def zigzag_pivots(daily, atrs, k):
    """Causal zigzag. Returns [(confirm_index, bar_index, price, 'H'|'L')], in order of
    confirmation; a pivot is known only after its confirm_index bar has closed."""
    pivots = []
    leg = 0
    hi_i = lo_i = 0
    for i, b in enumerate(daily):
        atr = atrs[i]
        if atr <= 0:
            hi_i = i if b.high >= daily[hi_i].high else hi_i
            lo_i = i if b.low <= daily[lo_i].low else lo_i
            continue
        th = k * atr
        if leg >= 0 and b.high >= daily[hi_i].high:
            hi_i = i
        if leg <= 0 and b.low <= daily[lo_i].low:
            lo_i = i
        if leg >= 0 and daily[hi_i].high - b.low >= th and hi_i < i:
            pivots.append((i, hi_i, daily[hi_i].high, "H"))
            leg, lo_i = -1, i
        elif leg <= 0 and b.high - daily[lo_i].low >= th and lo_i < i:
            pivots.append((i, lo_i, daily[lo_i].low, "L"))
            leg, hi_i = 1, i
    return pivots


def structure_label(pivots, upto, close):
    """Label from pivots confirmed on bars < upto and the close of bar upto-1."""
    known = [p for p in pivots if p[0] < upto]
    highs = [p[2] for p in known if p[3] == "H"]
    lows = [p[2] for p in known if p[3] == "L"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    h1, h2, l1, l2 = highs[-2], highs[-1], lows[-2], lows[-1]
    if l2 > l1 and close > h2:
        return UP
    if h2 < h1 and close < l2:
        return DOWN
    if h2 > h1 and l2 > l1 and close > l2:
        return UP
    if h2 < h1 and l2 < l1 and close < h2:
        return DOWN
    return ROT


# ------------------------------------------------------------------ value

def migration_scores(values):
    """values: day-ordered list of dicts (poc/vah/val/atr). score[i] for day i vs i-1."""
    out = [None]
    for prev, cur in zip(values, values[1:]):
        lv = lambda r: types.SimpleNamespace(poc_price=r["poc"], vah=r["vah"], val=r["val"])
        m = relations.classify_migration(lv(cur), lv(prev), cur["atr"])
        out.append(1 if m.label in ("HIGHER", "OVERLAPPING_HIGHER") else
                   -1 if m.label in ("LOWER", "OVERLAPPING_LOWER") else 0)
    return out


def value_label(values, scores, upto, n, m, p):
    """Label from the n completed days before index upto (needs n+1 days of values)."""
    if upto - n - 1 < 0:
        return None
    window = scores[upto - n:upto]
    if any(s is None for s in window):
        return None
    total = sum(window)
    atr = values[upto - 1]["atr"]
    if atr <= 0:
        return None
    net = (values[upto - 1]["poc"] - values[upto - 1 - n]["poc"]) / atr
    if total >= m and net >= p:
        return UP
    if total <= -m and net <= -p:
        return DOWN
    return ROT


def composite_value_label(values, upto, n, d):
    """Composite value migration (Dalton's multi-day value): mean daily POC of the last n
    completed days vs the n days before, in daily ATR. >= +d -> UP, <= -d -> DOWN, else
    ROTATION (value overlapping / balanced). Needs 2n days of values before upto."""
    if upto - 2 * n < 0:
        return None
    atr = values[upto - 1]["atr"]
    if atr <= 0:
        return None
    recent = np.mean([r["poc"] for r in values[upto - n:upto]])
    before = np.mean([r["poc"] for r in values[upto - 2 * n:upto - n]])
    shift = (recent - before) / atr
    return UP if shift >= d else DOWN if shift <= -d else ROT


# ------------------------------------------------------------------ truth

def truth_label(daily, atrs, i, w):
    if i - w < 0 or i + w >= len(daily) or atrs[i] <= 0:
        return None
    c = [b.close for b in daily[i - w:i + w + 1]]
    net = (c[-1] - c[0]) / atrs[i]
    path = sum(abs(a - b) for a, b in zip(c[1:], c[:-1]))
    eff = abs(c[-1] - c[0]) / path if path > 0 else 0.0
    if abs(net) >= TRUTH_NET and eff >= TRUTH_EFF:
        return UP if net > 0 else DOWN
    if abs(net) <= TRUTH_FLAT:
        return ROT
    return "UNCLEAR"


def hindsight_pivots(daily, atrs, k):
    """NON-causal major swings (truth only): the same zigzag, but each pivot is placed at
    its true extreme and known from the start - what a trader marks on a finished chart."""
    return [(bi, price, kind) for _ci, bi, price, kind in zigzag_pivots(daily, atrs, k)]


def truth_structure_labels(daily, atrs, k):
    """Per bar: Dow regime of the major leg the bar lies in, judged with hindsight.
    Leg from pivot S = piv[i] to E = piv[i+1]:
      breaking leg   up-leg whose high E exceeds the previous high -> UP;
                     down-leg whose low E undercuts the previous low -> DOWN
                     (so the first leg of a new trend is already that trend)
      pullback leg   down-leg holding a higher low that started from a higher high -> UP;
                     up-leg making a lower high that started from a lower low -> DOWN
      otherwise      ROTATION
    REVISED 2026-10-13 after a TRUTH-ONLY chart check (no detector score had been computed
    against it): the first version (both ends higher -> UP) labelled the first leg of
    every new trend - e.g. BTC 82k -> 59k in May/June - as rotation. Bars before the 3rd
    pivot or after the last one get None."""
    piv = hindsight_pivots(daily, atrs, k)
    out = [None] * len(daily)
    for i in range(2, len(piv) - 1):
        s, e, s_prev, e_prev = piv[i][1], piv[i + 1][1], piv[i - 2][1], piv[i - 1][1]
        up_leg = piv[i + 1][2] == "H"
        if up_leg and e > e_prev:
            lab = UP
        elif not up_leg and e < e_prev:
            lab = DOWN
        elif not up_leg and s > s_prev:
            lab = UP                     # pullback holding a higher low after a higher high
        elif up_leg and s < s_prev:
            lab = DOWN                   # bounce making a lower high after a lower low
        else:
            lab = ROT
        for j in range(piv[i][0], piv[i + 1][0]):
            out[j] = lab
    return out


# ------------------------------------------------------------------ combination

def combine(rule, s, v, cell_map=None):
    if s is None or v is None:
        return None
    opp = {(UP, DOWN), (DOWN, UP)}
    if rule == "R_STRICT":
        return s if s == v else ROT
    if rule == "R_STRUCT":
        return ROT if (s, v) in opp else s
    if rule == "R_VALUE":
        return ROT if (s, v) in opp else v
    if rule == "R_EITHER":
        if (s, v) in opp:
            return ROT
        return s if s != ROT else v
    if rule == "R_CELL":
        return cell_map.get((s, v), ROT)
    raise ValueError(rule)


# ------------------------------------------------------------------ dataset

def load_values(pattern):
    by_sym = collections.defaultdict(list)
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8") as h:
            for r in csv.DictReader(h):
                for k in ("open", "high", "low", "close", "atr", "poc", "vah", "val"):
                    r[k] = float(r[k])
                by_sym[r["symbol"]].append(r)
    for rows in by_sym.values():
        rows.sort(key=lambda r: r["day"])
    return by_sym


def build_rows(values_by_sym, daily_root):
    """One row per symbol-day with every detector variant's label and the truths."""
    cache = HistoricalCache(root=daily_root, strict=False)
    rows = []
    for sym, vals in values_by_sym.items():
        daily = cache.load_daily(sym)
        if len(daily) < 60:
            continue
        atrs = atr_series(daily)
        day_ids = [sessions.session_id(b.open_time) for b in daily]
        piv = {k: zigzag_pivots(daily, atrs, k) for k in K_GRID}
        truth = truth_structure_labels(daily, atrs, TRUTH_K)
        truth3 = truth_structure_labels(daily, atrs, 3.0)
        scores = migration_scores(vals)
        vidx = {r["day"]: j for j, r in enumerate(vals)}

        def contiguous(j, back):
            """values[j-back .. j-1] are exactly the `back` calendar days before day j."""
            if j - back < 0:
                return False
            span = (sessions.session_start_ms_from_id(vals[j]["day"]) -
                    sessions.session_start_ms_from_id(vals[j - back]["day"]))
            return span == back * sessions.MS_DAY

        for i in range(30, len(daily)):
            day = day_ids[i]
            if day not in vidx:
                continue
            j = vidx[day]
            row = {"symbol": sym, "day": day, "truth": truth[i], "truth3": truth3[i],
                   "old": cl.swing_structure_context(daily[max(0, i - 90):i]) or None}
            close = daily[i - 1].close
            for k in K_GRID:
                row[f"s{k}"] = structure_label(piv[k], i, close)
            for n in N_GRID:
                for p in P_GRID:
                    m = int(np.ceil(0.6 * n))
                    row[f"v{n}_{p}"] = (value_label(vals, scores, j, n, m, p)
                                        if contiguous(j, n + 1) else None)
            for n in C_N_GRID:
                for d in C_D_GRID:
                    row[f"c{n}_{d}"] = (composite_value_label(vals, j, n, d)
                                        if contiguous(j, 2 * n) else None)
            rows.append(row)
    return rows


# ------------------------------------------------------------------ scoring

def score(rows, col, truth="truth"):
    ok = [r for r in rows if r.get(col) in (UP, DOWN, ROT) and r[truth] in (UP, DOWN, ROT)]
    if not ok:
        return None
    rec = {}
    for t in (UP, DOWN, ROT):
        part = [r for r in ok if r[truth] == t]
        rec[t] = np.mean([r[col] == t for r in part]) if part else float("nan")
    trend = [r for r in ok if r[truth] in (UP, DOWN)]
    opposite = np.mean([(r[col], r[truth]) in ((UP, DOWN), (DOWN, UP)) for r in trend]) if trend else 0
    labelled_trend = [r for r in ok if r[col] in (UP, DOWN)]
    precision = np.mean([r[col] == r[truth] for r in labelled_trend]) if labelled_trend else float("nan")
    return {"n": len(ok), "bal_acc": float(np.nanmean(list(rec.values()))), "rec": rec,
            "opposite": float(opposite), "trend_precision": float(precision),
            "share": {t: np.mean([r[col] == t for r in ok]) for t in (UP, DOWN, ROT)}}


def flips_per_30(rows, col):
    flips = days = 0
    by = collections.defaultdict(list)
    for r in rows:
        by[r["symbol"]].append(r.get(col))
    for seq in by.values():
        seq = [x for x in seq if x is not None]
        days += len(seq)
        flips += sum(a != b for a, b in zip(seq, seq[1:]))
    return 30 * flips / days if days else float("nan")


def fmt(name, s, fl):
    if s is None:
        return f"  {name:16s} n/a"
    r = s["rec"]
    return (f"  {name:16s} n={s['n']:6d} bal.acc {s['bal_acc']:5.1%} | recall UP {r[UP]:5.1%} "
            f"DOWN {r[DOWN]:5.1%} ROT {r[ROT]:5.1%} | trend precision {s['trend_precision']:5.1%} "
            f"| opposite {s['opposite']:5.1%} | changes/30d {fl:4.1f}")


def lag_days(rows, col, truth="truth"):
    """For every chart-truth UP/DOWN regime: days from its first day until `col` first
    shows the same label inside it. Returns (median lag, share of regimes never caught)."""
    lags, missed = [], 0
    by = collections.defaultdict(list)
    for r in rows:
        by[r["symbol"]].append(r)
    for seq in by.values():
        k = 0
        while k < len(seq):
            t = seq[k][truth]
            end = k
            while end + 1 < len(seq) and seq[end + 1][truth] == t:
                end += 1
            if t in (UP, DOWN):
                hit = next((m - k for m in range(k, end + 1) if seq[m].get(col) == t), None)
                if hit is None:
                    missed += 1
                else:
                    lags.append(hit)
            k = end + 1
    total = len(lags) + missed
    return (float(np.median(lags)) if lags else float("nan"), missed / total if total else float("nan"))


def report(rows):
    out = []
    dev = [r for r in rows if r["day"] < SPLIT]
    val = [r for r in rows if r["day"] >= SPLIT]
    out.append(f"symbol-days: development {len(dev)}, validation {len(val)}")
    out.append(f"chart truth (hindsight Dow, {TRUTH_K} ATR major swings): "
               f"{dict(collections.Counter(r['truth'] for r in rows))}")

    # --- choose parameters on development only
    s_best = max(K_GRID, key=lambda k: score(dev, f"s{k}")["bal_acc"])
    v_cols = [f"v{n}_{p}" for n in N_GRID for p in P_GRID] + \
             [f"c{n}_{d}" for n in C_N_GRID for d in C_D_GRID]
    v_best = max(v_cols, key=lambda c: score(dev, c)["bal_acc"])
    out.append(f"\nPARAMETERS CHOSEN ON DEVELOPMENT: structure k = {s_best} ATR, value = {v_best}")

    def line(part, name, col, truth="truth"):
        lag, miss = lag_days(part, col, truth)
        return fmt(name, score(part, col, truth), flips_per_30(part, col)) + \
            f" | lag {lag:4.1f}d, missed {miss:5.1%}"

    for name, part in (("DEVELOPMENT (< %s)" % SPLIT, dev), ("VALIDATION (>= %s) - decides" % SPLIT, val)):
        out.append(f"\n=== {name}: each method alone vs the chart ===")
        out.append(line(part, "old swing label", "old"))
        for k in K_GRID:
            out.append(line(part, f"structure k={k}", f"s{k}"))
        for c in v_cols:
            out.append(line(part, f"value {c}", c))

    s_col, v_col = f"s{s_best}", v_best
    base = {name: collections.Counter(r["truth"] for r in part if r["truth"] in (UP, DOWN, ROT))
            for name, part in (("dev", dev), ("val", val))}

    def cells(part):
        tab = {}
        for s in (UP, ROT, DOWN):
            for v in (UP, ROT, DOWN):
                c = [r["truth"] for r in part if r[s_col] == s and r[v_col] == v
                     and r["truth"] in (UP, DOWN, ROT)]
                tab[(s, v)] = collections.Counter(c)
        return tab
    out.append(f"\n=== THE 9 COMBINATIONS: what the chart actually showed, {s_col} x {v_col} ===")
    for name in ("dev", "val"):
        b = base[name]
        nb = sum(b.values())
        out.append(f"  base rates {name}: UP {b[UP] / nb:.0%} DOWN {b[DOWN] / nb:.0%} ROT {b[ROT] / nb:.0%}")
    out.append("  structure / value           | development: n   UP DOWN  ROT -> rule (by lift) "
               "| validation: n   UP DOWN  ROT")
    cd, cv = cells(dev), cells(val)
    cell_map = {}
    bd = base["dev"]
    nbd = sum(bd.values())
    for key in cd:
        a, b = cd[key], cv[key]
        na, nb = sum(a.values()), sum(b.values())
        fa = lambda c, n, t: (c[t] / n) if n else float("nan")
        # the label whose share in this cell most exceeds its base rate (lift), so the
        # majority class (ROTATION) cannot win every cell by sheer frequency
        cell_map[key] = (max((UP, DOWN, ROT), key=lambda t: (a[t] / na) / (bd[t] / nbd))
                         if na >= 30 else ROT)
        agree = "AGREE" if key[0] == key[1] else ("OPPOSE" if {key[0], key[1]} == {UP, DOWN} else "disagree")
        out.append(f"  {key[0]:8s} / {key[1]:8s} {agree:8s} | {na:6d} {fa(a, na, UP):4.0%} {fa(a, na, DOWN):4.0%} "
                   f"{fa(a, na, ROT):4.0%} -> {cell_map[key]:8s}      | {nb:6d} {fa(b, nb, UP):4.0%} "
                   f"{fa(b, nb, DOWN):4.0%} {fa(b, nb, ROT):4.0%}")

    for r in rows:
        for rule in ("R_STRICT", "R_STRUCT", "R_VALUE", "R_EITHER", "R_CELL"):
            r[rule] = combine(rule, r[s_col], r[v_col], cell_map)
    for truth in ("truth", "truth3"):
        for name, part in (("DEVELOPMENT", dev), ("VALIDATION - decides", val)):
            out.append(f"\n=== COMBINATION RULES, {name}, chart truth {truth} ===")
            for col in ("old", s_col, v_col, "R_STRICT", "R_STRUCT", "R_VALUE", "R_EITHER", "R_CELL"):
                out.append(line(part, col, col, truth))
    return "\n".join(out), s_col, v_col, cell_map


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--values", required=True)
    parser.add_argument("--daily-root", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    rows = build_rows(load_values(args.values), args.daily_root)
    text, s_col, v_col, cell_map = report(rows)
    print(text)
    os.makedirs(args.out, exist_ok=True)
    stem = "report" if "xau" not in args.values else "report_xau"
    with open(os.path.join(args.out, f"{stem}.txt"), "w", encoding="utf-8") as h:
        h.write(text + "\n")
    fields = list(rows[0].keys())
    with open(os.path.join(args.out, f"{stem}_labels.csv"), "w", encoding="utf-8", newline="") as h:
        w = csv.DictWriter(h, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
