"""PLAN item 11: an INDEPENDENT regime label, to calibrate profile/shape.py against.

WHY THIS LIVES IN research/, NOT IN THE LIVE PATH. Principle P1 (config.py's own
docstring): volume distribution is the sole source of trading premises, and this
system deliberately carries no indicator periods beyond the ATR used to normalise
distances. Efficiency ratio is a classic price-series indicator - exactly the kind of
thing P1 excludes from ever gating a trade. Its only job here is as a CROSS-CHECK: a
regime read that shares none of shape.py's own inputs (bins, POC, value area), so it
can answer whether shape.py's D/P/b/B/trend labels - and the thresholds that produce
them, `SHAPE_TREND_MAX_VA_RANGE_RATIO` and `SHAPE_TREND_MIN_POC_MIGRATION_ATR` -
actually track a real, independently-measurable regime, or merely something shape.py
agrees with itself about. Nothing here is ever imported by the live pipeline.

EFFICIENCY RATIO (Kaufman). Net directional movement over a window, divided by the
sum of every candle's absolute movement in that window:

    ER = |close[-1] - close[0]| / sum(|close[i] - close[i-1]|)

ER -> 1 means every bar contributed to the net move in the same direction - a clean
trend. ER -> 0 means the bars fought each other to a standstill - chop, whatever the
net move ends up being. It needs no bins, no volume, no profile - just closes - which
is exactly what makes it a genuine second opinion rather than a restatement of
va_range_ratio or poc_position under another name.
"""


def efficiency_ratio(candles):
    """ER over one contiguous run of candles, using each candle's close.

    Returns None for fewer than 2 candles (no movement to measure) and 0.0 when
    total movement is zero (a session that never printed a different close is
    reported as "no efficiency", not "perfectly efficient" - division by zero is
    the wrong reason to claim a trend).
    """
    if candles is None or len(candles) < 2:
        return None

    closes = [float(candle.close) for candle in candles]
    net_move = abs(closes[-1] - closes[0])
    total_move = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))

    if total_move <= 0:
        return 0.0
    return net_move / total_move
