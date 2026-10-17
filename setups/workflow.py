"""S6-WF: the context-aware volume-profile + order-flow workflow, live (WF_ENABLED).

Design and pre-registration: calibration/workflow_v2/design_plan_DRAFT.md; order-flow process:
calibration/workflow_v2/orderflow_redesign.md. Every decision below is made by the SAME research
functions the study measures (research/wf_state, wf_levels, impulse_leg, of_ledger, wf_trade),
called on closed 1m candles and recorded trades only - this module adds the live data plumbing,
the minute-by-minute bookkeeping and the order candidate, never a rule of its own.

ONCE PER SYMBOL PER UTC SESSION
  1 state   Turtle 55/20 on daily bars AND the 10-day composite value migration must agree:
            TREND_UP / TREND_DOWN / BALANCE; anything else is UNCLEAR -> no trade today.
            A thin prior day against the trend is a veto day -> no trend trades today.
    middle  the last 3 sessions' value migration (MID_UP / MID_DOWN / MID_ROTATION) against the
            trend: ALIGNED / RANGE / CONFLICT (playbook 5A). With WF_MTF_ALIGNMENT on, a
            CONFLICT day has no trend trades and a RANGE day trades only the with-trend
            composite lower edge (FC); off (default), it is journaled with every trade.
    profiles  the prior session (VAH / VAL / POC); the 10-session COMPOSITE (cVAH / cVAL / cPOC
            / HVNs; research/wf_composite); anchored impulse-leg profiles (LVN / POC).
EVERY CLOSED MINUTE
  2 where   TREND: T1 (prior VAH/VAL retest after 2 x 30m acceptance beyond it) and T2/T3 (the
            latest impulse leg's LVN / POC). BALANCE: X (breakout -> retest of the old edge, or
            of the breakout leg's LVN / POC), F (fade of the prior value edge before any
            acceptance), FC (fade of the composite value edge before any acceptance outside
            it) and R (80% rule). One look per level: its first touch from the approach side.
            Each trade records whether its zone coincides with a composite level (confluence)
            and where today's developing POC sat at the touch.
  3 order flow  from the touch, the recorded trades are read minute by minute for up to 60
            minutes (of_ledger): sellers attack (unusual selling) -> the attack fails
            (absorption or exhaustion, AT the level) -> buyers respond (unusual buying that moves
            price, back above the zone) = CONFIRM; acceptance below / a free break = VETO.
  4 entry   market, right after the confirming minute closes (wf_trade entry M). R: market after
            the second 30m close back inside value.
  5 exits   stop beyond the interaction low (R: the session extreme outside value) + 0.2 x 15m
            ATR; one target: path T / X the nearest reference at >= 2R (prior-day and composite
            levels, naked POCs, 15m swings), F / R the opposite prior value edge, FC the
            opposite composite value edge. Skipped when the target is under 2R or fees exceed
            1/3 of the risk.

DATA THE LIVE RULES NEED, AND WHAT HAPPENS WITHOUT IT
  - order flow reads ops/feed_recorder.py's trade files (FEED_OUT_DIR). The recorder must run
    for the same symbols. Every minute is checked against the 1m kline (data/footprint_live);
    an untrusted minute inside the interaction, or more than REF_INVALID_MAX in the 24-hour
    reference, ends the read as NO_DATA - a gap is never read as a quiet market. So the first
    trades are possible ~24 hours after the recorder starts (impulse-leg levels need the leg's
    days recorded too).
  - daily state needs ~1500 daily candles (one REST call per symbol per day) and the 20
    completed days' value areas (computed once from 1m candles, cached under WF_CACHE_DIR).
FRESHNESS. A confirmation is acted on only in the cycle right after it (FRESH_MIN minutes of
tolerance for the recorder's flush); one found later - a restart, a slow cycle - is recorded
as STALE and not traded, because the research entry is the next minute's open.
Nothing here sends an order: candidates go through main.py's try_open (gates, sizing,
observation mode) like every other setup's.
"""
import bisect
import json
import logging
import os
from dataclasses import dataclass, field

import config
import sessions
from data import footprint_live as fpl
from data import klines as klines_mod
from research import footprint as fp_research
from research import impulse_leg as il
from research import of_ledger
from research import wf_composite as wc
from research import wf_levels as wl
from research import wf_orderflow as wo
from research import wf_state as ws
from research import wf_trade as wt
from setups.base import MARKET_ENTRY, Candidate, RejectReason, SetupState, reject

log = logging.getLogger(__name__)

SETUP = "S6-WF"
LAST_TOUCH_MIN = 1440 - 1 - of_ledger.WINDOW_MIN       # the 60-minute read must end in the day
LEG_HISTORY_DAYS = 10
TARGET_LOOKBACK_DAYS = 10
DAILY_HISTORY = 1500
VALUE_DAYS = 2 * ws.VALUE_N
REF_INVALID_MAX = 15            # untrusted minutes allowed in the 1440-minute reference (~1%)
LEG_INVALID_SHARE = 0.02        # untrusted share allowed in a leg day's footprint
RECORDER_LAG_MIN = 3            # recorder minutes this far behind the klines count as missing
FRESH_MIN = 1
CONTEXT_RETRY_MS = 10 * 60_000
PLAN_ATR_MIN_BARS = 15          # setups.orderflow_reversal.PLAN_ATR_MIN_BARS

SKIP_REASONS = {"stop_side": RejectReason.GEOMETRY_INVALID,
                "stop_too_tight": RejectReason.STOP_TOO_TIGHT,
                "no_t2": RejectReason.REWARD_BELOW_MINIMUM,
                "t2_under_2r": RejectReason.REWARD_BELOW_MINIMUM}


# ------------------------------------------------------------------ data

class LiveData:
    """Market data for the workflow from the exchange, the kline cache and the recorder."""

    def __init__(self, rest, cache, recorder_root=None, cache_dir=None):
        self._rest = rest
        self._cache = cache
        self.recorder = fpl.RecorderTrades(recorder_root or config.FEED_OUT_DIR)
        self._dir = cache_dir or config.WF_CACHE_DIR
        self._daily = {}            # symbol -> (session_id, candles)
        self._values = {}           # symbol -> {day: row}

    def daily(self, symbol, session_start):
        """Closed daily candles before session_start (up to DAILY_HISTORY)."""
        sid = sessions.session_id(session_start)
        hit = self._daily.get(symbol)
        if hit and hit[0] == sid:
            return hit[1]
        start = session_start - DAILY_HISTORY * sessions.MS_DAY
        candles = [c for c in klines_mod.fetch(self._rest, symbol, "1d", start, session_start)
                   if c.close_time < session_start]
        self._daily[symbol] = (sid, candles)
        return candles

    def minute_day(self, symbol, sid):
        """1m candles of a completed session (persisted by the kline cache, else fetched and
        persisted)."""
        got = self._cache.load_session(symbol, sid)
        if got:
            return got
        start = sessions.session_start_ms_from_id(sid)
        got = klines_mod.fetch(self._rest, symbol, "1m", start, start + sessions.MS_DAY)
        if got and got[-1].close_time >= start + sessions.MS_DAY - 1:
            self._cache.persist_session(symbol, sid, got)
        return got

    def hist15(self, symbol, start_ms, end_ms):
        return klines_mod.fetch(self._rest, symbol, "15m", start_ms, end_ms)

    def values(self, symbol, sid, daily, tick):
        """Value rows of the VALUE_DAYS calendar days before sid (missing days are skipped, so
        value_before() sees the hole), oldest first. Cached on disk: a closed day never
        changes."""
        rows = self._values.get(symbol)
        if rows is None:
            rows = self._load_values(symbol)
            self._values[symbol] = rows
        start = sessions.session_start_ms_from_id(sid)
        changed = False
        out = []
        for k in range(VALUE_DAYS, 0, -1):
            day = sessions.session_id(start - k * sessions.MS_DAY)
            if day not in rows:
                candles = self.minute_day(symbol, day)
                if not candles:
                    continue                    # fetch failed: not cached, retried next session
                rows[day] = ws.day_value_row(symbol, day, candles, daily, tick)
                changed = True
            if rows[day] is not None:
                out.append(rows[day])
        if changed:
            self._save_values(symbol, rows)
        return out

    def _values_path(self, symbol):
        return os.path.join(self._dir, "values", f"{symbol.upper()}.json")

    def _load_values(self, symbol):
        try:
            with open(self._values_path(symbol), encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def _save_values(self, symbol, rows):
        path = self._values_path(symbol)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            keep = dict(sorted(rows.items())[-3 * VALUE_DAYS:])
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(keep, handle)
        except OSError as exc:
            log.warning("%s value cache not written: %s", symbol, exc)


# ------------------------------------------------------------------ per-session context

@dataclass
class _Day:
    symbol: str
    sid: str
    start: int
    state: str
    turtle: object
    value: object
    veto: bool
    shape: object
    levels: object
    prof: object
    atr: float
    tick: float
    daily: list
    prior_values: list
    hist15: list
    rs_today: float
    rs_prev: float
    comp: object = None                       # the 10-session composite (wf_composite)
    mid: object = None                        # middle-timeframe label (last 3 sessions)
    align: str = ""                           # 1D state vs mid (playbook 5A)
    armed: tuple = (None, [])                 # (number of session 15m bars, leg_armed list)
    breakout: tuple = (None, [])              # (number of session 15m bars, breakout_leg_armed)
    closed: list = field(default_factory=list)  # the session's closed 1m candles (this cycle)
    refs: dict = field(default_factory=dict)
    reading: set = field(default_factory=set)

    @property
    def trades_today(self):
        if self.state == ws.BALANCE:
            return True
        if self.state not in (ws.TREND_UP, ws.TREND_DOWN) or self.veto:
            return False
        return not config.WF_MTF_ALIGNMENT or self.align in (wc.ALIGNED, wc.RANGE)

    @property
    def trend_paths(self):
        """T1 / T2 / T3 allowed: trend day, and (with WF_MTF_ALIGNMENT) the middle timeframe
        agrees. In a RANGE alignment only the with-trend composite lower edge (FC) trades."""
        if self.state not in (ws.TREND_UP, ws.TREND_DOWN) or self.veto:
            return False
        return not config.WF_MTF_ALIGNMENT or self.align == wc.ALIGNED


def watch_key(w):
    return f"{w['path']}|{w['kind']}|{w['side']}|{float(w['level']):.10g}"


def atr15(closed, daily_atr):
    """research wf_run.Ctx.atr15: ATR of the session's closed 15m bars, else 0.1 daily ATR."""
    bars = klines_mod.resample(closed, "15m", "1m")
    a = klines_mod.atr(bars) if len(bars) > PLAN_ATR_MIN_BARS else 0.0
    return a if a > 0 else 0.1 * daily_atr


class Workflow:
    """Runs the workflow for every symbol the bot scans. observe(ctx) -> (candidate, rejections)."""

    def __init__(self, data, store=None):
        self._data = data
        self._store = store
        self._days = {}             # symbol -> _Day
        self._done = {}             # symbol -> (session_id, {watch key: outcome})
        self._failed = {}           # symbol -> (session_id, retry not before ms)

    # ---------------------------------------------------------------- persistence

    def restore(self):
        if self._store is None:
            return
        for symbol, payload in self._store.load_workflow_state().items():
            try:
                data = json.loads(payload)
                self._done[symbol] = (data["session_id"], dict(data["done"]))
            except (ValueError, KeyError):
                continue
        if self._done:
            log.info("restored workflow state for %d symbol(s)", len(self._done))

    def _finish(self, symbol, sid, key, outcome):
        done = self._done.setdefault(symbol, (sid, {}))[1]
        done[key] = outcome
        if self._store is not None:
            self._store.save_workflow_state(symbol, json.dumps({"session_id": sid, "done": done}))

    def _done_for(self, symbol, sid):
        hit = self._done.get(symbol)
        if hit is None or hit[0] != sid:
            hit = (sid, {})
            self._done[symbol] = hit
        return hit[1]

    # ---------------------------------------------------------------- main step

    def observe(self, ctx):
        if ctx.prior_levels is None or ctx.prior_profile is None or ctx.atr <= 0:
            return None, []
        closed = [c for c in ctx.session_candles if c.close_time <= ctx.as_of]
        if len(closed) < 2:
            return None, []
        day, why = self._day(ctx)
        if day is None:
            return None, ([reject(RejectReason.NO_DATA, SETUP, ctx.symbol, detail=why)]
                          if why else [])
        if not day.trades_today:
            return None, []
        day.closed = closed
        done = self._done_for(ctx.symbol, day.sid)
        rejections = []
        candidate = self._eighty(ctx, day, closed, done, rejections)
        watches = [w for w in self._watches(day, closed) if watch_key(w) not in done]
        if watches:
            found = self._read(ctx, day, closed, watches, rejections)
            candidate = candidate or found
        return candidate, rejections

    def _day(self, ctx):
        """The session's fixed context, built once. (None, reason) when the state cannot be
        computed - (None, None) for a reason already reported this session."""
        day = self._days.get(ctx.symbol)
        if day is not None and day.sid == ctx.session_id:
            return day, None
        failed = self._failed.get(ctx.symbol)
        if failed and failed[0] == ctx.session_id and ctx.as_of < failed[1]:
            return None, None
        start = sessions.session_start_ms_from_id(ctx.session_id)
        tick = float(getattr(ctx.spec, "tick_size", 0) or 0)
        try:
            daily = self._data.daily(ctx.symbol, start)
            prior = self._data.values(ctx.symbol, ctx.session_id, daily, tick)
            hist15 = self._data.hist15(ctx.symbol, start - LEG_HISTORY_DAYS * sessions.MS_DAY, start)
        except Exception as exc:                  # noqa: BLE001
            log.warning("%s workflow context failed: %s", ctx.symbol, exc)
            self._failed[ctx.symbol] = (ctx.session_id, ctx.as_of + CONTEXT_RETRY_MS)
            return None, f"workflow context: {exc}"
        h = [c.high for c in daily]
        lo = [c.low for c in daily]
        cl = [c.close for c in daily]
        state, turtle, value = ws.state_before(h, lo, cl, prior, ctx.session_id)
        shape = ws.prior_shape_label(ctx.prior_profile, ctx.prior_levels)
        rs_today = fp_research.row_size(daily, start, tick)
        rs_prev = fp_research.row_size(daily, start - sessions.MS_DAY, tick)
        try:
            comp_candles = [c for k in range(wc.COMPOSITE_SESSIONS, 0, -1)
                            for c in self._data.minute_day(
                                ctx.symbol, sessions.session_id(start - k * sessions.MS_DAY))]
            comp = wc.composite(ctx.symbol, comp_candles, start, ctx.prior_profile.bin_size,
                                ctx.atr)
        except Exception as exc:                  # noqa: BLE001
            log.warning("%s composite profile failed: %s", ctx.symbol, exc)
            comp = None
        mid = wc.mid_label(prior, ctx.session_id)
        day = _Day(symbol=ctx.symbol, sid=ctx.session_id, start=start, state=state,
                   turtle=turtle, value=value, veto=ws.veto_day(state, shape), shape=shape,
                   levels=ctx.prior_levels, prof=ctx.prior_profile, atr=ctx.atr, tick=tick,
                   daily=daily, prior_values=prior, hist15=[b for b in hist15 if b.close_time < start],
                   rs_today=rs_today or 0.0, rs_prev=rs_prev or 0.0, comp=comp, mid=mid,
                   align=wc.alignment(state, mid))
        self._days[ctx.symbol] = day
        self._data.recorder.drop_before(sessions.session_id(start - sessions.MS_DAY))
        log.info("%s workflow day %s: state %s (turtle %s, value %s) mid %s %s prior shape %s%s "
                 "composite %s", ctx.symbol, day.sid, state, turtle, value, mid, day.align, shape,
                 " VETO" if day.veto else "",
                 "none" if comp is None else f"VAL {comp.val:.8g} POC {comp.poc:.8g} VAH {comp.vah:.8g}")
        return day, None

    # ---------------------------------------------------------------- stage 2: where

    def _watches(self, day, closed):
        """Every level touched so far today that this day's rules allow (research wf_run builds
        the same lists): prior value (T1 / X / F), impulse-leg LVN / POC (T2 / T3), breakout-leg
        LVN / POC (X), composite edges (FC)."""
        b30 = wl.bars30(closed)
        session15 = klines_mod.resample(closed, "15m", "1m")

        def bins(t0, t1):
            return self._bins(day, t0, t1)

        out = []
        if day.state == ws.BALANCE or day.trend_paths:
            out += wl.value_watches(day.state, False, day.levels, day.atr, closed, b30)
        if day.trend_paths:
            if day.armed[0] != len(session15):
                armed, _ = wl.leg_armed(day.state, day.atr, day.hist15 + session15, day.start,
                                        closed[-1].close_time + 1, bins)
                day.armed = (len(session15), armed)
            side = "BUY" if day.state == ws.TREND_UP else "SELL"
            out += wl.leg_touches(day.armed[1], closed, side)
        if day.state == ws.BALANCE:
            if day.breakout[0] != len(session15):
                day.breakout = (len(session15), wl.breakout_leg_armed(
                    day.state, day.levels, day.atr, day.hist15 + session15, closed, b30, bins))
            out += wl.breakout_leg_touches(day.breakout[1], closed)
        sides = wc.fc_sides(day.state, day.align)
        if day.state != ws.BALANCE and not config.WF_MTF_ALIGNMENT:
            sides = ()                            # FC on a trend day is the alignment rule only
        out += wl.composite_watches(day.state, day.comp, day.atr, closed, b30, sides)
        return wl.drop_duplicate_fc(out)

    def _bins(self, day, t0, t1):
        return il.footprint_bins(lambda s, sid: self._leg_day(day, sid), day.symbol, t0, t1)

    def _leg_day(self, day, sid):
        """One day's footprint for an impulse-leg profile, or None when it is not trusted."""
        rec = self._data.recorder.day(day.symbol, sid)
        if rec is None:
            return None
        start = sessions.session_start_ms_from_id(sid)
        rs = fp_research.row_size(day.daily, start, day.tick) if start < day.start else day.rs_today
        if not rs:
            return None
        minutes, gaps = rec
        if start < day.start:
            candles, upto = self._data.minute_day(day.symbol, sid), fpl.DAY_MIN - 1
        else:
            candles = day.closed
            upto = min(max(minutes) if minutes else -1,
                       (candles[-1].open_time - start) // 60_000 if candles else -1)
        if upto < 0:
            return None
        bad = fpl.invalid_minutes(minutes, gaps, candles, start, upto)
        if len(bad) > LEG_INVALID_SHARE * (upto + 1):
            return None
        return fpl.day_raw(minutes, rs, upto)

    # ---------------------------------------------------------------- stage 3: order flow

    def _read(self, ctx, day, closed, watches, rejections):
        avail = (closed[-1].open_time - day.start) // 60_000
        flows, bad, upto, why = self._flows(ctx, day, closed, avail)
        candidate = None
        for w in watches:
            key = watch_key(w)
            t0 = (closed[w["i"]].open_time - day.start) // 60_000
            if t0 > LAST_TOUCH_MIN:
                self._finish(ctx.symbol, day.sid, key, "WINDOW")
                continue
            if key not in day.reading:
                day.reading.add(key)
                log.info("%s %s %s %s level %.8g touched at minute %d: reading order flow",
                         ctx.symbol, w["path"], w["side"], w["kind"], w["level"], t0)
            if flows is None:
                self._end(ctx, day, key, w, "NO_DATA", why, rejections)
                continue
            gap = self._gap(bad, t0, upto)
            if gap:
                self._end(ctx, day, key, w, "NO_DATA", gap, rejections)
                continue
            flow = flows[w["side"]]
            T = t0 + wo.DAY
            ref = day.refs.get(key) or of_ledger.Reference(flow, T)
            day.refs[key] = ref
            res = of_ledger.evaluate(flow, w["zone"][0], w["zone"][1], T, ref=ref)
            outcome = res["outcome"]
            if outcome == "NO_SIGNAL":
                if upto >= t0 + of_ledger.WINDOW_MIN - 1:
                    self._end(ctx, day, key, w, "NO_SIGNAL", "", rejections)
                continue
            if outcome != "CONFIRM":
                self._end(ctx, day, key, w, f"{outcome}_{res['path'] or ''}".rstrip("_"), "",
                          rejections)
                continue
            m = res["minute"] - wo.DAY
            if avail - m > FRESH_MIN or candidate is not None:
                self._end(ctx, day, key, w, "STALE" if candidate is None else "SECOND_CONFIRM",
                          f"confirmed at minute {m}, now {avail}", rejections)
                continue
            self._finish(ctx.symbol, day.sid, key, "CONFIRM_" + res["path"])
            candidate = self._order(ctx, day, closed, w, res, m, rejections)
        return candidate

    def _flows(self, ctx, day, closed, avail):
        """({side: TwoDayFlow}, untrusted minute set in two-day indexes, last readable minute,
        reason) - flows None with the reason when the day cannot be read at all."""
        if not day.rs_today or not day.rs_prev:
            return None, set(), -1, "no footprint row size (daily history)"
        rec = self._data.recorder.day(ctx.symbol, day.sid)
        if rec is None:
            return None, set(), -1, "no recorder file for today (is ops.feed_recorder running?)"
        minutes, gaps = rec
        last_rec = max(minutes) if minutes else -1
        upto = min(avail, last_rec) if last_rec >= avail - RECORDER_LAG_MIN else avail
        bad = {m + wo.DAY for m in fpl.invalid_minutes(minutes, gaps, closed, day.start, upto)}
        prev_sid = sessions.session_id(day.start - sessions.MS_DAY)
        prev = self._data.recorder.day(ctx.symbol, prev_sid)
        prev_start = day.start - sessions.MS_DAY
        if prev is None:
            prev_raw, prev_ohlc = None, None
            bad.update(range(wo.DAY))
        else:
            prev_candles = self._data.minute_day(ctx.symbol, prev_sid)
            bad.update(fpl.invalid_minutes(prev[0], prev[1], prev_candles, prev_start, wo.DAY - 1))
            prev_raw = fpl.day_raw(prev[0], day.rs_prev)
            prev_ohlc = fpl.ohlc_arrays(prev_candles, prev_start)
        raw = fpl.day_raw(minutes, day.rs_today, upto)
        ohlc = fpl.ohlc_arrays(closed, day.start, upto)
        flows = {s: wo.TwoDayFlow(prev_raw, prev_ohlc, raw, ohlc, s) for s in ("BUY", "SELL")}
        return flows, bad, upto, ""

    @staticmethod
    def _gap(bad, t0, upto):
        T = t0 + wo.DAY
        ref_bad = sum(1 for m in bad if T - of_ledger.REF_MIN <= m < T)
        if ref_bad > REF_INVALID_MAX:
            return f"{ref_bad} untrusted minutes in the 24h reference"
        inside = [m - wo.DAY for m in bad if T <= m <= upto + wo.DAY and m < T + of_ledger.WINDOW_MIN]
        if inside:
            return f"untrusted recorder minute {inside[0]} inside the interaction"
        return ""

    def _end(self, ctx, day, key, w, outcome, detail, rejections):
        self._finish(ctx.symbol, day.sid, key, outcome)
        log.info("%s %s %s %s level %.8g: %s %s", ctx.symbol, w["path"], w["side"], w["kind"],
                 w["level"], outcome, detail)
        if outcome in ("NO_DATA", "STALE"):
            rejections.append(reject(RejectReason.NO_DATA if outcome == "NO_DATA" else
                                     RejectReason.NOT_CONFIRMED, SETUP, ctx.symbol,
                                     direction=w["side"], level_price=w["level"],
                                     detail=f"{w['path']} {outcome}: {detail}"))

    # ---------------------------------------------------------------- stages 4-5

    def _order(self, ctx, day, closed, w, res, m, rejections):
        i_dec = next(k for k in range(len(closed) - 1, -1, -1)
                     if (closed[k].open_time - day.start) // 60_000 == m)
        side, path = w["side"], w["path"]
        upto = closed[:i_dec + 1]
        span = closed[w["i"]:i_dec + 1]
        extreme = min(c.low for c in span) if side == "BUY" else max(c.high for c in span)
        a15 = atr15(upto, day.atr)
        stop = wt.stop_price(side, extreme, a15)
        entry = closed[i_dec].close
        t1, t2 = self._targets(day, path, side, entry, abs(entry - stop), upto)
        stages = res["stages"]
        T = (closed[w["i"]].open_time - day.start) // 60_000 + wo.DAY
        attrs = {"wf_of_path": res["path"],
                 "wf_touch_min": T - wo.DAY, "wf_decision_min": m,
                 **{f"wf_{k}_dt": v - T for k, v in stages.items()},
                 "wf_conf": wc.watch_confluence(w, day.comp, day.atr),
                 "wf_dpoc": wc.dpoc_position(ctx.symbol, closed[:w["i"] + 1], day.start,
                                             ctx.prior_profile.bin_size, day.levels),
                 "wf_stack": res["evidence"].get("stack"),
                 "wf_low_rows_share": res["evidence"].get("low_rows_share")}
        return self._candidate(ctx, day, w, side, path, entry, stop, t1, t2, a15, attrs,
                               rejections)

    def _targets(self, day, path, side, entry, risk, upto):
        lv = day.levels
        if path == "FC":
            return wc.value_watch_targets(day.comp, side)
        if path in ("F", "R"):
            return lv.poc_price, (lv.vah if side == "BUY" else lv.val)
        now = upto[-1].close_time
        b15 = day.hist15 + klines_mod.resample(upto, "15m", "1m")
        hist = [b for b in b15 if b.close_time < now
                and b.open_time >= day.start - TARGET_LOOKBACK_DAYS * sessions.MS_DAY]
        cands = wt.target_candidates(lv, day.prof, day.prior_values[-TARGET_LOOKBACK_DAYS:],
                                     min(c.low for c in upto), max(c.high for c in upto),
                                     hist, day.atr, wc.composite_levels(day.comp))
        return wt.pick_targets(side, entry, risk, cands)

    def _eighty(self, ctx, day, closed, done, rejections):
        """Path R (BALANCE): the 80% rule's signal, acted on in the cycle right after it."""
        if day.state != ws.BALANCE or "R" in done:
            return None
        r = wl.eighty_rule(closed, wl.bars30(closed), day.levels.vah, day.levels.val)
        if r is None or r[0] != "R":
            return None
        _, side, t, ext = r
        i_sig = max(k for k, c in enumerate(closed) if c.close_time < t)
        w = {"path": "R", "side": side, "kind": "R", "level": day.levels.poc_price}
        if len(closed) - 1 - i_sig > FRESH_MIN:
            self._end(ctx, day, "R", w, "STALE", f"signal at {closed[i_sig].close_time}", rejections)
            return None
        self._finish(ctx.symbol, day.sid, "R", "SIGNAL")
        upto = closed[:i_sig + 1]
        a15 = atr15(upto, day.atr)
        stop = wt.stop_price(side, ext, a15)
        entry = closed[i_sig].close
        t1, t2 = self._targets(day, "R", side, entry, abs(entry - stop), upto)
        return self._candidate(ctx, day, w, side, "R", entry, stop, t1, t2, a15,
                               {"wf_decision_min": (closed[i_sig].open_time - day.start) // 60_000},
                               rejections)

    def _candidate(self, ctx, day, w, side, path, entry, stop, t1, t2, a15, attrs, rejections):
        skip = wt.plan(side, entry, stop, t1, t2)
        risk = abs(entry - stop)
        if skip:
            rejections.append(reject(SKIP_REASONS[skip], SETUP, ctx.symbol, direction=side,
                                     level_price=w["level"],
                                     detail=f"{path} {w['kind']} confirmed but {skip}"))
            log.info("%s %s %s confirmed but skipped: %s", ctx.symbol, path, side, skip)
            return None
        candidate = Candidate(
            setup=SETUP, symbol=ctx.symbol, direction=side, state=SetupState.TRIGGERED,
            level_price=float(w["level"]), level_kind=w["kind"], entry_price=entry,
            stop_price=stop, target_price=t2, session_id=day.sid, as_of=ctx.as_of,
            atr=day.atr, profile_snapshot=ctx.profile_row(),
            attributes={"entry_mode": MARKET_ENTRY, "wf_path": path, "wf_state": day.state,
                        "wf_turtle": day.turtle, "wf_value": day.value,
                        "wf_prior_shape": day.shape, "target_kind": "T2",
                        "wf_mid": day.mid, "wf_align": day.align,
                        "wf_t1": t1, "wf_t2_r": round(abs(t2 - entry) / risk, 3) if risk else None,
                        "atr15": a15, **attrs})
        log.info("%s S6-WF %s %s %s: entry ~%.8g stop %.8g target %.8g (%.2fR)", ctx.symbol,
                 path, side, w["kind"], entry, stop, t2, abs(t2 - entry) / risk)
        return candidate
