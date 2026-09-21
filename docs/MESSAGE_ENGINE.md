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

The language of every message is the operator's `MESSAGE_LANGUAGE`
(decision 23, owner instruction 2026-09-19, amending Q30's "English only");
since decision 24 the model writes in it, and the validator judges the text
in that language (decision 25). SMS: <=150
characters, GSM-7-safe, no emoji, septet-accurate counting. GSM-7 (3GPP
23.038) is the contract, not ASCII: the German letters ä ö ü Ä Ö Ü ß are
basic-table characters, one septet each, and never force UCS-2; a character
outside the table is refused in either language. iMessage: <=200 code points, at most 2 emoji from the allowlist.
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

### Decision 9, resolved (owner, 2026-09-06)

Three panel rounds on the validator each found a new edge of the directive
allow-list — a fifth word past its bound, an all-caps opener taken for a
ticker, a bare object with no determiner. That is the signature of an open
set, and the owner chose to stop narrowing it:

* **Bridge.** The validator merges with the residual stated in the rule's own
  docstring (`KNOWN RESIDUAL`) and pinned by a test that the statement exists.
* **Closure, upstream.** Decision 12 below: the composer's output becomes a
  structured selection over owner-approved phrasings with facts filled
  verbatim by the renderer. Free text never reaches the wire, so the detector
  becomes defence-in-depth rather than the gate.

## Decision 12 — the model selects a phrasing; it does not write the wire text

**OVERRULED by the owner on 2026-09-20 (decision 24): `generate` is the
shipped mode again; selection is kept as the conservative mode.** The
text below is the record of what was built and why.

Closes the open set from decision 9 by construction. Each trigger's library
entry carries `phrasings`: owner-approved sentence templates with fact slots,
starting as the single evergreen fallback. The model is shown the phrasings
and the facts and returns a structured choice — which phrasing — and the
renderer fills the slots from the facts, verbatim. What reaches the wire is
always an approved template with grounded values.

The model still adds judgement (which phrasing fits the moment), and the owner
adds phrasings by authoring, not by code. A reply that is not a valid choice
falls back to phrasing zero. The channel contract still runs on the rendered
result; the directive detector runs on it too and should never fire.

This narrows Phase C's premise — "the model writes the sentence" — to "the
model chooses the sentence", which is the containment `llm_selector` already
uses for the alert path (ruling Q41), applied to the message path.

### Decision 12, as built

* `phrasings_for(entry)`: the library's `phrasings`, defaulting to the single
  evergreen fallback. Variants are authoring, never code.
* The model's reply is `{"phrasing": N}`. Tolerant of surrounding prose,
  strict about the value; anything else is a FORMAT rejection and phrasing
  zero is sent.
* The rendered template is validated with **`prose_rules=False`**: the
  channel contract and every grounding check still run; the meaning-of-prose
  rules — lexicon, language, advice, imperatives, band-verb grammar — judge
  model text, of which there is none on this path. This mattered immediately:
  the owner's own `BAND_TO_*` templates were refused by the band-verb grammar
  on "(before: hold)", which the old contract never saw because the rendered
  fallback was never validated.
* **Slot resolution is explicit.** 21 of 32 shipped fallbacks use lowercase
  slots (`{band_effective}`) against `F_`-keyed facts and had always rendered
  dashes; the test meant to catch it matched uppercase slots only. Now: exact
  key, then `F_` form, then the two aliases the library's notes define
  (`next_check_utc` → `F_NEXT_CHECK` as a bare time; `override_suffix`
  computed). No shipped fallback renders a dash with its own declared facts.



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

## Status on main (2026-09-19)

Landed, in order: #104 (schema and settings), #105 (the validator,
standalone; 41 panel rounds), #111 (format controls at the message edges),
#106 and #109 (the governor and its tests), #112 (the composer and the
admission gate, standalone; 16 rounds), #113 (governor pins), #114 (composer
pins). Nothing on main calls the engine yet: decision 1 has it compose
BEFORE a delivery is queued, so its caller is the alert dispatcher, and
wiring it is the go-live step under the operator's takeover decision — a
separate PR (decision 22). Two facts the go-live PR inherits: the shipped
prompt library is UNSIGNED, so the engine is inert until the owner signs
its status line (decision 14); and the alert phrase registry is written in
German while the validator's language is English, so registry text reaches
the wire only by proof against the registry itself (decision 16).

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

## Decision 16 — a fact is a scalar, redacted, and judged before it fills a slot

Decision 12 judges the model's words and trusts the owner's template; the
grounding check judges numerals. A FACT was judged by nobody (#112 rounds
4–9, 11, 15). Now, in `compose()` and again where a slot reads a fact:

* only DECLARED facts fill slots (`grounding_fields`, compared by canonical
  contract id, so `band_base`, `base_action_band` and `F_BAND_BASE` are one
  fact); the override suffix is derived, never supplied;
* a fact is a scalar — a dict or list renders as a dash;
* every string passes the repository's redaction chokepoint
  (`app.redaction.sanitize`), the one the failure alert already used;
* a string carrying an emoji renders as a dash: data has no decoration;
* a PHRASE (whitespace inside) is held to every meaning-of-prose rule of the
  validator, the allow-list of clause openers included, grounded by itself
  so only meaning is judged; an ATOM ("trim", "14:00", "51/100") is held
  to the banned lexicon only, because alone a band name reads as an order
  and a score as a quotient, and neither is the atom's doing;
* a field an entry declares as `authorized_prose` is admitted only if it
  parses as a join of the phrase registry's own fragments, in one
  language, with each slot holding its fact's TYPED domain — a band enum,
  the rule's asset label, an HH:MM next check, else a number — exactly
  what the alert renderer can produce (round 8 found that trusting the
  KEY let a caller's "sell everything now" through under it; #119 round 4
  found that bounding a slot by width alone proved "Execution armed: SELL
  OUT, median 99." through the asset slot);
* a refused fact renders as a dash, is kept out of the prompt, and is
  logged by the library's name or not at all.

Why not judge the rendered sentence: the owner's templates were authored
against `prose_rules=False`, and the validator refuses their idiom whole
("(before: hold)" splits into a clause headed by a band word), so a
whole-sentence judgement cannot tell a hostile fact from the template.
Measured before it was rejected (round 5).

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
(round 4), and the fallback is held to the whole channel contract — the
emoji cap and allow-list included — with the bare event sent in its place
when it fails (round 7).

## Decision 19 — the wire and the log carry the owner's word or "unknown"

The bare-event line said "bubblegauge: {trigger} fired." with the caller's
string; filtering it to an identifier was not enough, because a credential
can be an identifier (#112 rounds 6, 7). The name is echoed only when it is
a key of the library; otherwise the line and the record say "unknown". No
log line carries a caller-supplied string: a refused fact is logged by the
library's name, an unissued `Composed` not at all (rounds 11, 14).

## Decision 20 — the library's writing instructions are removed before the choice

The library's prompts were authored for an engine that WROTE the text and
end in an output section asking for two labelled lines. The composer only
appended the decision-12 selection instruction after them; "last word wins"
was an assumption, and a model obeying the earlier one would be
format-rejected until the compose fell back (#112 round 10, SOTA-C).
`selection_prompt()` removes the output section and the writing bullets
before the selection instruction is given; the library is not rewritten.
(Since decision 26 the library carries neither: the channel, language and
output instructions are the composer's, and `selection_prompt()` is a
no-op on the shipped prompts, kept for a library that still has them.)

## Decision 21 — the contract's fact ids fill the slots

Two entries declared the contract's source attribute (`base_action_band`,
`missed_recompute_slots`) while the alert contract supplies the fact id
(`F_BAND_BASE`, `F_MISSED_SLOTS`), so the live value rendered as a dash and
was invisible to the model (#112 round 9). Every rule-driven entry declares
contract ids; the contract's own `FACT_SOURCES` table is the slot alias
table; a pin sweeps every headline-keyed entry for exactly this class.

## Decision 22 — standalone, and what the go-live PR must do

The engine has one path to a transport, `gate.emit`; it takes an issued
`Composed`; no engine module imports a transport; and the set of app
modules importing the engine is empty — all pinned. The go-live PR, under
the operator's takeover decision, will: give the dispatcher a sender that
names its channel and wraps `app.notify.sipgate` / `app.notify.imessage`;
call `compose()` outside any write transaction (decision 13) and hand the
`Composed` to `gate.emit`; rewrite the caller pin to name the dispatcher;
and land the owner's signature on the library (decision 14). Until then
the engine is a library, reviewed as one.

## Decision 23 — one language switch for both paths

The operator asked for every message in either English or German, chosen
by setting. `MESSAGE_LANGUAGE` (`en` | `de`) is that switch, and it is one
setting because the two paths must never disagree in a single day's
messages. The alert phrase set carries both languages since v3.5 (see
docs/ALERT_SYSTEM.md: one promotion admits the whole reviewed set, every
language held to the worst-case fit). The prompt library carries, per
entry, `translations.<lang>` with the same keys as the entry - `fallback`,
optional `phrasings`, `must_mention` where the entry has one - and the
composer selects that language's phrasings, checks the mandate in that
language, and tells the model which language the phrasings are written
in. The prompts stay English: the model selects, it does not write, so
the language of its instructions and the language of the wire are
independent by construction (decision 12).

What does not change with the language: the validator. Its meaning-of-
prose rules are English and they judge the MODEL's words, which never
reach the wire; the German fallbacks are the owner's templates, held to
the same runtime contract as the English ones (grounding, format, the
channel caps) and reviewed in the PR that added them, exactly as the
English were signed. A fact that is a phrase is still screened by the
English rules, which is why registry text - now in either language - is
admitted by proof against the registry rather than by judgement
(decision 16). An entry without a translation for the selected language
renders in the library's own language: the owner's words in one language
beat no words at all, and the case is pinned.

## Decision 24 — the model writes the message (owner, 2026-09-20; decision 12 overruled)

The owner, read back the specification and the built engine (the coherence
review of 2026-09-20), ruled: *"generate mode, that's the whole point, I
want a bit explaining of what the data in the bubble gauge actually mean"*.
Phase C as ruled (Q24–Q32) had the model write the sentence and the
validator judge it; decision 12 narrowed that to a choice among owner
templates, every entry shipped with exactly one, and the deployed digest
was the deterministic template with a model call in front of it. That
narrowing is undone:

* `MESSAGE_ENGINE_MODE` (`generate` | `select`), default `generate`. In
  generate mode `compose()` gives the model the owner's prompt for the
  trigger with the grounded facts in its DATA slots (and once more as a
  bare table), this channel's own contract and the language rule, and
  reads the reply as the message body (`written()`: a label, one pair of
  quotes and a two-variant reply are tolerated; nothing else is repaired).
* The reply is validated with the **prose rules on**, in the language it
  was written in (decision 25): the channel contract, the grounding of
  every numeral, the lexicon, advice and forecast grammar, imperatives,
  the mandate (`must_mention`). A refusal is a rejection the governor
  paces exactly as before (FORMAT: the 30 s retry; CONTENT: the floor and
  the cap of Q38), and the evergreen template goes out meanwhile
  (decision 7).
* **The delivery of one message can be patient.** The composer makes one
  attempt per invocation and counts iterations in the rows, so a trigger
  that fires once a day spent one content iteration per DAY. The delivery
  service can wait for the governor's next admission, up to the patience
  the CALLER grants - the digest passes `MESSAGE_ENGINE_RETRY_PATIENCE_S`
  (330 s: a format retry, or one content retry at the floor) - and never
  past the cap, the breaker or the budget, because it only asks the
  governor when. A refusal that was not a rejection ends the wait at once.
  `deliver()` itself grants none: a ready fallback is never withheld by
  default, so an alert routed through the engine is not held for minutes
  behind a rejected reply (#121 round 3, SOTA-A). The loop is bounded by
  the content cap whatever the governor answers (round 4, SOTA-C).
* **The prompt's own constants are grounded.** The owner's DATA lines
  quote frozen rule constants and call them quotable (the ten-month
  trend rule, the 55 gate, the 100 basis points), and the validator
  grounded numerals against the facts alone, so a compliant reply that
  quoted one was rejected (#121 round 9, SOTA-A). The numerals written
  in the entry's prompt are grounded like the facts; slot names, list
  markers and unit-glued numbers ("24h") are not, and a withheld constant
  (the S3 tiers) stays withheld. A constant is grounded IN ITS WORDING
  only - beside one of the CONTENT words the prompt writes next to it
  (the nearest on either side, past stopwords and numbers), never across
  a sentence boundary - so "the 55 gate" cannot become "the score stands
  at 55" (round 11, SOTA-A) or "the 55 level" (round 12: an article is
  not a wording; a unit word is not one either, round 13). A fact the
  registry declares in percent grounds its percent form too ("44.2%" for
  a bare 44.2), and a constant the prompt writes as a percentage is a
  percentage to the validator as well, with the same wording bound to it.
* **The quotable lines invite only words the validator admits.** Pinning
  the constants found the owner's own wording inviting refused text:
  "not a probability" (the lexicon), "two monthly declines" and "two
  years" (spelled-out numbers), "closing price" and "a close above"
  (the advice rule's verbs), "the flag switches on" (likewise). Those
  lines are reworded under the signature, and a pin holds every quotable
  line to the lexicon, the number words and the advice rule.
* **Background facts are read, never printed**, and the gauge labels are
  not printable either: a background numeral that equals a grounded one
  is grounded (provenance is not tracked), so the raw summary syntax
  (`s1=`) and the bare internal labels (`s1`, `d4`) are refused as content
  in any language (#121 round 3, SOTA-A). A background field the entry
  names in `never_printed_fields` may not have its VALUE reproduced in
  the message at all, numeric or not (round 10); the plain-language note
  is not among them, because the owner's prompt says the model may draw
  on it.
* Select mode is unchanged and pinned; the existing selection pins name
  it explicitly.
* **Background facts are read, never printed.** An entry may declare
  `background_fields` (a subset of its `grounding_fields`): they fill the
  prompt's DATA lines so the model can draw on them, and they are kept out
  of the grounded table and out of the grounding the validator judges by,
  so a numeral from them is an ungrounded numeral and the message is
  refused. The digest's gauge summaries and its plain-language note are
  declared so; "never print these values" had been prose only, and a
  printed sub-score validated (#121 round 1, SOTA-A, executed).

What decision 12 closed by construction — the open set of decision 9 — is
open again on the generate path, by the owner's choice and with the
owner's reason. The validator's 41 rounds exist for exactly this path.

## Decision 25 — German is judged by a reduced rule set, for now

The meaning-of-prose rules are English: the lexicon, the advice and
forecast grammar, the imperative shapes, the not-English backstop. A
German message would have failed the backstop every time and the operator
would never have seen an enriched German text. The owner's default is
German (decision 23), so German is judged by the language-agnostic rules
(script, grounding of every numeral, spelled-out numbers, zones,
arithmetic, format) plus a German set: a banned lexicon (probability,
advice, certainty, forecast, crash talk), an advice/forecast grammar
(reader-directed modals, impersonal recommendations, future or modal
movements, the passive modal "sollten ... werden" and "ist zu
verkaufen" - #121 round 5), the formal imperative ("Kaufen Sie") and the informal one (a
bare verb stem opening a clause: "Bleib in SPY.", "Halt Abstand.", from
an enumerated list of the verbs an instruction to an investor uses -
the shape of the English action-verb list, with its known limit), German
number words and compounds, and a POSITIVE check that the text is German
at all: the German function words must outnumber the English ones over
the whole message (a compliant English reply had passed as German, #121
round 1; one marker was then enough and "The die shows ..." passed,
round 8).
The English shape rules are not consulted on German clauses: German
puts its verb second, and "Langfristig sind SPY und QQQ IN." read to them
as an instruction on the first real digest. A clause WITHOUT a German
word in it is not German, though: "Move to cash. Die Spanne liegt bei
57-61." satisfied the marker with "die" and the German grammar with
nothing (#121 round 3), and "Die move to cash now." did the same inside
one clause (round 4). So a clause that carries an English function word
is English prose whatever else is in it and is judged by the English
advice rule and the English imperative shapes; a clause with neither
language's words is judged by the advice rule; a German label such as
"Langfristtrend:" is neither. `validate(..., language="de")` selects
the set; a language without rules is refused outright.

This is the interim the owner accepted so German is enriched at all
(option (c) of the coherence review). It is weaker than the English set:
the German clause-opener allow-list and the full imperative grammar do
not exist yet, and the residual of decision 9 applies to German until the
German validator program - the shape of #105 - lands.

## Decision 26 — the shared rules are authored once

Each of the library's prompts carried the same six rules in its own
words - five wordings of the numeral rule, four of the register - and
drifted (the composer already had to strip two spellings of the same
output bullet). On the owner's instruction (2026-09-20) the library
carries them once as `house_rules`; each prompt keeps ROLE, TASK, its own
rules (`RULES FOR THIS MESSAGE`) and its DATA; and the composer assembles
`HOUSE RULES` - the language rule first - ahead of the DATA section, then
the channel contract, the grounded facts and the output instruction, in
both modes. The owner's sentences were kept where they carry something
of their own; a present-but-malformed `house_rules` is a malformed
library (the bare event); an absent key is no shared rules. The status
line records the restructuring under the owner's signature.

