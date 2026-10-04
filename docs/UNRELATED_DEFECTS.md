# Unrelated defects — parked for later, NOT fixed in the alert-system work

Found while completing the alert system. Each is out of scope for that target,
so per instruction none of these were touched.

## U-01 — pyproject caps numpy/pandas for a CPU the deploy host no longer is
**Resolved 2026-09-28 (owner decision D8):** Python 3.12 only; the `[parquet]` extra and both SIGILL
probes are gone (pyarrow is a regular dependency). The numpy/pandas caps stay, restated in
pyproject.toml as deliberate upgrade gates against the golden fixture, not as a CPU constraint.

`pyproject.toml:23-28` pins `numpy>=1.26,<2.3` / `pandas>=2.2,<3.0` with the
rationale "newer numpy wheels raise the x86-64 CPU baseline and die with SIGILL
on older CPUs ... Relax only if all deploy targets expose x86-64-v2+".
The deploy host `leaf` is an AMD EPYC 7352 (x86-64-v3, sse4_2/avx/avx2), so the
stated precondition for relaxing is already met. Same for the `[parquet]`
pyarrow extra and the two SIGILL probes (`app/services/backfill.py`,
`Containerfile`). Not a bug — a pin that is now costing currency for no reason.
Deliberate change, needs its own review.

## U-02 — `render_time_status` has no callers
`app/alerts/render_context.py:199` implements mandate 17.5 and returns
`UNKNOWN_AT_RENDER`, but nothing anywhere calls it. Listed here only for
tracking: it IS in scope for the alert system and is scheduled as Phase B5
(audit B-14, per-member JIT rendering). Remove from this file once B5 lands.

## U-03 — stale "Atom N2800" framing in alert code
**Resolved 2026-10-04 (re-evaluation A8):** the dispatcher's docstring now says what the code
does: overlapping passes claim a row once and keep the budget caps, because every other claimed
or sending row reserves a slot. The caps do not depend on a single worker.

`app/alerts/dispatcher.py:3` justifies the single-worker design with "On the
Atom N2800 target this is a capacity decision". The host is an EPYC now. The
single-worker decision stays correct (the pre-send budget recheck depends on
it); only its stated reason is wrong. In scope, folded into Phase C5 docs.

## U-04 — the phrase set is German, the host-side outage notifier is English
`deploy/notify-outage.sh` sends `bubblegauge: alert watchdog unit failed ...`
in English, while every fragment in `config/alert_phrases.v3.2.json` is German.
Both are correct in isolation: the notifier is a host-side script that must run
with no application code available, and it is deliberately not built from the
phrase set (a phrase set it could not load is one of the things it exists to
report). But the operator receives two message families in two languages from
one product. Cosmetic, and a deliberate choice either way — parked rather than
decided unilaterally.

## U-05 — `.secrets.baseline` cannot carry 64-hex digests
The byte-identical baseline ratchet and the entropy detector are in tension:
detect-secrets cannot distinguish a published sha256 digest from a leaked
token, so any doc or fixture that gains a literal 64-hex string needs a
`# pragma: allowlist secret` rather than a baseline entry. Discovered while
adding gate evidence. Not a defect in either tool — a constraint worth writing
down, because the obvious fix (grow the baseline) is the wrong one.
