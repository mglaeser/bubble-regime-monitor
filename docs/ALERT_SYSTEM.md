# The bubblegauge alert system

An event-driven, stateful, replayable alert layer over the regime score. It
consumes committed scoring outcomes, exposes the complete alert state to a
frontend, and — once an operator explicitly turns it on — sends deterministic
SMS notifications. It never changes scoring.

**Current rollout: the governed deterministic delivery path and its operational
controls are implemented, and the committed ruleset is at Stage 3** (operator
decision 2026-08-27; its replay evidence passes in CI, section 12).
Sidecar capture is on (`ALERT_INPUT_CAPTURE`
defaults true — it records evidence and nothing else) while `ALERTS_MODE`
defaults `disabled`. Deterministic delivery, reminders, bundles,
watchdog/recovery and retention
are implemented and tested. They are not permission to send: a deployment
delivers only with `ALERTS_MODE=live`, and live mode runs only the promoted
artifact (section 11a2). The separate daily digest sends through its
configured transport, governed by its transport switches alone. See [Rollout status and remaining evidence](#rollout-status-and-remaining-evidence).

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
names the ruleset that opened it, and only the current ruleset decides
episodes (owner decision D2e): an episode another ruleset opened is resolved
as `RULESET_REPLACED`, at the promotion that replaces its rules or at the first
evaluation under a new candidate
(tests/test_alert_recovery.py::test_a_new_candidate_resolves_the_episodes_of_the_ruleset_it_replaces,
::test_an_evaluation_covers_exactly_the_current_ruleset). A live evaluation
applies only while its ruleset is still the promoted one: a promotion that
commits while it runs ends it CONFLICT with nothing applied
(::test_a_live_evaluation_applies_nothing_once_a_promotion_superseded_its_rules);
its apply transaction takes the write lock before that check. The run's
budget bounds the evaluation, not the apply, which commits what the
evaluation decided whatever the clock says
(::test_the_budget_bounds_the_evaluation_not_the_apply). Migration 0022
ended any live episode the deleted continuation had kept open under a ruleset
no longer promoted, as a promotion would
(tests/test_migrations.py::test_0022_resolves_live_episodes_left_under_a_ruleset_no_longer_promoted). A replay reads
persisted sidecars and archived bytes — it never asks a provider what the world
looks like now.

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
| P3 | API / log only (weekly digest deleted, D2a) | n/a | none | n/a |
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

`MESSAGE_LANGUAGE` (`en` | `de`; unset, each artifact speaks its own language)
selects the language of every operator message. A phrase set may carry more than one language — since v3.5
each fragment's `text` is an object keyed by language, and `meta.languages`
lists them — and the renderer writes the selected one; a language the promoted
set does not carry falls back to the set's own default (`meta.language`), which
the validation report states. Every language is held to the worst-case fit, and
the registry stores one digest for the whole set: switching language is a
setting, not a re-promotion. A value the settings do not admit (`fr`) is
refused where the language is resolved, never masked as the default: the
service does not start on it, and a validation run without a settings context
of its own reports `MESSAGE_LANGUAGE_INVALID`. The honesty lint reads both
vocabularies, and validation applies it to every fragment in every language
the set carries, not only the active one: a translation that would make the
renderer refuse every message using it cannot be promoted and then switched to.

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
ALERTS_WRITE_API_KEY=         # silences
ADMIN_API_KEY=                # alert reads (render text included), promotion, evaluation, recovery
```

Two scopes. **The alert reads are operator-only** (owner decision D3a,
2026-10-03): they take `ADMIN_API_KEY` and nothing else, and fail closed (503)
while it is empty or the placeholder, as the admin routes do. Silences take
`ALERTS_WRITE_API_KEY` alone. The separate read key, its rotation slot, the
public-read switch, the browser-token posture switch and the public-read rate
limit are retired settings (`ALERTS_READ_API_KEY`,
`ALERTS_READ_API_KEY_PREVIOUS`, `ALERTS_PUBLIC_READ`,
`ALERTS_READ_TOKEN_IS_PUBLIC`, `ALERTS_PUBLIC_READ_RATE_LIMIT`): one left in an
environment opens nothing and is named, as `DAILY_SMS_ENABLED` is below.

The daily digest is governed by its transport switches alone:
`IMESSAGE_ENABLED` selects iMessage when it is configured, and iMessage wins
when both configured transports are on; otherwise `SMS_ENABLED` selects
sipgate; there is no send-failure fallback. It has no retirement switch: the
`DAILY_SMS_ENABLED` migration alias went with the Stage-4 cutover it served
(owner decision D2c). A `DAILY_SMS_ENABLED` left in an environment - the
process environment or the `.env` file the settings read - changes nothing,
and never in silence: the boot logs `retired_setting_present`, the
alerts preflight fails `no_retired_settings`, and the alert health projection
names it. Turning the alert system on never changes the digest's transport,
and a digest without one is named by the alert health projection ("the daily
digest has no transport").

Volume, lease, retention and LLM settings live in `app/config.py`; each has a
safe default. Alert phrasing has no model path: configuring the runtime
gateway activates only the judgment/digest paths. Configuration alone never
grants delivery permission: live mode runs only the promoted artifact, and
promoting it is an operator action.

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
evaluation conflicts/timeouts, and UNKNOWN deliveries. Parquet export uses
pyarrow, a regular dependency since 2026-09-28.

`--promote` is the only way to promote from the CLI, and
`POST /api/v1/admin/alerts/promote` the only way over HTTP. Nothing promotes as
a side effect of a boot, a deploy or a validation run, and **promotion does not
change `ALERTS_MODE`**. Promotion validates the files structurally, registers
their exact bytes and marks them PROMOTED, superseding the previous promotion;
it reads no evidence (owner decision D2d) - the CI replay gate is the evidence.

In the same transaction, promotion resolves every open episode a different
ruleset opened, in every mode (owner decision D2e): the episode becomes
RESOLVED with the reason `RULESET_REPLACED`, and its `episode_resolved` event
names the promoted ruleset as the cause (causation `RULESET`). Promotion plans
no message. An alert still queued for such an episode is withdrawn at dispatch.
A condition that is
still true opens a new episode at the next evaluation, under the promoted rules
and from the start - a rule that needs two confirmations counts them again - and
a repeat within the cooldown of an alert already sent stays suppressed, because
the cooldown is keyed without a rules hash. A transition rule waits for its
next transition (tests/test_alert_recovery.py::test_promotion_resolves_the_replaced_rulesets_open_episodes,
::test_a_promotion_withdraws_the_replaced_rulesets_queued_alert_and_sends_nothing,
::test_a_still_true_condition_reopens_under_the_promoted_ruleset).

### Crash recovery

| state | meaning | action |
|---|---|---|
| lease live | in progress | leave it alone |
| lease expired, `plan_applied=0` | died before applying anything | `ABANDONED`; safe to retry under the same logical identity |
| lease expired, `plan_applied=1` | applied a plan but never recorded finishing | **never auto-repaired** — needs a human |

`recover-leases --once` is the separate delivery-lease sweep. An expired
`LEASED` row with no `request_started_at` is definitely pre-wire and returns to
`RETRY_DUE`; an expired `SENDING` row, or any row whose request had started,
becomes `UNKNOWN` because the provider may have accepted it. `UNKNOWN` is
terminal: nothing sends it again (section 11d).

`reconcile-sidecars` lists committed snapshots with no sidecar. A gap is
reported, never quietly filled: a sidecar reconstructed after the fact is
marked `RECONSTRUCTED` and never counts as successful mandatory-event recall.

---

## 10. The API

Reads and the admin actions take `ADMIN_API_KEY`; silences take
`ALERTS_WRITE_API_KEY`. Errors are the service's one format (owner decision
D3d, 2026-10-03): `application/json` `{"detail": ...}`, as on every route. Reads
set no `ETag` and answer no conditional request (owner decision D3c,
2026-10-03); the render read and the mutations are `Cache-Control: no-store`.

```
GET  /api/v1/alerts/overview          one screen: states, open episodes, pointers
GET  /api/v1/alerts/mechanisms        every rule instance, including dark ones
GET  /api/v1/alerts/mechanisms/{fp}   one mechanism, addressed by fingerprint
GET  /api/v1/alerts/rules/{id}/instances
GET  /api/v1/alerts/episodes[/{id}]   {id} includes its event trail
GET  /api/v1/alerts/events            cursor-paginated, stable ordering
GET  /api/v1/alerts/latest            fired and sent as SEPARATE pointers
GET  /api/v1/alerts/deliveries[/{id}] redacted
GET  /api/v1/alerts/renders/{id}     with its message text, no-store
GET  /api/v1/alerts/ruleset
GET  /api/v1/alerts/health
GET  /api/v1/alerts/silences
POST   /api/v1/alerts/silences        Idempotency-Key honoured; 409 on reuse with a different body
DELETE /api/v1/alerts/silences/{id}
POST /api/v1/admin/alerts/evaluate    one sidecar, shadow by default
POST /api/v1/admin/alerts/promote
POST /api/v1/admin/alerts/recover
POST /api/v1/admin/alerts/render      validate reviewed TEST bytes; never persist/send
POST /api/v1/admin/alerts/send-test   queue an audited TEST delivery
```

The episode, event and delivery listings run newest first, `limit` rows a page
(100 by default, at most 500). A full page carries `next_cursor`, the position
of its last row, `<RFC 3339 time>~<id>`; passed back as `cursor`, it continues
the listing strictly after that row. The cursor is a plain keyset position, not
a capability (owner decision D3b, 2026-10-03): unsigned, with no expiry and no
binding to a listing or a filter. One that names no position is a 422.

A mechanism that has never fired is still in `/mechanisms`, with
`activation_status`, `disabled_reason` and its unresolved pins. An operator has
to be able to see that a rule exists and why it is dark.

`docs/openapi-alerts.json` is generated from the running app
(`python -m scripts.export_alert_openapi`) and CI fails on drift. The
application's own `/openapi.json` remains the source of truth.

### Operator-only reads (owner decision D3a, 2026-10-03)

H-05 chose a browser-visible read token (2026-08-16); D3a replaced it: the
alert reads take `ADMIN_API_KEY`, per handler, as the admin routes do. The
render read returns the message text, `no-store`, so its admin twin
`GET /api/v1/admin/alerts/renders/{id}` went; like every alert read it is scoped
to the active mode and profile. Every alert read keeps its 60/min limit; the
public-read limit `ALERTS_PUBLIC_READ_RATE_LIMIT`, which nothing applied, went
too (D3e).

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
| 1 | schema, sidecar capture, pure evaluation, CAS, read API, replay | **implemented** |
| 2 | `[PIN]` calibration, replay budgets, mandatory-event fixtures | gate machinery is implemented; real calibration/mandatory-event artifacts remain operator evidence and are not invented |
| 3 | deterministic P1/P2 delivery and weekly digest | planner, outbox, renderer, typed sender, dispatcher and reminders are implemented; the weekly digest is deleted (owner decision D2a, 2026-10-03, section 11c); **this is the committed active stage** (operator decision 2026-08-27, section 11b), and its replay evidence passes |
| 4 | legacy daily-digest cutover | deleted with its switch (owner decision D2c, 2026-10-02): the daily digest is the message engine's product and is not retired. The gate's CLI never checked the switch - `alerts cutover apply` recorded the operator's intent and printed "set DAILY_SMS_ENABLED=false in the deployment environment", its one importer was that CLI, and the production database holds no cutover event (read-only, 2026-10-02) - and the `DAILY_SMS_ENABLED` alias, the cutover's one purpose, is gone (unset on the production host). The digest's transports decide; `/api/v1/alerts/health` names a digest without one |
| 5 | constellations and bundled P2 | evaluators, dominance and atomic multi-member bundling are implemented and stage-gated |
| 6 | EWMA / CUSUM | intentionally absent until immutable calibration and out-of-sample evidence exist |
| 7 | P3 enrichment and LLM A/B review | the dormant code-only selector and the actionability evidence trail are deleted (owner decision D2b, 2026-10-02): the dispatcher never called the selector, and neither table ever held a row. P3 itself is API and log only since the weekly digest went (owner decision D2a, 2026-10-03) |

Stage-5 bundling is exercised by the
planner, renderer and concurrency tests. Watchdog, dispatcher,
recovery, sidecar reconciliation and retention each expose a scored component
heartbeat with a cadence-appropriate freshness limit. The evaluator is scored
separately from its durable evaluation rows: in shadow or live mode the latest
run must be `COMMITTED`, have atomically applied its plan, and have a sane
completion timestamp no more than ten hours old. Disabled mode explicitly
reports the evaluator as not required.

The health projection also reports the latest and p95 evaluation duration, P1
enqueue-to-provider-attempt p95, rolling LLM cap/call/fallback evidence,
missing typed sidecars, overdue or malformed outbox holds, the UNKNOWN
deliveries, SQLite WAL/foreign-key/busy-timeout/RETURNING
capabilities, the Alembic revision, required partial indexes and immutability
triggers, and live artifact/promotion agreement. Missing scheduler components
or required schema objects are critical; sidecar gaps, overdue holds and P1
latency above 60 seconds are degraded rather than silently green. An UNKNOWN
delivery is counted and degrades nothing: it is terminal, and no operator step
awaits it (owner decision D2f, section 11d); a dispatch pass whose send ends
UNKNOWN reports its heartbeat critical.

None of that bypasses rollout. In live mode the evaluation, the dispatch job
and the message engine load through `load_active_for_mode`, which refuses a
candidate that is not the promoted artifact; the dispatch job refuses before a
sender is constructed. Shadow and dry-run paths exercise eligible work without
a provider call, while the Stage-3 replay runs notification planning and
records the actual resulting volume. Mandatory event recall is measured
against the five frozen catalogue entries (section 11b).

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
needed) or to a delivery not yet terminal. `UNKNOWN` is terminal (owner
decision D2f): nothing sends it again, so its body and its events expire on
these horizons as any settled delivery's do. Inverted horizons — metadata
shorter than messages — are refused outright rather than half-applied.

---

## 11a2. Promotion is not a delivery switch

Promotion marks the exact rules and phrase bytes that live mode runs, and it
reads no evidence: it takes only the artifacts this image ships
(`config/alert_rules.v3.2.yaml`, `config/alert_phrases.v3.5.json`), which are
exactly the bytes the CI replay gate checks. A candidate elsewhere - a
variant, or a file a host places at `ALERTS_RULES_PATH` - is refused by `alerts
validate --promote` (exit 1) and by `POST /api/v1/admin/alerts/promote` (409),
and runs in shadow mode only
(tests/test_alert_promotion.py::test_the_cli_promotes_only_the_shipped_bytes_the_replay_gate_checks).
`ALERTS_MODE=live` is the delivery switch, set by hand on the host. In live mode the evaluation, the dispatch job and the message engine
load through `load_active_for_mode`, which refuses a candidate that is not the
promoted artifact: the dispatch job raises before it constructs a sender, and
its heartbeat turns health critical.

There is no stage floor and no evidence check at promotion or at runtime
(owner decision D2d, 2026-10-03): the CI replay gate is the evidence - `python
-m scripts.export_alert_stage1_gate --check`, a blocking step of
`.github/workflows/ci.yml` (pinned in tests/test_alert_replay.py), so bytes
whose replay no longer matches the committed evidence cannot merge. The
non-P1 budget it judges is code, not a host setting (`app/alerts/budgets.py`
`LIMITS`: target 2, caps 5 per 24 h and 8 per 168 h), so no host runs caps the
replay never judged; the old `ALERTS_NON_P1_*` keys are retired. On leaf
(read-only, 2026-10-03) the promoted ruleset - rules v3.2.3, phrase set v3.5 -
is byte-identical to the committed artifact that gate checks, and no delivery
is queued. A host that overrides `ALERTS_RULES_PATH`, `ALERTS_PHRASE_PATH` or
the volume caps runs what CI never replayed. Stages 1 and 2 enable only the P4
ops rules, and the planner maps P4 to "API and log only", creating no delivery.
Separately, the dispatcher has no LLM path at any stage.

What the runtime no longer checks, by that decision: the evidence and the
stage, and nothing is re-checked at the wire - a demotion after planning no
longer withholds work already queued, and a promotion change reaches the wire
at the next dispatch pass, at most one pass later (the job runs at least every
20 s). What stops a live send now: ALERTS_MODE other than `live`; a silence;
the dispatch job's own check before every pass - a candidate that is not the
promoted artifact refuses the pass before any sender exists
(tests/test_alert_promotion.py::test_the_live_dispatch_job_refuses_an_unpromoted_candidate);
and the claim, which judges the ruleset that planned the work by its
promotion, never by re-reading evidence: in live mode it takes only work
planned under a ruleset that was promoted and is not revoked - REVOKED
outranks a past promotion - however and whenever the work was queued, and a
ruleset superseded since still finishes what it planned, except an alert whose
episode the superseding promotion resolved, which is withdrawn (section 9)
(tests/test_alert_promotion.py::test_live_dispatch_sends_no_work_planned_under_rules_nobody_promoted,
::test_live_dispatch_sends_work_planned_under_a_promoted_ruleset). The
listing and the claim's own conditional UPDATE carry the same condition, so a
ruleset revoked after the listing is not claimed
(::test_a_ruleset_revoked_after_the_listing_is_not_claimed); a delivery
already claimed goes out. To stop live sends, set ALERTS_MODE to anything but
`live`, or add a silence.
Live work is also planned under the ruleset promoted at that moment:
evaluation and the admin send-test load through
`load_active_for_mode`, so in live mode a ruleset that was never promoted
plans nothing (tests/test_alert_api.py::test_a_live_send_test_is_planned_under_the_promoted_ruleset_only);
a row planned under a ruleset since superseded was authorized when it was
planned. Every promoted ruleset was promoted through the service: migration
0021 withdrew any promotion made before promotion checked evidence - it
authorised no live work under 0020 either - before it dropped the stamp; a
withdrawn PROMOTED row leaves live mode refusing to load until the operator
promotes again (tests/test_migrations.py::test_the_stamp_migration_withdraws_a_promotion_made_without_it).
On leaf (read-only, 2026-10-03) both registry rows - the PROMOTED one (rules
v3.2.3, phrase set v3.5) and the SUPERSEDED v3.2.2 - carried the stamp. Production
holds no REVOKED ruleset and held no queued delivery when the gate went
(read-only, 2026-10-03).

## 11b. The former Stage 2 blocker, resolved by named operator decisions

Until 2026-08-27 the stage-3 replay FAILED on its own non-P1 volume caps
(24h 5 > 3, 168h 8 > 6 on the coverage history) and, later, on the empty
mandatory-event catalogue. Both were held open as operator decisions rather
than absorbed, and on 2026-08-27 the operator made them ("I want that it
takes over now"):

* **Caps raised 3→5 / 6→8**, recorded in `app/config.py` beside the values.
  The quiet-regime target stays 2. The evidence artifact records the limits
  each verdict used, and admission refuses caps the evidence never saw — so
  the raise is bound into the gate, not slipped past it.
* **The mandatory-event catalogue frozen** at five pipeline-recall entries the
  coverage history genuinely activates (recall 5/5). `override.fires` and
  `structure.s3_tier_150` are named IN the catalogue as known Tier-A coverage
  gaps — must-never-miss rules with no staged arc yet — so their absence
  reads as outstanding evidence work, not demotion.
* **`active_stage` committed to 3** in the same decision. Committing the stage
  is still not delivery: the deployment sends only after the operator promotes
  the exact artifact through the evidence-gated service and sets
  `ALERTS_MODE=live` by hand.
* **The two-week/two-digest observation gates removed from cutover preflight**
  ("I don't want a two weeks clock"). The safeguard set stands in their place:
  component heartbeats bounded on both sides, any-open-UNKNOWN blocking, the
  host-side outage notifier, and the weekly digest's own liveness event. Their
  absence is pinned by a test exactly as their presence was.

Superseded by owner decision D2d (2026-10-03): neither promotion nor the
runtime reads the evidence or compares the caps with it. The CI replay gate is
the evidence, replayed under the default caps. And by owner decision D2f
(2026-10-03): an UNKNOWN delivery blocks nothing (section 11d). And by owner
decision D2a (2026-10-03): the weekly digest, its liveness event with it, is
deleted (section 11c).

## 11c. The weekly digest, deleted

Owner decision D2a (2026-10-03) deleted the weekly P3 digest. It could never
send: all 17 P3 rules (14 rules and 3 constellations) are disabled in every
committed revision of the ruleset and gated to Stage 7 (`override.first_flag`
to Stages 5-7), so no P3 episode could activate and no digest item could be
written. Its scheduler job, its `alerts digest` command and the `digest`
component heartbeat went with it, so health no longer expects that heartbeat.
A P3 activation is API and log only, like a P4 (section 6): the planner notes
it and plans nothing. `DIGEST` remains stored delivery-kind vocabulary
(`ck_alert_delivery_kind` admits it). Migration 0024 drops the
`alert_digest_item` table and the `digest` heartbeat row, and takes the
digest's branch out of the member trigger: a member dropped before the send
(resolved, say) no longer represents a `DIGEST` row, which, like every kind but
TEST, needs a member that was not dropped (section 7). It first cancels every
`DIGEST` delivery that has not gone out (queued, due for a retry or leased) as
`WEEKLY_DIGEST_REMOVED`, so none reaches the wire, and ends one in flight
`UNKNOWN`, so no `DIGEST` row is left that could move to `SENDING` or `SENT`
(tests/test_migrations.py::test_0024_cancels_a_weekly_digest_still_queued), and
it refuses while the table holds a row. The daily digest is a different
message and is unchanged.

## 11d. The audited admin surface

The HTTP operator actions are admin-scoped and `no-store`:

* **`POST /api/v1/admin/alerts/evaluate`**, **`promote`** and **`recover`** —
  exercise one captured input, promote the validated artifacts on disk (no
  evidence is read, owner decision D2d), or sweep stale evaluation leases
  respectively.
* **`POST /api/v1/admin/alerts/render`** — resolves the active reviewed
  `TEST_MESSAGE` and runs the exact TEST renderer, returning phrase-set
  provenance and validation. It creates no delivery/render row and cannot call
  a sender. The immutable body of a render that really was persisted is read
  at **`GET /api/v1/alerts/renders/{id}`**, under the same admin key (owner
  decision D3a).

* **`POST /api/v1/admin/alerts/send-test`** — queues a memberless TEST delivery. TEST
  is the one kind allowed zero members (it is about the transport, not any
  market condition), it is outside the non-P1 budgets, and its body is the
  reviewed `TEST_MESSAGE` fragment. It goes through the ordinary dispatcher —
  same claim, same admission, same classification — because a test that
  bypassed the pipeline would prove the wrong thing.

No route sends an UNKNOWN delivery again (owner decision D2f, 2026-10-03).
Until then `POST /api/v1/admin/alerts/deliveries/{id}/retry` was the only way
past an UNKNOWN outcome - a new delivery under a new key, which an operator
authorised with a duplicate-risk acknowledgement - and an UNKNOWN blocked its
notification generation until an operator retried it. Both are deleted, with
their columns (migration 0023), and nothing retries an ambiguous attempt in
their place: a send that may have landed ends `UNKNOWN` on every transport.
D2f as first planned also retried an ambiguous iMessage attempt automatically
under its own idempotency key; that was dropped because an automatic retry can
send twice - across an idempotency-domain change (a transport switch, an
`IMESSAGE_API_KEY` rotation) or when a retry still waiting for the proxy's
verdict is cancelled and its digest items are carried again - so `UNKNOWN` is
terminal instead. It is a state, not a workflow: never claimed, recovered or
sent again, and blocking nothing
(tests/test_alert_delivery.py::test_unknown_is_a_terminal_state_nothing_claims_or_recovers).
The same generation of the same episode, planned again, carries the UNKNOWN
row's dedupe key and is that intent, not a second one; a new episode is
planned as usual
(tests/test_alert_end_to_end.py::test_an_unknown_alert_blocks_nothing_and_the_next_episode_is_planned,
::test_the_same_reminder_generation_is_not_planned_again_after_an_unknown).
A reminder is planned as after a definite failure: nothing confirmed advances
the notification memory, so its delay counts from the instance's last
confirmed send, and when that send is older than the delay, an episode whose
first alert ended UNKNOWN is reminded at the next evaluation (while the
instance has a reminder left). An UNKNOWN body and its events expire on the
normal horizons (section 11a). Health counts UNKNOWN deliveries, and a
dispatch pass whose send ends UNKNOWN reports its heartbeat critical; nothing
awaits an operator. Production held no UNKNOWN delivery, no
manual retry and no replanning block when they went (read-only, 2026-10-03).

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

Owner decision D2f (2026-10-03) bumps replay-summary schema `2 -> 3`: the
summary's `unknown_blocks` and `p1_bypasses_of_unknown` went with the
replanning block they counted. Both were 0 in every run, and no other number
moved.

Owner decision D2a (2026-10-03) removes the summary's `digest_items` with the
weekly digest. It was 0 in every run, and no other number moved.

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
token-shaped string in the committed artifact. `--check` regenerates them from
the committed files, so a rules or phrase-set edit that does not regenerate the
artifact fails CI; nothing at runtime reads them (owner decision D2d). Per-run
summaries still omit bare digests, and the *Alert artifacts* CI step
independently validates the files.

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
replay fails unless the shipped catalogue is non-empty, frozen and detected at
100% across its evaluable events, and the artifact binds the catalogue's bytes,
so `--check` fails when the catalogue changes without a regeneration. These
checks validate operator-frozen evidence; they do not
create the real events, dates, or sources that Stage 2 still requires.

---

## 13. Epistemic posture

The headline is a structured 0–100 regime heuristic. It is **not a
probability**, it is uncalibrated, and the reference class is far too small for
honest probability calibration. Alert text never states crash odds, certainty,
buy/sell instructions or guaranteed outcomes — phrase validation applies the
honesty lint to every fragment in every language, and the final renderer
applies the same lint to the body before anything can reach a wire. Denying
the noun ("keine Wahrscheinlichkeit", "not a probability") is the one honest
use of it and passes; the same stem anywhere else does not. A model may only
select reviewed codes.

Exactly-once SMS delivery is not promised. Ambiguous delivery outcomes are made
visible and handled conservatively rather than retried into duplicates.

The SMS budgets and priority classes are project **judgments** validated
through replay, not scientifically derived constants. Alarm-fatigue and
industrial-alarm literature (EEMUA 191, IEC 62682, ANSI/ISA-18.2, the Google
SRE material, Sentinel Event Alert 50) support prioritisation, grouping and
low-noise design; they do not derive a personal SMS budget.

Research and engineering specification — not investment advice.
