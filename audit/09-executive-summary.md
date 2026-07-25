# audit/09 — Executive summary

**SCOPE: TRACKS A/B (CATALOGUE v1.0) AUDITED AGAINST THE IN-REPO MANDATE
TEXT. TRACK C (40 CHECKS) CARRIES FOUNDING VERDICTS BUT ITS NORMATIVE TEXT IS
NOT IN THE REPOSITORY — THOSE VERDICTS ARE NOT RE-VERIFIABLE HERE —
CONSTITUTION `IN_FORCE_PROVISIONAL` — NOT CLEARED FOR PRODUCTION TRAFFIC:
`production_eligible: false` (computed), OPERATOR RUNS PROD UNDER RECORDED
ACCEPTANCE, NOT CLEARANCE.**

*Rewritten 2026-07-25 at the mandate-enforcement installation + independent
re-audit. Supersedes the founding 2026-07-14 summary (which reported the
discovery-time state: pipeline catching ~1 of 6 seeded defects on a CI that
no runner executed; that state is over and its record lives in audit/02 and
audit/05).*

## What is still broken (lead items)

0. **Two STOP-SHIP records remain open** in the machine record: **B-06**
   (below) and **C-01**, whose verdict was re-banded to PARTIAL in its own
   text while its `band` field still reads STOP-SHIP — the count is
   deliberately left conservative rather than quietly downgraded.
1. **B-06 (STOP-SHIP):** nine provider credentials disclosed in chat remain
   unrotated. Compensating controls stand (clean repo history, fail-closed
   placeholder key); nothing substitutes for rotation.
2. **A-01/B-35 (BLOCKER-1):** the gates run and block, but branch protection
   (required checks, no bypass, CODEOWNERS review) is not enabled — an
   operator console action. Until it lands, write separation is
   hash-attested but ultimately advisory.
3. **A-02 (BLOCKER-1):** the suite's mutation score is unmeasured; the
   mutation clause of constitution Article III is explicitly unenforced.
4. 29 further operator-accepted blocker-band findings, each with
   compensating control + tripwire (`audit/06`, machine-mirrored in
   `governance/accepted-residuals.json` — the gate fails CI on any drift
   between that register and reality).

## What watches for the next break

- **Every change:** blocking CI (ruff-S, pip-audit, detect-secrets, the full
  suite at the ratcheted floor) + the mandate gate (status consistency, hash attestation over the
  governance set incl. the gate's own authority files, S11 ratchets, S12
  seeded-defect calibration) + the cross-vendor adversarial panel on pull requests (that one
  needs a configured key and does not run on push or on the schedule).
- **Weekly:** the same stack fires on schedule with no diff — the heartbeat
  that distinguishes "quiet" from "dead".
- **Proven, not assumed:** 12-tamper adversarial audit 2026-07-25 — 11
  fail-closed; the 1 fail-open found (hand-loosened ratchet baseline) fixed
  same-day and re-proven. The calibration corpus caught a live secret-scan
  gap (UUID-format tokens) during its own installation.

## What breaks next if nothing is done

Nothing in this repository rotates the credentials or flips branch
protection; those decay paths have no machine owner by nature. The regime
converts them from silent to loud: `production_eligible` stays pinned
`false`, the register cannot be pruned without the gate noticing, and the
ratification path is written down (`audit/08-standing-regime.md`). The
remaining honest exposure is anything the founding engagement's 31
NOT-APPLICABLE verdicts assumed about the architecture — two of those
assumptions (no tool calls, single tenancy) are now re-validated by the
calibration on every run; the rest re-validate at the annual catalogue
re-run.
