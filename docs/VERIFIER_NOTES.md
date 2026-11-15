# VERIFIER_NOTES — panel round log for the open PR (standing regime)

Reset on 2026-11-15, by the rule below ("This file is per-PR working state;
it resets at merge"): the round logs of #95 to #114, the last of them merged
on 2026-09-19, are in this file's history
(`git log -- docs/VERIFIER_NOTES.md`), with the reviewer guidance and the
lessons written between them. "Round 4" in the clause below is round 4 of
#95. The two owner-directed paragraphs stand as written; no pull request is
logged here now.

**Standing rule (owner-directed, 2026-08-28):** when a cross-vendor panel
refutation is *disproven*, the proof lives HERE — in the PR diff, where the
next panel round reads it — with `file:line` evidence and a reproducible
check. When a refutation is *upheld*, the fix and its test pin are recorded.
A finding is never ignored: it is either fixed or disproven on the record.
This file is per-PR working state; it resets at merge.

**Anti-backdoor clause (owner-directed, 2026-08-28):** the disproof path is
NOT an escape hatch. The default disposition of every finding is **FIX**.
Before any disproof is recorded, the author must adversarially challenge it:
steelman the finding, assume the verifier is right, and try to construct the
failure it describes. A disproof is admissible only when (a) that
construction is demonstrably impossible, (b) the evidence is a *mechanical,
reproducible check* (a grep, a test, a line number — never an argument), and
(c) no partial validity remains — a finding that is even partially right
takes the fix path (see round 4: literal claim wrong, drift risk real →
fixed). If a disproof is later shown wrong, the finding is reinstated and
fixed with priority.
