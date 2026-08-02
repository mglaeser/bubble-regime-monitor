# Ungated-amendment refusal — proof log (2026-07-25)

Per the monthly cadence requirement (mandate §9.2: "ungated
constitution-amendment attempt (must be refused)"), executed as part of the
2026-07-25 independent tamper audit in an isolated worktree:

- **Attempt:** append a line to `governance/constitution.md` without
  updating `governance/mandate/manifest.json`.
- **Result:** `mandate_gate.py status` exited 1: "constitution.md hash does
  not match governance/mandate/manifest.json — amendments must go through
  the gate and bump the attested hash (Article XIII)". REFUSED.
- **Attempt:** append a line to `governance/mandate/part1.md` (the immutable
  mandate text).
- **Result:** exit 1, "part1.md hash mismatch vs manifest". REFUSED.
- Ten further tampers (findings edits, status drift, register deletion,
  ratchet/emoji/credential/import/vacuous seeds) all failed closed; the one
  fail-open found (hand-loosened ratchet baseline) was closed the same day
  by adding the baseline file to the attested set — re-tested: REFUSED.

Full table in `audit/05-verification.md` (2026-07-25 addendum).
