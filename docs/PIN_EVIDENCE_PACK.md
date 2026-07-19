# bubblegauge — PIN Evidence Pack

**Prepared:** 2026-07-19 · **Companion to:** [`PINS_DECISION_MEMO.md`](./PINS_DECISION_MEMO.md)
**Status:** documentation-only. **No PIN is implemented and no runtime constant is changed by this document.**

## Scope

The operator accepted governance PR #17 (F-01/L-07 causal freeze) and issued
per-PIN verdicts (recorded in the decision-memo banner). Every open item must be
evidenced before any decision. This pack supplies that evidence:

1. Evidence for the two governance dates (A)
2. Executable NDX provider/instrument matrix (C)
3. `MIN_RESOLVED_SCORE` sensitivity analysis (B)
4. Joint D3 non-positive-OCF / minimum-issuer analysis (D + E)
5. ATH source and basis proposal (F)
6. LPPLS worker/RNG probe (G)
7. S5-v4 design specification and dual-report test plan (H)

All file:line references are against the tree at PR #17 (`9139314`).

---

## 1. Governance-date evidence (PIN A) — `methodology_frozen_at`, `falsification_tracking_since`

### What was searched

| Evidence source | Method | Result |
|---|---|---|
| **Git tags** | `git tag`, `git for-each-ref refs/tags` | **No tags exist at all.** There is no `v3.4.0` tag, and no tag carries a "frozen" timestamp. |
| **v3.4.0 commit** | `git log e4e0de7` | `2026-07-15 22:27:25 UTC` — but its subject is *"dashboard feed … (methodology unchanged)"*; it is not a freeze marker. |
| **First "frozen"/"methodology" commits** | `git log -i --grep` | The methodology label lives in commit subjects, not in a declared freeze field. |
| **First scored-value = golden 52.43** | `git log -S'52.43' -- tests/` | Introduced in **`d51b7c0` `2026-07-12 21:43:19 UTC`** — "v3.3.0 stage 1: rescale-then-aggregate … (headline 22→~53)". |
| **Last methodology change** | README changelog (authoritative) | **v3.3.2** (`0d6c9a0` `2026-07-15 20:56:53 UTC`, tidy `4bf5c53` `21:01:11 UTC`) — "D4 METHOD CHANGE". **Every version from v3.4.0 onward is explicitly "methodology unchanged; golden fixture byte-identical".** |
| **Persisted snapshots** | `git ls-files`, `ls data/snapshots` | Only `data/snapshots/.gitkeep` (an empty placeholder, dir created `2026-07-11`). **No production snapshot is persisted in the repo or on disk.** |
| **Falsification records** | README §"Falsification criteria"; `/api/v1/meta/methodology` | Criteria are defined (3 rules), but v3.7.1 documents that *"an empty `falsification_outcomes` list is the expected state (**manual recording**)"*. **No outcome has ever been recorded; no start-of-tracking date is stored anywhere.** |

### Candidate anchors (NOT auto-selected — presented for the operator)

- **`methodology_frozen_at`** — the last scored-methodology change was **v3.3.2**
  (`2026-07-15 20:56–21:01 UTC`); everything after is "methodology unchanged".
  The strongest *documentary* candidate for "the methodology stopped moving" is
  therefore the v3.3.2 commit timestamp. A weaker alternative is the golden's
  origin (v3.3.0, `2026-07-12`), but D4 changed after that in v3.3.2, so v3.3.2
  is the true freeze point of the *complete* scored methodology.
- **`falsification_tracking_since`** — **no evidence exists.** Tracking is manual,
  the outcomes list is empty, and no snapshot/record marks a start date. This
  date cannot be recovered from the codebase.

### Conclusion & request

Per the operator's rule (*"Do not infer either date automatically from the current
date, PR date, or commit date … the two concepts are distinct … if no documentary
evidence exists, leave both as `<PIN>` and request the two dates explicitly"*):

- Both fields **remain `<PIN>`** in `frozen_methodology.json`.
- `methodology_frozen_at`: a defensible commit-timestamp anchor exists (**v3.3.2,
  2026-07-15**) but is **not** auto-applied; the operator must confirm it or supply
  the intended formal freeze date.
- `falsification_tracking_since`: **no documentary evidence.** The operator must
  supply the date on which prospective tracking is deemed to have begun.
- **Do not equate the two dates for convenience.** When supplied, re-pin the
  artifact hash **without** a service-version bump and **without** resetting either
  clock (freeze-scope re-pin only).

---

## 2. NDX provider/instrument matrix (PIN C)

### Method

`resolve_symbol(canonical, provider)` (`app/sources/prices.py:93`) is the single
place vendor spellings/proxies are decided. The matrix below is **executable** —
produced by calling `resolve_symbol('NDX', p)` for every provider under both
`TWELVE_DATA_INDICES` states, cross-checked against each fetcher's own code.

Provider chain order (`PROVIDER_ORDER`, `prices.py:495`):
`tiingo → twelvedata → alphavantage → yfinance → stooq → terminal cache`.

### Resolution matrix for canonical `NDX`

| Path | Vendor symbol | Index vs ETF | Adjusted? | Cache key | In default scoring path? |
|---|---|---|---|---|---|
| **Tiingo** | `QQQ` | ETF proxy | **adjusted** (`adjClose`) | `NDX` | yes (primary daily + `fetch_tiingo_monthly` long GSADF series) |
| **Twelve Data — Basic** | `QQQ` | ETF proxy | **unadjusted** | `NDX` | yes (index on free tier → 403 → proxy) |
| **Twelve Data — Grow (`TWELVE_DATA_INDICES=true`)** | `NDX` | **native index** | vendor basis | `NDX` | only when the Grow flag is set |
| **Alpha Vantage** | `QQQ` | ETF proxy | **unadjusted** | `NDX` | yes |
| **yfinance** | **`^NDX`** | **native index** | adjusted (`auto_adjust=True`) | `NDX` | **YES — 4th in the default chain** |
| **Stooq (`STOOQ_ENABLED=true`)** | **`^ndx`** | **native index** | Stooq basis | `NDX` | only when Stooq enabled (disabled by default) |
| **terminal cache** | last good series | inherits whatever was cached | inherits | `NDX` (≤ 900 rows) | yes (serves stale on total failure) |

**Empirically confirmed** (`resolve_symbol('NDX', …)`):
- default: tiingo/twelvedata/alphavantage/stooq → `QQQ` (proxy); **yfinance → `^NDX` (native)**.
- `TWELVE_DATA_INDICES=true`: twelvedata → `NDX` (native), yfinance → `^NDX` (native).

### Consumers of the canonical `NDX` series

| Consumer | Source call | What it receives |
|---|---|---|
| **S4 GSADF (primary)** | `price_src.fetch_tiingo_monthly("NDX")` (`compute.py:478`) | **always QQQ** (Tiingo monthly, adjusted) |
| **S4 GSADF (fallback)** | `legs.monthly_closes(ndx.value)` from `_closes("NDX")` (`compute.py:483`) | **chain output — can be native `^NDX`** |
| **D4 LPPLS** | `closes = [c for _,c in ndx.value]` from `_closes("NDX")` (`compute.py:601`) | **chain output — can be native `^NDX`** |
| **QQQ trend leg** | `_closes("QQQ")` | always QQQ (not an index proxy) |

### Two latent drifts surfaced

1. **`fetch_stooq` bypasses `resolve_symbol`.** It uses its own map
   `{"NDX":"^ndx","SPX":"^spx"}` (`prices.py:446`), so `resolve_symbol` *reports*
   `QQQ` for Stooq while the fetcher actually pulls the **native `^ndx`**. A
   reporting-vs-behavior inconsistency (latent; Stooq is off by default).
2. **Mixed adjustment basis** independent of the index/ETF question: Tiingo `QQQ`
   and yfinance `^NDX` are dividend-adjusted; Twelve Data / Alpha Vantage `QQQ` are
   **unadjusted**. So which provider serves determines both instrument *and*
   adjustment.

### Conclusion (drives PIN C)

**A currently-executable scoring path can resolve canonical NDX to the native
index.** yfinance sits in the **default** `PROVIDER_ORDER` and returns `^NDX`; the
D4 LPPLS consumer (and the S4 GSADF fallback) use the chain output directly. Under
the operator's rule — *"if any path can currently use native NDX, changing it to
QQQ is a source substitution … stop and require the documented drift gate before
changing the policy"* — **freezing "QQQ everywhere" is NOT a freeze-scope
completion.** It is a source substitution and must go through a documented drift
gate (before/after series comparison on S4 and D4, provenance labelling, and an
explicit freeze-class decision), not a re-pin.

**Recommended drift-gate design (for approval, not yet built):**
- Pin one canonical basis in the artifact: `indicators.market_index.basis =
  "qqq_proxy"` (adjusted ETF), and make `resolve_symbol` return **QQQ for every
  provider** for the two index canonicals — including fixing the Stooq-fetcher
  bypass — so the executable behavior matches the frozen policy on all paths.
- Emit provenance `index_basis` on every snapshot so a native-index reading can
  never silently enter a scored series again.
- Land it as a **v4 drift change** with a documented before/after on S4/D4, not as
  a re-pin — because today's live behavior is not uniformly QQQ.

---

## 3. `MIN_RESOLVED_SCORE` sensitivity (PIN B)

### Current mechanics (verified)

- There is **no global minimum-resolved rule.** The only hard refusal:
  `if not sub_s or not sub_d: raise RuntimeError("an entire block is empty …")`
  (`compute.py:1009`) — a number is withheld only if an *entire* S or D block has
  zero resolved indicators.
- The **per-block coverage gate** (`_coverage_gate`, `compute.py:155`):
  `frac = obtained / total`; `degraded = frac < (1 − COVERAGE_DROP_THRESHOLD)` with
  `COVERAGE_DROP_THRESHOLD = 1/3` → **degraded when a block's fresh,
  quality-weighted coverage falls below 2/3 of nominal.** A degraded block's action
  band is suppressed, **but the numeric headline is still published.**
- `obtained` counts an indicator only if `not dropped and stale is False`, weighted
  by `w × quality`. Proxy substitution does not reduce quality.

### Nominal weights (artifact = REGISTRY, confirmed equal)

- **Block S:** s1 0.33 · s2 0.27 · s3 0.20 · s4 0.07 · s5 0.13 (Σ 1.00)
- **Block D:** d1 0.35 · d2 0.13 · d3 0.32 · d4 0.20 (Σ 1.00)

### Single-indicator-loss → block coverage (quality = 1 for survivors)

Two-thirds gate trips when remaining coverage `< 0.6667`.

| Block | Drop this indicator | Remaining coverage | Degraded (band suppressed)? |
|---|---|---|---|
| S | s1 (0.33) | 0.67 | **no** (0.67 ≥ 0.6667, razor-thin) |
| S | s2 (0.27) | 0.73 | no |
| S | s3 (0.20) | 0.80 | no |
| S | s4 (0.07) | 0.93 | no |
| S | s5 (0.13) | 0.87 | no |
| S | s1 + any second | ≤ 0.60 | **yes** |
| D | d1 (0.35) | 0.65 | **yes** |
| D | d2 (0.13) | 0.87 | no |
| D | d3 (0.32) | 0.68 | no (razor-thin) |
| D | d4 (0.20) | 0.80 | no |
| D | d1 + d2 (0.48) | 0.52 | **yes** |
| D | d3 + d4 (0.52) | 0.48 | **yes** |

**Observations:** losing d1 alone already degrades block D; losing s1 alone leaves
S at exactly 0.67 (one rounding step above the gate). So the existing gate is a
per-block, weight-based rule with sharp edges around the top-weighted indicators.

### Candidate global-minimum rules (illustrative — none recommended)

Let `Ws`, `Wd` = obtained quality-weight fraction per block; a *global* minimum
adds a headline-availability test on top of the existing per-block band gate.

| Rule variant | Semantics | Behavior when one block full, other sparse | Interaction with the 2/3 gate |
|---|---|---|---|
| **(i) per-block floor** `min(Ws,Wd) ≥ θ` | withhold headline unless **both** blocks clear θ | a sparse block withholds the whole headline even if the other is complete | if θ = 2/3 it **coincides** with the existing gate but escalates "band suppressed" → "no headline" |
| **(ii) combined floor** `(Ws+Wd)/2 ≥ θ` | withhold on the **average** | a full block can mask a very sparse one (avg stays high) | weaker than the per-block gate; can publish a headline for a block the gate degraded |
| **(iii) total-weight floor** over all 9 nominal weights | withhold unless global obtained-weight ≥ θ | same masking risk as (ii) | orthogonal to per-block; needs care to not contradict a degraded block |

**Illustrative θ levels** (no recommendation; showing behavior, not picking a
convenient one):

| θ | (i) per-block effect | Note |
|---|---|---|
| 0.50 | withholds only in already-heavily-degraded runs | **below** the 2/3 band gate, so a headline could be "shown but band-suppressed" between 0.50–0.667 — a **new intermediate state** whose semantics must be defined |
| 0.667 | coincides with the band gate threshold | escalates existing band-suppression to headline-withholding; cleanest to reason about but a **behavior change** (degraded runs currently still show a number) |
| 0.75 | withholds on a single top-weight loss (d1, or s1+one) | most conservative; materially reduces headline availability |

### Historical-impact caveat

**No persisted snapshots exist** (`data/snapshots` holds only `.gitkeep`), so the
"number of historical snapshots affected" **cannot be computed from stored data.**
Any historical-frequency figure would have to come from a backfill replay, which is
out of scope for this documentation-only pack. This gap is itself evidence: a
coverage-availability rule cannot be calibrated against history until snapshot
persistence is running.

### Conclusion (drives PIN B)

`0.50` is **not** substantiated as a safe default: between 0.50 and 0.667 it creates
an undefined intermediate state (headline shown, band suppressed). The only
threshold that composes cleanly with the existing machinery is **θ = 2/3**, and even
that is a behavior change (headline-withholding vs today's band-suppression). **No
value is pinned.** A decision needs: (a) chosen semantics (i/ii/iii), (b) a defined
state name for withheld headlines, and (c) a backfill replay once snapshots persist.

---

## 4. Joint D3 non-positive-OCF / minimum-issuer analysis (PINs D + E)

Treated jointly, as instructed — both govern *when and how* D3 exists.

### Current mechanics (verified)

- Per issuer, `capex_ocf_ttm = capex / ocf` (`edgar.py:162`). **An issuer with
  `ocf ≤ 0` is silently dropped:** `if ocf is None or capex is None or ocf[0] <= 0:
  continue` (`edgar.py:154`).
- D3 value = **simple mean** of surviving issuers' ratios:
  `ratio = sum(h.capex_ocf_ttm)/len(h)` (`compute.py:913`).
- `sub_score(r, gate) = clip((r−0.5)/0.5, 0, 1)`, capped at `GATE_OFF_CAP = 0.30`
  unless the gate fires (`d3_hyperscaler_fcf.py:77`).
- **No minimum issuer count:** D3 scores whenever ≥ 1 issuer resolves
  (`if raw.hyperscalers:`, `compute.py:912`); `quality = n/5` (`compute.py:924`)
  feeds the coverage gate. Universe = 5 CIKS (MSFT/AMZN/GOOGL/META/ORCL).

### The two coupled failure modes

- **Non-positive OCF (D):** a cash-burning hyperscaler — the strongest FCF-quality
  alarm — is *removed* from the mean, biasing D3 **downward** (less alarming) in
  exactly the tail the indicator exists to catch. Naively including a negative
  denominator would instead make `capex/OCF < 0`, which the clip floors to 0 — also
  wrong (reads as "healthy").
- **Minimum issuers (E):** a single resolved issuer can define the whole
  cross-sectional signal (quality-discounted but published).

### D — alternatives (quantified effect described; no historical replay available)

| # | Treatment of `OCF ≤ 0` | Effect on D3 value | Effect on D-block coverage | Freeze class |
|---|---|---|---|---|
| 1 | **Drop issuer, reduce quality** (current is drop; add explicit quality debit) | mean unchanged among survivors; under-alarms in the tail | quality falls (n_survivors/5) → may cross 2/3 gate | v4 (quality-rule change) |
| 2 | **Keep in coverage count, ratio marked unavailable** | value unchanged; coverage no longer silently shrinks | coverage counts the issuer as present-but-unscored (needs a defined weight) | v4 |
| 3 | **Separate DISTRESS state for `OCF ≤ 0`** | D3 emits a categorical distress flag alongside the mean | orthogonal; most transparent | v4 (new state) |
| 4 | **Saturated stress value (per-issuer base = 1.0)** | pushes D3 **up** (more bearish) precisely in the burn case | survivor count restored; quality full | v4 (new nonlinear map) — *this was my memo rec; operator did not approve pending this comparison* |
| 5 | **Drop D3 entirely if any issuer has `OCF ≤ 0`** | D3 absent → D-block renormalizes onto d1/d2/d4 | D3's 0.32 weight lost → **exceeds 1/3 → block degraded, band suppressed** | v4 |

**Economic note.** `OCF ≤ 0` with positive capex is unambiguously the worst
FCF-quality state, so options 3/4 (surface it as stress) are economically faithful;
option 5 (drop) is the most conservative but throws away the signal; option 1
(current) *under*-alarms. Option 4's risk is that a near-zero OCF makes the raw
ratio explode, which is why a *saturated* (bounded) mapping — not the raw ratio — is
the only safe way to "include" it.

### E — alternatives

| Minimum | Below-minimum behavior | Effect |
|---|---|---|
| **1 (current)** | n/a — always scores | one firm can drive D3; quality = n/5 is the only guard |
| **2** | drop D3, renormalize D | tolerates 2 SEC outages |
| **3** | drop D3, renormalize D | majority of 5; matches the "n≈4 reference class" epistemics in D3's own header |
| **4** | drop D3, renormalize D | strictest; D3 present only near-complete |
| **suppress vs drop** | *suppress* keeps D3's weight but bands off; *drop* renormalizes | drop is consistent with the existing "drop-and-renormalize" contract |

**Coupling to note:** D3 weight is 0.32; losing it alone leaves D at 0.68 (just
above the 2/3 gate). But if a min-issuer **drop** coincides with any *other* D-block
loss, D falls below 2/3 → band suppressed. So E's "drop below minimum" reliably
interacts with the coverage gate — desirable, but must be stated.

### Historical-frequency caveat

As in §3, **no persisted snapshots** → the historical frequency of `OCF ≤ 0` events
and sub-minimum-issuer runs cannot be measured from stored data. Present hyperscaler
OCF is strongly positive, so **today** no issuer is dropped and the golden is
unaffected; the decision is about tail behavior.

### Conclusion (drives D + E)

No mapping is pinned. Recommended *joint* decision to bring back for approval:
**D = option 3 or 4** (surface `OCF ≤ 0` as an explicit bounded stress/distress
signal rather than dropping it) **paired with E = min_issuers 3, drop-and-
renormalize below it.** Both are v4; both leave the golden 52.43 unchanged (golden
issuers have positive OCF and full basket). Final numbers await operator approval
and, ideally, a backfill replay once snapshots persist.

---

## 5. ATH source and basis proposal (PIN F)

### Current mechanics (verified)

- The red-flag input is
  `raw.index_within_2pct_of_ath = closes[-1] >= 0.98 * max(closes)`
  (`compute.py:448`), where `closes = raw.spy_daily_closes` from
  `_closes("SPY") → get_daily_closes("SPY")`.
- `get_daily_closes` serves the **provider cache capped at `CACHE_MAX_ROWS = 900`
  rows** (`prices.py:62`, `481`) — ≈ 3.5 trading years. So "all-time high" is really
  **"max of the last ≤900 adjusted SPY closes."**
- Basis is **dividend-adjusted / total-return-ish**: Tiingo `adjClose` and yfinance
  `auto_adjust=True` are dividend-adjusted; a total-return series makes new highs
  more readily than the price index, so the flag trips slightly **more** easily than
  a true price-index ATH would (fail-dangerous, but mislabelled).
- This flag is one of the **4 red-flag tripwires** feeding the ≥3-of-4 override
  (`max(score, 70)`), so its definition is safety-critical.

### Operator-approved direction

- **Preferred object:** S&P 500 **price index** all-time high.
- **Acceptable fallback:** **unadjusted SPY price close**, explicitly labelled an
  ETF proxy.
- **Not acceptable:** dividend-adjusted SPY total-return series presented as the
  S&P 500 price ATH.
- Watermark must use **genuinely long history**, not the 900-row cache.
- Provider, instrument, adjustment basis, history start, and watermark-update rule
  must be **persisted**.

### Proposed source & fallback chain (for approval, not built)

The robust, free-tier-compatible design is a **persisted monotonic watermark**
rather than a live `max()` over a short window:

1. **Seed** a persisted `sp500_price_ath` record once, from a genuinely long
   S&P 500 **price-index** history. Concrete public options, in preference order:
   - **Stooq `^spx`** daily (multi-decade price history; already integrated behind
     the PoW path in `app/sources/stooq.py`) — native price index, unadjusted.
   - **A committed static historical watermark** (the known S&P 500 price-index ATH
     as of the freeze date) shipped in the artifact/DB as the floor — zero network
     dependency, fully reproducible.
   - **FRED `SP500`** is **rejected as the seed**: it is the price index but FRED
     only serves a **10-year** rolling window, so it cannot establish a true ATH on
     its own (documented here so it is not proposed later).
2. **Update** the watermark **upward only** from the live price feed, using an
   **unadjusted** price close (fallback: unadjusted SPY labelled ETF proxy). The
   watermark never decreases and never depends on the 900-row cache window.
3. **Persist** `{source, instrument, adjustment_basis, history_start,
   last_update, watermark}` and emit it in provenance on every snapshot.
4. **Red flag** becomes `latest_unadjusted_price >= 0.98 * sp500_price_ath`.

### Continuity demonstration (required before implementation)

Because switching from "adjusted-SPY 900-row window max" to "long-history
price-index monotonic watermark" **can change red-flag state** (and therefore the
override), the change must ship with:
- a back-comparison of the old vs new `within_2pct_of_ath` boolean over a replayed
  history (needs snapshot persistence or a historical price pull), and
- an explicit **freeze-class** ruling: this is a **v4 red-flag-definition change**
  (not a re-pin), with the falsification implications of altering an override input
  documented.

### Conclusion (drives F)

Direction approved; **source still unpinned.** Recommended: **persisted monotonic
price-index watermark**, seeded from Stooq `^spx` or a committed static ATH,
updated upward from unadjusted price, with full provenance. Do **not** implement
until the concrete seed source is chosen and continuity is demonstrated; treat as a
v4 change affecting an override input.

---

## 6. LPPLS worker / RNG probe (PIN G)

### Static findings (package inspection — lppls==0.6.24)

- **RNG source is Python's stdlib `random`, not numpy.** `lppls/lppls.py:8`
  `import random`; the non-linear initial guesses are
  `non_lin_vals = [random.uniform(a[0], a[1]) for a in init_limits]`
  (`lppls.py:207`), inside the `max_searches` retry loop (`lppls.py:190–225`).
  **Consequence: seeding `np.random.seed()` does NOT control the LPPLS fit** — only
  `random.seed()` does.
- **Parallelism is `multiprocessing.Pool(processes=workers)`** (`lppls.py:3`, `656`)
  driving `pool.imap` over per-window fit tasks (`lppls.py:656–658`). The project
  runs this via `mp_compute_nested_fits(workers=LPPLS_WORKERS, …)`
  (`d4_lppls.py:210`) inside an isolated subprocess (`compute_confidence_isolated`).
- On Linux (default `fork` start method) forked workers inherit the parent's
  `random` state at fork time; Python does **not** auto-reseed `random` per worker.
  Whether that yields reproducible *or* pathologically correlated draws across
  `workers > 1` is exactly what the empirical probe settles.

### Empirical probe

**Setup** (fast, determinism-characterizing — not the production scan; the
determinism *mechanism* is config-independent): synthetic 220-point log-price
series, the project's real `FILTER_CONDITIONS = {m_min:0, m_max:1, w_min:4,
w_max:25}`, `window_size=120`, `smallest_window=60`, `inner_increment=5`,
`max_searches=8`, via `mp_compute_nested_fits`. Each cell run twice; before "seeded"
runs, **both** `random.seed(20260711)` and `np.random.seed(20260711)` were set in
the parent. Fingerprint = (`n_fits`, `n_qualified`, per-window `is_qualified` tuple,
first-8 `b` values). Full log: `scratchpad/lppls_probe.py` output.

| Run | n_fits | n_qual | qualified tuple | first `b` values (rounded) |
|---|---|---|---|---|
| unseeded workers=1 A | 12 | 0 | (0,0) | −0.001921, −0.00216, 0.0, 0.0 … |
| unseeded workers=1 B | 12 | 0 | (0,0,0) | −0.001921, −0.00216, −0.002128, 0.0 … |
| **seeded** workers=1 A | 12 | 0 | (0,0) | −0.001921, 0.0, 337.298, −0.002024 … |
| **seeded** workers=1 B | 12 | 0 | (0,0,0) | −0.001921, −0.00216, −0.002128, 0.0 … |
| unseeded workers=2 A | 12 | 0 | (0,0,0) | −0.001921, −0.00216, −0.002128, 326.02 … |
| unseeded workers=2 B | 12 | 0 | (0,0,0,0) | −0.001921, −0.00216, −0.002128, −0.002024 … |
| **seeded** workers=2 A | 12 | 0 | (0,0) | 0.0, −0.00216, −0.002128, 0.0 … |
| **seeded** workers=2 B | 12 | 0 | (0,) | −0.001921, 0.0, 0.0, 0.0 … |

**Reproducibility verdicts** (run A vs run B identical on n_fits/n_qual/qualified):

| Condition | Reproducible? |
|---|---|
| unseeded, workers=1 | **No** |
| **seeded (parent `random`+`numpy`), workers=1** | **No** |
| unseeded, workers=2 | No |
| **seeded, workers=2** | **No** |

Note the *set* of negative-`b` fits itself changes run-to-run (qualified-tuple
length varies 1–4), confirming fit-level — not just tie-breaking — nondeterminism.
(`n_qual` is 0 here only because the synthetic series has no qualified positive-`b`
window; the differing `b` fingerprints are the determinism signal.)

### Conclusion (drives G)

**Seeding `20260711` in the parent process does NOT make LPPLS reproducible — not
even at `workers=1`.** The RNG source is stdlib `random` (§static findings), and the
`random.uniform` draws execute **inside `multiprocessing.Pool` worker processes**,
whose RNG state is not governed by a `random.seed()` call in the parent. So the
simple fix proposed in the decision memo (add `LPPLS_SEED = 20260711` and seed
before the fit) is **empirically insufficient**.

Achieving D4 reproducibility requires one of:
- a **`Pool` initializer** that deterministically seeds each worker's `random`
  (e.g. `random.seed(base_seed + worker_index)`), which **lppls 0.6.24 does not
  expose** through `mp_compute_nested_fits` — it would need a wrapper around, or a
  vendored patch of, the package; and even then a fixed `workers` count and fixed
  task→worker assignment are required for bit-stability; or
- running the fits **in-process without a Pool** (the package's non-`mp`
  `compute_nested_fits` path) with a single parent seed — slower, and must be
  re-probed to confirm it is actually deterministic; or
- **accepting non-determinism** and documenting D4 as a distribution rather than a
  point (e.g. report a stable summary over N seeds).

**Therefore PIN G is not a freeze-neutral constant addition.** Reusing the house
number does not make it so — the mechanism does not work as assumed. Recommendation:
do **not** add a bare `lppls_seed` constant; if reproducibility is required, scope a
proper seeded-`Pool`-initializer wrapper (fixed `workers`, re-probed for
bit-stability) as its own v4 workstream, or formally adopt and document D4
non-determinism. Bring the chosen path back for approval before any implementation.

---

## 7. S5-v4 design specification and dual-report test plan (PIN H)

**Authorization scope:** design + test implementation only. Production S5 is
unchanged and keeps emitting `s5_lag="provisional_positional"`,
`known_defects=["C-01","C-02","C-03"]`. No headline activation until final operator
approval.

### Current defect (verified)

`t_minus_2_value(history_bps, lag_obs=LAG_OBS_2YR)` returns
`history_bps[-lag_obs-1]` with `LAG_OBS_2YR = 504` **positional** business days
(`s5_credit.py:55–64`). 504 rows ≠ exactly 24 calendar months once holidays/gaps
accrue, and the fallback tiers sample at different frequencies (Fed EBP **monthly**,
BAA-DGS10 **monthly**, accrued HY-OAS **daily**) so "t−2" means a different horizon
per tier. C-01/C-02/C-03.

### Design specification (S5 v4)

| Element | Specification |
|---|---|
| **Evaluation date / month** | the reference date of the newest usable S5 observation (month-end for monthly tiers) |
| **Target period** | exactly **24 calendar months** before the evaluation month (not 504 rows) |
| **EBP selection (monthly)** | the EBP observation for the target month; if absent, nearest prior month within a bounded window |
| **BAA-DGS10 selection (monthly)** | same calendar rule on the gap-free monthly grid already built for the proxy |
| **HY-OAS fallback (daily)** | the daily observation on/just-before the target **calendar** date (not −504 rows) |
| **Missing target period** | if no observation within the bounded look-back window → `INSUFFICIENT_HISTORY` (do not silently use the nearest row) |
| **Minimum history** | require ≥ target-lag + window before S5 is scored at t−2; else degrade/drop with provenance |
| **`INSUFFICIENT_HISTORY` behavior** | S5 drops (coverage-gated), never a fabricated percentile |
| **Empirical-CDF tie convention** | fixed, documented (e.g. `mean` ranks) so ties are reproducible |
| **Vintage policy** | point-in-time vs revised — **must be pinned**; backtests must avoid look-ahead (no future revisions leaking into a historical rank) |
| **Ranking-history endpoint** | the percentile is computed over history **up to the evaluation date**, not the full modern series (no future information in historical backtests) |
| **Source-specific quality** | EBP 1.0 / BAA-proxy 0.5 / HY-OAS 0.3 fidelity tiers retained |
| **Dual-report payload** | every snapshot emits BOTH `s5_positional` (current production) and `s5_calendar` (v4 candidate) with per-source target date + value, so the two run side-by-side before cut-over |

### Dual-report comparison (to be produced by the test harness)

For each evaluation date, emit and diff:

- current positional S5 sub-score vs calendar-anchored S5 sub-score,
- per-source target date and value (positional vs calendar),
- resulting **S-block** difference,
- **deterministic headline** difference,
- **MC median + quantile** difference,
- historical **action-band** changes.

### Test plan (RED-first, no production activation)

1. **Anchor correctness** — for a synthetic gapped daily HY-OAS series, assert the
   selected observation is on/just-before the date exactly 24 months prior, not
   `−504` rows.
2. **Per-tier consistency** — EBP/BAA/HY-OAS all resolve to the *same* 24-month
   calendar horizon.
3. **`INSUFFICIENT_HISTORY`** — a series shorter than lag+window drops S5 rather
   than fabricating a percentile.
4. **No look-ahead** — the percentile at date *t* uses only history ≤ *t*.
5. **Dual-report parity** — the payload carries both `s5_positional` and
   `s5_calendar`; production headline still consumes **positional** and still emits
   the provisional-lag flag.
6. **Golden guard** — with production consuming positional S5, the golden headline
   stays **52.43** (the v4 path is inert until activation).

### Cut-over (later, gated on operator approval)

New `methodology_version`, documented falsification-clock reset, dual reporting in
production, and a **regenerated golden fixture** — only after final operator
approval. S3 common-calendar returns and CAPE degraded-history MC remain queued
behind H.

---

*End of evidence pack. Nothing herein has been implemented. Awaiting operator
review before any runtime change.*
