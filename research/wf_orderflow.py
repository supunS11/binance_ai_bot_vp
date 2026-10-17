"""Workflow v2 stage 3: order-flow recipes by path - the TwoDayFlow is
shared with the live S6-WF setup (setups/workflow.py), so live and research run the same rules. Design: calibration/workflow_v2/design_plan_DRAFT.md (Stage 3). Builds on the frozen
research/of_rules.py (DayFlow, Setting, CANCEL rule), which is NOT modified.

Roles (BUY orientation; SELL is the exact mirror through DayFlow):
  DEF   the of_rules HOLD: absorption (ABS) or exhaustion (EXH) after a TEST.
  INI   the of_rules TURN (delta >= D_TURN, vol >= V_TURN x median, close above the
        test-to-hold high, hold low intact, within TURN_WINDOW of the hold) PLUS >= 3 stacked
        BUY imbalances in that bar: consecutive rows where aggressive buying at row r is
        >= 3x aggressive selling at row r-1 (diagonal comparison) and >= the mean cell
        (playbook Step 3 / ATAS: 300% diagonal, 3+ rows = stacked; same thresholds as the
        frozen CANCEL rule, mirrored).
  FA    failed auction (Dalton): price trades BELOW the zone, and the move is NOT accepted -
        a close back at or above the zone low comes before the of_rules acceptance condition
        (ACCEPT_MIN minutes of closes below), and no bar of the poke shows stacked selling
        imbalances below the zone. It is the logical complement of the CANCEL-acceptance rule.
  VETO  the frozen of_rules CANCEL (acceptance or initiative beyond the zone).
  CVD divergence is not used (logged-only role; playbook: "never an entry alone").

Recipes:
  DEF_INI     DEF -> INI                         (path T primary; playbook Fig. 6)
  EXH_INI     exhaustion hold only -> INI         (path T secondary)
  FA_DEF_INI  FA, and DEF -> INI (FA must have happened by the INI bar)   (path F primary)
  DEF_INI is also path F's secondary.
  Path R uses no recipe (the acceptance is the signal); see wf_levels.

Settings: of_rules MEDIUM (R2), 5-minute bars, 60-minute window, the 120-minute reference -
all as approved for the earlier definition; nothing is re-tuned.

CAUSALITY: as of_rules - bars are read only once complete; reference = 120 minutes before the
touch bar; nothing after the decision bar is read.
"""
import numpy as np

from research import of_rules
from research.of_rules import ACCEPT_MIN, IMB_RATIO, STACK_ROWS, WINDOW_MIN, _stacked_below

RECIPES = ("DEF_INI", "EXH_INI", "FA_DEF_INI")
SIZE = 5
SETTING = "MEDIUM"
DAY = 1440


class TwoDayFlow(of_rules.DayFlow):
    """The previous UTC day + the current one as one flow (minutes 0..2879; the current day is
    1440..2879), so a touch early in the session still has its 120-minute reference. The rules
    are identical; only the minute range is longer. Footprint rows are sized per day (from that
    day's ATR), so the previous day's rows are re-binned onto the current day's row grid by
    their centre price. Those cells only enter the REFERENCE statistics: every bar the rules
    decide on lies at or after the touch, inside the current day. prev_raw / prev_ohlc may be
    None (then the first 1440 minutes are empty and the original limits apply)."""

    def __init__(self, prev_raw, prev_ohlc, raw, ohlc, side):
        rs = float(np.ravel(raw["row_size"])[0])
        if prev_raw is not None and len(prev_raw["minute"]):
            rs0 = float(np.ravel(prev_raw["row_size"])[0])
            prow = np.floor((np.asarray(prev_raw["row"], dtype=float) + 0.5) * rs0 / rs).astype(np.int64)
            pm, pb, ps = (np.asarray(prev_raw[k]) for k in ("minute", "buy", "sell"))
        else:
            prow = np.array([], dtype=np.int64)
            pm, pb, ps = np.array([], dtype=np.int64), np.array([]), np.array([])
            prev_ohlc = None
        merged = {"minute": np.concatenate([pm.astype(np.int64), np.asarray(raw["minute"], dtype=np.int64) + DAY]),
                  "row": np.concatenate([prow, np.asarray(raw["row"], dtype=np.int64)]),
                  "buy": np.concatenate([pb, np.asarray(raw["buy"])]),
                  "sell": np.concatenate([ps, np.asarray(raw["sell"])]),
                  "row_size": raw["row_size"]}
        nan = np.full(DAY, np.nan)
        po = prev_ohlc if prev_ohlc is not None else (nan, nan, nan, nan)
        super().__init__(merged, tuple(np.concatenate([np.asarray(x, dtype=float), np.asarray(y, dtype=float)])
                                       for x, y in zip(po, ohlc)), side)
        self.min_against = np.bincount(self.minute, weights=self.against, minlength=2 * DAY)
        self.min_with = np.bincount(self.minute, weights=self.with_, minlength=2 * DAY)

    def bar(self, b, size):
        """of_rules.DayFlow.bar with the longer minute range."""
        a, z = b * size, b * size + size
        if a < 0 or z > 2 * DAY or np.isnan(self.c[a:z]).any():
            return None
        against = float(self.min_against[a:z].sum())
        with_ = float(self.min_with[a:z].sum())
        vol = against + with_
        return {"b": b, "end": z - 1, "o": self.o[a], "h": float(np.max(self.h[a:z])),
                "l": float(np.min(self.l[a:z])), "c": self.c[z - 1], "vol": vol,
                "against": against, "with": with_,
                "dfrac": (with_ - against) / vol if vol > 0 else 0.0}


def _buy_imbalance(cells, r, avg_cell):
    """Row r is a buying imbalance: 'with' volume at r >= IMB_RATIO x 'against' volume at
    row r-1 (diagonal) and >= the mean cell (BUY orientation)."""
    w = cells.get(r, (0.0, 0.0))[1]
    return w >= avg_cell and w >= IMB_RATIO * cells.get(r - 1, (0.0, 0.0))[0]


def stack_origin(cells, avg_cell):
    """Lowest row of the first run of >= STACK_ROWS consecutive (adjacent-row) buying
    imbalances, or None. Rows with no trades break a run."""
    run, first, prev = 0, None, None
    for r in sorted(cells):
        if _buy_imbalance(cells, r, avg_cell):
            if run and r == prev + 1:
                run += 1
            else:
                run, first = 1, r
            if run >= STACK_ROWS:
                return first
        else:
            run = 0
        prev = r
    return None


def stacked_with(cells, avg_cell):
    return stack_origin(cells, avg_cell) is not None


def evaluate(flow, zone_low, zone_high, touch_minute, recipe, size=SIZE, setting=SETTING):
    """One watch, one recipe. Returns dict(outcome CONFIRM / CANCEL / NO_SIGNAL / NO_DATA,
    minute (decision bar end), path ABS/EXH or ACCEPT/INITIATIVE, hold_low (original prices),
    fa (bool), ini_high (original-price extreme of the INI bar on the trade side), stack_row
    (Entry B origin row in ORIGINAL row units, or None))."""
    if recipe not in RECIPES:
        raise ValueError(recipe)
    s = of_rules.SETTINGS[setting]
    zl, zh = flow.zone(zone_low, zone_high)
    tb = touch_minute // size
    none = {"outcome": "NO_DATA", "minute": None, "path": None, "hold_low": None, "fa": False,
            "ini_high": None, "stack_row": None}
    ref = flow.reference(tb, size)
    if ref is None:
        return none
    med, avg_cell = ref
    deadline = touch_minute + WINDOW_MIN
    accept_bars = max(1, -(-ACCEPT_MIN // size))
    beyond = zl - s.cancel_margin * (zh - zl)
    test = hold = None
    run_below, run_delta, run_vol = 0, 0.0, 0.0
    since_test = []
    hold_low = hold_top = hold_bar = None
    fa = False
    poke_open = False            # price is below the zone, not yet back, not yet accepted
    poke_stacked = False
    b = tb
    while True:
        bar = flow.bar(b, size)
        if bar is None or bar["end"] > deadline:
            break
        at_zone = bar["l"] <= zh and bar["h"] >= zl
        rng = bar["h"] - bar["l"]
        upper = bar["c"] >= bar["l"] + s.c_frac * rng
        cells = None
        # ---- VETO (frozen of_rules CANCEL)
        if bar["c"] < zl:
            run_below += 1
            run_delta += bar["with"] - bar["against"]
            run_vol += bar["vol"]
        else:
            run_below, run_delta, run_vol = 0, 0.0, 0.0
        if run_below >= accept_bars and bar["c"] < beyond and run_delta <= -s.cancel_force * run_vol \
                and run_vol >= s.cancel_min_vol * med * run_below:
            return {**none, "outcome": "CANCEL", "minute": bar["end"], "path": "ACCEPT", "fa": fa}
        if bar["c"] < beyond and bar["dfrac"] <= -s.d_test:
            cells = flow.cells(b, size)
            if _stacked_below(cells, zl, flow.row_size, avg_cell):
                return {**none, "outcome": "CANCEL", "minute": bar["end"], "path": "INITIATIVE", "fa": fa}
        # ---- FA: poke below the zone, back inside before acceptance, no stacked selling
        if bar["l"] < zl:
            if cells is None:
                cells = flow.cells(b, size)
            poke_open = True
            poke_stacked = poke_stacked or _stacked_below(cells, zl, flow.row_size, avg_cell)
        if poke_open and bar["c"] >= zl:
            if not poke_stacked and run_below < accept_bars:
                fa = True
            poke_open, poke_stacked = False, False
        # ---- INI (needs a hold from an EARLIER bar)
        if hold is not None:
            stale = s.turn_window is not None and bar["end"] - hold_bar["end"] > s.turn_window
            if bar["l"] < hold_low or stale:
                hold = None
            elif bar["dfrac"] >= s.d_turn and bar["vol"] >= s.v_turn * med and bar["c"] > hold_top:
                if cells is None:
                    cells = flow.cells(b, size)
                if stacked_with(cells, avg_cell) and (recipe != "FA_DEF_INI" or fa):
                    origin = stack_origin(cells, avg_cell)
                    orig_row = None if origin is None else (origin if flow.side == "BUY" else -origin - 1)
                    return {"outcome": "CONFIRM", "minute": bar["end"], "path": hold,
                            "hold_low": flow.price(hold_low), "fa": fa,
                            "ini_high": flow.price(bar["h"]), "stack_row": orig_row}
        # ---- TEST
        if test is None and at_zone and bar["vol"] >= s.v_test * med and bar["dfrac"] <= -s.d_test:
            test = bar
            since_test = []
        if test is not None:
            prior_low = min((x["l"] for x in since_test), default=None)
            since_test.append(bar)
            if hold is None:
                path = None
                if recipe != "EXH_INI" and at_zone and bar["vol"] >= s.v_abs * med \
                        and bar["dfrac"] <= -s.d_abs and upper and bar["c"] >= zl:
                    path = "ABS"
                elif bar is not test and prior_low is not None and bar["l"] < prior_low \
                        and bar["against"] <= s.e_frac * test["against"] and upper and bar["c"] >= zl:
                    path = "EXH"
                if path:
                    hold, hold_bar = path, bar
                    hold_low = float(np.nanmin(flow.l[tb * size:bar["end"] + 1]))
                    hold_top = max(x["h"] for x in since_test)
        b += 1
    return {**none, "outcome": "NO_SIGNAL", "fa": fa}
