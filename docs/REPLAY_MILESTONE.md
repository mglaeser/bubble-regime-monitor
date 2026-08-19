# Milestone: Historical Replay Infrastructure

**Opened:** 2026-07-23, on the operator's PR #17 merge ruling. The governance
milestone is closed (see [`GOVERNANCE_FREEZE_RULE.md`](./GOVERNANCE_FREEZE_RULE.md));
this milestone is the highest-value next investment.

**Why:** without replay, almost every remaining PIN devolves into qualitative
judgement. With replay, B (coverage floor), D/E (D3 OCF/quorum), F (ATH basis),
G (LPPLS execution model), and H (S5 calendar activation) become **measurable**.

**Status: IMPLEMENTED (v3.8.0)** on operator authorization ("Go for it!
Implement all"). Per-workstream delivery:

- **RM-1 SHIPPED** — snapshot methodology stamp (migration 0006 +
  `Snapshot.methodology_sha256/_version`), append-only `falsification_outcomes`
  (DB triggers via migration AND model DDL), `POST /api/v1/admin/falsification`,
  `GET /api/v1/replay/evidence`.
- **RM-2 SHIPPED** — `GET /api/v1/replay/sufficiency`: distinct trading days
  overall + per S5 tier vs the ≥60-day gate; per-tier adequacy deliberately
  unpinned. Snapshots are never pruned (no retention job exists), so the
  comparison window accumulates by default.
- **RM-3 SHIPPED (tooling)** — `docs/harnesses/alfred_vintage_harness.py`
  (true PIT vintages via ALFRED realtime windows; runs on the production
  host, needs FRED_API_KEY); EDGAR PIT + price-seed harnesses were already
  committed.
- **RM-4 SHIPPED** — `scripts/replay_report.py`: B0–B5 candidate coverage
  policies and the S5 positional-vs-calendar dual report incl. the
  hypothetical headline delta, over persisted snapshots; D/F/C studies run
  via their harnesses on the host.
- **RM-5 SHIPPED** — `replay_report.py assemble`: per-PIN decision packages;
  host-dependent studies explicitly `PENDING_HOST`.

Original scoped proposal below, retained as the specification of record.

---

## Workstreams

### RM-1 — Snapshot evidence enrichment (prospective; runtime, non-scoring)

Every persisted snapshot additionally records:
- the **frozen-artifact SHA-256** and `methodology_version` in force at compute
  time (the operator's falsification-clock evidence rule: snapshot + timestamp
  + attached methodology hash);
- append-only semantics for falsification outcomes (evidence that history
  cannot be silently rewritten).

Acceptance: a snapshot row is sufficient evidence for the
`falsification_tracking_since` rule; zero score effect (golden byte-identical).

### RM-2 — Prospective accumulation

The 4-hourly production recompute already persists snapshots; define retention
(no pruning during the comparison window) and the observation-sufficiency
tracker for the S5 activation gate: **≥60 trading days AND adequate
observations across all three S5 source tiers** (EBP-only success does not
validate fallback behavior).

Acceptance: a queryable count of qualifying days per tier.

### RM-3 — Point-in-time source replay

| Source | PIT mechanism | Feeds |
|---|---|---|
| SEC EDGAR companyfacts | `filed <= as_of` filtering (harness exists: `docs/harnesses/de_d3_pit_harness.py`) | D/E grid |
| FRED → **ALFRED** archival vintages | true publication-vintage series for BAA/DGS10/HY-OAS (and EBP vintage policy evaluation) | H vintage PIN, B, F |
| Long price histories | Stooq `^spx`/`^ndx` or committed seed CSVs (network-policy dependent) | F watermark, C drift |

Acceptance: for each source, a dated series reconstructable *as of* an
arbitrary historical date with no future information.

### RM-4 — Policy replay engine

Runs candidate policies over accumulated + reconstructed history and emits the
operator's required outputs:
- **B0–B5** coverage policies → headline availability %, degraded-block rates,
  band availability, override behavior, renormalization displacement, worst
  suppression drivers, longest unavailable period, cross-block masking;
- **D0–D5 × quorum 1–5 × drop/suppress × both aggregation orders** → D3
  availability/sub-score/D-block/headline/band effects, occurrence frequency,
  leave-one-issuer-out sensitivity, "distress made less alarming" audit;
- **F0/F1/F2** ATH bases → daily flag series, disagreement dates, red-flag and
  override differences;
- **S5 positional vs calendar** dual-report backfill → sub-score/S-block/
  headline/MC/band drift for the H activation decision.

Acceptance: one JSON report per study, reproducible from pinned inputs.

### RM-5 — Activation-gate evidence assembly

Collates RM-2/RM-3/RM-4 outputs into the per-PIN decision packages the
operator's gates require (H activation; G execution-model decision with host
runtime benchmarks of the **G1 deterministic reference implementation** vs the
current multiprocessing path; B/D/E/F constants).

---

## Order and dependencies

RM-1 → RM-2 (prospective track, starts the clock) in parallel with
RM-3 (retrospective track) → RM-4 → RM-5. RM-1 is first because every waiting
day without artifact-hash-stamped snapshots lengthens the comparison window.

## Out of scope

Any score-shifting activation; any PIN resolution; any governance expansion
beyond the opportunistic ledger cleanups permitted by the Freeze Rule.
