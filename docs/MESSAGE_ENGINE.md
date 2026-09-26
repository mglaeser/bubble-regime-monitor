# MESSAGE_ENGINE — LLM-written operator messages (Phase C)

The engine writes one message per trigger: the model is given the trigger's
task from the owner's prompt library, every number the caller resolved, and
what the repository knows about the indicators behind the trigger; it writes
the message; a few basic checks decide whether the text can go out on its
channel; the owner's template, with the current numbers in it, goes out
otherwise. It is its own subsystem in the `MESSAGE_ENGINE_*` settings
namespace (ruling Q42); `app/alerts/llm_selector.py` is left untouched (Q41).

## Decision 24 — the model writes; basic checks; the reader interprets

The owner's ruling of 2026-09-25, which governs every decision below: "the
generated messages should follow a system and the prompt for them should be
including all possible numbers and references, for the LLM to create most
context out of it - BUT the implementation should be straight forward, it
should NOT have extensive quality gates, check the basic and the rest is also
up to the message receiver to interpret it correctly and reflect on the data
and message on his/her own." It ended a validator that judged the model's
words (numbers grounded against the facts, a banned lexicon, advice and
imperative grammar in English and German): a denylist for free text does not
converge, and 57 review rounds on #121 and 20 on #124 showed it.

- **The prompt** (`composer.prompt_for`): one fixed system paragraph (what
  bubblegauge is, that the owner interprets, research not advice, numbers
  as given); the entry's ROLE, TASK and DATA sections from the library with
  the numbers filled in; every fact the entry declares (`grounding_fields`
  - for the digest, every number it reports and the judgment), by name;
  the references for the trigger - the methodology and sources of its
  indicators (`app/message_engine/context.py`, repo-authored text only);
  and how to write: one message, in `MESSAGE_LANGUAGE`, for its channel
  only, without links, within the channel's length and alphabet as the
  check counts them (septets and GSM-7 on SMS, where `[ \ ] ^ { | } ~ €`
  count as two; code points on iMessage, in printable ASCII and Latin-1,
  the marks and the library's five emoji; #126 rounds 5-7). A fact the entry does not declare
  never reaches the model (#126 round 1), and a string fact is one of the
  monitor's own values - an action or trend state (the digest's band as the
  snapshot displays it, "suppressed (block degraded)" too; #126 round 9),
  a value written as text
  (digits, and for words only units, time zones, months, weekdays and the
  two trend assets: "14:00 UTC", "3h", "25 Sep 14:00Z", "SPY"), a block
  summary keyed by the indicators' own ids - or the bounded prior
  judgment; anything else renders as a dash and stays out of the prompt
  (AGENTS.md ground rule 1; #126 rounds 2-4). The library's other sections
  (its hard rules and output format, written for the replaced design) are
  not sent, and its tasks' instruction to write an SMS and an iMessage
  variant in one reply is left out (#126 round 4).
- **The basic checks** (`app/message_engine/checks.py`): something visible,
  no control character, only the channel's alphabet, no link, no channel
  name, within the channel's length. The alphabet is an allowlist: GSM-7
  on SMS; on iMessage printable ASCII, printable Latin-1 but the no-break
  space and the soft hyphen, the capital sharp s and a few typographic
  marks (`checks.MARKS`), and the library's five emoji
  (`channels.imessage.emoji_allowlist`) - so a character that draws
  nothing, a blank but the space, another script or Latin block, a
  fullwidth or look-alike form, a combining mark or any other emoji is
  refused (the reply is NFC-composed first, so a decomposed "ü" is the "ü"
  it shows). #126 rounds 2-7 found those one at a time while the check
  listed what to refuse; round 6 turned it round. A link is any URI - a
  scheme and its colon wherever it starts (the time of an ISO date-time,
  "2026-08-15T14:00Z", excepted - not "T14:payload"), "://" anywhere - "www.", a bare domain or address (any run of
  characters, a dot and a word of two letters or more, with no space
  between), and a number a phone dials: a "+" and seven digits or more
  (#126 round 8). A reply that names a channel - in any case, with or
  without accents - is variants for several. The length is counted in
  septets on SMS and in code points on iMessage. The template meets the checks too: a
  template a fact broke sends the bare event. Nothing about what the text
  says: a number the facts do not carry, or a word the old lexicon banned,
  goes out as written, and that is pinned.
- **Otherwise the template**: a reply that fails a basic check, a gateway
  failure, a paced or budgeted-out call, a fixed trigger, a disabled engine
  or a P1 - each sends the owner's template with the current numbers.
- **What stays**: the governor's pacing, budget and breaker (the cost of
  calling the model), the attempt rows, the library sign-off, provenance,
  and the admission gate. They are not gates on the content.

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

SMS: at most 150 septets, GSM-7 (3GPP 23.038) - the German letters ä ö ü Ä Ö
Ü ß are basic-table characters, one septet each. iMessage: at most 200 code
points (`MESSAGE_ENGINE_IMESSAGE_MAX_CHARS`). The model is told the channel's
length and alphabet; the basic checks hold it to them (decision 24).

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

## Decision 13 — the engine owns its transactions

`compose()` takes no session. Every `message_engine_attempts` write is made
by the governor on a short transaction of its own: `reserve()` writes the
IN_FLIGHT claim under `BEGIN IMMEDIATE`, evaluates every gate with that row
excluded, commits on ASK and returns the claim's id; `resolve()` closes the
claim by id, only while it is still in flight, so a reaped claim's strike is
never erased by a late reply; `record_fallback()` records the evergreen text
as NOT_ASKED, or as the FALLBACK_USED marker when the compose is exhausted —
written at the exhausting rejection, stamped strictly after the rows it
closes. No lock is held across the model call, the claim is durable and
visible to a concurrent worker before the call, and nothing of the caller's
is ever committed or rolled back on its behalf.

Why: rounds 32, 39, 40 and 41 of the #100 review argued about committing the
caller's session; the offline review before #106 round 8 executed the cost of
the truce (a claim lost with the caller's transaction on a crash, the reaper
unreachable, the write lock held for the whole call). Callers must not hold
an open write transaction while calling `compose()`; the dispatcher already
sends outside transactions.

## Decision 14 — the library must be signed before the wire

`config/message_prompts.v1.json` carries a status line — shipped as "DRAFT -
owner sign-off required" (ruling Q34) — and nothing read it, so an admitted
deployment could have sent unsigned content (#112 round 2). The owner signs
by editing the line to begin with `SIGNED` in a reviewed PR: data, never
code. Until then `compose()` is inert (no model call, no attempt row, only
the bare event line) and `gate.emit` refuses to put anything of the
engine's on a wire, even when admitted. An unreadable library is unsigned.

## Decision 15 — provenance is proved, not declared

`gate.emit` takes a `Composed`, not text, so the composer's product is the
only thing it puts on a wire — and the class is public, so a `Composed`
built by hand carried any text past every control (#112 rounds 1, 6). A
`Composed` now carries a keyed digest over its fields, minted only by the
composer's `_issue` with a key drawn at import; the gate checks it FIRST,
before the channel, the signature and admission, and its refusal logs
nothing of the object (round 14) — until a `Composed` is proved the
composer's, every field of it is the caller's string.

## Decision 16 — a fact is a scalar, redacted

A fact is a scalar - a dict or list renders as a dash - and every string
passes the repository's redaction chokepoint (`app.redaction.sanitize`), the
one the failure alert uses, before it reaches the prompt or a template slot
(#112 rounds 4 and 6); a string must also be one of the monitor's own values
(decision 24). The override suffix is derived, never supplied.

## Decision 17 — the transport is the channel the Composed was made for

A `Composed` is fitted and validated for ONE channel. A sender names its
channel; the gate refuses a sender that does not match the `Composed`'s, or
names none (#112 round 13). The `Composed`'s channel is bound by its token.

## Decision 18 — when a render overflows, the facts give way first

The fit clipped the rendered text from the end, so an over-long fact in the
middle of the breaker notice cost it "Scores and alerts unaffected." — the
sentence its library note calls load-bearing (#112 round 12). The owner's
sentences are the message; a fact is a value in it. `_fit_render` shortens
a phrase fact on a word boundary, never inside a numeral, then blanks the
longest fact to a dash, until the text fits; only a template that overflows
on its own is clipped. A clip never lands inside a numeral either way
(round 4).

## Decision 19 — the wire and the log carry the owner's word or "unknown"

The bare-event line said "bubblegauge: {trigger} fired." with the caller's
string; filtering it to an identifier was not enough, because a credential
can be an identifier (#112 rounds 6, 7). The name is echoed only when it is
a key of the library; otherwise the line and the record say "unknown". No
log line carries a caller-supplied string: a fact that is not a scalar is
logged by its kind, an unissued `Composed` not at all (rounds 11, 14).

## Decision 21 — the contract's fact ids fill the slots

Two entries declared the contract's source attribute (`base_action_band`,
`missed_recompute_slots`) while the alert contract supplies the fact id
(`F_BAND_BASE`, `F_MISSED_SLOTS`), so the live value rendered as a dash and
was invisible to the model (#112 round 9). Every rule-driven entry declares
contract ids, and the contract's own `FACT_SOURCES` table is the slot alias
table.

## Decision 23 — one language switch for both paths

The operator asked for every message in either English or German, chosen
by setting. `MESSAGE_LANGUAGE` (`en` | `de`) is that switch, one setting so
the two paths never disagree in a single day's messages. The alert phrase
set carries both languages since v3.5 (docs/ALERT_SYSTEM.md). The prompt
library carries, per entry, `translations.<lang>` with its `fallback`
template; the composer asks the model to write in the selected language and
sends that language's template otherwise. An entry without a translation
for the selected language renders in the library's own language: the
owner's words in one language beat no words at all.
