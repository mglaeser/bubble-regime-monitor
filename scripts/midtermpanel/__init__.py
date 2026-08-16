"""The mid-term single-repository review panel — deliberately NOT the trusted lane.

## Why this package exists separately

The trusted lane (`scripts/trustedlane/`) is designed for an architecture this
repository does not have: a protected default ref, environment-scoped secrets
with a deployment-branch policy, and sixteen authenticated operator
prerequisites. On GitHub Free a private repository has neither native branch
protection nor environment-scoped Actions secrets, so that lane cannot be
activated honestly. `phases.IMPLEMENTED_PHASE` is `D0` and every
capability-obtaining function in the trusted lane refuses anything else.

The operator has chosen a mid-term architecture instead: one repository, a
PERSISTENT repository-level Actions secret, and an automatic three-model panel
that runs from the default branch after ordinary CI succeeds.

That is a genuinely weaker thing, and the whole point of putting it in its own
package is that it cannot quietly become the stronger one.

## What this package must never do

**It must never raise `phases.IMPLEMENTED_PHASE`.** Raising it to D1 or D2 would
activate the trusted lane — a credential-bearing, environment-gated,
operator-attested path whose preconditions are all still open. The mid-term
panel therefore obtains its own capabilities through its own seam
(`midtermpanel.transport`) and never calls `trustedlane.transport.read_credential`,
`trustedlane.signing.read_signing_key` or
`trustedlane.statustransport.read_installation_token`.

**It must never emit a `TRUSTED_*` evidence class or publish a `trusted-*`
status.** Both name sets are reserved for the future write-separated
implementation. `assert_not_trusted_class` and the status registry enforce this
rather than trusting anyone to remember it.

Pure helpers ARE reused — digests, action pinning, status-name registration,
engine loading — because duplicating them would create a second copy that drifts.
The rule is narrower and mechanical: reuse anything that computes, never anything
that grants.

## The residual, stated once, plainly

A repository-level Actions secret is readable by workflows in the repository. A
pull request that adds or edits a `pull_request` workflow may be able to
reference it. The privileged panel itself is safe from this — it runs from the
default branch and treats the candidate as inert data — but the panel reviewing a
workflow-changing PR does not stop that PR's own workflows from reading the
secret. The operator accepts this. Nothing in this package may describe the
result as write-separated, independent, or trusted.

## The secret-name collision, recorded because it is load-bearing

`SECRET_NAME` below is `TRUSTED_VERIFIER_OPENAI_KEY`, at the operator's explicit
instruction, and it is the same name the trusted lane expects as an ENVIRONMENT
secret. Trusted-lane operator prerequisite 6 (`no_repository_or_org_fallback`)
exists specifically to forbid a repository-level secret of that name, because a
repository secret is readable from any ref and silently defeats an environment's
deployment-branch policy.

So installing this secret makes prerequisite 6 permanently unsatisfiable until
the secret is removed. That is not a reason to refuse the operator's choice; it
IS a reason that D1/D2 activation must refuse while the repository secret exists,
which `assert_trusted_lane_still_refuses` states as an assertion rather than as a
paragraph someone has to remember reading.
"""

from __future__ import annotations

#: The repository this panel is allowed to run in. Same value the trusted lane
#: pins; imported rather than re-declared would create an import cycle at
#: package-init time, so it is re-stated and asserted equal by test.
REPOSITORY_NUMERIC_ID = 1297332828

#: The repository-level Actions secret carrying the panel's provider key.
#:
#: See the module docstring: this name collides with the trusted lane's
#: environment-secret name by operator instruction. The collision is enforced as
#: a D1/D2 refusal, not merely documented.
#: The NAME of the secret, never its value. `ruff`'s S105 flags this because a
#: credential-shaped identifier assigned a string literal is usually a leak; it
#: is a name-based heuristic and this is the case it gets wrong.
#:
#: Deliberately the SAME string as `trustedlane.runtimebinding.PROVIDER_SECRET_NAME`,
#: and a test asserts the two are equal rather than merely similar. If they ever
#: diverge the collision described above stops being real and this module's
#: warning about prerequisite 6 becomes a false alarm — so the identity is
#: pinned, not left to coincidence.
SECRET_NAME = (
    "TRUSTED_VERIFIER_OPENAI_KEY"  # noqa: S105 - a NAME; pragma: allowlist secret
)

#: The ordinary-CI workflow whose completion triggers the panel. Matched by
#: workflow NAME, not by filename: `workflow_run` reports the name, and keying on
#: a filename would silently stop triggering if the file were renamed.
CI_WORKFLOW_NAME = "ci"

#: The two statuses the panel publishes onto the exact candidate head SHA.
#:
#: Never `trusted-*`. A mid-term panel satisfying a requirement written for the
#: trusted lane is the same fail-open as an inactive job satisfying a review
#: requirement, which is the defect `statusnames.py` was written for.
COUNT_STATUS = "midterm-panel-count"
REVIEW_STATUS = "midterm-panel-review"
PANEL_STATUSES = (COUNT_STATUS, REVIEW_STATUS)

#: Evidence classes this package may emit.
COUNT_EVIDENCE_CLASS = "MIDTERM_SINGLE_REPO_COUNT_EVIDENCE"
PANEL_EVIDENCE_CLASS = "MIDTERM_SINGLE_REPO_PANEL_EVIDENCE"
#: What a run that REFUSED emits. A separate class on purpose: a refusal
#: record is not a verdict, and every loader that expects
#: `PANEL_EVIDENCE_CLASS` must refuse this rather than read a decision out of
#: a run that never reached one. It is written to its own file for the same
#: reason.
PANEL_REFUSAL_EVIDENCE_CLASS = "MIDTERM_SINGLE_REPO_PANEL_REFUSAL"
MIDTERM_EVIDENCE_CLASSES = (COUNT_EVIDENCE_CLASS, PANEL_EVIDENCE_CLASS,
                            PANEL_REFUSAL_EVIDENCE_CLASS)

#: Evidence classes this package may NEVER emit, whatever it observes.
#:
#: Listed explicitly rather than derived from a `TRUSTED_` prefix. A prefix rule
#: would be defeated by the next class that means "trusted" without saying so —
#: `WRITE_SEPARATED_REVIEW_EVIDENCE` contains no `TRUSTED` at all and is exactly
#: the claim this architecture cannot make.
FORBIDDEN_EVIDENCE_CLASSES = (
    "TRUSTED_COUNT_EVIDENCE",
    "TRUSTED_EXECUTION_EVIDENCE",
    "WRITE_SEPARATED_REVIEW_EVIDENCE",
    "INDEPENDENT_THIRD_PARTY_ATTESTATION",
)

#: Status names this package may never publish, for the same reason.
FORBIDDEN_STATUSES = (
    "cross-vendor",
    "independent-verify-inactive",
    "trusted-verifier-count",
    "trusted-cross-vendor-review",
)

#: The governed panel. Order is not significance; the roles below are.
PANEL_MODELS = ("gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini")

#: The required approver. A dated snapshot of the same model counts; a variant
#: suffix does not. `independent_verify.model_matches` implements exactly that
#: and is reused rather than re-derived.
REQUIRED_APPROVER = "gpt-5.6-sol"

#: Distinct non-approver models that must also approve. Repeat votes from one
#: model are not independent corroboration.
MIN_DISTINCT_OTHER_APPROVALS = 1

#: Mid-term policy turns STRICT refutation ON.
#:
#: `independent_verify.strict_any_refutation` is opt-in and OFF by default,
#: because the reference mechanism greens on "Sol + one corroborator" even when a
#: third voice refutes. The mid-term plan requires that ANY valid refutation
#: blocks, so this package sets it on explicitly. Recorded as a named constant
#: rather than a literal `True` at a call site, because it is a policy decision
#: that differs from the default and a reader deserves to find it.
STRICT_ANY_REFUTATION = True

#: Paths whose modification makes a pull request the highest-risk class while a
#: repository-scoped provider secret exists. The panel reviews such a PR's diff
#: as data; it cannot stop that PR's OWN workflows from reading the secret.
HIGH_RISK_PATH_PREFIXES = (
    ".github/workflows/",
    ".github/actions/",
    "scripts/trustedlane/",
    "scripts/midtermpanel/",
)

#: The marker a high-risk pull request receives in every summary.
HIGH_RISK_MARKER = "HIGH_RISK_WORKFLOW_CHANGE"

#: The live workflow file this package deploys.
WORKFLOW_FILENAME = "midterm-panel-review.yml"

#: The workflow's declared `name:`.
WORKFLOW_NAME = "midterm-panel-review"

#: What this architecture is, in one machine-readable word set. Anything that
#: reports panel results must carry these verbatim rather than paraphrasing.
ARCHITECTURE_FACTS = {
    "repository_plan": "GITHUB_FREE_PRIVATE",
    "native_branch_protection": "UNAVAILABLE_ACCEPTED_RESIDUAL",
    "native_environment_secret_isolation":
        "UNAVAILABLE_ACCEPTED_RESIDUAL",  # pragma: allowlist secret
    "provider_secret_scope":
        "PERSISTENT_REPOSITORY_ACTIONS_SECRET",  # pragma: allowlist secret
    "merge_enforcement": "HUMAN_EXACT_HEAD_PROTOCOL",
    "write_separated": False,
    "trusted_review_authority": "NOT_WRITE_SEPARATED",
    "production_eligible": False,
}
