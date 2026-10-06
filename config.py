"""Single source of truth for every tunable in binance_ai_bot_vp.

Three rules govern this file, and they are not stylistic:

1. EVERY value is read from the environment with an explicit default, so a
   deployment is fully described by its .env plus this file's defaults. Nothing
   is hard-coded at a call site.

2. Any flag that expresses a MARKET OPINION defaults OFF. A flag that prevents
   arithmetic nonsense (a stop tighter than fees, a profile built from 40
   trades) may default ON, because being wrong about those is a bug rather than
   a hypothesis. The distinction is load-bearing: it is what stops unvalidated
   beliefs from silently becoming production behaviour.

3. Thresholds carry their provenance in a comment. Until a phase gate in the
   roadmap has passed, that comment reads PROVISIONAL. A number with no
   evidence behind it must say so, otherwise the next reader will assume it was
   measured.

Volume distribution is the sole source of trading premises (principle P1), so
there are deliberately no indicator periods in this file beyond the ATR used
for normalising distances, which is a unit of measure and not a signal.
"""
import os

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------- env helpers

def env_str(name, default):
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def env_bool(name, default):
    raw = env_str(name, default).lower()
    return raw in ("1", "true", "yes", "on")


def env_int(name, default):
    try:
        return int(float(env_str(name, str(default))))
    except (TypeError, ValueError):
        return int(default)


def env_float(name, default):
    try:
        return float(env_str(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def env_str_list(name, default):
    raw = env_str(name, "")
    if not raw:
        return list(default)
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


# A standalone copy of data.klines.INTERVAL_MS's keys, minutes rather than ms. Cannot
# import data.klines here - it imports config, and config must not import it back.
# Only the units this file's own interval settings actually use.
_INTERVAL_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720,
    "1d": 1440, "3d": 4320, "1w": 10080,
}


def interval_minutes(text, default_minutes=15):
    """Minutes per candle for an interval string like "15m" - unknown -> the default."""
    return _INTERVAL_MINUTES.get(str(text).strip().lower(), default_minutes)


# ------------------------------------------------------------------- identity

BOT_NAME = env_str("BOT_NAME", "binance_ai_bot_vp")

# Kept separate from the trading key so a research run can never place an order
# even by accident - research code loads no credentials at all.
BINANCE_API_KEY = env_str("BINANCE_API_KEY", "")
BINANCE_API_SECRET = env_str("BINANCE_API_SECRET", "")

# Binance USD-M testnet. Default TRUE: a fresh clone must not be able to touch
# real money until someone deliberately turns this off.
USE_TESTNET = env_bool("USE_TESTNET", "True")

# The master switch. False means: scan, profile, detect, journal - but never
# send an order. This is the mode Phase 5 runs in.
TRADING_ENABLED = env_bool("TRADING_ENABLED", "False")

# Readable without a restart; ops/killswitch.py polls it.
KILL_SWITCH_FILE = env_str("KILL_SWITCH_FILE", "KILL_SWITCH")


# ------------------------------------------------------------------ sessions
# See sessions.py. The venue is UTC-native: 1d klines open at 00:00:00.000 UTC
# and funding settles at 00/08/16 UTC, so the exchange session IS the UTC day.
# This is also the only DST-immune choice available.

# NOT env-driven, and deliberately so. The session IS the UTC day: sessions.py floors
# on MS_DAY directly, which is exact only because UTC has no offset changes. Exposing
# this as a setting would advertise a flexibility that does not exist - nothing reads it,
# and changing it would not move a single boundary.
SESSION_INTERVAL = "1d"

# Regional windows are FEATURES ONLY - recorded on every candidate, never used
# to slice a profile, because they overlap and cannot partition volume.
SESSION_TAG_ASIA = env_str("SESSION_TAG_ASIA", "00:00-08:00")
SESSION_TAG_LONDON = env_str("SESSION_TAG_LONDON", "07:00-16:00")
SESSION_TAG_NEWYORK = env_str("SESSION_TAG_NEWYORK", "12:00-21:00")

FUNDING_HOURS_UTC = [0, 8, 16]
FUNDING_GUARD_MINUTES = env_int("FUNDING_GUARD_MINUTES", 15)

# A developing profile is not a profile yet. Setups reading the CURRENT session
# wait for both floors. PROVISIONAL - Phase 4 sweeps these.
DEVELOPING_MIN_ELAPSED_MINUTES = env_int("DEVELOPING_MIN_ELAPSED_MINUTES", 120)
DEVELOPING_MIN_VOLUME_FRACTION = env_float("DEVELOPING_MIN_VOLUME_FRACTION", 0.15)


# ------------------------------------------------------------------- universe

# Research wide, trade narrow (open decision #1). The research universe is
# ranked once per run and the rank is RECORDED, so an in-sample/held-out split
# can never drift between two separately-launched jobs.
UNIVERSE_SIZE = env_int("UNIVERSE_SIZE", 600)
UNIVERSE_QUOTE_ASSET = env_str("UNIVERSE_QUOTE_ASSET", "USDT")

# Live trading universe: the liquid head of the same ranking.
TRADE_UNIVERSE_SIZE = env_int("TRADE_UNIVERSE_SIZE", 120)
TRADE_MIN_24H_QUOTE_VOLUME = env_float("TRADE_MIN_24H_QUOTE_VOLUME", 50_000_000.0)

SYMBOL_DENYLIST = env_str_list("SYMBOL_DENYLIST", [])
# Live scan universe, as bot_ds does it. SCAN_SYMBOLS pins the list (in the order given,
# volume floor bypassed); empty means the TRADE_UNIVERSE_SIZE liquid head by volume.
# The 24h ticker is refetched at most every WATCHLIST_REFRESH_SECONDS.
SCAN_SYMBOLS = env_str_list("SCAN_SYMBOLS", [])
WATCHLIST_REFRESH_SECONDS = env_float("WATCHLIST_REFRESH_SECONDS", 300.0)

# Live 1m candles over websockets (data/ws_feed.py). OFF by default: REST stays the path
# until the feed has been checked against it. When on, the feed only fills the candle
# cache and REST still covers every gap.
WS_ENABLED = env_bool("WS_ENABLED", "False")
WS_SYMBOLS_PER_SOCKET = env_int("WS_SYMBOLS_PER_SOCKET", 100)
WS_STALE_SECONDS = env_float("WS_STALE_SECONDS", 45.0)
WS_WATCHDOG_INTERVAL_SECONDS = env_float("WS_WATCHDOG_INTERVAL_SECONDS", 15.0)
WS_RESTART_COOLDOWN_SECONDS = env_float("WS_RESTART_COOLDOWN_SECONDS", 30.0)


# ------------------------------------------------------------- profile engine
# See profile/builder.py and profile/levels.py.

# Profiles are accumulated from 1m klines: full history is available, so live
# and replay compute identical levels. A tape-fed profile has no history and
# cannot be validated at all.
PROFILE_SOURCE_INTERVAL = env_str("PROFILE_SOURCE_INTERVAL", "1m")

# Bin width is ATR-normalised with a tick floor so profiles are comparable
# across symbols and days. Fixed row counts make bin width a function of
# session range, which destroys that comparability.
# 0.02 -> ~50 bins per ATR of range. PROVISIONAL.
BIN_ATR_FRACTION = env_float("BIN_ATR_FRACTION", 0.02)
BIN_ATR_PERIOD_DAYS = env_int("BIN_ATR_PERIOD_DAYS", 14)
BIN_MAX_COUNT = env_int("BIN_MAX_COUNT", 4000)  # guard against absurd grids

# The central fraction of volume defining value. 70% ~ 1 standard deviation,
# the Market Profile convention. Phase 4 sweeps it.
VALUE_AREA_PCT = env_float("VALUE_AREA_PCT", 0.70)

# Intra-candle volume placement. "range" spreads each candle's volume uniformly
# across its high-low span weighted by bin overlap; "close" dumps it at the
# close and exists only so the difference can be measured, never for live use.
INTRA_CANDLE_DISTRIBUTION = env_str("INTRA_CANDLE_DISTRIBUTION", "range")

# HVN/LVN thresholds, as a fraction of POC bin volume. PROVISIONAL.
# HVN DETECTION METHOD. "va_plan" (default) is the value-area method in profile/va_hvn.py,
# built to the plan's specification. "current" is the original definition (contiguous runs of
# bins at or above HVN_PCT of the POC volume, across the whole histogram) that item 17's
# validated result was measured on. Switching back restores that measurement's exact basis.
# Item 17's validated numbers were measured on "current", so the va_plan default has NOT yet
# been replayed against them.
HVN_METHOD = env_str("HVN_METHOD", "va_plan").strip().lower()
# The va_plan thresholds below are the plan's recommended starting points, fixed before any
# replay. They are NOT to be tuned on the windows used to judge them.
VA_HVN_MIN_POC_FRACTION = env_float("VA_HVN_MIN_POC_FRACTION", 0.60)
VA_HVN_MIN_MEAN_MULTIPLE = env_float("VA_HVN_MIN_MEAN_MULTIPLE", 1.75)
VA_HVN_MIN_PROMINENCE = env_float("VA_HVN_MIN_PROMINENCE", 0.10)
VA_HVN_GROW_FRACTION = env_float("VA_HVN_GROW_FRACTION", 0.50)
VA_HVN_MAX_WIDTH_ATR = env_float("VA_HVN_MAX_WIDTH_ATR", 1.0)
VA_HVN_MAX_COUNT = env_int("VA_HVN_MAX_COUNT", 3)
VA_HVN_MIN_RUN_BINS = env_int("VA_HVN_MIN_RUN_BINS", 2)
# The developing profile gets HVNs only once the session is this old - the plan says a
# developing profile is not meaningful until it has enough volume.
DEV_HVN_MIN_MINUTES = env_int("DEV_HVN_MIN_MINUTES", 90)

# S4-OFR (order-flow reversals, the order-flow plan). ON by default per the owner; not yet validated by replay.
# Thresholds are the plan's starting points, fixed before any replay. Stacked footprint
# imbalance and resting large orders are not available from the data this system holds.
S4_OFR_ENABLED = env_bool("S4_OFR_ENABLED", "True")
# The only setups the state machine may evaluate, live and in replay. Setups outside this
# list stay in the code but are rejected as SETUP_DISABLED before their detector runs.
ACTIVE_SETUPS = env_str_list("ACTIVE_SETUPS", ["S4-OFR"])
OFR_BAR_INTERVAL = env_str("OFR_BAR_INTERVAL", "5m")
OFR_MIN_BARS = env_int("OFR_MIN_BARS", 25)
OFR_TOUCH_TOL_ATR = env_float("OFR_TOUCH_TOL_ATR", 0.10)
OFR_ABSORB_VOLUME_MULT = env_float("OFR_ABSORB_VOLUME_MULT", 2.0)
OFR_ABSORB_DELTA_FRAC = env_float("OFR_ABSORB_DELTA_FRAC", 0.20)
OFR_FLIP_DELTA_FRAC = env_float("OFR_FLIP_DELTA_FRAC", 0.10)
OFR_CVD_LOOKBACK_BARS = env_int("OFR_CVD_LOOKBACK_BARS", 12)
OFR_FOLLOW_ATR = env_float("OFR_FOLLOW_ATR", 0.10)
OFR_EXCEPTIONAL_FOLLOW_ATR = env_float("OFR_EXCEPTIONAL_FOLLOW_ATR", 0.50)
# Entry, stop and target distances follow the entry/TP/SL plan and are measured in 15m ATR
# (OFR_PLAN_ATR_INTERVAL), not the daily ATR the zone band and follow-through still use.
OFR_PLAN_ATR_INTERVAL = env_str("OFR_PLAN_ATR_INTERVAL", "15m")
OFR_ENTRY_BUFFER_ATR = env_float("OFR_ENTRY_BUFFER_ATR", 0.10)
OFR_ENTRY_MAX_DISTANCE_ATR = env_float("OFR_ENTRY_MAX_DISTANCE_ATR", 0.50)
OFR_STOP_BUFFER_ATR = env_float("OFR_STOP_BUFFER_ATR", 0.20)
OFR_TP_MIN_R = env_float("OFR_TP_MIN_R", 1.2)
OFR_TP_FALLBACK_R = env_float("OFR_TP_FALLBACK_R", 2.0)
OFR_DEV_SUPPORT_ATR = env_float("OFR_DEV_SUPPORT_ATR", 0.50)
# Which touches of a zone can trigger S4. "any": every visit this session, each still needing
# order-flow confirmation - the owner's choice, since true first visits are rare once a
# previous-day profile is in use. "first": only the zone's first visit (the original rule).
# T3 and the X exception still require a first visit, whichever rule is set.
OFR_VISIT_RULE = env_str("OFR_VISIT_RULE", "any").strip().lower()

# STACKED and RESTING from the feed recorder (data/feed_reader.py). OFF until replayed:
# recording started recently, so there is little history to test against. Thresholds are
# the starting points proposed in the order-flow plan, fixed before any result is seen.
OFR_USE_RECORDED_FLOW = env_bool("OFR_USE_RECORDED_FLOW", "False")
OFR_FLOW_WINDOW_MINUTES = env_int("OFR_FLOW_WINDOW_MINUTES", 10)
OFR_STACK_RATIO = env_float("OFR_STACK_RATIO", 3.0)
OFR_STACK_MIN_LEVELS = env_int("OFR_STACK_MIN_LEVELS", 3)
OFR_RESTING_SIZE_MULT = env_float("OFR_RESTING_SIZE_MULT", 3.0)
OFR_RESTING_PERSIST = env_float("OFR_RESTING_PERSIST", 0.6)
# Profile-shape bias for S4: on a BULL or BEAR day, reversals against the bias are refused
# (see profile/shape.py day_bias). A market opinion, so OFF until replay validates it.
BIAS_FILTER_ENABLED = env_bool("BIAS_FILTER_ENABLED", "False")
VA_HVN_STABILITY_TOL_BINS = env_float("VA_HVN_STABILITY_TOL_BINS", 1.0)
HVN_PCT = env_float("HVN_PCT", 0.70)
LVN_PCT = env_float("LVN_PCT", 0.35)
LVN_MIN_WIDTH_BINS = env_int("LVN_MIN_WIDTH_BINS", 2)

# Level stability: recompute at these bin-width multiples and require the level
# to survive. A level that moves when you re-bin it is an artifact of binning,
# not a participation peak, and cannot be traded.
STABILITY_BIN_MULTIPLIERS = [0.75, 1.33]

# SEPARATE THRESHOLDS FOR THE POC AND THE VALUE BOUNDS, because they are not
# comparably stable and one number for both is wrong for one of them.
#
# Measured on live sessions (BTCUSDT / ETHUSDT / SOLUSDT):
#     poc_shift_bins  0.125 / 0.125 / 0.375     <- very robust
#     vah_shift_bins  1.00  / 0.75  / 2.25      <- much less so
#     val_shift_bins  1.25  / 1.07  / 0.74
#
# The asymmetry is structural, not noise. A POC is an ARGMAX: re-binning has to
# change which bin is tallest to move it, which takes a real change. A value bound
# is where a CUMULATIVE SUM crosses 70%, so a small change in bin width shifts the
# crossing point by a bin or two almost every time.
#
# A single threshold of 1 bin - the first draft - rejected the value bounds on ALL
# THREE liquid symbols, which would have gated S2 and S3 off across the entire
# tradable universe and produced a textbook false null: Phase 1 would have concluded
# "no edge" about a filter rather than about the market.
# CALIBRATED, Phase 0, n=14,185 symbol-sessions.
#
#   poc_shift_bins   p05 0.13  p20 0.36  p50 0.62  p80 1.38  p90 4.78  p95 11.36
#   vah_shift_bins   p05 0.25  p20 0.60  p50 1.16  p80 1.97  p90 3.00  p95  6.25
#   val_shift_bins   p05 0.25  p20 0.59  p50 1.13  p80 1.96  p90 3.03  p95  6.25
#
# The three live symbols were unrepresentatively stable (0.125-0.375 against a corpus
# median of 0.62), which is what a three-symbol sample buys you.
#
# THE POC THRESHOLD MOVES 1.0 -> 2.0. At 1.0 it rejected 24% of all sessions, making it
# by far the most aggressive gate in the system. The distribution explains where to put
# it: there is a sharp break between p80 = 1.38 and p90 = 4.78, so sessions either
# re-bin to within ~2 bins or they fall apart completely. 2.0 sits inside that gap - it
# still cuts the genuinely unstable tail (~16%) without discarding a quarter of the
# corpus, which is the permissive-first rule applied to a measured distribution rather
# than to a guess.
#
# The VALUE-AREA threshold stays at 3.0, which the corpus says is already exactly p90.
# That one was calibrated correctly by accident, and the asymmetry it was introduced to
# handle is confirmed: a value bound really is ~2x less stable than a POC at every
# percentile.
STABILITY_MAX_POC_SHIFT_BINS = env_float("STABILITY_MAX_POC_SHIFT_BINS", 2.0)
STABILITY_MAX_VA_SHIFT_BINS = env_float("STABILITY_MAX_VA_SHIFT_BINS", 3.0)

# POC prominence = POC bin volume / mean occupied bin volume. A flat
# distribution has no meaningful mode however stable its argmax is - the argmax
# of near-uniform noise is perfectly reproducible and perfectly meaningless, so
# stability alone is not enough and this is the companion check.
#
# CALIBRATION REFERENCE: synthetic Gaussians measured 1.17 / 1.50 / 2.63 / 2.78
# at 11 / 27 / 53 / 56 occupied bins - prominence RISES with resolution, because
# finer bins lower the mean without lowering the peak. At the ~50 bins/ATR that
# BIN_ATR_FRACTION=0.02 targets, the Gaussian archetype sits near 2.7.
#
# Set to 1.25, deliberately PERMISSIVE. A realistic balanced session - a dense
# fairly even core with short rejected tails - measured only 1.29, so a floor at
# 1.8 would have rejected the very population S1 exists to trade.
#
# THE ASYMMETRY THAT DECIDES THIS. A quality gate set too LOOSE costs some bad
# trades, which Phase 2 will measure and price. A quality gate set too TIGHT
# produces a FALSE NULL: the setup never fires on the sessions where it works, and
# Phase 1 concludes "no edge" about a filter rather than about the market. The
# second failure is far worse because it is invisible - there is no signal that a
# measurement was strangled rather than informative. So a mechanical gate should
# start permissive and be tightened on evidence, never the reverse.
#
# CALIBRATED, Phase 0, n=14,185 symbol-sessions over 120 symbols x 119 UTC days.
#
#   p05 2.02   p10 2.23   p20 2.49   p50 3.15   p80 4.19   p90 5.11   p95 6.26
#
# The synthetic estimate was badly wrong, and wrong in the dangerous direction. A
# realistic-looking hand fixture measured 1.29, which is why this was set to 1.25 - but
# REAL sessions start at 2.02 at the 5th percentile, so 1.25 rejected 1.1% of the
# corpus. The gate was inert: it looked like a quality filter and filtered nothing.
#
# This is the cleanest confirmation of the lesson in CALIBRATION.md - a synthetic
# fixture encodes an assumption about what real profiles look like, and that assumption
# IS the thing being calibrated. Prominence rises with resolution, and the fixture's
# bin count was not the corpus's bin count.
#
# Set at p10, keeping the permissive-first asymmetry above: it now rejects the
# structureless bottom decile (~10%) rather than nothing at all.
POC_MIN_PROMINENCE = env_float("POC_MIN_PROMINENCE", 2.20)

# Profile quality floors. Below these the histogram is not a distribution.
PROFILE_MIN_CANDLES = env_int("PROFILE_MIN_CANDLES", 600)      # of 1440
PROFILE_MIN_OCCUPIED_BINS = env_int("PROFILE_MIN_OCCUPIED_BINS", 20)
PROFILE_MIN_QUOTE_VOLUME = env_float("PROFILE_MIN_QUOTE_VOLUME", 1_000_000.0)

# Value-area sanity. A VA covering nearly the whole range says nothing; one
# covering almost none of it means the session was a single spike.
# Measured live: BTCUSDT and ETHUSDT value areas came in at 0.14 ATR, SOLUSDT at
# 0.26 - all on ordinary sessions. The first draft's floor of 0.10 was only just below
# the normal case, so a slightly quieter day would have tripped VA_DEGENERATE on the
# most liquid symbols on the venue. Lowered to 0.05, which still catches the genuine
# degenerate case (a session that was a single spike) without rejecting normal ones.
VA_MIN_WIDTH_ATR = env_float("VA_MIN_WIDTH_ATR", 0.05)
VA_MAX_WIDTH_ATR = env_float("VA_MAX_WIDTH_ATR", 3.00)
VA_MAX_RANGE_FRACTION = env_float("VA_MAX_RANGE_FRACTION", 0.92)


# ------------------------------------------------------------ shape classifier
# See profile/shape.py. Quantitative, not visual.

# value_area_width / total_range - the ELONGATION measure, and a SINGLE
# threshold. Below it the profile is elongated (trend); above it, shape is decided
# by POC position alone. See profile/shape.classify.
#
# WHY ONE THRESHOLD AND NOT TWO. The first draft had a trend ceiling of 0.28 and a
# separate "balanced floor" of 0.40, which created a DEAD ZONE: a profile with a
# perfectly central POC and a ratio of 0.33 matched neither the P nor the b test,
# failed the balanced floor, and fell through to `trend` - the archetype of
# balance classified as its opposite. Two thresholds on one axis with distinct
# outcomes either side always leaves that gap. Collapsed to one.
#
# WHY A GAUSSIAN IS THE WRONG REFERENCE FOR THIS PARTICULAR NUMBER. Measured
# synthetic Gaussians return VA/range ~0.33 at usable resolution (0.328 at 53
# bins, 0.310 at 56), because a normal distribution has long thin tails relative
# to its core. Real session profiles are PLATYKURTIC by comparison - boxier, with
# short tails - so a genuinely balanced session reads much higher, commonly
# 0.45-0.70, while a trend day reads 0.20-0.35. The Gaussian therefore calibrates
# POC_MIN_PROMINENCE well (same core-vs-mean logic) and calibrates this ratio
# badly.
#
# 0.35 is set from that reasoning, NOT from a measurement, and it is consequently
# the single most important number for Phase 0 to recalibrate against the observed
# distribution of real sessions. Getting it wrong mislabels the auction state,
# which is the one error that makes every setup trade backwards.
# PHASE 0, n=14,185: p05 0.275  p10 0.331  p20 0.389  p50 0.497  p80 0.623  p95 0.767
#
# 0.35 sits at roughly the 13th percentile, so it labels the most elongated ~13% as
# trend on distribution alone; migration adds more, for ~20% total. That is in line with
# the Market Profile prior that trend days are a minority of sessions, so the value is
# KEPT - but honestly, it is a CHOICE rather than a discovery. The measured distribution
# is smooth and unimodal with no natural break anywhere near it, which means no
# threshold on this axis is privileged by the data. Phase 4's robustness sweep is where
# this earns or loses its place; a smooth distribution is precisely the case where a
# result should degrade gracefully under perturbation, and if it does not, the finding
# is about the threshold rather than about the market.
SHAPE_TREND_MAX_VA_RANGE_RATIO = env_float("SHAPE_TREND_MAX_VA_RANGE_RATIO", 0.35)

# Intra-session POC migration, in ATR, above which the session is a TREND
# regardless of how its aggregate histogram looks.
#
# WHY THIS EXISTS - found by testing, and it closes a hole nothing else could.
# VA/range measures volume CONCENTRATION, not direction. A uniform one-directional
# session (a steady ramp) spreads volume evenly across its range, so its value area
# covers ~70% of that range and its POC sits centrally: the trend-day fixture
# measured 0.708 and classified as D, identical to a balanced session. Distribution
# alone genuinely cannot separate them, because a completed histogram has discarded
# the time ordering that makes travel different from spread.
#
# Splitting the session in half and comparing the two POCs restores exactly that
# missing information - a rotating auction agrees with itself across halves, a
# travelling one does not. This is developing value migration applied within a
# session instead of between them.
#
# 0.5 ATR is PROVISIONAL and reasoned, not measured: half a day's typical range of
# POC movement is a clear directional statement while staying above the noise of a
# POC jumping one or two bins. Phase 0 calibrates it against real sessions.
SHAPE_TREND_MIN_POC_MIGRATION_ATR = env_float(
    "SHAPE_TREND_MIN_POC_MIGRATION_ATR", 0.5)
# POC position within range: below/above these -> b-shape / P-shape.
# PHASE 0, n=14,185: poc_position p20 0.263  p50 0.474  p80 0.672
# 0.65 and 0.35 sit at about p78 and p27, and produce near-symmetric outcomes
# (P 10.3% / b 13.4%). KEPT UNCHANGED, with the reason stated plainly: nothing trades
# on the P-versus-b distinction. S1 requires D, S2 and S3 do not read shape at all by
# default, so these two only label journal rows. They are therefore uncalibrated in the
# sense that no outcome has ever been measured against them - and harmless for the same
# reason. If a future setup keys on P or b, calibrate them then.
SHAPE_P_MIN_POC_POSITION = env_float("SHAPE_P_MIN_POC_POSITION", 0.65)
SHAPE_B_MAX_POC_POSITION = env_float("SHAPE_B_MAX_POC_POSITION", 0.35)

# Bimodality: a second mode this large, separated by a valley this deep, is B.
#
# PHASE 0 SENSITIVITY SWEEP, 400 rebuilt sessions across the 0.60-0.90 x 0.15-0.35 grid:
#
#     thresholds        D       B    trend      P      b
#     0.60 / 0.35    32.8%   25.8%   16.8%  12.5%  12.2%   <- current
#     0.90 / 0.15    37.2%    7.0%   23.2%  15.5%  17.0%
#
# B's rate is extremely threshold-sensitive - a 3.7x swing across the grid - so 25.8%
# should NOT be read as "a quarter of sessions have genuinely split value". But the
# conclusion that matters runs the other way: D is STABLE at 33-37% throughout. The
# sessions that leave B become trend, P or b, not D, which means they were one-sided
# sessions rather than balanced ones all along.
#
# So the initial worry - that over-detecting B starves S1, since S1 requires D and D is
# only ~30% - is NOT supported by the data. S1's eligible population is robust to these
# thresholds. They are therefore left alone: tuning a number that moves B around while
# leaving every trading decision unchanged would be fitting for its own sake.
SHAPE_BIMODAL_MIN_SECOND_MODE = env_float("SHAPE_BIMODAL_MIN_SECOND_MODE", 0.60)
SHAPE_BIMODAL_MAX_VALLEY = env_float("SHAPE_BIMODAL_MAX_VALLEY", 0.35)

# Excess: a tail this thin across this many bins at an extreme is genuine
# rejection. Its absence is a "poor" extreme, which tends to be revisited.
EXCESS_MAX_BIN_VOLUME_PCT = env_float("EXCESS_MAX_BIN_VOLUME_PCT", 0.15)
EXCESS_MIN_BINS = env_int("EXCESS_MIN_BINS", 3)

# POOR EXTREME'S OWN THRESHOLD - previously hardcoded at 0.40 directly inside
# profile/shape.py, not even an env-tunable value, and unsatisfiable in practice
# (CALIBRATION.md finding 24). Measured across all 13,901 profiled sessions in the
# corpus: the single extreme bin's volume as a fraction of the POC bin sits at
# p50=0.016, p90=0.08-0.09, p99=0.24-0.28 - the old 0.40 ceiling sat ABOVE the 99th
# percentile, which is why S2_REQUIRE_POOR_EXTREME measured n=5 (finding 18) and
# poor_high/poor_low fired on only 0.3%/0.5% of sessions system-wide. Not a rare
# market phenomenon - an unreachable threshold, the same shape of bug already found
# and fixed twice in this file for S2_MIN_EXCURSION_VA_FRACTION and
# S1_LVN_MIN_POC_DISTANCE_VA_FRACTION.
#
# 0.15 - the SAME fraction as EXCESS_MAX_BIN_VOLUME_PCT, deliberately - is the
# measured replacement: it yields 3.3%/4.0% of all sessions (poor_high/poor_low),
# a real testable population instead of single digits, and it is semantically
# coherent with excess rather than an arbitrary second number: excess asks whether
# the last EXCESS_MIN_BINS bins are ALL at or below this fraction of the POC;
# poor asks whether the single edge bin specifically EXCEEDS that same fraction
# while the run as a whole did not. One bar, two readings of the same measurement.
POOR_EXTREME_MAX_BIN_VOLUME_PCT = env_float(
    "POOR_EXTREME_MAX_BIN_VOLUME_PCT", 0.15)


# ---------------------------------------------------------------- acceptance
# See acceptance.py - the S2/S3 discriminator, and the single most important
# measurement in the system. Acceptance is TIME and VOLUME outside value, never
# distance: a move that travels far but transacts little is a spike, while one
# that builds volume outside old value is price discovery.

# Moved ahead of the thresholds below (its natural home is with the rest of the
# confirmation-vocabulary settings, further down) because every one of them is a
# CANDLE COUNT that means a specific real-world DURATION only at this interval.
# PLAN item 7: re-expressed in minutes below, precisely so a confirm-interval
# ablation (this setting itself) rescales the candle counts instead of silently
# changing what they mean - "13 candles outside" is 3.25h at 15m and 0.22h at 1m,
# a different claim entirely, not the same threshold measured more often.
CONFIRM_INTERVAL = env_str("CONFIRM_INTERVAL", "15m")
CONFIRM_INTERVAL_MINUTES = interval_minutes(CONFIRM_INTERVAL)


def candles_for_minutes(minutes):
    """A duration, expressed as however many CONFIRM_INTERVAL candles it takes.

    Rounds to the nearest candle rather than flooring: at an interval the duration
    doesn't divide evenly by, flooring would systematically understate every
    threshold, and this is a boundary a trade's whole eligibility rests on.
    """
    return max(1, round(minutes / CONFIRM_INTERVAL_MINUTES))


ACCEPT_MIN_CANDLES = env_int("ACCEPT_MIN_CANDLES", candles_for_minutes(45))
# There is deliberately no ACCEPT_CANDLE_INTERVAL. Acceptance measures whatever candles
# it is handed, and it is always handed CONFIRM_INTERVAL candles - a second interval
# setting could silently disagree with the first, and then "3 closes outside value" and
# the confirmation that follows it would be counting different bars.

# The discriminator is a RATE RATIO: volume per candle transacted outside value,
# divided by this session's own volume per candle.
#
#     ratio << 1   price advertised prices nobody transacted at -> a spike, reverts
#     ratio ~= 1   business outside value is running at the normal rate -> discovery
#
# WHY NOT A FRACTION OF THE EXCURSION'S OWN VOLUME - the first version's bug, and it
# was meaningless rather than merely imprecise. The excursion window IS the run of
# candles that closed outside, so nearly all of its volume is outside by construction:
# the measure returned ~1.000 on every excursion, thin or heavy alike. The rejection
# threshold was unreachable, acceptance was trivially satisfied, and the S2/S3
# discriminator did not discriminate. Caught by a pipeline test asserting that the two
# setups can never both fire.
#
# Comparing the session against itself makes the ratio scale-free: no reference to
# ATR, notional size, or any cross-symbol constant, so one threshold means the same
# thing on BTCUSDT and on a thin alt.
#
# THE GAP BETWEEN THE TWO IS THE POINT. Between 0.45 and 0.80 the verdict is PENDING
# and NEITHER setup fires. Overlapping thresholds would let both branches trigger on
# the same data, which is exactly the ambiguity the state machine exists to remove.
#
# UNVALIDATED, AND PHASE 0 COULD NOT VALIDATE THEM - n=31,079 excursions, see
# CALIBRATION.md finding 11. The ratio is measured correctly now (three bugs fixed, and
# the exactly-1.000 cluster is gone) but it carries no information about the thing these
# thresholds claim to separate. Its apparent AUC of 0.5808 against CONTINUED/REVERTED
# collapses to 0.5035 with excursion distance held fixed and to 0.4868 as the control
# tightens to fifty strata, while distance keeps 0.6619 with the ratio held fixed. The
# ratio is a PROXY for distance and adds nothing to it. Re-measured at every candle of
# every excursion (348,103 events) in case acceptance needed time to build, as the theory
# says it should: flat at ~0.49 from candle 3 to candle 40+.
#
# So these stay at their reasoned values rather than being moved to whatever the outcome
# data flatters, which would be fitting a threshold on a quantity known to be inert. They
# are now a STRUCTURAL device - the gap is what makes S2 and S3 mutually exclusive - and
# Phase 3's ablation carries a required arm that removes the gate entirely. If removing it
# costs nothing, it should go.
ACCEPT_MIN_VOLUME_RATE_RATIO = env_float("ACCEPT_MIN_VOLUME_RATE_RATIO", 0.80)
REJECT_MAX_VOLUME_RATE_RATIO = env_float("REJECT_MAX_VOLUME_RATE_RATIO", 0.45)

# WHICH QUANTITY DECIDES THE VERDICT. "duration" (default) or "volume_rate".
#
# Acceptance in Market Profile is a claim about TIME - price is accepted when it trades
# there LONG ENOUGH for value to develop. The volume-rate ratio above was a convenience
# proxy for that, and Phase 0 measured the proxy as inert while the time-based measure it
# was standing in for is strongly informative. Both were put through the identical control
# on 14,268 excursions, one independent read each, session-clustered errors, five seeds:
#
#                                  alone          within distance strata
#   time outside value             0.658-0.669    0.606-0.619   <- informative, stable
#   volume rate ratio              0.545-0.550    ~0.50         <- null
#
# Against the scale-free directional label the continuation rate rises monotonically
# across every duration band, with non-overlapping intervals at the extremes:
#
#   0.8h+  28.4%    2.0h+  46.8%    5.2h+  61.7%
#   1.2h+  35.9%    3.2h+  53.6%    8.5h+  73.1%
#
# The crossover from reversion-dominant to continuation-dominant sits at 2-3 hours, which
# is the auction-theory claim stated as a number. Thresholds are set from the DISTRIBUTION
# (p40 and p75 of duration at a decision point) rather than from whichever band scored
# best, and Phase 1 measures them in R.
#
# "volume_rate" is kept, not deleted, because it is the ablation arm: the Phase 1 run
# already completed under it is the control this replaces, and a setting that can be
# switched is evidence where a deleted branch is only an assertion.
ACCEPT_DISCRIMINATOR = env_str("ACCEPT_DISCRIMINATOR", "duration").strip().lower()

# Confirmation candles closed outside value, expressed as durations (PLAN item 7) so a
# confirm-interval ablation rescales these rather than silently redefining them. At the
# 15m default: 195min = 3.25h = 13 candles, 75min = 1.25h = 5 candles - the exact prior
# values. The GAP between them is still what makes S2 and S3 mutually exclusive - inside
# it the verdict is PENDING and neither fires.
ACCEPT_MIN_CANDLES_OUTSIDE = env_int("ACCEPT_MIN_CANDLES_OUTSIDE",
                                     candles_for_minutes(195))
REJECT_MAX_CANDLES_OUTSIDE = env_int("REJECT_MAX_CANDLES_OUTSIDE",
                                     candles_for_minutes(75))

# THE DENOMINATOR MUST NOT CONTAIN THE NUMERATOR, and the paragraph above understated
# how far that goes. Averaging over the WHOLE session includes the excursion being
# measured, so writing the session as M excursion candles at rate r_out and N-M
# in-value candles at r_in:
#
#     session_rate = (M*r_out + (N-M)*r_in) / N      ->   ratio -> 1.0 as M -> N
#
# An excursion therefore dilutes its own reference in proportion to its own LENGTH,
# biasing hardest on exactly the long excursions S3 is built to trade. The extreme
# case turned up in the Phase 0 sweep: a session that OPENS outside yesterday's value
# and never trades back inside has M == N, so the ratio is 1.000 EXACTLY, carrying no
# information - and it was 22% of all measured excursions, every one of them clearing
# ACCEPT_MIN_VOLUME_RATE_RATIO and arming S3. Since opening outside value is precisely
# what makes S3 eligible, the degenerate case was concentrated in the traded population.
#
# So the baseline comes from this session's candles BEFORE the excursion started, and
# this is how many of them are required before their mean is trusted. Below it, the
# PRIOR session's rate is used instead; with neither, the verdict is NO_BASELINE and
# nothing trades. 90 minutes of independent reference - 6 candles at the 15m default,
# rescaled by PLAN item 7 like the two thresholds above.
# PROVISIONAL - Phase 0 calibrates it against how often each source ends up used.
ACCEPT_MIN_BASELINE_CANDLES = env_int("ACCEPT_MIN_BASELINE_CANDLES",
                                      candles_for_minutes(90))


# ------------------------------------------------------------------- setups

# CONFIRM_INTERVAL and CONFIRM_INTERVAL_MINUTES are defined earlier, immediately
# ahead of the acceptance thresholds - every one of which is a candle count that
# needs this value to mean a fixed duration. Not redefined here.

# --- shared confirmation vocabulary (confirm.py) -------------------------
CONFIRM_WICK_MIN_ATR = env_float("CONFIRM_WICK_MIN_ATR", 0.15)
# 30 minutes at the 15m default = 2 candles, the exact prior value. PLAN item 7.
CONFIRM_CONSECUTIVE_CANDLES = env_int("CONFIRM_CONSECUTIVE_CANDLES",
                                      candles_for_minutes(30))
CONFIRM_ENGULF_MIN_BODY_RATIO = env_float("CONFIRM_ENGULF_MIN_BODY_RATIO", 1.0)
CONFIRM_DELTA_DIVERGENCE_MIN = env_float("CONFIRM_DELTA_DIVERGENCE_MIN", 0.15)

# --- S1-POC: POC rotation fade -------------------------------------------
# DISABLED BY DEFAULT. Phase 1 + matched random control (CALIBRATION.md finding 15):
# n=308, mean -0.26R, no separation from a same-symbol/session/direction/geometry random
# entry (lift +0.05R, 95% CI [-0.17,+0.28] - contains zero). This setup's specific entry
# timing adds nothing measurable over just being at the right location. Not proven
# harmful, unlike S3-BRK below - proven UNPROVEN, which the pre-committed rule treats the
# same way: disabled rather than loosened, pending a fix or more data.
S1_POC_ENABLED = env_bool("S1_POC_ENABLED", "False")

# How far outside the prior value area the session must have opened, as a fraction
# of the VALUE WIDTH - deliberately not in ATR.
#
# WHY NOT ATR, found by construction. S1 needs the open to be outside VALUE but
# inside the prior RANGE (opening beyond the whole range is an imbalance statement,
# not a stretched balance - see poc_rotation.py). That band is bounded by the
# session's tail: value covers roughly 50-70% of a balanced session's range, so each
# tail is only ~15-25% of the range, i.e. ~0.2-0.5 of the value width.
#
# An ATR-denominated threshold ignores that geometry. With ATR ~ the daily range, a
# 0.25 ATR requirement asks the open to sit further outside value than the tail is
# wide - so nearly every qualifying open is ALSO outside the range, and S1 rejects
# itself. The first draft had exactly that bug: the two conditions were close to
# mutually exclusive, and S1 would have been near-unreachable on precisely the
# balanced sessions it is designed for.
#
# Scaling by value width makes the threshold commensurate with the band it has to
# fit inside. 0.15 is PROVISIONAL and sits comfortably below the ~0.2-0.5 tail width
# measured on the balanced fixture.
S1_MIN_OPEN_DISTANCE_VA_FRACTION = env_float(
    "S1_MIN_OPEN_DISTANCE_VA_FRACTION", 0.15)
S1_TOUCH_TOLERANCE_ATR = env_float("S1_TOUCH_TOLERANCE_ATR", 0.10)
# TESTED AT 2 (CALIBRATION.md finding 25): full Phase 1 + matched-random control.
# S1-POC's point estimate nearly doubled (+0.05R -> +0.10R lift) but still contains
# zero (95% CI [-0.14,+0.34], n=285 vs the default's 308 - fewer trades since 2-of-4
# is more selective). NOT read as an improvement: the BUY split moved to +0.21R while
# SELL stayed flat at -0.005R, and that specific asymmetry is already explained by
# finding 17's confirmed market-drift confound rather than by the confirmation count -
# a real fix should not depend on which half of the corpus's drift a trade happened to
# sit in. S1-LVN's already-thin sample (n=14 at the default) shrank further to n=7 -
# raising this trades off directly against S1-LVN's own need for a WIDER sample
# (queued separately), since both setups read this same threshold.
S1_MIN_CONFIRMATIONS = env_int("S1_MIN_CONFIRMATIONS", 1)
S1_STOP_BUFFER_ATR = env_float("S1_STOP_BUFFER_ATR", 0.30)
S1_STOP_BUFFER_TICKS = env_int("S1_STOP_BUFFER_TICKS", 3)
S1_INVALIDATE_ATR = env_float("S1_INVALIDATE_ATR", 0.50)
# Balance trade: only offered on a balanced (D) profile.
S1_REQUIRED_SHAPES = env_str_list("S1_REQUIRED_SHAPES", ["D"])

# WHERE THE STOP GOES: "buffer" (default, current behaviour) or "lvn".
#
# THE DIAGNOSIS THIS ANSWERS (CALIBRATION.md finding 15, and poc_rotation.py's own
# module docstring, written before this was ever tested): the POC is the price of
# maximum ACCEPTANCE - both sides are willing to transact there, which is exactly
# why price rotates around it. A stop parked a small buffer beyond the POC sits in
# the single densest, most rotation-prone region of the whole distribution: ordinary
# two-sided business there is enough to run it, independent of whether the setup's
# actual premise (a clean rejection) was right or wrong.
#
# "lvn" moves the stop to just beyond the nearest LOW-volume node on the side that
# would invalidate the trade - a thin area is where one side is largely absent, so a
# stop placed past one is more likely to be hit by the setup genuinely failing than
# by ordinary two-sided churn. Falls back to the value-area edge (still structural,
# just less selective) when no qualifying LVN exists between the POC and the prior
# session's own high/low - the search never reaches past the session's own recorded
# range. A volatility floor (S1_STOP_LVN_FLOOR_ATR) stops a very close LVN producing
# a stop that is tighter than the noise floor; the existing STOP_MAX_ATR gate already
# catches the opposite failure (an LVN or edge so far out the trade is not worth
# taking), so no separate ceiling is added here.
#
# MEASURED AND REJECTED (CALIBRATION.md finding 20). Full 120-symbol Phase 1 +
# matched-random control, S1_STOP_MODE="lvn": n=127 (down from 308 under "buffer" on
# the identical symbol universe), mean -0.50R (worse than "buffer"'s -0.26R), lift
# -0.06R vs control, 95% CI [-0.35,+0.23] - no separation, and BUY/SELL splits are
# both non-positive (-0.14R, -0.01R). The diagnosis (POC sits in the densest,
# rotation-prone zone) may still be correct; this specific fix is not what resolves
# it. The mechanism that sank it: under fixed_r targeting the target scales with the
# stop distance, and the LVN/value-edge reference sits much farther from the POC than
# the old buffer, so the wider target takes far longer to resolve - resolved sample
# collapsed from 308 to 127 (59% censored vs the buffer arm's baseline), and the
# censoring is not random (trades needing the most room to work are exactly the ones
# most likely to run out of data first). Kept at "buffer", the default, since nothing
# here beats it. Re-test only under TARGET_MODE="structural" (removes the scaling
# link between stop and target) before concluding the LVN idea itself is dead.
# Re-tested under TARGET_MODE="structural" (finding 21), which removes the
# stop-widens-target link above: population shrank to n=95 (GATE:REWARD_BELOW_MINIMUM
# now trips more, a real filtering effect, not censoring), lift still -0.03R, 95% CI
# [-0.38,+0.31], BUY/SELL disagree in sign. No fix for S1-POC found yet on either lever.
S1_STOP_MODE = env_str("S1_STOP_MODE", "buffer").strip().lower()
S1_STOP_LVN_FLOOR_ATR = env_float("S1_STOP_LVN_FLOOR_ATR", 0.6)

# --- S1-LVN: LVN rejection into the POC ----------------------------------
# The variant auction theory actually predicts: enter at the THIN area, target
# the thick one. Shares conditions 1-2 with S1-POC.
#
# DISABLED BY DEFAULT. Phase 1: n=14 over 120 symbols x ~180 sessions - the setup is
# real but rare, and 14 trades cannot pass or fail a phase gate; the point estimate
# (-0.41R) is not to be read as a finding either way. Disabled for the same reason an
# unmeasured claim is disabled, not because it was measured and found wanting: there is
# no evidence yet, in either direction, and the pre-committed rule does not loosen on an
# absence of evidence. Needs a wider universe or a longer history before its own gate is
# meaningful.
S1_LVN_ENABLED = env_bool("S1_LVN_ENABLED", "False")
S1_LVN_TOUCH_TOLERANCE_ATR = env_float("S1_LVN_TOUCH_TOLERANCE_ATR", 0.10)
S1_LVN_STOP_BUFFER_ATR = env_float("S1_LVN_STOP_BUFFER_ATR", 0.15)
# How far the POC must be from the LVN to be worth targeting, as a fraction of the
# VALUE WIDTH. The fourth and most clear-cut instance of the ATR mis-scaling: BOTH
# levels sit inside the value area, so their separation is bounded by the value width
# by construction. On BTCUSDT that width is 0.14 ATR, so the first draft's 0.30 ATR
# requirement was not merely strict - it was UNSATISFIABLE, and S1-LVN could never
# fire on any symbol whose value area is narrower than 0.30 ATR (which is most of
# them). The live reject read "poc only 0.120 ATR from lvn, need 0.3".
#
# The general rule this establishes: a threshold measuring a distance BETWEEN TWO
# PROFILE LEVELS must be denominated in value width. ATR is the right unit only for
# distances that are genuinely about volatility - stop buffers, touch tolerances.
S1_LVN_MIN_POC_DISTANCE_VA_FRACTION = env_float(
    "S1_LVN_MIN_POC_DISTANCE_VA_FRACTION", 0.25)

# DISABLED BY DEFAULT, BUT SEE THE UPDATE BELOW - this is the one setup with a real
# positive result so far, gated on a config value that is not yet this file's own
# default.
#
# Phase 1 + matched random control (CALIBRATION.md finding 15), measured under BOTH
# acceptance discriminators as a replication check:
#   volume_rate arm   n=448   mean -0.27R   lift +0.07R  95% CI [-0.09,+0.22]
#   duration arm      n=1462  mean -0.31R   lift +0.08R  95% CI [-0.02,+0.19]
# Both intervals contain zero under fixed_r targeting; the duration arm's 3.3x
# larger, better-measured population did not resolve it either way.
#
# UNDER TARGET_MODE="structural" IT DOES (CALIBRATION.md finding 19): n=936, lift
# +0.15R, 95% CI [+0.02,+0.28] - excludes zero, BEATS CONTROL, the only setup in this
# project to clear the phase gate so far. Marginal (the lower bound sits close to
# zero) but REPLICATES across every independent split checked: BUY +0.18R and SELL
# +0.12R separately, early-half +0.15R and late-half +0.16R separately - all four
# positive, none flipping sign, which is stronger evidence than the pooled p-value
# alone. Mechanistically coherent too: structural mode aims most of these trades at
# the nearest untested POC rather than a blind 2R, which is exactly what auction
# theory predicts a returning move should reach.
#
# PHASE 2 NOW COMPLETE, BOTH LEGS, AND BOTH PASS.
#
# Cost stress (finding 22): the lift is invariant to the cost model - +0.1514 to
# +0.1515R across baseline, 2x/3x slippage, and taker-fills-on-both-legs, because a
# harsher cost model drags the twins down by nearly the same amount. Not a
# lenient-cost artifact.
#
# Second, independent historical window (finding 23): replayed against
# 2026-01-30..2026-05-29 (non-overlapping with the original corpus, same frozen
# universe). Alone it does not independently clear (n=201, lift +0.20R, 95% CI
# [-0.06,+0.46] - smaller population, mostly INSUFFICIENT_DAILY_HISTORY since many
# universe symbols are recent listings) but the POINT ESTIMATE IS AS LARGE OR LARGER
# than window 1's, replicates in direction across all four BUY/SELL/early/late splits
# (none flip), and S1-POC/S3-BRK reproduce their established (unrescued / harmful)
# dispositions exactly on this new window - the regression check that makes this
# result trustworthy rather than an artifact of how the second corpus was built.
# POOLED across both windows: n=1137, lift +0.1611R, 95% CI [+0.0440,+0.2781] -
# BEATS CONTROL, tighter than either window alone.
#
# STILL DISABLED, on purpose, one step further: this is now the strongest evidence
# behind any result in this project, but "beats control" is not "is profitable" -
# pooled win rate ~28.7% remains below the ~34.3% breakeven this system's 2:1
# reward:risk needs. Flipping this default is a live-capital decision, not a replay
# decision, and should be made deliberately with that distinction stated plainly,
# not inherited silently from a config default. TARGET_MODE's own default should
# move to "structural" alongside this one, for the same reason.
S2_ENABLED = env_bool("S2_ENABLED", "False")
# How far beyond the value bound the excursion must reach before it is worth fading,
# as a fraction of the VALUE WIDTH. Same scaling lesson as
# S1_MIN_OPEN_DISTANCE_VA_FRACTION, and the same bug found the same way.
#
# MEASURED ON LIVE SESSIONS, which is what exposed it. Daily ATR is the 14-day
# average range; a single session's value width is a fraction of ONE day's range, and
# on a quiet day the two differ by ~10x:
#     BTCUSDT  ATR 2471.77   prior session range 708.60   value width 345.80 (0.14 ATR)
#     SOLUSDT  ATR    5.60   value width 1.43 (0.26 ATR)
# So the first draft's 0.35 ATR asked SOL's price to travel 1.96 - about 1.4x the
# entire value area - before the fade was allowed. The setup would essentially never
# fire, and the reject journal showed exactly that: "excursion 0.054 ATR < 0.35".
#
# 0.20 of value width is a real excursion without being an absurd one. PROVISIONAL.
S2_MIN_EXCURSION_VA_FRACTION = env_float("S2_MIN_EXCURSION_VA_FRACTION", 0.20)
S2_STOP_BUFFER_ATR = env_float("S2_STOP_BUFFER_ATR", 0.20)

# WHERE THE STOP GOES: "extreme" (default, current) or "lvn" - the same idea as
# S1_STOP_MODE, adapted to this setup's own anchor. "extreme" already places the
# stop beyond the excursion's own high/low with an ATR floor, which is already a
# structural reference rather than an arbitrary offset - this is not the same
# diagnosed weakness S1-POC has (the excursion extreme sits OUTSIDE value, not in
# the profile's densest region). "lvn" is the variant worth testing anyway: a thin
# area further beyond the extreme, when one exists, is a more selective invalidation
# than a fixed buffer past whatever price the excursion happened to reach. Falls
# back to today's exact behaviour (extreme + buffer) when no LVN qualifies, so this
# can only ever be as tight as the current default, never tighter.
#
# MEASURED AND REJECTED (CALIBRATION.md finding 20), same run as S1_STOP_MODE above.
# S2_STOP_MODE="lvn": n=504 (down from 1462 under "extreme"), mean -0.55R (worse than
# "extreme"'s -0.31R), lift +0.06R vs control, 95% CI [-0.09,+0.20] - no separation,
# and BUY/SELL splits disagree in sign (+0.14R / -0.02R), so it does not even
# replicate internally the way finding 19's structural-target result did. Same
# censoring mechanism as S1-POC: resolved sample collapsed from 1462 to 504 (66%
# censored) because the wider stop widens the fixed_r target with it. Kept at
# "extreme", the default.
# Re-tested under TARGET_MODE="structural" (finding 21): n=206 (down from finding 19's
# 936 under the default "extreme" stop), lift +0.18R, 95% CI [-0.09,+0.45] - point
# estimate is AS GOOD OR BETTER than the +0.15R that already beat control, but the
# smaller sample keeps the CI from clearing zero. Replicates in direction across all
# four splits (BUY +0.30, SELL +0.11, early +0.21, late +0.15 - none flip), same
# pattern finding 19 relied on, just underpowered. Read as an echo of the same signal,
# not a separate finding - more data would settle it, not a different config.
S2_STOP_MODE = env_str("S2_STOP_MODE", "extreme").strip().lower()
S2_STOP_LVN_FLOOR_ATR = env_float("S2_STOP_LVN_FLOOR_ATR", 0.6)

S2_MAX_PER_SESSION = env_int("S2_MAX_PER_SESSION", 2)

# Quality overlays. Both express a MARKET OPINION, so both default OFF and are
# switched on only by a Phase 3 ablation result.
#   BALANCED_SHAPE  reverting into a D-shape is a balance trade; reverting against
#                   a one-sided P/b/trend profile is fading a directional auction.
#   POOR_EXTREME    an excursion into an extreme that showed EXCESS was already
#                   defended by the auction; one into a POOR extreme (no tail,
#                   volume still building there) is likelier to extend. Identical
#                   geometry, opposite prognosis - which is exactly the kind of
#                   claim that must be measured rather than assumed.
# PHASE 3 (CALIBRATION.md finding 18): tested as a population filter against the
# matched random control. No rescue - lift +0.10R [-0.05,+0.25], still contains zero,
# barely moved from the +0.08R unfiltered baseline.
S2_REQUIRE_BALANCED_SHAPE = env_bool("S2_REQUIRE_BALANCED_SHAPE", "False")
# PHASE 3: the theorised population is almost EMPTY on this corpus - only 5 of 2,438
# S2-VAR candidates tested a poor (no-tail) extreme. Too thin to judge either way,
# not tested and found wanting; would need a much larger corpus to mean anything.
S2_REQUIRE_POOR_EXTREME = env_bool("S2_REQUIRE_POOR_EXTREME", "False")

# --- S3-BRK: value area breakout continuation ----------------------------
# DISABLED BY DEFAULT, AND MORE STRONGLY THAN THE OTHER THREE. Phase 1 + matched random
# control (CALIBRATION.md finding 15), replicated under both acceptance discriminators:
#   volume_rate arm   n=2210  mean -0.12R   control +0.26R   lift -0.39R  95% CI [-0.60,-0.17]
#   duration arm      n=1965  mean -0.15R   control +0.33R   lift -0.47R  95% CI [-0.71,-0.24]
# The control isolates ONLY the entry bar - same symbol, session, direction, stop
# distance and target multiple, entered at a uniformly random bar instead of S3's
# pullback-then-continuation trigger. Both CIs exclude zero on the NEGATIVE side: this
# is not "no measured edge", it is "the entry-timing logic picks worse entries than
# chance at the same location", replicated across two independent measurement regimes.
# Mechanically consistent with waiting for acceptance, a pullback AND continuation past
# the pre-pullback high before entering - by the time every condition is satisfied, a
# real part of the favourable move has often already happened, so a random bar in the
# same excursion does at least as well. Also: control win rate ~43-45% vs S3's own
# ~29-30% (win rate n=2210-2694, Wilson 95% CI excludes overlap) - not a payoff-shape
# artifact, an entry-selection one. Same asymmetric BUY/SELL split (SELL far worse) and
# the same early/late sign-flip in both runs; this is not an acceptance-discriminator
# artifact, since it holds either way acceptance is measured. Do not loosen this one on
# a partial retest - it needs a redesigned entry rule, not a threshold nudge.
#
# THE REDESIGNED ENTRY RULE WAS BUILT AND TESTED - see S3_ENTRY_MODE below. It closed
# most of the gap (lift -0.47R -> -0.12R, CI now contains zero) without reaching BEATS
# CONTROL, and exposed that the SELL-side weakness lives in the control too, not only
# in S3's own entries. Still disabled either way.
S3_ENABLED = env_bool("S3_ENABLED", "False")
# The break-distance floor. VALUE WIDTH is the primary criterion; the ATR term is a
# small NOISE FLOOR and must not be the binding constraint.
#
# WHY IT WAS WRONG, measured live. At 0.50 ATR the ATR term dominated by 14x on
# BTCUSDT - it demanded a break of 1235.89 where the value-width term asked for 86.45,
# and where the whole prior session had a range of 708.60. S3 could not fire on a
# quiet-day profile at all.
#
# The deeper point: ACCEPTANCE is the discriminator here, not distance. Three
# consecutive closes outside plus a normal volume rate is what separates discovery
# from a spike; the distance floor exists only to skip the trivially small. Setting it
# high does not make the setup more selective, it makes it blind.
S3_BREAK_MIN_ATR = env_float("S3_BREAK_MIN_ATR", 0.10)
S3_BREAK_MIN_VA_FRACTION = env_float("S3_BREAK_MIN_VA_FRACTION", 0.30)
S3_PULLBACK_MAX_DEPTH = env_float("S3_PULLBACK_MAX_DEPTH", 0.25)
S3_STOP_BUFFER_ATR = env_float("S3_STOP_BUFFER_ATR", 0.25)
S3_REQUIRE_DEVELOPING_POC_MIGRATION = env_bool(
    "S3_REQUIRE_DEVELOPING_POC_MIGRATION", "True")

# Move a stop that lands INSIDE a low-volume node to just beyond it. Defaults ON
# because it is mechanical rather than an opinion: price traverses an LVN fast
# precisely because there is nothing there to transact against, so a stop placed
# inside one is likely to be run on noise rather than on the setup actually
# failing. This widens risk and therefore reduces size - it never increases
# exposure - which is why it is safe to have on before it is measured.
S3_WIDEN_STOP_PAST_LVN = env_bool("S3_WIDEN_STOP_PAST_LVN", "True")

# WHICH BAR S3 ENTERS ON: "confirmed" (default, current behaviour) or "accepted".
#
# The diagnosis behind this flag, from the matched-random control: real S3 entries
# score like twins entered AFTER them (-0.15R, 31% win) and nothing like twins entered
# BEFORE them (+0.83R, 62% win), median 6.2h earlier, in the SAME session and
# direction. Waiting for a pullback and a confirmed new high past it is waiting past
# the part of the move that pays.
#
# "accepted" tests the earliest entry the system can honestly justify without
# hindsight: the bar acceptance itself first confirms (verdict.accepted flips True),
# BEFORE any pullback exists to wait for. Stop moves from the pullback extreme (which
# does not exist yet in this mode) to the value bound the excursion broke from -
# ACCEPTED, NOT ARGUED: acceptance requires ACCEPT_MIN_CANDLES_OUTSIDE candles before
# it can fire at all, so this entry is already ~3.25h and one full acceptance
# measurement removed from the break itself - it is earlier than the confirmed mode,
# not undisciplined. The value bound is also the level whose failure (price back
# inside value) is what falsifies the trade under acceptance's own logic, so it is a
# principled stop reference, not an arbitrary tighter one.
#
# MEASURED, Phase 1 + matched random control, full 120-symbol corpus, duration
# discriminator (CALIBRATION.md finding 16):
#   pooled  n=2857   mean -0.11R   control +0.01R   lift -0.12R  95% CI [-0.31,+0.07]
#   BUY     n=1506   mean +0.13R   control +0.28R   lift -0.15R  95% CI [-0.43,+0.14]
#   SELL    n=1351   mean -0.38R   control -0.28R   lift -0.10R  95% CI [-0.39,+0.20]
# The diagnosis was RIGHT about the mechanism and WRONG about being sufficient. Lift
# moved from clearly negative under "confirmed" (-0.39R and -0.47R, both CIs excluding
# zero, replicated across both acceptance discriminators) to a CI that now CONTAINS
# ZERO - the entry no longer loses to a random bar at the same location, but it does
# not beat one either. "No separation" is a real improvement over "worse than
# control", and not a pass: the phase-gate bar is BEATS CONTROL, and this does not
# clear it.
#
# THE BUY/SELL SPLIT DID NOT CLOSE, AND IT MOVED TO A DIFFERENT PLACE. Under
# "confirmed" the split was in the treatment only. Here it is in the CONTROL too -
# random entries in SELL excursions score -0.28R, not the +0.26/+0.33R random entries
# score everywhere else this system has measured them. That means the SELL-side
# weakness is a property of WHICH excursions and WHERE, not of when within them S3
# enters - re-timing the entry cannot fix a problem that survives in the twins.
#
# So: kept at "confirmed" as the default (neither mode has earned a change from the
# status quo), both remain available for the next entry-design attempt, and S3-BRK
# stays disabled under either. This is now the second setup in DISABLED-UNPROVEN
# rather than DISABLED-HARMFUL - closer to shippable, not shipped.
S3_ENTRY_MODE = env_str("S3_ENTRY_MODE", "confirmed").strip().lower()


# --------------------------------------------------------- opinion gates
# Every flag here expresses a view about the MARKET rather than about measurement
# quality, so every one defaults False. They are switched on individually by a
# Phase 3 ablation that shows a replicated positive contribution on the gated
# population - never by argument, however plausible the argument.
#
# They are wired into every setup's gate profile already (gates/profiles.py), so
# enabling one is a config change and not a code change. That is what makes the
# ablation a sweep rather than a series of edits.

# PHASE 3 ATTRIBUTION, S1-POC and S2-VAR, full corpus (CALIBRATION.md finding 18).
# Every gate below tested individually as a population filter against the matched
# random control, same methodology as findings 15-16: does restricting to the
# gate-passing population turn "no separation" into "beats control"? None do -
# every filtered lift's 95% CI still contains zero, movement from baseline is within
# noise (S2-VAR baseline +0.08R [-0.02,+0.19] moves to +0.07R..+0.10R depending on
# which single gate is applied). S1-POC's shape_opposed result is a true no-op, not
# a null: S1 already requires shape=="D" by construction (poc_rotation.py condition
# 2), so this gate never has anything left to remove there.
GATE_SHAPE_OPPOSED_ENABLED = env_bool("GATE_SHAPE_OPPOSED_ENABLED", "False")

GATE_DELTA_OPPOSED_ENABLED = env_bool("GATE_DELTA_OPPOSED_ENABLED", "False")
# Normalised per-bin taker imbalance (delta/volume, so -1..+1) above which flow at
# the level is treated as opposing the trade. PHASE 3: tested at this threshold,
# finding 18 - no rescue, see above.
GATE_DELTA_OPPOSED_MIN = env_float("GATE_DELTA_OPPOSED_MIN", 0.20)

# PHASE 3: tested via ctx.migration (session-over-session, NOT the separate weekly
# composite despite this flag's gate function being named htf_value_opposed - see
# finding 18). No rescue, same as the other three gates above.
GATE_HTF_VALUE_OPPOSED_ENABLED = env_bool("GATE_HTF_VALUE_OPPOSED_ENABLED", "False")
GATE_STOP_INSIDE_HVN_ENABLED = env_bool("GATE_STOP_INSIDE_HVN_ENABLED", "False")
GATE_TARGET_BEHIND_HVN_ENABLED = env_bool("GATE_TARGET_BEHIND_HVN_ENABLED", "False")

# PLAN item 6: the same discriminator as GATE_DELTA_OPPOSED, widened from the single
# bin the level falls in to a window of MULTIBIN_DELTA_WINDOW_BINS on each side -
# finding 18 tested only the single-bin version, never this one. Recorded
# unconditionally on every S1-POC/S2-VAR candidate as `multi_bin_delta_normalized`
# so it can be swept from stored rows exactly like bin_delta_normalized was; this
# gate itself is not yet part of any Phase 3 sweep.
GATE_DELTA_OPPOSED_MULTIBIN_ENABLED = env_bool("GATE_DELTA_OPPOSED_MULTIBIN_ENABLED",
                                               "False")
GATE_DELTA_OPPOSED_MULTIBIN_MIN = env_float("GATE_DELTA_OPPOSED_MULTIBIN_MIN", 0.20)
MULTIBIN_DELTA_WINDOW_BINS = env_int("MULTIBIN_DELTA_WINDOW_BINS", 2)


# ---------------------------------------------------------------- targeting

# "fixed_r" tests the source material's claim as stated. "structural" uses the
# levels the profile actually produces (POC, opposite value edge, LVN, naked
# POC) and is the measured challenger - a fixed R target will regularly sit
# inside a high-volume shelf price has no reason to cross.
#
# MEASURED (CALIBRATION.md finding 19): full 120-symbol Phase 1 under "structural"
# for all four setups. S2-VAR BEATS CONTROL under it (lift +0.15R, 95% CI
# [+0.02,+0.28]) where it did not under "fixed_r" - the only positive Phase 1 result
# in this project so far. S1-POC moves in the same direction but does not clear the
# bar (+0.09R, CI still contains zero). Still defaults "fixed_r" pending Phase 2 and
# a second corpus on the S2-VAR result (see S2_ENABLED's comment) - the default
# should flip once that work lands, not before.
TARGET_MODE = env_str("TARGET_MODE", "fixed_r")
TARGET_FIXED_R = env_float("TARGET_FIXED_R", 2.0)
# When structural targeting yields less than this, the setup is skipped rather
# than taken at a poor reward.
TARGET_MIN_R = env_float("TARGET_MIN_R", 1.2)

# PLAN item 17: enrich structural_target()'s candidate set with HVN peaks and the
# prior session's opposite extreme (high for a BUY, low for a SELL - the full
# traded range, not just the value-area edge). Measured and fully replicated
# across 3 independent windows with no sign flips (findings 46/47): win rate
# 28.2% -> 32.3%/37.3%/36.3%, pooled-3-window control lift +0.1971
# [+0.1295,+0.2648] BEATS CONTROL, and - the bar the original candidate set
# itself needed findings 19/23/37 to clear - both BUY and SELL independently
# beat control when pooled across windows. Defaulted True on that evidence.
TARGET_INCLUDE_HVN = env_bool("TARGET_INCLUDE_HVN", "True")
TARGET_INCLUDE_PRIOR_EXTREME = env_bool("TARGET_INCLUDE_PRIOR_EXTREME", "True")

# PLAN item 25: the diagnosis behind the system's low live win rate. Win rate
# falls monotonically as target distance grows (44.8% at 1.2-1.5R down to 7.8%
# at 3R+), and `structural_target()`'s OWN top preference - an untested naked
# POC - ends up chosen on 63-66% of S2-VAR trades at an average 2.83R away,
# winning only ~22%, while the session's own POC/VAH/VAL (1.4-1.5R average)
# win 38-49% of the time. Default OFF: must be measured as a new variant
# against the proven baseline (findings 19/23/37), never a silent change to
# what TARGET_MODE="structural" already does.
TARGET_PREFER_NEAR_LEVEL = env_bool("TARGET_PREFER_NEAR_LEVEL", "False")


# ------------------------------------------------- state machine / arbitration
# See state_machine.py. S2 and S3 trade the same location in OPPOSITE
# directions; they are resolved by the acceptance measurement, not by a
# priority list.

# "sequential" (default, conservative): an open balance position runs to its own
# stop even when acceptance later confirms against it.
# "flip": acceptance against an open balance position closes it and arms the
# continuation setup. Measured in Phase 4 - the spread between these is large
# enough that it must not be a designer's guess.
ARBITER_MODE = env_str("ARBITER_MODE", "sequential")

# S1-POC vs S1-LVN, the only genuine conflict the state machine has to resolve -
# both can fire on one session when a thin area sits close to the POC.
#
# Defaults to LVN_FIRST because auction theory PREDICTS it: a low-volume node is
# where a reaction is mechanically likely (one side absent, nothing to transact
# against), while the POC is where price rotates. That is a reasoned prior and NOT a
# result. Phase 1's head-to-head decides it, and stating the prior explicitly here is
# what makes a later change visibly evidence-driven rather than a quiet preference.
# "POC_FIRST" is the alternative; anything else falls through to best net R.
S1_ARBITRATION = env_str("S1_ARBITRATION", "LVN_FIRST")

MAX_SETUPS_PER_SESSION_PER_SYMBOL = env_int("MAX_SETUPS_PER_SESSION_PER_SYMBOL", 3)

# Open positions survive the session boundary on their original stop/target -
# those levels were derived from the profile current at entry. Pending
# candidates do NOT survive: their reference profile is no longer current.
CLOSE_AT_SESSION_END = env_bool("CLOSE_AT_SESSION_END", "False")


# ----------------------------------------------------------------------- risk

RISK_PCT = env_float("RISK_PCT", 0.0025)

# Notional equity for OBSERVATION RUNS ONLY, used when the account cannot be read and
# TRADING_ENABLED is False. Without it a credential-free observation run sizes nothing -
# size() refuses every candidate as "equity is zero" - so the journal fills with sizing
# failures instead of the sized candidates the run exists to record, and none of it can
# be compared against a replay.
#
# It can never size a real order: the fallback is gated on TRADING_ENABLED being False.
# With trading enabled and no account access, equity stays 0 and the system refuses to
# trade, which is the only safe response to not knowing what it holds.
PAPER_EQUITY = env_float("PAPER_EQUITY", 10_000.0)          # 0.25% of equity per trade
LEVERAGE = env_int("LEVERAGE", 5)
MARGIN_TYPE = env_str("MARGIN_TYPE", "ISOLATED")

MAX_CONCURRENT_POSITIONS = env_int("MAX_CONCURRENT_POSITIONS", 6)
# Alt perps are close to a one-factor market: ten alt longs is one leveraged
# bet on BTC, not a diversified book. Capped per DIRECTION for that reason.
MAX_CONCURRENT_PER_DIRECTION = env_int("MAX_CONCURRENT_PER_DIRECTION", 4)
MAX_POSITIONS_PER_SYMBOL = 1                       # structural, not tunable

DAILY_LOSS_LIMIT_R = env_float("DAILY_LOSS_LIMIT_R", 3.0)
CONSECUTIVE_LOSS_LIMIT = env_int("CONSECUTIVE_LOSS_LIMIT", 4)

# Stop geometry bounds. Below the tight bound R is meaningless because the cost
# of trading exceeds the risk unit; above the wide bound position size collapses.
STOP_MIN_COST_MULTIPLE = env_float("STOP_MIN_COST_MULTIPLE", 3.0)
STOP_MAX_ATR = env_float("STOP_MAX_ATR", 2.0)


# ------------------------------------------------------------------ execution

# Profile setups enter AT A LEVEL, which suits passive execution. GTX is
# Binance's post-only time-in-force: it is rejected outright rather than
# crossing the book, which is the behaviour we want.
ENTRY_ORDER_TYPE = env_str("ENTRY_ORDER_TYPE", "LIMIT")
ENTRY_TIME_IN_FORCE = env_str("ENTRY_TIME_IN_FORCE", "GTX")
ENTRY_TIMEOUT_SECONDS = env_int("ENTRY_TIMEOUT_SECONDS", 900)
# There is deliberately no ENTRY_MARKET_FALLBACK. It existed as a flag that nothing
# read, which is the worst of both worlds: an operator could set it true and believe
# unfilled entries were being chased. They are not, and should not be - a missed fill
# costs nothing, while a bad fill costs real R against a stop anchored to a level.
# Crossing the spread to enter a setup defined BY a price level contradicts the premise
# of every setup here. To change that, change ENTRY_ORDER_TYPE deliberately.

# HOW LONG open() MAY BLOCK WAITING FOR A FILL, as opposed to how long the order is
# allowed to rest (ENTRY_TIMEOUT_SECONDS above).
#
# These were originally the same number, and that was a serious bug. Entries are posted
# GTX at the setup's reference price, so resting unfilled is the ORDINARY outcome - the
# router's contract is that it never chases. Blocking for the full timeout therefore
# stalled the whole bot for fifteen minutes in the normal case: reconcile never ran, so
# open positions went unmonitored, a position that had gone flat kept its survivor order
# resting, a target that failed to place was never retried, and several unfilled entries
# in one scan multiplied the stall.
#
# So an entry in flight is tracked STATE now, advanced once per cycle by
# PositionManager.advance_pending(). This short window exists only to catch the common
# case of an order that fills at once, because protecting immediately is strictly better
# than protecting one cycle later. Keep it well under SCAN_INTERVAL_SECONDS.
ENTRY_FAST_FILL_SECONDS = env_int("ENTRY_FAST_FILL_SECONDS", 8)
ENTRY_POLL_SECONDS = env_float("ENTRY_POLL_SECONDS", 2.0)
# How far past the level a limit entry may be placed, to gain queue priority
# without materially changing the setup. 0 = exactly at the level.
ENTRY_OFFSET_TICKS = env_int("ENTRY_OFFSET_TICKS", 0)

# MARK_PRICE for stops: the mark is an index-anchored average, so it is far
# harder to wick than last-traded price on a thin alt. CONTRACT_PRICE exists
# for the comparison, not as a recommendation.
STOP_WORKING_TYPE = env_str("STOP_WORKING_TYPE", "MARK_PRICE")
# No TAKE_PROFIT_WORKING_TYPE: the target is a resting LIMIT order, not a trigger order,
# so it has no working type to set. It fills when the BOOK reaches the price. The
# asymmetry with the stop is real and intended, not an omission - see
# OrderRouter.place_target.
# Binance's own guard against triggering a stop on a mark/last divergence.
STOP_PRICE_PROTECT = env_bool("STOP_PRICE_PROTECT", "True")

# No trailing or partial exits in v1: deterministic exits are what make a
# first-touch replay faithful, and every measurement depends on that.
TRAILING_ENABLED = False
PARTIAL_TAKE_PROFIT_ENABLED = False

# Fee model. Taker on both legs is the conservative assumption for R accounting
# even when the entry is posted, because the stop always exits at market.
FEE_MAKER = env_float("FEE_MAKER", 0.0002)
FEE_TAKER = env_float("FEE_TAKER", 0.0005)
SLIPPAGE_MODEL_BPS = env_float("SLIPPAGE_MODEL_BPS", 2.0)

MAX_SPREAD_BPS = env_float("MAX_SPREAD_BPS", 6.0)


# ------------------------------------------------------------------- feed recorder
# Read-only public-stream recorder (ops/feed_recorder.py) for the STACKED and RESTING
# order-flow signals. Records data only; it does not feed any setup yet.

# Empty FEED_SYMBOLS means: the FEED_SYMBOL_COUNT perpetuals with the largest 24h quote
# volume in FEED_QUOTE_ASSET. Set FEED_SYMBOLS to pin the list explicitly.
FEED_SYMBOLS = env_str_list("FEED_SYMBOLS", [])
FEED_SYMBOL_COUNT = env_int("FEED_SYMBOL_COUNT", 50)
FEED_QUOTE_ASSET = env_str("FEED_QUOTE_ASSET", "USDT").upper()
FEED_OUT_DIR = env_str("FEED_OUT_DIR", "feed_data")
FEED_DEPTH_SAMPLE_SECONDS = env_float("FEED_DEPTH_SAMPLE_SECONDS", 5.0)
FEED_SYMBOLS_PER_SOCKET = env_int("FEED_SYMBOLS_PER_SOCKET", 100)


# ------------------------------------------------------------------- runtime

SCAN_INTERVAL_SECONDS = env_int("SCAN_INTERVAL_SECONDS", 60)
REST_THROTTLE_SECONDS = env_float("REST_THROTTLE_SECONDS", 0.15)
REST_MAX_RETRIES = env_int("REST_MAX_RETRIES", 4)
REST_TIMEOUT_SECONDS = env_int("REST_TIMEOUT_SECONDS", 15)
RECONCILE_EVERY_CYCLES = env_int("RECONCILE_EVERY_CYCLES", 1)

DB_PATH = env_str("DB_PATH", "vp_state.sqlite")
PARQUET_DIR = env_str("PARQUET_DIR", "data_cache")
LOG_PATH = env_str("LOG_PATH", "logs/vp_bot.log")
LOG_LEVEL = env_str("LOG_LEVEL", "INFO")
HEARTBEAT_EVERY_CYCLES = env_int("HEARTBEAT_EVERY_CYCLES", 15)


# --------------------------------------------------------------- integrity

def config_hash():
    """Stable hash of every tunable above, stamped onto every journal row.

    Without it, a performance figure spanning a parameter change is an average
    over two different systems and the only honest aggregation is GROUP BY this
    value. Credentials are excluded so the hash can be logged freely.
    """
    import hashlib
    import json

    secret = {"BINANCE_API_KEY", "BINANCE_API_SECRET"}
    payload = {
        key: value
        for key, value in sorted(globals().items())
        if key.isupper() and key not in secret
        and isinstance(value, (str, int, float, bool, list, type(None)))
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
