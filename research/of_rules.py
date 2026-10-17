"""Order-flow confirmation rules at a zone - research only.

Implements calibration/orderflow_definition/definition_draft.md (story TEST -> HOLD ->
TURN, plus CANCEL and NO SIGNAL), with the user's decisions of 2026-10-14:
  * absorption (ABS) and exhaustion (EXH) are separate HOLD paths, recorded per signal;
  * bar size is a parameter (1, 5 or 15 minutes; 5 is primary);
  * the whole story must complete within 60 minutes of the touch;
  * three threshold settings - STRICT, MEDIUM (primary), LOOSE - fixed here BEFORE any
    outcome on the fresh data is computed (see calibration/of_fresh_test/preregistration.md).

CAUSALITY: a bar is read only once it is complete, and every decision uses only bars that
have completed by then. The reference statistics come from the 120 minutes BEFORE the
touch bar. Nothing after the decision bar is read.

ORIENTATION: everything is written for a BUY watch at support (price arrived from above).
A SELL watch is evaluated on a mirrored copy: prices negated (so highs become lows), and
"against" volume = aggressive buyers instead of aggressive sellers. That mirroring is exact,
so BUY and SELL use literally the same rule code.

Definitions on a completed bar (BUY orientation):
  vol      total traded volume
  against  volume of aggressive sellers (traded at the bid); with = aggressive buyers
  dfrac    (with - against) / vol, in [-1, 1]
  med      median bar volume over the reference window (same bar size)
  avg_cell mean non-zero per-side cell volume over the reference window
  at_zone  the bar's range overlaps the zone

  TEST     at_zone, vol >= V_TEST x med, dfrac <= -D_TEST                (sellers push in)
  HOLD-ABS at_zone, vol >= V_ABS x med, dfrac <= -D_ABS, the close in the top
           (1 - C_FRAC) of the bar's range, and the close not below the zone
           (heavy selling, little result - "effort without result")
  HOLD-EXH a bar after the TEST making a new low below every bar since the TEST, on
           against-volume <= E_FRAC x the TEST bar's against-volume, closing in the top
           (1 - C_FRAC) of its range and not below the zone       (selling dried up)
           A bar can be both TEST and HOLD-ABS. If both HOLD paths fire, ABS is recorded.
  TURN     a bar AFTER the hold with dfrac >= D_TURN, vol >= V_TURN x med, a close above
           the highest high from the TEST through the HOLD bar, and a low that does not
           undercut the hold low                              (buyers take over, with result)
           -> CONFIRM at the close of this bar.
           If a later bar undercuts the hold low before a TURN, the hold has failed: it
           is discarded and that bar is re-examined as a possible new hold.
  CANCEL   (checked first on every bar, from the touch bar on)
           acceptance: closes below the zone for ACCEPT_MIN consecutive minutes of bars
             (>= 1 bar) with net selling (sum of their deltas <= 0); or
           initiative: a bar closing below the zone with dfrac <= -D_TEST and >= 3
             consecutive rows of selling imbalance entirely below the zone (sell volume at
             row r >= 3 x buy volume at row r+1, and >= avg_cell)
  NO_SIGNAL neither within 60 minutes of the touch (bars ending later are not read).

REVISION R2 (2026-10-14, before any outcome was computed; preregistration section 9).
Made because the review examples showed readings that do not match the written definition:
"turns" that were ordinary bars long after the hold, and "cancels" on barely-there,
low-volume drifts past the zone edge. The revised settings keep the names STRICT / MEDIUM
(primary) / LOOSE. The original R1 settings remain as *_V1, reported for comparison only.
  TURN     must end within TURN_WINDOW minutes of the HOLD bar's end. Otherwise the hold
           expires, and that bar is re-examined as a possible new hold (like a failed hold).
           V_TURN raised to 1.5 / 1.25 / 1.0.
  CANCEL   the deciding bar must close at least CANCEL_MARGIN x zone width beyond the edge.
           acceptance also needs force: the run's net delta <= -CANCEL_FORCE x its volume,
             and its volume >= CANCEL_MIN_VOL x med x the number of bars in the run.
           initiative: the same margin on the close.
  With turn_window=None, cancel_margin=0, cancel_force=0 and cancel_min_vol=0, the R1
  behaviour is reproduced exactly (verified on a full symbol's output).
"""
from dataclasses import dataclass

import numpy as np

WINDOW_MIN = 60
REF_MIN = 120
ACCEPT_MIN = 10
IMB_RATIO, STACK_ROWS = 3.0, 3


@dataclass(frozen=True)
class Setting:
    v_test: float
    d_test: float
    v_abs: float
    d_abs: float
    c_frac: float
    e_frac: float
    d_turn: float
    v_turn: float
    turn_window: int = None          # minutes from the HOLD bar's end; None = no limit (R1)
    cancel_margin: float = 0.0       # x zone width beyond the edge for the deciding close
    cancel_force: float = 0.0        # acceptance run: net delta <= -force x run volume
    cancel_min_vol: float = 0.0      # acceptance run: volume >= this x med x bars


_R2 = dict(turn_window=30, cancel_margin=0.10, cancel_force=0.15, cancel_min_vol=1.0)
SETTINGS = {
    # R2 (primary family)
    "STRICT": Setting(v_test=1.5, d_test=0.20, v_abs=2.0, d_abs=0.25, c_frac=0.50, e_frac=0.50,
                      d_turn=0.25, v_turn=1.50, **_R2),
    "MEDIUM": Setting(v_test=1.2, d_test=0.15, v_abs=1.5, d_abs=0.15, c_frac=0.40, e_frac=0.60,
                      d_turn=0.15, v_turn=1.25, **_R2),
    "LOOSE": Setting(v_test=1.0, d_test=0.10, v_abs=1.2, d_abs=0.10, c_frac=0.30, e_frac=0.75,
                     d_turn=0.10, v_turn=1.00, **_R2),
    # R1, as first pre-registered (comparison only)
    "STRICT_V1": Setting(v_test=1.5, d_test=0.20, v_abs=2.0, d_abs=0.25, c_frac=0.50, e_frac=0.50,
                         d_turn=0.25, v_turn=1.25),
    "MEDIUM_V1": Setting(v_test=1.2, d_test=0.15, v_abs=1.5, d_abs=0.15, c_frac=0.40, e_frac=0.60,
                         d_turn=0.15, v_turn=1.00),
    "LOOSE_V1": Setting(v_test=1.0, d_test=0.10, v_abs=1.2, d_abs=0.10, c_frac=0.30, e_frac=0.75,
                        d_turn=0.10, v_turn=0.80),
}


class DayFlow:
    """One UTC day of per-minute footprint + 1m OHLC, oriented for one side.

    raw: mapping with arrays minute (0..1439), row, buy, sell and row_size (as stored by
    research/footprint.py). ohlc: four arrays of length 1440 (NaN where missing)."""

    def __init__(self, raw, ohlc, side):
        self.side = side
        minute = np.asarray(raw["minute"], dtype=np.int64)
        row = np.asarray(raw["row"], dtype=np.int64)
        buy = np.asarray(raw["buy"], dtype=float)
        sell = np.asarray(raw["sell"], dtype=float)
        self.row_size = float(np.asarray(raw["row_size"]).ravel()[0])
        o, h, l, c = (np.asarray(x, dtype=float) for x in ohlc)
        if side == "BUY":
            self.minute, self.row, self.against, self.with_ = minute, row, sell, buy
            self.o, self.h, self.l, self.c = o, h, l, c
        else:                                     # mirror: negate prices, swap roles
            self.minute, self.row, self.against, self.with_ = minute, -row - 1, buy, sell
            self.o, self.h, self.l, self.c = -o, -l, -h, -c
        self.min_against = np.bincount(minute, weights=self.against, minlength=1440)
        self.min_with = np.bincount(minute, weights=self.with_, minlength=1440)

    def zone(self, low, high):
        return (low, high) if self.side == "BUY" else (-high, -low)

    def price(self, p):
        return p if self.side == "BUY" else -p

    def bar(self, b, size):
        """Aggregates of completed bar b (minutes b*size .. b*size+size-1), or None."""
        a, z = b * size, b * size + size
        if a < 0 or z > 1440 or np.isnan(self.c[a:z]).any():
            return None
        against = float(self.min_against[a:z].sum())
        with_ = float(self.min_with[a:z].sum())
        vol = against + with_
        return {"b": b, "end": z - 1, "o": self.o[a], "h": float(np.max(self.h[a:z])),
                "l": float(np.min(self.l[a:z])), "c": self.c[z - 1], "vol": vol,
                "against": against, "with": with_,
                "dfrac": (with_ - against) / vol if vol > 0 else 0.0}

    def cells(self, b, size):
        """{row: (against, with)} for bar b."""
        sel = (self.minute >= b * size) & (self.minute < (b + 1) * size)
        out = {}
        for r, a, w in zip(self.row[sel], self.against[sel], self.with_[sel]):
            x = out.get(int(r), (0.0, 0.0))
            out[int(r)] = (x[0] + a, x[1] + w)
        return out

    def reference(self, first_bar, size):
        """(median bar volume, mean non-zero per-side cell volume) over the REF_MIN minutes
        before first_bar; None if under half of those bars exist or volume is zero."""
        n = REF_MIN // size
        bars = [self.bar(b, size) for b in range(first_bar - n, first_bar)]
        bars = [x for x in bars if x is not None]
        if len(bars) < max(1, n // 2):
            return None
        med = float(np.median([x["vol"] for x in bars]))
        a0, z0 = (first_bar - n) * size, first_bar * size
        sel = (self.minute >= max(a0, 0)) & (self.minute < z0)
        sides = np.concatenate([self.against[sel][self.against[sel] > 0],
                                self.with_[sel][self.with_[sel] > 0]])
        if med <= 0 or sides.size == 0:
            return None
        return med, float(sides.mean())


def _stacked_below(cells, zone_low, row_size, avg_cell):
    """>= STACK_ROWS consecutive rows of selling imbalance lying entirely below zone_low."""
    run = 0
    for r in sorted(cells):
        a = cells[r][0]
        w_above = cells.get(r + 1, (0.0, 0.0))[1]
        top = (r + 1) * row_size
        ok = top <= zone_low and a >= avg_cell and a >= IMB_RATIO * w_above
        run = run + 1 if ok else 0
        if run >= STACK_ROWS:
            return True
    return False


def evaluate(flow, zone_low, zone_high, touch_minute, size, setting):
    """Run the story for one watch. Returns a dict:
      outcome  CONFIRM / CANCEL / NO_SIGNAL / NO_DATA
      minute   1m index whose close is the decision point (bar end), or None
      path     ABS / EXH for CONFIRM, ACCEPT / INITIATIVE for CANCEL
      hold_low hold low in ORIGINAL prices (BUY: a low, SELL: a high) for CONFIRM
      test_min / hold_min  end minute of the bar read as TEST / the current HOLD (output
               only, for review pages; added 2026-10-14, cannot change any decision)
    """
    s = SETTINGS[setting] if isinstance(setting, str) else setting
    zl, zh = flow.zone(zone_low, zone_high)
    tb = touch_minute // size
    ref = flow.reference(tb, size)
    if ref is None:
        return {"outcome": "NO_DATA", "minute": None, "path": None, "hold_low": None,
                "test_min": None, "hold_min": None}
    med, avg_cell = ref
    deadline = touch_minute + WINDOW_MIN
    accept_bars = max(1, -(-ACCEPT_MIN // size))
    beyond = zl - s.cancel_margin * (zh - zl)          # the deciding close must be below this
    test = hold = None
    run_below, run_delta, run_vol = 0, 0.0, 0.0
    since_test = []                                   # bars from the TEST bar on
    hold_low = hold_top = hold_bar = None

    def marks():                                      # output only: which bars were read as
        return {"test_min": test["end"] if test else None,          # TEST and HOLD
                "hold_min": hold_bar["end"] if hold is not None else None}
    b = tb
    while True:
        bar = flow.bar(b, size)
        if bar is None or bar["end"] > deadline:
            break
        at_zone = bar["l"] <= zh and bar["h"] >= zl
        rng = bar["h"] - bar["l"]
        upper = bar["c"] >= bar["l"] + s.c_frac * rng
        # ---- CANCEL
        if bar["c"] < zl:
            run_below += 1
            run_delta += bar["with"] - bar["against"]
            run_vol += bar["vol"]
        else:
            run_below, run_delta, run_vol = 0, 0.0, 0.0
        if run_below >= accept_bars and bar["c"] < beyond and run_delta <= -s.cancel_force * run_vol \
                and run_vol >= s.cancel_min_vol * med * run_below:
            return {"outcome": "CANCEL", "minute": bar["end"], "path": "ACCEPT", "hold_low": None, **marks()}
        if bar["c"] < beyond and bar["dfrac"] <= -s.d_test and \
                _stacked_below(flow.cells(b, size), zl, flow.row_size, avg_cell):
            return {"outcome": "CANCEL", "minute": bar["end"], "path": "INITIATIVE", "hold_low": None, **marks()}
        # ---- TURN (needs a hold from an EARLIER bar)
        if hold is not None:
            stale = s.turn_window is not None and bar["end"] - hold_bar["end"] > s.turn_window
            if bar["l"] < hold_low or stale:
                hold = None                           # hold failed or expired; re-examine this bar
            elif bar["dfrac"] >= s.d_turn and bar["vol"] >= s.v_turn * med and bar["c"] > hold_top:
                return {"outcome": "CONFIRM", "minute": bar["end"], "path": hold,
                        "hold_low": flow.price(hold_low), **marks()}
        # ---- TEST
        if test is None and at_zone and bar["vol"] >= s.v_test * med and bar["dfrac"] <= -s.d_test:
            test = bar
            since_test = []
        if test is not None:
            prior_low = min((x["l"] for x in since_test), default=None)
            since_test.append(bar)
            # ---- HOLD
            if hold is None:
                path = None
                if at_zone and bar["vol"] >= s.v_abs * med and bar["dfrac"] <= -s.d_abs \
                        and upper and bar["c"] >= zl:
                    path = "ABS"
                elif bar is not test and prior_low is not None and bar["l"] < prior_low \
                        and bar["against"] <= s.e_frac * test["against"] and upper and bar["c"] >= zl:
                    path = "EXH"
                if path:
                    hold, hold_bar = path, bar
                    a0 = tb * size
                    hold_low = float(np.nanmin(flow.l[a0:bar["end"] + 1]))
                    hold_top = max(x["h"] for x in since_test)
        b += 1
    return {"outcome": "NO_SIGNAL", "minute": None, "path": None, "hold_low": None, **marks()}
