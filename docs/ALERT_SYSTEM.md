# The bubblegauge alert system

An event-driven, stateful, replayable alert layer over the regime score. It
consumes committed scoring outcomes, exposes the complete alert state to a
frontend, and — once an operator explicitly turns it on — sends deterministic
SMS notifications. It never changes scoring.

**Current rollout: the governed deterministic delivery path and its operational
controls are implemented, but the committed ruleset remains at Stage 1.**
Sidecar capture is on (`ALERT_INPUT_CAPTURE`
defaults true — it records evidence and nothing else) while `ALERTS_MODE`
defaults `disabled`. Deterministic delivery, reminders, bundles, the weekly
digest, watchdog/recovery, retention, cutover checks and actionability evidence
are implemented and tested. They are not permission to send: live alert
delivery is refused below Stage 3, and the committed Stage-3 replay currently
fails its non-P1 volume gate. The separate legacy daily digest may still send
through its configured transport until the observed Stage-4 cutover is
completed. See [Rollout status and remaining evidence](#rollout-status-and-remaining-evidence).

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
constellations, 29 enabled, the rest disabled with a recorded reason. No rule
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

### The basis is inert when confirmation is 1

Mandate §8.1 scopes the candidate latch to "a rule with confirmation greater
than one". That is exactly where a period-naming basis —
`new_filing`, `new_release_period`, `new_month_end_period`,
`distinct_economic_observation`, `distinct_trading_date` — is enforced: two
readings of one period are counted once, so the rule confirms only on a
genuinely new period.

**At `count: 1` there is no candidate.** The rule fires on the transition
itself and the basis is never consulted. Twenty-one of the shipped rules are
written this way, including every Faber leg and both s3 tiers.

So for those rules the declaration describes intent, not enforcement, and what
actually limits a repeat notification is `cooldown_seconds` — 2 days on the
Faber legs, 30 on the release-driven rules. If a source flip-flops inside one
period, the condition re-fires and the cooldown is the only thing standing
between that and a second message.

The loader emits a warning naming each such rule and the cooldown carrying the
weight, because the failure mode this documents is a reader trusting an
enforcement that is not happening. It is a warning rather than a rejection: the
rules are not broken, and all of them carry a real cooldown.

If a rule ever needs true one-period-one-notification semantics, the honest
routes are to give it `count: 2` on that basis, or to add per-period
suppression to the state machine — which is persistence the mandate does not
currently ask for, and should not be added without deciding that it should.

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
- **a non-TEST delivery always carries a represented member at the provider
  boundary** — one trigger rejects a memberless transition to `SENDING` or
  `SENT`, and a companion insert trigger rejects a non-TEST row created
  directly in either status. SQLite has no deferred cross-table constraint, so
  legitimate intents are inserted pre-wire, gain their members, and then cross
  the guarded transition.

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
ALERT_INPUT_CAPTURE=true    # persist the point-in-time sidecar  (Stage 1: ON)
ALERTS_MODE=disabled        # disabled | shadow | live           (Stage 1: off)
```

Capture runs with alerting fully disabled — that is how Stage 1 collects replay
material, and it is why capture defaults **on**. Leaving it off would make the
stage inert: no sidecars means nothing to replay, while still claiming the
stage had been reached. Capture writes one immutable evidence row per recompute
in its own transaction; it calls no provider, alters no score and cannot roll
back a snapshot.

`ALERTS_MODE` is the switch that decides whether the service *acts*, and it is
the one that defaults off. Enabling alerts never implies capture, and `live` is
never reached automatically: it needs promoted artifacts *and* a deliberate
edit.

Capture has **two** authorities and they are not interchangeable.
`ALERT_INPUT_CAPTURE` is the operator's kill switch; `capture.enabled` in the
promoted ruleset is the artifact's own declaration, and it is read rather than
decorative — an artifact that says capture is on while the code has it off is
worse than one that says nothing. A ruleset that fails to load does **not**
stop capture: the sidecars are exactly what an operator needs to diagnose the
ruleset that failed, and a lost sidecar can never be backfilled.

```bash
ALERTS_READ_API_KEY=          # alert reads (or ALERTS_PUBLIC_READ=true)
ALERTS_READ_API_KEY_PREVIOUS= # rotation overlap; clear it to retire the old key
ALERTS_WRITE_API_KEY=         # silences and operator actions
ADMIN_API_KEY=                # promotion, evaluation, recovery, render text
ALERTS_READ_TOKEN_IS_PUBLIC=true   # the read key is browser-visible (H-05)
```

Three separate scopes. **Alert reads do not fall back to the admin key** —
unlike the scoring API, which does. That fallback is exactly what would put an
admin credential in a browser.

`DAILY_SMS_ENABLED` is the migration alias for `SMS_ENABLED` and the explicit
Stage-4 **master switch** for the whole legacy daily digest. Before cutover,
when the alias is unset, `IMESSAGE_ENABLED` may select iMessage and iMessage
wins when both configured transports are on; there is no send-failure fallback.
After the observed Stage-4 gate, setting `DAILY_SMS_ENABLED=false` disables the
legacy schedule regardless of whether its previous carrier was sipgate or
iMessage. Turning the alert system on still never changes this value or
implicitly disables the legacy digest.

Volume, lease, retention and LLM settings live in `app/config.py`; each has a
safe default. The `ALERTS_LLM_*` settings reserve the dormant Stage-7/A-B
selector — the dispatcher does not invoke it, and configuring the runtime
gateway activates only the judgment/digest paths. Configuration alone never
grants delivery permission: the stage, evidence, promotion and per-delivery
admission checks remain authoritative.

---

## 9. Operating it

```bash
bubblegauge alerts validate [--rules PATH] [--phrases PATH] [--promote]
bubblegauge alerts preflight                  # pre-stage checks
bubblegauge alerts ruleset                    # active ruleset summary
bubblegauge alerts health
bubblegauge alerts pending                    # open episodes
bubblegauge alerts evaluate --input-identity ID [--shadow]
bubblegauge alerts explain --evaluation-id ID
bubblegauge alerts recover-evaluations --once
bubblegauge alerts recover-leases --once
bubblegauge alerts reconcile-sidecars
bubblegauge alerts watchdog --once             # exit 2 = outage detected
bubblegauge alerts dispatch --once             # one outbox pass
bubblegauge alerts digest --window 2026-W33 --dry-run

bubblegauge export snapshots --all --format parquet --out snapshots.parquet
bubblegauge stats deltas --economic-observations --out deltas.json
bubblegauge stats transitions --out transitions.json
```

`explain` returns facts and decisions — never private reasoning.

The export and statistics commands are point-in-time, read-only reports over
persisted sidecars and alert metadata. They do not import a provider, query
current market state, or recompute a score. The delta report keeps economic
observations, provider revisions, computation fingerprints, evidence
occurrences, and recompute inputs separate. The transition report covers
entries into de-risk by origin, one-snapshot reversals, base/effective
divergence, rf3/rf4 and Faber transitions, non-fresh evidence, sidecar gaps,
evaluation conflicts/timeouts, and UNKNOWN deliveries. Parquet export needs
the optional `bubblegauge[parquet]` dependency; on unsupported hosts it refuses
rather than silently writing another format.

`digest --dry-run` exercises artifact registration and the real digest planner
inside one explicit transaction, then rolls back **all** registry, event,
item, member, and delivery mutations. It never constructs a sender. Omitting
`--dry-run` is an operator action against the configured alert namespace; an
open ISO week is refused before its dedupe key can be consumed.

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

`recover-leases --once` is the separate delivery-lease sweep. An expired
`LEASED` row with no `request_started_at` is definitely pre-wire and returns to
`RETRY_DUE`; an expired `SENDING` row, or any row whose request had started,
becomes `UNKNOWN` because the provider may have accepted it. The latter is
never auto-retried under the same generation.

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
POST /api/v1/admin/alerts/render      validate reviewed TEST bytes; never persist/send
GET  /api/v1/admin/alerts/renders/{id} operator-only message text
POST /api/v1/admin/alerts/send-test   queue an audited TEST delivery
POST /api/v1/admin/alerts/deliveries/{id}/retry
POST /api/v1/admin/alerts/actionability
```

A mechanism that has never fired is still in `/mechanisms`, with
`activation_status`, `disabled_reason` and its unresolved pins. An operator has
to be able to see that a rule exists and why it is dark.

`docs/openapi-alerts.json` is generated from the running app
(`python -m scripts.export_alert_openapi`) and CI fails on drift. The
application's own `/openapi.json` remains the source of truth.

### Browser topology — decided: browser-visible scoped token

```
browser (ALERTS_READ_API_KEY) -> bubblegauge alerts API   [redacted projection]
operator (ADMIN_API_KEY)      -> /api/v1/admin/alerts/*   [message text, no-store]
```

H-05 offered two architectures. The chosen one is the **browser-visible scoped
token**, declared in config as `ALERTS_READ_TOKEN_IS_PUBLIC=true` so it is a
stated posture rather than an assumption — the server-side-proxy alternative is
still available by setting it false, which asserts the read key never reaches a
browser.

A static key in browser JavaScript is extractable, so it is treated as a
**public capability, not a secret**, and the four conditions the review attaches
to that choice are enforced rather than intended:

| condition | how |
|---|---|
| redacted projection only | no recipient, no provider correlation id, no raw provider error, no raw model output — **and no rendered message text** |
| rate-limited | every alert read route carries a limit; `ALERTS_PUBLIC_READ_RATE_LIMIT` (30/min) is the public ceiling, tighter than the operator read limit |
| rotates independently | `ALERTS_READ_API_KEY_PREVIOUS` keeps the outgoing key valid during overlap, so rotation needs no synchronized dashboard deploy; clearing it is its own edit |
| no silence / retry / render / admin | the scopes deliberately do **not** nest — the write key cannot read, the admin key is not a read key, and message text is not on the read surface at all |

That last row is why `GET /api/v1/alerts/renders/{id}` returns
`final_message: null` with a stated reason. Since no caller can present a
stronger scope to the read surface (there is one `X-API-Key` header), the text
lives at `GET /api/v1/admin/alerts/renders/{id}` behind the admin key, served
`no-store`. The dashboard still gets what it actually needs from a render: the
reviewed phrase codes chosen, the phrase-set provenance, the septet count, and
whether it fell back.

The app's CORS posture is GET-only, so the write routes are not browser-reachable
cross-origin without a separate, deliberate security review.

---

## 11. Rollout status and remaining evidence

Code completeness and rollout authority are intentionally separate. A feature
can be present, tested and schedulable while the committed artifact still
refuses to use it in production.

| stage | scope | current status |
|---|---|---|
| 0 | typed snapshot contract | **implemented and regression-gated** |
| 1 | schema, sidecar capture, pure evaluation, CAS, read API, replay | **implemented; this is the committed active stage** |
| 2 | `[PIN]` calibration, replay budgets, mandatory-event fixtures | gate machinery is implemented; real calibration/mandatory-event artifacts remain operator evidence and are not invented |
| 3 | deterministic P1/P2 delivery and weekly digest | planner, outbox, renderer, typed sender, dispatcher, reminders, digest and admission controls are implemented; promotion is blocked by the measured non-P1 volume failures below |
| 4 | reversible legacy daily-digest cutover | preflight/apply/confirm/rollback workflow is implemented; completion awaits two observed live weeks, two exact-window digests, healthy live components and an explicit operator configuration change |
| 5 | constellations and bundled P2 | evaluators, dominance and atomic multi-member bundling are implemented and stage-gated |
| 6 | EWMA / CUSUM | intentionally absent until immutable calibration and out-of-sample evidence exist |
| 7 | P3 enrichment and LLM A/B review | P3 inventory, code-only selector and actionability evidence trail are implemented; the selector is not invoked by the production dispatcher and retention depends on future A/B evidence |

The weekly digest is a real scheduler job, with quiet-week liveness recorded by
its durable component heartbeat,
missed-window recovery and digest-item outcome reconciliation. Actionability is
a real append-only admin workflow, and Stage-5 bundling is exercised by the
planner, renderer and concurrency tests. Watchdog, dispatcher, digest,
recovery, sidecar reconciliation and retention each expose a scored component
heartbeat with a cadence-appropriate freshness limit. The evaluator is scored
separately from its durable evaluation rows: in shadow or live mode the latest
run must be `COMMITTED`, have atomically applied its plan, and have a sane
completion timestamp no more than ten hours old. Disabled mode explicitly
reports the evaluator as not required.

The health projection also reports the latest and p95 evaluation duration, P1
enqueue-to-provider-attempt p95, rolling LLM cap/call/fallback evidence,
missing typed sidecars, overdue or malformed outbox holds, unresolved UNKNOWN
blockers, SQLite WAL/foreign-key/busy-timeout/RETURNING capabilities, the
Alembic revision, required partial indexes and immutability triggers, and live
artifact/promotion agreement. Missing scheduler components or required schema
objects are critical; sidecar gaps, overdue holds, unresolved ambiguities and
P1 latency above 60 seconds are degraded rather than silently green.

None of that bypasses rollout. At committed Stage 1 the live-admission floor
refuses before a sender is constructed. Shadow and dry-run paths exercise
eligible work without a provider call, while forward-looking Stage-3 replay
runs notification planning and records the actual resulting volume. Mandatory
event recall remains unmeasured because the frozen catalogue is deliberately
empty; filling it with invented events would be false evidence.

The LLM code selector stays dormant Stage-7/A-B work: the dispatcher neither
imports nor calls it, so neither shadow nor live alert delivery opens the
runtime gateway for alert phrasing.

Operational mechanisms use the strongest producer that actually exists. The
recompute watchdog captures and evaluates its own typed input; recovery and the
dispatcher persist their real outcomes. Inventory-only mechanisms whose typed
producer or calibration is unavailable remain disabled with an explicit
reason instead of pretending to evaluate.

---

## 11a. Retention: two horizons

```bash
ALERTS_MESSAGE_RETENTION_DAYS=400    # rendered message BODIES
ALERTS_METADATA_RETENTION_DAYS=800   # the audit trail
python -m app.alerts.cli retention [--dry-run]
```

The short sweep **redacts, it does not delete**. An `alert_render` row carries
the phrase-set provenance it was planned under, the render source, the septet
count and the validation results — metadata — alongside the text. Dropping the
row to expire the text would take the provenance with it, so the body is
emptied in place and `body_redacted_at` is stamped.

That is the one exception to render immutability, and it is enforced rather
than trusted: migration `0009` replaces the `alert_render_no_update` trigger
with one that permits *exactly* the transition `final_message -> ''` at the
same moment `body_redacted_at` goes from NULL to set. A rewrite still aborts, a
second redaction still aborts, and `gsm7_septets` is left alone so the length
of what was sent stays auditable after the text is gone.

Two things are never swept: a body whose delivery is not yet terminal (a retry
could still reuse that exact render), and events belonging to an open episode
(the trail explaining a still-firing mechanism is the one most likely to be
needed). Inverted horizons — metadata shorter than messages — are refused
outright rather than half-applied.

The dormant selector's attempt schema needs no raw-output sweep:
`alert_llm_attempt` stores only status, timing, hashes and an already-redacted
error string, never raw model output.

---

## 11a2. Promotion is not a delivery switch

Two questions that look like one, and must not be:

* **May these artifact bytes be accepted as a Stage-N artifact?** —
  `promotion_blockers`. It can pass at Stage 1, and passing means an operator
  accepted the exact rules and phrase bytes against evidence for that stage.
* **May this deployment construct a sender and deliver?** —
  `live_admission_blockers`. Below `LIVE_DELIVERY_STAGE` (3) the answer is
  always no, and neither passing evidence nor exact promotion lifts it.

Stage 1 has no sender by design. The dispatcher therefore refuses BEFORE
constructing one rather than after: building one and declining to use it would
break that promise quietly, since the object reads credentials and can open a
client. Separately, the dispatcher has no LLM path at any current stage: it
never imports or calls the dormant future Stage-7/A-B selector.

The floor was briefly removed on the reasoning that `ops.indicator_stale` and
`ops.coverage_degraded_info` are enabled at Stage 1 and could therefore send.
They are enabled and they cannot send — both are P4, and the planner maps P4 to
"API and log only", creating no delivery. Checking that the rules were enabled
without checking what they produce turned a refusal into an evidence check, and
a promoted Stage-1 artifact then cleared live admission.

## 11b. Open Stage 3 blocker: the ruleset exceeds its own non-P1 budget

Wiring the planner into the atomic apply (audit B-01) turned every non-P1
volume figure in the replay from "0 by construction" into a real count. On the
replayed history the stage-3 replay plans 13 deliveries and breaches both caps:

```
non-P1 24h  : 5   cap 3          BREACHED
non-P1 168h : 8   cap 6          BREACHED
non-P1 mean : 8.0 per 168h       UNMEASURED (see below)
```

`docs/alert-stage1-gate.json` records **stage 3 as FAILING**, and
`tests/test_alert_replay.py` pins both failures exactly so a new one cannot be
absorbed silently.

### Why a 76-hour window can prove a one-week breach

The replay covers 76 hours, which looks too short to judge a 168-hour cap. It
is not, and the asymmetry is worth stating because I got it wrong once and had
to be corrected.

A sliding-window **maximum is monotonic** in the window length. Observing 8
non-P1 messages inside 76 hours means every 168-hour window containing them
holds at least 8 — so the cap of 6 is broken, and no additional history can
undo it. A longer window only accumulates more.

The converse does not hold. Staying *under* a cap for 76 hours says nothing
about a week, so a non-breach on a short window is reported UNMEASURED rather
than passed.

The **mean** is different again: it is not monotonic, so a per-168h mean taken
from 76 hours is an arithmetic accident rather than a rate. It is the one
volume figure this window cannot establish.

### Deciding what to do about it

Two things are worth knowing:

* The history is a **coverage fixture** (`history.source` in the artifact),
  built to exercise every rule, so its density is a property of the fixture as
  much as of the ruleset. Stage 2 exists to re-measure on real captured
  sidecars. The breach is real; its magnitude is not a production forecast.
* Grouping is working. P2s firing in the SAME evaluation bundle into one
  delivery; these 13 are spread across 20 inputs over three days, so there is
  nothing for bundling to collapse. Bundling messages hours apart would mean
  delaying the first, which is not a trade the mandate makes.

This is an operator decision, not an engineering one. The options are to tune
the rules that generate the volume, to raise the caps deliberately with a
recorded reason, or to accept the breach for Stage 3 and re-measure on real
history before Stage 4. What must not happen is the caps being relaxed to make
a gate pass: the budget exists precisely to catch a ruleset that talks too
much, and it has just done its job on the first history it was ever able to
measure.

Until this is decided, Stage 3 cannot be promoted. That refusal is executable,
not advisory:

* the evidence carries the complete rules, phrase-set, and mandatory-event
  catalogue digests as stable grouped values; promotion checks the rule and
  phrase bytes at every stage and, from Stage 2 onward, checks the exact frozen
  catalogue plus its version/schema/count against the replay results;
* the promotion service refuses every recorded replay failure;
* runtime live admission rechecks the active stage, evidence and currently
  promoted bytes before constructing a sender; and
* wire-time delivery admission verifies that the exact planning ruleset was
  deliberately promoted with evidence, was not revoked, and is not from a
  stage above the current deployment.

`tests/test_alert_replay.py` pins both volume failures literally, and CI
regenerates the gate artifact. A changed ruleset therefore needs changed,
reviewable evidence; a version label alone cannot authorize different bytes.

---

## 11c. The weekly digest

The product this replaces sent one message every day at 10:00 whether or not
anything had happened. The replacement is event alerts **plus** a weekly
digest — and the digest half matters more than it looks, because the Stage 4
cutover switches the daily message off.

It runs Monday 08:30 Europe/Berlin and digests the window that has **closed**,
never the one in progress. One delivery per window, identified by the window
key itself, so a retried job, a restarted scheduler and a manual run all
converge on the same single message rather than three.

**A quiet week records liveness evidence, not a provider intent.** The
scheduler must still distinguish a genuinely quiet week from a digest job that
died, but the mandate also makes `TEST` the only delivery kind allowed zero
members. The `digest` component heartbeat is the current proof-of-life and
health turns a stale/missing heartbeat critical; an append-only
`digest_window_observed_quiet` scheduler event preserves the exact closed
window in the audit trail. No empty `DIGEST` row is fabricated, no quiet event
can count as one of Stage 4's successfully sent weekly digests, and an event
arriving late can still use that window because no delivery dedupe key was
burned. The reviewed `DIGEST_QUIET` template remains deterministically
validated, but it is not authority to bypass the member invariant.

Two consequences worth knowing:

* Every digest that reaches the provider has at least one represented episode
  member. A resolved episode remains valid retrospective evidence; a silenced
  member does not. Resolved members therefore remain eligible for silence
  checks until the provider intent is terminal, and the wire-time gate compares
  this exact represented set rather than only episodes still firing. The
  dispatcher and the `alert_delivery_requires_member` trigger independently
  enforce the transition-time member guard; the
  `alert_delivery_insert_requires_member` trigger closes the direct-insert
  bypass, while only `TEST` may carry zero rows.
* A pending DIGEST's represented member set and its attached digest-item ledger
  are one-to-one. Every represented or resolved member has exactly one
  `PLANNED` item; every silenced member has exactly one `CANCELLED` item; and no
  attached item may lack a member. Revalidation checks the complete graph
  before mutating it. Any missing, duplicate or mismatched binding cancels the
  provider intent as `DIGEST_MEMBER_ITEM_UNBOUND`, before rendering or sending,
  rather than deriving a count from whichever side still happens to exist.
* The digest reports a **count**, not a sample. One SMS is 160 septets and a
  week of events does not fit; a message quietly containing the first three of
  twelve would be lying about the other nine. The episodes are on record as
  delivery members for anyone who needs to know which ones.

Per mandate 9.2 the digest is reported in user load but does **not** consume
the non-P1 budget: it is a scheduled summary, not an interruption.

## 11d. The audited admin surface

The HTTP operator actions are admin-scoped and `no-store`:

* **`POST /api/v1/admin/alerts/evaluate`**, **`promote`** and **`recover`** —
  exercise one captured input, perform evidence-gated artifact promotion, or
  sweep stale evaluation leases respectively.
* **`POST /api/v1/admin/alerts/render`** — resolves the active reviewed
  `TEST_MESSAGE` and runs the exact TEST renderer, returning phrase-set
  provenance and validation. It creates no delivery/render row and cannot call
  a sender. **`GET /api/v1/admin/alerts/renders/{id}`** retrieves the immutable
  body of a render that really was persisted.

* **`POST /api/v1/admin/alerts/send-test`** — queues a memberless TEST delivery. TEST
  is the one kind allowed zero members (it is about the transport, not any
  market condition), it is outside the non-P1 budgets, and its body is the
  reviewed `TEST_MESSAGE` fragment. It goes through the ordinary dispatcher —
  same claim, same admission, same classification — because a test that
  bypassed the pipeline would prove the wrong thing.
* **`POST /api/v1/admin/alerts/deliveries/{id}/retry`** — the ONLY way past an
  UNKNOWN outcome. Requires an `Idempotency-Key`, an operator comment, and
  `acknowledge_duplicate_risk=true`; creates a NEW delivery with the same
  members and generation, `manual_retry_sequence` incremented (which changes
  the dedupe key), linked through `prior_unknown_delivery_id`. Same key with a
  different body is 409. Anything not UNKNOWN is refused: definite failures
  retry automatically, successes need nothing. Authorization keeps the
  ancestor's immutable transport status `UNKNOWN` but retires its open-blocker
  fields and notification-memory pointer in the same transaction. The pending
   child reserves that exact generation; if it also becomes UNKNOWN, it becomes
   the sole unresolved chain tip. Authorization also revalidates current
   episode and silence eligibility without rewriting the historical UNKNOWN
   row; if the frozen bytes no longer represent exactly what may be sent, it
   returns 409 and leaves the original blocker intact.
* **`POST /api/v1/admin/alerts/actionability`** — one human label per confirmed-SENT
  provider message (or per episode when no delivery is supplied), the Stage 7
  evidence. Dropped/undelivered members, TEST and DIGEST messages are refused;
  AMBIGUOUS is first-class so an unsure reviewer cannot inflate the KPI. A
  delivery-less episode label is qualitative evidence only; because it cannot
  identify a render source, it cannot enter a deterministic-vs-LLM A/B result.
* **`bubblegauge alerts cutover status|preflight|apply|confirm|rollback|confirm-rollback`** — the Stage
  4 gate made checkable. Preflight evaluates every mandate condition from the
  database (a two-week live span with recently created-and-sent market
  activity, one successful digest in each of the exact two closed weekly
  windows, zero suppressed P1
  activations and zero P1 holds, fresh healthy live-namespace heartbeats,
  reconciled live UNKNOWNs) and names each unmet one. `apply` records a request
  and prints the exact deployment change;
  it reports `applied=false`. After setting `DAILY_SMS_ENABLED=false` and
  restarting, `confirm --request-event …` rechecks the gate, observes that the
  explicit toggle and effective transport are off, then records completion.
  Rollback uses the same request/observation split, but requesting a rollback
  is never gated on the health that prompted it.

  Temporal evidence is bound to the intent, not just its provider timestamp or
  label. A market intent created before the 14-day window cannot become recent
  operation merely because an old queue is drained now. Likewise, ISO week
  `W` qualifies only when its digest was both created and sent after `W` closed
  at the following Monday 00:00 UTC and before the next Monday (and never after
  the preflight clock). Draining two historical windows together is recovery,
  not two weeks of observed digest cadence.

  A terminal `SENT` scalar is not sufficient historical evidence. Each
  qualifying digest must still have an exact one-to-one member/item graph and
  at least one non-silenced represented member. A surviving member must carry
  its render-proven `delivered` bit and a `DELIVERED` item stamped at the
  provider `sent_at`; a resolved retrospective member may remain dropped while
  its item records delivery; and a silenced member must remain undelivered with
  a `CANCELLED` item. This validation deliberately distrusts legacy/imported
  rows that predate today's transition triggers.

## 11e. Render-time truth (mandate 17.5)

A member is rendered under one of four statuses, and the dispatcher now
consults all four rather than the two easy ones: `STILL_FIRING` renders;
`RESOLVED_BEFORE_SEND` drops the member (telling somebody about a condition
that has cleared is worse than silence); `UNKNOWN_AT_RENDER` renders WITH the
data-quality caveat and claims no resolution; `MATERIALLY_CHANGED_BUT_ACTIVE`
renders trigger and current values rather than presenting stale numbers as
now. Phrase set v3.4 provides the reviewed `MATERIAL_CHANGE` clause and its
runtime-only `F_TRIGGER_VALUE` / `F_CURRENT_VALUE` slots. Both values are built
from one rule-authorized typed fact at the same reviewed display precision;
the complete trigger view, compatible current view, and every visible delta
remain separate in the render-context hash. Scheduling metadata such as
`F_NEXT_CHECK` cannot manufacture a material market change.

Current facts join a render only when their schema and methodology match the
trigger's (17.4) — otherwise the member renders from trigger facts with
`CONTEXT_STALE`, because mixing numbers computed two different ways into one
comparison is worse than admitting staleness. An archived phrase set that
predates the reviewed two-value clause remains recoverable: it keeps the
trigger facts and adds `CONTEXT_STALE`; runtime code never mutates or
retroactively extends its phrase bytes.

## 12. Replay (the Stage 1 gate)

Stage 1's gate is *deterministic replay; no PII; no scoring regression*.

```bash
python -m scripts.alert_replay --state-db /tmp/replay.db          # committed stage
python -m scripts.alert_replay --state-db /tmp/replay.db --stage 3 --out report.json
python -m app.alerts.cli dryrun --state-db /tmp/replay.db --from 2026-01-01
```

Exit 0 means every check the run could make held; exit 1 means one failed, or
the artifacts are invalid.

Three properties are structural rather than a matter of care:

- **It reads history, not the world.** Replay consumes persisted
  `alert_input_snapshot` rows and archived artifact bytes. `app/alerts/replay.py`
  imports no provider, no HTTP client and no sender, and a test walks the
  import graph so the guarantee cannot be quietly lost.
- **It cannot touch production.** State goes into a throwaway database opened
  through its own engine; the source database is only ever selected from. Mode
  is `dryrun`, which is its own state namespace — shadow and live never see a
  replay's episodes.
- **It is deterministic.** `now` comes from each input's own `computed_at`,
  never from a clock, and the summary carries no id, no run timestamp and no
  wall-clock duration. Two runs of the same history produce byte-identical
  JSON.

`--stage N` gates the rules at a rollout stage other than the committed one.
That is the reason replay exists: the evidence for advancing to stage N is what
stage N *would have done* over real history, and that cannot be gathered by
first advancing to it. It is confined to dry-run, and the re-stamped ruleset is
re-validated and re-hashed, so a forward-looking report can never claim the
committed ruleset's identity. Nothing in the production path chooses its own
stage.

### The committed evidence

`docs/alert-stage1-gate.json` is a replay of a synthetic history at stages 1,
3, and 4. CI regenerates it and fails on any difference:

```bash
python -m scripts.export_alert_stage1_gate           # write
python -m scripts.export_alert_stage1_gate --check   # CI: fail on drift
```

That check *is* the determinism gate. The committed bytes were produced by a
different process on a different machine; if the evaluator stops being
deterministic they stop matching. "Byte-identical" is scoped to **fixed code
and fixed committed inputs**. A reviewed evaluator, planner, rules, phrase-set
or fixture change is expected to regenerate and change this file; that visible
delta is the evidence under review, not a contradiction of determinism. The
artifact carries this contract, its generator and its exact command in the
machine-generated `generation` block.

The earlier reviewed regeneration moved the bound hashes to rules `v3.2.1`
plus phrase set `v3.4`, changed `held_budget` from `0 -> 3` at Stages 3/4 when
queued and held work began reserving the budget it can consume, and changed
`DATA_QUALITY_GUARD` from `1 -> 5` when replay began preserving suppression
evidence for every affected open episode. This completion changes the bound
phrase digest from
`e1300895-3fdbb27d-1377bef3-09bc4507-dc5b01a8-91d9cc22-8fa82837-69dece50`
to
`96d915f5-1a8fb496-aded9c1d-907abe76-b5d9be96-e23b8fc1-fab525fc-567d3196`
for the reviewed trigger/current clause, and bumps replay-summary schema
`1 -> 2` for strict per-window mandatory-event results. No existing behavioral
metric moved. Stage 3/4 retain the same two non-P1 cap breaches and now also
refuse explicitly because the deliberately empty catalogue leaves mandatory
recall unmeasured; Stage 1 remains green. None of these is a scoring input:
`frozen_methodology.json`, `MC_SEED=20260711`, and the score golden fixture are
outside this alert-only artifact and remain separately gated and unchanged by
the alert implementation.

`tests/fixtures/alert_replay_history.json` declares the **arc** — twenty
recompute slots through hold → trim → de-risk → recovery, with two blind slots
(one inside a firing episode) and one transient single-snapshot excursion —
and `alert_replay_history.py` builds the inputs from it. The serialized inputs
are deliberately *not* committed: observation keys, revision keys and
computation fingerprints are all derived from those twenty rows, so committing
them would trade a reviewable table for two thousand lines of content hashes
that no reviewer can check.

The gate artifact records both declared versions and the **complete** rules and
phrase-set hashes. The hashes are split into stable eight-character groups:
that preserves all 256 bits for byte binding while avoiding a bare high-entropy
token-shaped string in the committed artifact. Promotion joins the groups and
compares the full values. Per-run summaries still omit bare digests, and the
*Alert artifacts* CI step independently validates the files.

The history establishes regression coverage, **not** recall. Recall is a Stage
2 question and needs `config/alert_mandatory_events.v3.2.json`, which ships
empty on purpose: inventing historical windows would manufacture a recall
number nothing measured, the same failure mode as inventing a `[PIN]`.

Supplying a catalogue is an evidence-bearing action, so replay fails closed on
a missing file, invalid JSON, malformed envelope, duplicate or unsafe event
id, unknown rule, invalid priority, naive/reversed time window, negative slot
limit, missing field, or extra event field. A non-empty catalogue must declare
`frozen: true`. Recall then requires an activation of the exact rule at the
expected priority, inside that event's own UTC window, and no later than its
declared recompute-slot allowance. `NOT_EVALUABLE` is decided per event window,
not copied from a run-wide missing-input count. If every event window is blind,
or the deliberately empty shipped catalogue is used, recall remains explicitly
UNMEASURED. Stage 1 reports that honestly and remains eligible; a Stage 2+
replay fails, and promotion independently refuses unless the exact shipped
catalogue is non-empty, frozen, byte-bound, and detected at 100% across its
evaluable events. These checks validate operator-frozen evidence; they do not
create the real events, dates, or sources that Stage 2 still requires.

---

## 13. Epistemic posture

The headline is a structured 0–100 regime heuristic. It is **not a
probability**, it is uncalibrated, and the reference class is far too small for
honest probability calibration. Alert text never states crash odds, certainty,
buy/sell instructions or guaranteed outcomes — phrase validation constrains
the reviewed fragments and the final renderer applies the honesty lint before
anything can reach a wire. A model may only select reviewed codes.

Exactly-once SMS delivery is not promised. Ambiguous delivery outcomes are made
visible and handled conservatively rather than retried into duplicates.

The SMS budgets and priority classes are project **judgments** validated
through replay, not scientifically derived constants. Alarm-fatigue and
industrial-alarm literature (EEMUA 191, IEC 62682, ANSI/ISA-18.2, the Google
SRE material, Sentinel Event Alert 50) support prioritisation, grouping and
low-noise design; they do not derive a personal SMS budget.

Research and engineering specification — not investment advice.
