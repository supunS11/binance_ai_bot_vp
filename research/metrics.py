"""Statistics for the research phases. Standard library only, on purpose.

No numpy/pandas: the live bot needs `requests` and nothing else, and a research
module that drags in a scientific stack makes the two environments diverge - which
eventually means "works in replay, missing in production".

WHAT THIS MODULE IS FOR, PHASE BY PHASE
    Phase 0  `describe` and `percentiles` - a threshold should be an observed
             percentile of the real distribution, not an argued number.
    Phase 1  `wilson_interval` for direction accuracy against 50%, `mean_interval`
             for excursion vs cost.
    Phase 2  `split_half` and `net_r` - a result that does not hold in both halves
             of the sample is not a result.

THE TWO RULES ENCODED HERE

  A CONFIDENCE INTERVAL, NOT A POINT ESTIMATE. "52.1% win rate" invites belief;
  "52.1% (95% CI 48.7-55.4)" shows that the honest answer is a shrug. Every
  comparison helper returns an interval, so a null cannot be read as a finding.

  A THRESHOLD IS A PERCENTILE OF A DISTRIBUTION. If `SHAPE_TREND_MAX_VA_RANGE_RATIO`
  is meant to select the most elongated fifth of sessions, the number to write in
  config is that distribution's 20th percentile - and the distribution has to be
  looked at first, because a bimodal one means the single threshold is the wrong
  instrument regardless of where it goes.
"""
import math
import statistics


# ---------------------------------------------------------------- description

def percentile(values, fraction):
    """Linear-interpolated percentile. `fraction` in [0, 1].

    Interpolating rather than picking the nearest rank matters at the tails, which is
    exactly where thresholds get set: with 40 sessions the 5th percentile falls
    between ranks 2 and 3, and nearest-rank quietly moves it by a whole observation.
    """
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    fraction = min(1.0, max(0.0, float(fraction)))
    position = fraction * (len(ordered) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def describe(values, label=""):
    """n / mean / sd and the percentile ladder thresholds are actually set from."""
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return {"label": label, "n": 0}
    return {
        "label": label,
        "n": len(clean),
        "mean": statistics.fmean(clean),
        "sd": statistics.stdev(clean) if len(clean) > 1 else 0.0,
        "min": min(clean),
        "p05": percentile(clean, 0.05),
        "p10": percentile(clean, 0.10),
        "p20": percentile(clean, 0.20),
        "p25": percentile(clean, 0.25),
        "p50": percentile(clean, 0.50),
        "p75": percentile(clean, 0.75),
        "p80": percentile(clean, 0.80),
        "p90": percentile(clean, 0.90),
        "p95": percentile(clean, 0.95),
        "max": max(clean),
    }


def format_description(row):
    """One-line rendering of `describe`, aligned for reading in a terminal."""
    if not row.get("n"):
        return f"{row.get('label', ''):<34} n=0"
    return (
        f"{row['label']:<34} n={row['n']:<6d} "
        f"mean={row['mean']:>9.4f} sd={row['sd']:>8.4f}  "
        f"p05={row['p05']:>8.4f} p20={row['p20']:>8.4f} p50={row['p50']:>8.4f} "
        f"p80={row['p80']:>8.4f} p95={row['p95']:>8.4f}"
    )


def text_histogram(values, bins=20, width=54, low=None, high=None):
    """ASCII histogram. Reading the SHAPE is the point, not the summary stats.

    A percentile ladder cannot tell you a distribution is bimodal, and a bimodal
    distribution means a single threshold is the wrong instrument - so this gets
    looked at before any number is written into config.
    """
    clean = sorted(float(value) for value in values if value is not None)
    if not clean:
        return "  (no data)"
    low = clean[0] if low is None else float(low)
    high = clean[-1] if high is None else float(high)
    if high <= low:
        return f"  (degenerate range at {low:.4f}, n={len(clean)})"

    counts = [0] * bins
    step = (high - low) / bins
    for value in clean:
        if value < low or value > high:
            continue
        index = min(bins - 1, int((value - low) / step))
        counts[index] += 1

    peak = max(counts) or 1
    lines = []
    for index, count in enumerate(counts):
        edge = low + index * step
        bar = "#" * int(round(width * count / peak))
        lines.append(f"  {edge:>10.4f} | {bar:<{width}} {count}")
    return "\n".join(lines)


# ----------------------------------------------------------------- intervals

def wilson_interval(successes, total, z=1.96):
    """Wilson score interval for a proportion.

    Wilson rather than the textbook normal approximation because the approximation
    misbehaves exactly where these measurements live: near p=0.5 with a few hundred
    observations it is tolerable, but on a thin per-setup slice it can produce bounds
    outside [0, 1], which reads as a coding error and undermines a real result.
    """
    if total <= 0:
        return (0.0, 0.0, 0.0)
    proportion = successes / total
    denominator = 1.0 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = (z / denominator) * math.sqrt(
        proportion * (1.0 - proportion) / total + z * z / (4 * total * total))
    return (proportion, max(0.0, centre - margin), min(1.0, centre + margin))


def mean_interval(values, z=1.96):
    """Mean with a normal-approximation CI on the standard error.

    Adequate for n in the hundreds, which is the regime the phase gates require. For
    thin samples the interval is wide enough to make the thinness obvious, which is
    the behaviour wanted.
    """
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return {"n": 0, "mean": 0.0, "low": 0.0, "high": 0.0, "se": 0.0}
    mean = statistics.fmean(clean)
    if len(clean) < 2:
        return {"n": 1, "mean": mean, "low": mean, "high": mean, "se": 0.0}
    standard_error = statistics.stdev(clean) / math.sqrt(len(clean))
    return {
        "n": len(clean),
        "mean": mean,
        "se": standard_error,
        "low": mean - z * standard_error,
        "high": mean + z * standard_error,
    }


def beats_zero(values, z=1.96):
    """Does the mean's CI exclude zero? The Phase 1/2 gate, stated once."""
    interval = mean_interval(values, z=z)
    if interval["n"] < 2:
        return False
    return interval["low"] > 0.0 or interval["high"] < 0.0


def bonferroni_z(tests, base_alpha=0.05):
    """Two-sided z for `base_alpha` split across `tests` comparisons.

    Bonferroni because it is the conservative choice and the cost of a false positive
    here is shipping a strategy with no edge, which is far more expensive than missing
    one. No scipy: the normal quantile is computed from an inverse-erf series, which is
    ample at these precisions and keeps research on the standard library like the rest of
    this package.
    """
    tests = max(int(tests), 1)
    alpha = base_alpha / tests
    return _normal_quantile(1.0 - alpha / 2.0)


def _normal_quantile(probability):
    """Inverse standard normal CDF, Acklam's rational approximation.

    Accurate to ~1.15e-9 in relative terms over the whole range, which is far more than
    a confidence bound needs.
    """
    if probability <= 0.0 or probability >= 1.0:
        raise ValueError("probability must be in (0, 1)")

    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    low, high = 0.02425, 1.0 - 0.02425

    if probability < low:
        q = math.sqrt(-2.0 * math.log(probability))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if probability > high:
        q = math.sqrt(-2.0 * math.log(1.0 - probability))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    q = probability - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)


def auc(positive, negative, z=1.96):
    """P(a random positive ranks above a random negative), ties at half.

    THE RIGHT INSTRUMENT FOR A RATIO WITH AN UNBOUNDED TAIL, and the reason this
    exists is a real misreading it caused. The acceptance sweep originally judged its
    own discriminator with a difference of MEANS, on a quantity whose mean was 64.2
    with sd 4212 against a p95 of 3.61 - a handful of excursions with a near-zero
    denominator dominated both the mean and its standard error, so the comparison
    reported "no separation" on a population where the continuation rate in fact rose
    monotonically across every band of that same quantity. A mean test asks whether
    the AVERAGE differs, which a single outlier can answer either way; this asks
    whether the ORDERING differs, which no outlier can move by more than its one rank.

    0.5 is no information. The interval is the Hanley-McNeil variance, which accounts
    for the two sample sizes separately - important here because CONTINUED is ~2.5x
    rarer than REVERTED.

    Returns None below 20 per side rather than a wide interval, because a rank
    statistic on a handful of points invites reading structure into noise.
    """
    positive = [float(value) for value in positive if value is not None]
    negative = [float(value) for value in negative if value is not None]
    if len(positive) < 20 or len(negative) < 20:
        return None

    merged = sorted([(value, 1) for value in positive]
                    + [(value, 0) for value in negative])
    # Midranks. Ties MUST share a rank or a quantity with a common value - a ratio
    # pinned at exactly 1.0 by a bug, say - would score as separation.
    ranks = [0.0] * len(merged)
    index = 0
    while index < len(merged):
        stop = index
        while stop + 1 < len(merged) and merged[stop + 1][0] == merged[index][0]:
            stop += 1
        midrank = (index + stop) / 2.0 + 1.0
        for position in range(index, stop + 1):
            ranks[position] = midrank
        index = stop + 1

    n_pos, n_neg = len(positive), len(negative)
    rank_sum = sum(rank for rank, (_, label) in zip(ranks, merged) if label == 1)
    value = (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)

    q1 = value / (2.0 - value)
    q2 = 2.0 * value * value / (1.0 + value)
    variance = (value * (1.0 - value)
                + (n_pos - 1) * (q1 - value * value)
                + (n_neg - 1) * (q2 - value * value)) / (n_pos * n_neg)
    standard_error = math.sqrt(max(variance, 0.0))
    return {
        "auc": value,
        "se": standard_error,
        "low": value - z * standard_error,
        "high": value + z * standard_error,
        "n_pos": n_pos,
        "n_neg": n_neg,
    }


def auc_verdict(result):
    """One word for an AUC interval: informative, inverted, or null."""
    if result is None:
        return "too thin"
    if result["low"] > 0.5:
        return "informative"
    if result["high"] < 0.5:
        return "INVERTED"
    return "null"


def auc_within(rows, stratify_of, measure_of, label_of, bins=5, z=1.96):
    """AUC of one measure inside strata of another, pooled by sample size.

    THE ATTRIBUTION TEST, and the one that decides whether a threshold deserves to
    exist. Two correlated quantities both separate an outcome; stratifying on one and
    re-measuring the other says which carries the information. A quantity that keeps
    its AUC inside the other's strata is contributing; one that collapses to 0.5 was a
    proxy and its threshold is redundant with the other's.

    Strata are equal-count quantiles rather than equal-width bins, so no stratum is
    thin enough to be noise. Pooling is weighted by n and the standard errors are
    combined assuming independence, which holds because the strata partition the rows.
    """
    clean = [row for row in rows
             if stratify_of(row) is not None and measure_of(row) is not None]
    if len(clean) < bins * 40:
        return None, []

    # DE-DUPLICATE THE EDGES. Quantile cuts on a heavily tied variable collapse: five
    # equally likely discrete values produce edges like [-0.5, -0.5, 0.0, 1.0], and the
    # repeat leaves an empty stratum while two real levels share another. Worse, a
    # stratifier that is CONSTANT - which is not hypothetical, consecutive_closes is
    # exactly that in the default acceptance corpus - yields no edges at all, so every
    # row lands in one stratum, NO control is actually applied, and the pooled number
    # comes back equal to the unstratified AUC while reading as though it had survived
    # a control. That is the same failure this project has met four times: a check whose
    # own construction guarantees the answer. So `strata` is returned, and one stratum
    # means the control was vacuous.
    edges = sorted({percentile([stratify_of(row) for row in clean], index / bins)
                    for index in range(1, bins)})

    total, weighted, variance = 0, 0.0, 0.0
    detail = []
    for index in range(len(edges) + 1):
        low = edges[index - 1] if index > 0 else None
        high = edges[index] if index < len(edges) else None
        group = [row for row in clean
                 if (low is None or stratify_of(row) >= low)
                 and (high is None or stratify_of(row) < high)]
        result = auc([measure_of(row) for row in group if label_of(row)],
                     [measure_of(row) for row in group if not label_of(row)], z=z)
        if result is None:
            continue
        count = result["n_pos"] + result["n_neg"]
        total += count
        weighted += result["auc"] * count
        variance += (result["se"] * count) ** 2
        detail.append({"index": index + 1, "low": low, "high": high,
                       "n": count, **result})
    if not total:
        return None, detail
    mean = weighted / total
    standard_error = math.sqrt(variance) / total
    return {"auc": mean, "se": standard_error,
            "low": mean - z * standard_error, "high": mean + z * standard_error,
            "n_pos": total, "n_neg": 0,
            # `strata` is not decoration. 1 means the stratifying variable did not vary
            # and NO control was applied, so the number must not be read as a control.
            "strata": len(detail), "vacuous": len(detail) <= 1}, detail


# ------------------------------------------------------------------- splits

def split_half(rows, key, value_of):
    """Split on `key` at its median and compare the two halves' means.

    THE ONLY REPLICATION TEST THAT COSTS NOTHING. A threshold fitted to a sample
    will look good on that sample; the cheapest evidence that it is not pure
    overfitting is that it points the same way in both halves. `key` is usually time
    (early vs late sessions) or qv_rank (liquid vs thin symbols) - two different
    kinds of fragility, both worth knowing about.

    Returns `consistent` only when both halves have data AND agree in sign.
    """
    usable = [row for row in rows if row.get(key) is not None
              and value_of(row) is not None]
    if len(usable) < 4:
        return {"n": len(usable), "consistent": False, "reason": "too few rows"}

    ordered = sorted(usable, key=lambda row: row[key])
    midpoint = len(ordered) // 2
    first = [value_of(row) for row in ordered[:midpoint]]
    second = [value_of(row) for row in ordered[midpoint:]]

    left = mean_interval(first)
    right = mean_interval(second)
    same_sign = (left["mean"] > 0) == (right["mean"] > 0)

    return {
        "n": len(ordered),
        "split_key": key,
        "first": left,
        "second": right,
        "consistent": bool(same_sign),
        "reason": "" if same_sign else "halves disagree in sign",
    }


def group_by(rows, key):
    """Bucket rows by a field, for per-setup and per-shape breakdowns."""
    out = {}
    for row in rows:
        out.setdefault(row.get(key), []).append(row)
    return out


# ------------------------------------------------------------------ outcomes

def round_trip_cost_r(risk_distance, entry_price, quantity=1.0,
                      maker_fee=None, taker_fee=None, slippage_bps=None):
    """Fees plus slippage expressed in R - the number every edge has to clear.

    Stated in R rather than percent because that is the unit a result arrives in: a
    setup with a mean excursion of +0.15R and a round-trip cost of 0.18R is a losing
    strategy no matter how good its direction accuracy looks.

    Assumes the system's actual mechanics: maker in (GTX), taker out (STOP_MARKET) on
    the losing side, which is the conservative assumption.
    """
    import config
    maker = config.FEE_MAKER if maker_fee is None else maker_fee
    taker = config.FEE_TAKER if taker_fee is None else taker_fee
    slip = config.SLIPPAGE_MODEL_BPS if slippage_bps is None else slippage_bps

    if risk_distance <= 0 or entry_price <= 0:
        return 0.0
    notional = entry_price * quantity
    fees = notional * (maker + taker)
    slippage = notional * (slip / 10_000.0)
    risk_value = risk_distance * quantity
    return (fees + slippage) / risk_value if risk_value > 0 else 0.0


def net_r(gross_r, cost_r):
    return float(gross_r) - float(cost_r)


def clustered_mean_interval(rows, value_of, cluster_of, z=1.96):
    """Mean with a CLUSTER-ROBUST CI. The honest interval for correlated trades.

    THE ASSUMPTION A PLAIN CI MAKES HERE IS FALSE, and it fails in the direction that
    manufactures significance. `mean_interval` divides by sqrt(n) because it treats every
    observation as independent evidence. These trades are not: roughly two thousand of
    them sit inside about a hundred and eighty UTC days, and on any one day the whole alt
    perpetual complex moves together. risk.py already argues the point from the other side
    - "ten alt longs is one leveraged bet on BTC, not a diversified book" - and a
    statistic cannot assume the diversification that the risk module explicitly denies.

    Twenty trades taken on one day during one BTC move are close to ONE observation
    repeated twenty times. Counting them as twenty shrinks the interval by up to sqrt(20)
    and turns a coin flip into a finding. This is the standard way a backtest reports
    confidence it has not earned.

    So the variance is computed BETWEEN clusters rather than between trades:

        Var(mean) = G/(G-1) * (1/n^2) * sum_g ( sum_{i in g} (x_i - mean) )^2

    A cluster whose trades all deviate the same way contributes its whole summed
    deviation, which is what makes correlated trades cost their own precision. When
    trades happen to be uncorrelated within clusters this returns approximately the
    ordinary interval, so it is never worse than the naive one - only sometimes much
    wider, which is the point.

    Clustering is by UTC session, because that is the unit a common market move arrives
    in. It does NOT correct for correlation ACROSS adjacent days (a three-day trend is
    still three clusters), so this remains optimistic - just far less so.
    """
    pairs = [(value_of(row), cluster_of(row)) for row in rows
             if value_of(row) is not None]
    if not pairs:
        return {"n": 0, "clusters": 0, "mean": 0.0, "low": 0.0, "high": 0.0, "se": 0.0}

    values = [value for value, _ in pairs]
    count = len(values)
    mean = statistics.fmean(values)
    if count < 2:
        return {"n": count, "clusters": 1, "mean": mean, "low": mean, "high": mean,
                "se": 0.0}

    sums = {}
    for value, key in pairs:
        sums[key] = sums.get(key, 0.0) + (value - mean)
    groups = len(sums)
    if groups < 2:
        # One cluster cannot estimate between-cluster variance. Falling back to the naive
        # interval would silently report the very number this function exists to replace,
        # so the fallback is flagged.
        naive = mean_interval(values, z=z)
        return {**naive, "clusters": groups, "degenerate": True}

    correction = groups / (groups - 1.0)
    variance = correction * sum(total * total for total in sums.values()) / (count ** 2)
    standard_error = math.sqrt(max(variance, 0.0))
    return {
        "n": count,
        "clusters": groups,
        "mean": mean,
        "se": standard_error,
        "low": mean - z * standard_error,
        "high": mean + z * standard_error,
        "degenerate": False,
    }


def summarise_r(rows, value_of=lambda row: row.get("net_r"),
                cluster_of=lambda row: row.get("session_id"), z=1.96):
    """Headline R statistics: mean with CI, win rate with CI, payoff ratio.

    Two intervals are returned and they are NOT interchangeable. `r_low`/`r_high` are
    cluster-robust and are what `significant` is judged on; `naive_low`/`naive_high` are
    the independence-assuming pair, kept only so the gap between them is visible. A large
    gap is itself the finding: it says the apparent sample size was mostly repetition.

    Pass `cluster_of=None` to opt out, which is correct only when the rows are genuinely
    independent by construction.
    """
    values = [value_of(row) for row in rows if value_of(row) is not None]
    if not values:
        return {"n": 0}

    wins = [value for value in values if value > 0]
    losses = [value for value in values if value <= 0]
    win_rate, win_low, win_high = wilson_interval(len(wins), len(values))
    naive = mean_interval(values, z=z)

    if cluster_of is None:
        interval = naive
        clusters = None
    else:
        interval = clustered_mean_interval(rows, value_of, cluster_of, z=z)
        clusters = interval.get("clusters")

    average_win = statistics.fmean(wins) if wins else 0.0
    average_loss = abs(statistics.fmean(losses)) if losses else 0.0

    return {
        "n": len(values),
        "clusters": clusters,
        "mean_r": interval["mean"],
        # The cluster-robust standard error, exported so a caller combining two summaries
        # (the control's lift test) does not have to back it out of the bounds and a
        # hard-coded z. Backing it out breaks the moment a summary is taken at a
        # corrected z, which the multiplicity section now does.
        "se": interval.get("se", 0.0),
        "z": z,
        "r_low": interval["low"],
        "r_high": interval["high"],
        "naive_low": naive["low"],
        "naive_high": naive["high"],
        # The inflation factor the naive interval was claiming. 1.0 means clustering
        # changed nothing; 3.0 means the plain CI was three times too narrow.
        "se_inflation": (interval["se"] / naive["se"]) if naive.get("se") else 1.0,
        "significant": interval["low"] > 0.0,
        "significant_naive": naive["low"] > 0.0,
        "win_rate": win_rate,
        "win_low": win_low,
        "win_high": win_high,
        "avg_win": average_win,
        "avg_loss": average_loss,
        "payoff": (average_win / average_loss) if average_loss > 0 else 0.0,
        "total_r": sum(values),
    }


def format_r_summary(label, row):
    """One line per setup. The interval shown is the CLUSTER-ROBUST one.

    `*` marks a mean whose cluster-robust lower bound clears zero. `~` marks one that
    would have been starred under the independence assumption but is not once same-day
    correlation is paid for - the single most useful character in this report, because
    that is exactly the trade this project must not ship.
    """
    if not row.get("n"):
        return f"{label:<26} n=0"
    if row["significant"]:
        mark = "  *"
    elif row.get("significant_naive"):
        mark = "  ~"
    else:
        mark = "   "
    clusters = row.get("clusters")
    cluster_text = f" g={clusters:<4}" if clusters else " " * 7
    return (
        f"{label:<26} n={row['n']:<5d}{cluster_text}"
        f"meanR={row['mean_r']:>+7.4f} [{row['r_low']:>+7.4f},{row['r_high']:>+7.4f}]"
        f"{mark} "
        f"win={row['win_rate']*100:>5.1f}% "
        f"payoff={row['payoff']:>5.2f} totalR={row['total_r']:>+8.1f}"
    )
