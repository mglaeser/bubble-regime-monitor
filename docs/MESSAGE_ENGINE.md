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

The model's words are English only (Q30); the WIRE language is the
operator's `MESSAGE_LANGUAGE` since decision 23 (owner instruction
2026-09-19, amending Q30's "English only" for the wire). SMS: <=150
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

## Decision 24 — German is judged by a reduced rule set; a context carries no numbers

The owner's rulings (2026-09-20, 2026-09-24): the model writes, so that a
message explains what the data means, and by the less complex path. The
template writes the numbers, and on iMessage the model adds a short
context saying what they mean (the context mode, next PR). This decision
is the validator's part: judging German prose, and the one rule every
context meets.

**German prose rules.** `validate(..., language="de")` judges German with
the language-agnostic rules (script, format, grounding, arithmetic, zones)
plus a German set. The English prose rules would misread German, which
puts its verb second. The set has these parts:

- **One folded spelling.** Every German scan reads one folded spelling of
  the text, and every word set carries its folded forms. An accent cannot
  hide a word ("Káufe", "Háltén Sie"), and every umlaut admits its ASCII
  transliteration ("duerfte").
- **A banned lexicon.** It covers probability, advice, certainty, forecast
  and crash talk, including the verb "raten" in every form: present, past,
  both subjunctives ("du ratest", "du rietest"), the present participle,
  the prefixed verbs ("abraten", "zuraten", "angeraten"), and "geraten"
  after "zu"/"zur"/"zum" ("zur Vorsicht geraten"; "unter Druck geraten" is
  another verb) (#124 round 8), anywhere before it in its clause ("zu
  großer Vorsicht am Markt geraten"; the infinitive's "um nicht unter
  Druck zu geraten" stays), and "gut beraten" (#124 round 13); "dazu
  geraten" too (#124 round 18). The noun "die Rate", capitalised (not in
  capitals) and after a determiner, is not banned; "Alle raten ..." is the
  verb (#124 round 1). German joins its words, so the stems that are
  always advice or forecast are banned inside a compound too
  ("Kaufempfehlung", "Kursprognose", "Crashgefahr"); "kauf" only with an
  advice part, since "Verkaufsdruck" describes the market (#124 round 3).
- **An advice and forecast grammar.** It covers reader-directed modals,
  the subject-first modal, impersonal recommendations, the passive modal,
  "ist zu verkaufen" and "zu" infixed in a separable verb. A forecast's
  movement and a passive modal's "werden"/"sein" are found anywhere in the
  sentence, not within a count of words or characters, and past any word
  ("wir werden sie bald steigen sehen") (#124 rounds 1 and 3). Every shape
  is matched with its verb last too, the order of a subordinate clause
  ("weil der Kurs steigen wird", "dass man Gewinne mitnehmen sollte",
  "weil Positionen reduziert werden sollten", "dass es sich lohnt", "zu
  reduzieren ist"), and "zu" infixed after "mit" ("mitzunehmen") (#124
  round 6). The reader's modal is one list for every order, in every
  person and both moods of "sollen", "müssen" and "können" ("du
  solltest", "ihr sollt", "man müsste", "Anleger sollen") (#124 round 7).
- **Imperatives.** This covers the formal imperative in any case of the
  verb ("Kaufen Sie", "halten Sie", "BLEIBEN Sie") with "Sie" keeping its
  capital, and the informal imperative from the action stems - at the head
  of a clause, the plural in -et included ("Haltet die Position."), and
  after a comma, where the verb's form decides: the bare form and the form
  in -e are the command whatever follows ("..., nimm die Gewinne mit",
  "..., halte Abstand") unless "ich" follows; the plural command of a verb
  whose third person changes its vowel ("nehmt", "haltet", "lasst") is the
  command unless "ihr" follows; a form in -t that is also the third person
  is a statement ("..., bleibt die Lage ruhig") (#124 rounds 2 and 5).
  A separable verb's base stem is generated from its prefixed stem, since
  its imperative opens without the prefix ("Stoße die Aktien ab") (#124
  round 2). The strong verbs' imperatives ("gib", "nimm", "wirf") come
  from one map of their stems. The infinitive-order rule is generated from
  the same stems. A clause also opens after a bracket, and after a quote
  with no space after it ("(Bleiben Sie ruhig.)", "„Bleib ruhig“"); a
  closing quote before a verb is a statement ("„Bewertung“ bleibt hoch")
  (#124 round 4). A comma or a closing mark ends the informal imperative
  ("Bleib, wenn die Daten fehlen, investiert."), and a closing quote or
  bracket ends the clause of an infinitive instruction ("Die Devise lautet
  „Positionen abbauen“.") (#124 round 8).
- **Number words.** Model text refuses the ordinals from "third"/"dritte"
  up and the counts and multiples ("twice", "dreimal", "verdoppelt"), in
  both languages; "first"/"second" stay words, as #100 decided. The
  cardinals and their compounds are generated from the word list
  (hundred-led, teen- and ten-led thousands, tens joined by "und", halves,
  and a number word joined to a period or a unit: "Zweiwochenhoch" - #124
  round 3; joined to a fraction, "halb" joined to a period, and the
  periods of years: "Zweidrittelmehrheit", "Halbjahreshoch", "Jahrzehnt" -
  #124 round 10; the number nouns and the decades: "Dreier",
  "Zwanzigerjahre" - #124 round 11; the adjectives of a count:
  "dreimalig", "zweistellig", "dreistufig", "zweiwöchig" - #124 round 12).
  The fractions ("ein Fünftel") are checked in every German message, as
  the cardinals are, and "Milliarde" makes "milliardste" (#124 round 12).
  The counts and multiples in "-mal" and "-fach" are generated from every
  number word, the mixed numbers included ("anderthalbmal"), and so are
  the verbs and nouns of a multiple ("Vervierfachung", "verfünffacht" -
  #124 round 18). The fractions and the scale nouns have their genitive
  ("eines Drittels", "eines Dutzends", #124 round 18). "ein"/"eine" counts
  as the number one before a counted noun, read by one scanner that walks
  the whole noun phrase: any number of lowercase modifiers, then the run
  of capitalised words, ending at the first lowercase word after it that
  is no inflected adjective - a verb or a function word ("Ein Berliner
  politisches Warnsignal" is one phrase, #124 round 17). A counted noun
  anywhere in the phrase is the count, since a capitalised word can be an
  adjective ("Eine Berliner Warnflagge", #124 round 4); a ticker in
  capitals is a modifier. A period counts by the head of its compound
  ("seit einem Handelstag", "Geschäftsjahr"), "Quartal" and "Dekade" among
  them (#124 round 13). A comma between two modifiers, and a bracket or a
  quote before the nouns, stay inside the phrase ("Eine aktive, bestätigte
  Warnflagge", #124 round 5), and so does a comma after the nouns ("Eine
  Berliner, bestätigte Warnflagge") unless a clause opens after it ("Ein
  Treiber, der ...") (#124 round 11). The lexical counts - the ordinal
  adverbs from "drittens" up, the fractions, the plural scale words,
  "zweierlei", the numbered periods - are numbers in model text of either
  language, and so are the quantifiers ("beide", "single", "pair") where a
  counted noun stands in their clause, before or after them, however many
  modifiers between ("beide Flaggen", "Die Warnflaggen sind beide aktiv";
  "Gold und Bargeld hielten beide" counts nothing) (#124 rounds 15 and
  16). The ordinals include "nullte" (#124 round 16). "achte" and "achten"
  are the verb as well ("achten auf"), so they count as the ordinal only
  after a determiner ("im achten Monat", #124 round 8).
- **The reader is not addressed.** A German message never addresses its
  reader: "..., reduziert eure Positionen" passed, the verb's form being a
  statement's too, and any verb outside the stems would pass with it
  ("prüft eure Depots"). The informal forms ("du", "dich", "dir", "dein-",
  "deins", "euch", "euer", "eur-", generated with every ending - #124
  round 17) are refused anywhere; lowercase "ihr" is "her" and "their" as
  well and stays. The formal forms ("Sie", "Ihnen", "Ihr-") are refused
  mid-sentence, where only the formal "you" is capitalised (#124 round
  12), and in capitals ("wie SIE sehen", #124 round 14). The English "you"
  in German prose addresses the reader too, judged after the
  English-clause rules so a whole English clause is judged as one (#124
  round 13).
- **A positive language check.** A German message needs at least two
  distinct German function words, outnumbering the English evidence.
  English evidence includes common English words as well as function
  words, but no word German spells the same way. A clause or comma-part
  with two English words and more English than German makes the message
  not German. A third language is refused. A clause carrying English prose
  is judged by the English advice and imperative rules.
- **Capital ẞ.** It is a German letter on iMessage; SMS refuses it for
  GSM-7.

Each item was a finding of the #121 rounds (20-55), and each fix is kept
in the form it ended in: a generated rule where the item came from a list.
The residual is stated, not hidden: the advice and imperative rules
enumerate verbs, so an instruction with a verb outside the lists passes.
That is the residual of decision 9, in German. The English path of
`validate()` changes in one respect (#124 round 15): its model text
refuses the German number words ("vier flags"; "null" and "elf" are
English words), the lexical counts, and a quantifier before a counted
noun in its clause ("both flags", "the flags both fired"; "Gold and
cash both held." names its two and counts nothing, as #100 pinned).

**A context carries no numbers.** `validate_context(text, language,
max_chars)` judges the context a model writes for a message. It applies
the prose rules of its language - an English context addresses no reader
either ("you", "your"; #124 round 12) - and refuses any digit and any
number word in either language:

- cardinals and their compounds, the scale words in the plural
  ("hundreds", "Tausende") and the fractions ("a quarter", "ein Fünftel")
  (#124 round 7), a fraction in a compound ("Zweidrittelmehrheit") (#124
  round 10);
- the periods that are a number: "decade", "century", "fortnight",
  "biweekly", "Jahrzehnt", "Jahrhundert" (#124 round 10), "Dekade" (#124
  round 13);
- the words that count without a number word: "both", "beide",
  "zweierlei", "sole", "trio", and the plural cardinals ("tens", "the
  twenties") (#124 round 11), and the adjectives of a count
  ("dreimalig", "zweistellig") (#124 round 12);
- ordinals, "first"/"second" and "erste"/"zweite" included, "erstmals",
  and the ordinal adverbs ("thirdly", "drittens"), generated from the
  ordinals (#124 round 7);
- "once" and "einmal", words elsewhere and a count in a context (#124
  round 2);
- counts and multiples ("twice", "half", "doubled", "single", "pair",
  "dreimal", "verdoppelt", "doppelt so hoch"), generated from the
  cardinals where the language builds them;
- Roman numerals: a word of two letters or more that reads as one, in
  capitals or in lowercase from i, v and x ("level IV", "phase iii"), and
  any numeral character (#124 round 4), read off the folded text ("level
  ÍV", #124 round 6). Single letters stay words (the V and D blocks, "I",
  "M&A"), and so do the credit ratings CCC and CC. An acronym that reads
  as a numeral ("IV" for implied volatility) costs the context, not the
  message.

The numbers of a message are the owner's template's. The context only
says what they mean, so it never has to ground a number. That closes by
construction the largest class of the #121 findings: rule constants used
in the wrong role, gauge labels, ordinals, counts, and values that are
never printed. Those rounds found them one word at a time.
