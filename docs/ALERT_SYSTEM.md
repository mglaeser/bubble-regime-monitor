# The bubblegauge alert system

An event-driven, stateful, replayable alert layer over the regime score. It
consumes committed scoring outcomes, exposes the complete alert state to a
frontend, and — once an operator explicitly turns it on — sends deterministic
SMS notifications. It never changes scoring.

**Current state: Stages 0 and 1 are implemented and the delivery path is built
but unreachable. Nothing sends anything.** `ALERT_INPUT_CAPTURE` and
`ALERTS_MODE` both default off; under `shadow` the whole pipeline runs against
a `NullSender`, and only an operator setting `ALERTS_MODE=live` can change that
— which the Stage 2 and Stage 3 gates do not yet permit. See
[What is not built](#what-is-not-built).

---

## 1. The four invariants everything else serves

**Alerting never touches scoring.** It reads persisted outcomes. It does not
re-derive a band, a red flag, an override or a coverage verdict — not even to
double-check. Re-implementing one of those formulas is a defect *even when the
result matches*, because the two copies will diverge eventually and the alert
copy is the one nobody validates. `tests/test_alert_snapshot_contract.py`
asserts the golden fixture, the frozen-methodology hash, and that no scoring
module imports the alert contract.

**UNKNOWN is not NORMAL.** An evaluation that could not read what it needed
returns `UNKNOWN`. It never resolves an episode, never advances confirmation,
never resets it. A pending candidate survives an outage and dies only through
its explicit TTL. The three-valued truth is a real type in `primitives.py`, so
collapsing it into a boolean is not something you can do by accident.

**Delivery is not condition state.** A firing episode may be silenced,
superseded, held, queued, sent, failed or ambiguous while still firing. The API
reports `condition_state`, `suppression_reasons`, `planning_state` and
`notification_disposition` as four separate fields, and `/latest` keeps "fired"
and "sent" in different pointers.

**Everything is content-addressed and replayable.** Rules, phrases and the
input sidecar are immutable artifacts identified by SHA-256. Every episode
names the ruleset that opened it, and that ruleset keeps being evaluated until
the episode closes. A replay reads persisted sidecars and archived bytes — it
never asks a provider what the world looks like now.

---

## 2. How a snapshot becomes an alert

```
T1   SCORING            snapshot COMMITS  ─────────────────────────┐
                                                                   │ separate
P0a  INPUT CAPTURE      short write txn, commits on its own        │ txns, in
P0b  EVALUATION CLAIM   short write txn, commits separately        │ this order
                                                                   │
P1   PURE EVALUATION    NO write txn, NO I/O, monotonic deadline   │
P2   ATOMIC APPLY       one write txn, CAS every state row  ───────┘
```

Each phase has its own exception boundary and none may roll back T1 — a scoring
snapshot is never held hostage to the alert layer. P0a and P0b are separate
because a lost sidecar is a permanent hole in the replay record, while a lost
evaluation is simply retried.

P1 holds no write lock. On a single-writer SQLite database, doing rule
evaluation and history reads inside the write transaction would block the next
recompute. P2 is all-or-nothing: any compare-and-set miss or deadline overrun
rolls back the entire plan, because half an episode is worse than none.

### Where the boundary is in code

`app/services/compute.py::run_recompute` calls
`app/services/alert_integration.py::on_snapshot_committed(snap_id)` strictly
after the snapshot commits, inside a `try/except` that logs and swallows.

---

## 3. The typed snapshot contract (Stage 0)

The persisted `action_band` is a *display* string that folds three facts into
one field (`"suppressed (block degraded)"`). Parsing it would be guessing;
recomputing the band would be a shadow scorer. So the scoring layer persists
the decomposition it already knows:

| column | meaning |
|---|---|
| `score_action_band` | band implied by the Monte Carlo **median** alone |
| `base_action_band` | after the override, before coverage suppression |
| `effective_action_state` | `hold` / `trim` / `de-risk` / `suppressed` |
| `band_suppressed_by_coverage` | coverage suppressed the base decision |
| `data_degraded` | typed coverage verdict, independent of any prose |
| `red_flag_meta` | per-flag active/fireable/state/distance/provenance |
| `override_required_count`, `override_fireable_universe_count` | the override arithmetic, read not restated |
| `prev_snapshot_id`, `expected_recompute_slot`, `alert_contract_version` | lineage |

`app/engine/snapshot_contract.py` derives these by **calling**
`app/engine/aggregate.py` — it restates no threshold and no formula. The legacy
`action_band` column and its API field are unchanged.

Full derivation and backfill policy: **`docs/ALERT_STAGE0_AUDIT.md`**.

---

## 4. Rules are data

`config/alert_rules.v3.2.yaml` is the complete inventory: 90 rules and
constellations, 30 enabled, the rest disabled with a recorded reason. No rule
may exist only in Python.

A condition is one of a small closed set of shapes — `transition`,
`boolean_transition`, `boolean_state`, `enum_equals`, `threshold`, `range`,
`crossing`, `delta`, `count`, `freshness`, `never`, `all_of`, `any_of`. There is
deliberately **no expression node**: a formula is how an alert layer
accidentally becomes a second scorer.

### What the loader refuses

Fail-closed and total — every problem is reported together, and an invalid
ruleset is rejected whole rather than having the offending rule dropped (a
silently-dropped rule is a mechanism that looks configured and never fires):

- unknown fields, unknown sources, unknown operators;
- a bare `score` source (ambiguous between the median and the point score);
- a threshold or hysteresis on a persisted **decision**;
- an `enabled` rule referencing an unresolved `[PIN]`;
- a P1 that is not exempt from quiet hours *and* budgets;
- a hold source with no freshness requirement;
- a multi-observation confirmation with no candidate TTL;
- a confirmation source that is also a hold source;
- dominance cycles, self-supersession, unknown supersession targets;
- methodology or service-version mismatch;
- `distinct_source_revision` without a written `revision_sensitive` justification.

### Thresholds and `[PIN]`

A threshold with `value: null` and `attribution: PIN` is unresolved. The rule
stays disabled and the API reports `value: null` plus `unresolved_reason` —
**never** the literal string `"<PIN>"` in a numeric field. Sixteen rules are
currently blocked this way; five more on inputs this service does not have.

---

## 5. Confirmation, and why a failover cannot fake it

Observation identity is split three ways:

```
economic_observation_key   WHAT was measured, for WHICH economic period.
                           Provider-independent.
source_revision_key        WHICH vintage, from WHICH provider.
computation_fingerprint    WHICH code produced the value.
```

Confirmation counts `economic_observation_key`. So a provider failover halfway
through a two-observation confirmation produces the *same* key for the same
day — it collides instead of counting twice. The same holds for a vendor
revision and for a code redeploy.

`confirmation_sources` must **advance**; `hold_sources` only have to stay true
and stay fresh. A constellation whose daily leg advanced twice while its
monthly leg did not has *not* been confirmed — every declared confirmation
source must reach the required count.

A stale hold source makes the rule `UNKNOWN`, not false: "it was true four
weeks ago" is not evidence that it is true now.

### TTLs live in real calendars

`RECOMPUTE_SLOT` (the 02/06/10/14/18/22 UTC cron), `US_TRADING` (NYSE sessions,
computed from the rules including Good Friday and Juneteenth),
`MONTHLY_RELEASE`, `QUARTERLY_FILING`. A candidate needing "two more breadth
observations" does not expire over a long weekend.

---

## 6. Priorities, budgets and quiet hours

| class | channel | quiet hours | budget | default cooldown |
|---|---|---|---|---|
| P1 | immediate SMS | **ignored** | **exempt** | 48h |
| P2 | bundled SMS | `[07:00, 22:00)` Europe/Berlin | non-P1 caps | 24h |
| P3 | weekly digest | n/a | digest channel | n/a |
| P4 | API / log only | n/a | none | n/a |

Quiet hours use IANA rules, so the release time moves with DST; exactly 22:00
is held. The non-P1 budget is 2 per rolling 168h in quiet regimes, hard-capped
at 3/24h and 6/168h. **P1 is never held by either** — enforced by the ruleset
loader *and* by a CHECK constraint on `alert_delivery`, so a future planner bug
cannot even persist the mistake.

---

## 7. Database guarantees

Three things are enforced by the database rather than by application code:

- **one open episode per `(mode, live_profile, instance_fingerprint)`** — a
  partial unique index, not a SELECT-then-INSERT race;
- **immutable artifacts** — triggers reject a change to phrase-set bytes under
  an existing version, ruleset bytes under an existing hash, any update to an
  input sidecar, or a rewrite of a final render;
- **a non-TEST delivery always carries a live member** — a trigger, since
  SQLite has no deferred cross-table constraint.

The Alembic migration installs them as frozen literal DDL, and the ORM installs
the same triggers on the `create_all` path. The guarantee must not depend on
which bootstrap ran; `tests/test_migrations.py::test_migrations_match_models`
checks the schemas match.

`busy_timeout` is now set (`ALERTS_BUSY_TIMEOUT_MS`, default 5000). The alert
system adds a second and third writer to what was a single-writer service;
without it SQLite returns `SQLITE_BUSY` immediately on contention — a lost
alert plan rather than a slower one.

---

## 8. Configuration

Two **independent** switches:

```bash
ALERT_INPUT_CAPTURE=false   # persist the point-in-time sidecar
ALERTS_MODE=disabled        # disabled | shadow | live
```

Capture may run with alerting fully disabled — that is how Stage 1 collects
replay material. Enabling alerts never implies capture. `live` is never reached
automatically: it needs promoted artifacts *and* a deliberate edit.

```bash
ALERTS_READ_API_KEY=        # alert reads (or ALERTS_PUBLIC_READ=true)
ALERTS_WRITE_API_KEY=       # silences and operator actions
ADMIN_API_KEY=              # promotion, evaluation, recovery
```

Three separate scopes. **Alert reads do not fall back to the admin key** —
unlike the scoring API, which does. That fallback is exactly what would put an
admin credential in a browser.

`DAILY_SMS_ENABLED` is a migration-friendly alias for `SMS_ENABLED`. The legacy
daily digest keeps its own switch; turning the alert system on never disables
it. Cutover is the explicit Stage 4 gate.

Volume, lease, retention and LLM settings: see `app/config.py` — every one has
a safe default and none of them is read by anything that sends.

---

## 9. Operating it

```bash
python -m app.alerts.cli validate [--rules PATH] [--phrases PATH] [--promote]
python -m app.alerts.cli preflight            # pre-stage checks
python -m app.alerts.cli ruleset              # active ruleset summary
python -m app.alerts.cli health
python -m app.alerts.cli pending              # open episodes
python -m app.alerts.cli evaluate --input-identity ID [--shadow]
python -m app.alerts.cli explain --evaluation-id ID
python -m app.alerts.cli recover-evaluations --once
python -m app.alerts.cli reconcile-sidecars
python -m app.alerts.cli watchdog --once       # exit 2 = outage detected
python -m app.alerts.cli dispatch --once       # one outbox pass
```

`explain` returns facts and decisions — never private reasoning.

`--promote` is the only way to promote from the CLI, and
`POST /api/v1/admin/alerts/promote` the only way over HTTP. Nothing promotes as
a side effect of a boot, a deploy or a validation run, and **promotion does not
change `ALERTS_MODE`**.

### Crash recovery

| state | meaning | action |
|---|---|---|
| lease live | in progress | leave it alone |
| lease expired, `plan_applied=0` | died before applying anything | `ABANDONED`; safe to retry under the same logical identity |
| lease expired, `plan_applied=1` | applied a plan but never recorded finishing | **never auto-repaired** — needs a human |

`reconcile-sidecars` lists committed snapshots with no sidecar. A gap is
reported, never quietly filled: a sidecar reconstructed after the fact is
marked `RECONSTRUCTED` and never counts as successful mandatory-event recall.

---

## 10. The API

Read (`ALERTS_READ_API_KEY`), write (`ALERTS_WRITE_API_KEY`), admin
(`ADMIN_API_KEY`). Errors are RFC 9457 `application/problem+json`. Reads carry
an `ETag`; mutations are `Cache-Control: no-store`.

```
GET  /api/v1/alerts/overview          one screen: states, open episodes, pointers
GET  /api/v1/alerts/mechanisms        every rule instance, including dark ones
GET  /api/v1/alerts/mechanisms/{fp}   one mechanism, addressed by fingerprint
GET  /api/v1/alerts/rules/{id}/instances
GET  /api/v1/alerts/episodes[/{id}]   {id} includes its event trail
GET  /api/v1/alerts/events            cursor-paginated, stable ordering
GET  /api/v1/alerts/latest            fired and sent as SEPARATE pointers
GET  /api/v1/alerts/deliveries[/{id}] redacted
GET  /api/v1/alerts/renders/{id}
GET  /api/v1/alerts/ruleset
GET  /api/v1/alerts/health
GET  /api/v1/alerts/silences
POST   /api/v1/alerts/silences        Idempotency-Key honoured; 409 on reuse with a different body
DELETE /api/v1/alerts/silences/{id}
POST /api/v1/admin/alerts/evaluate    one sidecar, shadow by default
POST /api/v1/admin/alerts/promote
POST /api/v1/admin/alerts/recover
```

A mechanism that has never fired is still in `/mechanisms`, with
`activation_status`, `disabled_reason` and its unresolved pins. An operator has
to be able to see that a rule exists and why it is dark.

`docs/openapi-alerts.json` is generated from the running app
(`python -m scripts.export_alert_openapi`) and CI fails on drift. The
application's own `/openapi.json` remains the source of truth.

### Browser topology

```
browser -> authenticated dashboard backend/proxy -> bubblegauge
```

A browser-visible read token is not secret and may reach only the redacted
projection. The app's CORS posture is GET-only, so the write routes are not
browser-reachable cross-origin without a separate security review. Delivery
projections carry no recipient, no provider correlation id, no raw provider
error and no raw model output.

---

## 11. What is not built

Present and honest about it, rather than half-built:

| stage | scope | status |
|---|---|---|
| 0 | typed snapshot contract | **done** |
| 1 | schema, sidecar capture, pure evaluation, CAS, read API | **done** |
| 2 | `[PIN]` calibration, replay budgets, mandatory-event fixtures | not started — needs operator artifacts |
| 3 | deterministic P1 delivery | planner, outbox, renderer, typed sender and dispatcher **built and off**; not gated |
| 4 | legacy daily-digest cutover | not started |
| 5 | constellations, bundled P2 | rules present, delivery not built |
| 6 | EWMA / CUSUM | not started — needs immutable calibration artifacts |
| 7 | P3 enrichment, LLM A/B review | not started |

Concretely absent: the **weekly digest job** (digest ITEMS are created and
tracked; the job that turns a window's items into one SMS is not written), the
**statistical monitors**, the **replay/dry-run harness** and its budget and
mandatory-event reports, and the **actionability review workflow**.

Built but deliberately unreachable: the planner, the outbox, the renderer, the
LLM code selector, the typed sipgate sender and the dispatcher. They run end to
end under `ALERTS_MODE=shadow` against the `NullSender` — claiming,
revalidation, budget recheck, rendering and outcome classification all execute
and persist — but no SMS can leave the host until an operator sets
`ALERTS_MODE=live`, and the Stage 2 and Stage 3 gates (calibrated pins, replay
budgets, mandatory-event recall) have not been met.

`ops.*` rules that are raised by machinery rather than evaluated from the
sidecar (`ops.rules_invalid`, `ops.delivery_unknown`,
`ops.condition_unknown_persistent`, …) are in the inventory as `never`
conditions with a recorded reason, so the mechanism is visible even though its
producer is not built.

---

## 12. Epistemic posture

The headline is a structured 0–100 regime heuristic. It is **not a
probability**, it is uncalibrated, and the reference class is far too small for
honest probability calibration. Alert text never states crash odds, certainty,
buy/sell instructions or guaranteed outcomes — the phrase-set validator checks
for that vocabulary, and the model may only select reviewed codes.

Exactly-once SMS delivery is not promised. Ambiguous delivery outcomes are made
visible and handled conservatively rather than retried into duplicates.

The SMS budgets and priority classes are project **judgments** validated
through replay, not scientifically derived constants. Alarm-fatigue and
industrial-alarm literature (EEMUA 191, IEC 62682, ANSI/ISA-18.2, the Google
SRE material, Sentinel Event Alert 50) support prioritisation, grouping and
low-noise design; they do not derive a personal SMS budget.

Research and engineering specification — not investment advice.
