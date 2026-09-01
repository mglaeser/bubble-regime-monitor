# MESSAGE_ENGINE — LLM-written operator messages (Phase C)

The engine curates one prompt per message trigger just-in-time, grounds it in
API data plus local context, asks the configured LLM route for the sentence,
validates the result against a hard per-channel contract, and hands the final
text to delivery. It is a NEW subsystem in its own `MESSAGE_ENGINE_*` settings
namespace (ruling Q42); `app/alerts/llm_selector.py` is left untouched (Q41).

## Why this is not llm_selector

`llm_selector` implements a deliberate containment: the model selects CODES,
never writes a digit, and the renderer interpolates every fact. The program
asks for the opposite — the model writes the sentence. Ruling Q25 anticipated
the conflict and directs that the no-LLM claim in `docs/ALERT_SYSTEM.md` be
consciously amended rather than quietly contradicted, with "P1-style
exemptions to define". Both paths therefore coexist: nothing about the
existing alert render path changes.

## Decision 1 — compose BEFORE the delivery is queued

The pacing rules (>=5 min between LLM requests, up to 3 content iterations,
>=2 min after a technical error) mean a single message can take ~15 minutes to
compose. The dispatcher holds a 120 s lease (`alerts_dispatch_lease_s`), polls
on >=20 s, runs `max_instances=1`, and treats a render as immutable and never
re-rendered on retry. Composing inside a claimed delivery would therefore
either expire the lease or wedge the only worker — and a wedged worker delays
P1, which is the one thing the alert system refuses to allow.

So: **the engine composes first and queues an already-final text.** Delivery
keeps its existing semantics (immutable render, retry transports the same
bytes). Composition latency is bounded by the engine's own governor and never
by a lease.

## Decision 2 — the P1 exemption

A P1 is the message that must arrive. The engine never sits on its critical
path:

- the deterministic/evergreen text is produced FIRST and is what gets sent;
- an LLM attempt may replace it only if it returns AND validates before the
  trigger's compose deadline;
- if the pacing governor would delay a P1 for any reason, the governor yields:
  the P1 goes out immediately with the deterministic text.

Pacing, budget and breaker state can delay or downgrade phrasing. They can
never delay delivery of a P1.

## Decision 3 — state lives in the DB (ruling Q36, AMENDED)

Q36 originally read "JSON STATE FILE ONLY for governor/breaker (no DB
dependency)" and was tagged a deliberate deviation. This engine was built
DB-backed instead, and the compliance audit of 2026-08-29 caught that as a
silent reversal. The owner was asked and **amended Q36** to permit state
derived from the `message_engine_attempts` rows, because:

- Q46 already mandates those audit rows in the database, so a second store
  would give two records of the same facts that can disagree; and
- the row insert is what takes the write lock in `reserve()`, closing the
  two-worker race the panel found in round 1 — a plain file cannot do that
  without additional locking machinery.

Recorded here because the reversal was originally undocumented. The process
rule that failed is worth restating: a better engineering argument is grounds
to REQUEST an amendment, never to ship the opposite quietly.

## Decision 4 — a separate render/audit table, not a relaxed CHECK

`AlertRender.gsm7_septets` is NOT NULL with `CHECK (0..160)` and
`app/alerts/gsm7.py:septets()` RAISES on emoji. An iMessage body of up to 200
code points carrying up to two emoji cannot compute that column at all.
Relaxing the constraint would weaken a live SMS invariant to make room for a
different channel. Instead the engine persists its own rows: code-point count,
emoji count, channel, source (`generated` | `fallback` | `deterministic`),
every attempt including timeouts and rejections (Q46: full rows, 90-day
retention). The alert schema is untouched.

## Decision 5 — admission is a GATE, not a re-modelled delivery

Ruling Q25 requires every outbound message to pass alert-system delivery
admission. Several triggers (daily digest, failure/recovery/stuck alerts, the
breaker notice, host outage) are not rules in `config/alert_rules.v3.2.yaml`,
and `AlertDelivery.planning_rules_sha256` is a NOT NULL FK into the ruleset
registry. Synthesising fake rules to satisfy a foreign key would put rules in
Python, which the rule-as-data invariant forbids, and would force a rule
version bump plus a re-promotion for a purely cosmetic reason.

The engine therefore calls the SAME admission check the dispatcher calls
(`live_admission_blockers`) before any send, and refuses identically when it
returns blockers — including the Stage-3 floor. Rule-backed triggers continue
through the normal planner/dispatcher path unchanged.

Implemented in `app/message_engine/gate.py`. Three things about it are load
bearing, and each is pinned by a test that goes red when the control is
reverted:

**It is checked immediately before the wire, not once per compose.** The
dispatcher reaches the same conclusion in `withdrawn_admission`: admission can
turn false in the gap, and a demotion is precisely the change an operator
makes when they want messages to stop. A compose can legitimately take fifteen
minutes, so a gate checked at the start of one is a gate with a fifteen-minute
hole in it.

**A P1 does NOT bypass it.** This is the one place the P1 exemption stops, and
the distinction is worth stating plainly: decision 2 exempts a P1 from pacing,
budget and breaker because those govern PHRASING, and delaying the message that
must arrive in order to think about wording is indefensible. Admission is not
phrasing — it is whether this deployment may put bytes on a wire at all. A P1
that bypassed it would make the Stage-3 floor advisory, because a deployment
held below the delivery stage would still send its most urgent messages. The
gate records `priority` and never branches on it.

**A gate that cannot be evaluated is a blocker, not an absence of blockers.**
`live_admission_blockers` reports rather than raises in the paths its authors
anticipated, but `promotion_blockers` runs unguarded on a payload that only had
to be a `dict` to reach it, so a malformed evidence artifact can still raise
out. An escaping exception would reach the engine's caller, which classifies
exceptions as `TECHNICAL_ERROR` and retries — silently converting "this
deployment is not authorised to send" into "try again in two minutes", forever.

## The six governor constants (ruling Q42 — settings, these defaults)

| Setting | Default | Meaning |
|---|---|---|
| `MESSAGE_ENGINE_MIN_INTERVAL_S` | 300 | floor between two LLM requests |
| `MESSAGE_ENGINE_FORMAT_RETRY_S` | 30 | pause for a format-only retry |
| `MESSAGE_ENGINE_MAX_CONTENT_ITERATIONS` | 3 | content attempts before fallback |
| `MESSAGE_ENGINE_TECHNICAL_BACKOFF_S` | 120 | pause after a 4xx/5xx/timeout |
| `MESSAGE_ENGINE_BREAKER_STRIKES` | 5 | consecutive technical errors before all-fallback |
| `MESSAGE_ENGINE_BREAKER_COOLDOWN_S` | 86400 | all-fallback dwell before retrying |

Plus `MESSAGE_ENGINE_ENABLED` (default false — inert until switched on, Q42)
and `MESSAGE_ENGINE_DAILY_BUDGET` (default 100, ruling Q40; at cap the
evergreen fallback is used and the exhaustion is reported in the next digest).

## Channel contract (rulings Q27, Q29, Q30)

English only. SMS: <=150 characters, GSM-7-safe, no emoji, septet-accurate
counting. iMessage: <=200 code points, at most 2 emoji from the allowlist.
Rejected output is retried, never transliterated. Numerals must appear
verbatim from the grounded facts; the banned lexicon (probability, chance,
likely, buy, sell, guaranteed, ...) is rejected — with band names such as
"hold" exempt, because a band name is a state, not an instruction.

## Decision 6 — "did not ask" is not "tried and failed" (round 32)

The breaker exists to notice a broken PROVIDER. That only works if the rows it
reads distinguish two things that both end in the same evergreen sentence:

| the engine… | outcome | strike? | closes the compose? |
|---|---|---|---|
| asked and gave up (iterations exhausted) | `FALLBACK_USED` | yes | yes |
| asked and the gateway failed | `TECHNICAL_ERROR` | yes | no |
| asked and the answer was refused | `CONTENT_/FORMAT_REJECTED` | via the closing marker | no |
| **was not permitted to ask** | `NOT_ASKED` | **no** | **no** |

`NOT_ASKED` covers the pacing floor, the engine switched off, a P1 rendering
deterministically, the daily budget, and a breaker already open. No model call
is made and no attempt is spent, so it is an audit row and nothing else —
`content_attempts()` skips it rather than counting it or stopping at it.

Originally every one of these wrote `FALLBACK_USED`, and the cross-vendor panel
refused the PR over it twice, independently. Five triggers inside the
five-minute floor — an ordinary burst — wrote five strikes and opened the
24-hour breaker; while it was open each suppressed trigger wrote another, so
the state fed itself; and a single gateway timeout cost two strikes, because
the `TECHNICAL_ERROR` row and the fallback row both counted.

The rule the taxonomy encodes: **a refusal the engine issued to itself is not
evidence about the provider.** Only an answer the provider actually gave, or
failed to give, may move the breaker.

### What `NOT_ASKED` cost to introduce (round 33)

Adding an outcome is not a local change, and the panel found four places that
had silently assumed every row was an attempt:

* **Anything that filters rows must filter IN THE QUERY.** `content_attempts()`
  excluded NOT_ASKED in Python, after `.limit()`, so a run of paced refusals
  filled the scan window and the real attempts fell off the end — 200 of them
  hid three rejections and let a call through past the content cap. Round 13
  fixed exactly this for `BUDGET_SKIPPED`; the warning comment sits four lines
  above the code that repeated it.
* **"The newest row" is rarely the question.** `_last_failure_class()` reads
  one row to decide whether the short format retry applies. Every rejection is
  now followed by the NOT_ASKED row of its own fallback, so that query always
  answered None and the 30-second retry never fired. NOT_ASKED is excluded
  there too: the question is how the last ATTEMPT ended.

## Decision 7 — the fallback is a CONTRACT, not a consolation

The generated path is validated and rejected on overrun. The fallback path had
no check at all — and it is the path taken when something is already wrong, so
it is the last one that should be trusted blindly. Sweeping every slot of every
shipped fallback with a hostile fact produced 40 channel-contract violations,
worst a 432-character body against a 150-character SMS cap, plus newline
injection that turns one message into several.

Two rules now hold for it:

* **Substituted values are data, and one line of it.** Newlines, tabs and
  control characters in a fact become spaces. An SMS has no lines; a multiline
  body becomes a multipart send or a truncated one depending on the transport.
* **The text is clipped to the channel cap, not rejected.** There is nothing to
  fall back TO from here, so the honest failure mode is a shortened true
  sentence rather than silence. The cut prefers a word boundary and is marked
  with an ellipsis, so a reader is not left with a sentence that merely seems
  to end.

## Decision 8 — a P1 touches the database not at all

Decision 2 keeps the engine off a P1's critical path. Round 32 moved the
governor's queries out of the way but still recorded an audit row, and
`session.add()` + `session.flush()` takes SQLite's write lock — so the message
that must arrive could block behind an unrelated writer, or raise.

A P1 now performs no query and no write. Losing the row costs nothing real:
`message_engine_attempts` records what the engine did with the MODEL, and a P1
never reaches the model. The delivery itself is recorded by the alert system,
which is where a P1's audit trail belongs. Its `source` is `deterministic`
rather than `fallback`, which is decision 2's own word and separates "never
asked, by rule" from "asked and gave up".

## Decision 9 — the directive detector is a denylist, and that is a known limit

Four rounds of the cross-vendor panel found the same class of gap in the rule
that refuses instructions: round 29 (verb inflections), round 34 (stative
verbs), round 37 (verbs again, after a fix that claimed to stop enumerating
them), round 38 (the OBJECTS, and then the adjective forms in front of them).

The rule now keys on SHAPE rather than vocabulary wherever it can. English
imperatives are subjectless, so a clause that opens with one word, names a
position, and ends there is an instruction about that position — whatever the
verb, and whatever modifiers sit in between (counted, not recognised, because
"safer" has an adjective ending and "quality" does not).

**The object list cannot simply be deleted.** Finding a position in second
place is what implies the first word was a verb acting on it. Without that
anchor, "Choose safer assets." and "Gold rose." are the same shape to a regex:
verb-first and verb-second are indistinguishable without knowing which word is
the verb. Removing the list would either miss every directive or refuse every
observation.

So a residual risk stands, and it is stated rather than hidden: **an
instruction naming a position noun outside the list will validate.** Closing it
properly needs one of

  * part-of-speech tagging, so imperative mood is detected rather than
    inferred; or
  * an ALLOWLIST of approved observational shapes — viable here because the
    engine's message space is genuinely small (band changes, score readings,
    flag transitions, freshness), and an allowlist fails safe where a denylist
    fails open.

The second is the better fit and is a deliberate design change, not a patch.
It wants an owner's decision because it can refuse legitimate output, which
the fallback then replaces — a real behaviour change on a live channel.

## Decision 10 — the engine does not commit the caller's session (round 40)

Decision 1 has the engine composing ahead of delivery, and round 32 added a
commit inside `compose()` so SQLite's write lock would not be held across a
model call that can run to a 60-second deadline.

That was wrong, and it took two rounds to establish how wrong. `compose()`
receives the CALLER's session, so the commit made every other pending write in
that unit of work durable: a caller that meant to roll back on a later error
no longer could (round 39). The guard added for it — "commit only if the
session was clean on entry" — cannot see work that was flushed before
`compose()` was called, or issued as Core DML that never enters `session.new`
(round 40).

There is no reliable way to ask a shared `Session` whether anything in it
belongs to someone else. So the commit is REMOVED rather than guarded a third
time.

**The cost, stated plainly.** The write lock is held for the duration of the
model call. Other writers in the process block until it resolves, which is
precisely what round 32 set out to prevent. The trade is deliberate: a held
lock DELAYS and is bounded by `_DEADLINE_S` and `reap_stale_claims()`, while a
premature commit CORRUPTS and is bounded by nothing.

**The real fix**, for whoever picks this up: the engine should own its
transactions — insert and commit the claim on its OWN session, keep the row
id, and resolve by id afterwards. Then the caller's session is never touched
and the lock is never held. That changes how `compose()` is invoked, so it is
a deliberate refactor rather than a review-round patch.

## Decision 11 — the directive check is an ALLOW-LIST of clause openers

Five rounds enumerated what to refuse — verb inflections (29), stative verbs
(34), verbs again (37), objects (38), adjective forms (38). Each closed the
instance the panel named and the next round found another, because the set of
ways to phrase an instruction is open.

**The message space is NOT tiny**, which rules out the obvious inversion. The
32 shipped fallbacks open their clauses 34 different ways, several with domain
prose ("Borrowing against brokerage accounts has turned down from its recent
high"), and one opener is itself a verb ("Compute run later."). An allow-list
of whole sentence shapes would refuse legitimate output.

What holds is narrower: **an imperative is SHORT and subjectless.** Every short
clause the library writes opens with a noun, a determiner, an adverb, a ticker
or a grounded value — never with a verb. So a clause of four words or fewer
must open with an approved token, and the openers were EXTRACTED from the
shipped fallbacks rather than invented. Longer clauses are exempt, which is
what keeps the domain prose legal.

Measured on nineteen imperatives that appear nowhere in the validator — dump,
ditch, hoard, offload, unwind, deleverage, fade, front-run and others — all
nineteen are refused, with no shipped fallback refused.

The deny-lists above remain as belt-and-braces. They are no longer the primary
defence, so their open-set problem can no longer reach the operator.

**The failure direction is deliberate**: an unlisted SUBJECT costs a fallback,
an unlisted VERB sends advice to the operator.
