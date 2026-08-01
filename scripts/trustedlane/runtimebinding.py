"""EX5-R21 — what a valid MAC does NOT prove.

Authentication proves one thing exactly: *this payload was tagged by a key in
the operator's trust store*. It does not prove that the runtime is using the
fact the payload names.

That gap is not theoretical. Every example below is a genuinely authenticated
envelope, minted by the real operator key, that authorizes something other than
what the run is about to do:

* the typed schema accepts any positive `run_id`, and the cross-record check
  only requires `delete_failed_run.run_id == verify_run_404.run_id`. A validly
  MAC'd pair for run **1** satisfies prerequisites 1 and 2 while workflow run
  30214247762 — the one whose logs are believed to contain probe output — is
  still there;
* `approve_engine_identity` authenticates five digests. D1 separately verifies
  the artifact against `engine_artifact["expected_sha256"]`. Nothing compared
  the two, so an operator could approve engine A while the run loaded engine B,
  and both checks would pass;
* `protected_trusted_environment` authenticates an observation digest. D1
  separately evaluates `protectedstate.assert_ready_for_credential` over an
  observation document. Nothing required them to be the same observation;
* `approve_count_spending` authenticates caps. The active PINs carry their own.
  Two numbers for one budget is how the two copies end up disagreeing;
* `approve_generation_separately` authenticates a plan digest. D2 executes a
  plan. Nothing required it to be that plan.

**The shape of the fix.** One gate, after authentication and before any
capability is spent, that compares each authenticated payload against the
actual runtime fact it claims to authorize. It is deliberately a separate
function rather than more checks inside `trustedverifier`: authentication asks
"did the operator say this", and this asks "is this what is about to happen".
Those are different questions and a reader must be able to see both.

**Every mismatch is zero provider attempts**, because the gate runs before the
transport exists.

**What this module must never do.** Decide what the runtime facts ARE. It is
handed them — the observation the runtime used, the engine identity the runtime
verified, the policy records the runtime loaded — and compares. A version that
went and looked things up for itself could be handed a lie about where to look.
"""

from __future__ import annotations

import hashlib
import json

from .errors import refuse

#: The incident this lane exists downstream of. Workflow run 30214247762 is the
#: one whose logs are believed to contain probe output, and prerequisites 1 and
#: 2 are about THAT run. Fixed in trusted code rather than accepted as a
#: parameter: an incident id a caller supplies is an incident the caller chooses,
#: and `docs/TRUSTED_LANE_OPERATOR_ACTIONS.md` names this number to the operator.
INCIDENT_RUN_ID = 30214247762

#: The environment and secret names the trusted lane is actually configured
#: with. Prerequisite 5 says the replacement key exists ONLY as an environment
#: secret; an approval naming a different environment approves a different
#: containment.
TRUSTED_ENVIRONMENT_NAME = "trusted-verifier"
TRUSTED_GENERATION_ENVIRONMENT_NAME = "trusted-verifier-generation"
#: The NAME of the secret, never its value. `ruff`'s S105 flags this because a
#: constant whose name ends in KEY usually holds one; here it holds the
#: identifier prerequisite 5 approves, and the lane refuses to read the value at
#: all outside D1/D2.
PROVIDER_SECRET_NAME = (
    "TRUSTED_VERIFIER_OPENAI_KEY"  # noqa: S105 - a NAME; pragma: allowlist secret
)

#: Which prerequisite binds which runtime fact. Kept as data so the coverage
#: test can assert that every prerequisite carrying a runtime-comparable field
#: is actually compared — a binding that exists in prose and not in this table
#: is a binding nothing performs.
RUNTIME_BOUND_PREREQUISITES = (
    "delete_failed_run",
    "verify_run_404",
    "install_environment_key",
    "protected_trusted_environment",
    "authorize_capability_policy",
    "approve_review_request_policy_v2",
    "approve_engine_identity",
    "approve_bootstrap_branch",
    "approve_count_spending",
    "approve_generation_separately",
)


def observation_digest(observation) -> str:
    """The digest an operator approves when they approve a protection state.

    Over the canonical JSON of the exact document the runtime evaluated, so
    "the operator approved this protection state" and "the runtime checked this
    protection state" are the same sentence about the same bytes."""
    blob = json.dumps(observation, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    return hashlib.sha256(b"protected-state-observation-v1\x00"
                          + blob).hexdigest()


def _require(observed, approved, *, prerequisite: str, field: str) -> None:
    if observed != approved:
        refuse(f"category=authorization_does_not_match_the_runtime "
               f"prerequisite={prerequisite} field={field} — the operator "
               "authenticated one value and this run is using another. A valid "
               "MAC proves the operator said something; it does not prove they "
               "said it about this")


def _typed(record_set, key: str):
    """The typed payload for one prerequisite, or a refusal naming it."""
    return record_set.require(key).typed_payload


def assert_authorizations_match_runtime(record_set, *, phase: str,
                                        observation, engine_identity: dict,
                                        capability_policy_sha256: str,
                                        review_request_policy_sha256: str,
                                        bootstrap: dict,
                                        pin_values: dict,
                                        candidate_range: dict,
                                        executable_plan_sha256=None) -> dict:
    """The gate. Runs after authentication, before any capability is spent.

    `executable_plan_sha256` is None in D1 — there is no executable plan yet,
    which is what D1 produces — and required in D2, where it is the digest of
    the plan D1 signed. That asymmetry is the point of prerequisite 16: an
    approval to count is not an approval to generate, and an approval to
    generate names WHICH plan."""
    # The phase/plan shape, FIRST. "D1 was handed an executable plan" and "D2
    # was handed none" are caller errors, not authorization failures, and
    # discovering either after nine comparisons reports the wrong thing about
    # the wrong layer.
    if phase == "D2" and not executable_plan_sha256:
        refuse("category=generation_approval_has_no_plan_to_match — D2 "
               "executes a plan D1 signed; without that digest the approval "
               "names nothing this run can be compared against")
    if phase != "D2" and executable_plan_sha256:
        refuse("category=count_phase_given_an_executable_plan — D1 PRODUCES "
               "the plan; a D1 run handed one is being asked to count "
               "something it did not derive")

    checked = {}

    # 1-2. The incident. Both halves must name the run this lane exists about.
    for key in ("delete_failed_run", "verify_run_404"):
        _require(_typed(record_set, key)["run_id"], INCIDENT_RUN_ID,
                 prerequisite=key, field="run_id")
    checked["incident_run_id"] = INCIDENT_RUN_ID

    # 5. The environment and the secret name, as configured.
    environment = _typed(record_set, "install_environment_key")
    permitted_environments = (TRUSTED_ENVIRONMENT_NAME,
                              TRUSTED_GENERATION_ENVIRONMENT_NAME)
    if environment["environment_name"] not in permitted_environments:
        refuse(f"category=authorization_does_not_match_the_runtime "
               f"prerequisite=install_environment_key field=environment_name "
               f"permitted={list(permitted_environments)} — an approval naming "
               "a different environment approves a different containment")
    _require(environment["secret_name"], PROVIDER_SECRET_NAME,
             prerequisite="install_environment_key", field="secret_name")
    checked["environment_name"] = environment["environment_name"]

    # 7. The protection state, by the digest of the exact document evaluated.
    observed = observation_digest(observation)
    _require(observed,
             _typed(record_set,
                    "protected_trusted_environment")["protection_observation_sha256"],
             prerequisite="protected_trusted_environment",
             field="protection_observation_sha256")
    checked["protection_observation_sha256"] = observed

    # 9, 12. The two governed policies, as the engine actually loaded them.
    _require(capability_policy_sha256,
             _typed(record_set,
                    "authorize_capability_policy")["capability_policy_sha256"],
             prerequisite="authorize_capability_policy",
             field="capability_policy_sha256")
    _require(review_request_policy_sha256,
             _typed(record_set, "approve_review_request_policy_v2")[
                 "review_request_policy_sha256"],
             prerequisite="approve_review_request_policy_v2",
             field="review_request_policy_sha256")
    checked["capability_policy_sha256"] = capability_policy_sha256
    checked["review_request_policy_sha256"] = review_request_policy_sha256

    # 14. The engine, all five digests, against the one this run verified.
    approved_engine = _typed(record_set, "approve_engine_identity")
    if not isinstance(engine_identity, dict):
        refuse("category=engine_identity_not_supplied — the gate compares the "
               "approved engine to the one this run verified; with nothing to "
               "compare against, approving an engine approves nothing")
    for field in ("engine_artifact_sha256", "engine_source_sha256",
                  "runtime_lock_sha256", "sbom_sha256", "provenance_sha256"):
        if field not in engine_identity:
            refuse(f"category=engine_identity_incomplete field={field} — five "
                   "digests were approved and fewer were established; the "
                   "unestablished one is the interesting one")
        _require(engine_identity[field], approved_engine[field],
                 prerequisite="approve_engine_identity", field=field)
    checked["engine_artifact_sha256"] = engine_identity["engine_artifact_sha256"]

    # 15. The bootstrap branch and head actually deployed.
    approved_bootstrap = _typed(record_set, "approve_bootstrap_branch")
    for field, source in (("branch", "branch"),
                          ("branch_head_sha", "head_sha"),
                          ("bootstrap_policy_sha256", "policy_sha256")):
        if source not in bootstrap:
            refuse(f"category=bootstrap_state_incomplete field={source}")
        _require(bootstrap[source], approved_bootstrap[field],
                 prerequisite="approve_bootstrap_branch", field=field)
    checked["bootstrap_head_sha"] = bootstrap["head_sha"]

    # 11. Spending, against the ACTIVE PINs and the exact range.
    spending = _typed(record_set, "approve_count_spending")
    _require(spending["authorized_target_base_sha"],
             candidate_range["target_base_sha"],
             prerequisite="approve_count_spending",
             field="authorized_target_base_sha")
    _require(spending["authorized_diff_base_sha"],
             candidate_range["diff_base_sha"],
             prerequisite="approve_count_spending",
             field="authorized_diff_base_sha")
    _require(spending["authorized_head_sha"], candidate_range["head_sha"],
             prerequisite="approve_count_spending", field="authorized_head_sha")
    # The caps the operator approved must be the caps the run will enforce.
    # These live in two places by design — the PIN record is what the engine
    # enforces, the spending envelope is what the operator agreed to — and two
    # numbers for one budget is how the two copies end up disagreeing.
    _require(spending["count_attempt_cap"],
             pin_values["VERIFIER_MAX_COUNT_CALLS"],
             prerequisite="approve_count_spending", field="count_attempt_cap")
    _require(spending["micro_usd_cap"],
             pin_values["VERIFIER_COST_CAP_MICRO_USD"],
             prerequisite="approve_count_spending", field="micro_usd_cap")
    checked["count_attempt_cap"] = spending["count_attempt_cap"]
    checked["authorized_input_tokens"] = spending["max_input_tokens"]

    # 16. D2 only: WHICH plan was approved for generation.
    if phase == "D2":
        generation = _typed(record_set, "approve_generation_separately")
        _require(executable_plan_sha256, generation["executable_plan_sha256"],
                 prerequisite="approve_generation_separately",
                 field="executable_plan_sha256")
        for field, source in (("authorized_target_base_sha", "target_base_sha"),
                              ("authorized_diff_base_sha", "diff_base_sha"),
                              ("authorized_head_sha", "head_sha")):
            _require(candidate_range[source], generation[field],
                     prerequisite="approve_generation_separately", field=field)
        _require(generation["generation_attempt_cap"],
                 pin_values["VERIFIER_MAX_GENERATION_CALLS"],
                 prerequisite="approve_generation_separately",
                 field="generation_attempt_cap")
        checked["executable_plan_sha256"] = executable_plan_sha256

    return {"runtime_binding": "AUTHORIZATIONS_MATCH_RUNTIME",
            "phase": phase,
            "prerequisites_bound": list(RUNTIME_BOUND_PREREQUISITES),
            "checked": checked,
            "honest_scope": (
                "every authenticated payload above was compared to the fact "
                "this run established for itself. It does not prove the "
                "operator was right — only that they were talking about this "
                "run")}
