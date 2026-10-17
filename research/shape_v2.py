"""Shape classifier v2 - research; classify_v2 is shared with the live S6-WF setup (setups/workflow.py), so live and research run the same rules (the veto input).

WHY. A blind eye check (calibration/shape_fix: 180 random profiles labelled by eye, without
the classifier's label, against the book definitions below) found the live classifier
(profile/shape.classify) right for D 86%, b 74%, P 59%, B 30%, and 'trend' ~2%: the
'elongated' trend days are really P/b days with long tails, the 'migration' trend days are
mostly double distributions, and single spiky bins fake B's second peak and valley.

DEFINITIONS (Dale VP pp.56-59, Dale OF pp.117-120, AK ch.6, Capital B guide):
  D     one central bulge, POC near the middle, thin tails of similar length both sides
  P     the bulge near the TOP, a long thin tail below (fast rise, then rotation high)
  b     the mirror: bulge near the BOTTOM, long thin tail above
  THIN  volume spread thinly over a wide range, no major bulge; one-directional (opens
        near one end, closes near the other) - THIN_UP / THIN_DOWN
  B     two separate bulges joined by a clearly thin neck (two balances in one day)

MEASUREMENT. The day's histogram is resampled onto ROWS equal-height rows spanning the day
range and lightly smoothed (3-row moving average), so the shape is read at a chart-like
resolution instead of 0.02-ATR bins whose spikes fake peaks and valleys.
  B     the two highest separated local peaks (>= SEP of the range apart), the lowest
        smoothed row between them <= VALLEY x the smaller peak, and each side of that
        valley holding >= MASS of the day's volume
  THIN  smoothed max / mean <= FLAT (no major bulge) AND |close - open| >= BODY x range
  P / b by where the value area sits: centre = value-area midpoint position in the range.
        P when centre >= CENTRE and the tail below the value area is longer than the tail
        above it; b the mirror; otherwise D
  DEGENERATE  fewer than MIN_BINS non-empty original bins (e.g. near-empty weekend days)
  SKIP  (only when SKIP_MASS is set) a pair passes SEP and VALLEY but its smaller side holds
        between SKIP_MASS and MASS of the volume - a small separate cluster in a tail. By eye
        these split evenly between B, P and b (14 reference days: 5 / 5 / 4), so the day gets
        no shape instead of a guess (user decision 2026-10-14; ~5% of days)
Order: DEGENERATE, B/SKIP, THIN, P/b, D (residual) - as in the live classifier, the
distribution-level reads come first and D is what remains.

Parameters are chosen on the ODD-numbered reference profiles only and checked on the EVEN
ones (research/shape_v2 --tune); see calibration/shape_fix/report.txt.
"""
import itertools
import json
import os

import numpy as np

ROWS = 30
DEFAULTS = {"SEP": 0.25, "VALLEY": 0.6, "MASS": 0.2, "FLAT": 1.8, "BODY": 0.6,
            "CENTRE": 0.6, "MIN_BINS": 8, "SKIP_MASS": None}
# Approved configuration: tuned parameters (calibration/shape_fix/chosen_params.json) with
# B requiring 0.25 per side and 0.15-0.25 skipped. 0.25 was the first-round tuned MASS; on
# the dev half 0.25 and 0.30 tied for the skip cut, 0.25 skips fewer days.
FINAL = {"SEP": 0.25, "VALLEY": 0.3, "MASS": 0.25, "FLAT": 1.8, "CENTRE": 0.6,
         "SKIP_MASS": 0.15}


def rows_profile(bins, low, high, rows=ROWS):
    """Resample {price: volume} onto `rows` equal-height rows over [low, high], smooth."""
    if high <= low:
        return None
    vol = np.zeros(rows)
    for price, v in bins.items():
        k = int((float(price) - low) / (high - low) * rows)
        vol[min(max(k, 0), rows - 1)] += v
    if vol.sum() <= 0:
        return None
    padded = np.concatenate([[vol[0]], vol, [vol[-1]]])
    return np.convolve(padded, np.ones(3) / 3, mode="valid")


def _best_b_pair(smooth, p):
    """(second-bump share, valley row) of the qualifying peak pair with the largest share;
    (0.0, None) when no pair qualifies."""
    rows = len(smooth)
    peaks = [i for i in range(rows)
             if smooth[i] >= (smooth[i - 1] if i > 0 else 0) and smooth[i] >= (smooth[i + 1] if i < rows - 1 else 0)]
    sep = max(2, int(round(p["SEP"] * rows)))
    best, best_valley = 0.0, None
    for a, b in itertools.combinations(sorted(peaks, key=lambda i: -smooth[i])[:6], 2):
        lo, hi = sorted((a, b))
        if hi - lo < sep:
            continue
        valley = lo + 1 + int(np.argmin(smooth[lo + 1:hi]))
        if smooth[valley] > p["VALLEY"] * min(smooth[lo], smooth[hi]):
            continue
        below = smooth[:valley].sum() / smooth.sum()
        above = smooth[valley + 1:].sum() / smooth.sum()
        if min(below, above) > best:
            best, best_valley = min(below, above), valley
    return best, best_valley


def b_strength(smooth, p):
    """Second-bump share: among the top-6 peak pairs that are >= SEP apart with a valley
    <= VALLEY x the smaller peak, the largest volume share on the smaller side of the
    valley. 0.0 when no pair qualifies."""
    return _best_b_pair(smooth, p)[0]


def b_split_price(day, p=None):
    """Price of the thin neck between a B day's two bulges (centre of the valley row), or
    None when the day has no qualifying pair. Used to tell the upper from the lower cluster."""
    p = {**DEFAULTS, **(p or {})}
    smooth = rows_profile(day["bins"], day["low"], day["high"])
    if smooth is None:
        return None
    _, valley = _best_b_pair(smooth, p)
    if valley is None:
        return None
    return day["low"] + (valley + 0.5) / len(smooth) * (day["high"] - day["low"])


def bimodal(smooth, p):
    return b_strength(smooth, p) >= p["MASS"]


def classify_v2(day, p=None):
    """day: dict with bins {price: volume}, low, high, open, close, vah, val. Returns
    'P' | 'b' | 'D' | 'B' | 'THIN_UP' | 'THIN_DOWN' | 'SKIP' (unsure) | None (degenerate)."""
    p = {**DEFAULTS, **(p or {})}
    if sum(1 for v in day["bins"].values() if v > 0) < p["MIN_BINS"]:
        return None
    low, high = day["low"], day["high"]
    smooth = rows_profile(day["bins"], low, high)
    if smooth is None:
        return None
    rng = high - low
    strength = b_strength(smooth, p)
    if strength >= p["MASS"]:
        return "B"
    if p["SKIP_MASS"] is not None and strength >= p["SKIP_MASS"]:
        return "SKIP"
    body = (day["close"] - day["open"]) / rng
    if smooth.max() / smooth.mean() <= p["FLAT"] and abs(body) >= p["BODY"]:
        return "THIN_UP" if body > 0 else "THIN_DOWN"
    centre = ((day["vah"] + day["val"]) / 2 - low) / rng
    tail_below = (day["val"] - low) / rng
    tail_above = (high - day["vah"]) / rng
    if centre >= p["CENTRE"] and tail_below > tail_above:
        return "P"
    if centre <= 1 - p["CENTRE"] and tail_above > tail_below:
        return "b"
    return "D"


# ------------------------------------------------------------------ tuning / evaluation

# VALLEY and MASS were extended after the first tuning chose the strictest value at the
# edge of the original grid (0.5 / 0.25); same dev/test split, test still untouched.
GRID = {"SEP": (0.2, 0.25, 0.3, 0.35), "VALLEY": (0.3, 0.4, 0.5, 0.6, 0.7),
        "MASS": (0.15, 0.2, 0.25, 0.3, 0.35),
        "FLAT": (1.6, 1.8, 2.0), "CENTRE": (0.56, 0.58, 0.6, 0.62, 0.65)}


def _ref_name(r):
    return {"TU": "THIN_UP", "TD": "THIN_DOWN"}.get(r, r)


def accuracy(days, ref, p):
    """Accuracy on the clear (non-X) profiles the classifier labels; SKIP days are left out
    of the accuracy and counted separately. Returns (accuracy, labelled n, skipped n)."""
    clear = [d for d in days if ref[d["id"]] != "X"]
    labels = [(classify_v2(d, p), _ref_name(ref[d["id"]])) for d in clear]
    kept = [(c, r) for c, r in labels if c != "SKIP"]
    hits = sum(c == r for c, r in kept)
    return hits / len(kept) if kept else float("nan"), len(kept), len(labels) - len(kept)


def main():
    import argparse
    import csv
    import collections
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default="calibration/shape_fix")
    args = parser.parse_args()
    with open(os.path.join(args.dir, "reference_sample.jsonl"), encoding="utf-8") as h:
        days = [json.loads(line) for line in h]
    with open(os.path.join(args.dir, "reference_labels.csv"), encoding="utf-8") as h:
        ref = {int(r["id"]): r["ref"] for r in csv.DictReader(h)}
    with open(os.path.join(args.dir, "reference_key.csv"), encoding="utf-8") as h:
        key = {int(r["id"]): r for r in csv.DictReader(h)}
    dev = [d for d in days if d["id"] % 2 == 1]
    test = [d for d in days if d["id"] % 2 == 0]
    names = list(GRID)
    scored = []
    for combo in itertools.product(*(GRID[n] for n in names)):
        p = dict(zip(names, combo))
        scored.append((accuracy(dev, ref, p)[0], p))
    best_acc = max(s for s, _ in scored)
    # among ties, prefer the parameters closest to the pre-set DEFAULTS (least tuning)
    ties = [p for s, p in scored if s == best_acc]
    dist = lambda p: sum(abs(p[n] - DEFAULTS[n]) / max(GRID[n]) for n in names)
    best = min(ties, key=dist)

    def old_acc(part):
        clear = [d for d in part if ref[d["id"]] != "X"]
        old = lambda d: {"trend": "THIN"}.get(key[d["id"]]["old_label"], key[d["id"]]["old_label"])
        refn = lambda r: "THIN" if r in ("TU", "TD") else r
        return sum(old(d) == refn(ref[d["id"]]) for d in clear) / len(clear), len(clear)

    out = [f"reference profiles: {len(days)} (dev = odd ids {len(dev)}, test = even ids {len(test)})",
           f"best dev accuracy {best_acc:.1%} reached by {len(ties)} parameter sets; chosen "
           f"(closest to pre-set defaults): {best}"]
    for name, part in (("DEV", dev), ("TEST - decides", test)):
        a_new, n, _ = accuracy(part, ref, best)
        a_def, _, _ = accuracy(part, ref, None)
        a_old, _ = old_acc(part)
        a_fin, n_fin, sk_fin = accuracy(part, ref, FINAL)
        out.append(f"  {name:15s} n={n}: live classifier {a_old:.1%} | v2 pre-set defaults "
                   f"{a_def:.1%} | v2 tuned {a_new:.1%} | FINAL (skip unsure B) {a_fin:.1%} "
                   f"on {n_fin}, skipped {sk_fin}")
    out.append("\nTEST confusion (rows = eye label, columns = v2 tuned):")
    labs = ["P", "b", "D", "B", "THIN_UP", "THIN_DOWN", None]
    conf = collections.Counter((_ref_name(ref[d["id"]]), classify_v2(d, best)) for d in test
                               if ref[d["id"]] != "X")
    out.append("  eye \\ v2     " + "".join(f"{str(l):>10s}" for l in labs))
    for r in labs[:-1]:
        out.append(f"  {r:12s}" + "".join(f"{conf[(r, c)]:10d}" for c in labs))
    out.append("\nALL 180 confusion (eye vs v2 tuned), incl. X:")
    conf_all = collections.Counter((ref[d["id"]], classify_v2(d, best)) for d in days)
    for r in ("P", "b", "D", "B", "TU", "TD", "X"):
        out.append(f"  {r:4s} " + ", ".join(f"{c}: {conf_all[(r, c)]}" for c in labs if conf_all[(r, c)]))
    out.append(f"\nFINAL {FINAL} - ALL 180 confusion (eye vs FINAL), incl. X and SKIP:")
    labs_f = labs + ["SKIP"]
    conf_fin = collections.Counter((ref[d["id"]], classify_v2(d, FINAL)) for d in days)
    for r in ("P", "b", "D", "B", "TU", "TD", "X"):
        out.append(f"  {r:4s} " + ", ".join(f"{c}: {conf_fin[(r, c)]}" for c in labs_f if conf_fin[(r, c)]))
    text = "\n".join(out)
    print(text)
    with open(os.path.join(args.dir, "report.txt"), "w", encoding="utf-8") as h:
        h.write(text + "\n")
    with open(os.path.join(args.dir, "chosen_params.json"), "w", encoding="utf-8") as h:
        json.dump(best, h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
