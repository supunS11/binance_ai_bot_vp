# Plan

One consolidated list, tracked to completion. Items 1-12 come from the "Full System
Review & Solutions" document (theoretical mismatches, setup-specific weaknesses,
architectural weaknesses, Priorities 1-5); items 13-18 were added afterward from the
separate "Deep Review of the TP/SL & Protection Mechanism" document. Status is updated
as each item lands; see CALIBRATION.md for the underlying evidence behind every
"done" or "resolved by measurement" verdict.

Legend: `[x]` done · `[~]` blocked/in progress · `[ ]` not started · `[shelved]`
resolved without building anything · `[deferred]` deliberately not now · `[out of
scope]` explicitly declined.

## Now / imminent

- [x] **1. Second-window S2-VAR validation.** Finish the in-flight second-window
  replay, merge, run control on S2-VAR under `structural` against the independent
  window. Gating result for everything downstream. **Result: cleared the phase gate**
  — pooled both windows, lift +0.1611R, 95% CI [+0.0440, +0.2781], excludes zero.
  CALIBRATION.md finding 23. **Window 2's leg re-checked** after the `end_as_of`
  fix (it had been silently underpowered by the same bug, 1,505 trades instead
  of the true 5,830) — corrected, window 2 now independently clears
  (+0.1846, [+0.0566,+0.3126]) and the repooled result tightens to +0.1674,
  [+0.0755,+0.2594], same point estimate, narrower interval both sides.
  Finding 37.

## Cheap, novel, done next (independently testable, no dependencies)

- [x] **2. `S1_MIN_CONFIRMATIONS`: 1 → 2**, replay, control. **Result: rejected** —
  the apparent lift was BUY-only and matches the already-diagnosed market-drift
  confound (finding 17), not independent evidence. Stays at default 1. Also revealed
  a real tradeoff with item 5 (shared knob, S1-LVN's sample shrinks further at 2-of-4).
  Finding 25.
- [x] **3. Base rate of poor/excess extremes across *all* sessions**, not just
  S2-qualifying ones, before touching any threshold. **Result: real bug found** — the
  old hardcoded `poor_ceiling` (0.40) sat above the 99th percentile; measured and
  replaced with `config.POOR_EXTREME_MAX_BIN_VOLUME_PCT=0.15`. Finding 24.
- [x] **4. S2-VAR rejection-quality measurement** — return speed and return-candle
  delta against outcome, pooled across both windows (n=1,137 resolved). Alone: slow
  excursions (>=4 candles) beat control (+0.26R), fast ones don't (opposite of the
  naive "fast rejection = clean" theory); delta barely discriminates either direction
  alone. **Combined (slow + delta-supportive)**: n=157, g=92, mean R turns **positive**
  (+0.0355) for the first time anywhere in this project, lift +0.4299 CI
  [+0.1632,+0.6967] BEATS CONTROL — the strongest number yet. Replication is mixed,
  not clean: BUY clears (SELL doesn't), but window1/window2 point estimates nearly
  match (window2 just underpowered, n=31) and early/late agree in direction. A drift
  check (finding 17's own method) shows market drift explains only ~1/3 of the
  BUY/SELL gap here, not nearly all of it like findings 17/25 - a materially
  different, not-yet-resolved case. **Not shipped**, flagged as the top open lead;
  needs a third window or SELL-focused follow-up. Finding 31.
  **Closed, did not replicate on window 3**: same combined filter, and the
  `prior_shape=='b'` exclusion the follow-up flagged as unconfirmed, both checked
  against the third independent window (4,981 trades, post `end_as_of` fix).
  Every point estimate collapsed to no-separation (pooled +0.07, BUY flipped to
  -0.08, SELL +0.19 but CI wide) — no config or gate change was ever made on this
  lead. Finding 36. S2-VAR's only standing result is finding 23's unfiltered
  baseline lift plus finding 34's funding-opposed bucket.
- [x] **5. S1-LVN sample expansion** — wider symbol universe and/or longer history, to
  move S1-LVN from "unmeasured" (n=14) to an actual verdict. **Result: it is
  structurally rare, not narrow-corpus-starved.** Doubled both the universe
  (120→188 symbols) and the history window (120→242 sessions) at once; only
  n=24 came back (up from 14). Control comparison: no separation
  (+0.1092, [-0.5329,+0.7514]). Early/late split flips sign (-0.59/+0.34) - the
  point estimate disagrees with its own two halves, a second independent
  reason not to read anything into it. Finding 41.

## Moderate cost, after the above

- [x] **6. Multi-bin delta confirmation** — opposing delta across the bins *around*
  the level, not just the single POC/VAH-VAL bin `delta_opposed` reads. Modest
  expectations given the single-bin version didn't rescue anything (finding 18).
  **Plumbing done**: `setups.base.multibin_delta_normalized`, recorded
  unconditionally as `multi_bin_delta_normalized` on S1-POC/S2-VAR candidates (same
  "measure before gate" pattern as `bin_delta_normalized`), new opinion gate
  `gates.checks.delta_opposed_multibin` (default OFF, `GATE_DELTA_OPPOSED_MULTIBIN_ENABLED`,
  window `MULTIBIN_DELTA_WINDOW_BINS=2`), wired into every setup's gate profile, 8
  new tests (275 total). **Measured: null, same as the single-bin version.**
  Turned out to need no new replay — item 21's `phase5_weekly_poc` corpus already
  populated it on every S1-POC/S2-VAR trade. AUC against win/loss: S1-POC 0.506
  [0.430,0.583], S2-VAR 0.530 [0.488,0.571], both null, BUY/SELL splits too. No
  gate built. Finding 42.
- [x] **7. Confirmation-interval (15m) ablation** — **prerequisite done**: every
  candle-count threshold that reads `CONFIRM_INTERVAL` (`ACCEPT_MIN_CANDLES`,
  `ACCEPT_MIN_CANDLES_OUTSIDE`, `REJECT_MAX_CANDLES_OUTSIDE`,
  `ACCEPT_MIN_BASELINE_CANDLES`, `CONFIRM_CONSECUTIVE_CANDLES`) is now derived from a
  canonical minute duration (`config.candles_for_minutes`), so changing the interval
  rescales them instead of silently redefining what they mean. At the 15m default,
  reproduces the exact historical values (3/13/5/6/2) — regression-locked by test.
  New `research.replay --confirm-interval` flag rescales and reruns; smoke-tested at
  5m -> 9/39/15/18/6, matches the math exactly. 280 tests pass (275 + 5). **Ablation
  result: S2-VAR's edge does NOT hold at 5m.** Full primary-corpus replay at
  `--confirm-interval 5m` (`calibration/phase3_confirm_5m`, 6,975 candidates, 1,743
  resolved), matched-random control built at 5m too (twins must share the interval
  they're compared against). S2-VAR's 15m pooled lift (+0.1674, BEATS CONTROL,
  finding 37) drops to +0.0669 [-0.0488,+0.1826] no separation at 5m — neither BUY
  nor SELL clears either. S3-BRK stays WORSE than control (expected regression
  check). S1-POC's BUY split newly clears at 5m (+0.4500) but is a single
  unreplicated split on a setup with no established edge, recorded not acted on.
  **Conclusion: `CONFIRM_INTERVAL=15m` stays as-is** — the ablation's purpose was
  to check whether the interval is free to tune, and the answer is no: it is
  load-bearing for the only edge this project has validated. Finding 39.
- [x] **8. HTF regime overlay as a combination test** — measured retrospectively from
  existing pooled data (no new replay). S1-POC: confirmed no-op, as expected. S2-VAR:
  the combined filter's pooled lift (+0.2031, CI [+0.0093,+0.3969]) technically clears
  BEATS CONTROL, but fails the phase-gate replication check outright - early/late
  splits diverge sharply (+0.33 vs +0.09), so it does not replicate. Almost the entire
  effect comes from `shape_opposed` alone (which itself misses zero by 0.002);
  `htf_value_opposed` contributes almost nothing. **Not shipped** - noise on the
  favourable side of zero, not a rescue. Finding 28.

## Strategic call, not a re-test

- [x] **9. Demote S1-POC, promote S1-LVN** as the primary mean-reversion-into-the-
  magnet setup, once item 5's sample is large enough to actually judge. Backed by two
  independent failed fixes (findings 20, 21), not another experiment. **Call: no,
  do not promote.** Item 5's expanded sample shows no control-beating edge, no
  replication (early/late flips sign), and independently confirms S1-LVN fires too
  rarely to serve as a primary setup regardless of its economics. This does not
  vindicate S1-POC either - findings 20/21 already closed its fixes as failures.
  Neither setup is validated; this is a reason not to act, not a tiebreaker.
  Finding 41.

## Large, deferred, own sub-project

- [deferred] **10. 4h session-length rebuild** — theoretically legitimate (24h profile
  averages Asia/EU/US regime changes together), but not a parameter tweak: bin width,
  ATR definition, every acceptance/excursion threshold, all of Phase 0 calibration is
  anchored to a daily session. Scoped as its own sub-project, not started until 1-9
  are further along.
- [x] **11. Shape-threshold calibration against an independent regime label** — built
  `research/regime.py` (Kaufman efficiency ratio, closes-only, no volume-profile
  inputs — research-only, never imported live per principle P1), computed for all
  14,185 existing corpus sessions with no new fetch/replay. Result: the classifier's
  **output** validates cleanly (trend vs D, AUC 0.718 CI [0.706,0.730], monotonic
  across all 5 labels) — but the two mechanisms behind it are very unequal:
  `intra_poc_migration_atr` alone (AUC 0.787) carries more signal than the combined
  output, while `va_range_ratio` alone is weak (AUC 0.558) and, surprisingly, points
  in the **opposite** direction from its "low ratio = trend" assumption. Nothing
  shipped/changed live — diagnostic only, flags `va_range_ratio`'s direction as
  worth a second look. Finding 30.

## Explicitly out of scope

- [out of scope] **12. Liquidity-sweep confluence for S1-POC** — real feature
  addition (order-block/liquidity-sweep detection), belongs to a different bot in
  this workspace. Not recommended unless scope is deliberately expanded.

## From the TP/SL mechanism review

- [x] **13. Partial-fill target-sizing race** — `_settle_fill` sized the target off
  whichever poll first detected `PARTIAL`, before the cancel landed; more could fill
  in that gap, undersizing the target with no self-healing path. Fixed: re-query
  `order_state()` after cancel, use the freshest filled quantity. Correctness fix, not
  a calibration result. Finding 26.
- [shelved] **14. Widen stop past HVN** (reuse S3's `_widen_past_lvn` pattern) —
  measured first: `stop_inside_hvn=True` is 0/1,137 (S2-VAR) and 4/347 (S1-POC) under
  current stop formulas. No population to act on without a more fundamental change to
  how these stops are computed. Finding 27.
- [shelved] **15. Test `stop_inside_hvn`/`target_behind_hvn` as reject gates** (as
  they exist today, never swept per finding 18) — same measurement as 14 kills this
  too: `target_behind_hvn` is 95.9% true for S2-VAR's structural targets (near-
  tautological, since a structural target aims at a POC, the center of an HVN by
  definition) — gating on it would reject almost the entire validated population.
  Finding 27.
- [x] **16. Document the MARK_PRICE-vs-replay-price divergence** — live stops trigger
  on mark price, replay has no mark-price series and triggers off candle high/low.
  Recorded as a known limitation in README, not fixed (historical mark price likely
  unavailable to replay against).
- [x] **17. Enrich `structural_target()`'s candidate set** (HVNs, prior session's
  opposite extreme) — item 4 has landed, no longer blocked. Must be validated as a
  new variant against the current, already-proven candidate set, never a silent
  swap. **Built**: `structural_target()`/`choose_target()` take new opt-in
  `hvns`/`prior_extreme` params (None by default - the proven candidate set is
  unchanged unless explicitly supplied), wired into S1-POC/S2-VAR behind two new
  config flags (`TARGET_INCLUDE_HVN`, `TARGET_INCLUDE_PRIOR_EXTREME`, both
  default False), new `research.replay --target-include-hvn`/
  `--target-include-prior-extreme` CLI overrides. 12 new tests (339 total),
  including a pipeline-level case showing the real motivation: a fixture where
  VAH is too close to clear `TARGET_MIN_R` and the baseline falls back to it
  anyway as "best available" - exactly the failure mode this item exists to
  improve on - while the enriched set finds a further level that genuinely
  clears the floor. **Measured and fully replicated across all 3 independent
  windows.** Win rate keeps improving on held-out data, not just holding:
  32.3% (window 1) → 37.3% (window 2) → 36.3% (window 3), vs 28.2% baseline.
  Control lift positive in every window/direction cell with zero sign flips;
  pooled-3-window lift +0.1971 [+0.1295,+0.2648] BEATS CONTROL, and critically
  **both BUY and SELL clear independently when pooled across windows**
  (BUY +0.1748 [+0.0375,+0.3121], SELL +0.2184 [+0.1076,+0.3293]) — SELL also
  clears standalone in windows 2 and 3. This is the same bar findings 19/23/37
  together took to validate the baseline itself (BUY needed pooling for power
  there too). **Not yet shipped** — validated evidence now exists, but flipping
  `TARGET_INCLUDE_HVN`/`TARGET_INCLUDE_PRIOR_EXTREME` to default True is a
  live-behavior change and a separate decision to raise explicitly. Findings
  46, 47.
- [deferred] **18. Time/regime-based exit, developing-profile invalidation, partial-
  profit scaling, confirmation-gated breakeven** — explicitly post-validation only,
  each to be built as its own toggleable, measured-before-gated mode. Not started.

## From the confirmation-methods audit (user request: VWAP, order flow, market
## structure, HTF levels, momentum, OI/funding, candlestick rejection)

- [x] **19. Audit what already exists under each heading.** Candlestick rejection
  and order-flow delta are already core (S1-POC/S1-LVN's confirmation vote);
  market structure is already S3-BRK's mechanism in substance; HTF value migration
  is measured (weak). Genuine gaps found: VWAP computed-but-unused, a real weekly
  composite profile never built (`weekly_levels` dead field), RSI/momentum absent
  by deliberate design (principle P1), OI/funding entirely absent. Free bonus: the
  4 confirmation signals' individual contribution had never been ablated despite
  the architecture being built for exactly that — done, none discriminate for
  S1-POC. Finding 32.
- [x] **20. VWAP-band position, measured.** `setups.base.vwap_zscore`, recorded
  unconditionally as `vwap_zscore_at_level` on S1-POC/S2-VAR candidates, same
  pattern as the delta fields. Not yet populated on real trades (needs a replay);
  not gated.
- [x] **21. Real HTF weekly composite profile** (`ctx.weekly_levels`) — built,
  not a new design: `Scanner.frozen_weekly_bundle`, aggregates the previous
  complete calendar week onto the same bin lattice as the daily profile,
  frozen/cached the same way `frozen_bundle` is. Live-wired (`build_context`
  sets `ctx.weekly_levels` for both live and replay). Its measure-before-gate
  companion `weekly_poc_distance_atr` recorded unconditionally on S1-POC/S2-VAR.
  Verified against real corpus data (BTCUSDT), 18 new tests, 327 total. Finding
  35. **Measured: clean negative.** AUC against win/loss is null for both
  setups (S1-POC 0.457, S2-VAR 0.475, both CIs straddle 0.5). The one
  pre-specified split the field was built to test (favorable vs unfavorable —
  is weekly POC ahead of price in the trade's direction) doesn't separate
  either: S2-VAR's two bucket CIs overlap heavily ([-0.04,+0.29] vs
  [+0.00,+0.40]), S1-POC clears nothing in either bucket. No gate built — same
  outcome as VWAP and the candlestick/delta confirmation signals. Finding 40.
- [out of scope, pending] **22. RSI/momentum** — directly contradicts principle P1.
  Recommended against; a volume-native alternative (CVD/order-flow rate-of-change)
  proposed instead if the user wants the same purpose served. Awaiting explicit
  direction before any code.
- [shelved] **23. Open interest** — Binance's free API caps history at ~30 days
  (confirmed against official docs + a live empirical check), far short of what
  this project's methodology needs to backtest against. Not worth building
  plumbing for a feature that can't be validated here without paid third-party data.
- [x] **24. Funding rate - closed as a null split (finding 48).** Pooled three
  windows: the aligned and opposed buckets both beat control and their gap is
  not significant (+0.05, SE ~0.07), so funding does not separate S2-VAR's edge.
  Window 2 reversed the window-1/3 ordering. Nothing shipped.
  Earlier history: **Funding rate, fetched, diagnosed, and control-checked.** No retention
  cap (verified back to contract inception). Fetch infra built and tested
  (finding 33). Control comparison (finding 34, reused the existing phase1/phase2
  matched-random twins) found the naive "aligned/opposed" gap is NOT a setup-
  specific contribution — a random entry in the same symbol/session/direction
  shows the identical gap, sometimes larger. But it reframes something real:
  S2-VAR's *already-validated* edge over its own control concentrates entirely in
  the "funding opposed" bucket (+0.2249R lift, BEATS CONTROL, bigger than the
  unfiltered baseline) and is statistically absent in "aligned" (+0.0919, no
  separation). Replication is mixed (BUY/early/window2 don't individually clear)
  but unusually tight — every point estimate sits in +0.19 to +0.26, no sign
  disagreements anywhere, unlike finding 31's 3x BUY/SELL spread. **Replicated on
  window 3**: fetched funding for `data_cache_window3`, same aligned/opposed
  tagging, same control methodology — opposed bucket clears again at nearly
  identical magnitude (+0.2138 vs +0.2249), aligned still doesn't clear, SELL
  clears within opposed but BUY still doesn't (same shape as before, SELL's point
  estimate even larger: +0.3653 vs +0.2576). Finding 38. **Still not shipped** —
  BUY not independently clearing is the same honest limitation as before, and
  funding still has no live-wiring path (finding 33's second caveat). Three
  independent windows now agree at closely matching magnitudes; pooling all three
  for one tightened estimate (the way finding 37 did for the unfiltered baseline)
  is the natural next step, not yet done.

## Diagnosing the live win rate

- [x] **25. Rank near (same-session) levels ahead of the naked POC in
  `structural_target()`** — user asked directly why the win rate stays low
  despite everything tested. Diagnosed first (finding 43): not direction (BUY
  28.1% vs SELL 28.5%, no bias), not "usually reverses immediately" (65% of
  losers move favourably first, winners endure real drawdown too) - it's target
  distance. Win rate falls from 44.8% (1.2-1.5R targets) to 7.8% (3R+), and the
  naked POC - `structural_target()`'s own top preference - wins the plain
  nearest-worthwhile sort on 63-66% of S2-VAR trades at 2.83R average, winning
  only ~22%, while the session's own POC/VAH/VAL (1.46R average) win 37-46% -
  close to or above the 37.7% breakeven line on its own. **Built**: opt-in
  `prefer_near` on `structural_target`/`choose_target`, `config.
  TARGET_PREFER_NEAR_LEVEL` (default False), `research.replay
  --target-prefer-near`. Caught and fixed one real bug while building it (a
  swapped tuple unpack in the "nothing clears the floor" fallback, caught by
  its own new unit test before any replay touched it). 9 new tests (348
  total). Finding 44. **Result: a near no-op, and that is itself informative.**
  n=936 - identical to baseline - win rate 28.2% (baseline 28.2%), naked POC
  still chosen 58.5% of the time (baseline 60.9%). Reordering candidates
  doesn't help because the near levels almost never clear `TARGET_MIN_R` in
  the first place, regardless of ranking - the real constraint is a shortage
  of viable near candidates, not their priority. Not shipped; the real next
  lever is something that changes whether near levels clear the floor at all
  (smaller `TARGET_MIN_R` for them specifically, or a stop-placement change),
  not candidate ordering. Finding 45.

---

Working order from here: 4, 6, 7 (prerequisite), 8, 11, 21 are all done or built.
7's ablation experiment and 5's wide-universe fetch are still running in the
background. 17 is unblocked now that 4 has landed, not yet started.
9 still depends on 5.

**Finding 31 is closed** — checked the combined filter (S2-VAR, slow excursion +
delta-supportive) and its `prior_shape=='b'` exclusion follow-up against window 3,
genuinely new data never used to derive either. Neither replicates: every point
estimate collapsed to no-separation, BUY flipped negative. Finding 36. No config
or gate was ever changed on this lead, so nothing to roll back.

Window 3's replay (`calibration/phase4_window3_structural`, 4,981 trades) finished
— `INSUFFICIENT_DAILY_HISTORY` down to 0.1%, confirming the `end_as_of` fix holds.
Window 2's re-replay also finished (`calibration/phase2_window2_structural_v2`,
5,830 trades vs the original 1,505 — the same bug had silently underpowered
finding 23's second leg too) and **strengthens finding 23 rather than changing
it**: window 2 now independently clears control (+0.1846, [+0.0566,+0.3126]) and
the repooled headline number tightens to +0.1674, [+0.0755,+0.2594]. Finding 37.
Item 7's confirm-5m ablation finished too — S2-VAR's edge does NOT hold at 5m
(+0.0669, no separation, vs +0.1674 BEATS CONTROL at the 15m default);
`CONFIRM_INTERVAL` stays at 15m with direct evidence behind it now. Finding 39.

Item 24's window-3 funding-fetch and recheck is done (finding 38). Item 21's
validation replay finished and was measured — clean negative, no gate built
(finding 40). Item 5's wide-universe fetch and replay finished and was
measured too — S1-LVN stays too rare and unreplicated to promote; item 9's
strategic call is no (finding 41). Item 6 also turned out to be answerable from
data already on disk (`phase5_weekly_poc` happened to populate its field too)
— another null, no gate built (finding 42).

**All four original background jobs are done, and every item that depended on
them has been measured and closed.** Nothing is running right now.

Remaining: 17 (unblocked, not started), pooling all three windows' funding-
opposed bucket (queued by finding 38, not started), item 22 (RSI/momentum —
awaiting the user's call on principle P1), and item 10 (deferred, large, own
sub-project). Of these, 17 is the only one that's cheap, unblocked, and ready
to start without further input.
