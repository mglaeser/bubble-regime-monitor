# bubblegauge — PIN Evidence Pack 2 (post-decision workstreams)

**Prepared:** 2026-07-23 · **Predecessors:** [`PINS_DECISION_MEMO.md`](./PINS_DECISION_MEMO.md) (operator verdicts of record) · [`PIN_EVIDENCE_PACK.md`](./PIN_EVIDENCE_PACK.md)
**Status:** analysis + records for the operator's authorized sequence. The only runtime changes in this cycle are the three the operator authorized: the **PIN-A metadata re-pin**, the **PIN-H inactive shadow**, and the **PIN-F provenance label**. Everything else here is documentation. **No score-shifting change is activated.**

Environment constraint disclosed up front: this analysis container's network policy
denies CONNECT to every market-data host tested (`stooq.com`, `query1.finance.yahoo.com`,
`fred.stlouisfed.org`, `data.sec.gov` — gateway 403; no `.env`/API keys; no Rscript).
All *data-dependent* comparisons below are therefore specified as runnable harnesses
for the production host and marked **BLOCKED(here)** rather than fabricated.

---

## A. Governance clocks — EXECUTED (metadata-only re-pin)

Per the operator's approval: `_meta.methodology_frozen_at = "2026-07-15"` (the
v3.3.2 commits `0d6c9a0`/`4bf5c53` — the last score-effective methodology change).
`falsification_tracking_since` **remains `<PIN>`** and was not backdated.

Invariance evidence (before → after):

| Quantity | Before | After |
|---|---|---|
| artifact SHA-256 | `d9080427…f7e307b9` | `0bfb716f…a093415f` |
| **score-effective tree SHA-256** (artifact minus `_meta`) | `be1cd89bfc04f0c50f0035a00df946c96eff50843eaa1612693448649b4c2482` | **identical** |
| deterministic golden score (bit-level) | 52.42817253528893 | **identical** |
| MC seed / samples | 20260711 / 100000 | **identical** |
| full suite | 314 passed | 335 passed (314 + new shadow/label tests) |
| methodology_version / service_version | v3-final / 3.7.8 | **unchanged** |

Also updated: `_meta._note` (same `_meta` block, non-score-effective prose) so the
artifact does not carry a stale claim that both clocks are unrecorded — disclosed
here per the "change only `methodology_frozen_at`" instruction.

**Future rule for the second clock** (operator's, recorded): before proposing
`falsification_tracking_since`, produce the first persisted production snapshot,
its timestamp, the methodology hash attached to it, evidence of prospective
outcome recording, and evidence that history cannot be silently rewritten.
`persist_snapshot` already stamps `service_version` per snapshot; attaching the
**frozen-artifact SHA-256** per snapshot is the missing piece and is proposed as
part of the B/D-E replay infrastructure below (not yet implemented).

---

## H. S5 calendar anchoring — EXECUTED (inactive shadow only)

Implemented per authorization: `app/indicators/s5_calendar.py` +
`s5_dual_report` in the s5 payload. Production S5 is untouched (positional, all
tiers, still emitting `s5_lag="provisional_positional"`,
`known_defects=["C-01","C-02","C-03"]`). The candidate is
`included_in_score=false` everywhere, computed under a **labelled, unapproved**
parameter set (`parameters_approved:false` in every payload), with the six S5
constants held as explicit unresolved PINs:

`S5_MAX_PRIOR_DISTANCE_MONTHLY · S5_MAX_PRIOR_DISTANCE_DAILY ·
S5_MIN_HISTORY_MONTHS · S5_EMPIRICAL_CDF_TIE_METHOD · S5_EBP_VINTAGE_POLICY ·
S5_HISTORICAL_REVISION_POLICY`

### Mini decision table (executable; synthetic controlled series)

**Monthly tolerance** (`S5_MAX_PRIOR_DISTANCE_MONTHLY`), evaluation 2025-12-31,
target month 2023-12:

| Scenario | exact month only | 1 prior month | 2 prior months |
|---|---|---|---|
| target month missing | TARGET_GAP | PRIOR_WITHIN_TOLERANCE (2023-11-30) | PRIOR_WITHIN_TOLERANCE (2023-11-30) |
| 3 consecutive months missing | TARGET_GAP | TARGET_GAP | TARGET_GAP |

**Minimum history** (`S5_MIN_HISTORY_MONTHS`) on 30/40/70-month histories:

| History | min 25 | min 36 | min 60 |
|---|---|---|---|
| 30 months | EXACT, sub 0.8000 | INSUFFICIENT_HISTORY | INSUFFICIENT_HISTORY |
| 40 months | EXACT, sub 0.6000 | EXACT, sub 0.6000 | INSUFFICIENT_HISTORY |
| 70 months | EXACT, sub 0.3429 | EXACT, sub 0.3429 | EXACT, sub 0.3429 |

**Daily tolerance** (`S5_MAX_PRIOR_DISTANCE_DAILY`), target 2023-12-31 (a Sunday):

| Scenario | 3 days | 5 days | 10 days |
|---|---|---|---|
| ordinary weekend | PRIOR_WITHIN_TOLERANCE (dist 2) | same | same |
| 9-calendar-day publication outage | TARGET_GAP | TARGET_GAP | PRIOR_WITHIN_TOLERANCE (dist 9) |

**CDF tie convention** (`S5_EMPIRICAL_CDF_TIE_METHOD`), flat-tail series
(76% of observations ≤ the t-2 value, 48% strictly below):

| Convention | percentile(value) | candidate sub-score |
|---|---|---|
| `weak_le` (== production's `<=`) | 0.7619 | 0.2381 |
| `mid_rank` | 0.3810 | **0.6190** |

**Readings.** The tie convention is the single most score-material of the six
PINs on tie-heavy series (Δsub up to ~0.38 in the constructed case); `weak_le`
is the continuity choice (bit-compatible with production's percentile).
The daily tolerance discriminates only on genuine outages, not weekends; the
monthly tolerance decides whether a single missing publication month degrades
S5 or slides one month. **None of these is approved; activation requires the
operator to pin all six.** The vintage/revision policies cannot be exercised
without archived vintages — dedupe-keep-last is implemented as the labelled
mechanical default only.

### Test coverage delivered (RED-first, 18 tests)

All ten operator cases: leap-year month-end transitions; Feb 28/29 evaluation
dates; duplicated observations; missing target month; consecutive missing
months; future observations excluded from selection AND the CDF; revised
same-period observations (mechanical latest-revised default); tier-fallback
labels (EBP/BAA/HY-OAS); candidate-unavailable-while-production-available;
deterministic dual-report serialization — plus zero-influence, the
`weak_le`==production identity, and grid-divergence proofs.

### Activation gate (recorded, unchanged)

All of: final approval of the six constants; vintage policy; historical
dual-report analysis; score/band drift report; regenerated deterministic + MC
goldens; new methodology version; **explicitly supplied**
`falsification_tracking_since`; rollback plan retaining positional S5; a
comparison period of ≥60 trading days **with sufficient observations across all
three source tiers** (60 days of EBP-only success does not validate fallback).

---

## C. NDX instrument policy — V4_SOURCE_IDENTITY_CHANGE (comparison spec; empirics BLOCKED here)

Classification accepted: converting every provider to QQQ is a **source
substitution**. Nothing in `resolve_symbol`, Stooq behavior, S4, D4, or cache
identity was modified.

### Corrected executable resolution matrix (actual fetcher behavior)

Produced by executing `resolve_symbol` per provider/state AND by calling
`fetch_stooq("NDX")` with the PoW fetcher stubbed to capture the real vendor
symbol (the diagnostic no longer trusts `resolve_symbol` for Stooq):

| State | Provider | Vendor symbol actually requested | Native index? |
|---|---|---|---|
| default | tiingo | QQQ | no |
| default | twelvedata | QQQ | no |
| default | alphavantage | QQQ | no |
| default | **yfinance** | **^NDX** | **yes** |
| default | stooq (disabled) | — (`ProviderNotConfigured`) | — |
| `TWELVE_DATA_INDICES=true` | twelvedata | NDX | **yes** |
| `STOOQ_ENABLED=true` | **stooq via `fetch_stooq`** | **`^ndx`** | **yes** |
| (any) | stooq via `resolve_symbol` (diagnostic only) | QQQ *(wrong — divergence)* | — |

**Confirmed divergence:** `resolve_symbol("NDX","stooq")` reports `QQQ`/proxy
while `fetch_stooq("NDX")` actually requests `^ndx` (`prices.py:446` bypasses
the central map). Per instruction this is only *reported* here; scored behavior
was not touched.

### Candidate policies and the comparison the production host must run

`C0_CURRENT_CHAIN` (provider-dependent mix) · `C1_QQQ_ADJUSTED` ·
`C2_QQQ_UNADJUSTED` · `C3_NATIVE_NDX`.

Adjustment bases in the current chain (from code): Tiingo `adjClose`
(adjusted); Twelve Data free (unadjusted); Alpha Vantage `TIME_SERIES_DAILY`
(unadjusted); yfinance `auto_adjust=True` (adjusted); terminal cache inherits
whichever provider wrote it (`CACHE_MAX_ROWS=900`, `CACHE_SLA_DAYS=3`). So C0's
identity varies on BOTH axes (instrument and adjustment) purely by which
provider answered.

Required report per policy pair over overlapping history (runnable harness
below): price-level and daily/monthly return correlations; max tracking
divergence; S4 GSADF state changes + statistic/CV deltas (needs R/exuber);
D4 confidence changes; D-block and headline deltas; provider-fallback
frequency; missing-history frequency; stale-cache behavior; adjustment-basis
provenance.

**BLOCKED(here):** every data-dependent row — this container cannot reach any
price host and has no R runtime. The harness is committed as
`docs/harnesses/c_ndx_drift_harness.py` and runs on the production host
unchanged (it uses the service's own price layer + gsadf runner + d4 module).

**No canonical basis is proposed for approval in this document.**

---

## F. ATH basis — source/continuity study spec + honest label (label executed)

### Executed now (authorized): provenance label

Every snapshot's `data_freshness` now carries
`_ath_provenance: {"ath_basis": "adjusted_spy_provider_window_max",
"ath_history_complete": false}`; the scored rule is untouched (tested).

### Discovered freeze gap (reported, deliberately NOT fixed this cycle)

`frozen_methodology.json` pins `red_flags.index_near_ath_frac = 0.98`, but
`compute.py:478` still evaluates the literal `0.98` — an unwired duplicate that
the completeness test missed. Behavior is identical (same value); the F-01
"no hardcoded duplicate" guarantee is violated for this one constant. Fix is a
one-line wiring + one completeness assertion; it awaits explicit authorization
since today's instruction was "no runtime change" beyond the three authorized.

### Design record (operator direction, unchanged)

Target object: **S&P 500 price-index ATH**; fallback **unadjusted SPY** labelled
ETF proxy; adjusted-SPY-as-ATH not acceptable; typed `AthSeriesIdentity` +
persisted watermark (identity, ath_value/date, seed bounds, update timestamps,
source chain); watermark safety rules (no state before seed completes; no
identity mixing; no downward revision in ordinary updates; explicit migration
for corrections; no silent cross-identity fallback inheritance).

### Comparison spec F0/F1/F2 (empirics BLOCKED here)

F0 = current adjusted-SPY ≤900-row window max; F1 = native S&P 500 price index
with a long-history seed; F2 = unadjusted-SPY long history. Required outputs:
daily `within_2pct_of_ath` states, disagreement dates, red-flag-count and
≥3-of-4 override differences, headline-floor activations, gaps/stale periods,
behavior around splits/dividends/provider switches/cache loss.

Candidate seed sources requiring continuity demonstration on the production
host: Stooq `^spx` (long price-index history; PoW caveat), or a committed
static price-index watermark as the seed floor. FRED `SP500` remains rejected
as a seed (10-year window only). Harness:
`docs/harnesses/f_ath_continuity_harness.py`.

---

## B. MIN_RESOLVED_SCORE — BLOCKED (replay-gated); policy set formalized

No runtime change; no constant added; `insufficient_coverage` state NOT
introduced. The candidate policies to evaluate once replay data exists, exactly
as the operator enumerated:

| Policy | Definition |
|---|---|
| B0 | current behavior — headline always visible (band suppression only) |
| B1 | headline withheld when EITHER block is degraded (reuses the existing 2/3 gate as the availability gate) |
| B2 | per-block minimum obtained quality-weight ≥ 0.50 |
| B3 | per-block minimum obtained quality-weight ≥ 2/3 |
| B4 | combined two-block minimum (masking risk: a full block can hide an empty one — must be reported) |
| B5 | minimum COUNT of resolved indicators per block (weight-independent) |

Required per-policy outputs (operator list, recorded): % of historical
recomputes with headline available; % with one/both blocks degraded; band
availability; override behavior; renormalization score displacement; which
missing indicators most often trigger suppression; longest continuous
unavailable period; cross-block masking check.

**Why BLOCKED:** there are zero persisted production snapshots and no
historical multi-source archive reachable from this container; a
point-in-time-safe replay cannot be built from nothing, and the operator has
forbidden choosing a constant from synthetic subset enumeration alone.

**Unblocking path (proposed, not built):** (i) turn on prospective collection —
every 4-hourly production snapshot already persists per-indicator
value/quality/staleness; add the frozen-artifact SHA-256 stamp per snapshot
(also feeds the falsification clock's evidence rule); (ii) after ≥60 trading
days, replay B0–B5 against the accumulated snapshots; (iii) separately, a
PIT replay of *upstream* sources is only possible for sources with archived
vintages (EDGAR filings, FRED ALFRED) — scoped in the D/E design below.

---

## D + E. D3 non-positive OCF × minimum issuers — BLOCKED (replay-gated); joint design fixed

No mapping and no quorum implemented. The joint experiment, per the operator:

**Policies:** D0_DROP (explicit provenance) · D1_DROP_AND_DEBIT_QUALITY ·
D2_DISTRESS_FLAG_ONLY · D3_SATURATED_BASE · D4_DROP_ENTIRE_D3 ·
D5_TWO_COMPONENT — crossed with minimum usable issuers ∈ {1,2,3,4,5} and
below-minimum action ∈ {drop-and-renormalize, suppress-D-block-output}.

**Mathematical-order constraint (recorded as binding):** the pipeline is
`issuer facts → issuer-level state/transformed stress → cross-company
aggregation → gate/cap → D3 sub-score`. Averaging raw capex/OCF ratios and
averaging bounded issuer stresses are DIFFERENT methodologies; the experiment
must run both aggregation orders wherever a bounded mapping (D3_SATURATED_BASE,
D5) is evaluated. Current production is avg-raw-then-transform
(`compute.py:913` → `sub_score`), which the study must hold as its baseline.

**Data path (the one genuinely PIT-safe replay available):** SEC EDGAR
`companyfacts` carries every filed duration fact with `end` AND `filed` dates,
so issuer-level TTM capex/OCF series can be reconstructed *as of* any
historical date using only facts with `filed <= as_of` — a true point-in-time
vintage. Harness committed as `docs/harnesses/de_d3_pit_harness.py`; it needs
`data.sec.gov` access (denied from this container, available to the production
host, which already talks to EDGAR daily).

**Required outputs per combination (recorded):** D3 availability; D3 sub-score;
D-block coverage; D-block score; headline; band state; occurrence frequency;
affected issuers/periods; leave-one-issuer-out sensitivity; and the audit
question *"does any mapping make an OCF-distress event LESS alarming?"* (the
known failure mode of D0/D1).

---

## G. LPPLS single-process reproducibility probe

Executed per authorization; per the operator's files-changed constraint
("none or scratch only") the run table lives in the session report and the
scratch directory, not in this document. Summary of what was run: fixed input
bytes; `lppls==0.6.24`; child-process seeding of BOTH `random` and NumPy;
`compute_nested_fits` (single-process, no Pool); 20 independent seeded process
launches + 5 unseeded + 3 alternate-seed launches; per-run serialized-output
SHA-256, window counts, runtime, peak RSS. Interpretation and the G0–G3 design
comparison accompany the table in the session report. No production code path
was touched; no seed constant was added anywhere.

**Operator G-clarification (2026-07-23, recorded):** the G1 option is to be
described precisely as the **deterministic reference implementation** — not
merely "single-process deterministic" — because it may become the scientific
baseline against which any future parallel implementation (G3 or a repaired
multiprocessing path) is validated. Any parallel execution model must
reproduce, or explicitly justify divergence from, the G1 reference output on
identical input bytes and seed.

---

## Committed harnesses (runnable on the production host)

- `docs/harnesses/c_ndx_drift_harness.py` — C0–C3 series construction through
  the service's own price layer, correlation/divergence stats, S4 (via
  gsadf_runner) and D4 (via d4_lppls) per-policy runs, JSON report.
- `docs/harnesses/f_ath_continuity_harness.py` — F0/F1/F2 watermark
  construction, daily flag series, disagreement/override-input diff, gap audit.
- `docs/harnesses/de_d3_pit_harness.py` — EDGAR companyfacts PIT
  reconstruction (filed-date filtered), D0–D5 × quorum grid, both aggregation
  orders, leave-one-out sensitivity.

Each harness is read-only toward production state (no DB writes, no cache
mutation) and prints a single JSON document for operator review.

---

*No PIN beyond A was resolved in this cycle. No score-shifting change is
activated. The remaining PINs stay explicitly open: falsification date; B
semantics/threshold; C canonical basis; D mapping; E quorum; F source chain;
G execution design; the six S5 constants.*
