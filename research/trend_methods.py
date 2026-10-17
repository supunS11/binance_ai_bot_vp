"""Established trend-detection methods on daily candles - research; m_turtle is
shared with the live S6-WF setup (setups/workflow.py), so live and research run the same rules. Implements the candidates of calibration/trend_methods/test_plan.md exactly as
their sources describe them; settings are the sources' own, not tuned.

Indicator conventions follow TA-Lib (the reference the values are checked against):
SMA/EMA seeded with the simple mean of the first n values, Wilder smoothing for ATR/ADX
seeded with a simple mean, true range from the second bar on.

Every label for day D is computed from candles BEFORE D only (closes up to D-1), the same
convention as the earlier trend labels (known before day D opens).
"""
import math

import numpy as np

UP, DOWN, RANGE = "UP", "DOWN", "RANGE"


# ------------------------------------------------------------------ indicators

def sma(x, n):
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema(x, n):
    """alpha = 2/(n+1), seeded with the SMA of the first n values (TA-Lib)."""
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    a = 2.0 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def wma(x, n):
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    w = np.arange(1, n + 1, dtype=float)
    for i in range(n - 1, len(x)):
        out[i] = np.dot(x[i - n + 1:i + 1], w) / w.sum()
    return out


def hma(x, n):
    """Hull MA: WMA(2*WMA(n/2) - WMA(n), sqrt(n))."""
    half, root = int(n / 2), int(math.floor(math.sqrt(n)))
    raw = 2 * wma(x, half) - wma(x, n)
    out = np.full(len(raw), np.nan)
    ok = ~np.isnan(raw)
    if ok.sum() >= root:
        first = int(np.argmax(ok))
        out[first:] = wma(raw[first:], root)
    return out


def vwma(close, volume, n):
    close, volume = np.asarray(close, float), np.asarray(volume, float)
    num, den = sma(close * volume, n), sma(volume, n)
    with np.errstate(invalid="ignore", divide="ignore"):
        return num / den


def true_range(h, l, c):
    tr = np.full(len(c), np.nan)
    for i in range(1, len(c)):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return tr


def wilder(x, n, start):
    """Wilder smoothing of x[start:], seeded with the simple mean of its first n values."""
    out = np.full(len(x), np.nan)
    if len(x) - start < n:
        return out
    out[start + n - 1] = np.mean(x[start:start + n])
    for i in range(start + n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def atr(h, l, c, n=14):
    return wilder(true_range(h, l, c), n, 1)


def adx(h, l, c, n=14):
    """Wilder ADX, TA-Lib form: DM and TR summed Wilder-style, DX averaged into ADX."""
    h, l, c = (np.asarray(v, float) for v in (h, l, c))
    m = len(c)
    pdm, mdm, tr = np.zeros(m), np.zeros(m), true_range(h, l, c)
    for i in range(1, m):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm[i] = up if up > dn and up > 0 else 0.0
        mdm[i] = dn if dn > up and dn > 0 else 0.0
    out = np.full(m, np.nan)
    if m < 2 * n:
        return out
    s_tr, s_p, s_m = tr[1:n].sum(), pdm[1:n].sum(), mdm[1:n].sum()
    dx = np.full(m, np.nan)
    for i in range(n, m):
        s_tr = s_tr - s_tr / n + tr[i]
        s_p = s_p - s_p / n + pdm[i]
        s_m = s_m - s_m / n + mdm[i]
        pdi, mdi = 100 * s_p / s_tr, 100 * s_m / s_tr
        dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi > 0 else 0.0
    out[2 * n - 1] = np.mean(dx[n:2 * n])
    for i in range(2 * n, m):
        out[i] = (out[i - 1] * (n - 1) + dx[i]) / n
    return out


def supertrend(h, l, c, n=10, mult=3.0):
    """Direction (+1 up / -1 down) of the TradingView Supertrend: bands hl2 -/+ mult*ATR,
    the lower band only rises and the upper band only falls while price stays beyond them;
    a close through the active band flips the direction."""
    h, l, c = (np.asarray(v, float) for v in (h, l, c))
    a = atr(h, l, c, n)
    hl2 = (h + l) / 2
    d = np.full(len(c), np.nan)
    upper = lower = None
    direction = None
    for i in range(len(c)):
        if np.isnan(a[i]):
            continue
        bu, bl = hl2[i] + mult * a[i], hl2[i] - mult * a[i]
        if upper is None:
            upper, lower, direction = bu, bl, 1
        else:
            lower = bl if (bl > lower or c[i - 1] < lower) else lower
            upper = bu if (bu < upper or c[i - 1] > upper) else upper
            if direction == 1 and c[i] < lower:
                direction = -1
            elif direction == -1 and c[i] > upper:
                direction = 1
        d[i] = direction
    return d


def donchian_mid(h, l, n):
    h, l = np.asarray(h, float), np.asarray(l, float)
    out = np.full(len(h), np.nan)
    for i in range(n - 1, len(h)):
        out[i] = (h[i - n + 1:i + 1].max() + l[i - n + 1:i + 1].min()) / 2
    return out


# ------------------------------------------------------------------ methods
# Each returns a list of labels per bar index i, where label[i] reads the chart as of the
# CLOSE of bar i. day_labels() shifts this by one bar (label for day D = state at D-1 close).

def m_tsmom(c, lookbacks):
    """AQR / Moskowitz et al: sign of the past return over each lookback; all agree -> trend."""
    out = [None] * len(c)
    for i in range(len(c)):
        if i < max(lookbacks):
            continue
        signs = {np.sign(c[i] / c[i - k] - 1) for k in lookbacks}
        out[i] = UP if signs == {1.0} else DOWN if signs == {-1.0} else RANGE
    return out


def m_price_vs_sma(c, n=200):
    s = sma(c, n)
    return [None if np.isnan(s[i]) else UP if c[i] > s[i] else DOWN if c[i] < s[i] else RANGE
            for i in range(len(c))]


def m_ema_cross(c, fast=20, slow=50):
    f, s = ema(c, fast), ema(c, slow)
    return [None if np.isnan(s[i]) else UP if f[i] > s[i] else DOWN if f[i] < s[i] else RANGE
            for i in range(len(c))]


def m_turtle(h, l, c, entry=55, exit_=20):
    """Turtle System 2 as a state: a close above the prior 55-day high -> UP (below the
    prior 55-day low -> DOWN); a close through the prior 20-day low (high) ends it -> RANGE
    until the next 55-day breakout."""
    out = [None] * len(c)
    state = RANGE
    for i in range(len(c)):
        if i < entry:
            continue
        hi55, lo55 = max(h[i - entry:i]), min(l[i - entry:i])
        hi20, lo20 = max(h[i - exit_:i]), min(l[i - exit_:i])
        if c[i] > hi55:
            state = UP
        elif c[i] < lo55:
            state = DOWN
        elif state == UP and c[i] < lo20:
            state = RANGE
        elif state == DOWN and c[i] > hi20:
            state = RANGE
        out[i] = state
    return out


def m_supertrend(h, l, c):
    d = supertrend(h, l, c, 10, 3.0)
    return [None if np.isnan(v) else UP if v > 0 else DOWN for v in d]


def tv_ma_rating(h, l, c, v):
    """TradingView Technical Ratings, moving-average part: 15 votes (SMA and EMA 10/20/30/
    50/100/200, Hull 9, VWMA 20: +1 if MA < price, -1 if MA > price; Ichimoku 9/26/52: +1
    if spanA > spanB and base > spanA and conversion > base and price > conversion, -1 for
    the mirror, else 0). Spans are taken at the current bar, without the 26-bar forward
    shift of the plotted cloud. Returns the mean vote, or nan without full history."""
    c = np.asarray(c, float)
    lines = [sma(c, n) for n in (10, 20, 30, 50, 100, 200)] + \
            [ema(c, n) for n in (10, 20, 30, 50, 100, 200)] + [hma(c, 9), vwma(c, v, 20)]
    conv, base = donchian_mid(h, l, 9), donchian_mid(h, l, 26)
    span_a, span_b = (conv + base) / 2, donchian_mid(h, l, 52)
    score = np.full(len(c), np.nan)
    for i in range(len(c)):
        vals = [m[i] for m in lines]
        if any(np.isnan(x) for x in vals) or np.isnan(span_b[i]):
            continue
        votes = [1 if x < c[i] else -1 if x > c[i] else 0 for x in vals]
        if span_a[i] > span_b[i] and base[i] > span_a[i] and conv[i] > base[i] and c[i] > conv[i]:
            votes.append(1)
        elif span_a[i] < span_b[i] and base[i] < span_a[i] and conv[i] < base[i] and c[i] < conv[i]:
            votes.append(-1)
        else:
            votes.append(0)
        score[i] = np.mean(votes)
    return score


def m_tv_rating(h, l, c, v):
    s = tv_ma_rating(h, l, c, v)
    return [None if np.isnan(x) else UP if x > 0.1 else DOWN if x < -0.1 else RANGE for x in s]


def with_adx(labels, h, l, c, threshold=20):
    a = adx(h, l, c, 14)
    return [None if lab is None or np.isnan(a[i]) else RANGE if a[i] < threshold else lab
            for i, lab in enumerate(labels)]


def ensemble(*label_lists, bound=0.5):
    vote = {UP: 1, DOWN: -1, RANGE: 0}
    out = []
    for labs in zip(*label_lists):
        if any(x is None for x in labs):
            out.append(None)
            continue
        m = np.mean([vote[x] for x in labs])
        out.append(UP if m >= bound else DOWN if m <= -bound else RANGE)
    return out


def all_methods(h, l, c, v):
    """{method id: per-bar labels as of each bar's close}."""
    m1 = m_tsmom(c, (20, 60))
    m3 = m_price_vs_sma(c, 200)
    m4 = m_ema_cross(c, 20, 50)
    m5 = m_turtle(h, l, c, 55, 20)
    m6 = m_supertrend(h, l, c)
    return {
        "M1": m1, "M2": m_tsmom(c, (20, 60, 250)), "M3": m3, "M4": m4, "M5": m5, "M6": m6,
        "M7": m_tv_rating(h, l, c, v),
        "M3a": with_adx(m3, h, l, c), "M4a": with_adx(m4, h, l, c), "M6a": with_adx(m6, h, l, c),
        "E1": ensemble(m1, m3, m4, m5, m6),
    }


def day_labels(per_bar):
    """Label for day index i = state as of the close of bar i-1 (known before day i)."""
    return [None] + list(per_bar[:-1])
