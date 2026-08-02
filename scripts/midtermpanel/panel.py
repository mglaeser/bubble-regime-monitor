"""Execute the counted plan, aggregate the panel, and refuse to soften it.

## The handoff is the security property

The panel must execute the plan the count job produced — not a plan it rebuilt
that looks similar. The plan carries the provider's COUNTS, and a rebuild without
calling the provider has nothing to compare them against, so "rebuild and compare
digests" is unachievable by construction. The artifact crosses from count to
panel within the same workflow run, and `verify_handoff` refuses it unless every
binding matches.

## Aggregation is not re-implemented

`scripts/independent_verify.py` already encodes this exact policy, with the edges
worked out: `model_matches` accepts a dated snapshot and refuses
`gpt-5.6-solaris`; `require_approvals` demands the required approver be RESOLVED
rather than merely listed, and counts DISTINCT non-approver models so repeat
votes from one model are not corroboration; `decide` fails closed on a reply with
no boolean `refuted`.

Because the privileged workflow checks out the default branch, that file is
trusted code in this architecture. Re-deriving its rules here would create a
second implementation of the same policy, and the first time they disagreed the
evidence would still look identical.

One policy difference, applied explicitly: `strict_any_refutation` is OPT-IN and
OFF by default upstream, because the reference mechanism greens on "approver plus
one corroborator" even when a third voice refutes. Mid-term policy requires ANY
valid refutation to block, so this module turns it on and says so.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import (
    MIN_DISTINCT_OTHER_APPROVALS,
    PANEL_MODELS,
    REQUIRED_APPROVER,
    STRICT_ANY_REFUTATION,
)
from .count import assert_plan_is_executable
from .errors import refuse
from .evidence import digest_of

#: Attempts per model/batch pair, including the first.
MAX_ATTEMPTS_PER_PAIR = 3


def _upstream():
    """Import the reviewed panel-decision functions from the default branch.

    `scripts/` is placed on the path by the workflow (`PYTHONPATH`) and by the
    test suite; this adds it defensively so a direct `python -m` invocation from
    the repository root behaves the same way."""
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import independent_verify
    return independent_verify


def verify_handoff(*, count_record: dict, plan: dict, expected_head: str,
                   expected_base: str, expected_engine_digest: str,
                   expected_policy_digest: str) -> dict:
    """Refuse the plan unless it is the one this run counted.

    Every binding is compared explicitly rather than trusting that the artifact
    came from the same run. A same-run download is a strong claim about
    PROVENANCE; it is not a claim about CONTENT, and the two are only the same
    while nothing has gone wrong."""
    assert_plan_is_executable(plan)
    mismatches = []
    for field, expected in (
            ("candidate_head_sha", expected_head),
            ("candidate_base_sha", expected_base),
            ("engine_digest", expected_engine_digest),
            ("policy_digest", expected_policy_digest)):
        if plan.get(field) != expected:
            mismatches.append(
                f"{field}: plan={str(plan.get(field))[:16]} "
                f"expected={str(expected)[:16]}")
    if count_record.get("candidate_head_sha") != expected_head:
        mismatches.append("count_evidence.candidate_head_sha")
    counted_semantics = (count_record.get("body") or {}).get(
        "request_semantics_digest")
    if counted_semantics != plan.get("request_semantics_digest"):
        mismatches.append(
            "request_semantics_digest: the plan describes different requests "
            "than the count evidence recorded")
    if mismatches:
        refuse(f"category=count_to_panel_handoff_mismatch found={mismatches} — "
               "the panel must execute the plan this run counted; anything else "
               "spends money on requests nobody counted and reports the result "
               "as if they were the same")
    return {"handoff": "verified",
            "plan_sha256": plan["plan_sha256"],
            "execution_requests": len(plan["execution_requests"])}


def execute(*, engine: dict, plan: dict, transport, **engine_kwargs) -> dict:
    """Execute through the ENGINE's executor. This module runs no request loop.

    `verifier.executor.execute_batch` already implements retries, attempt
    ledgering, usage-token drift, response-envelope validation, the anti-copy
    tripwire, output secret scanning and per-model verdict evidence. Every one
    of those is a rule; re-running them from here would be a second set of the
    same rules that could disagree.

    What this function contributes is the binding: it refuses to execute
    anything whose request hash is not in the counted plan. The engine decides
    HOW a request is executed; the plan decides WHICH requests exist."""
    from trustedlane import enginebridge

    permitted = set(plan["execution_request_hashes"])
    if not permitted:
        refuse("category=plan_has_no_execution_requests")

    with enginebridge.engine_refusals(engine, where="execute_batch"):
        result = engine["modules"]["verifier.executor"].execute_batch(
            transport=transport, **engine_kwargs)

    executed = {str(h) for h in (result.get("request_hashes") or [])}
    unexpected = sorted(executed - permitted)
    if unexpected:
        refuse(f"category=executed_a_request_that_was_never_counted "
               f"hashes={[h[:16] for h in unexpected]} — the panel executed a "
               "request the count job did not produce; its tokens were never "
               "counted and its content was never in the plan")
    missing = sorted(permitted - executed)
    if missing:
        refuse(f"category=counted_request_was_not_executed "
               f"hashes={[h[:16] for h in missing]} — a plan partially executed "
               "and reported whole is a review with a hole in it")

    evidence = result.get(enginebridge.EVIDENCE_RECORDS_KEY) or []
    votes = [{"model": record.get("model"), "v": record}
             for record in evidence]
    for vote in votes:
        if vote["model"] not in PANEL_MODELS:
            refuse(f"category=engine_returned_an_ungoverned_model "
                   f"model={vote['model']!r}")
    return {"votes": votes,
            "generation_calls": int(getattr(transport, "call_count", 0) or 0),
            "engine_result_keys": sorted(result)}


def aggregate(*, votes: list, challenge: str = None) -> dict:
    """Apply the panel rules. Refutations are never softened.

    Delegates to `independent_verify`, then applies the mid-term strict rule on
    top. The order matters: role gate first (was the required approver resolved
    and did it approve, with a distinct corroborator), then strict refutation
    (did ANY valid voice refute). A green from the first is not a green overall."""
    upstream = _upstream()
    models = [v["model"] for v in votes]
    role = upstream.require_approvals(
        votes, models, REQUIRED_APPROVER,
        min_others=MIN_DISTINCT_OTHER_APPROVALS, challenge=challenge)

    strict = {"block": False, "reason": "strict mode disabled"}
    if STRICT_ANY_REFUTATION:
        strict = upstream.strict_any_refutation(votes, models)

    blocked = bool(role.get("block")) or bool(strict.get("block"))
    return {
        "decision": "blocked" if blocked else "approved",
        "role_gate": role,
        "strict_gate": strict,
        "models_voting": sorted({v["model"] for v in votes}),
        "required_approver": REQUIRED_APPROVER,
        "strict_any_refutation": STRICT_ANY_REFUTATION,
        "votes": len(votes),
    }


def assert_synthesis_cannot_clear_a_refutation(aggregate_record: dict,
                                               *, proposed_state: str) -> str:
    """A blocked aggregate may not be published as success.

    The gap this closes is small and entirely realistic: aggregation is correct,
    and then a later step maps its result onto a status and gets the mapping
    wrong — or someone adds a summarising pass that "resolves" a refutation. The
    check sits at the boundary where the decision becomes a published claim."""
    if aggregate_record.get("decision") == "blocked" and proposed_state == "success":
        refuse("category=synthesis_would_clear_a_refutation — the panel "
               "blocked; publishing success would replace a refusal with a "
               "summary of it")
    return proposed_state


def anti_copy_tripwire(votes: list) -> dict:
    """Independent voices that produce identical reasons are not independent.

    Not proof of anything on its own — three models can agree, and short
    agreements collide honestly, which is why this reports rather than refuses
    and why very short reasons are excluded. It is a signal for the human who
    reads the summary, recorded in the evidence so it cannot be seen only once."""
    upstream = _upstream()
    normalised = {}
    for vote in votes:
        reason = upstream.norm_reason((vote.get("v") or {}).get("reason"))
        if len(reason) < 24:
            continue
        normalised.setdefault(reason, set()).add(vote["model"])
    collisions = sorted(
        {"reason_sha256": digest_of({"r": reason})[:16],
         "models": sorted(models)}.__repr__()
        for reason, models in normalised.items() if len(models) > 1)
    return {"identical_reason_groups": len(collisions),
            "collisions": collisions,
            "honest_scope": ("agreement is not collusion; this is a signal for "
                             "a human, not a verdict")}
