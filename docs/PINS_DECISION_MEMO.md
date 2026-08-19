# bubblegauge — PIN Decision Memo

**Prepared:** 2026-07-19 · **For:** operator review before any v4 (score-shifting) work
**Governance PR landed:** #17 (F-01/L-07 causal freeze; golden headline 52.43 unchanged; no version bump)

---

## Operator decisions of record (2026-07-19)

> This banner records the **authoritative operator verdicts** on each PIN. The
> analysis below (my original recommendations) is retained unchanged as the
> reasoning of record, but where a recommendation conflicts with a verdict here,
> **this banner governs.** No PIN is implemented. Every open item now routes
> through the companion **[PIN Evidence Pack](./PIN_EVIDENCE_PACK.md)** before any
> decision; nothing is a "freeze-scope completion" until proven so there.

| PIN | Operator verdict | Note |
|-----|------------------|------|
| **A. Governance clocks** | **HOLD** | Do **not** infer from current/PR/commit dates. The two concepts are distinct. Build an evidence section; if no documentary evidence exists, leave both `<PIN>` and request the two dates explicitly. |
| **B. MIN_RESOLVED_SCORE** | **HOLD — 0.50 NOT approved** | `coverage.min_resolved_weight = 0.50` is rejected. Requires a full sensitivity report (per/cross-block semantics, renormalization + two-thirds-gate interaction, historical impact) before any rule is pinned. |
| **C. NDX instrument policy** | **CONDITIONAL HOLD** | Do not freeze "QQQ everywhere" until an executable provider-resolution matrix proves every scoring path is already QQQ. If any path can currently resolve native NDX, freezing to QQQ is a **source substitution** → stop, require a documented drift gate. |
| **D. D3 non-positive OCF** | **HOLD** | "Saturate base to 1.0" not approved. Compare ≥5 alternatives with quantified historical effect. Evaluate **jointly with E** as one D3 decision. |
| **E. D3 minimum issuers** | **HOLD** | `min_issuers = 3` not approved. Sensitivity across 1–5 usable issuers and hard minimums 2/3/4, drop-vs-suppress, historical effect. Joint with D. |
| **F. ATH basis** | **DIRECTION APPROVED, SOURCE UNPINNED** | Intended object = **S&P 500 price index** ATH (fallback: unadjusted SPY price close, labelled ETF proxy). Dividend-adjusted total-return SPY is **not acceptable** as the ATH basis. Watermark must use genuinely long history, not the 900-row cache. Propose the concrete public source + fallback chain and demonstrate continuity before implementing; evaluate its freeze class explicitly. |
| **G. LPPLS_SEED** | **HOLD PENDING PROBE** | Run a package-level probe of lppls==0.6.24 (RNG source, worker inheritance, workers=1 control, isolated-subprocess reproducibility, before/after D4 effect). Reusing `20260711` is not automatically freeze-neutral. Return a repeated-run before/after table. |
| **H. S5 calendar anchoring** | **AUTHORIZE v4 DESIGN PHASE ONLY** | Proceed with the S5-v4 **design + test implementation** as the first score-shifting workstream. Do **not** activate in the production headline. Production S5 stays positional and keeps emitting `s5_lag="provisional_positional"`, `known_defects=[C-01,C-02,C-03]`. New methodology version + falsification-clock reset + dual reporting + regenerated golden only after final operator approval. |

**Next deliverable (this document's companion):** a documentation-only **PIN
Evidence Pack** with (1) governance-date evidence, (2) the NDX provider/instrument
matrix, (3) MIN_RESOLVED sensitivity, (4) the joint D3 OCF/min-issuer analysis,
(5) the ATH source proposal, (6) the LPPLS worker/RNG probe, (7) the S5-v4 design
spec + dual-report test plan. **No runtime change until that pack is reviewed.**

---

## Purpose

The freeze PR made `frozen_methodology.json` the single causal source of every
score-effective constant, but it deliberately did **not** decide any open policy
question — it pinned current behavior byte-for-byte. This memo enumerates every
held PIN so you can approve an exact constant/policy per item. **Nothing here is
implemented.** Each PIN, once decided, becomes a v4 change (version bump +
falsification-clock reset + regenerated golden), except the two governance clocks
(A), which are pure bookkeeping and land as a freeze-scope re-pin.

Ordering of v4 work is fixed by your instruction: **S5 calendar anchoring (H) is
the first candidate; S3 common-calendar returns and CAPE degraded-history MC
remain behind it.** The rest (B–G) are independent and can be scheduled freely.

Each PIN has the seven fields you asked for: current behavior · alternatives ·
rationale · estimated effect · backward-compat/freeze implications · recommendation
· exact constant needing approval.

---

## A. Governance clocks — `methodology_frozen_at`, `falsification_tracking_since`

*(the only two literal `<PIN>` sentinels in the artifact; not score-effective)*

1. **Current behavior.** `_meta.methodology_frozen_at` and
   `_meta.falsification_tracking_since` are `"<PIN>"`. The Phase-0 audit confirmed
   these dates were **never recorded** anywhere in the codebase — no field, no git
   tag, no changelog clock. `methodology_version` is `"v3-final"` (sourced from the
   remediation docs). The loader permits `<PIN>` only inside `_meta`; both are
   excluded from the completeness gate, so the runtime is unaffected.
2. **Alternatives.**
   (a) Set both to the date the v3-final methodology was declared frozen (a real
   historical date you supply).
   (b) Set `methodology_frozen_at` to the freeze-PR merge date (2026-07-19) and
   `falsification_tracking_since` to whenever live falsification tracking actually
   began.
   (c) Leave as `<PIN>` indefinitely (permitted, but the two clocks never anchor).
3. **Rationale.** The falsification framework's credibility depends on an honest
   "tracking since" anchor — post-hoc backdating would itself be a falsification
   failure. The freeze date documents when the methodology stopped moving.
4. **Estimated effect.** Zero on scores/coverage/bands/red flags. Display/audit
   metadata only.
5. **Backward-compat / freeze implications.** Resolving these edits the artifact
   bytes → the SHA-256 guard fails by design. Because the values are non-score-
   effective, this lands as a **freeze-scope re-pin** (update `EXPECTED_SHA256`,
   clocks now populated, **no version bump**), exactly the re-pin path the guard
   message describes.
6. **Recommendation.** Option (a)/(b) split: you supply the two true dates. I
   cannot invent them.
7. **Exact value needing approval.** Two ISO-8601 dates:
   `_meta.methodology_frozen_at = "YYYY-MM-DD"` and
   `_meta.falsification_tracking_since = "YYYY-MM-DD"`.

---

## B. `MIN_RESOLVED_SCORE` — minimum resolved coverage to publish a headline

1. **Current behavior.** There is **no global minimum**. The only hard refusal is
   `if not sub_s or not sub_d: raise RuntimeError("an entire block is empty …")`
   (`compute.py:1009`) — i.e. a number is withheld only when an *entire* S or D
   block has zero resolved indicators. Otherwise the per-block quality-weighted
   coverage gate (`COVERAGE_DROP_THRESHOLD = 1/3`) marks a block `degraded` and
   **suppresses its action band**, but the numeric headline is still published
   from whatever resolved (`compute.py:1022-1025`). So one resolved indicator per
   block (2 of 9 total) yields a published score with suppressed bands.
2. **Alternatives.**
   (a) Keep as-is (band suppression is the sole guard; headline always shown).
   (b) Add a global floor: withhold the numeric headline (show "insufficient
   coverage" instead of a number) unless total resolved quality-weight ≥ *X* of
   1.0 across both blocks.
   (c) Per-block floor: require ≥ *k* resolved indicators in **each** block before
   publishing any number.
3. **Rationale.** A headline computed from 2 of 9 indicators is arithmetically
   valid but epistemically thin; showing it with suppressed bands still invites the
   reader to over-read the number. A coverage floor makes "we don't have enough to
   score" a first-class output rather than a footnote.
4. **Estimated effect.** No change to the golden or to any well-covered run. Effect
   is confined to heavily-degraded live runs: option (b)/(c) would replace a
   low-confidence number with an explicit "insufficient" state. Bands are already
   suppressed in those runs, so band behavior is unchanged; red-flag override still
   fires independently (fail-dangerous is preserved).
5. **Backward-compat / freeze implications.** New constant + new output state.
   Golden unaffected (golden is fully covered). v4 change (version bump), but the
   golden headline stays 52.43.
6. **Recommendation.** Option (b) with a conservative floor — publish the number
   only when obtained quality-weight ≥ **0.50** of total nominal weight, else emit
   an explicit `insufficient_coverage` state. This is the least surprising and
   composes with the existing gate. **Open question for you:** the exact threshold.
7. **Exact constant needing approval.** `coverage.min_resolved_weight` (proposed
   `0.50`), plus a decision on whether the guard is global (b) or per-block (c).

---

## C. NDX instrument policy — proxy vs. native index

1. **Current behavior.** Every "NDX" consumer uses **QQQ** (the ETF) as the
   Nasdaq-100 proxy: S4 GSADF pulls QQQ monthly 1999+ and labels it
   "Nasdaq-100 via QQQ proxy" (`compute.py:478-481`); D4 LPPLS runs on the QQQ
   daily close series (`compute.py:601`); the dashboard feed reports
   `ndx_close.available = False` and never fabricates a native NDX print
   (`test_dashboard_feed.py:142`). A native-index path exists but is gated OFF:
   `twelve_data_indices = False` unless on the Twelve Data Grow tier
   (`config.py:37`, `.env.example:18`).
2. **Alternatives.**
   (a) Keep QQQ-proxy as the frozen policy (free-tier friendly, long history).
   (b) Prefer native `^NDX` when `twelve_data_indices` is enabled, else fall back
   to QQQ — a documented, tier-dependent policy.
   (c) Require native index (hard dependency) — rejected: breaks the free-tier
   deployment contract.
3. **Rationale.** QQQ tracks NDX closely but is total-return/expense-adjusted and
   has its own liquidity microstructure; for **explosiveness detection** (GSADF/
   LPPLS) the level path matters and the proxy is defensible, but the policy should
   be an explicit frozen decision, not an accident of which key is present.
4. **Estimated effect.** With (a): none. With (b): when native NDX is enabled, S4
   and D4 inputs change slightly (dividend-adjustment and level differences) → S4
   is CONTESTED-and-capped at 0.25 so its score barely moves; D4 confidence could
   shift within a band. No effect on the golden (fixed sub-scores). Potential small
   effect on live d4 sub-score and thus the D block.
5. **Backward-compat / freeze implications.** Option (b) makes the *input series*
   config-dependent, which is a methodology fork unless the artifact fixes one
   canonical basis. Cleanest: freeze **one** basis in the artifact
   (`indicators.market_index.basis = "qqq_proxy" | "native_ndx"`) and make the
   other a documented non-default. v4 change if the basis flips.
6. **Recommendation.** Option (a): **freeze QQQ-proxy as the canonical basis** and
   record it explicitly in the artifact, keeping native NDX as a documented,
   non-score-effective operational override. It preserves the free-tier contract
   and the entire measured history.
7. **Exact constant needing approval.** `indicators.market_index.basis =
   "qqq_proxy"` (canonical), with `twelve_data_indices` retained as an
   operational, non-frozen toggle.

---

## D. D3 non-positive-OCF treatment

1. **Current behavior.** An issuer whose TTM operating cash flow is ≤ 0 is
   **silently dropped** from the reading set: `if ocf is None or capex is None or
   ocf[0] <= 0: continue` (`edgar.py:154`). The D3 ratio is then the simple mean of
   `capex/OCF` over the *surviving* issuers (`compute.py:913`). So a hyperscaler
   burning cash — arguably the strongest FCF-quality alarm — is excluded, biasing
   D3 toward under-alarming.
2. **Alternatives.**
   (a) Keep dropping (current; conservative-but-blind).
   (b) Map non-positive OCF to the **maximum-alarm** ratio (treat OCF ≤ 0 as
   `capex/OCF → 1.0` after clip, i.e. base = 1.0 for that issuer) since negative
   OCF with any capex is unambiguously the worst FCF-quality state.
   (c) Keep the issuer but **cap** its ratio contribution at a defined ceiling
   (e.g. clip the per-issuer base to 1.0 and floor OCF at a small ε to avoid a
   divide-by-zero explosion) — same direction as (b) but bounded.
3. **Rationale.** `base = clip((r − 0.5)/0.5, 0, 1)` already saturates at r ≥ 1.0
   (capex ≥ OCF). A negative-OCF issuer has capex/OCF < 0 numerically, which would
   *under*-state alarm if naively averaged — hence the current drop. But dropping
   removes signal in exactly the tail the indicator exists to catch. Economically,
   OCF ≤ 0 while spending on capex is the definitive "buildout outrunning cash"
   condition.
4. **Estimated effect.** No effect in normal regimes (hyperscaler OCF is strongly
   positive today → no issuer is dropped, ratio unchanged, golden unaffected).
   Effect appears only if a hyperscaler's TTM OCF turns non-positive: (b)/(c) would
   push D3 up (more bearish) in precisely that scenario, and could contribute to
   the D-block and to the ≥3-of-4 override indirectly (D3 is not itself a red-flag
   tripwire).
5. **Backward-compat / freeze implications.** Changes indicator semantics in a
   tail case → v4. Golden stays 52.43 (golden issuers all have positive OCF). Needs
   a red→green test with a synthetic negative-OCF issuer.
6. **Recommendation.** Option (b), implemented as (c)'s bounded form: **retain the
   issuer and set its per-issuer base to 1.0 when OCF ≤ 0**, with a provenance note.
   It removes the under-alarming blind spot without letting a near-zero denominator
   explode the mean.
7. **Exact constant/policy needing approval.** `indicators.d3.nonpositive_ocf_base
   = 1.0` (per-issuer saturated base when TTM OCF ≤ 0), replacing the silent
   `continue`.

---

## E. D3 minimum issuer count

1. **Current behavior.** **No minimum.** D3 scores whenever ≥ 1 issuer resolves
   (`if raw.hyperscalers:` — `compute.py:912`); quality is `n / 5`
   (`compute.py:924`) and feeds the coverage gate, but a single issuer can drive the
   entire D3 value. The universe is the 5 CIKS (MSFT/AMZN/GOOGL/META/ORCL).
2. **Alternatives.**
   (a) Keep n ≥ 1 (quality-discounted).
   (b) Require n ≥ *k* (e.g. 3 of 5) to publish a D3 value; below that, **drop D3**
   and renormalize the D block.
   (c) Require a minimum **quality-weight** rather than a raw count.
3. **Rationale.** A cross-sectional "hyperscaler capex discipline" mean from one
   firm is not a sector signal; it is an idiosyncratic firm datum. A quorum makes
   D3 a genuine aggregate. The drop-and-renormalize machinery already exists, so
   below-quorum D3 degrades gracefully rather than misleading.
4. **Estimated effect.** None on the golden (golden resolves all 5). On live runs
   with EDGAR partially unreachable, (b) would drop D3 below quorum and shift D-block
   weight onto D1/D2/D4 — a coverage/gate effect, not a value distortion. Could push
   a block toward `degraded` if D3's 0.32 weight is lost (that alone exceeds the 1/3
   coverage-drop threshold → band suppression, which is the intended honest signal).
5. **Backward-compat / freeze implications.** New constant; changes when D3 exists.
   v4 change. Note the interaction: D3 weight (0.32) losing pushes the D block over
   `COVERAGE_DROP_THRESHOLD` by itself, so a quorum-drop reliably suppresses the D
   band — desirable but worth stating explicitly.
6. **Recommendation.** Option (b) with **k = 3** (majority of the 5). It matches the
   "n ≈ 4 reference-class" epistemics already documented in D3's header and yields a
   true cross-sectional read.
7. **Exact constant needing approval.** `indicators.d3.min_issuers = 3`.

---

## F. ATH source and basis

1. **Current behavior.** The "within 2% of all-time high" red-flag input is
   `raw.index_within_2pct_of_ath = closes[-1] >= 0.98 * max(closes)`
   (`compute.py:448`), where `closes` is the **SPY total-return-adjusted daily
   close** series returned by the price chain (Tiingo/Twelve Data/…), over whatever
   history that provider returns. So "ATH" = max of the adjusted SPY series in
   memory, `0.98` is the frozen proximity threshold (`red_flags.ath = 0.98`), and
   the basis is total-return-adjusted (dividends reinvested), not raw price.
2. **Alternatives.**
   (a) Keep: adjusted-close SPY, provider-window max, 0.98.
   (b) Use **raw (unadjusted) price** ATH — closer to the headline "index at record
   high" a reader pictures; a total-return series is monotone-biased upward so it
   hits new highs more readily than the price index.
   (c) Anchor ATH to the **native index** (S&P 500 price level) rather than the SPY
   ETF, and/or extend the lookback to a fixed multi-decade window instead of the
   provider's returned window.
3. **Rationale.** A total-return series reaching an ATH is a *weaker* statement than
   the price index doing so (reinvested dividends guarantee more frequent record
   highs), so the current basis makes the red flag slightly easier to trip — a
   conservative (fail-dangerous) direction, but arguably mislabeled as "ATH." The
   provider-window `max` is also not a true ATH if the window is short.
4. **Estimated effect.** This flag is one of the 4 red-flag tripwires feeding the
   ≥3-of-4 override → `max(score, 70)`. Switching to raw-price ATH (b) would make the
   flag trip *less* often (fewer records), marginally reducing override frequency in
   near-ATH regimes. No effect on the golden (golden sets red-flag inputs directly).
   Live effect is on the breadth-<50-near-ATH red flag and the override band.
5. **Backward-compat / freeze implications.** Changes a red-flag input definition →
   v4, and it interacts with the override (the strongest bearish signal), so it
   deserves explicit sign-off. Golden stays 52.43.
6. **Recommendation.** Option (a) is defensible as fail-dangerous, but if the label
   "ATH" should mean what a reader expects, **(b): raw-price SPY ATH over the full
   returned window**, documented as such. Given the override is safety-critical, I
   lean to keeping (a) unless you want the stricter literal-ATH semantics.
7. **Exact constants needing approval.** `red_flags.ath_basis =
   "total_return" | "price"` (currently implicitly total_return) and, if desired,
   `red_flags.ath_lookback_days` (currently unbounded = full provider window). The
   0.98 proximity threshold is not in question.

---

## G. `LPPLS_SEED` — determinism of the D4 fit

1. **Current behavior.** **No seed is set.** `model.mp_compute_nested_fits(...)`
   (`d4_lppls.py:210`) runs the lppls 0.6.24 optimizer from random initial guesses
   (up to `max_searches` per window) with no RNG control anywhere in the module or
   the isolated subprocess. Consequently the D4 confidence (fraction of qualified
   windows) is **not bit-reproducible** run-to-run; two recomputes on identical
   prices can yield slightly different d4 sub-scores.
3. **Rationale.** The whole freeze program exists so identical inputs give identical
   scores; D4 is the one remaining non-deterministic score-effective path. Seeding
   the optimizer (or numpy global RNG in the subprocess before the fit) restores
   reproducibility. Note: this does **not** touch the seeded Monte-Carlo (already
   `PCG64(20260711)`); it is a separate RNG inside the lppls dependency.
2. **Alternatives.**
   (a) Keep non-deterministic (accept run-to-run D4 jitter, document it).
   (b) Seed numpy's global RNG in the LPPLS subprocess immediately before
   `mp_compute_nested_fits` with a frozen constant.
   (c) Increase `max_searches` to shrink the variance without full determinism
   (partial mitigation, more CPU — bad on the Atom N2800).
4. **Estimated effect.** No effect on the golden (D4 sub-score is fixed in the
   fixture). On live runs, (b) makes D4 reproducible; the *level* of D4 could shift
   from today's unseeded expected value to the specific seed's realization — a
   one-time, documented relevel, then stable. Feeds only the D block (D4 weight 0.20);
   not a red-flag tripwire.
5. **Backward-compat / freeze implications.** Adds a constant and makes D4
   reproducible → v4. Caveat: lppls 0.6.24 uses Python `multiprocessing` workers;
   the seed must be set so each worker inherits it deterministically (verify the
   package respects a global seed across `workers > 1`, else pin `LPPLS_WORKERS = 1`
   for reproducibility — a CPU/latency trade on the Atom).
6. **Recommendation.** Option (b) with the existing house seed for consistency, and
   validate worker-inheritance empirically before committing; if workers break
   determinism, pin `workers = 1` in the reproducible path. **Requires a probe** of
   the 0.6.24 RNG/worker behavior (like the earlier lppls schema probe) before I can
   guarantee it.
7. **Exact constant needing approval.** `indicators.d4.lppls_seed = 20260711`
   (reuse the house seed), plus a decision on `LPPLS_WORKERS` if worker-inheritance
   is non-deterministic.

---

## H. S5 calendar anchoring — **FIRST v4 candidate (C-01 / C-02 / C-03)**

1. **Current behavior.** The S5 "t−2yr" credit-sentiment lookup is a **positional
   index, not calendar-anchored**: `t_minus_2_value` returns
   `history_bps[-lag_obs - 1]` with `lag_obs = LAG_OBS_2YR = 504` business days
   (`s5_credit.py:55-64`). Every computed S5 path already carries the runtime flag
   `S5_PROVISIONAL_LAG = {"s5_lag": "provisional_positional", "known_defects":
   ["C-01","C-02","C-03"]}` (`compute.py:90`). The defect: 504 positions ≠ exactly
   24 calendar months once holidays/gaps accrue, and the fallback tiers use
   different sampling frequencies (Fed EBP is **monthly**, the HY-OAS accrued
   history is **daily**) so "t−2" means a different horizon per tier.
2. **Alternatives.**
   (a) Keep positional 504 (current; flagged).
   (b) **Calendar-anchor**: select the observation on/just-before the date exactly
   24 months before the last observation, per series, regardless of row count.
   (c) Resample all S5 tiers to a common monthly grid, then take a fixed 24-month
   lag — uniform horizon across EBP / BAA-proxy / HY-OAS tiers.
3. **Rationale.** The LSSZ (2017) t−2 credit-sentiment horizon is a **structural
   2-year** economic lag; approximating it by row count drifts and is
   tier-inconsistent, undermining the very horizon the indicator claims. This is the
   highest-value correctness fix among the PINs, which is why you ranked it first.
4. **Estimated effect.** Directly moves the S5 sub-score on live runs (different
   anchor observation → different inverted-percentile). S5 weight is 0.13 in the S
   block, so the headline can move. **The golden fixture will need regeneration** —
   this is the first PIN that changes the 52.43 reference, so it must go through full
   v4 (version bump, falsification-clock reset, dual-report, new golden). Coverage/
   bands unaffected structurally; red flags unaffected (S5 is not a tripwire).
5. **Backward-compat / freeze implications.** **This is the v4 that breaks the
   golden.** It must land alone (not bundled), with a regenerated seeded-MC golden
   and a documented before/after. C-01/C-02/C-03 are closed together. S3
   common-calendar returns and CAPE degraded-history MC stay queued behind it, per
   your instruction.
6. **Recommendation.** Option (c): resample every S5 tier to a monthly grid and take
   a fixed 24-month calendar lag, giving one consistent structural horizon across all
   tiers. Pair with a red→green test asserting the anchor date is 24 months before
   the last observation on each tier.
7. **Exact constant/policy needing approval.** `indicators.s5.lag_basis =
   "calendar_months"` with `indicators.s5.lag_months = 24` (replacing
   `lag_obs_2yr_daily = 504`), and confirmation that S5 is the sole change in its v4
   release.

---

## Recommended decision order

| # | PIN | Type | Blocks golden? | Suggested sequencing |
|---|-----|------|----------------|----------------------|
| A | Governance clocks | freeze re-pin | no | **now** — you supply 2 dates; no version bump |
| H | S5 calendar anchoring | v4 | **yes** | **first v4** (your instruction) |
| D | D3 non-positive OCF | v4 | no | independent, low-risk |
| E | D3 min issuers | v4 | no | independent, low-risk |
| B | MIN_RESOLVED_SCORE | v4 | no | independent |
| F | ATH basis | v4 | no | safety-critical review (override) |
| G | LPPLS seed | v4 | no | needs a 0.6.24 RNG/worker probe first |
| C | NDX instrument policy | freeze/doc | no | freeze QQQ-proxy explicitly; low effort |

**S3 common-calendar returns** and **CAPE degraded-history MC** remain behind H and
are not detailed here per your "hold" instruction; they get their own memo once H is
decided.

---

*No PIN in this memo has been implemented. Awaiting your approval of the exact
constants/policies above before any v4 (score-shifting) work begins.*
