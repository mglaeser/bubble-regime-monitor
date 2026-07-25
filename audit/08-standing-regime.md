# audit/08 — The standing regime (§9, instantiated 2026-07-25)

**This is the deliverable that outlives every engagement.** Everything here is
enforced by code that runs without anyone remembering it; the items that
require the operator are named as such, dated, and carried in
`governance/accepted-residuals.json` — the mandate gate fails CI if that
register and reality drift apart.

Owning role for the regime: **operator (mglaeser)**. The regime's health
metric: the seeded-defect calibration staying green (§9.3 — it runs on every
change, so a regression is discovered at the next push, not the next audit).

## The enforcement stack (what runs on every change)

| Control | Mechanism | Fails closed on |
|---|---|---|
| Deterministic gate | `ci.yml`: ruff (incl. S), pip-audit, detect-secrets-hook, full pytest suite — all blocking | any lint/vuln/secret/test failure |
| Mandate gate — status | `scripts/mandate_gate.py status` | findings/catalogue mismatch; PASS with null standing_control; any open blocker not operator-accepted; stale acceptance; governance hash mismatch; engagement-status drift |
| Mandate gate — ratchet | `mandate_gate.py ratchet` vs `audit/ratchet-baselines.json` | test-count floor falls; noqa/type-ignore/emoji ceilings rise |
| Mandate gate — calibration (S12) | `mandate_gate.py calibrate`: seeded corpus re-proven | any seeded class uncaught; credential-shape or vacuous-assert hit in live source; unresolvable live import; N/A-class architecture assumption voided |
| Independent adversarial verifier (S2) | `independent-verify.yml`: cross-vendor OpenAI panel, Sol veto, deterministic arbiter | refuted verdict, missing proof, insufficient approvals — on every PR |
| Write separation (B-35) | `.github/CODEOWNERS`: `.github/`, `scripts/`, `governance/`, frozen artifacts → operator | full force requires branch protection (open item below) |

## The ratchet register (S11)

Lives in `audit/ratchet-baselines.json`; enforced every run. Founding
baselines (measured 2026-07-25): tests ≥ 385; `# noqa` ≤ 20;
`# type: ignore` ≤ 2; emojis in source = 0 (Article XIV exclusivity).
Loosening any baseline requires an operator decision record in the file and
is automatically a finding. **Open slots** (may not be silently forgotten —
they are listed in the baseline file itself): mutation-score floor (A-02,
needs `mutmut`), quantitative pipeline catch-rate (currently binary).

## The calibration corpus (S12) — §9.3

From `audit/02-calibration.md`, grown by what has hurt this system since:

1. **Hard-coded credential (UUID shape)** → credential-shape scanner in the
   mandate gate. *2026-07-25: installing this calibration caught a live
   hole — detect-secrets misses `*_token` names and UUID-format values,
   which is exactly this app's real credential format. The scanner exists
   because the calibration fired.*
2. **Swallowed exception** → ruff S110 (blocking).
3. **Hallucinated dependency** → import-resolution check over every
   top-level `app/` import (new standing gate; `lppls` exempted as the
   documented optional engine).
4. **Vacuous test assertion** → dedicated scanner (ruff's S101 exempts
   `tests/`, so this class needed its own detector).
5. **Untrusted text → tool call** — N/A by architecture; the absence is
   re-validated every run (no `tools=` in `app/`).
6. **Cross-tenant ownership** — N/A by architecture; re-validated every run
   (no per-user/tenant columns in `app/models.py`).

## The cadence (§9.2)

| Cadence | What runs | Executor |
|---|---|---|
| Every change | The full enforcement stack above | GitHub Actions (blocking) |
| Every PR | Cross-vendor adversarial panel | `independent-verify.yml` |
| Weekly (Mon 06:17 UTC) | The full stack on an empty diff — the heartbeat: proves the gates still run and still catch, independent of anyone pushing | `ci.yml` `schedule:` |
| Per deploy | Health-check with automatic rollback (proven live: the B-12 rollback, `audit/05`) | `deploy.sh` on the host |
| 4-hourly | Production recompute; snapshots hash-stamped (RM-1) — the falsification evidence clock | APScheduler |
| Monthly (operator) | Restore drill from backup; credential-rotation review; branch-protection assertion | operator — **overdue = finding** |
| Quarterly (operator) | Re-read `governance/accepted-residuals.json` aloud: every entry still deserved? Prune what closed (the gate forces pruning), re-justify what stays | operator |
| Annually | Re-run the full 119-check catalogue against the then-current system | operator + agent session |

## Re-run triggers (§9.5)

New tool/connector → A-11, A-34, C-06, C-08 and the class-5 N/A voids (the
calibration will fail the build the moment `tools=` appears — this trigger
is wired, not remembered). New model/provider change → A-39 fleet
re-assertion, B-13. New data class or per-user state → C-01/C-04 and the
class-6 N/A void (wired: the models.py scan). Any incident → its defect
joins the calibration corpus before the incident closes.

## The decay watch (§9.4) — what is actually wired

- **Suppression creep** → noqa/type-ignore ceilings (wired).
- **Alarm dilution** → emoji-in-source ratchet (wired).
- **Gate capture** → CODEOWNERS + governance hash attestation (wired);
  *full* enforcement needs branch protection (open).
- **Threshold drift** → loosening a baseline without a decision record fails
  the gate (wired).
- **Regime stall** → the weekly scheduled run is the dead-man switch: if the
  regime stops running, the schedule's absence is visible in the Actions
  history; a stronger freeze (releases blocked on stale heartbeat) needs
  branch protection (open).
- **Registry rot** → the accepted-residuals register is pruned by force: a
  closed finding left in it fails the gate (wired).

## Open operator actions (the ratification path)

These cannot be closed by any agent, and every one is a standing
`production_eligible: false` reason:

1. **B-06** — rotate the 9 disclosed provider credentials; adopt host
   secret store.
2. **A-01/B-35** — branch protection: `test`, `image`, `mandate-gate`,
   `cross-vendor` as required checks, no bypass, on `main`.
3. **A-02** — wire `mutmut`, measure the mutation floor, add the ratchet
   slot.

When all three land: flip `constitution_state` to `RATIFIED` in
`governance/accepted-residuals.json` (decision record), prune the register,
and let `mandate_gate.py status --write` recompute — `production_eligible`
becomes `true` by computation, never by assertion.
