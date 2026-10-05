# Calibration log

Every threshold in `config.py` is provisional until Phase 0 replaces it with an
observed percentile from real sessions. This file records what was **found by
testing** during the build, so the next reader does not re-derive it — and so the
Phase 0 work list is explicit rather than implied.

The general lesson, stated once: **synthetic fixtures cannot calibrate a
distributional threshold.** A fixture embeds an assumption about what real profiles
look like, and that assumption is the thing being calibrated. Synthetic data is
still excellent for correctness (does the algorithm compute what it claims?) and
useless for calibration (is this number right?).

---

## 1. Value-area overshoot is expected, and resolution-dependent

Whole-pair expansion overshoots the 70% target because it adds two bins at a time.

| occupied bins | realised value fraction | overshoot |
|---|---|---|
| 11 | 0.7213 | +2.1pp |
| 27 | 0.7257 | +2.6pp |
| 53 | 0.7111 | +1.1pp |
| 56 | 0.7106 | +1.1pp |

A 9-bin hand fixture overshot to **0.825** — each pair was 25% of the whole
distribution. Not a bug; it is why `PROFILE_MIN_OCCUPIED_BINS` exists and why
`value_fraction` is recorded on every profile rather than assumed to equal
`VALUE_AREA_PCT`.

**Phase 0 action:** none. Verified correct.

---

## 2. `SHAPE_*` — two real bugs, found by trying to build a D-shape fixture

**Bug A: a dead zone between two thresholds on one axis.** The first draft used a
trend ceiling of 0.28 and a separate balanced floor of 0.40. A profile with a
central POC and a ratio of 0.33 matched neither the P nor the b test, failed the
balanced floor, and fell through to `trend` — *the archetype of balance classified
as its opposite.* Fixed by collapsing to a single elongation threshold and making
**D the residual**. Two thresholds on one axis with different outcomes either side
always leave that gap.

**Bug B: a Gaussian is the wrong reference model for VA/range.** Measured Gaussians
sit at ~0.33 because a normal distribution has long thin tails relative to its
core. Real sessions are **platykurtic** — boxier, short tails — and a realistic
balanced fixture measured **0.578**. So the Gaussian calibrates
`POC_MIN_PROMINENCE` well (same core-vs-mean logic) and calibrates this ratio
badly. The first draft was anchored to the wrong archetype.

**Phase 0 action:** `SHAPE_TREND_MAX_VA_RANGE_RATIO = 0.35` is reasoned, not
measured. It is the **single most important number to calibrate**: it decides the
auction state, and getting the state wrong makes every setup trade backwards.

---

## 3. Elongation cannot detect a trend at all — a hole nothing else closed

A uniform one-directional session (a steady ramp) spreads volume evenly across its
range, so its value area covers ~70% of that range and its POC sits dead centre.
The trend fixture measured **VA/range 0.708, POC position 0.504 → classified `D`**,
identical to a balanced session.

Distribution alone genuinely cannot separate travel from spread, because a
completed histogram has discarded the time ordering. Added
`intra_session_poc_migration`: split the traded candles in half, compare the two
POCs. A rotating auction agrees with itself across halves; a travelling one does
not.

| fixture | VA/range | POC pos | migration (ATR) | label |
|---|---|---|---|---|
| balanced (oscillating) | 0.578 | 0.510 | −0.300 | `D` |
| trend up | 0.708 | 0.504 | **+0.500** | `trend` |
| trend down | 0.713 | 0.378 | **−0.542** | `trend` |

Two sub-findings:

- **The split must be on traded candles, not the window midpoint.** Splitting a UTC
  day at 12:00 gives a 3-hour-old session an empty second half, so the check would
  have returned `None` for the whole first half of every day — silently disabled
  exactly where a developing profile needs it most.
- **The metric is noisy.** The balanced fixture still reads −0.300 against a 0.5
  threshold, purely from candle ordering within the core. The margin is thinner
  than it should be.

**Phase 0 action:** calibrate `SHAPE_TREND_MIN_POC_MIGRATION_ATR` (0.5,
provisional) against the real distribution, and measure how noisy it is on genuine
balanced sessions before relying on it.

---

## 4. `POC_MIN_PROMINENCE` — a gate that would have strangled its own measurement

Prominence rises with resolution (finer bins lower the mean without lowering the
peak): Gaussians measured **1.17 / 1.50 / 2.63 / 2.78** at 11 / 27 / 53 / 56 bins.
But a realistic balanced session — dense even core, short tails — measured only
**1.29**. A floor at 1.8 would have rejected the exact population S1 exists to
trade.

**The asymmetry that decides the default.** A quality gate set too loose costs some
bad trades, which Phase 2 measures and prices. A gate set too tight produces a
**false null**: the setup never fires where it works, and Phase 1 concludes "no
edge" about a filter rather than about the market. The second failure is far worse
because it is *invisible* — nothing signals that a measurement was strangled rather
than informative.

So mechanical gates start **permissive** and tighten on evidence, never the
reverse. Lowered to **1.25** (1.0 is a flat histogram, so it still rejects the
structureless case, which is all this gate is for).

**Phase 0 action:** set from an observed percentile of real sessions.

---

---

## 5. The systematic one: ATR is the wrong denominator for level distances

Found by running the scanner against **live** public data on BTCUSDT / ETHUSDT /
SOLUSDT. Synthetic fixtures could not have found it, because a fixture's ATR and its
value width are whatever the fixture author made them.

The measurements that exposed it:

| symbol | daily ATR (14d) | prior session range | value width | VA width in ATR |
|---|---|---|---|---|
| BTCUSDT | 2471.77 | 708.60 | 345.80 | **0.14** |
| ETHUSDT | 103.22 | 34.76 | 14.42 | **0.14** |
| SOLUSDT | 5.60 | 2.64 | 1.43 | **0.26** |

Daily ATR is a **14-day average range**. A value area is the central 70% of **one
session's** distribution. On a quiet day the two differ by roughly **10x** — BTC's whole
prior session spanned 708 against an ATR of 2472.

So any threshold that asks "how far is this, relative to the value area" and denominates
it in ATR is ~10x too strict. Four thresholds were wrong this way:

| threshold | was | consequence | now |
|---|---|---|---|
| `S1_MIN_OPEN_DISTANCE` | 0.25 ATR | Near-contradictory with the inside-prior-range condition: the tail between a value edge and the session extreme is ~0.2–0.5 of value width, so nearly every qualifying open was *also* outside the range, and S1 rejected itself | 0.15 × value width |
| `S1_LVN_MIN_POC_DISTANCE` | 0.30 ATR | **Mathematically unsatisfiable.** Both levels sit inside the value area, so their separation is bounded by the value width — 0.14 ATR on BTC. S1-LVN could never fire on any symbol with a value area narrower than 0.30 ATR, i.e. most of them | 0.25 × value width |
| `S2_MIN_EXCURSION` | 0.35 ATR | On SOL that demanded price travel 1.96 — about **1.4x the entire value area** — before a fade was allowed. Live reject: `excursion 0.054 ATR < 0.35` | 0.20 × value width |
| `S3_BREAK_MIN_ATR` | 0.50 ATR | Dominated the value-width term by **14x** on BTC (1235.89 vs 86.45), where the whole prior session range was 708.60. S3 could not fire on a quiet-day profile | 0.10 ATR as a noise floor only; 0.30 × value width is now the criterion |

**The rule this establishes:** a threshold measuring a distance **between two profile
levels** must be denominated in value width. ATR is the right unit only for distances
that are genuinely about volatility — stop buffers and touch tolerances.

After the fix, the live rejects moved from threshold artifacts to real market states:
`NO_CONTINUATION` (price has not made a new high), `NOT_CONFIRMED` (0/1 confirmations),
`FAILED_AUCTION_NOT_TRADED`. That is the signature of a funnel that is measuring the
market rather than its own parameters.

---

## 6. Value bounds are structurally less stable than the POC

Also from the live run:

| symbol | poc_shift_bins | vah_shift_bins | val_shift_bins |
|---|---|---|---|
| BTCUSDT | 0.125 | 1.00 | 1.25 |
| ETHUSDT | 0.125 | 0.75 | 1.07 |
| SOLUSDT | 0.375 | 2.25 | 0.74 |

The asymmetry is structural, not noise. A POC is an **argmax** — re-binning must change
which bin is tallest to move it. A value bound is where a **cumulative sum crosses 70%**,
so a small change in bin width shifts the crossing by a bin or two almost every time.

A single 1-bin threshold — the first draft — rejected the value bounds on **all three**
liquid symbols, which would have gated S2 and S3 off across the entire tradable universe:
a textbook false null. Split into `STABILITY_MAX_POC_SHIFT_BINS=1.0` and
`STABILITY_MAX_VA_SHIFT_BINS=3.0`.

Related: `VA_MIN_WIDTH_ATR` was 0.10 while ordinary sessions measure 0.14–0.26, so a
slightly quieter day would have tripped `VA_DEGENERATE` on the most liquid symbols on the
venue. Lowered to 0.05.

---

## 7. The acceptance discriminator did not discriminate

Caught by the pipeline test asserting S2 and S3 can never both fire.

The first version measured "what fraction of the excursion window's volume transacted
outside the bound". But the excursion window **is** the run of candles that closed
outside, so almost all of its volume is outside by construction: the measure returned
**~1.000 on every excursion**, thin or heavy alike. The rejection threshold was
unreachable, acceptance was trivially satisfied, and the module the whole state machine
rests on was inert while looking like it worked.

Replaced with a **rate ratio**: volume per candle outside value, over this session's own
volume per candle. Scale-free, compares the session against itself, and it actually
varies — a regression test now asserts the ratio rises monotonically across a 1000x
volume sweep.

A second bug in the same module: when measuring an excursion that had **ended**, the
window was found by "everything after the last inside close" — but by then price has
already returned inside, so the last inside close is the return itself. The window became
the post-return candles, producing a **negative excursion distance**.

**Phase 0 action:** calibrate both thresholds, and the **gap** between them — too narrow
and both setups fire on the same move, too wide and neither ever does.

---

## 8. The acceptance denominator contained its own numerator

Found by the Phase 0 sweep itself, on the first real corpus, before any threshold was
set. It is the same *class* of bug as finding 7 and survived that fix — worth stating
plainly, because two independent versions of this module both got the denominator
wrong in the same direction.

**The tell.** 13 of 58 excursions (22.4%) had a rate ratio of **exactly 1.000**, and
every one of them had `decision_index == 2` and `volume_outside_fraction == 1.000`.
A cluster on an exact value is never a market fact.

**The arithmetic.** The denominator averaged over the whole session, excursion
included. Write the session as M excursion candles at rate `r_out` and N−M in-value
candles at `r_in`:

```
session_rate = (M*r_out + (N-M)*r_in) / N
ratio        = r_out / session_rate      ->  1.0  as  M -> N
```

So an excursion **dilutes its own reference in proportion to its own length**. Two
consequences, the second worse than the first:

| | effect |
|---|---|
| general case | Ratio inflated by excursion length alone. Measured on a fixed thin excursion: **0.0559 → 0.2400** (4.3x) as it ran from 5 to 200 candles. The bias is largest on long excursions, which are exactly what S3 trades. |
| M == N | A session that **opens outside** yesterday's value and never trades back inside has no in-value candles at all, so `ratio == 1.000` exactly, carrying **no information**. 1.000 clears `ACCEPT_MIN_VOLUME_RATE_RATIO = 0.80`, so all 13 were classified ACCEPTED and armed S3. |

And the second case is not a rare corner: **opening outside value is precisely what
makes S3 eligible.** The degenerate measurement was concentrated in the population
the setup trades.

**The fix.** The baseline is drawn from candles *before* the excursion began, so the
denominator is independent of the numerator by construction. When too few exist, the
**prior session's** rate per confirmation candle stands in — independent, and always
available since a frozen profile is already a precondition. With neither, the verdict
is the new `NO_BASELINE` and nothing trades.

That last branch matters more than it looks. With no denominator the ratio computes
to **0.0**, which reads as the *thinnest possible* excursion and would arm **S2** — so
a missing measurement would have produced a trade in the opposite direction. A
missing measurement must be a refusal, never a value; `ACCEPTANCE_NOT_MEASURABLE` is
a separate reject reason from `ACCEPTANCE_PENDING` for the same reason (missing data
is not a middling auction).

After the fix: exact-1.000 events **0.0%**, baseline source `SESSION` 63.8% /
`PRIOR_SESSION` 36.2%, and the ratio is exactly length-invariant on a fixed thin
excursion (0.0500 at 5, 20, 60 and 200 candles).

**The general lesson, since this is twice now:** whenever a measure is a ratio of a
part to a whole that *contains* that part, it has a fixed point it will drift to, and
the drift looks like signal. The check is mechanical — feed the measure a sweep in
which the answer must not change, and assert that it does not. Both regression tests
are now written that way (`test_ratio_actually_varies_with_volume` for finding 7,
`test_ratio_does_not_drift_to_one_as_the_excursion_lengthens` for this one), and the
sweep report prints the exact-1.000 count on every run as a permanent tripwire.

**Phase 0 action:** calibrate `ACCEPT_MIN_BASELINE_CANDLES` (6, provisional) from how
often each baseline source ends up used, and check whether `PRIOR_SESSION`-baselined
events behave differently from `SESSION`-baselined ones — a cross-day denominator
ignores today's regime, so they may not be poolable.

---

## 9. S3-BRK could never fire, and its rejects looked like market states

Found by reading `_pullback_structure` line by line, not by any test. **S3-BRK was
structurally incapable of producing a candidate** — measured at **0 of 30,000** random
windows, in both directions.

The continuation test asked `candle.close > extreme`, where `extreme` was the maximum
high across a window that **included that same candle**. So:

```
extreme >= candle.high >= candle.close      ->   close > extreme is unsatisfiable
```

And the other branch closed the trap. When the signal candle *did* make a new high —
which is precisely what continuation means — it became the extreme itself, `after` went
empty, and the function returned `None`, which the caller reports as *"no pullback has
formed yet"*. Both paths reject, for opposite stated reasons.

**Why nothing caught it.** A setup that can never fire passes every correctness test
trivially: it emits no wrong candidate, violates no geometry, mis-applies no gate. Its
rejections are recorded as ordinary market states, and they read as completely
plausible. Two live scans reported:

```
reject S3-BRK  NO_CONTINUATION  close 84437.1 has not exceeded the pre-pullback extreme 84640
reject S3-BRK  NO_CONTINUATION  close 124.06 has not exceeded the pre-pullback extreme 124.8
```

Both were reported in this project as evidence the funnel was "measuring the market
rather than its own parameters". For S3 that reading was wrong — those were the
signature of a dead code path, and the numbers in the message are exactly what an
unsatisfiable comparison prints.

**The fix.** Measure the structure over `window[:-1]` — the history in which the
breakout and pullback happened — and judge continuation with `window[-1]`. Splitting
the candle being judged from the evidence it is judged against turns an unsatisfiable
comparison into a real one. After the fix S3 fires on ~2.7% of random windows,
symmetrically (816 up / 831 down of 30,000), and end-to-end on a constructed ideal
breakout with correct geometry.

**The generalisable lesson, and a new test class.** Correctness tests cannot detect a
dead setup; only asking *"can this fire at all?"* can. `SetupReachabilityTests` now
asserts that each setup produces a candidate on a scenario built to suit it, in both
directions where direction applies. This is the third bug of this family — a condition
that cannot be satisfied (`S1_LVN_MIN_POC_DISTANCE` in finding 5, the acceptance ratio
in findings 7 and 8) — and the pattern is consistent: **a measure compared against a
bound derived from the measure itself.** Worth checking first in anything new.

**Phase 0 consequence:** S3 has produced no data at all, so nothing measured about it so
far means anything. Its thresholds (`S3_BREAK_MIN_VA_FRACTION`, `S3_PULLBACK_MAX_DEPTH`,
`S3_REQUIRE_DEVELOPING_POC_MIGRATION`) are wholly uncalibrated and the acceptance sweep
must be re-read now that the setup downstream of it can actually act.

---

## 10. PHASE 0 RESULTS — profile distributions, n = 14,185

Corpus: 120 symbols × 119 complete UTC sessions, 1m klines, fetched once with ranks
frozen. Every number below is an observed percentile, not an argument.

| metric | p05 | p10 | p20 | p50 | p80 | p90 | p95 |
|---|---|---|---|---|---|---|---|
| `poc_prominence` | 2.02 | 2.23 | 2.49 | 3.15 | 4.19 | 5.11 | 6.26 |
| `poc_shift_bins` | 0.13 | 0.16 | 0.36 | 0.62 | 1.38 | **4.78** | 11.36 |
| `vah_shift_bins` | 0.25 | 0.43 | 0.60 | 1.16 | 1.97 | 3.00 | 6.25 |
| `va_range_ratio` | 0.275 | 0.331 | 0.389 | 0.497 | 0.623 | 0.701 | 0.767 |
| `poc_position` | 0.118 | 0.174 | 0.263 | 0.474 | 0.672 | 0.766 | 0.825 |
| `abs(migration)` ATR | 0.00 | 0.02 | 0.06 | 0.19 | 0.45 | 0.68 | 0.95 |
| `value_width_atr` | 0.138 | 0.176 | 0.230 | 0.376 | 0.663 | 0.908 | 1.205 |
| `occupied_bins` | 15 | 22 | 29 | 48 | 79 | 108 | 140 |

### What each threshold was actually doing

| threshold | was | rejected | now | why |
|---|---|---|---|---|
| `POC_MIN_PROMINENCE` | 1.25 | **1.1%** | **2.20** | The gate was inert — it looked like a quality filter and filtered nothing |
| `STABILITY_MAX_POC_SHIFT_BINS` | 1.0 | **24.0%** | **2.0** | The most aggressive gate in the system, on the strength of a 3-symbol sample |
| `STABILITY_MAX_VA_SHIFT_BINS` | 3.0 | 10.0% | 3.0 | Already exactly p90 — correct by accident |
| `VA_MIN_WIDTH_ATR` | 0.05 | 0.8% | 0.05 | Appropriate noise floor |
| `VA_MAX_WIDTH_ATR` | 3.0 | 0.3% | 3.0 | Appropriate noise floor |
| `PROFILE_MIN_OCCUPIED_BINS` | 20 | 7.9% | 20 | Near p10, appropriately permissive |
| `SHAPE_TREND_MAX_VA_RANGE_RATIO` | 0.35 | 12.7% | 0.35 | Kept — see the caveat below |
| `SHAPE_TREND_MIN_POC_MIGRATION_ATR` | 0.5 | 17.1% | 0.5 | ≈ p82, and ~20% trend days matches the prior |

**`POC_MIN_PROMINENCE` is the headline, and it confirms this file's own opening
lesson.** Finding 4 lowered it from 1.8 to 1.25 because a realistic-looking hand
fixture measured 1.29 and 1.8 "would have rejected the population S1 exists to trade."
Real sessions begin at **2.02 at the 5th percentile**. The fixture was not merely
imprecise, it was outside the real distribution entirely — so the reasoning in finding 4
was sound and its input was wrong, which is the exact failure mode that finding warned
about. Prominence scales with bin count, and the fixture's resolution was not the
corpus's.

**`STABILITY_MAX_POC_SHIFT_BINS` shows what a 3-symbol sample is worth.** BTC/ETH/SOL
measured 0.125–0.375; the corpus median is 0.62 and p90 is 4.78. The distribution has a
sharp break between p80 = 1.38 and p90 = 4.78 — sessions either re-bin within ~2 bins or
fall apart completely — so 2.0 sits in the gap, cutting the unstable tail (~16%) instead
of a quarter of the corpus.

**One threshold was right for the wrong reason.** `STABILITY_MAX_VA_SHIFT_BINS = 3.0` is
exactly p90. It was set by comparing three symbols' value bounds against their POCs, and
the corpus confirms that asymmetry at *every* percentile: a value bound is ~2× less
stable than a POC throughout. The structural argument was correct even though the sample
was not.

### `va_range_ratio` is a choice, not a discovery

The distribution is smooth and unimodal, peaking around 0.33–0.50, with **no natural
break anywhere**. So 0.35 is defensible (it labels the most elongated ~13% as trend,
~20% including migration, matching the Market Profile prior) but it is not privileged by
the data. This is precisely the case where Phase 4's robustness sweep matters: a
threshold on a smooth axis should degrade gracefully under ±25% perturbation, and if the
result does not, the finding is about the threshold rather than about the market.

### The shape mix, and a worry the data dismissed

| shape | share |
|---|---|
| D (balance) | **29.9%** |
| B (split value) | 25.8% |
| trend | 20.5% |
| b | 13.4% |
| P | 10.3% |

B at 25.8% looked alarming: B is the first branch in `classify()`, and S1 requires D, so
over-detecting B would starve S1 on sessions that are really balanced. A sensitivity
sweep over the 0.60–0.90 × 0.15–0.35 threshold grid (400 rebuilt sessions) settled it:

| thresholds | D | B | trend | P | b |
|---|---|---|---|---|---|
| 0.60 / 0.35 (current) | 32.8% | 25.8% | 16.8% | 12.5% | 12.2% |
| 0.90 / 0.15 | 37.2% | 7.0% | 23.2% | 15.5% | 17.0% |

B's rate swings **3.7×** across the grid, so 25.8% must not be read as "a quarter of
sessions have genuinely split value". But **D is stable at 33–37% throughout**, and the
sessions that leave B become trend, P or b — one-sided sessions, not balanced ones. So
S1's eligible population is robust to these thresholds and the worry was unfounded. They
are left untouched: tuning a number that moves B while changing no trading decision
would be fitting for its own sake.

**Value-area overshoot is confirmed at scale:** realised `value_fraction` p50 = 0.722
against a 0.70 target, exactly the ~2pp whole-pair overshoot predicted in finding 1.

---

## 11. PHASE 0 RESULTS — the acceptance discriminator is inert, n = 31,079

The sweep findings 7 and 8 were built to enable. Three bugs in this module had already
been fixed; the denominator fix is confirmed at scale — **ratio exactly 1.000: 0 rows
(0.0%)**, against 22% before it — and the baseline resolves as `SESSION` 78.1% /
`PRIOR_SESSION` 21.9%, so every excursion is now measured against data that excludes it.

The measurement is sound. The quantity it measures carries no information.

### The sweep's own verdict was wrong, in the opposite direction

It printed `95% CIs overlap: True -> NO separation; the discriminator does not work on
real data`. That verdict came from a **difference of means** on a quantity whose mean is
64.2 with sd 4212 against a p95 of 3.61: a handful of excursions with a near-zero
baseline own both the mean and its standard error, so the comparison could not resolve
anything and would have reported overlap whatever the truth was. Directly beneath it, the
report's own band table showed the continuation rate rising monotonically across all
seven bands of that same quantity.

A wrong simulator returns a worse number; **a wrong statistic returns a confident one.**
Nothing crashed and no test failed, because there was no test. `research/metrics.py` now
carries `auc`, `auc_verdict` and `auc_within`, and `tests/test_metrics.py` pins them by
invariant — including that a **constant** must read 0.500, which is exactly what the bug
family of findings 7, 8 and 5 produces, and that a **monotone rescaling must not move a
rank statistic**.

### What the ratio is actually doing: standing in for distance

Rank separation on the CONTINUED/REVERTED contrast looks real at first: **AUC 0.5808
[0.5730, 0.5885]**, replicating across time (0.5716 / 0.5903), liquidity (0.5835 /
0.5780) and side (0.5824 / 0.5774).

It does not survive the attribution test.

| held fixed | `volume_rate_ratio` | `excursion_distance_va` |
|---|---|---|
| nothing | 0.5808 | 0.6800 |
| 5 strata of the other | 0.5035 null | 0.6646 |
| 25 strata | 0.4931 null | 0.6622 |
| 50 strata | **0.4868 INVERTED** | **0.6619** |

Distance keeps ~97% of its standalone separation with the ratio held fixed. The ratio
keeps **none** with distance held fixed, and degrades to mildly inverted as the control
tightens. No slice rescues it: ABOVE 0.5085, BELOW 0.4984, `SESSION`-baseline subset
0.5131.

The direction of the residual bias matters for reading this. Coarse strata **overstate** a
proxy — a synthetic pure proxy reads 0.7121 at 2 strata, 0.5920 at 5 and 0.5013 at 100 —
so a quantity already at ~0.50 under coarse stratification is a *conservative* null. That
granularity behaviour is pinned as a test, because it is what licenses the reading.

### The measurement was also being taken at its thinnest point — checked, and it does not help

The corpus fires once per excursion, which pinned `consecutive_closes` to exactly
`ACCEPT_MIN_CANDLES` in **all 31,079 rows**. So the whole corpus measured acceptance at
the earliest and noisiest instant available, while Market Profile's acceptance premise is
about **time** — value is supposed to *build* out there — and the live scanner re-evaluates
a long-running excursion every cycle rather than only on its third candle. Declaring the
discriminator dead on that sampling risked a **false null**, which is the expensive error
here.

So `--every-candle` was added (348,103 events over 14,268 excursions, 60 symbols) and the
measurement re-read at each duration. Slicing by `candles_into_run` makes each slice
independent — one row per excursion — which the pooled corpus is not.

| read at | n | within-distance AUC | |
|---|---|---|---|
| candle 3 | 13,033 | 0.4978 | null |
| candle 6–9 | 29,284 | 0.4916 | inverted |
| candle 10–19 | 46,641 | 0.4917 | inverted |
| candle 40+ | 46,451 | 0.4895 | inverted |

Flat at every duration. A matched cohort of the 4,671 excursions that reached candle 20,
read at 3/5/10/15/20 so only the timing varies, peaks at **0.5090 [0.4875, 0.5305]** —
still null, and that mild rise tracks the label's base rate moving toward balance, which
is where an AUC is best estimated. **Acceptance does not arrive late. It does not arrive.**

Two sampling traps in this corpus, recorded so the numbers are not misread:

- The continuation base rate rises 28.0% → 78.1% from candle 3 to candle 40+. That is
  **survivorship**, not signal: an excursion still running has by definition not yet
  returned inside value, and returning inside value is what REVERTED means.
- The cohort's directional rate (92.6% away at candle 3) is **selection on the future** —
  the cohort was chosen for having stayed outside 20 candles. It is not a tradable edge
  and is not counted as one. It remains valid for comparing the ratio's power *across
  reads*, because the same selection applies to every read.

### The label itself is distance-coupled, so distance's 0.68 is not an edge either

`CONTINUED` is measured from the decision close and is clean. `REVERTED` means "closed
back inside the value area", which is near-automatic 0.1 value widths outside the edge and
hard at 1.5. So distance predicts this contrast **partly by definition**, and its 0.68
cannot be read as a directional edge.

The scale-free test settles it. Among excursions where **exactly one** of two symmetric
thresholds was reached, was it the favourable one? Under directional neutrality that is
50% at every threshold in every unit, because both thresholds carry the unit and it
cancels. Excursions hitting both or neither are dropped — they carry no direction, and
scoring "neither" as a failure to continue is what made earlier race labels read as
reversion edges.

| threshold | n | away from value | |
|---|---|---|---|
| ±0.25 VA | 17,395 | 44.8% [44.1, 45.6] | reversion |
| ±0.50 VA | 18,148 | 47.1% [46.3, 47.8] | reversion |
| ±1.00 VA | 10,814 | 49.2% [48.3, 50.2] | flat |
| ±0.25 ATR | 14,946 | 48.7% [47.9, 49.5] | reversion |
| ±0.50 ATR | 5,758 | 53.9% [52.6, 55.2] | **continuation** |
| ±1.00 ATR | 1,505 | 60.0% [57.5, 62.4] | **continuation** |

Against this label **both** candidate variables are null: ratio 0.5070, distance 0.4860.

### The one real directional finding is about TARGETS, not about entries

The units disagree, and that is not a contradiction to resolve by picking one. A value
area is typically **0.34 ATR** wide (p50; p05 0.137, p95 1.065), so ±0.50 VA is a *small*
move that resolves inside the value area's pull, while ±0.50 ATR is ~1.5 value widths and
resolves outside it. **Small excursions revert; large ones continue.** The crossover is a
property of the market, and reporting both units is what keeps the choice of unit from
deciding the verdict silently — the report now prints all eight rows for that reason.

This bears on `TARGET_MODE` and `TARGET_FIXED_R`, not on acceptance. It is the substantive
thing Phase 1 and Phase 2 have to test.

### Action taken, and what was deliberately NOT done

- **The thresholds are unchanged at 0.80 / 0.45.** They could be moved to symmetric
  percentiles so each setup gets a comparable population, and there is a clean
  measurement-power argument for that. But setting them from this outcome data is
  precisely the threshold-fitting that was ruled out in advance, and Phase 1's job is to
  measure the system as designed. They stay as reasoned defaults, now explicitly marked
  **unvalidated** rather than calibrated.
- **`excursion_distance_va` was NOT promoted to the discriminator**, despite being the
  variable that carries the label. Its power is against a definitionally distance-coupled
  label and it is null against the scale-free one; promoting it would be building on the
  artifact rather than on a finding.
- **S3 is not disabled.** The pre-committed rule is that a setup failing its *phase gate*
  gets disabled rather than loosened. Phase 0 is calibration, not a gate — the gate is
  Phase 1's net-R measurement, which is now running. What Phase 0 establishes is that S3
  enters that test **with no prior support for its premise**, which is a fact about the
  prior, not a verdict.
- **Phase 3's ablation gains a required arm:** remove the acceptance gate entirely. On
  this evidence it is a structural device that makes S2 and S3 mutually exclusive, and
  nothing more. If the ablation shows no cost to removing it, it should go.

---

## 12. The naked-POC approximation was wrong on half the corpus, and ~90% of the sessions that matter

`profile/composite.py` was the last unwritten module. Until now `scanner.naked_pocs`
estimated each prior session's POC by its **typical price**, `(H+L+C)/3`, with the
reasoning that the exact POC would need that session's 1m candles and a fetch per session
per symbol is unaffordable. The premise was right and the conclusion did not follow.

**It needs no fetch.** `frozen_bundle` already builds the previous session's complete
profile and levels once per symbol per session, and already writes `poc_price`, `high` and
`low` to `profile_snapshots`. A closed session's POC is immutable, so the registry is a
*read* over data the system was already producing. Live it accumulates as the bot runs and
reloads from the journal after a restart; replay needs no separate path at all, because
`available_sessions` sorts ascending and `replay_symbol` iterates in order, so processing
session N records session N−1's POC and the registry warms up exactly as it does live.
Writing a corpus-scanning builder was the obvious first move and would have been worse: it
would have handed replay a fully populated registry from its first session, which live
never has.

### How wrong the approximation was, measured rather than argued

The error is provable without any new data. For `(H+L+C)/3` the estimate's position within
the session range is

```
((H+L+C)/3 − L) / (H−L)  =  (1 + c) / 3        where c = (C−L)/(H−L)
```

so as the close `c` sweeps its entire range from 0 to 1, the estimate is **confined to
[0.333, 0.667]**. Any session whose true POC sits outside that band is one the
approximation could not have located at any close whatsoever.

Measured `poc_position` over 14,185 sessions: p05 0.118, p50 0.474, p95 0.825.
**7,107 of 14,185 — 50.1% [49.3, 50.9] — fall outside the reachable band.**

| prior shape | n | true POC outside the band |
|---|---|---|
| D (balanced) | 4,244 | **0.0%** [0.0, 0.1] |
| B | 3,662 | 60.8% [59.2, 62.3] |
| trend | 2,909 | 64.1% [62.4, 65.8] |
| P | 1,463 | 88.7% [86.9, 90.2] |
| b | 1,907 | **90.2%** [88.8, 91.4] |

D at exactly 0.0% is the check that the arithmetic is right rather than a lucky result:
the shape classifier *defines* D by `SHAPE_B_MAX_POC_POSITION`–`SHAPE_P_MIN_POC_POSITION`,
i.e. POC position in [0.35, 0.65], which is inside [0.333, 0.667] by construction. The
measurement agrees with a definition it was not given.

So the approximation was accurate exactly where it did not matter — balanced sessions,
where the POC sits mid-range and a price average finds it — and wrong ~90% of the time on
the one-sided shapes. That is the worst possible distribution of error, because a
**one-sided session is precisely the one that leaves an abandoned POC worth targeting**:
value migrated, price left a shelf behind, and the shelf is the unfinished business. On a
balanced session there is rarely a naked POC to find in the first place.

Magnitude, in ATR: the *minimum* mis-location for those 7,107 sessions is p50 0.085,
p90 0.333, p99 0.977, max 3.80 ATR. Minimum because it assumes the close sat exactly where
it would drag the estimate as near the true POC as arithmetically possible.

### Why this was worth fixing before Phase 2 rather than after

`TARGET_MODE=structural` is the measured challenger to `fixed_r`, and it draws its targets
from this list. Feeding it invented levels would have produced the same unfair outcome as
the `structural_target` self-rejection bug (finding 9's neighbour): a mode losing a
head-to-head because of its inputs rather than its thesis. Phase 1 is unaffected — it runs
`fixed_r`, and `choose_target` never consults `naked_pocs` in that mode — so the run in
flight did not need restarting.

### The cold start is reported, not hidden

A fresh deployment has no prior snapshots and therefore no naked POCs, and the list fills
over the following days. `NakedPocRegistry.coverage()` exposes that, because an empty list
has two opposite causes — "price revisited every prior POC" and "this deployment has no
history" — and no caller can distinguish them from the list alone. The alternative was
falling back to the approximation, and a target drawn from a fabricated level is worse than
no target: setups with no naked POC fall back to their structural levels, which is a
documented path rather than a failure.

`tests/test_composite.py` adds 28 tests. Three pin the anti-lookahead bounds
specifically, because this is the one place in the profile engine where reading *later*
sessions is correct — the nakedness test must read them — which makes it the one place a
real leak would look like normal operation. One pins `>` against `>=` in the
traded-through scan: a session's own range necessarily contains its own POC, so a single
character there would cancel every POC the instant it was recorded and the feature would
silently return an empty list forever.

---

## 13. The Phase 1 gate was measuring confidence it had not earned

Audited *before* the Phase 1 data landed, specifically because finding 11 turned up a
wrong statistic in `report_acceptance`. The same question was asked of `replay.report`,
which is the instrument that decides whether a setup passes: **is it measuring what it
claims?** Two answers came back no.

### The intervals assumed independence, and the risk module already denies it

`summarise_r` used `mean_interval`, which divides by `sqrt(n)` because it treats every
trade as independent evidence. These trades are not. Roughly two thousand of them sit
inside about a hundred and eighty UTC days, and on any one day the whole alt perpetual
complex moves together.

`risk.py` already argues this from the other direction, in its own docstring: *"Alt
perpetuals are close to a one-factor market: ten alt longs is one leveraged bet on BTC,
not a diversified book."* A statistic cannot assume the diversification the risk module
explicitly denies. Twenty trades taken on one day during one BTC move are close to **one
observation repeated twenty times**; counting them as twenty shrinks the interval by up
to `sqrt(20)` and turns a coin flip into a finding. This is the standard way a backtest
reports confidence it has not earned, and it fails in the direction that manufactures
significance.

`clustered_mean_interval` now computes the variance **between** UTC sessions rather than
between trades, with the usual `G/(G−1)` correction. Two analytically known cases pin it:
trades uncorrelated within clusters return approximately the naive interval (so genuinely
independent data is never penalised), and trades identical within clusters inflate the
standard error by `sqrt(cluster size)` (so correlated trades pay for their own
precision). On synthetic data with a realistic per-day common shock the correction widened
the standard errors **1.5×**; with real trade density per day it will be larger.

The report now prints both intervals and the ratio between them, because **the gap is
itself a finding** — it says how much of the nominal sample size was repetition. It also
marks with `~` any setup that would have been starred under the independence assumption
but is not once correlation is paid for. That character is the most useful one in the
report: it is exactly the trade this project must not ship.

This correction propagates for free to the matched-random control, since `treatment_se`
reads the summary's standard error — so a lift that only looked real because trades were
counted as independent will no longer clear its interval either.

### ~16 comparisons at 95% each is not a 95% test

The report tests four setups pooled, each setup by direction, and each setup's split
halves. At a nominal 95% apiece the chance that **at least one** star appears by chance is
about **56%**, not 5%. A single star among many comparisons is not evidence, and the
count has to be printed beside the stars or it gets forgotten.

The primary gate is the per-setup pooled mean — four tests — so the report now also prints
the Bonferroni-corrected bar, `z = 2.50` rather than 1.96, and each setup's interval at
that bar. Bonferroni because it is conservative and the asymmetry is stark: the cost of a
false positive here is shipping a strategy with no edge.

The corrected bar is stated as a **floor, not the test**. Replication across both halves
and a lift over the matched random control are stronger guarantees than any p-value
adjustment, and they remain the real gate.

`tests/test_metrics.py` covers all of this (45 tests): the normal quantile against
published values, the two bracketing cluster cases, and a deterministic fixture built to
sit *between* the naive and clustered significance bars — the region where the
independence assumption invents a finding — with a further test guarding that the fixture
still sits there, since if it drifts the demotion tests prove nothing.

### `merge.py` was untested, and its type map had already drifted

The merge is the single step between three hours of replay and the answer, and it had no
tests. Its hand-written list of numeric columns had fallen behind `TRADE_FIELDS`, leaving
`exit_price`, `exit_ts` and `decision_index` as **strings** in the merged dataset —
invisible only because nothing had yet done arithmetic on them. Types are now declared
once as `TRADE_FIELD_TYPES` beside `TRADE_FIELDS`, and a test asserts the declaration
covers the schema, so adding a field without classifying it fails loudly instead of
arriving as text in a Phase 2 comparison. 19 tests, including that a blank cell stays
`None` and never becomes `0.0` — which would score an unfilled entry as break-even and
flatter a setup in exact proportion to how often its entries go unfilled.

---

## 14. Acceptance IS real — I measured it with the wrong instrument

Finding 11 concluded that the S2/S3 discriminator carries no information, and left the
thresholds unvalidated. That conclusion was too broad, and the correction matters more than
the original result.

**What finding 11 actually established** is that `volume_rate_ratio` — volume per candle
outside value over a baseline rate — is inert. What it did *not* establish, but implied by
proximity, is that *acceptance* is inert. Those are different claims, because the volume
rate was never the theory's definition of acceptance. Market Profile defines acceptance by
**time**: price is accepted when it trades at a level long enough for value to develop
there. The volume rate was a convenience proxy I substituted for that, and the proxy is
what failed.

### The same control, applied to the quantity the theory actually names

`candles_into_run` was sitting in the every-candle corpus the whole time, and finding 11
reported only its standalone AUC (0.5768) without ever putting it through the
distance-stratified control that condemned the volume ratio. Standalone AUC is precisely
the number that had already misled once.

The first attempt at the control was wrong in a way worth recording: it pooled every read
of every excursion, reporting `n = 118,287` when the ~15 reads per excursion made that a
gross overcount, and it length-weighted the sample — a 60-candle excursion contributed 60
rows against a 4-candle excursion's 4, and long excursions are exactly the ones that
continued. The "away" rate jumped from ~47% to 60–68% purely from that bias. **This is the
same independence error corrected in `replay.report` earlier the same day, made again one
script later.**

Redone on independent rows — one read per excursion sampled uniformly, session-clustered
standard errors by delete-one-cluster jackknife, five seeds:

| measure | alone | within distance strata | within decision-index strata | seed spread |
|---|---|---|---|---|
| **time outside value** | **0.658–0.669** | **0.606–0.619** | 0.640–0.649 | 0.013 |
| volume rate ratio | 0.545–0.550 | ~0.50 null | — | — |

It holds at larger scales too: 0.586 at ±1.0 VA, 0.584 at ±0.5 ATR, 0.555 at ±1.0 ATR.

### The relationship, and where the crossover sits

Against the scale-free directional label, on independent rows, the continuation rate rises
**strictly monotonically across every band**, with non-overlapping intervals at the
extremes:

| time outside value | n | continues |
|---|---|---|
| 0.8 h+ | 3,362 | 28.4% [26.9, 30.0] |
| 1.2 h+ | 1,633 | 35.9% [33.6, 38.2] |
| 2.0 h+ | 1,149 | 46.8% [44.0, 49.7] |
| 3.2 h+ | 892 | **53.6%** [50.3, 56.8] |
| 5.2 h+ | 710 | 61.7% [58.1, 65.2] |
| 8.5 h+ | 802 | 73.1% [69.9, 76.0] |

The crossover from reversion-dominant to continuation-dominant is at **2–3 hours outside
value**. That is the two-state auction model stated as a number: brief excursions are
rejections and revert, sustained ones are acceptance and continue.

### Why this is not lookahead, checked specifically

At the decision point the system knows price has been outside for *d* candles. It does not
know the excursion's eventual length, and the outcome is measured forward **from** that
read. Conditioning on "has been outside for *d*" is exactly the information available live.
The sampling scheme means a read at duration 50 only exists for excursions that lasted 50+,
but that is a property of the information set, not a leak: live, at that moment, the same
is true.

### What changed in the code

`ACCEPT_DISCRIMINATOR` selects `duration` (new default) or `volume_rate`. Thresholds
`ACCEPT_MIN_CANDLES_OUTSIDE = 13` (3.25 h) and `REJECT_MAX_CANDLES_OUTSIDE = 5` (1.25 h)
are set from the **distribution** — p75 and p40 of duration at a decision point — not from
whichever band scored best. The gap between them still carries the mutual-exclusion job.

Three consequences worth noting:

- **Duration needs no denominator**, which removes this module's entire recurring failure
  mode. Findings 7, 8 and 11 were all variations on a measure contaminated by its own
  bound; there is nothing to divide here, so `NO_BASELINE` no longer blocks a verdict under
  the duration arm. That difference between the arms is asserted directly.
- **The ended-excursion three-way split reads better under duration.** A three-candle spike
  that returned is a *rejection*; an excursion that held outside for four hours and then
  came back is a *failed auction* — real business done and undone. Same three categories,
  now measured by the quantity that carries the information.
- **An `allow_rejected` flag preserves a strategy decision I nearly broke.** While price is
  still outside value the excursion is in progress and can only be not-yet-accepted;
  rejection is *confirmed* by the close back inside, which is S2's actual trigger. My first
  version let the live path return `REJECTED`, which would have turned "fade a confirmed
  rejection" into "catch a falling knife" — a different strategy under the same name, and
  one that no test would have caught because both versions produce trades.

`volume_rate` is kept rather than deleted, because the Phase 1 run already in flight
launched before this change and is therefore the **control** for it. A switchable setting is
evidence; a deleted branch is only an assertion. Its existing tests are pinned to that arm
(`AcceptanceDiscriminatesTests`, `S2ReversionTests`) so the control cannot rot, and
`S2ReversionDurationTests` covers the new default — including a test that duration alone
flips the verdict at **identical volume**, which is the regression test for the new
discriminator turning out as inert as the old one.

226 tests pass.

---

## 15. PHASE 1 RESULTS — no setup beats a random entry at its own location; all four disabled by default

Two full Phase 1 replays (120 symbols, ~180 sessions each) plus the matched-random
control, run as a deliberate pair: the first launched before finding 14's acceptance fix
and is therefore the **volume_rate control**, the second is the **duration arm** — same
corpus, same code, one changed setting. Both point the same way.

### The P&L, before any control

| setup | volume_rate arm | duration arm |
|---|---|---|
| S1-POC | n=308, mean **−0.26R** | n=308, mean **−0.26R** *(identical — S1 does not consult acceptance)* |
| S2-VAR | n=448, mean **−0.27R** | n=1,462, mean **−0.31R** *(3.3× more candidates qualify; not more profitable)* |
| S3-BRK | n=2,210, mean **−0.12R** | n=1,965, mean **−0.15R** |
| ALL | n=2,980, mean **−0.16R** | n=3,749, mean **−0.22R** |

The mechanics were checked before drawing any conclusion, because a uniform loss across
every setup is exactly what a cost or sign bug produces: STOP outcomes average **−1.03R**,
TARGET outcomes average **+1.97R** (matching `TARGET_FIXED_R=2.0`), cost drag is a sane
**+0.034R**, and `structural:POC` target kinds are all S1-LVN working as designed. No bug.
The actual arithmetic: at a 2R:1R payoff the breakeven win rate is **34.3%**; the realised
win rate is **27.0%**. A real hit-rate shortfall, not an implementation error.

**The duration fix (finding 14) did not rescue profitability.** It changed which
candidates qualify — S2-VAR's population grew 3.3× under the theoretically correct
discriminator — but that larger, better-measured population is not more profitable; if
anything it is very slightly worse. Finding 14 stands as a measurement result (duration
carries information the volume-rate proxy did not). It is not, on this evidence, a
trading result.

### The matched-random control — the finding that decides the setups' disposition

Every real trade gets twins: same symbol, session, direction, stop distance and target
multiple, entered at a uniformly random confirmation bar in the same session instead of
the setup's chosen one. Everything is held constant except the ONE thing a volume profile
setup claims to contribute — which bar to enter on. Run through the identical `simulate()`,
so no discrepancy in fill rule, pessimistic-bar rule or fee model can leak in as false lift.

| setup | lift (volume_rate arm) | lift (duration arm) | verdict |
|---|---|---|---|
| S1-LVN | −0.20 [−0.89, +0.50] | −0.06 [−0.72, +0.60] | too thin (n=14) |
| S1-POC | +0.12 [−0.10, +0.35] | +0.05 [−0.17, +0.28] | no separation |
| S2-VAR | +0.07 [−0.09, +0.22] | +0.08 [−0.02, +0.19] | no separation |
| **S3-BRK** | **−0.39 [−0.60, −0.17]** | **−0.47 [−0.71, −0.24]** | **WORSE than control** |

S1-POC and S2-VAR are indistinguishable from a random entry at the same location, in both
arms — not proven to have an edge, not proven not to. S3-BRK is the one setup with an
affirmative negative finding, replicated under two independent acceptance measurements: its
control's win rate is **43–45%**, its own win rate is **29–30%** (Wilson intervals do not
overlap). This is not "the setup found no edge" — it is "the setup's entry-selection logic
picks a worse spot than chance would, at the identical location."

**Why, mechanically.** S3-BRK requires an accepted breakout, then a pullback of bounded
depth, then continuation back past the pre-pullback high, before it will enter. By the time
every one of those conditions is satisfied, a real part of the favourable move has
typically already happened — so a random bar anywhere in the same excursion, sharing the
same stop and target, does at least as well. The theory that predicts continuation is not
what is being refuted here; the specific rule for WHEN inside that continuation to enter is.

The direction split and the time split replicate the same story independently of the
control: S3-BRK BUY averages **+0.16R**, SELL averages **−0.52R**, in both runs; and the
split-half-by-time check **flips sign** (early negative, late positive), also in both runs.
Neither of those needed the control to see, and both point at the same setup.

### Action taken — the pre-committed rule, applied

*"Any setup failing its phase gate is disabled by default, not loosened"* was agreed before
Phase 0 began specifically to remove discretion at a moment like this one. The phase gate
is this control: a positive lift with a 95% CI excluding zero. **None of the four setups
clear it.** `S1_POC_ENABLED`, `S1_LVN_ENABLED`, `S2_ENABLED`, `S3_ENABLED` are now `False`
by default in both `config.py` and `.env.example`, each with the specific evidence recorded
inline. Their dispositions are not identical, and the comments say so:

- **S1-LVN** — unmeasured (n=14). Needs a wider universe or longer history before its gate
  means anything; not a finding either way.
- **S1-POC, S2-VAR** — measured and inconclusive. No separation from random. Disabled
  because the rule doesn't loosen on an absence of evidence, not because harm was shown.
- **S3-BRK** — measured and negative, replicated twice. The strongest signal of the day,
  and it says "fix the entry rule," not "this setup is close." Should not be re-enabled on
  a partial retest or a threshold nudge.

**Research must not go blind when a setup is disabled.** `research/replay.run()` now
force-enables all four setups by default regardless of their live-trading flag
(`force_enable_setups=True`), because a disabled setup is exactly the one a fix or Phase 3's
ablation most needs to measure. Without this, today's own disable would have silently
zeroed every future replay's candidate count for these setups. `--respect-live-flags` on the
CLI opts out, for the rarer case of deliberately reproducing what the live bot would trade
today. `tests/test_pipeline.py` pins all four flags `True` at module scope for the same
reason in the other direction — that file tests detection logic on hand-built fixtures, not
whether a setup should be live-traded.

230 tests pass.

---

## 16. S3-BRK's redesigned entry rule — closed most of the gap, did not clear the bar

Finding 15 diagnosed a mechanism, not just a symptom: real S3-BRK entries scored like
twins entered *after* them and nothing like twins entered *before* them, median 6.2h
earlier, same session and direction — the pullback-and-continuation wait was consuming
the part of the move that pays. `S3_ENTRY_MODE="accepted"` tests the fix that diagnosis
implies: enter the instant acceptance itself confirms, no pullback wait, stop moved from
the (now nonexistent) pullback extreme to the value bound the excursion broke from. Built
alongside "confirmed" as a switch — same pattern as `ACCEPT_DISCRIMINATOR` — so this is a
controlled comparison on the same corpus, not a replacement on faith. Full 120-symbol
replay, duration discriminator, matched-random control:

| | n | mean R | control mean | lift | 95% CI | verdict |
|---|---|---|---|---|---|---|
| pooled | 2,857 | −0.11R | +0.01R | **−0.12R** | [−0.31, +0.07] | no separation |
| BUY | 1,506 | **+0.13R** | +0.28R | −0.15R | [−0.43, +0.14] | no separation |
| SELL | 1,351 | −0.38R | −0.28R | −0.10R | [−0.39, +0.20] | no separation |

Free regression check first: S1-POC (569), S1-LVN (14) and S2-VAR (2,438) row counts are
bit-for-bit identical between this run and finding 15's duration arm, confirming
`S3_ENTRY_MODE` touches only `va_breakout.py` as designed.

**The diagnosis was right about the mechanism and insufficient as a fix.** Lift moved from
clearly negative under "confirmed" (−0.39R and −0.47R, both CIs excluding zero, replicated
across both acceptance discriminators) to a CI that now contains zero. "No separation" is
a real improvement over "worse than control" — the entry no longer loses to a random bar
at the same location — but the phase gate is BEATS CONTROL, and this does not clear it.
Win rate tells the same story at a coarser grain: the confirmed-mode gap was 29–30% vs a
non-overlapping 43–45% control; here it is 30.6% vs 34.6%, much closer but still control's
favour.

**The BUY/SELL split did not close — it moved into the control.** Under "confirmed" the
asymmetry sat entirely in S3's own entries (BUY +0.16R / SELL −0.52R) while random entries
scored evenly. Here BUY's own mean turned **positive** (+0.13R) — the re-timing genuinely
helped that side — but SELL's *control* now also runs negative (−0.28R), which nothing
before this run had shown. A random entry in a SELL excursion at this location and geometry
is a bad bet regardless of when within it you enter. That is a location/direction property
of the population S3-BRK-SELL draws from, not a timing defect re-timing the entry rule can
fix — it says where to look next (SELL-side excursion quality, not entry mechanics), not
that the fix failed on its own terms.

**Action taken.** `S3_ENTRY_MODE` default stays `"confirmed"` — neither mode has earned a
change from the status quo, so there is no evidence-backed reason to move it. Both modes
stay available; `S3-BRK` stays disabled under either. Recorded in `config.py` inline,
matching the other three setups' comments. This moves S3-BRK from the only setup with an
*affirmative negative* finding into the same DISABLED-UNPROVEN bucket as S1-POC and
S2-VAR — closer to shippable, not shipped, and the SELL-side control result is the
concrete next question if this setup is revisited.

237 tests pass (unchanged by this finding — no new code path, only a config default and
its evidence comments).

---

## 17. The BUY/SELL asymmetry is a market-drift confound, not a setup defect

Finding 15's own numbers already showed it and it went unremarked at the time: every one
of the four setups' SELL side underperforms its BUY side, sharply, on the identical
120-symbol corpus. Chased down now because S3-BRK's accepted-mode SELL result stayed
negative even in its own matched-random control (finding 16) — a control that never
touches any setup's entry logic, which rules out "S3's detection is bad at shorting" as
the explanation by construction.

### It is in the control, for every setup, not only S3-BRK

`phase1_duration/controls.csv`, mean R by direction — twins share only symbol, session,
direction and geometry with their real trade; nothing about which setup produced them:

| setup | BUY control | SELL control |
|---|---|---|
| S1-POC | −0.17R | −0.43R |
| S1-LVN | −0.31R (n=10, too thin) | −0.35R |
| S2-VAR | −0.27R | −0.49R |
| S3-BRK | +0.65R | −0.08R |

A random entry, in every setup's population, does better long than short. Random entries
carry no setup logic at all — this cannot be an S1/S2/S3 detection problem, because the
thing being measured has no detection in it.

### The gap is concentrated in time, which is the market-drift signature

Splitting the same control rows at the corpus midpoint (119 sessions, split 2026-07-29):

| | BUY | SELL |
|---|---|---|
| early half | −0.08R | −0.19R |
| late half | **+0.44R** | **−0.42R** |

The asymmetry roughly doubles in the second half of the window, in both directions at
once. A stable structural property of shorting crypto alts would show up evenly across
the whole period; a market that spent the back half of the sample drifting up produces
exactly this — inflated random longs, depressed random shorts, growing over time rather
than constant. This is consistent with, not proof of, broad upward drift in the tested
universe over the corpus's later sessions.

### Confirmed directly against BTCUSDT and ETHUSDT — already-cached data, same window

Both benchmarks already exist in the fetched corpus, so this needed no new download —
daily open/close read straight from `data_cache/1d/`, split at the identical boundary
(2026-07-29) finding 17's control split used:

| | early half (05-31 → 07-29) | late half (07-29 → 09-26) |
|---|---|---|
| BTCUSDT | **−13.39%** | **+32.04%** |
| ETHUSDT | **−4.95%** | **+40.27%** |

Not a marginal drift — a real, sharp regime change, and it lands in exactly the half of
the corpus where the control's BUY/SELL gap widened. Alt perps move with BTC/ETH closely
enough (the reasoning already stated in `config.py` for capping exposure per direction)
that this is sufficient confirmation without pulling the full 120-symbol basket. The
diagnosis stands confirmed, not merely consistent: the corpus's back half was a genuine,
large bull run, and every direction-symmetric reading of Phase 1 needs to be filtered
through that fact.

### What this changes, and what it does not

**It does not rescue any SELL-side result** — a headwind that also depresses the control
is still a headwind on the real trades. Finding 16's disposition for S3-BRK-SELL stands.

**It does temper every BUY-side improvement claimed so far**, including S3-BRK-accepted's
BUY mean turning positive (+0.13R) in finding 16 — some or most of that could be the same
drift lifting every BUY-direction population in the corpus's later sessions, real trades
and twins alike, rather than the entry-timing fix specifically. The lift-over-control
number (−0.15R, still no separation) already nets this out for the comparison that
matters, so finding 16's verdict does not change — but the raw BUY mean should not be read
as "the fix made BUY profitable."

**It reframes what Phase 1's numbers can and cannot say.** Every result in findings 15-16
was measured on one historical window. A setup's apparent edge (or lack of one) on this
corpus is entangled with whatever this corpus's market did, and a corpus with a strong
one-sided drift in its back half is not a neutral test bed for direction-symmetric claims.
This does not invalidate the phase-gate methodology — the matched-random control is
specifically what surfaces confounds like this rather than hiding them — but it means a
future replay on a different historical window is worth more than another pass over the
same one.

No code or config changed for this finding — it is a measurement result about the test
data, not about the system.

---

## 18. Phase 3 attribution — none of the four opinion gates rescue S1-POC or S2-VAR

The mechanism first: two of the five opinion gates (`shape_opposed`, S2's own
`poor_at_extreme`/`excess_at_extreme` overlays) were already recorded on every stored
row. The other two that matter here — order-flow delta and session-over-session value
migration — were computed by the pipeline for other purposes but discarded before
reaching the research CSV. Widened `TRADE_FIELDS` to capture `bin_delta_normalized`
(the exact quantity `gates.checks.delta_opposed` would gate on, computed at whichever
level each setup actually trades — POC for S1-POC, the traded VAH/VAL bound for
S2-VAR) and `value_migration` (already computed per session, just not threaded
through), and fixed `poor_at_extreme`/`excess_at_extreme` to survive the CSV round
trip. `research.control`'s twin-builder was extended to copy the same four fields —
valid because they are properties of the session/level, not of when within it a twin
enters. 9 new tests (246 total) pin the computation and the CSV round trip, including
the specific regression a raw Python bool written to CSV and reread as text would
cause (`float("False")` raises).

A fresh full 120-symbol replay (identical candidate selection to finding 15's duration
arm — row counts match exactly, S1-POC 569, S1-LVN 14, S2-VAR 2,438, S3-BRK 4,715 —
confirming the schema change touched no detection logic) captured all four gates'
inputs on real data. Each was tested the same way as findings 15-16: restrict the
matched-random control to the gate-passing population only, and ask whether that
turns "no separation" into "beats control".

| gate (population kept) | S1-POC lift | S2-VAR lift | verdict |
|---|---|---|---|
| baseline, unfiltered | +0.05R [−0.17,+0.28] | +0.08R [−0.02,+0.19] | no separation |
| `shape_opposed` (shape==D only) | *no-op — see below* | +0.10R [−0.05,+0.25] | no separation |
| `delta_opposed` (bin delta ≤ 0.20 vs direction) | +0.05R [−0.19,+0.29] | +0.07R [−0.05,+0.19] | no separation |
| `htf_value_opposed` (day migration not opposed) | +0.05R [−0.18,+0.27] | +0.09R [−0.02,+0.19] | no separation |
| `S2_REQUIRE_POOR_EXTREME` | — | n=5, too thin | insufficient data |

**None clear the bar.** Every filtered lift's 95% CI still contains zero, and every
movement from the unfiltered baseline is inside noise — S2-VAR's best single filter
(`shape_opposed`) moves the point estimate from +0.08R to +0.10R while the interval
stays essentially the same width and still straddles zero. No individual gate is
close to rescuing either setup on its own.

**S1-POC's `shape_opposed` row is a true no-op, not a null result.** S1-POC already
requires `prior_shape.label == "D"` as its own condition 2 (`poc_rotation.py`) — every
S1-POC candidate that exists is already shape-balanced, so a gate that removes
shape-opposed candidates has nothing left to remove there. Recorded as a structural
fact, not tested and found wanting.

**`S2_REQUIRE_POOR_EXTREME` could not be tested at all.** Only 5 of 2,438 S2-VAR
candidates tested a "poor" (no-tail) extreme on this corpus — the theorised
population is almost empty, not merely small. This needs a much wider corpus before
it can be judged either way; it is not evidence against the hypothesis.

**What this does and does not rule out.** Single-gate filtering is the cheapest test
and the one the gates were built for, and it does not work. It does not rule out a
*combination* of gates, or a differently-calibrated threshold on `GATE_DELTA_OPPOSED_MIN`
— but testing those now would mean either an open-ended search (which the project's
own multiplicity lesson, finding 13, says not to run without correcting for it) or a
specific, pre-registered combination with a stated reason. Neither is done here. On
present evidence, S1-POC and S2-VAR's disposition from finding 15 stands unchanged:
disabled, unproven, and these four gates are not the fix.

246 tests pass.

---

## 19. TARGET_MODE="structural" — the first setup to beat its own control

Untested until now: `TARGET_MODE` has had a "structural" arm (aim at the nearest
untested POC, this session's POC, or the opposite value edge - whichever is nearest
that still clears a minimum R) since early in this project, built specifically
because a fixed 2R target was one of the concrete weaknesses in the source material.
It had never actually been run against S1-POC or S2-VAR. Full 120-symbol Phase 1
replay, `TARGET_MODE=structural` applied to all four setups, matched-random control
on the result.

### S2-VAR clears the phase gate

| | n | mean R | control mean | lift | 95% CI | verdict |
|---|---|---|---|---|---|---|
| pooled | 936 | −0.24R | −0.39R | **+0.1515R** | **[+0.0202, +0.2828]** | **BEATS CONTROL** |

This is the first setup in the project to clear the pre-committed bar (positive lift,
95% CI excluding zero) since Phase 1 began. The margin is not large - the lower bound
sits close to zero - so it was checked against replication before being taken at
face value, the same discipline finding 15 already established for a negative
result:

| split | n | lift | 95% CI |
|---|---|---|---|
| BUY | 452 | +0.1814R | [−0.0618, +0.4246] |
| SELL | 484 | +0.1215R | [−0.0551, +0.2980] |
| early half | 460 | +0.1456R | [−0.0404, +0.3315] |
| late half | 476 | +0.1571R | [−0.0302, +0.3445] |

None of the four individual splits clears its own bar alone (each has half the
sample, so a wider interval was expected) - but **all four point the same direction,
in a tight +0.12R to +0.18R band, and none flips sign.** A result driven by one
lucky subgroup (e.g. the late-half market drift from finding 17 alone) would show up
concentrated in one split and weak or reversed in its complement; this shows up
evenly in all four. That consistency is better evidence than the pooled p-value on
its own, though it does not replace the real test still outstanding: a second
historical window.

**Why, mechanistically.** Under `structural`, 83% of S2-VAR's targets (1,241 of
1,486) aim at a naked or session POC rather than a fixed distance - exactly the
auction-theory claim: a move returning into value should reach the point of
highest agreement, not stop at an arbitrary multiple of its own risk. The remaining
population shrank from 2,438 (fixed_r) to 1,486 because `TARGET_MIN_R` now skips
trades where no structural level offers adequate reward - a real change in which
trades are taken, not just where they exit, and a legitimate part of what
"S2-VAR under structural targeting" means as a strategy.

### S1-POC moves the same direction, does not clear the bar

Pooled: n=271, lift +0.0938R, 95% CI [−0.1452, +0.3327] - still contains zero, though
the point estimate improved from fixed_r's +0.05R. Structural targeting alone is not
the S1-POC fix; the LVN-stop experiment (in progress) remains the more targeted lever
for its specific diagnosed weakness.

### S3-BRK, S1-LVN - no new information

S3-BRK was run at its default `S3_ENTRY_MODE="confirmed"` (already known bad) with
structural targets layered on top - worse than control as expected (lift −0.51R,
95% CI [−0.74,−0.27]), not informative about the entry-mode question finding 16
already answered. S1-LVN stays at n=14, still too thin to judge.

### Action taken

`TARGET_MODE` stays at its `"fixed_r"` default and `S2_ENABLED` stays `False` -
this is Phase 1 evidence, not Phase 2, and finding 17's lesson applies directly: one
historical window is not enough to trust a result that will decide what trades real
capital. Both comments updated with the full evidence inline. The concrete next
steps this unlocks: Phase 2 (fees/slippage) on the structural-target, S2-VAR
population specifically, and replaying against a second, independent historical
window before any default changes.

258 tests pass.

---

## 20. LVN stops for S1-POC and S2-VAR — measured and rejected

The top-priority fix proposed for S1-POC's diagnosed weakness (finding 15: its stop sits
in the POC's own densest, most rotation-prone zone) and the analogous variant for
S2-VAR. Both built as switchable modes (`S1_STOP_MODE` / `S2_STOP_MODE` = `"lvn"`),
fallback-never-tighter by construction, full 120-symbol Phase 1 replay against the
default `TARGET_MODE="fixed_r"`, matched-random control on the result.

### Neither setup beats its own control

| setup | mode | n | mean R | control mean | lift | 95% CI | verdict |
|---|---|---|---|---|---|---|---|
| S1-POC | buffer (baseline) | 308 | −0.26R | −0.31R | +0.05R | [−0.17,+0.28] | no separation |
| S1-POC | lvn | 127 | −0.50R | −0.44R | −0.06R | [−0.35,+0.23] | no separation |
| S2-VAR | extreme (baseline) | 1462 | −0.31R | −0.39R | +0.08R | [−0.02,+0.19] | no separation |
| S2-VAR | lvn | 504 | −0.55R | −0.60R | +0.06R | [−0.09,+0.20] | no separation |

Both lifts stayed inside zero, and both point estimates got *worse* relative to their
own baseline, not better. Replication check:

| setup | split | lift | 95% CI |
|---|---|---|---|
| S1-POC | BUY | −0.14R | [−0.64,+0.35] |
| S1-POC | SELL | −0.01R | [−0.34,+0.32] |
| S1-POC | early half | −0.07R | [−0.51,+0.36] |
| S1-POC | late half | −0.05R | [−0.43,+0.34] |
| S2-VAR | BUY | +0.14R | [−0.26,+0.54] |
| S2-VAR | SELL | −0.02R | [−0.14,+0.09] |
| S2-VAR | early half | +0.04R | [−0.17,+0.25] |
| S2-VAR | late half | +0.07R | [−0.13,+0.27] |

S1-POC is non-positive in all four splits - consistent, just consistently not helping.
S2-VAR's splits **disagree in sign** (BUY positive, SELL negative), so unlike finding
19's structural-target result it does not even clear the cheap replication bar
internally, on top of the pooled interval already containing zero.

### Why it failed: a censoring mechanism, not just "no edge"

The resolved sample size collapsed under "lvn" mode:

| setup | mode | candidates with same setup | resolved (used for R) |
|---|---|---|---|
| S1-POC | buffer | 308 | 308 (100%) |
| S1-POC | lvn | 308 | 127 (41%) |
| S2-VAR | extreme | 1462 | 1462 (100%) |
| S2-VAR | lvn | 1462 | 504 (34%) |

Under `TARGET_MODE="fixed_r"` the target is a fixed multiple of the stop distance. The
LVN/value-edge reference sits, on average, much farther from the POC (or excursion
extreme) than the old ATR buffer - so the stop widens, and the target widens with it.
A wider target takes longer to travel, so far more trades ran out of historical data
before resolving and were excluded from the R calculation entirely (this replay's
resolution window is the same for every arm; only the target distance changed). This
censoring is not random: trades that need the most room to work are exactly the ones
most likely to be cut off first, so the survivors are a biased subsample, not a fair
comparison to the baseline's fully-resolved population. That the absolute mean R and
win rate got worse under "lvn" is at least partly an artifact of which trades survived
to be counted, not purely a verdict on stop placement.

### Action taken

Both flags stay at their current defaults (`"buffer"` / `"extreme"`) - "lvn" is not an
improvement on this evidence. The diagnosis behind the original idea (the POC/excursion
buffer sits somewhere that ordinary two-sided churn can run it) is not refuted by this
result, because the test was confounded by the fixed_r target-scaling link the moment
the stop moved. The clean re-test is the same LVN-stop modes run under
`TARGET_MODE="structural"`, which severs that link (target is chosen structurally, not
as a multiple of the stop) - queued as the next experiment rather than concluding the
LVN idea itself is dead on this one confounded measurement.

258 tests pass (no code changed this pass - this finding is a replay + control run
against the stop-mode switches already built and tested).

---

## 21. LVN stops re-tested under structural targeting — the confound is gone, the signal is underpowered

Finding 20's re-test, as planned: same `S1_STOP_MODE`/`S2_STOP_MODE = "lvn"` switches,
this time with `TARGET_MODE="structural"` so the target no longer scales with the stop
distance. Full 120-symbol Phase 1 replay, matched-random control.

### S2-VAR: point estimate improves, sample too small to separate

| | n | mean R | control mean | lift | 95% CI | verdict |
|---|---|---|---|---|---|---|
| structural, extreme stop (finding 19) | 936 | −0.24R | −0.39R | +0.15R | [+0.02,+0.28] | **BEATS CONTROL** |
| structural, lvn stop (this finding) | 206 | −0.26R | −0.44R | **+0.18R** | [−0.09,+0.45] | no separation |

The point estimate did not fall - it is if anything slightly higher than the result
that already cleared the bar - but the population shrank from 936 to 206 (`lvn` mode
pushes the stop wider, which now trips `GATE:REWARD_BELOW_MINIMUM` more often under
structural's own `TARGET_MIN_R` check, a legitimate filtering effect rather than the
censoring bug in finding 20) and the wider interval swallows the lift. Replication
check, same discipline as finding 19:

| split | n | lift | 95% CI |
|---|---|---|---|
| BUY | 80 | +0.30R | [−0.23,+0.84] |
| SELL | 126 | +0.11R | [−0.19,+0.40] |
| early half | 83 | +0.21R | [−0.19,+0.62] |
| late half | 123 | +0.15R | [−0.21,+0.50] |

All four positive, none flipping sign - the same pattern finding 19 read as evidence,
just on roughly a fifth of the sample and correspondingly wider intervals. Read this as
**an underpowered echo of the same signal, not a separate result and not a refutation**:
nothing here argues against structural targeting's existing evidence, and nothing here
promotes the LVN stop on top of it either. Resolving it needs a bigger population -
more symbols or more history - not a different config.

### S1-POC: still no fix

Pooled n=95, lift −0.03R, 95% CI [−0.38,+0.31] - no separation, and the BUY/SELL splits
disagree in sign (−0.22R / +0.15R). Combining the LVN stop with structural targeting
does not rescue S1-POC either. Two independent stop-placement and target-placement
ideas have now both been tested for this setup (findings 19 and 20/21) without one -
the diagnosed weakness (finding 15) may need a different lever entirely, or may not be
fixable within this setup's current entry definition.

### Action taken

No config change - `S1_STOP_MODE`/`S2_STOP_MODE` stay at their defaults, `S2_ENABLED`
and `TARGET_MODE` stay as finding 19 left them. This result does not move S2-VAR closer
to or further from shipping; it says the LVN-stop lever specifically is not the one
worth spending more replay time on for S2-VAR - the structural-target lever already
measured is. Confirmed as a clean isolation test: S3-BRK (n=1809) and S1-LVN (n=14),
neither of which reads either stop-mode flag, matched the pure-structural-target
baseline exactly.

258 tests pass (no code changed this pass).

---

## 22. Phase 2, part 1: S2-VAR's lift survives worse-than-default costs

Finding 19's `S2-VAR` structural-target result was measured at the system's default
cost model (`FEE_MAKER=0.02%`, `FEE_TAKER=0.05%`, `SLIPPAGE_MODEL_BPS=2.0`). Phase 2
asks whether that lift is an artifact of a lenient cost assumption. Recomputed
`net_r` directly from each row's own `entry_price`/`quantity`/`risk_distance`/`gross_r`
under harsher assumptions - no re-replay needed, since cost is a pure function of
those already-stored fields - applied identically to the real trades AND their
matched-random twins, on the same n=936 population from finding 19.

| scenario | mean R | control mean | lift | 95% CI | verdict |
|---|---|---|---|---|---|
| baseline (current default) | −0.236R | −0.387R | +0.1515R | [+0.0202,+0.2828] | BEATS CONTROL |
| 2x slippage | −0.243R | −0.395R | +0.1515R | [+0.0202,+0.2827] | BEATS CONTROL |
| 3x slippage | −0.251R | −0.403R | +0.1514R | [+0.0203,+0.2826] | BEATS CONTROL |
| taker both legs (no maker fill) | −0.247R | −0.399R | +0.1515R | [+0.0202,+0.2827] | BEATS CONTROL |
| taker both + 2x slippage | −0.255R | −0.407R | +0.1514R | [+0.0203,+0.2826] | BEATS CONTROL |
| stressed: taker both + 3x slippage | −0.263R | −0.414R | +0.1514R | [+0.0203,+0.2825] | BEATS CONTROL |

The lift is invariant to the cost assumption to four decimal places. This is expected,
not a coincidence to double-check: a real trade and its twins share the same symbol,
session, direction, stop distance and target multiple, so they share almost the same
`entry_price`/`risk_distance` distribution too - a harsher cost model subtracts nearly
the same amount from both sides, so it moves the absolute level (mean R gets worse
under every harsher scenario, as it should) without moving the SEPARATION between them.
The lift the setup is being judged on is therefore not a lenient-cost artifact -
it survives every cost scenario tried, including a deliberately stressed one (taker
fills on both legs at 3x the modelled slippage).

### Action taken

No config change. This is the first half of Phase 2 for the structural-target result;
the second half - a second, independent historical window - is the harder remaining
test (Finding 21's own lesson, and finding 17's before it: one window is not enough to
trust a result deciding what trades real capital). That fetch is in progress
separately: `research/dataset.py` gained an `--end-date` option (previously the fetch
window could only end "yesterday") to pull the 120 sessions immediately preceding the
existing corpus (2026-01-30 .. 2026-05-29, non-overlapping) into a separate cache root
(`data_cache_window2`), using the SAME frozen 120-symbol universe rather than
re-ranking by current volume - re-ranking as of today to select which symbols
"count" for a five-month-old window would quietly exclude anything since delisted or
faded, which is exactly the survivorship bias a second window is meant to guard
against, not reintroduce.

262 tests pass (4 new: `tests/test_dataset.py` covers `session_id_range`'s `end_as_of`
- unchanged default behaviour when omitted, the exact 2026-01-30..2026-05-29 window,
that the end date itself is excluded, and oldest-first ordering).

---

## 23. Phase 2, part 2: S2-VAR replicates on a second, independent historical window

The test finding 17 and finding 19 both said was still outstanding. `TARGET_MODE=
structural` replayed against 2026-01-30..2026-05-29 - non-overlapping with the
original 2026-05-30..2026-09-26 corpus, same frozen 120-symbol universe, matched-random
control computed against its own independently-fetched cache (`data_cache_window2`).

### On the second window alone: same direction, same-or-larger magnitude, smaller sample

| | n | mean R | control mean | lift | 95% CI | verdict |
|---|---|---|---|---|---|---|
| window 1 (finding 19) | 936 | −0.24R | −0.39R | +0.1515R | [+0.0202,+0.2828] | BEATS CONTROL |
| window 2 (this finding) | 201 | −0.22R | −0.42R | +0.2018R | [−0.0609,+0.4646] | no separation |

The point estimate did not shrink or flip - it is if anything larger than window 1's.
What kept it from independently clearing is sample size: this window's S2-VAR
population is a fifth of window 1's, driven almost entirely by `INSUFFICIENT_DAILY_
HISTORY` (39.6% of all rejects here, vs a negligible share in window 1) - many symbols
in the 120-strong universe are recent listings that simply did not have 14 days of
daily candles yet in January-February 2026, so the tradeable population is concentrated
in the back third of the window (session range 2026-04-27..2026-05-29 for S2-VAR
specifically). That is a data-availability artifact of reusing a universe frozen for
its LATER liquidity, not a defect in the setup or the test.

Replication within this window (splitting the same active sub-range):

| split | n | lift | 95% CI |
|---|---|---|---|
| BUY | 88 | +0.12R | [−0.21,+0.46] |
| SELL | 113 | +0.26R | [−0.22,+0.75] |
| early | 99 | +0.01R | [−0.16,+0.17] |
| late | 102 | +0.38R | [−0.05,+0.80] |

All four positive, none flipping sign - the same pattern finding 19 and finding 21
both showed, now on a genuinely independent sample of history rather than a stop-mode
variant of the same one.

### Pooled across both windows: clears the bar, and the interval tightens

Concatenating both windows' real trades and twins - fair, since each real trade's own
twins already come from its own window's independently-fetched cache, so there is no
cross-window contamination in how a twin's entry bar was chosen:

| | n | mean R | control mean | lift | 95% CI | verdict |
|---|---|---|---|---|---|---|
| pooled, both windows | 1137 | −0.23R | −0.39R | **+0.1611R** | **[+0.0440,+0.2781]** | **BEATS CONTROL** |

The combined interval is tighter than either window's alone (as expected with 1.7x the
sample) and stays clearly on the positive side. This is now the strongest evidence
behind any result in this project: not a single lucky corpus, but two non-overlapping,
independently-fetched periods pointing the same direction, at a magnitude that held
rather than decayed.

### The other three setups, as a sanity check

S1-POC (lift +0.03R, CI [−0.29,+0.36]) and S3-BRK (**WORSE than control**, lift
−0.59R, CI [−0.82,−0.36]) reproduce their established dispositions exactly on this new
window - S1-POC still unrescued, S3-BRK's default entry mode still actively harmful.
Neither setup's stop-mode or target-mode logic reads anything from the second-window
fetch differently than the first, so this is the expected regression check, not a new
finding - and it passing is what makes the S2-VAR result trustworthy rather than a
fluke of how the second corpus happened to be built.

### Action taken

This is Phase 2's second and harder leg, now complete, and S2-VAR is the first setup
in the project to have survived both required tests: a cost-model stress sweep
(finding 22) and an independent historical window (this finding), pooling to a result
that clears the pre-committed bar. `S2_ENABLED` and `TARGET_MODE`'s defaults are
strong candidates to flip on this evidence - held back one more step deliberately: the
win rate at this result (~28.7% pooled) is still below the ~34.3% breakeven this
system's 2:1 reward:risk needs, so "beats control" is not yet "is profitable", and
that distinction should be stated plainly before any default changes, not discovered
by whoever reads the config next.

262 tests pass (no code changed this pass).

---

## 24. `poor_high`/`poor_low` were unreachable, not rare - the same bug shape as findings 5/7

Prompted by a user-proposed solution: "the poor extreme idea was too rare to test
(finding 18, n=5) - itself a signal the threshold might be too strict, not that the
phenomenon is rare." Measured rather than assumed, per this project's own discipline.

### The measurement

Across all 14,185 rows in `calibration/profiles.csv` (every frozen profile computed
during the Phase 1 corpus): `excess_high` fires on 64.0% of sessions, `excess_low` on
58.5% - both common, as the concept predicts. `poor_high` fires on 0.3%, `poor_low`
on 0.5% - two orders of magnitude rarer, which is not what "the opposite of a common
thing" should look like on its own.

Rebuilt every session's profile directly (13,901 of them, same ATR/bin-size/profile/
levels computation `scanner.frozen_bundle` uses) and recorded the single extreme
bin's volume as a fraction of the POC bin's volume - the quantity `poor_ceiling`
actually tests:

| percentile | top (high) extreme | bottom (low) extreme |
|---|---|---|
| p50 | 0.0165 | 0.0146 |
| p90 | 0.0823 | 0.0891 |
| p95 | 0.1233 | 0.1340 |
| p99 | 0.2427 | 0.2848 |

The old ceiling - `0.40`, hardcoded directly inside `profile/shape.py`, not even an
env-tunable config value - sat **above the 99th percentile of the real distribution**.
It was not measuring a rare market phenomenon; it was asking for something the data
essentially never contains. Exactly the same shape of bug as finding 5
(`S2_MIN_EXCURSION_VA_FRACTION`) and finding 7-adjacent
(`S1_LVN_MIN_POC_DISTANCE_VA_FRACTION`) - a threshold that looks like a reasonable
percentage until it is checked against what the quantity it is applied to can
actually produce.

### Choosing the replacement

Swept candidate ceilings against the same 13,901-session population:

| ceiling | poor_high | poor_low |
|---|---|---|
| 0.40 (old) | 0.27% | 0.47% |
| 0.20 | 1.65% | 2.32% |
| 0.15 | 3.26% | 4.01% |
| 0.10 | 6.30% | 7.42% |
| 0.08 | 8.37% | 9.88% |
| 0.05 | 13.38% | 15.51% |

Chose **0.15** - not the most permissive option, but the same value already used for
`EXCESS_MAX_BIN_VOLUME_PCT`, deliberately. This makes the two concepts read as one
measurement rather than two independent magic numbers: excess asks whether the last
`EXCESS_MIN_BINS` bins are ALL at or below this fraction of the POC; poor asks
whether the single edge bin specifically EXCEEDS that same fraction while the run as
a whole did not. One bar, two readings. It yields a real testable population
(3.3%/4.0% of all sessions) without diluting "poor" into "anything not extremely
thin" the way 0.05 or 0.08 would.

### Action taken

Added `config.POOR_EXTREME_MAX_BIN_VOLUME_PCT` (default 0.15), wired `profile/
shape.py`'s `_excess()` to read it instead of the literal `0.40`. `tests/test_shape.py`
added (4 tests) - critically, one asserts the SAME hand-built profile classifies
differently under the old 0.40 value (patched in) versus the new default, which is
the test that would actually catch a regression to the hardcoded constant; asserting
only the new default's behaviour would still pass if `0.40` were reintroduced as a
literal, since nothing would contradict it.

This does not by itself resolve finding 18 - `S2_REQUIRE_POOR_EXTREME` is still an
opinion gate, still default OFF, and has not been re-run under the corrected
threshold. What it does is make that gate testable for the first time: n=5 was never
going to produce a verdict in either direction, and now it can. Re-running S2-VAR
with `S2_REQUIRE_POOR_EXTREME=True` under the corrected threshold is the natural next
step, not yet done.

266 tests pass (4 new).

---

## 25. `S1_MIN_CONFIRMATIONS=2` — a mild point-estimate move, not a replicating fix

User-proposed: require 2 of the 4 confirmation signals instead of 1, on the theory
that a stronger confirmation requirement should filter out weaker S1-POC entries.
Full Phase 1 replay at the default `TARGET_MODE=fixed_r`/`S1_STOP_MODE=buffer`,
matched-random control.

### The pooled number moved the right direction, but not enough

| | n | mean R | control mean | lift | 95% CI | verdict |
|---|---|---|---|---|---|---|
| 1-of-4 (default, finding 15) | 308 | −0.26R | −0.31R | +0.0517R | [−0.17,+0.28] | no separation |
| 2-of-4 (this finding) | 285 | −0.25R | −0.35R | +0.0992R | [−0.14,+0.34] | no separation |

The lift nearly doubled and the sample shrank only modestly (2-of-4 is more
selective, as expected), which sounds like real progress. It is not, once split:

| split | n | lift | 95% CI |
|---|---|---|---|
| BUY | 136 | +0.2078R | [−0.13,+0.55] |
| SELL | 149 | −0.0050R | [−0.35,+0.34] |
| early half | 137 | +0.1473R | [−0.21,+0.50] |
| late half | 148 | +0.0547R | [−0.27,+0.38] |

The pooled improvement is coming almost entirely from the BUY side; SELL is flat at
essentially zero. That specific pattern - BUY moving, SELL not - is exactly the
signature finding 17 already diagnosed and confirmed directly against BTC/ETH price
action: a market-drift confound concentrated in this corpus's window, not a property
of any setup's logic. A stronger confirmation requirement should, if it were doing
what it claims, help both directions roughly alike; instead it looks like the BUY
subset simply contains more of the corpus's already-known directional drift. Read
against that finding, this result is not independent evidence for the confirmation
count - it is likely the same confound showing up again in a new cut of the data.

### The cost this experiment revealed

S1-LVN reads the same `S1_MIN_CONFIRMATIONS` threshold. At 2-of-4 its already-thin
sample (n=14 at the default) shrank to n=7 - actively working against the separate,
already-queued goal of widening S1-LVN's sample enough to get its first real verdict.
Raising this threshold and expanding S1-LVN's universe are in direct tension as long
as both setups share one confirmation-count knob.

### Action taken

`S1_MIN_CONFIRMATIONS` stays at its default of 1. Not because 2 was disproven -
the interval still contains zero either way - but because the one thing that moved
is attributable to a confound this project has already named and confirmed, not to
the change under test. A real S1-POC fix should show up evenly across BUY and SELL,
the same bar finding 19's S2-VAR result was held to.

266 tests pass (no code changed this pass).

---

## 26. Partial-fill target sizing raced the cancel, and reconciliation could not have caught it

User-proposed, from an independent review of the TP/SL mechanism: a partial entry
fill is protected by cancelling the unfilled remainder, then sizing the stop and
target off `candidate.filled_qty`. That quantity comes from whichever poll first
detected the `PARTIAL` outcome - in `positions.py`'s `open()` path, the fast-fill
poll inside `wait_for_fill`; in `advance_pending()`'s deadline path, the poll at the
top of that cycle's loop. Both polls run **before** `_settle_fill` calls
`router.cancel()`. Nothing stops more of the order filling in that gap.

### Why this is a real gap, not a theoretical one

The stop needs no correction: it is `STOP_MARKET closePosition=true`, which closes
whatever is actually open regardless of any quantity bookkeeping. The target is the
problem - it is placed with an **explicit** `quantity=filled_qty`. A stale,
too-low `filled_qty` places a target that covers only part of the real position,
leaving the rest stop-protected but with no take-profit order.

That undersized target is not self-healing. `_replace_missing_targets` only acts
when `_has_live_target(symbol)` reads exactly `False` - and an undersized-but-present
target order reads as `True` (present), so reconciliation has no signal that
anything is wrong. The gap sits there until the position closes some other way.

### The fix

`_settle_fill` now re-queries `order_state()` immediately after the cancel call
lands, and uses that fresh `filled`/`avg_price` for `filled_qty`/`filled_price` if
it reports anything filled - overwriting the pre-cancel value the caller had set.
`execution/positions.py`, inside the `outcome == "PARTIAL"` branch of `_settle_fill`.

### Test

`tests/test_execution.py::ProtectionInvariantTests::
test_partial_fill_race_sizes_the_target_off_the_freshest_fill` - fills an entry to
0.8 of a 2.0 quantity, forces the deadline path, and makes an extra 0.5 land
*during* the `cancel_order` call (simulating the race), so the pre-fix code would
size the target at 0.8. Asserts both the resulting position and the target order's
quantity land at 1.3, the freshest true value.

This is a correctness fix, not a calibration result - nothing here was measured or
gated, because there is nothing to measure: a target that doesn't cover the real
position size is wrong regardless of how any setup performs. 267 tests pass (266 +
1 new).

---

## 27. `stop_inside_hvn` is nearly inert under the current stop formulas - measured before building anything on top of it

User-proposed: widen a stop past an HVN it lands inside, rather than the existing
reject-only `gates.checks.stop_inside_hvn`/`target_behind_hvn` (default OFF, and per
finding 18 never actually included in a Phase 3 sweep). Both setups already compute
and record `stop_inside_hvn`/`target_behind_hvn` on every candidate unconditionally
(`poc_rotation.py`, `va_reversion.py`), so - following the same discipline as finding
24 - the real population was measured from the existing pooled window 1 + window 2
data (findings 19/23) before writing a widen mechanism or spending a replay on the
reject gate.

| setup | n (resolved) | `stop_inside_hvn=True` | rate |
|---|---|---|---|
| S2-VAR (structural) | 1,137 | 0 | 0.0% |
| S1-POC | 347 | 4 | 1.2% |

Neither setup's stop lands inside an HVN in any meaningful volume. This is not
surprising in hindsight: S2-VAR's stop sits beyond the value-area edge, well clear of
the dense central region an HVN occupies; S1-POC's stop sits just beyond the POC with
a volatility buffer, and the buffer alone is apparently enough to clear the POC's own
HVN in all but 4 of 351 cases. Both a reject gate and a widen-past-HVN mechanism act
on a condition that essentially never fires under the stops these setups already
place - there is nothing here to gate or to widen, not because the idea is wrong, but
because the population doesn't exist without a different, larger change to how these
stops are computed.

`target_behind_hvn` tells a different, opposite story - it is *nearly universal*, not
rare:

| setup | n (resolved) | `target_behind_hvn=True` | rate | mean net R (True) | mean net R (False) |
|---|---|---|---|---|---|
| S2-VAR (structural) | 1,137 | 1,090 | 95.9% | −0.2320R | −0.2654R (n=47) |
| S1-POC | 347 | 116 | 33.4% | −0.3134R | −0.2532R |

For S2-VAR under `TARGET_MODE=structural`, 95.9% is close to a tautology rather than a
discriminator: a structural target aims at a naked or session POC, and a POC is by
definition the center of the densest HVN in its profile, so "an HVN sits between entry
and target" is true of nearly every structural trade by construction. Gating on it as
written would reject almost the entire validated S2-VAR structural population -
the one result in this project that has cleared the phase gate (finding 23) - for a
reason that doesn't distinguish good trades from bad ones (True vs. False net R is
statistically indistinguishable at n=47 on the False side). S1-POC's split is more
balanced (33.4%) and directionally consistent with the "behind HVN is worse" theory
(−0.31R vs −0.25R), but at n=116 vs. n=231 with no control-adjusted CI this is
suggestive at best, not a result.

### Action taken

Plan items 14 (widen stop past HVN) and 15 (test the existing reject gates) are not
being built or run as originally scoped. `stop_inside_hvn` has no population to act on
without first changing how S1-POC/S2-VAR's stops are computed - which is a materially
bigger change than the cheap addition either item was scoped as, and not undertaken
here without it being deliberately chosen. `target_behind_hvn` as currently defined is
not a usable filter for structural targets specifically because it is confounded with
the target selection logic being filtered - a different, direction-of-approach-aware
definition (e.g. "target sits beyond the HVN's far edge" rather than "an HVN's center
lies anywhere between entry and target") would be needed before this is worth another
look, and that redefinition has not been attempted.

No code changed this pass; the split was computed from existing trades.csv data, no
replay run. 267 tests pass (unchanged).

---

## 28. PLAN item 8 — the HTF regime overlay is a marginal, fragile result, not a rescue

Combination test of `shape_opposed` and `htf_value_opposed` together (population
restricted to sessions where NEITHER fires - shape balanced AND value migration not
against the trade), computed retrospectively from the pooled window 1 + window 2
structural-target corpus (findings 19/23/27) using the project's own cluster-robust
`research.metrics.summarise_r`, not a hand-rolled SE. No new replay - both flags are
reconstructible from `prior_shape`/`value_migration`/`direction`, already in every
stored row.

One methodology note up front: finding 18's `S2_REQUIRE_BALANCED_SHAPE` test predates
`TARGET_MODE=structural` entirely - it ran under the old `fixed_r` baseline. So
`shape_opposed`'s row below is not a duplicate of finding 18; it is that same filter,
genuinely re-measured for the first time under the config that actually matters now.

| setup / filter | n | g (clusters) | lift | 95% CI | verdict |
|---|---|---|---|---|---|
| S1-POC baseline | 347 | 125 | +0.0764 | [-0.1258,+0.2786] | no separation |
| S1-POC shape_opposed excluded | 347 | 125 | +0.0764 | [-0.1258,+0.2786] | identical - true no-op, `S1_REQUIRED_SHAPES=["D"]` already forces this |
| S1-POC combined | 344 | 125 | +0.0728 | [-0.1305,+0.2761] | no separation |
| S2-VAR baseline | 1,137 | 148 | +0.1611 | [+0.0440,+0.2781] | BEATS CONTROL (finding 23) |
| S2-VAR shape_opposed excluded | 355 | 117 | +0.1864 | [-0.0020,+0.3747] | no separation - misses by 0.002 |
| S2-VAR htf_value_opposed excluded | 1,103 | 148 | +0.1773 | [+0.0575,+0.2972] | BEATS CONTROL, ~unchanged from baseline |
| S2-VAR **combined** | 346 | 117 | +0.2031 | **[+0.0093,+0.3969]** | **BEATS CONTROL** |

S1-POC: exactly as expected, a genuine no-op (`shape_opposed` cannot fire when the
setup already requires `D`-shape by construction) and `htf_value_opposed` removes 3 of
347 trades - no combination effect exists to find.

S2-VAR is where the plan's "temper expectations" framing earns its keep. The combined
filter's lift (+0.2031) does clear the same BEATS-CONTROL bar as the baseline, and on
its face reads like the strongest S2-VAR number yet. But look at what is actually
doing the work: `htf_value_opposed` alone removes only 34 of 1,137 trades (3%) and
barely moves the baseline lift - almost the entire restriction, and almost the entire
population drop (1,137 -> ~350), comes from `shape_opposed`. And `shape_opposed`
*alone* does **not** clear zero - its CI's low edge is -0.0020, a hair's width short.
Adding the nearly-inert HTF filter on top nudges that edge to +0.0093 and flips the
verdict. That is not a combination effect; it is a single dominant filter sitting
right at the statistical boundary, with a second filter that removes almost nothing
tipping it over by less than a hundredth of an R. A result that fragile - g=117
clusters, CI width nearly 0.4R, verdict decided by whether one weak filter is stacked
on top of one strong one - is not something this project's own standard (replicate
across BUY/SELL and early/late splits, per the phase-gate rule) would pass without
that replication check, which has not been run here.

### The replication check the fragility above called for

Run rather than left as a suggestion - BUY/SELL and early/late splits of the same
n=346 combined population:

| split | n | g | lift | 95% CI | verdict |
|---|---|---|---|---|---|
| BUY | 172 | 73 | +0.2033 | [-0.0893,+0.4958] | no separation |
| SELL | 174 | 87 | +0.2001 | [-0.0803,+0.4804] | no separation |
| early half | 163 | 56 | +0.3328 | [+0.0505,+0.6151] | BEATS CONTROL |
| late half | 183 | 61 | +0.0876 | [-0.1781,+0.3533] | no separation |

Fails the phase-gate rule outright. BUY and SELL agree closely in point estimate
(+0.20 both, so no drift-confound signature here, unlike finding 17's pattern
elsewhere) but NEITHER half clears zero alone - the pooled result is entirely a
function of combining two correlated, underpowered halves. Worse, the time split
does not hold: early clears the bar, late does not, by a wide margin (+0.33 vs
+0.09). This is exactly the profile the phase-gate rule exists to catch - a pooled
CI that clears zero by a small margin dissolving on any real split.

### Action taken

Not shipped, not added to any default, and not queued for further pursuit as
currently defined. `GATE_SHAPE_OPPOSED_ENABLED` and `GATE_HTF_VALUE_OPPOSED_ENABLED`
both stay False. The combined filter's pooled BEATS-CONTROL verdict does not survive
the same replication test every other result in this file is held to - it is noise
that happened to land on the favourable side of zero, not a rescue.

No code changed this pass; both flags already existed, only measured together for the
first time. 267 tests pass (unchanged).

---

## 29. PLAN item 7 — confirm-interval thresholds now rescale, instead of silently changing meaning

Prerequisite work, not a result: five candle-count thresholds (`ACCEPT_MIN_CANDLES`,
`ACCEPT_MIN_CANDLES_OUTSIDE`, `REJECT_MAX_CANDLES_OUTSIDE`,
`ACCEPT_MIN_BASELINE_CANDLES`, `CONFIRM_CONSECUTIVE_CANDLES`) all count
`CONFIRM_INTERVAL`-timeframe candles, and were hardcoded as counts (13, 5, 3, 6, 2)
rather than durations. Running the confirm-interval ablation this item exists for
without fixing this first would have silently changed what every threshold MEANS
alongside its granularity: "13 candles outside value" is 195 minutes at the 15m
default and 13 minutes at 1m - a materially weaker acceptance requirement, not the
same claim sampled more often.

Fixed by introducing `config.candles_for_minutes(minutes)`, which converts a
canonical duration to however many `CONFIRM_INTERVAL` candles that takes
(`round(minutes / CONFIRM_INTERVAL_MINUTES)`, floored at 1). Each threshold's default
is now expressed as its duration (45/195/75/90/30 minutes respectively) run through
this function, rather than a bare literal - at the 15m default this reproduces the
exact prior values (3/13/5/6/2), regression-locked by test. An explicit env override
of any of the five candle-count variables still wins, unchanged from before -
`env_int` reads its own env var first regardless of how the default was computed.

`research.replay` gets a new `--confirm-interval` flag that overrides
`CONFIRM_INTERVAL` and rescales all five thresholds together for that run - smoke-
tested at `5m`: rescaled to 9/39/15/18/6, exactly 45/195/75/90/30 minutes divided by
5. No new data fetch needed - confirm candles are resampled from the always-cached 1m
data at replay/live time (`klines.resample`), so any interval is available from the
existing corpus.

### Action taken

Infrastructure only - `CONFIRM_INTERVAL` itself is untouched (stays "15m"), so no
live or default behaviour changed. 280 tests pass (275 + 5 new, covering the rescale
math and the zero-candle floor). The ablation experiment itself - full primary-corpus
replay at `--confirm-interval 5m` vs. the 15m baseline - is running now
(`calibration/phase3_confirm_5m`); results follow in the next finding once it lands.

---

## 30. PLAN item 11 — an independent regime label validates the shape classifier's OUTPUT, but exposes which of its two mechanisms actually carries the signal

`profile/shape.py`'s D/P/b/B/trend labels have always been calibrated against
themselves - `SHAPE_TREND_MAX_VA_RANGE_RATIO` and `SHAPE_TREND_MIN_POC_MIGRATION_ATR`
were reasoned, and every earlier calibration pass measured their own OUTPUT
distribution, never an outside check. Built one: `research/regime.py`'s
`efficiency_ratio()` (Kaufman ER - net directional move over a window, divided by the
sum of every candle's absolute move in it) uses only closes, none of shape.py's own
inputs (bins, POC, value area). Computed for every session in the existing 14,185-row
`profiles.csv` corpus directly from cached 1m candles - no new replay, no new fetch,
~20 minutes of local computation. This is explicitly a research-only cross-check;
principle P1 (config.py) excludes it from ever gating a live trade, and nothing in
`research/regime.py` is imported by the live pipeline.

### The classifier's final output IS validated

| shape label | n | mean ER | median ER |
|---|---|---|---|
| D (balanced) | 4,244 | 0.0218 | 0.0175 |
| P | 1,463 | 0.0266 | 0.0228 |
| b | 1,907 | 0.0280 | 0.0225 |
| B (bimodal) | 3,662 | 0.0396 | 0.0332 |
| trend | 2,909 | 0.0415 | 0.0378 |

Clean, monotonic ordering, exactly auction theory's own prediction, from a signal
that shares nothing with the classifier that produced these labels. AUC of ER
separating `trend` from `D` directly: **0.7180, 95% CI [0.7056,0.7303]** - strongly
informative, comparable in strength to acceptance's own duration discriminator
(finding 14: 0.606-0.619 within distance strata). The absolute ER values are small
across the board (a known artifact of measuring 1-minute-candle noise over a
1,440-candle, 24h window - high-frequency chop dominates the denominator regardless
of session character), so the right read is the *ordering*, not the magnitude.

### But the two mechanisms that produce it are not equally responsible

Broken down by which of shape.py's two trend conditions the label depends on:

| independent variable | AUC vs. ER (high-ER=positive) | 95% CI | verdict |
|---|---|---|---|
| `va_range_ratio` (elongation) | 0.5577 | [0.5443,0.5710] | informative, but weak |
| `intra_poc_migration_atr` (migration) | 0.7865 | [0.7760,0.7971] | strongly informative |

Two things stand out. First, `va_range_ratio` alone is a much weaker independent
predictor (0.558) than the classifier's final trend/D split (0.718) - most of that
gap is `intra_poc_migration_atr` doing the real work (0.787, actually *stronger* than
the combined output). Second, and more surprising: the sign runs opposite to the
naive reading of "low `va_range_ratio` = elongated = trend." Sessions in the
independently-measured high-ER (genuinely trend-like) group have HIGHER
`va_range_ratio` on average, not lower - `AUC(va_range_ratio, low-ER as positive)` is
0.4423, i.e. INVERTED relative to what "elongation predicts trend" would expect.

A plausible mechanism, offered as a hypothesis and not confirmed here: a smooth,
steadily-progressing trend spreads volume relatively evenly across the whole distance
it travels (nothing to consolidate at, since price keeps moving), which makes the
70%-volume value area span a WIDE fraction of the session's range - high
`va_range_ratio`, not low. What classical "elongation" (thin value relative to range)
more plausibly flags is a fast, wick-heavy IMPULSE - a different animal from a
grinding directional session, and one ER (built from closes, not wicks) may not
reward the same way. This is not settled by the data gathered here; it would need
`va_range_ratio` decomposed by move smoothness to confirm.

### Action taken

Nothing changed live - `SHAPE_TREND_MAX_VA_RANGE_RATIO` and
`SHAPE_TREND_MIN_POC_MIGRATION_ATR` are unmodified, and this finding is diagnostic,
not a rescaled threshold. What it does establish: the shape classifier's *output* has
now cleared an independent validation it had never been checked against, which is
real evidence its five labels track something real rather than an artifact of the
profile's own construction. It also identifies where to spend calibration effort
next if item 11 continues - `intra_poc_migration_atr`'s threshold is the stronger,
better-evidenced lever, `va_range_ratio`'s the one whose direction deserves a second
look before trusting it further. `research/regime.py` and `calibration/regime_er.csv`
(the full per-session ER table, one row per symbol/session with its shape label,
`va_range_ratio`, `poc_position`, `intra_poc_migration_atr` and `er`) are both
reusable for that follow-up without recomputing anything. 288 tests pass (280 + 8
new, in `tests/test_regime.py` - covering ER's monotonic trend-vs-chop separation,
the zero-movement floor, and boundedness).

---

## 31. PLAN item 4 — S2-VAR's rejection-quality measurement finds the strongest lift yet, and a replication result that doesn't cleanly resolve either way

The refetch that repopulated `excursion_candles` on the original window (queued since
finding 23) finished; merged with window 2, S2-VAR now has 1,137 resolved trades with
both `excursion_candles` (return speed) and `bin_delta_normalized` (return-candle
delta, at the value bound the trade re-enters through) populated - both already in
the schema, recorded unconditionally, no new replay needed for this step. Measured
each alone, then together, against the matched-random control, cluster-robust.

### Alone

| filter | n | g | lift | 95% CI | verdict |
|---|---|---|---|---|---|
| baseline (all) | 1,137 | 148 | +0.1611 | [+0.0440,+0.2781] | BEATS CONTROL |
| fast return (<=2 candles) | 506 | 134 | +0.0646 | [-0.0671,+0.1962] | no separation |
| slow return (>=4 candles) | 377 | 130 | +0.2555 | [+0.0779,+0.4330] | BEATS CONTROL |
| delta supportive | 464 | 138 | +0.1975 | [+0.0311,+0.3640] | BEATS CONTROL |
| delta opposing | 651 | 143 | +0.1318 | [+0.0029,+0.2608] | BEATS CONTROL |

Return speed runs opposite to the naive "fast rejection = clean rejection" theory a
quality filter might assume: SLOW excursions (nearer the `REJECT_MAX_CANDLES_OUTSIDE`
ceiling) carry the edge, fast ones (nearer the acceptance floor) carry none. An AUC
of `excursion_candles` against win/loss directly is null (0.537, CI straddles 0.5) -
duration doesn't rank individual outcomes, but it does track WHERE the edge over a
random entry concentrates, a different and more useful question. Delta alone barely
discriminates - both directions beat control by similar margins, consistent with
finding 18's earlier null on the single-bin delta gate.

### Combined - the strongest number this project has produced

| filter | n | g | mean R | lift | 95% CI | verdict |
|---|---|---|---|---|---|---|
| slow (>=4) AND delta supportive | 157 | 92 | **+0.0355** | **+0.4299** | **[+0.1632,+0.6967]** | **BEATS CONTROL** |

The first time S2-VAR's own raw mean R has gone positive, not merely "less negative
than control," anywhere in this project. g=92 independent session-clusters is real
power, not a handful of correlated trades.

### The replication check - mixed, not a clean pass or a clean fail

| split | n | g | lift | 95% CI | verdict |
|---|---|---|---|---|---|
| BUY | 59 | 44 | +0.7470 | [+0.3113,+1.1827] | BEATS CONTROL |
| SELL | 98 | 68 | +0.2391 | [-0.1128,+0.5910] | no separation |
| early half | 66 | 44 | +0.3386 | [-0.0759,+0.7530] | no separation |
| late half | 91 | 48 | +0.4962 | [+0.1487,+0.8438] | BEATS CONTROL |
| window 1 | 126 | 72 | +0.4353 | [+0.1502,+0.7204] | BEATS CONTROL |
| window 2 | 31 | 20 | +0.4083 | [-0.2833,+1.0999] | no separation |

This does not cleanly pass the phase-gate rule (BUY and SELL must both clear), but it
is a materially different picture from findings 17/25's confound cases. Window 1 and
window 2's point estimates are nearly identical (+0.4353 vs +0.4083) - it is window
2's small sample (n=31) that keeps its CI open, not a sign disagreement. Early/late
are the same story: both positive, same order of magnitude, early merely underpowered
(n=66 vs 91). BUY/SELL is the genuine concern - SELL's raw mean R is negative even
though it still beats its control.

**The drift check, run the same way finding 17 confirmed the confound elsewhere:**
does the matched-random CONTROL show the same BUY > SELL gap under this exact filter?

| population | BUY mean R | SELL mean R | gap |
|---|---|---|---|
| control, unfiltered | -0.2983 (n=2,745) | -0.4799 (n=3,086) | 0.1816 |
| control, same filter | -0.3775 (n=63) | -0.5249 (n=117) | 0.1474 |
| **real, same filter** | **+0.3526** (n=59) | **-0.1553** (n=98) | **0.5079** |

Market drift IS real here - the control shows the same direction of asymmetry, filtered
or not, confirming this corpus window's known BUY-favouring drift (finding 17) is
still present. But it is smaller than the real population's gap: the control's
asymmetry (0.147-0.182) accounts for roughly a third of the real gap (0.508), leaving
about two-thirds unexplained by drift alone - unlike findings 17 and 25, where drift
explained close to the whole effect. This is not the same failure mode, and dismissing
it on the same grounds would be too quick.

### Action taken

Not shipped - the phase-gate rule (BUY and SELL both clear) is not met, and a result
this good deserves exactly the scrutiny that rule exists to apply. But it is not
dismissed either: two of three replication axes look like real signal thinned by
sample size rather than absence of effect, and the drift confound that explained away
similar-looking BUY-skewed results elsewhere only explains part of this one. Flagged
as the single most promising open lead in the project - the natural next steps are a
third independent time window (to add power to the SELL side and window 2
specifically) or a SELL-focused investigation of what's different about that side's
"slow + supportive" trades. No config or gate changed. `excursion_candles` and
`bin_delta_normalized` were already in the schema; no code changed this pass, only
measurement. 288 tests pass (unchanged).

### Follow-up (SELL-focused, exploratory, not yet independent evidence)

Took the two candidate follow-ups from finding 31's "action taken" and started with the
cheaper one: what's different about the combined filter's SELL trades. Swept every
attribute already in the schema for AUC/win-rate separation between SELL wins and
losses inside the n=98 combined-filter SELL population (`poc_prominence`,
`va_range_ratio`, `acceptance_ratio`, `atr`, `value_width`, `risk_distance`,
`bars_to_fill`, `qv_rank`, plus the categorical fields `prior_shape`, `auction_state`,
`value_migration`, `target_kind`, `excess_at_extreme`). Every numeric attribute's AUC
sat inside noise (0.44-0.57, CIs straddling 0.5). One categorical split stood out:
`prior_shape == "b"` (POC in the bottom slice of the session range).

| subset | n | g | mean R | 95% CI |
|---|---|---|---|---|
| SELL, combined filter, shape=b | 18 | 18 | -0.5575 | [-1.0846,-0.0303] |
| SELL, combined filter, shape!=b | 80 | 58 | -0.0648 | [-0.4661,+0.3364] |

At baseline (no combined filter at all), shape=b is not SELL's worst bucket - it's
one of the *better* ones (-0.1691, vs -0.3149 to -0.4317 for B/D/trend). The combined
filter helps SELL in every other shape bucket (D flips to +0.16, B improves to -0.07,
trend improves to -0.28) but makes shape=b SELL worse, not better. That interaction -
not a main effect - is what concentrates the filter's SELL underperformance.

Excluding shape=b from the combined filter (24 of 157 trades, all directions):

| split | n | g | mean R | 95% CI |
|---|---|---|---|---|
| pooled | 133 | 83 | +0.1433 | [-0.1436,+0.4302] |
| BUY | 53 | 41 | +0.4575 | [+0.0234,+0.8916] |
| SELL | 80 | 58 | -0.0648 | [-0.4661,+0.3364] |
| early half | 66 | 46 | +0.1418 | [-0.2678,+0.5515] |
| late half | 67 | 38 | +0.1447 | [-0.2696,+0.5590] |
| window 1 | 111 | 69 | +0.1397 | [-0.1599,+0.4393] |
| window 2 | 22 | 14 | +0.1614 | [-0.7140,+1.0369] |

Early/late and window1/window2 now line up far more tightly than finding 31's
unrefined numbers did (+0.142/+0.145, +0.140/+0.161 - all four essentially the same
point estimate). BUY's CI now fully excludes zero. But **SELL still does not clear**
- its mean is less negative (-0.065 vs -0.155) but the CI still straddles zero, so
this is a real improvement in the picture, not a resolution.

**Why this is not a new finding yet, and nothing is shipped or reprioritized on it:**
`prior_shape` was one of roughly fifteen attributes swept for a split that separates
SELL win from loss inside the same n=98 finding-31 population - the classic setup for
a false positive from multiple comparisons, and this is the same data finding 31 was
already measured on, not independent evidence. The right test is the one already
queued: a genuinely new time window (or a SELL-focused symbol set) that was never
looked at while searching for this split. Recorded here so the specific hypothesis
("exclude prior_shape=='b' from the slow+delta-supportive S2-VAR SELL filter") is
precise and ready to check the next time new data is pulled, rather than re-derived
from scratch. No config or gate changed; no new tests (measurement only, reusing
existing schema fields).

---

## 32. An audit of every candidate "add more confirmation signals" idea - most are already here

Prompted by a direct question: should VWAP, order-flow/delta, market structure, HTF
profile levels, momentum/RSI, open interest/funding, and candlestick rejection be
added to sharpen entries. Before evaluating any of them as new work, audited what the
codebase already does under each heading - several turned out to already exist,
under different names, at different stages of validation.

| requested method | status found |
|---|---|
| candlestick rejection | **already core**: `confirm.wick_rejection` + `confirm.engulfing`, part of every S1-POC/S1-LVN candidate's N-of-M confirmation vote since day one |
| order flow / CVD / delta | **already core AND already an opinion gate**: `confirm.delta_divergence` (same N-of-M vote, S1-POC/S1-LVN) plus `gates.checks.delta_opposed`/`delta_opposed_multibin` (S1-POC/S2-VAR, default OFF, finding 18 found no rescue) |
| market structure (BOS-equivalent) | **already the core mechanism of S3-BRK**: "continuation is a new extreme" (a literal break-of-structure test) is what its acceptance+continuation logic already requires, and it is the one setup with a validated, shipped redesign (finding 16, finding 19) |
| HTF profile levels | **half-built**: `ctx.migration` (session-over-session POC/value shift) is measured and tested (weak/fragile, findings 18, 28). A genuine higher-timeframe *composite* profile was architected for - `SetupContext.weekly_levels` exists as a field - but never populated. Dead code, not a shipped feature. |
| VWAP | **computed, never used**: `profile/levels.py` already builds session VWAP + 1σ/2σ bands for every profile (used internally only as the POC tie-breaker) and `profile_row()` already surfaces `prior_vwap` - but it is never persisted to `trades.csv` and no gate reads it. |
| momentum / RSI | **absent, and deliberately so** - this is not an oversight. Config's own docstring states principle P1: "volume distribution is the sole source of trading premises... no indicator periods... beyond the ATR used for normalising distances." RSI is exactly the category P1 exists to exclude. |
| open interest / funding rate | **absent, no plumbing at all**. `exchange/rest.py` has no OI/funding endpoint wrapper; `research/dataset.py` has no corresponding cache format. The larger issue is data availability, not code: Binance's OI history endpoint has a much shorter retention window than this project's existing corpus, which would need checking before any fetch is worth building. |

### The one thing measurable for free: are the two already-core confirmation signals earning their place

`confirm.collect()`'s docstring says the four signals are independently toggleable
"so Phase 3 can ablate each one and find which actually earn their place" - that
ablation had never actually been run. `confirmations` (which signals fired) is
already stored per candidate, so this needed no new replay: split S1-POC's pooled
window1+window2 population by whether each signal was present, cluster-robust.
(S1-LVN carries the same fields but n=15 is too thin to read; S2-VAR/S3-BRK don't
use this confirmation vocabulary at all - they have their own setup-specific
evidence, per findings 16 and 31.)

| signal | present: n / mean R | absent: n / mean R | AUC (present vs absent) |
|---|---|---|---|
| wick_rejection | n=14  mean=−0.8596 | n=333 mean=−0.2487 | n/a (<20/side) |
| engulfing | n=73  mean=−0.4609 | n=274 mean=−0.2233 | 0.506 CI=[0.431,0.581] |
| consecutive_closes | n=113 mean=−0.1883 | n=234 mean=−0.3144 | 0.493 CI=[0.428,0.558] |
| delta_divergence | n=299 mean=−0.2772 | n=48  mean=−0.2492 | 0.520 CI=[0.433,0.607] |

Every AUC that could be computed sits on 0.5 - none of the four discriminate S1-POC
winners from losers, and where a difference in raw mean R exists (wick_rejection,
engulfing) it runs the WRONG way: present is worse than absent. This does not
indict candlestick/order-flow confirmation in general - S1-POC is already the
weakest setup at baseline (findings 20/21, PLAN item 9), so a confirmation signal
that cannot rescue a structurally weak premise is a different claim from a signal
that carries no information at all. It does mean: two of the exact methods asked
about are not just "not yet tried" here, they are already live and, on the
evidence available today, not adding measurable value to the one setup that uses
them.

### Action taken

Diagnostic only - no code or config changed, no new tests. Full recommendation and
next-step ranking given directly to the user rather than recorded as a plan item
here, since several of these (P1's stance on RSI, OI/funding's data-availability
ceiling) are calls for the user to make, not calibration results to report.

---

## 33. Funding rate - the naive "fade the crowd" thesis is backwards, and it replicates cleanly

Follow-up to finding 32: open interest was shelved there (Binance's free API caps
its history at ~30 days, confirmed against the official docs and empirically
against the live endpoint - not enough to backtest against this project's 6-12
month corpus). Funding rate has no such cap - verified empirically against the
live endpoint back to a symbol's perpetual-listing date - so it was fetched for
real: `exchange.rest.RestClient.funding_rate_history` (new), `research.dataset.
fetch_funding` (new, `end_as_of`-aware from day one, learned from finding 29's
`fetch_daily` bug), 120 symbols x their full available history via a new
`--with-funding` flag. Research-only so far - nothing wired into the live scanner
or any setup's candidate attributes yet.

### The hypothesis tested

The standard "funding extremes mark crowded positioning" idea: a SELL fading an
extreme should do better when funding is strongly POSITIVE (longs paying shorts -
crowded longs, ripe to unwind); a BUY should do better when funding is strongly
NEGATIVE (crowded shorts). Tagged every S1-POC/S2-VAR candidate (pooled window 1 +
window 2, n=1,484, all resolved) with the funding print in force at `decision_ts`,
direction-signed so "aligned" always means "funding agrees with the contrarian-crowd
thesis for this trade's direction."

### The result runs the opposite way, and it is not close

| split | aligned (fade the crowd) | opposed (go with the crowd) |
|---|---|---|
| pooled | n=727 g=143 mean=**-0.3555** CI=[-0.4583,-0.2527] | n=757 g=147 mean=**-0.1344** CI=[-0.2507,-0.0180] |
| S1-POC BUY | mean=-0.3405 | mean=-0.1398 |
| S1-POC SELL | mean=-0.3902 | mean=-0.2458 |
| S2-VAR BUY | mean=-0.2620 | mean=-0.0196 |
| S2-VAR SELL | mean=-0.4125 | mean=-0.2266 |
| early half | mean=-0.2894 | mean=-0.1565 |
| late half | mean=-0.4175 | mean=-0.1108 |
| window 1 | mean=-0.3534 | mean=-0.1090 |
| window 2 | mean=-0.3648 | mean=-0.2424 |

"Opposed" (trading WITH whatever the crowd is already positioned toward, not
against it) beats "aligned" in **every single split** - both setups, both
directions, both time halves, both independent windows. That is a cleaner sweep
than any other split-replication in this project, finding 31 included. Neither
group is profitable in absolute terms (both setups are net-negative at baseline,
per findings 20/21), but the GAP between them - roughly 0.13-0.31R depending on
the split - never once inverts.

### Why this is not yet a finding that changes anything, despite the sweep

Two things separate "replicates cleanly" from "validated":

1. **No matched-random control yet.** Every other lift claimed in this project
  (findings 19, 22-23, 28, 31) was checked against a control built the same way -
  same symbol/session, randomised entries - before being called real. This is
  an internal comparison (aligned vs. opposed), not a beats-control claim. The
  next real step is building that control, the way `research/control.py` already
  does for everything else.
2. **A live wiring path does not exist yet.** VWAP and delta are derived from data
  the scanner already holds; funding is not - it needs either a maintained live
  cache or a REST call at signal time, a materially bigger lift than either of
  the two follow-ups from finding 32. Not started, and shouldn't be until the
  control comparison says this is worth it.

The interpretation, for what it's worth: this reads less like "funding rate marks
reversal risk" and more like funding rate proxying the SAME persistent directional
drift findings 17/25/31 have already characterised in this corpus - except unlike
those cases, this one holds up within-direction (BUY alone, SELL alone), so it is
not simply the known BUY/SELL asymmetry wearing a new name. What it actually is
remains open.

### Action taken

Research-only. `data_cache/funding/` now holds full history for all 120 frozen-
universe symbols. `fetch_funding`/`funding_rate_history` are new, tested (10 new
tests: 7 for `fetch_funding`'s pagination/`end_as_of` behaviour, 3 for `build`'s
opt-in `--with-funding` threading), 309 tests pass overall. No config or gate
changed, no candidate attribute added yet - that and the control comparison are
the natural next steps, in that order, before any live-wiring decision.

---

## 34. Funding rate's control comparison - not a direct predictor, but it locates where S2-VAR's already-validated edge concentrates

Finding 33's open question, resolved: is the "aligned vs opposed" gap something a
volume-profile-timed entry contributes, or does a random entry show it too?
Reused the matched-random controls already built for findings 19/23 (`phase1_
structural_target/controls.csv`, `phase2_window2_structural/controls.csv` - same
real trades, so the same twins apply unchanged) rather than rebuilding them, and
tagged each control twin with the funding print at ITS OWN `decision_ts` (twins
land on a random bar in the same session, not the real trade's bar, so this is
never copied through - computed fresh for every twin exactly like for every real
trade).

### The drift check: the random-entry control ALSO shows the aligned/opposed gap

| setup | control aligned | control opposed | gap |
|---|---|---|---|
| S1-POC | n=821 mean=-0.4694 | n=1006 mean=-0.2519 | 0.2175 |
| S2-VAR | n=3103 mean=-0.4434 | n=2728 mean=-0.3387 | 0.1047 |

Answer: **no**, this is not a setup-specific timing contribution - a same-symbol,
same-session, same-direction RANDOM entry shows the identical pattern, and for
S1-POC the control's own gap (0.2175) is even LARGER than the real trades' gap
(0.1753). Finding 33's "opposed beats aligned" is a property of the funding/
session/direction combination itself, not of where in the session a volume-
profile entry happens to fire. So funding rate is not, on this evidence, a viable
direct win/loss predictor the way `excursion_candles`+`bin_delta_normalized` are
for finding 31.

### But it reframes something real: WHERE S2-VAR's validated edge lives

The standard question - does either bucket beat ITS OWN control - tells a
different and more useful story for S2-VAR specifically (S1-POC clears nothing in
either bucket, consistent with every other S1-POC result in this project):

| S2-VAR bucket | n (real/ctrl) | lift | 95% CI | verdict |
|---|---|---|---|---|
| baseline (finding 23, unfiltered) | 1,137 / 5,831 | +0.1611 | [+0.0440,+0.2781] | BEATS CONTROL |
| funding ALIGNED (naive thesis direction) | 572 / 3,103 | +0.0919 | [-0.0502,+0.2340] | no separation |
| funding OPPOSED (against naive thesis) | 565 / 2,728 | **+0.2249** | **[+0.0677,+0.3822]** | **BEATS CONTROL** |

S2-VAR's already-validated edge over control is not evenly spread across the
population it was measured on - it concentrates specifically in sessions where
funding does NOT support the naive "fade the crowd" story, and is statistically
indistinguishable from the control in sessions where it does. The lift in the
"opposed" bucket (+0.2249) is not only real, it is LARGER than the unfiltered
baseline lift finding 23 already shipped-relevant evidence on.

### Replication check on the opposed-bucket lift - same mixed pattern as finding 31, tighter point estimates

| split | n | g | lift | 95% CI | verdict |
|---|---|---|---|---|---|
| pooled | 565 | 141 | +0.2249 | [+0.0677,+0.3822] | BEATS CONTROL |
| BUY | 308 | 113 | +0.1931 | [-0.0524,+0.4386] | no separation |
| SELL | 257 | 108 | +0.2576 | [+0.0229,+0.4924] | BEATS CONTROL |
| early half | 282 | 68 | +0.2088 | [-0.0204,+0.4380] | no separation |
| late half | 283 | 74 | +0.2411 | [+0.0251,+0.4572] | BEATS CONTROL |
| window 1 | 461 | 112 | +0.2295 | [+0.0490,+0.4099] | BEATS CONTROL |
| window 2 | 104 | 29 | +0.2023 | [-0.1082,+0.5128] | no separation |

Does not cleanly pass (BUY, early, window 2 don't individually clear), but unlike
finding 31's split - where BUY and SELL disagreed by a factor of 3 - every point
estimate here sits in a tight +0.19 to +0.26 band regardless of which half of the
data is doing the measuring. The three that don't clear are all the smaller-n
side of their split (BUY < SELL in count, early's window happens to have thinner
funding coverage, window 2 is a fifth the size of window 1 - the same power
problem finding 23 already flagged for window 2 generally). This reads like a
real effect thinned by sample size, not a sign disagreement.

### Action taken

Not shipped - three of six splits don't individually clear, and per this
project's own rule that is not optional to wave through regardless of how tight
the point estimates look. But this is now a second precise, ready-to-check
hypothesis queued behind the same third independent window already flagged for
finding 31's `prior_shape=='b'` lead: **does S2-VAR's edge, filtered to sessions
where funding does not already support the fade, replicate on data that was
never used to find the split?** No config or gate changed. No new tests (reused
the existing control infrastructure and metrics module exactly as built;
measurement only).

---

## 35. PLAN item 21 - the real weekly composite profile, finished rather than invented

`SetupContext.weekly_levels` has existed since `setups/context.py` was written,
always `None` - nothing ever populated it. `sessions.week_start_ms`/
`current_week`/`previous_week` existed too, unused anywhere, with a docstring
already explaining the Monday-vs-Sunday choice ("Binance's own weekly kline uses
Monday"). `gates.checks.htf_value_opposed`'s own docstring flags the gap
directly: it reads `ctx.migration` (prior-session-vs-current, already measured
weak, findings 18/28) "despite this flag's gate function being named
`htf_value_opposed`" - the real weekly composite this was named for was never
built. This finding closes that gap; nothing here is a new architectural
decision, it is finishing one already made.

### What was built

`Scanner.frozen_weekly_bundle` (`scanner.py`), new: the previous COMPLETE
calendar week's candles (`sessions.previous_week`), aggregated into one
composite `Profile` on the SAME bin lattice as the daily profile (the caller's
own `bin_size`, so weekly POC/VAH/VAL sit on prices directly comparable to
`prior_levels`'), then reduced to `Levels` the identical way `frozen_bundle`
already does for a session. Frozen per symbol for the whole week - recomputed
only when the calendar week rolls over, the same caching discipline as the
daily bundle, verified directly (`tests/test_scanner.py`): three calls inside
one week fetch once; the week rolling over triggers exactly one recompute; a
week with no candles (a fresh listing) returns `None`, never a scan-blocking
rejection - `weekly_levels` is optional context, not a requirement.

Live-wired, not research-only: `Scanner.build_context` (the same function
research/replay.py calls) now sets `ctx.weekly_levels` on every context it
builds, live and replay both.

Its own "measure before gate" companion, matching how `bin_delta_normalized`
and `vwap_zscore_at_level` were introduced: `setups.base.weekly_poc_distance_atr`
(signed distance from the traded level to last week's POC, in ATR), recorded
unconditionally on S1-POC/S2-VAR candidates, persisted as
`weekly_poc_distance_atr` in `research/replay.py`'s trade schema. Nothing reads
it yet.

### Verified against the real corpus, not just synthetic fixtures

Ran `Scanner.build_context` directly against `data_cache` (BTCUSDT, 2026-08-15):
`ctx.weekly_levels` came back a fully-formed, real composite (POC 64295.98,
VAH 65037.36, VAL 63759.11) - genuinely different from that day's own
`prior_levels` POC (62864.34), confirming this is a real independent
aggregation and not an accidental duplicate of the daily profile.

### Action taken

Built and tested (18 new tests: 6 for the calendar-week helpers themselves,
which had never been unit-tested despite predating this work; 6 for
`frozen_weekly_bundle`'s caching/window/None-handling; 5 for
`weekly_poc_distance_atr`; 1 pipeline wiring check with a real weekly bundle,
proving the field carries a genuine value end to end and not just None==None).
327 tests pass overall. Not yet measured - needs a fresh replay to populate
`weekly_poc_distance_atr` on real trades, held until one of the four background
jobs already running frees a slot rather than adding a fifth. No gate reads it;
no config changed.

---

## 36. Finding 31's combined filter does not replicate on window 3 - the project's
    "most promising open lead" is closed

Finding 31 flagged S2-VAR's "slow excursion (>=4 candles) AND delta-supportive"
combined filter as the strongest lift the project had produced (+0.4299,
[+0.1632,+0.6967], g=92), but withheld shipping because the phase-gate rule
(BUY and SELL both clear) was not met - SELL's CI straddled zero even though its
point estimate was in the same range as BUY's. The follow-up exploratory pass
found that excluding `prior_shape=='b'` tightened the early/late and
window1/window2 splits substantially, but was explicit that this was the same
data the split was searched on, not independent evidence, and named the exact
test that would resolve it: "a genuinely new time window ... that was never
looked at while searching for this split."

Window 3 (`data_cache_window3`, replayed after the `end_as_of` daily-history fix,
120/120 symbols, 4,981 real trades) is exactly that test. Same methodology:
`research/control.py` build (24,905 twins, seed unchanged), cluster-robust lift
against the matched-random control, same filter definition (`excursion_candles`
>= 4, `bin_delta_normalized` sign agreeing with trade direction), checked both
with and without the `prior_shape=='b'` exclusion.

| filter | split | n | g | mean R | lift | 95% CI | verdict |
|---|---|---|---|---|---|---|---|
| combined | pooled | 95 | 54 | -0.3690 | +0.0700 | [-0.2534,+0.3935] | no separation |
| combined | BUY | 42 | 29 | -0.6784 | -0.0775 | [-0.4758,+0.3207] | no separation |
| combined | SELL | 53 | 35 | -0.1238 | +0.1930 | [-0.3311,+0.7170] | no separation |
| combined, shape!=b | pooled | 74 | 48 | -0.4324 | +0.0690 | [-0.2836,+0.4217] | no separation |
| combined, shape!=b | BUY | 36 | 28 | -0.6200 | -0.0534 | [-0.4941,+0.3873] | no separation |
| combined, shape!=b | SELL | 38 | 26 | -0.2546 | +0.1845 | [-0.3869,+0.7559] | no separation |
| shape=b alone | pooled | 21 | 18 | -0.1457 | +0.0545 | [-0.6589,+0.7680] | no separation |
| shape=b alone | SELL | 15 | 13 | +0.2075 | +0.2227 | [-0.6065,+1.0520] | no separation |

Nothing clears here. Every point estimate that was +0.41 to +0.50 in windows 1/2
is now +0.05 to +0.19, and BUY's point estimate has flipped negative (-0.0775,
raw mean R -0.68). The `prior_shape=='b'` exclusion, which visibly tightened
the picture on the data it was derived from, does not tighten anything here -
window 3's BUY and SELL point estimates are barely different with or without it,
and neither approaches significance. This is the outcome the follow-up section
itself flagged as the live risk: a split found by sweeping ~15 attributes for
whatever separates a specific n=98 population, that does not survive contact
with data it was never fit to.

### Action taken

Combined filter and the `prior_shape=='b'` exclusion are both closed as leads -
not shipped, not carried forward as a follow-up. S2-VAR's only standing,
replicated result remains the unfiltered baseline lift from finding 23
(+0.1611 to +0.1961 depending on window) plus finding 34's funding-opposed
bucket (queued for its own window-3 check separately, since it needs funding
history that window 3's corpus does not have fetched yet). No config or gate
changed; no new tests (measurement only, reusing the existing schema and
`research/control.py`).

---

## 37. Finding 23's window 2 leg was underpowered by the same `end_as_of` bug -
    corrected, it independently clears, and pooled it tightens

Finding 23 attributed window 2's thin S2-VAR sample (n=201, 39.6% of rejects
`INSUFFICIENT_DAILY_HISTORY`) to a genuine data-availability artifact - recent
listings that hadn't accumulated 14 days of daily candles by January-February
2026. That explanation was wrong: it was the `end_as_of` bug (this session's
first fix), which made `fetch_daily` always anchor to real `now()` instead of
the caller's historical window, so the daily-candle fetch for window 2's corpus
covered the wrong calendar period entirely and starved sessions inside the
actual window of daily history they should have had.

Re-fetched window 2's daily history with the fix and re-ran the full replay
(`calibration/phase2_window2_structural_v2`, 120/120 symbols): total trades
went from 1,505 to 5,830 - roughly 4x - and `INSUFFICIENT_DAILY_HISTORY` no
longer appears in the top-18 reject funnel at all.

### Window 2 alone: same direction, same magnitude, now enough power to clear

| | n | lift | 95% CI | verdict |
|---|---|---|---|---|
| window 2 original (finding 23) | 201 | +0.2018 | [-0.0609,+0.4646] | no separation |
| window 2 corrected (this finding) | 809 | +0.1846 | [+0.0566,+0.3126] | **BEATS CONTROL** |

The point estimate barely moved (+0.2018 -> +0.1846) - exactly what should
happen if the fix only restored statistical power rather than changing the
underlying signal. S1-POC's SELL split also newly clears on the corrected
corpus (+0.2682, [+0.0114,+0.5249]), and S3-BRK stays firmly WORSE than
control at the larger sample (pooled -0.4323, [-0.6904,-0.1743]) - both the
expected regression-check outcome, not a new finding.

### Re-pooled with window 1: finding 23's headline number tightens

| | n | lift | 95% CI | verdict |
|---|---|---|---|---|
| pooled, finding 23 (original) | 1,137 | +0.1611 | [+0.0440,+0.2781] | BEATS CONTROL |
| pooled, this finding (corrected) | 1,745 | +0.1674 | [+0.0755,+0.2594] | **BEATS CONTROL** |
| pooled BUY | 828 | +0.1581 | [-0.0133,+0.3296] | no separation |
| pooled SELL | 917 | +0.1745 | [+0.0396,+0.3094] | BEATS CONTROL |

The pooled point estimate is essentially unchanged (+0.1611 vs +0.1674) and the
interval is tighter on both sides, not just wider from more data in one
direction - the strongest form of confirmation a bug fix can produce: the
correction changed how much evidence there was, not what the evidence said.
BUY still does not independently clear (as in finding 23), which remains the
one honest caveat on this result.

### Action taken

Finding 23's conclusion stands, strengthened rather than revised. No config or
gate changed - `S2_ENABLED`/`TARGET_MODE` defaults remain exactly where finding
23 left them (deliberately held back on the win-rate-vs-breakeven distinction,
not on statistical confidence, which this finding does not touch). No new
tests (measurement only, reusing `research/control.py` and window 1's existing
`controls.csv`).

---

## 38. Finding 34's funding-opposed bucket replicates on window 3

Finding 34 queued a precise, ready-to-check hypothesis: does S2-VAR's edge,
filtered to sessions where funding does NOT support the naive fade-the-crowd
thesis (the "opposed" bucket), replicate on data that was never used to find the
split? Fetched funding history for `data_cache_window3` (120 symbols, same
explicit list the corpus was originally built from - see the correction note
below), tagged every window-3 S1-POC/S2-VAR trade and its matched-random control
twin with the funding print in force at its own `decision_ts` (twins get their
own tag, never copied from the real trade - identical to finding 34's method),
same aligned/opposed definition (SELL aligned = funding positive, BUY aligned =
funding negative).

**Correction made during this fetch**: the first attempt ran `research.dataset`
against `data_cache_window3` without an explicit `--symbols` list. That root had
never had a `universe.csv` (the original corpus was built with an explicit
symbol list, bypassing it entirely), so the omission silently re-resolved the
universe by TODAY's live volume ranking and began overwriting `specs.csv` with a
different 120-symbol composition than the one `phase4_window3_structural`'s
trades actually came from. Caught after 9 symbols, stopped, `universe.csv`
removed, `specs.csv` restored from this session's own record of the original
120-symbol list, and the fetch re-run with `--symbols` explicit - which also
correctly skipped the 9 symbols' already-cached candles/daily/funding. No
corpus data was lost; the funding fetch that matters was not yet begun for any
symbol wrongly ranked out when this was caught.

### The result

| S2-VAR bucket | window 1+2 (finding 34) | window 3 (this finding) |
|---|---|---|
| baseline (unfiltered) | +0.1611 [+0.0440,+0.2781] BEATS CONTROL | +0.2039 [+0.0518,+0.3560] BEATS CONTROL |
| aligned (fade the crowd) | +0.0919 [-0.0502,+0.2340] no separation | +0.1936 [-0.0274,+0.4146] no separation |
| opposed | **+0.2249 [+0.0677,+0.3822] BEATS CONTROL** | **+0.2138 [+0.0347,+0.3929] BEATS CONTROL** |

The magnitude barely moved (+0.2249 vs +0.2138) on a corpus that had no part in
finding the split, and the qualitative pattern is identical: opposed clears
control, aligned does not (though aligned's point estimate sits closer to
clearing here than in finding 34 - +0.19 vs +0.09).

Replication within window 3's opposed bucket, same split finding 34 checked:

| split | n | g | lift | 95% CI | verdict |
|---|---|---|---|---|---|
| pooled | 357 | 103 | +0.2138 | [+0.0347,+0.3929] | BEATS CONTROL |
| BUY | 193 | 83 | +0.0844 | [-0.1523,+0.3212] | no separation |
| SELL | 164 | 75 | +0.3653 | [+0.0534,+0.6771] | BEATS CONTROL |

Same shape as finding 34's BUY/SELL split (BUY doesn't clear, SELL does, both
positive) - not a sign disagreement, and SELL's point estimate is if anything
larger here (+0.3653 vs +0.2576). S1-POC again clears nothing in either bucket,
consistent with every other S1-POC result in this project.

### Action taken

This does not, on its own, cross the phase-gate bar (BUY still doesn't
independently clear) - same honest limitation finding 34 already carried. But
three independent windows now agree on the same pattern at closely matching
magnitudes, which is materially stronger evidence than finding 34 had alone.
Not shipped, not live-wired (funding still has no live-wiring path - finding 33's
second caveat still applies). The natural next step, if pursued, is pooling all
three windows' opposed-bucket trades for a single tightened estimate the way
finding 37 did for the unfiltered baseline - not done here, left as a queued
follow-up rather than assumed. No config or gate changed; no new tests
(measurement only, reusing `research/control.py` and the existing funding
fetch/tag pattern).

---

## 39. PLAN item 7 - S2-VAR's validated edge does not hold at a 5m confirm interval

The ablation queued since item 7's plumbing landed: replay the primary corpus
(`data_cache`, `TARGET_MODE=structural`) at `--confirm-interval 5m` instead of
the 15m default, with every candle-count threshold that reads the interval
rescaled to the same real-world duration (the whole point of the earlier
plumbing pass), and compare each setup's lift against its own matched-random
control - built at 5m too, since a twin resampled at the wrong interval would
answer a different question than the real trades it stands in for.
`calibration/phase3_confirm_5m`: 6,975 candidates, 1,743 resolved.

| setup | 15m baseline (finding 37/23) | 5m (this finding) |
|---|---|---|
| S2-VAR pooled | **+0.1674 [+0.0755,+0.2594] BEATS CONTROL** | +0.0669 [-0.0488,+0.1826] no separation |
| S1-POC pooled | +0.03 [-0.29,+0.36] no separation (finding 23) | +0.1722 [-0.0750,+0.4193] no separation |
| S3-BRK pooled | -0.4687 [-0.6417,-0.2956] WORSE than control | -0.3663 [-0.5622,-0.1704] WORSE than control |
| S1-LVN | no established baseline (too thin) | -0.1219 [-0.6534,+0.4095] no separation |

**S2-VAR's replicated, three-window edge does not survive a finer confirmation
interval.** The lift that clears control cleanly at 15m (+0.1674, tightened by
finding 37) drops to a point estimate less than half the size at 5m and no
longer separates from its own control (+0.0669, CI straddles zero, BUY +0.1199
and SELL +0.0189 individually - neither clears either). S3-BRK stays WORSE than
control at 5m, same direction and similar magnitude to its established 15m
disposition - the expected regression-check pass, not a new finding. S1-LVN
stays too thin to say anything (n=24). S1-POC is the one loose thread: its BUY
split newly clears at 5m (+0.4500, [+0.0363,+0.8637], n=63/g=46) despite S1-POC
having no established edge at any interval before - a single unreplicated split
on a setup this project has never found an edge for, so it is recorded and not
acted on, the same discipline applied to every other single-split result here.

### What this means

The confirmation logic's 15m granularity is not an arbitrary implementation
choice S2-VAR happens to tolerate - it appears to be load-bearing for the one
edge this project has actually validated. A move to a finer interval, for
latency or trade-frequency reasons, would trade away S2-VAR's only standing
result, not just its confirmation lag. This is a reason to leave
`CONFIRM_INTERVAL` at 15m, not a reason to change it - the ablation's job was to
find out whether interval granularity was free to tune, and the answer is no.

### Action taken

No config or gate changed - `CONFIRM_INTERVAL` stays at its 15m default, now
with direct evidence behind that choice rather than an untested assumption. No
new tests (measurement only, reusing the existing `--confirm-interval` plumbing
and `research/control.py`).

---

## 40. PLAN item 21's first measurement - `weekly_poc_distance_atr` does not
    discriminate, and its one natural split doesn't separate either

Item 21's validation replay (`calibration/phase5_weekly_poc`, default corpus,
structural target mode - confirmed identical real-trade population to
`phase1_structural_target`, S2-VAR's n=936 matches exactly) finally populated
`weekly_poc_distance_atr` on real trades. Two "measure before gate" checks, same
discipline every other new signal in this project has gone through first.

**Check 1 - raw discrimination (finding 32's free method, AUC against win/loss).**
Direction-normalized so positive always means "weekly POC sits ahead of price in
the trade's own direction" - the economically motivated hypothesis this field
exists to test (a magnet effect: room for price to keep moving toward where last
week's volume concentrated).

| setup | n | AUC | 95% CI | verdict |
|---|---|---|---|---|
| S1-POC | 271 | 0.457 | [0.382,0.532] | null |
| S2-VAR | 932 | 0.475 | [0.434,0.516] | null |

Both null, BUY/SELL splits too (0.493/0.433 for S1-POC, 0.511/0.463 for S2-VAR) -
the same pattern finding 32 already found for every other confirmation signal
checked this way (VWAP, delta, candlestick). Only 1,203 of 3,030 resolved trades
even have the field populated - it needs a full prior calendar week, so the
first ~7 days of any corpus window never qualifies.

**Check 2 - the one pre-specified split (favorable vs unfavorable), against
control.** AUC null doesn't end the question by itself - finding 31's
`excursion_candles` also had null AUC but did show real control-beating
structure once bucketed. So this gets the single hypothesis the field was built
to test, not a multi-attribute sweep: does S1-POC/S2-VAR's result change between
"weekly POC ahead" (favorable) and "weekly POC behind" (unfavorable)? Reused
`phase1_structural_target/controls.csv` (identical real trades underlie it);
control twins don't carry this field (no traded level the way a real candidate
has one), so it was computed fresh per twin via `Scanner.frozen_weekly_bundle` -
the exact live code path, anchored to each twin's own entry price.

| S2-VAR bucket | n | lift | 95% CI | verdict |
|---|---|---|---|---|
| baseline (all) | 932 | +0.1519 | [+0.0202,+0.2835] | BEATS CONTROL |
| favorable (POC ahead) | 682 | +0.1244 | [-0.0425,+0.2914] | no separation |
| unfavorable | 250 | +0.2029 | [+0.0043,+0.4016] | BEATS CONTROL |

A mild version of the same shape finding 34 found for funding - but unlike
funding's clean, non-overlapping split, these two intervals overlap heavily
([-0.04,+0.29] vs [+0.00,+0.40]), so this does not read as a real separation,
just baseline noise around the setup's already-known edge. S1-POC clears
nothing in either bucket (+0.12/+0.10, both no separation), consistent with
every other S1-POC result in this project.

### Action taken

No gate built, no live-wiring beyond what finding 35 already shipped (the field
itself, recorded unconditionally). This closes out item 21's "measure" step with
a clean negative, the same outcome as VWAP and the candlestick/delta
confirmation signals - most new signals tried in this project carry no
information the setups weren't already using. No config or gate changed; no new
tests (measurement only, reusing `research/control.py`, `research/metrics.py`,
and `Scanner.frozen_weekly_bundle`).

---

## 41. PLAN items 5 and 9 - S1-LVN stays too rare to validate, and is not promoted

Item 5 asked for a sample large enough to give S1-LVN an actual verdict, after
sitting at n=14 since findings 20/21. Fetched 188 symbols (vs the original 120)
over 242 sessions (vs the original 120 days) - both the universe AND the
history window roughly doubled, specifically to find out whether n=14 was a
narrow-corpus artifact or a structural property of the setup.

**Result: it is structural.** Doubling both dimensions at once produced 24
resolved trades, not the order-of-magnitude jump a narrow-corpus explanation
would predict. Across 188 symbols x 242 sessions, S1-LVN fires often enough to
resolve roughly once per 47 symbol-months - a setup this rare would see very
little live volume even if it had a validated edge.

| | n | g | mean R | 95% CI | split-half | verdict |
|---|---|---|---|---|---|---|
| pooled | 24 | 20 | -0.1246 | [-0.7243,+0.4751] | early -0.59 / late +0.34, **FLIPS** | does not clear |

Matched-random control comparison (same methodology as every other setup):

| | n | g | lift | 95% CI | verdict |
|---|---|---|---|---|---|
| pooled | 24 | 20 | +0.1092 | [-0.5329,+0.7514] | no separation |
| BUY | 4 | 4 | +0.1650 | [-1.3385,+1.6684] | no separation |
| SELL | 20 | 16 | +0.0971 | [-0.6171,+0.8113] | no separation |

No separation, at intervals wide enough to contain almost any outcome. The
early/late sign flip is a second, independent reason not to read anything into
the raw mean - this is not "a real effect thinned by sample size" the way
finding 31's SELL split or finding 34's opposed bucket were (those had tight,
consistent point estimates across splits); here the point estimate itself
disagrees with its own two halves.

### The item 9 call

PLAN item 9 asked: demote S1-POC and promote S1-LVN as the primary mean-
reversion-into-the-magnet setup, once this sample existed to judge by. **The
call is no, do not promote.** Three independent reasons, not one:

1. No evidence of an edge - control comparison shows no separation, at a CI
   wide enough that a real edge in either direction is still possible, which is
   a different claim from "no edge."
2. The one replication check available (early/late) disagrees with itself.
3. Even setting aside R performance, S1-LVN's firing rate is now independently
   established as structurally low (confirmed by widening the corpus rather
   than assumed from the small original sample) - a setup this rare is a weak
   candidate for "primary" regardless of what its edge turns out to be, since
   it would rarely trade live.

This does not vindicate S1-POC either - findings 20/21 already closed its two
attempted fixes as failures, and it remains without a validated edge (finding
15's phase-gate result still stands). The honest state of both setups is
unchanged: neither is validated, and this result is a reason not to act, not a
reason to prefer one over the other.

### Action taken

No config or gate changed - both setups remain exactly as findings 15/20/21
left them. No new tests (measurement only, reusing `research/control.py`).
Item 5 and item 9 are both closed by this finding; item 9 was a strategic call
to be made once data existed, and the data says: not yet, and possibly not ever
at this setup's firing rate.

---

## 42. PLAN item 6 - the multi-bin delta read is null, same as the single-bin
    version it widens

Item 6 widened `delta_opposed`'s single-bin taker-imbalance read to a window of
bins (`multi_bin_delta_normalized`), recorded unconditionally since it was
built - and was already "modest expectations given the single-bin version
didn't rescue anything" (finding 18). It was marked as needing a fresh replay
to measure; that replay already happened for an unrelated reason
(`calibration/phase5_weekly_poc`, item 21's corpus) and populated this field on
every S1-POC/S2-VAR trade in the process (1,207 of 1,207 - full coverage,
unlike `weekly_poc_distance_atr` which needs a prior calendar week). No new
replay was needed; this had simply never been checked against data that
already existed.

Same free AUC screen (finding 32's method), same direction-normalization as
finding 31's "delta supportive" (BUY positive = supportive, SELL negative):

| setup | n | AUC | 95% CI | verdict |
|---|---|---|---|---|
| S1-POC | 271 | 0.506 | [0.430,0.583] | null |
| S2-VAR | 936 | 0.530 | [0.488,0.571] | null |

Both null, BUY/SELL splits too (0.521/0.496 for S1-POC, 0.548/0.528 for
S2-VAR). Widening the window did not surface discrimination the single bin
lacked - the expected outcome given finding 18's own result, now confirmed
rather than assumed.

### Action taken

No gate built (`delta_opposed_multibin` stays default OFF, as it always was).
Not pursuing a combined-filter search the way finding 31 tried for the
single-bin version - that combo (with `excursion_candles`) was already checked
and closed on independent grounds (finding 36), and inventing a new multi-bin
combination post-hoc here would be exactly the kind of search finding 36 itself
warns against. No config or gate changed; no new tests (measurement only,
reusing data already on disk).

---

## 43. Why the live win rate is low: not direction, the target distance -
    specifically the naked POC

A direct question deserved a direct, mechanism-level answer rather than another
aggregate statistic: given S2-VAR is the only setup with any validated edge
(finding 37, +0.1674R lift, BEATS CONTROL, win rate 28.3%, breakeven 37.7%),
WHY is the win rate still this low? Three hypotheses - the core premise is
weak, price usually reverses immediately, or the target is too far - checked
directly against `mfe_r`/`mae_r`/`r_multiple`, already in the schema, no new
replay needed. Pooled across all three independent windows, n=2,466 resolved
S2-VAR trades.

**Not the core premise.** BUY and SELL win rates are nearly identical (28.1%
vs 28.5%) - no directional bias. And when the target happens to be a nearby
level, the setup works: see below.

**Not "usually reverses immediately" either.** Of the 71.7% of trades that
stop out, only 34.8% show near-zero favourable movement (mfe_r < 0.15R) before
reversing. The other 65% move somewhat in the trade's favour first - 19.4% get
within 30% of a full R before failing. Winners endure real drawdown too (mean
`mae_r` 0.307R) before working. Entries are noisy in both directions, not
uniquely doomed.

**It is the target distance, and specifically one candidate.** Win rate falls
off a cliff as the chosen target gets further away:

| target distance | n | win rate |
|---|---|---|
| 1.2-1.5R | 875 | **44.8%** |
| 1.5-2.0R | 557 | 29.1% |
| 2.0-3.0R | 525 | 20.0% |
| 3.0R+ | 502 | **7.8%** |

And the mechanism is `structural_target()`'s own top preference - the untested
("naked") POC. It wins the flat nearest-worthwhile sort on 63-66% of S2-VAR
trades, consistently across all three independent windows, at an average 2.83R
away:

| | w1 | w2 | w3 |
|---|---|---|---|
| naked-POC targets (share of trades) | 60.9% | 64.4% | 65.9% |
| naked-POC win rate | 23.3% | 22.1% | 19.6% |
| near-level (POC/VAH/VAL) win rate | 36.9% | 40.5% | 45.5% |

The near-level bucket (same-session POC/VAH/VAL, averaging 1.46R) wins
36.9-45.5% of the time - close to or above the 37.7% breakeven line on its
own. Two-thirds of trades are instead sent at a far, speculative level, and
that is what pulls the pooled win rate down to 28%.

### Action taken (diagnostic only, this finding)

No code or config changed by this finding itself - it identifies the mechanism
and hands off to PLAN item 25 (below) to build and test the direct fix: rank
the session's own levels first, fall through to the naked POC only when
nothing near clears `TARGET_MIN_R`. No new tests (measurement only, reusing
`mfe_r`/`mae_r`/`r_multiple`/`target_kind`, all already in the schema).

---

## 44. PLAN item 25 - ranking near levels first, built and under test

Built the direct fix finding 43 diagnosed: `structural_target()` now takes an
opt-in `prefer_near` parameter (and `choose_target()`/`config.
TARGET_PREFER_NEAR_LEVEL` passing it through, default False, plus a
`research.replay --target-prefer-near` CLI override) - when True, the
session's own POC/VAH-VAL are tried first and ranked among themselves only;
the naked-POC/HVN/prior-extreme set is tried only once nothing near clears
`TARGET_MIN_R`. False reproduces the exact existing flat-list,
nearest-worthwhile-wins behaviour with no change.

Caught one real bug while building it: the "nothing anywhere clears the
floor" fallback branch unpacked a `(kind, price)` tuple as `price, kind`,
silently swapping a price for a kind string. Caught immediately by the new
unit test that exercises exactly that branch (`TypeError` on first run, since
the swapped values later hit a numeric comparison) - fixed before any replay
ran on it. 9 new tests (348 total): 7 for `structural_target`'s near/far
ranking and fallback logic (including the exact scenario finding 43 diagnosed
- a far candidate nearer in raw distance than a near one, which must still
lose to the near one), 1 for `choose_target`'s wiring, 1 pipeline-level case
proving the config flag reaches `poc_rotation.detect()` for real.

### Action taken

Not shipped - this is the build, not the measurement. A full default-corpus
replay with `TARGET_PREFER_NEAR_LEVEL=True` is running now
(`calibration/phase7_prefer_near`) to compare against the proven baseline
(findings 19/23/37: n=2,466 pooled across three windows, win rate 28.3%,
breakeven 37.7%) the same way every other variant in this project has been
tested - never a silent default change. Results pending.

---

## 45. PLAN item 25 measured - `prefer_near` is a near no-op, and that is itself
    the finding

`calibration/phase7_prefer_near` (default corpus, `TARGET_PREFER_NEAR_LEVEL=True`):
S2-VAR n=936 - **the exact same n as the untouched baseline** - win rate 28.2%
(baseline 28.2%), mean R -0.2357 (baseline -0.236), naked POC still chosen on
58.5% of trades (baseline 60.9%) still winning only 22.3%. Matched-random
control: +0.1345 lift, [+0.0002,+0.2687], BEATS CONTROL by a hair - essentially
unchanged from baseline's own standing result.

**The fix targeted the wrong lever.** Ranking the session's own POC/VAH/VAL
ahead of the naked POC only matters when the near levels are viable
competitors - and they almost never are: `prefer_near=True` still falls
through to the far group on the large majority of trades, because the near
levels fail `TARGET_MIN_R` on their own almost two-thirds of the time
regardless of ranking. The flat-list baseline was never "wrongly" picking the
naked POC over a better nearby option - the nearby option is usually just too
close to produce a 1.2R target at all, given S2-VAR's own stop placement. This
is a more precise statement of finding 43's mechanism, not a correction of it:
distance is still the right diagnosis, but reordering candidates cannot fix a
shortage of viable near candidates.

### Action taken

Not shipped - no change over baseline to justify one. `TARGET_PREFER_NEAR_LEVEL`
stays default False. No config or gate changed. The natural next lever, not
built here: something that changes whether near levels clear the floor at all
(a smaller `TARGET_MIN_R` for near candidates specifically, or a stop-placement
change that leaves more room for VAH/VAL/POC to clear it) rather than how
candidates already in the running get ordered.

---

## 46. PLAN item 17 measured - HVN/prior-extreme enrichment genuinely improves
    the picture, but does not clear the phase-gate bar

`calibration/phase6_target_enriched` (default corpus, both flags True):
S2-VAR n=1,333, win rate **32.3%** (baseline 28.2%), mean R **-0.1978**
(baseline -0.236) - a real shift, not noise-sized. Mechanism: the prior
session's high/low is nearer than the naked POC (1.68R vs 2.83R average) and
clears `TARGET_MIN_R` more easily under the SAME flat nearest-worthwhile sort
baseline already uses, so it displaces the naked POC as the dominant choice
(55.7% of trades vs the naked POC's 21.4%, down from the naked POC's 60.9%
share at baseline).

| | n | win rate | mean R | breakeven | control lift | verdict |
|---|---|---|---|---|---|---|
| baseline (window 1) | 936 | 28.2% | -0.236 | 37.7% | +0.1515 [finding 19] | BEATS CONTROL |
| item 17 enriched | 1,333 | 32.3% | -0.198 | 40.0% | +0.1643 [+0.0612,+0.2673] | BEATS CONTROL |

| S2-VAR split | n | lift | 95% CI | verdict |
|---|---|---|---|---|
| pooled | 1,333 | +0.1643 | [+0.0612,+0.2673] | BEATS CONTROL |
| BUY | 659 | +0.1976 | [-0.0154,+0.4107] | no separation |
| SELL | 674 | +0.1302 | [-0.0242,+0.2847] | no separation |

Clears pooled, same as baseline always has - but the same honest limitation
every variant in this project carries: BUY and SELL do not individually clear.
This is single-window (window 1 only), so it has not yet been checked for the
early/late or independent-window replication the phase-gate rule requires
either. Breakeven also rose slightly (40.0% vs 37.7%, since the payoff ratio
dropped a little as targets got nearer on average) - the improvement is in win
rate and mean R, not a free lunch on every dimension.

### Action taken

Not shipped - promising, genuinely moves the headline numbers in the right
direction, but has cleared exactly one of the three checks (window1
control-beat) that findings 19/23/37 together took to validate the current
baseline. `TARGET_INCLUDE_HVN`/`TARGET_INCLUDE_PRIOR_EXTREME` stay default
False. Queued next steps, not yet done: BUY/SELL and early/late splits on this
same corpus, then an independent second/third window the way finding 23 did
for the baseline - before any live-wiring decision.

---

## 47. PLAN item 17 fully replicated - HVN/prior-extreme enrichment clears the
    same three-window, BUY/SELL-split bar the baseline needed findings 19/23/37
    to clear

Finding 46 left item 17 with exactly one of three checks done (window 1
control-beat, pooled only). Replayed the same enriched-target variant
(`--target-mode structural --target-include-hvn --target-include-prior-extreme`)
on `data_cache_window2` and `data_cache_window3`, then ran matched-random
control comparisons (fresh twins per window, since enrichment changes trade
geometry) with BUY/SELL splits on all three windows plus a pooled-all-three
estimate - the same methodology finding 37 used to finally validate the
baseline.

Win rate and mean R keep improving on the held-out windows, not just holding:

| window | n (S2-VAR) | win rate | mean R | breakeven |
|---|---|---|---|---|
| baseline, window 1 | 936 | 28.2% | -0.236 | 37.7% |
| item 17, window 1 | 1,333 | 32.3% | -0.198 | 40.0% |
| item 17, window 2 | 1,225 | **37.3%** | **-0.076** | 40.3% |
| item 17, window 3 | 1,026 | **36.3%** | **-0.111** | 40.6% |

Control lift, pooled and split, all three windows plus the combined estimate:

| window | split | n | g (clusters) | lift | 95% CI | verdict |
|---|---|---|---|---|---|---|
| window 1 | pooled | 1,333 | 118 | +0.1643 | [+0.0612,+0.2673] | BEATS CONTROL |
| window 1 | BUY | 659 | 111 | +0.1976 | [-0.0154,+0.4107] | no separation |
| window 1 | SELL | 674 | 115 | +0.1302 | [-0.0242,+0.2847] | no separation |
| window 2 | pooled | 1,225 | 119 | +0.2047 | [+0.0951,+0.3144] | BEATS CONTROL |
| window 2 | BUY | 594 | 110 | +0.1529 | [-0.0974,+0.4032] | no separation |
| window 2 | SELL | 631 | 114 | +0.2535 | [+0.0607,+0.4464] | **BEATS CONTROL** |
| window 3 | pooled | 1,026 | 118 | +0.2305 | [+0.0874,+0.3736] | BEATS CONTROL |
| window 3 | BUY | 501 | 106 | +0.1688 | [-0.0932,+0.4307] | no separation |
| window 3 | SELL | 525 | 111 | +0.2888 | [+0.0555,+0.5220] | **BEATS CONTROL** |
| **all 3 pooled** | **pooled** | 3,584 | 355 | +0.1971 | [+0.1295,+0.2648] | **BEATS CONTROL** |
| **all 3 pooled** | **BUY** | 1,754 | 327 | +0.1748 | [+0.0375,+0.3121] | **BEATS CONTROL** |
| **all 3 pooled** | **SELL** | 1,830 | 340 | +0.2184 | [+0.1076,+0.3293] | **BEATS CONTROL** |

Every single cell across all three windows and both directions is a positive
point estimate - zero sign flips anywhere, which is the thing finding 31's
combined filter failed outright (window 3 flipped negative). SELL clears
independently in windows 2 and 3 (not window 1, which is itself consistent -
window 1 is the smallest-n SELL leg here). BUY never clears in any single
window alone (each window's BUY leg is ~500-660 trades, ~110 clusters - underpowered
on its own, same shape as finding 23's original window 2 before the
`end_as_of` fix added power), but pooling the three windows' BUY trades
(n=1,754, g=327 clusters - nearly 3x the clusters of any single window) gives
BUY real power and it clears cleanly: +0.1748 [+0.0375,+0.3121].

This is the first targeting change in the project to clear the full bar
findings 19/23/37 together needed for the baseline itself: three independent
windows, no sign flips, and both BUY and SELL beating control (BUY needs the
pooled-window combination to get there, exactly as the baseline's BUY leg
needed finding 37's bug fix to get there - not a new standard, the same one).

### Action taken

This is now validated evidence, not a lead. Recorded here as the full
replication result; whether to flip `TARGET_INCLUDE_HVN` /
`TARGET_INCLUDE_PRIOR_EXTREME` to default True (i.e. change live behavior) is
a separate decision to raise explicitly, not an automatic next step of
measurement - per this project's standing practice of keeping "validate" and
"ship" as distinct, separately-authorized steps.

---

## 48. PLAN item 24 - the pooled three-window funding check does not separate
    S2-VAR's edge, so the window-3 "opposed bucket" finding does not survive

Finding 34 found S2-VAR's edge concentrated in the "funding opposed" bucket, and
finding 38 replicated that on window 3. The pooled three-window check (window 2
funding fetched for this purpose: 5,214 records, `data_cache_window2/funding`)
uses the same aligned/opposed tagging and the same matched-random controls,
with each window's controls built fresh against its own root.

| window | aligned lift | opposed lift | opposed BUY | opposed SELL |
|---|---|---|---|---|
| window 1 (n=936) | +0.0700 no sep. | +0.2295 BEATS | +0.2011 no sep. | +0.2558 no sep. |
| window 2 (n=809) | +0.2112 BEATS | +0.1533 no sep. | +0.0909 no sep. | +0.2036 no sep. |
| window 3 (n=721) | +0.1936 no sep. | +0.2138 BEATS | +0.0844 no sep. | +0.3653 BEATS |

| all three pooled | n | lift | 95% CI | verdict |
|---|---|---|---|---|
| baseline | 2,466 | +0.1784 | [+0.0997,+0.2571] | BEATS CONTROL |
| aligned | 1,276 | +0.1535 | [+0.0498,+0.2572] | BEATS CONTROL |
| opposed | 1,190 | +0.2044 | [+0.1030,+0.3059] | BEATS CONTROL |
| opposed BUY | 621 | +0.1437 | [-0.0164,+0.3038] | no separation |
| opposed SELL | 569 | +0.2689 | [+0.1119,+0.4259] | BEATS CONTROL |

The aligned bucket also beats control once pooled, and the opposed-minus-aligned
gap is +0.05 with a standard error near 0.07. The two buckets are not separated.
Window 2 has the reverse ordering from windows 1 and 3, so the earlier opposed
story was a two-window artefact of the window-1 pattern plus window 3's replication, not a
stable split. The "opposed" bucket is not a filter, and nothing is shipped.

### Action taken

Item 24 closed as a null split. Funding remains recorded for measurement only;
no gate or config is changed.

---

## 49. Order-flow confirmation from aggTrades (window 3) - underpowered, and a
    control-timing bug found along the way

Built per-minute taker-flow aggregates from Binance's public aggTrades archives for
window 3 (streamed, one zip at a time, 14,280 symbol-days; 12,649 built, 1,109 not
listed on the archive for that day, 119 persistent download failures across 19 symbols,
concentrated in newly listed coins). Joined to S2-VAR real trades and matched-random
controls via research/control.py.

- **$100k large-order net flow (15 minutes before entry):** the pre-registered primary
  had no usable sample. Only 34 of 718 real trades had any $100k trade in the window.
  Not evaluable.
- **$10k mid-and-large net flow (pre-registered after the $100k result):** supportive
  n=132 lift +0.2435 [-0.0471,+0.5340]; not supportive n=88 lift +0.1662
  [-0.1510,+0.4834]. No separation in either bucket, BUY or SELL. Coverage is thin: 220
  of 718 real trades and 1,062 controls had any qualifying trade in their window. An
  underpowered null, not evidence either way.

**Control-timing bug (found while reading the $10k output):** `research/control.py`
records a twin's `decision_ts` as the confirm bar's OPEN, but the twin enters at the
bar's CLOSE. Real trades record the bar's last millisecond, so the two conventions are
15 minutes apart. Any feature measured at `decision_ts` therefore reads controls 15
minutes earlier than real trades. The control's net_r is unaffected (it uses the close).
Features that are time-sensitive at minute scale, such as the CVD and absorption reads
being built now, and any 15-minute window, are affected. Earlier result tables that
measured features at `decision_ts` for controls should be treated as carrying this
offset until re-checked: funding (finding 48, where a print is about eight hours apart,
so the impact is expected to be small), and the weekly-POC measure (finding 40, which
used a per-twin recomputation anchored at `decision_ts`). The two flow scripts now use
each row's real entry boundary. `control.py` itself is not changed here, so the shared
convention stays as it was until that is decided deliberately.

### Action taken

Order flow remains unvalidated for this corpus. Option 1 (CVD and absorption from 1m
klines, post-hoc) was stopped mid-run after the timing bug was found and restarted with
corrected entry boundaries. The Option 2 result is recorded as underpowered, not as a
rejection of flow confirmation.

---

## Phase 0 calibration work list

Ordered by how much damage a wrong value does.

1. `SHAPE_TREND_MAX_VA_RANGE_RATIO` — decides the auction state.
2. `SHAPE_TREND_MIN_POC_MIGRATION_ATR` — also decides the auction state, and is
   known to be noisy.
3. `POC_MIN_PROMINENCE`, `PROFILE_MIN_*` — false-null risk if too strict.
4. `ACCEPT_MIN_OUTSIDE_VOL_FRACTION` / `REJECT_MAX_OUTSIDE_VOL_FRACTION` — the
   S2/S3 discriminator. The **gap** between them matters as much as the levels:
   too narrow and both setups fire on the same move, too wide and neither ever
   does.
5. `SHAPE_P_MIN_POC_POSITION` / `SHAPE_B_MAX_POC_POSITION`, `HVN_PCT` / `LVN_PCT`,
   `EXCESS_*`.
6. `BIN_ATR_FRACTION` — verify the ~50-bins-per-ATR target actually holds across
   the liquidity range, since every threshold above is resolution-sensitive.
