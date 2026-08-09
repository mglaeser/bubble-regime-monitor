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


#: Every builder-identity field the panel must prove it shares with the count
#: job. The source-role digest is NOT enough: two releases built from the same
#: two commits can carry different artifacts, identity documents, build runs
#: and control classes, so a panel comparing only `engine_digest` can execute
#: under a builder the count job never saw.
IDENTITY_HANDOFF_FIELDS = (
    "engine_identity_sha256",
    "engine_identity_state",
    "engine_native_branch_protection",
    "engine_control_class",
    "engine_build_run_id",
    "engine_build_run_attempt",
    "engine_provenance_sha256",
    "engine_artifact_sha256",
    "engine_release_binding_sha256",
)


def verify_handoff(*, count_record: dict, plan: dict, expected_head: str,
                   expected_base: str, expected_engine_digest: str,
                   expected_policy_digest: str,
                   panel_identity: dict | None = None) -> dict:
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
    body = count_record.get("body") or {}
    if body.get("request_semantics_digest") != plan.get(
            "request_semantics_digest"):
        mismatches.append(
            "request_semantics_digest: the plan describes different requests "
            "than the count evidence recorded")
    # THE plan, not a plan. The count evidence is published (its digest goes
    # into the status); the plan is private. Comparing only the semantics
    # digest would accept any plan asking the same questions under a different
    # challenge or a different pin record — and the challenge is what proves a
    # verdict was written for this run.
    if body.get("plan_sha256") != plan.get("plan_sha256"):
        mismatches.append(
            "plan_sha256: the count evidence identifies a different plan than "
            "the one handed to the panel")
    # The BUILDER's identity, compared before a single generation call. The
    # rebuilt-skeleton check catches a candidate that moved between the jobs;
    # nothing caught an ENGINE that moved. If the release asset were replaced
    # between count and panel while the two source commits stayed put, every
    # existing comparison still passed and the panel executed under a builder
    # the count evidence does not describe.
    if panel_identity is not None:
        for field in IDENTITY_HANDOFF_FIELDS:
            counted, observed = body.get(field), panel_identity.get(field)
            if counted != observed:
                mismatches.append(
                    f"{field}: count={str(counted)[:16]} "
                    f"panel={str(observed)[:16]}")
    if mismatches:
        refuse(f"category=count_to_panel_handoff_mismatch found={mismatches} — "
               "the panel must execute the plan this run counted, using the "
               "engine this run counted with; anything else spends money on "
               "requests nobody counted and reports the result as if they "
               "were the same")
    return {"handoff": "verified",
            "plan_sha256": plan["plan_sha256"],
            "identity_compared": panel_identity is not None,
            "execution_requests": len(plan["execution_request_hashes"])}


def execute(*, engine: dict, plan: dict, skeleton: dict, transport,
            repository_path: str, authorizations=None) -> dict:
    """Execute through the ENGINE's Stage 3. One operation, not a loop.

    `enginebridge.execute_review_plan` is the whole of D2 and it is already
    correct: it rebuilds every payload from the commits, hash-checks each
    assembled request against the plan, scans EVERY execution payload before
    any is sent, runs the panel through `execute_batch` with the operator's
    retry policy, synthesises additively so a refutation survives, and scans
    the provider's own returned text before it is persisted.

    The previous version called `executor.execute_batch(transport=transport,
    **engine_kwargs)` with no batch, no policy, no ledger and no assemblies —
    a signature that has never existed — and then compared a `request_hashes`
    key the engine has never emitted. Both would have surfaced in the job
    holding the credential.

    What this function contributes is the binding either side of that call: the
    plan decides WHICH requests exist, and the engine decides HOW each is
    executed."""
    from trustedlane import enginebridge

    permitted = set(plan["execution_request_hashes"])
    if not permitted:
        refuse("category=plan_has_no_execution_requests")

    result = enginebridge.execute_review_plan(
        engine, skeleton=skeleton, plan=plan,
        repository_path=repository_path, transport=transport,
        authorizations=authorizations, challenge=plan["execution_challenge"])

    coverage = assert_every_batch_and_model_was_executed(result, plan=plan)

    votes = []
    for batch in result["batch_results"]:
        for record in batch[enginebridge.EVIDENCE_RECORDS_KEY]:
            model = record.get("model") or record.get("model_id")
            if model not in PANEL_MODELS:
                refuse(f"category=engine_returned_an_ungoverned_model "
                       f"model={model!r}")
            votes.append({"model": model, "v": record})
    if not votes:
        refuse("category=panel_produced_no_verdicts — an empty vote set "
               "aggregates to green, which is the one thing a review that "
               "reached nobody must not do")
    return {"votes": votes,
            "coverage": coverage,
            "synthesis": result["synthesis"],
            "generation_ledger": result["generation_ledger"],
            "execution_preflight": result["execution_preflight"],
            "output_privacy": result["output_privacy"],
            # Named for what they are. Every call this transport can make goes
            # to the generation endpoint — the path allowlist is what makes
            # that true rather than a convention — so "provider calls" and
            # "generation calls" are the same number, and the evidence uses the
            # narrower name because it says more.
            "generation_calls": int(getattr(transport, "call_count", 0) or 0)}


def assert_every_batch_and_model_was_executed(result: dict, *,
                                              plan: dict) -> dict:
    """Every planned batch produced a result, and every model voted in each.

    Deliberately NOT a re-check that each executed request's hash is in the
    plan: `executor.assert_request_matches_plan` already requires hash equality
    for every assembled request before any is sent, and the engine refuses on
    mismatch. A second copy here would be a second opinion about a rule the
    engine owns — and the first version of this function was worse than that,
    because it read a `request_hashes` key the engine has never emitted, so it
    compared an empty set and reported that everything matched.

    What the engine does NOT assert is the shape this lane depends on: that the
    loop ran to the end. A run that executed two of three batches and returned
    would satisfy every per-request check and aggregate over a review with a
    hole in it."""
    results = result["batch_results"]
    planned = [batch["batch_id"] for batch in plan["batches"]]
    observed = [batch["batch_id"] for batch in results]
    if sorted(observed) != sorted(planned):
        refuse(f"category=planned_batches_were_not_all_executed "
               f"planned={len(planned)} executed={len(observed)} "
               f"missing={sorted(set(planned) - set(observed))} — a plan "
               "partially executed and reported whole is a review with a hole "
               "in it")
    expected_models = sorted(plan["review_request_policy"]["model_ids"])
    for batch in results:
        voted = sorted({record.get("model") or record.get("model_id")
                        for record in batch["per_model_verdict_evidence"]})
        if voted != expected_models:
            refuse(f"category=batch_missing_a_models_verdict "
                   f"batch={batch['batch_id']!r} voted={voted} "
                   f"expected={expected_models} — a missing voice is a voice "
                   "that cannot refute, and the aggregate would green on the "
                   "remaining ones")
    return {"batches_planned": len(planned), "batches_executed": len(observed),
            "models_per_batch": expected_models}


def aggregate(*, votes: list, synthesis: dict, challenge: str = None) -> dict:
    """The engine's decision, plus the one rule this lane adds.

    ## Where the role gate lives

    Not here. `executor.decide_unit_or_block` already applies it, per unit,
    from `review_request_policy`: the required approver must have a VALID vote
    that explicitly approves, a refutation of any confidence is a veto, and at
    least `minimum_other_approvers` DISTINCT other models must approve. Then
    `synthesize` intersects across batches so a unit any panelist refuted stays
    refuted. Every one of those is the same rule `independent_verify` states.

    The first version of this function ran `require_approvals` over the
    engine's evidence anyway. That was a second implementation of a rule the
    engine owns, and it did not merely duplicate — it disagreed. The engine
    emits ONE `verdict_evidence` record per model per batch, carrying
    `verdicts_by_unit`; `require_approvals` expects one flat vote per model
    with a boolean `refuted` and an `ok` flag. So `_is_valid` was false for
    every record, and the gate blocked EVERY review with "required approver
    without a valid vote (error/unparsable) -> fail-closed" — a fail-closed
    default doing exactly what it should, over a shape mismatch, reporting a
    refusal the models never made.

    ## What this lane genuinely adds

    `strict_any_refutation`. It is OPT-IN and OFF by default upstream, because
    the reference mechanism greens on "approver plus one corroborator" even
    when a third voice refutes. Mid-term policy requires ANY valid refutation
    to block, so this reads the engine's own per-model records and blocks on a
    refuted unit from any governed voice — including one the engine's per-unit
    decision resolved in favour of approval.
    """
    del challenge  # the engine bound it into every request and checked it back
    engine_blocked = not synthesis["overall_approved"]

    refuting = sorted({vote["model"] for vote in votes
                       if (vote["v"] or {}).get("refuted_count", 0)})
    strict_blocked = bool(STRICT_ANY_REFUTATION and refuting)

    blocked = engine_blocked or strict_blocked
    return {
        "decision": "blocked" if blocked else "approved",
        "engine_gate": {
            "block": engine_blocked,
            "reason": ("the engine's synthesis refuted "
                       f"{synthesis['refuted_unit_count']} unit(s)"
                       if engine_blocked else
                       "every unit approved under the governed role gate"),
            "refuted_unit_count": synthesis["refuted_unit_count"],
            "approved_unit_count": synthesis["approved_unit_count"],
            "synthesis_sha256": synthesis["synthesis_sha256"],
        },
        "strict_gate": {
            "block": strict_blocked,
            "reason": (f"strict mode: refuted by {refuting}" if strict_blocked
                       else "strict mode: no model refuted any unit"),
            "refuting_models": refuting,
        },
        "models_voting": sorted({v["model"] for v in votes}),
        "required_approver": REQUIRED_APPROVER,
        "minimum_other_approvers": MIN_DISTINCT_OTHER_APPROVALS,
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
        # The engine's record is per model per BATCH and carries every unit's
        # verdict under `verdicts_by_unit`. Reading `vote["v"]["reason"]` found
        # nothing there and normalised `None` to the empty string for every
        # voice, so the tripwire reported zero collisions over zero reasons and
        # every test that asserted "no collusion detected" passed.
        for unit_hash, verdict in sorted(
                ((vote.get("v") or {}).get("verdicts_by_unit") or {}).items()):
            reason = upstream.norm_reason(verdict.get("reason"))
            if len(reason) < 24:
                continue
            normalised.setdefault((unit_hash, reason), set()).add(vote["model"])
    collisions = sorted(
        {"unit_sha256": unit_hash[:16],
         "reason_sha256": digest_of({"r": reason})[:16],
         "models": sorted(models)}.__repr__()
        for (unit_hash, reason), models in normalised.items()
        if len(models) > 1)
    return {"identical_reason_groups": len(collisions),
            "reasons_examined": len(normalised),
            "collisions": collisions,
            "honest_scope": ("agreement is not collusion; this is a signal for "
                             "a human, not a verdict")}
