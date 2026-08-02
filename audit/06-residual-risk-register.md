# audit/06 — Residual risk register (Phase 6)

Every unfixed item at MUST-FIX or above, and the material SHOULD-FIX items, with a **compensating control** and an **executable tripwire** and an **owning role**. A risk with neither a compensating control nor a tripwire is not accepted — it is ignored; there are none of those here.

Owning role for all items: **operator (mglaeser)** — the sole maintainer and human-in-command.

| ID | Residual risk | Band | Compensating control (now) | Executable tripwire | Target |
|---|---|---|---|---|---|
| **B-06** | 9 provider credentials were disclosed in chat and are not yet rotated; no vault/rotation. | STOP-SHIP | Repo history is clean (verified); admin key now fails closed on the placeholder; reads expose no secret. | Add a startup log-warning + a monthly reminder; alert if `ADMIN_API_KEY` equals the placeholder (already 503s). | **Rotate all 9 at the providers before serving prod.** Then adopt a host secret store. |
| **A-01 / B-35** | CI is rebuilt to be blocking+green-capable, but nothing yet *enforces* it as a required check — an operator could still merge/deploy on red, and the code-author identity can edit `ci.yml`. | STOP-SHIP (esc.) | The rebuilt gate + `deploy.sh` health-check auto-rollback; this audit as the record. | Enable branch protection: require the `test` check, disallow force-push/bypass. A CI job that asserts `ci.yml` is unchanged vs a signed policy hash. | Branch protection owned by the human-in-command; policy file with separate write access. |
| **A-39** | No independent, different-vendor adversarial verifier; author and verifier share a model family. | STOP-SHIP (esc.) | Deterministic golden-fixture gate + (once wired) mutation gate as the non-model arbiter; the partial same-vendor verifier run this engagement. | Mutation score below threshold blocks merge (once `mutmut` is wired). | Add a different-vendor verifier for auth/scoring/deploy changes. |
| **A-02** | The suite's mutation score is unmeasured — its fault-detection power is unknown. | BLOCKER-1 | 171 passing tests + golden fixtures + the red→green evidence in `audit/05`. | `mutmut run` in CI; score `< 0.75` on core logic blocks. | Wire `mutmut` on `app/engine/` + `app/indicators/`. |
| **B-04 / C-03** | No hash-lockfile / dependency-existence gate; CI installs from floors. | BLOCKER-1 | `pip-audit` (blocking) + `detect-secrets` now in CI; every dep verified real this engagement; `lppls` exact-pinned. | `pip-audit` non-zero blocks; add a pre-install existence check. | Generate a hash lockfile; install from it in CI + Containerfile. |
| **A-24 / B-19 / B-31** | No SLO/error-budget; no tested backup/restore of the SQLite DB. | BLOCKER-1/2 | Strong graceful degradation; `/readyz`; `deploy.sh` auto-rollback. | A cron `sqlite3 .backup` + a scheduled restore-drill whose result is a signal; a freshness SLI alert. | Backup+restore drill this week; define an availability SLO. |
| **B-13 / B-24 / B-36** | Model is a floating alias; a silent provider model change could shift the judgment/SMS text with no pinned version to roll back to. | BLOCKER-2 | Intra-vendor fallback chain + deterministic degradation (never an outage); output is a disclaimered note, not a decision. | A canary re-eval of a fixed prompt on provider model change; a lint failing on `latest`-style aliases. | Pin a dated snapshot or document the alias as canonical + monitor deprecations. |
| **A-13** | mypy errors (advisory, not blocking); count is measured in CI, not pinned in prose (43 at founding 2026-07-14, materially higher now). | MUST-FIX | Advisory mypy still runs and is visible in CI (honestly labelled, not disguised). | Make `mypy app` blocking once the count is 0; a CI check that the error count does not rise. | Drive to zero, then block. |
| **C-05 / C-38** | The LLM note has no factual-grounding gate — it could misstate the snapshot it summarises. | SHOULD-FIX | Disclaimer + "not a probability" framing + `_clean_completion` shape check + science audit; single user. | A deterministic check that the note's stated direction/band is consistent with the snapshot; hallucination-rate monitor. | Add the grounding sanity check. |
| **C-09 / C-36** | AI-generated text is shown to a reader without an explicit "AI-generated" marker (EU AI Act Art. 50 from 2026-08-02). | BLOCKER-2/SHOULD | Disclaimer present; minimal-risk classification recorded (`audit/03` C-09). | Add `ai_generated: true` on the `judgment_call` field + a one-line SMS/UI disclosure. | Ship the marker before 2026-08-02. |
| **B-03 / B-28** | Logs are structured but there are no metrics/traces and detection routes to a human, not an automated response. | MUST-FIX | structlog + `/healthz`/`/readyz` + deploy-time auto-rollback. | A synthetic freshness check wired to restart/rollback. | Add OpenTelemetry traces + golden-signal metrics. |

## Acceptance

The **STOP-SHIP** items (B-06 rotation, A-01/B-35 branch protection, A-39 independent verifier) are **not closed by this engagement** — they require operator/repo-settings actions and a second vendor. They are accepted here only *with* the compensating controls and tripwires above, and they are the lead items in `audit/08`. Per the mandate, the honest status is: **the system should not be treated as independently verified until branch protection is enabled and credentials are rotated.**

## Addendum — 2026-07-26 (reconciled external review; audit/13)

- **V-TRUST (verifier trust boundary) — CRITICAL, OPEN, disputes A-39=PASS.**
  `independent-verify.yml` runs `actions/checkout` (PR head), injects
  `SECOND_VENDOR_API_KEY`/`OPENAI_API_KEY`, then executes the PR-controlled
  `scripts/independent_verify.py`. On a **same-repo** PR the author controls
  both that script and the workflow, so the vendor secret is exposed to
  PR-head code that could exfiltrate it, alter the reviewed diff, or hardcode
  green. Therefore the panel is **not** a trustworthy *independent* control on
  same-repo PRs, and the "independently enforced / permanently enforced" A-39
  framing is overstated. **Recommendation (operator):** reopen A-39 to
  PARTIAL, move verifier execution out of the application PR (a trusted
  external service / policy repo / GitHub App that fetches the diff as inert
  data and binds a status to exact base/head SHAs), and provision a dedicated
  low-privilege key. Until then the panel is **conditional advisory evidence**,
  and deterministic CI (ruff/pip-audit/detect-secrets/pytest/mandate-gate) is
  the only trusted in-repo control. Compensating control now: single committer,
  branch pushes only; tripwire: this residual + audit/13. Not closable in the
  application repo by an AI session.
- **SECRET-SCAN SCOPE — HIGH, OPEN.** The CI secret scan is
  `git ls-files | detect-secrets-hook` = current tracked files only; it does
  NOT scan the full PR commit range or deleted blobs, and detect-secrets
  excludes `audit/`. The custom UUID/entropy scanner covers tracked `*.py`
  only. Operator/follow-up: scan all tracked text, scan the PR commit range,
  and run a trusted redaction/secret pre-flight BEFORE any external model call
  in the verifier path. (review 3.7)

## Addendum — 2026-07-26 (Part 3 / Track D non-adoption)

- **D-ALL (Part 3 addendum unadopted) — STOP-SHIP, OPEN.** The independent
  review `audit/09-part3-implementation-review.md` (2026-07-22) finds the
  Part 3 verification-economics addendum (checks D-01…D-11) not adopted.
  **This item cannot be closed inside this repository**, and by design must
  not be:
  - The Part 3 **normative source text is not in this repo** (only the review
    is), so implementing D-01…D-11 here would require inventing the
    band-to-rung mapping, escalation probabilities, HMAC epoch structure,
    verdict grammar, response budgets, SVG allowlist and catch-rate baselines.
    That is barred by the frozen-methodology rule (never invent constants —
    `<PIN>` and stop) and by the review's own directive ("Do not fabricate…
    declare them closed in this application repository").
  - The review's required mechanisms are **out-of-repository by construction**:
    a write-separated policy-bundle repo, a separately-operated Runner (not
    PR-controlled CI), branch protection with no code-author bypass, and a
    200-change observe-only burn-in with recorded reconstruction/drill
    evidence. No file in this repo can satisfy or attest them.
  - **Compensating control (now):** `production_eligible` is computed `false`
    and stays so; the review is retained in-repo as the standing record; the
    existing cross-vendor panel (`independent-verify.yml`) and mandate gate
    remain in force as the partial deterministic substitute they already are.
  - **Executable tripwire / honesty note:** `production_eligible` is already
    computed `false` by the existing open A/B/C blockers, so nothing regresses
    while Track D is unadopted. This item is recorded **in prose only**,
    exactly like the review — it is deliberately **NOT** entered in the
    findings register or `governance/accepted-residuals.json`, because no
    D-01…D-11 check exists in the immutable catalogue and creating one
    requires the Part 3 source text this repo does not hold. Registering
    fabricated D-checks to get a machine tripwire would itself be the
    substitution the review forbids. Closure requires the executable evidence
    the review's "Re-review acceptance criteria" enumerates — not prose, not a
    local run, and not an in-repo check invented without its spec.
  - **Owner:** operator (mglaeser). Adoption is an operator + infrastructure
    action (policy-bundle repo, Runner, branch protection), not a code change
    an AI session can make in this repository.

## Addendum — 2026-07-25 (mandate-enforcement installation)

- **A-39 CLOSED**: the independent different-vendor adversarial verifier now
  exists and enforces — `independent-verify.yml` (OpenAI panel, required-
  approver veto, fail-closed deterministic arbiter), proven live across 16
  veto rounds on PRs #21/#22. Removed from the STOP-SHIP set.
- **A-01/B-01 partially discharged**: CI demonstrably executes and blocks on
  GitHub-hosted runners (the "no runner executes it" state is over). Branch
  protection remains the open operator action.
- The register above is now machine-enforced: `governance/accepted-residuals.json`
  mirrors it, and `scripts/mandate_gate.py` fails CI on any open blocker-band
  finding not in it, and on any entry that has closed but was not pruned.
