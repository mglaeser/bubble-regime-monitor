"""R04/R13 — the plan D1 counted, as an object D2 can execute.

D1 used to publish `trusted_plan_sha256` and nothing else. D2 received a plan
from somewhere and checked the digest matched. That looks like a binding and is
not one: the digest identifies a plan, and whoever hands D2 the plan chooses
which plan gets identified. If the two disagree D2 refuses, but if the supplier
hands over a plan whose digest happens to match, D2 executes it — and the
supplier is the runner, not the operator.

The mandate is explicit about the failure mode: *do not serialize only an
aggregate `trusted_plan_sha256` without the private plan it identifies*. So D1
emits the plan itself, signed, and D2 verifies the signature before it looks at
the contents.

**Why the plan is PRIVATE.** It contains the exact prompt bytes of every
request: the diff, the reconstructed atom text, the instructions. That is the
candidate's code and the reviewer's questions, and publishing either would make
"private artifact retention" (prerequisite 13) a statement about a file that had
already been published. The signed envelope goes into a retained private
artifact; only its digest appears in anything a pull request can see.

**What is in it, and why each part.** D2 has to be able to reconstruct every
execution request byte-for-byte and prove it is the one that was counted. That
needs the units, the batches, the panel, the per-model request hashes, the
challenge, and every policy digest the requests were built under. It does NOT
need — and must not carry — the credential, the signing key, the trust store, or
the operator's envelopes: D2 authenticates those itself.
"""

from __future__ import annotations

import hashlib
import json

from .errors import refuse

PLAN_CLASS = "TRUSTED_EXECUTABLE_REVIEW_PLAN"
PLAN_DIGEST_LABEL = b"trusted-executable-review-plan-v1"

#: Everything D2 needs and nothing it does not. Closed both ways: a missing
#: field is a plan D2 cannot execute, and an extra one is a field the digest
#: covers that no consumer reads.
PLAN_FIELDS = (
    "plan_class",
    "schema_version",
    "repository_numeric_id",
    "repository_identity",
    "target_base_sha",
    "diff_base_sha",
    "candidate_head_sha",
    "review_skeleton_sha256",
    "disposition_root",
    "requested_model_ids",
    "required_approver",
    "minimum_other_approvers",
    "final_units",
    "batches",
    "capability_policy_sha256",
    "review_request_policy_sha256",
    # The FULL record, not only its digest: `executor.reconstruct_batch_requests`
    # and `executor.execute_batch` read the panel, the approver, the minimum
    # corroborators and the max-output bound out of it. A digest alone would
    # force D2 to rebuild the record, and a rebuilt record is a second opinion
    # about the policy the requests were actually counted under.
    "review_request_policy",
    "operator_pin_record",
    "literal_authorization_set_sha256",
    "preflight_manifest",
    "count_ledger",
    "count_records",
    "cost_plan",
    "total_input_tokens",
    "authorized_input_tokens",
    "count_attempt_cap",
    "generation_attempt_cap",
    "execution_challenge",
    "execution_challenge_sha256",
    "trusted_run_id",
    "trusted_run_attempt",
    "produced_at",
)

#: Fields that must NEVER appear. D2 authenticates the operator's envelopes for
#: itself, and a plan carrying them would let D1's run decide what D2 treats as
#: authorized. The credential and the keys are listed for the same reason they
#: are listed everywhere else: the check is cheap and the failure is total.
FORBIDDEN_PLAN_FIELDS = (
    "credential",
    "signing_key",
    "trust_store",
    "operator_claims",
    "operator_envelopes",
    "mac_key",
)


def plan_digest(plan: dict) -> str:
    """Over every field except the digest itself.

    Includes `execution_challenge` in the clear, which is the whole reason the
    plan is private: D2 must send the challenge D1 counted, and a challenge D2
    could re-derive from a public digest is a challenge the models could
    re-derive too."""
    payload = {k: v for k, v in plan.items() if k != "plan_sha256"}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    return hashlib.sha256(PLAN_DIGEST_LABEL + b"\x00" + blob).hexdigest()


def build(core: dict, *, skeleton: dict, engine, record_set, candidate: dict,
          trusted_run: dict, produced_at: str, run_challenge: str,
          total_input_tokens: int, governed: dict) -> dict:
    """Assemble the plan from the core's output. No new decisions.

    Everything here was already computed by
    `verifier.finalize.prepare_review_plan_core`; this selects and names it.
    A field derived here rather than copied would be a second opinion about
    something the engine already decided."""
    from . import enginebridge

    spending = record_set.require("approve_count_spending").typed_payload
    generation = None
    if "approve_generation_separately" in record_set.records:
        generation = record_set.require(
            "approve_generation_separately").typed_payload
    plan = {
        "plan_class": PLAN_CLASS,
        "schema_version": 1,
        "repository_numeric_id": candidate["repository_numeric_id"],
        "repository_identity": record_set.occurrence["repository_identity"],
        "target_base_sha": candidate["target_base_sha"],
        "diff_base_sha": candidate["target_base_sha"],
        "candidate_head_sha": candidate["candidate_head_sha"],
        "review_skeleton_sha256": skeleton["review_skeleton_sha256"],
        "disposition_root": skeleton["coverage"]["disposition_root"],
        "requested_model_ids": list(enginebridge.model_panel(engine)),
        "required_approver": enginebridge.required_approver(engine),
        "minimum_other_approvers": 1,
        "final_units": core["final_units"],
        "batches": core["batches"],
        "capability_policy_sha256": governed["capability_policy_sha256"],
        "review_request_policy_sha256":
            governed["review_request_policy_sha256"],
        "review_request_policy": core["review_request_policy"],
        "operator_pin_record": core["operator_pin_record"],
        "literal_authorization_set_sha256": (
            record_set.literal_authorizations.digest()
            if record_set.literal_authorizations is not None else None),
        "preflight_manifest": core["preflight_manifest"],
        "count_ledger": core["count_ledger"],
        "count_records": core["count_records"],
        "cost_plan": core["cost_plan"],
        "total_input_tokens": total_input_tokens,
        "authorized_input_tokens": spending["max_input_tokens"],
        "count_attempt_cap": spending["count_attempt_cap"],
        "generation_attempt_cap": (generation["generation_attempt_cap"]
                                   if generation else None),
        # In the clear, and this artifact is private for exactly this reason.
        "execution_challenge": run_challenge,
        "execution_challenge_sha256": hashlib.sha256(
            run_challenge.encode()).hexdigest(),
        "trusted_run_id": trusted_run["id"],
        "trusted_run_attempt": trusted_run["attempt"],
        "produced_at": produced_at,
    }
    plan["plan_sha256"] = plan_digest(plan)
    return validate(plan)


def validate(plan) -> dict:
    """Shape, closed both ways, and the digest recomputed.

    Recomputed rather than read: the stored value is part of a document a
    caller controls, and comparing a value to itself always agrees."""
    if not isinstance(plan, dict):
        refuse("category=executable_plan_not_an_object")
    if plan.get("plan_class") != PLAN_CLASS:
        refuse(f"category=executable_plan_wrong_class "
               f"class={plan.get('plan_class')!r} expected={PLAN_CLASS}")
    missing = [f for f in PLAN_FIELDS if f not in plan]
    if missing:
        refuse(f"category=executable_plan_incomplete fields={missing} — D2 "
               "reconstructs every request from this document; a field it is "
               "missing is a request it cannot prove was the counted one")
    # FORBIDDEN before UNKNOWN, and the order is the whole point. Every
    # forbidden field is also an unknown field, so with the unknown check first
    # this one was unreachable — a plan carrying a credential was reported as
    # "unknown_fields fields=['credential']", which is true, uninteresting, and
    # buries the only sentence a reader needed. Found by a test that expected
    # the capability refusal and got the shape one.
    present = [f for f in FORBIDDEN_PLAN_FIELDS if f in plan]
    if present:
        refuse(f"category=executable_plan_carries_a_capability fields={present} "
               "— D2 authenticates the operator's envelopes and obtains its own "
               "credential; a plan carrying either would let D1's run decide "
               "what D2 treats as authorized")
    unknown = sorted(set(plan) - set(PLAN_FIELDS) - {"plan_sha256"})
    if unknown:
        refuse(f"category=executable_plan_unknown_fields fields={unknown} — a "
               "field the digest covers and no consumer reads is where a "
               "difference hides")
    if plan_digest(plan) != plan.get("plan_sha256"):
        refuse("category=executable_plan_digest_mismatch — the plan was edited "
               "after it was signed")
    if not plan["batches"] or not plan["final_units"]:
        refuse("category=executable_plan_has_nothing_to_execute")
    return plan


def assert_plan_matches_evidence(plan: dict, *, evidence_payload: dict) -> dict:
    """The signed evidence and the signed plan must describe one run.

    They are two documents with two signatures, and D2 verifies both. That is
    not redundant: the evidence is what a reader sees a digest of, the plan is
    what D2 executes, and a run where those disagree is a run whose public
    record describes something other than what happened."""
    for plan_field, evidence_field in (
            ("plan_sha256", "trusted_plan_sha256"),
            ("review_skeleton_sha256", "review_skeleton_sha256"),
            ("candidate_head_sha", "candidate_head_sha"),
            ("execution_challenge_sha256", "execution_challenge_sha256"),
            ("total_input_tokens", "total_input_tokens"),
            ("requested_model_ids", "requested_model_ids"),
            ("required_approver", "required_approver")):
        if plan[plan_field] != evidence_payload.get(evidence_field):
            refuse(f"category=executable_plan_disagrees_with_its_evidence "
                   f"field={plan_field} — the signed plan and the signed count "
                   "evidence describe different runs, and the public record is "
                   "the one that would be believed")
    return {"plan_sha256": plan["plan_sha256"], "matches_evidence": True}


def assert_challenge_is_the_counted_one(plan: dict) -> str:
    """D2 sends the challenge D1 counted. It does not mint a new one.

    A fresh challenge changes the request bytes, and the request bytes are what
    was counted — so a D2 that minted its own would execute requests whose cost
    nobody approved, while every digest that was supposed to detect that
    compared documents rather than bytes."""
    challenge = plan["execution_challenge"]
    if not isinstance(challenge, str) or len(challenge) < 16:
        refuse("category=executable_plan_challenge_missing")
    if hashlib.sha256(challenge.encode()).hexdigest() != plan[
            "execution_challenge_sha256"]:
        refuse("category=executable_plan_challenge_digest_mismatch")
    return challenge


def public_reference(plan: dict) -> dict:
    """What may leave the private artifact.

    The digest and the counts, never the units, the batches or the challenge.
    A reader needs to know which plan ran and what it cost; the prompt bytes
    are the candidate's code and the reviewer's questions."""
    return {
        "plan_class": PLAN_CLASS,
        "plan_sha256": plan["plan_sha256"],
        "review_skeleton_sha256": plan["review_skeleton_sha256"],
        "candidate_head_sha": plan["candidate_head_sha"],
        "requested_model_ids": list(plan["requested_model_ids"]),
        "required_approver": plan["required_approver"],
        "unit_count": len(plan["final_units"]),
        "batch_count": len(plan["batches"]),
        "total_input_tokens": plan["total_input_tokens"],
        "execution_challenge_sha256": plan["execution_challenge_sha256"],
        "honest_scope": ("the digest identifies the plan; the plan itself is "
                         "private because it carries the exact prompt bytes — "
                         "the candidate's code and the reviewer's questions"),
    }
