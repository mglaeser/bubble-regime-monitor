# Part 3 implementation review — not adopted / not implemented

**Review date:** 2026-07-22  
**Scope:** repository contents at `b0aa74a`, the tracked GitHub Actions workflow, and
the repository's existing audit evidence. This review does **not** assert the state of
GitHub branch protections, secrets, service identities, or a separate policy-bundle
repository: none is represented by verifiable configuration in this checkout.

## Verdict

**No — the Part 3 addendum is not fully implemented. It has not been adopted as a
repository control, and none of its required verification-economics enforcement is
present.** The existing workflow is a useful deterministic quality/security job, but
it is not a panel router, a verifier ledger, or long-term enforcement of Part 3.

This is a `STOP-SHIP` governance gap under Part 3's own adoption rule. In particular,
the required `D-01`, `D-02`, `D-03`, and `D-09` controls have no evidence-backed
implementation. Therefore `production_eligible` must be treated as **false** until a
separately write-protected implementation and its required evidence exist. This report
does not mark prior findings closed and does not substitute prose for a gate.

## Evidence checked

The following mandated artifacts are absent from the tracked repository:

| Required artifact | Result | Consequence |
| --- | --- | --- |
| `governance/mandate/part3.md`, manifest, regenerated mandate and re-attestations | Missing | The addendum was not adopted or integrity-attested. |
| `governance/verification-routing.json` | Missing | No band-to-rung mapping, override set, excluded-type policy, panel inventory, output contract, escalation probabilities, or key commitment. |
| `audit/00-audit-surface.json`, `audit/03b-coverage-ledger.md`, `audit/00-check-catalogue.json` | Missing | Routing cannot be deterministically derived from catalogue coverage; unknown-path-to-`P4` cannot be enforced. |
| `audit/evidence/routing-ledger` | Missing | No append-only record of content hashes, draw/executed rungs, panel identities/versions, or reasons. |
| `audit/10-verification-economics.md` | Missing | No permanent SLI, escalation, calibration, paired-comparison, or drift evidence. |

The only workflow, `.github/workflows/ci.yml`, runs ruff, an advisory mypy job,
`pip-audit`, `detect-secrets-hook`, pytest, and an image build. It invokes no
independent verifier, router, HMAC draw, verdict parser, asset ingest gate, ledger
writer, reconciliation process, or scheduled calibration. Its own comments say that
required-check enforcement still needs external branch protection. Existing audit
records independently retain the same open branch-protection and different-vendor
verification gaps.

## Track D disposition

| Check | Result | Missing enforced mechanism / required evidence |
| --- | --- | --- |
| D-01 router independence | **FAIL** | No write-separated policy bundle, router inputs, key isolation, entitlement reconciliation, or override tests. |
| D-02 every change has an independent panel | **FAIL** | No verifier invocation, co-reviewer, vendor/model assertion, or per-merge panel record. |
| D-03 keyed, reconstructible escalation | **FAIL** | No epoch-key commitment, HMAC draw, closed-epoch reconstruction, or grinding detector. |
| D-04 rung secrecy | **FAIL** | No runner/ledger boundary, normalised queue, or side-channel inference test. |
| D-05 calibration and zero misrouting escape | **FAIL** | No seeded per-rung corpus, suspension control, catch-rate baseline, or monthly job. |
| D-06 paired comparison | **FAIL** | No escalated sample, paired verdict data, confidence intervals, or automatic rung demotion. |
| D-07 routing-drift ratchet | **FAIL** | No routing distribution SLI, cheap-rung ceiling, append-only exclusion control, or release block. |
| D-08 panel supply chain | **FAIL** | No pinned verifier inventory, swap assertion, re-baselining, or upward-only fallback. |
| D-09 inspectable, unsuppressible reasons | **FAIL** | No fixed output parser, Runner-owned append-only ledger, CI rendering, or reason-less response drill. |
| D-10 terse output contract | **FAIL** | No schema/budget validator, response-length SLI, or rejection drill. |
| D-11 asset exclusion deterministic gate | **FAIL** | No byte-level media verifier, metadata transform, SVG allowlist sanitiser, provenance, or seeded fixture gate. |

## What the existing CI does and does not establish

The current CI is still valuable as a baseline deterministic gate: it makes lint,
dependency audit, secret scan, tests, and image construction visible. However:

1. **It cannot satisfy panel presence.** No job calls a differently-vendored verifier,
   validates the closed verdict grammar, or produces a machine-readable reason.
2. **It cannot satisfy unpredictable escalation.** There is no content-bound HMAC
   draw, protected epoch key/commitment, reconstruction drill, or anti-grinding state.
3. **It cannot satisfy write separation.** The workflow and all in-repository policy
   candidates are normal tracked files. No repository file can prove the required
   separation from a code-writing identity; that must be enforced by a distinct policy
   repository and GitHub rules/credentials.
4. **It cannot satisfy long-term enforcement.** `on: push` and `pull_request` provide
   no scheduled reconciliation, calibration injection, SLI publication, model-swap
   assertion, or release freeze/demotion mechanism. Moreover, the workflow explicitly
   records required status-check enforcement as an out-of-repository action.

## Remediation sequence — do not claim compliance before completion

1. **Adopt and attest the mandate.** Add the exact Part 3 source, ordered manifest
   hash, generated combined mandate, and constitution/manifest/combined attestations
   in the *policy-bundle repository*. File the three specified weakening amendments as
   open findings. Do not fabricate hashes or declare them closed in this application
   repository.
2. **Build the separately operated Runner and ledger.** The Runner—not PR-controlled
   CI—must derive assignment only from protected catalogue/coverage artifacts plus
   computed capability/schema/egress diffs; fail expensive; make and record the HMAC
   draw; enforce `executed = max(assigned, drawn, escalations)`; and write immutable
   panel records and reasons.
3. **Enforce the panel contract before merge.** Configure pinned, different-vendor
   models for `P1`–`P4`; keep the co-reviewer on every panel; validate vendor diversity,
   disagreement/co-reviewer escalation, and the exact response grammar/budget. Missing,
   empty, or malformed responses must block instead of silently passing.
4. **Add deterministic asset ingress.** Verify bytes rather than extensions, transform
   raster metadata away, reject invalid/costumed payloads, and fail closed through an
   explicit SVG element/attribute allowlist. Run it per file, not as a change-wide
   exemption.
5. **Turn it into a durable gate.** Enable branch protection/rulesets requiring the
   external Runner result with no bypass available to code-writing identities. Add
   scheduled (or external-scheduler) reconciliation, closed-epoch reconstruction,
   seeded misrouting/reason-less/asset drills, panel-supply-chain reassertion, SLI
   publishing, and automatic rung demotion/release freeze. Keep evidence in the
   append-only ledger and render CI from that ledger, never mutable job logs.
6. **Finish the definition of done with observed evidence.** Complete the 200-change
   observe-only burn-in, at least one independent closed-epoch reconstruction, every
   D-01–D-11 probe, and a recorded drill in which each standing control actually
   blocked or suspended the required path. Only then may the four `BLOCKER-1` adoption
   controls and the three weakening findings be considered for closure.

## Re-review acceptance criteria

A follow-up review should be limited to executable evidence: policy-bundle commit and
write-access proof; protected Runner configuration; routing/contract/asset unit tests;
CI and scheduler records; append-only ledger records; model/vendor pins; the burn-in
and reconciliation windows; and evidence that each fail-closed control has blocked a
synthetic violation. Configuration prose, a successful local test run, or an
in-repository workflow alone is insufficient for any of D-01 through D-11.
