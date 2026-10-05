# binance_ai_bot_vp

A standalone volume-profile trading system for Binance USDⓈ-M perpetuals.

**Volume distribution is the sole source of trading premises.** No moving averages,
oscillators, swing-structure patterns, or open interest as directional inputs. Where a
non-profile input appears at all it is either a mechanical safety check (spread,
notional minimums) or a feature recorded for later measurement — never a premise. The
one exception is ATR, used as a *unit of measure* for volatility-scaled distances, not
as a signal.

Independent of every other system: its own repo, its own sub-account, its own API key.

---

## Status

Built and unit-verified. **Not calibrated, not validated, not traded.**

- 53 tests pass (`python -m unittest discover -s tests -t .`)
- The scan pipeline runs end-to-end against live public market data
- Phase 0 is **in progress**: the research harness is built and the corpus is being
  fetched. The sweep has already found one real bug in the acceptance discriminator
  (CALIBRATION.md finding 8) — no threshold has been recalibrated yet
- `TRADING_ENABLED=false` and `USE_TESTNET=true` by default — two separate deliberate
  acts are needed before a real order is possible
- **Every threshold is provisional.** See [CALIBRATION.md](CALIBRATION.md).

---

## The model

A market exists to facilitate trade. Price is an advertisement; **volume is the
validation.** Where a lot of volume trades, price was *accepted*; where little trades,
it was *rejected*. Accumulate that across a session and you get a distribution whose
mode is the **POC** and whose central 70% is the **value area**.

Auctions run in exactly two states, and every setup is a bet on which one is active:

| State | The auction | Profile signature | Posture |
|---|---|---|---|
| **Balance** | Two-sided; rotates around an agreed price | D-shape, wide value, excess both ends | Mean reversion |
| **Imbalance** | One-sided price discovery | Elongated / P / b / value migrating | Continuation |

So the hardest and most valuable job in this system is **not entry timing — it is state
classification.** Entry rules are cheap and largely interchangeable; getting the regime
wrong means taking every trade backwards. A state machine therefore sits at the centre
and the setups hang off it as leaves.

## The four setups

| Setup | Posture | Entry | Stop | Target |
|---|---|---|---|---|
| `S1-POC` | balance | At the prior POC on a rejection | Just beyond the POC | 2R or structural |
| `S1-LVN` | balance | At the nearest LVN between price and the POC | Just beyond the thin zone | **The POC** |
| `S2-VAR` | balance | Close back inside value after an unaccepted excursion | Beyond the excursion extreme | 2R or structural |
| `S3-BRK` | imbalance | Continuation after an accepted break holds its pullback | Beyond the pullback extreme | 2R or next naked POC |

`S1-LVN` exists because auction theory predicts it and `S1-POC` does not. The POC is
maximum *acceptance*, so what follows from its definition is **rotation** — price
arrives and churns. A sharp rejection is the behaviour of a **low**-volume level, where
one side is absent. So `S1-POC` uses the magnet as an entry and the dense area as a stop
location; `S1-LVN` inverts both. Their head-to-head is the cleanest early test
available: are the profile's *thick* regions or its *thin* regions the tradeable ones?

## Acceptance — the discriminator everything rests on

`S2-VAR` and `S3-BRK` trade the same location in opposite directions. They are resolved
by `acceptance.py`, never by a priority list:

```
outside_rate  = volume transacted beyond the value bound / candles in the excursion
baseline_rate = volume per candle BEFORE the excursion began
                (or the prior session's rate, when the session opened outside value)
ratio         = outside_rate / baseline_rate
```

A spike advertises prices at a fraction of normal volume; real discovery transacts at or
above it. `S2` requires `ratio <= 0.45`, `S3` requires `ratio >= 0.80`, and **the gap
between them is the point** — inside it neither fires, which is what makes them mutually
exclusive rather than a tie-break. A test asserts they can never both produce a
candidate.

**The baseline excludes the excursion, and that is load-bearing.** A denominator
averaged over the whole session contains the numerator, so the ratio drifts toward 1.0
in proportion to the excursion's *length* — and a session that opens outside value and
never trades back has no in-value candles at all, making the ratio exactly 1.000
regardless of volume. That was 22% of real excursions in the Phase 0 sweep, every one
clearing the acceptance threshold on a measurement containing no information. When no
independent baseline exists at all the verdict is `NO_BASELINE` and nothing trades —
never a value, because a ratio of 0.0 reads as the thinnest possible excursion and
would arm S2 in the opposite direction. See CALIBRATION.md finding 8.

A fifth state falls out of this: **failed auction** — acceptance-level volume outside
value that was then reclaimed. Stronger than a thin-spike rejection in theory,
deliberately not traded in v1, but classified and journaled so its frequency can be
measured.

## Sessions are UTC, and that is not a preference

Binance USDⓈ-M exposes no timezone. Every timestamp is Unix ms in UTC, `1d` klines open
at 00:00:00.000 UTC, funding settles at 00/08/16 UTC. So the exchange session **is** the
UTC day, and any level derived from it can be verified against the venue's own candle.

The stronger reason is **DST immunity**: a calendar anchored to local clock time shifts
by an hour twice a year, in each region, on *different dates* — silently redefining
every level and making any backtest spanning the change incomparable with itself.

Regional windows (Asia / London / New York) exist only as **feature tags** recorded on
candidates. They overlap, so they cannot partition volume and cannot define a profile
period.

---

## Layout

```
config.py          every tunable, with provenance; opinion flags default OFF
sessions.py        UTC calendar, boundaries, funding guards, maturity
scanner.py         symbol -> SetupContext (the whole scan)
acceptance.py      the S2/S3 discriminator
confirm.py         wick rejection, engulfing, N-consecutive, delta divergence
state_machine.py   per-symbol auction state, eligibility, arbitration
risk.py            sizing from the stop, portfolio limits, realised R
main.py            the scan-to-order loop

exchange/          filters (Decimal rounding), symbols, REST
data/              klines (+ signed delta, resample), incremental cache
profile/           builder, levels, stability, shape, relations, composite
setups/            base types, context, the four detectors
gates/             pure gate functions + per-setup profiles
execution/         order router, position lifecycle
journal/           four sinks on SQLite, no reason allowlist
ops/               logging, kill switch, state recovery, heartbeat
research/          corpus fetch, disk-backed replay cache, Phase 0 sweeps, statistics
tests/             288 tests
```

`profile/composite.py` holds the naked-POC registry: prior session POCs price has not
traded through, which are the only non-arbitrary targets the profile offers. It costs no
extra requests — `frozen_bundle` computes each session's POC anyway, so the registry
records it in passing and reloads from `profile_snapshots` after a restart. A fresh
deployment therefore starts with none and fills over ~10 sessions; `coverage()` reports
that, since an empty list otherwise cannot be told apart from "price revisited them all".

## Running it

```bash
pip install requests python-dotenv
cp .env.example .env          # defaults are safe: testnet on, trading off

python main.py --symbol BTCUSDT   # scan one symbol, print its context and rejects
python main.py --once            # one full cycle, then exit
python main.py                   # the loop
python -m unittest discover -s tests -t .
```

`--symbol` needs no credentials — it reads public klines only.

## Research

```bash
python -m research.dataset --days 120 --universe-size 120   # one-time corpus fetch
python -m research.calibrate --sweep both --out calibration  # Phase 0 sweeps
```

The corpus fetch is resumable — every session file is written once and skipped if
present — and the ranked universe is **frozen to `universe.csv` on first run**, because
re-ranking by current volume later would silently overlap an in-sample/held-out split.

`research/historical.py` is what makes replay trustworthy: research calls the **same**
`Scanner.build_context` live calls, with the seam at the data layer rather than a
second implementation of the scan. Its one critical job is truncation. In live, future
candles do not exist; read from a completed session on disk they do, and three things
in `build_context` read the candle list directly rather than through
`profile_as_of` — `last_price`, the resampled confirmation candles, and
`session_candles` itself. Each would leak the future silently and produce an excellent
backtest. So the cache bounds every read by the simulated clock, reproducing the
physical fact live gets for free.

---

## Design decisions worth knowing before changing anything

**Everything is `as_of` the last CLOSED 1m candle.** Not wall-clock, not the last trade.
`profile_as_of()` is the only accessor and it filters by `close_time <= as_of`, so
lookahead is not a discipline anyone has to remember — it is unavailable. This is what
makes the pipeline replayable.

**The bin lattice is absolute**, anchored at zero, with a tick-size floor. If the grid
origin or width depended on current price, the same trades would produce different bins
at different times, and a POC that moves when nothing happened cannot be traded.

**Reference entry is the close of the candle that completed the setup** — not the level.
That is the first price at which the setup demonstrably existed. The router then
improves it toward the passive side under one invariant: *never worse than the
reference*.

**Stop first, always.** After a fill the stop is placed before the target. If the target
fails, the position is still protected; the reverse ordering would risk an unprotected
one. `PositionManager` closes at market only when it cannot protect a position — the
single automatic close in the system.

**The two exit orders differ on purpose.** Stop is `STOP_MARKET` + `closePosition` (it
must exit, and `closePosition` is correct under partial fills without quantity
bookkeeping). Target is a resting `LIMIT` + `reduceOnly` (maker fees, fills *at* the
target, which also makes realised R comparable with replayed R). There is no OCO on this
venue, so the survivor is cancelled explicitly the moment the position is seen flat.

**Rejection is a typed value and journaling is the only thing done with one.** There is
no reason allowlist. A reason list maintained separately from the gates always falls
behind them, and the reasons it omits become *invisible* rather than under-reported.

**Distances between profile levels are denominated in VALUE WIDTH, not ATR.** Daily ATR
is a 14-day average range; a value area is a fraction of one session's range. On a quiet
day they differ by ~10x. Four thresholds were originally mis-scaled this way and one was
mathematically unsatisfiable — see CALIBRATION.md. ATR is the right unit only for
genuinely volatility-scaled distances: stop buffers and touch tolerances.

**Mechanical gates start permissive.** A gate too loose costs some bad trades, which
Phase 2 measures and prices. A gate too tight produces a **false null** — the setup never
fires where it works and Phase 1 blames the market for a filter. The second failure is
invisible, so it is the one to design against.

**Open positions survive the session boundary; pending candidates do not.** A position's
stop and target came from the profile current at entry, and that does not stop being the
reason for the trade because a clock rolled over. A pending candidate's levels, by
contrast, reference a profile that is no longer the reference.

**Known live/replay divergence: mark price vs. last price.** Live stops trigger on mark
price (`workingType=MARK_PRICE`, `priceProtect=TRUE` — deliberately, to resist wick-driven
liquidation-hunting). Replay has no mark-price series to simulate against and triggers
stops off candle high/low instead, which is effectively last price. The two can genuinely
disagree during a funding-driven mark/last spread. Not fixed, because historical mark
price likely isn't available to replay against anyway — recorded here so a live/replay
result mismatch isn't mistaken for a logic bug before this is checked first.

---

## Roadmap

No real money until a phase gate passes, and each gate is a number agreed **before** the
result is seen.

| Phase | What | Gate | Status |
|---|---|---|---|
| **0** | Calibrate every threshold against real sessions | Thresholds set from observed percentiles, not argument | **done** — 14,185 sessions |
| **1** | Directional accuracy vs matched random control | Positive lift, 95% CI excludes zero | **done, all four failed** — see below |
| **2** | Net-of-fees R, first-touch, on the gated population | Positive net R, same sign both time halves and both samples, n ≥ 300 | blocked on a setup that clears Phase 1 |
| **3** | Ablate every confirmation and opinion gate | Only replicated positive contributors switch on | not started |
| **4** | `ARBITER_MODE`, acceptance gap, parameter robustness | Degrades smoothly under ±25% perturbation | not started |
| **5** | Forward paper | Recomputed levels match decision-time levels | not started |
| **6** | Minimum-size live | Forward R consistent with Phase 2 | not started |

**Phase 1 result (CALIBRATION.md finding 15):** the honest prior below is what happened.
Two full replays (120 symbols, ~180 sessions), one per acceptance discriminator, plus the
matched-random control on each. **No setup shows a positive lift with a CI excluding
zero.** `S1-POC` and `S2-VAR` are indistinguishable from a random entry at the same
location in both runs; `S1-LVN` has too few trades (n=14) to judge; `S3-BRK` is
**measurably worse** than its own random-entry twin, replicated under both
discriminators (lift −0.39R and −0.47R, both 95% CIs excluding zero on the negative
side) — its entry-timing rule picks a worse spot than chance at the identical location.

All four setups are now **disabled by default** (`config.py`, `.env.example`), per the
rule agreed before Phase 0 began: a setup failing its phase gate is disabled, not
loosened. `research/replay.run()` force-enables them regardless, so a future fix or
Phase 3's ablation can still measure a disabled setup — the live-trading flag and the
research-measurability question are deliberately kept separate.

**The honest prior, and it held:** the source material shows only winning examples,
gives no sample size, and models neither fees nor slippage. The most likely Phase 1
outcome for any given setup was a null, and that is what three of the four setups
delivered — S3-BRK's result was a specific negative rather than a null. The build was
ordered so that finding this out was cheap: Phases 0 and 1 needed only the profile
engine, the detectors, and one replay. No execution stack, no live keys, no capital.

**Follow-up (CALIBRATION.md finding 16):** S3-BRK's diagnosis — real entries scored like
twins entered after them, nothing like twins entered before — was tested directly with a
redesigned entry (`S3_ENTRY_MODE="accepted"`, entering on acceptance itself instead of
after a pullback). It closed most of the gap (lift −0.47R → −0.12R, CI now contains zero)
but still doesn't beat control, and revealed the SELL-side weakness lives in the control
too, not only in S3's own entries. Still disabled either way — moved from the one
*affirmative negative* setup into the same disabled-unproven bucket as `S1-POC`/`S2-VAR`.

**Follow-up (CALIBRATION.md finding 17):** the BUY/SELL split above isn't a setup defect
— it shows up in the matched-random control too, for every setup, and concentrates in
the second half of the corpus's date range. That's the signature of market-wide upward
drift in the sample window, not a directional flaw in any setup's logic.

**Follow-up (CALIBRATION.md finding 18):** tested whether any of the four built-but-off
opinion gates (shape, order-flow delta, day-over-day value migration, S2's poor-extreme
overlay) rescue `S1-POC` or `S2-VAR` individually. None do — every filtered lift's CI
still contains zero, and the one theorised population (`S2_REQUIRE_POOR_EXTREME`) turned
out too thin on this corpus (n=5) to judge at all. Both setups' disposition is unchanged.

**Follow-up (CALIBRATION.md finding 19) — the first positive result in this project:**
tested `TARGET_MODE="structural"` (aim at the nearest untested POC or opposite value
edge instead of a blind 2R) against all four setups. `S2-VAR` **beats its own
matched-random control** under it — lift +0.15R, 95% CI [+0.02, +0.28] — and the result
replicates independently across BUY/SELL and early/late splits (all four positive, none
flipping sign), not just in the pooled number. `S1-POC` moves the same direction but
doesn't clear the bar. Still disabled by default pending Phase 2 (fees/slippage) and a
second historical window — one corpus is not enough to trust a result that would decide
what trades real capital, per finding 17's own lesson — but this is the first setup with
real evidence in its favour rather than against it or absent.

**Follow-up (CALIBRATION.md finding 20):** tested LVN-based stop placement
(`S1_STOP_MODE`/`S2_STOP_MODE = "lvn"`) — the top-priority fix for S1-POC's diagnosed
weakness (its stop sits in the POC's own densest, most rotation-prone zone). Neither
setup beats its own control (S1-POC lift −0.06R, S2-VAR lift +0.06R, both CIs contain
zero), and S2-VAR's BUY/SELL splits disagree in sign. Root cause: under the default
`fixed_r` target, a wider stop widens the target with it, so far more trades ran out of
data before resolving — the resolved sample shrank 59–66% and the survivors are a biased
subsample, not a clean comparison. Both flags stay at their defaults; the un-confounded
re-test (same LVN-stop modes under `TARGET_MODE="structural"`) is queued next.

**Follow-up (CALIBRATION.md finding 21):** ran that un-confounded re-test. For `S2-VAR`
the point estimate held up (+0.18R lift, at or above finding 19's +0.15R) and replicated
in direction across all four BUY/SELL/early/late splits — but the population dropped to
206 (from 936) once the wider LVN stop tripped structural targeting's own minimum-reward
gate more often, and the resulting wider CI ([−0.09,+0.45]) no longer clears zero. Read
as an underpowered echo of the same signal, not a refutation — more data would resolve
it, not a different config. `S1-POC` still shows no fix (lift −0.03R, BUY/SELL disagree
in sign). No config changes; `S2_ENABLED`/`TARGET_MODE` stay exactly as finding 19 left
them.

**Phase 2, part 1 (CALIBRATION.md finding 22):** does `S2-VAR`'s structural-target lift
survive costs worse than the default model? Recomputed net R under 2x/3x slippage and
a taker-both-legs assumption, applied identically to the real trades and their twins.
The lift holds at +0.1514–0.1515R across every scenario, including a deliberately
stressed one — a harsher cost model moves the absolute level but not the separation
from control, because a trade and its twins share almost the same cost structure. Part
2, the harder test, is now running: a second, independent historical window
(2026-01-30 .. 2026-05-29, the 120 sessions immediately preceding the existing corpus,
same frozen symbol universe) fetched into a separate cache root via a new
`research/dataset.py --end-date` option.

**Phase 2, part 2 (CALIBRATION.md finding 23) — complete, and S2-VAR passes both
legs.** The second window (2026-01-30..2026-05-29) landed: alone it doesn't
independently clear (n=201, lift +0.20R, CI [-0.06,+0.46] - a smaller, data-availability-
limited population) but the point estimate is as large or larger than window 1's,
replicates in direction across all four BUY/SELL/early/late splits, and S1-POC/S3-BRK
reproduce their established dispositions exactly on the new window. **Pooled across
both windows: n=1137, lift +0.1611R, 95% CI [+0.0440, +0.2781] — BEATS CONTROL**,
tighter than either window alone. This is the strongest evidence behind any result in
this project so far. `S2_ENABLED`/`TARGET_MODE` stay at their defaults one step
longer regardless: "beats control" is not "is profitable" — pooled win rate (~28.7%)
is still below the ~34.3% breakeven this system's 2:1 reward:risk needs, and flipping
a live-capital default deserves a deliberate decision, not an inherited one.

**Follow-up (CALIBRATION.md finding 24):** `poor_high`/`poor_low` fired on only
0.3%/0.5% of all 14,185 profiled sessions, against 58-64% for `excess_high`/
`excess_low` — too lopsided to be "just rare." Measured the real distribution: the
old hardcoded ceiling (`0.40`, not even a config value) sat above the 99th percentile
of what the single extreme bin's volume, as a fraction of the POC, actually reaches.
Replaced with `config.POOR_EXTREME_MAX_BIN_VOLUME_PCT` (default `0.15`, matching
`EXCESS_MAX_BIN_VOLUME_PCT` deliberately), which yields a real testable population
(3.3%/4.0%) instead of single digits. This doesn't resolve finding 18 by itself —
`S2_REQUIRE_POOR_EXTREME` still needs re-running under the corrected threshold — but
it makes that gate testable for the first time; n=5 never could have produced a
verdict.

**Follow-up (CALIBRATION.md finding 25):** tested `S1_MIN_CONFIRMATIONS=2` (require 2
of 4 confirmation signals instead of 1). The pooled lift nearly doubled (+0.05R →
+0.10R) but still contains zero, and splitting it shows why it doesn't count as
progress: the move is almost entirely BUY-side (+0.21R), SELL stayed flat (−0.005R) —
exactly the signature finding 17 already diagnosed as a market-drift confound, not a
property of confirmation strength. A real fix should move both directions alike.
Also revealed a real tradeoff: S1-LVN reads the same threshold, and its already-thin
sample (n=14) shrank to n=7 at 2-of-4 — directly working against the separate goal of
widening S1-LVN's own sample. Stays at its default of 1.

**Follow-up (CALIBRATION.md finding 26):** a partial-fill correctness bug, found by
independent review rather than replay. `_settle_fill` sized the target off whichever
poll first detected a `PARTIAL` fill — a poll that runs *before* the unfilled
remainder is cancelled, so more size could land in that gap. The stop needs no
correction (`closePosition` covers whatever's actually open), but the target is
placed with an explicit quantity, so a stale, too-low reading would under-cover the
position with no take-profit on the uncovered slice — and reconciliation can't catch
it, because an undersized-but-present target reads as "has a target." Fixed by
re-querying `order_state()` after the cancel lands and using that fresh figure. Not a
calibration result — nothing here needed measuring or gating.

**Follow-up (CALIBRATION.md finding 27):** before building a widen-past-HVN stop
mechanism or spending a replay testing the existing (never-swept) `stop_inside_hvn`/
`target_behind_hvn` reject gates, measured how often the condition each would act on
actually occurs — both setups already record these flags on every candidate
unconditionally. `stop_inside_hvn=True`: 0/1,137 for S2-VAR, 4/347 for S1-POC — under
current stop formulas this essentially never happens, so neither a widen mechanism nor
the reject gate has a population to act on. `target_behind_hvn` goes the other way —
95.9% true for S2-VAR's structural targets, because a structural target aims at a POC,
which *is* the center of an HVN by definition, making the flag close to a tautology
that would reject almost the entire validated S2-VAR population for a reason unrelated
to trade quality. Neither item is being built as originally scoped; both would need a
different, more fundamental redefinition first. No replay run — computed from existing
trades.csv data.

**Follow-up (CALIBRATION.md finding 28):** tested `shape_opposed` and
`htf_value_opposed` together (population restricted to sessions where neither fires),
computed retrospectively from the pooled structural-target corpus. S1-POC: a true
no-op, as `S1_REQUIRED_SHAPES=["D"]` already forces it. S2-VAR: the combined filter's
pooled lift (+0.2031R) clears the same BEATS-CONTROL bar as the unfiltered baseline —
but almost the entire effect comes from `shape_opposed` alone (which itself misses
zero by 0.002R), and the result does not survive this project's own replication check:
early/late splits diverge sharply (+0.33R vs +0.09R). Not shipped — a boundary result
that dissolves under the same test every other finding here is held to, not a rescue.

**Follow-up (CALIBRATION.md finding 29):** prerequisite work for a confirm-interval
ablation — five candle-count thresholds (`ACCEPT_MIN_CANDLES_OUTSIDE` etc.) counted
`CONFIRM_INTERVAL`-timeframe candles as bare literals (13, 5, 3, 6, 2), which would
have silently changed what each threshold *means*, not just its granularity, the
moment the interval changed (195 minutes at 15m vs. 13 minutes at 1m for the same
"13 candles"). Fixed with `config.candles_for_minutes()`, which derives each
threshold's default from a canonical duration instead — reproduces the exact prior
values at the 15m default (regression-locked by test), and a new
`research.replay --confirm-interval` flag rescales all five together for an ablation
run (smoke-tested at 5m: 9/39/15/18/6, exact match to the math). No data refetch
needed — confirm candles are resampled from the always-cached 1m data. The ablation
experiment itself is running now; its result is a separate, later finding.

**Follow-up (CALIBRATION.md finding 30):** built the first independent check on the
shape classifier — `research/regime.py`'s Kaufman efficiency ratio, using only
candle closes, none of `profile/shape.py`'s own bin/POC/value-area inputs. Computed
for all 14,185 existing corpus sessions from cached 1m data, no new fetch or replay.
The classifier's *output* validates cleanly: ER rises monotonically across all five
shape labels (D lowest, trend highest), and separates trend from D directly at AUC
0.718 (CI [0.706,0.730]). But the two conditions behind "trend" carry very unequal
weight — `intra_poc_migration_atr` alone (AUC 0.787) actually beats the combined
output, while `va_range_ratio` alone is weak (AUC 0.558) and points in the opposite
direction from its own "low ratio means trend" assumption. Nothing shipped or
changed live — this is diagnostic, and flags `va_range_ratio`'s direction as worth a
second look, not a fix already made.

**Follow-up (CALIBRATION.md finding 31):** S2-VAR's rejection-quality measurement —
return speed and return-candle delta, pooled across both windows (n=1,137). Slow
excursions (≥4 confirm candles) beat control, fast ones don't, opposite the naive
"quick rejection is cleaner" theory. Combined with delta agreeing with the trade
direction: n=157, g=92 clusters, **mean R turns positive** (+0.0355) for the first
time anywhere in this project — lift +0.4299R, CI [+0.1632,+0.6967], beats control by
the widest margin measured so far. Replication is genuinely mixed rather than a clean
pass or fail: BUY clears and SELL doesn't, but a drift check (the same method that
confirmed findings 17/25's confound) shows market drift explains only about a third
of that BUY/SELL gap here, not nearly all of it — a materially different case, not
dismissed the same way. **Not shipped** — doesn't clear the phase gate — but flagged
as the strongest open lead in the project, worth a third independent window or a
SELL-focused follow-up before either shipping or dropping it.
