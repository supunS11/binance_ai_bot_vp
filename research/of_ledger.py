"""Order-flow confirmation at a zone as an ACCUMULATED READ of the interaction - research only,
shared with the live S6-WF setup (setups/workflow.py), so live and research run the same rules. Design and sources: calibration/workflow_v2/orderflow_redesign.md.

Observations (directly from executed trades, validated footprints): per 1-minute footprint,
aggressive buy and sell volume per price row, and the minute's traded prices. Nothing is read
from candle shapes: no close-location, no bar patterns. Every decision accumulates the minutes
since the touch.

One boundary is used for every "result" judgement: abnormally low price response per unit of
aggression = below the 1-UNUSUAL quantile (absorption); otherwise the aggression had a result.

Reference ("normal" for this coin now): the REF_MIN minutes before the touch. For a window of
L minutes, the distribution over all reference windows of that length of
    net  = aggressive buys - aggressive sells   (delta)
    dp   = price change (close at window end - close just before it)
"Unusual" = beyond the UNUSUAL quantile of that distribution; "normal" = the median.

Stages (BUY at support; SELL is the exact mirror through the oriented flow):
  ATTEMPT     selling since the touch (-net) >= the unusual selling for that window length
  ABSORPTION  (inferred) AT THE LEVEL: during an attempt, with most (>= AT_LEVEL_SHARE) of the
              aggressive selling since the touch executed at prices inside or below the zone, the price response PER UNIT of net selling since
              the touch (dp / net) is below the 1-UNUSUAL quantile of the same ratio in reference
              windows with unusual selling: price fell less per unit sold than in 90% of
              comparable windows (Cont-Kukanov-Stoikov: impact per unit of order-flow imbalance
              is set by resting liquidity, so abnormally low impact = abnormal passive buying)
  EXHAUSTION  (inferred) after an attempt, a 2nd+ new interaction low AT the level (reaching into
              the zone) made in a minute with
              below-median aggressive selling, with CVD since the touch higher than at the
              previous new low
  A failure reading is cancelled by a later new interaction low without a new reading.
  RESPONSE    after a failure reading, from the interaction low: buying >= the unusual buying
              for that window length, price rise per unit bought NOT abnormally low (>= the
              1-UNUSUAL quantile of reference windows with unusual buying: the buying is not
              absorbed - effort WITH result), and a close above the zone -> CONFIRM.
  VETO        10 consecutive closes below the zone with net selling (acceptance), or price more
              than one zone width below the zone while the attempt's price change is at or below
              the median for unusual selling (sellers move price freely).
Supporting evidence recorded at a CONFIRM (not required): stacked buying imbalances in the
footprint aggregated over the response window; the share of the attack's selling executed in
the interaction's lowest STACK_ROWS rows.

CAUSALITY: only minutes <= the current one are read; the reference ends before the touch.
"""
import numpy as np

from research.of_rules import IMB_RATIO, STACK_ROWS
from research.wf_orderflow import stack_origin

REF_MIN = 1440
WINDOW_MIN = 60
UNUSUAL = 0.90
ACCEPT_MIN = 10
MIN_REF_WINDOWS = 100
AT_LEVEL_SHARE = 0.5           # absorption: most of the selling must have hit the level


class Reference:
    """Window statistics of the REF_MIN oriented minutes before the touch (minutes a..t0-1)."""

    def __init__(self, flow, t0):
        a = max(0, t0 - REF_MIN)
        self.net = (flow.min_with[a:t0] - flow.min_against[a:t0]).astype(float)
        self.sell = flow.min_against[a:t0].astype(float)
        before = [flow.c[a - 1]] if a > 0 else [np.nan]
        self.cref = np.concatenate([before, np.asarray(flow.c[a:t0], dtype=float)])
        self._win, self._sell, self._buy = {}, {}, {}
        traded = self.sell[self.sell > 0]
        self.median_sell = float(np.median(traded)) if traded.size else 0.0
        sel = (flow.minute >= a) & (flow.minute < t0)
        cells = np.concatenate([flow.against[sel][flow.against[sel] > 0],
                                flow.with_[sel][flow.with_[sel] > 0]])
        self.avg_cell = float(cells.mean()) if cells.size else 0.0

    def windows(self, L):
        """(net, dp) over every reference window of L minutes: net = summed delta of its
        minutes, dp = close at its last minute - close just before its first minute."""
        if L not in self._win:
            n = len(self.net)
            if n < L:
                self._win[L] = (np.array([]), np.array([]))
            else:
                cs = np.concatenate([[0.0], np.cumsum(self.net)])
                s = np.arange(0, n - L + 1)
                net = cs[s + L] - cs[s]
                dp = self.cref[s + L] - self.cref[s]
                ok = np.isfinite(dp)
                self._win[L] = (net[ok], dp[ok])
        return self._win[L]

    def selling(self, L):
        """(unusual selling threshold, price response per unit of net selling - dp / net,
        positive when price fell - over the reference windows with unusual selling)."""
        if L not in self._sell:
            net, dp = self.windows(L)
            if net.size < MIN_REF_WINDOWS:
                self._sell[L] = None
            else:
                thr = max(float(np.quantile(-net, UNUSUAL)), 0.0)
                k = (-net >= thr) & (net < 0)
                self._sell[L] = (thr, dp[k] / net[k])           # price response per unit sold
        return self._sell[L]

    def buying(self, L):
        if L not in self._buy:
            net, dp = self.windows(L)
            if net.size < MIN_REF_WINDOWS:
                self._buy[L] = None
            else:
                thr = max(float(np.quantile(net, UNUSUAL)), 0.0)
                k = (net >= thr) & (net > 0)
                self._buy[L] = (thr, dp[k] / net[k])            # price response per unit bought
        return self._buy[L]


def evaluate(flow, zone_low, zone_high, touch_minute, ref=None):
    """Read one interaction. flow: an oriented flow with per-minute arrays (of_rules.DayFlow or
    wf_orderflow.TwoDayFlow; touch_minute in its minute index). Returns a dict:
      outcome   CONFIRM / VETO / NO_SIGNAL / NO_DATA
      minute    decision minute (end of the deciding minute), or None
      path      ABS / EXH (CONFIRM), ACCEPT / BREAK (VETO)
      stages    {attempt, absorption, exhaustion, failure, response}: first minute each was read
      evidence  numbers behind the decision (see module docstring)
    """
    zl, zh = flow.zone(zone_low, zone_high)
    width = zh - zl
    t0 = touch_minute
    ref = ref or Reference(flow, t0)
    out = {"outcome": "NO_DATA", "minute": None, "path": None,
           "stages": {}, "evidence": {}}
    if ref.median_sell <= 0 or len(ref.net) < REF_MIN // 2 or not np.isfinite(flow.c[t0 - 1]):
        return out
    p0 = float(flow.c[t0 - 1])
    stages = {}
    cum = 0.0
    low, low_m = np.inf, None
    pushes = []                      # (minute, cvd at that new low)
    attempt = False
    failure = None                   # (minute, path)
    below_run, below_net = 0, 0.0
    # aggressive selling executed AT the level (rows whose price is inside or below the zone)
    at = (flow.row + 0.5) * flow.row_size <= zh
    at_level = np.bincount(flow.minute[at], weights=flow.against[at], minlength=len(flow.c))
    sold = sold_at = 0.0
    last = min(t0 + WINDOW_MIN, len(flow.c))
    for t in range(t0, last):
        if not np.isfinite(flow.c[t]):
            continue
        n_t = float(flow.min_with[t] - flow.min_against[t])
        cum += n_t
        L = t - t0 + 1
        dp = float(flow.c[t]) - p0
        new_low = flow.l[t] < low
        if new_low:
            low, low_m = float(flow.l[t]), t
        sel = ref.selling(L)
        if sel is None:
            return out
        sell_thr, sell_r = sel
        r_t = dp / cum if cum < 0 else None          # this interaction's response per unit sold
        # ---- VETO
        if flow.c[t] < zl:
            below_run += 1
            below_net += n_t
        else:
            below_run, below_net = 0, 0.0
        if below_run >= ACCEPT_MIN and below_net <= 0:
            return _done(out, "VETO", t, "ACCEPT", stages, {"cvd": cum})
        if low < zl - width and r_t is not None and sell_r.size >= 20                 and r_t >= float(np.quantile(sell_r, 1 - UNUSUAL)):
            return _done(out, "VETO", t, "BREAK", stages, {"cvd": cum, "dp": dp})
        # ---- ATTEMPT
        if -cum >= sell_thr and cum < 0:
            attempt = True
            stages.setdefault("attempt", t)
        # ---- FAILURE readings
        if failure is not None and new_low:
            failure = None                                  # the defence broke
        sold += float(flow.min_against[t])
        sold_at += float(at_level[t])
        at_share = sold_at / sold if sold > 0 else 0.0
        if attempt and -cum >= sell_thr and r_t is not None and sell_r.size >= 20                 and at_share >= AT_LEVEL_SHARE and r_t < float(np.quantile(sell_r, 1 - UNUSUAL)):
            failure = failure or (t, "ABS")
            stages.setdefault("absorption", t)
        if new_low:
            if attempt and pushes and flow.l[t] <= zh and flow.min_against[t] <= ref.median_sell                     and cum > pushes[-1][1]:
                failure = (t, "EXH")
                stages.setdefault("exhaustion", t)
            pushes.append((t, cum))
        if failure is not None:
            stages.setdefault("failure", failure[0])
        # ---- RESPONSE
        if failure is not None and t > failure[0] and t > low_m:
            L2 = t - low_m
            buy = ref.buying(L2)
            if buy is None:
                continue
            buy_thr, buy_r = buy
            net_up = float(np.sum(flow.min_with[low_m + 1:t + 1] - flow.min_against[low_m + 1:t + 1]))
            dp_up = float(flow.c[t]) - float(flow.c[low_m])
            if net_up >= buy_thr and net_up > 0 and buy_r.size >= 20 \
                    and dp_up / net_up >= float(np.quantile(buy_r, 1 - UNUSUAL)) and flow.c[t] > zh:
                stages["response"] = t
                ev = {"cvd": cum, "net_up": net_up, "buy_thr": buy_thr, "dp_up": dp_up,
                      "low": flow.price(low), "low_minute": low_m}
                ev.update(_support(flow, ref, t0, failure[0], low_m, t, low))
                return _done(out, "CONFIRM", t, failure[1], stages, ev)
    out.update(outcome="NO_SIGNAL", stages=stages)
    return out


def _support(flow, ref, t0, tf, low_m, t, low):
    """Supporting evidence: stacked buying imbalances in the footprint aggregated over the
    response window (low_m, t]; share of the attack's selling in the lowest STACK_ROWS rows."""
    sel = (flow.minute > low_m) & (flow.minute <= t)
    cells = {}
    for r, a, w in zip(flow.row[sel], flow.against[sel], flow.with_[sel]):
        x = cells.get(int(r), (0.0, 0.0))
        cells[int(r)] = (x[0] + a, x[1] + w)
    stack = stack_origin(cells, ref.avg_cell) is not None
    att = (flow.minute >= t0) & (flow.minute <= tf)
    rows = flow.row[att]
    vol = flow.against[att]
    share = None
    if vol.sum() > 0:
        low_row = int(np.floor(low / flow.row_size + 1e-9))
        share = float(vol[rows <= low_row + STACK_ROWS - 1].sum() / vol.sum())
    return {"stack": int(stack), "low_rows_share": share}


def _done(out, outcome, t, path, stages, ev):
    out.update(outcome=outcome, minute=t, path=path, stages=stages, evidence=ev)
    return out
