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
                   panel_identity: dict | None = None,
                   require_identity: bool = False) -> dict:
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
    if require_identity:
        # A comparison of two absences passes and proves nothing. In provider
        # mode both sides MUST be fully populated, or the equality below is
        # `None == None` nine times and the panel has demonstrated only that
        # neither job knew which engine it used.
        blank = sorted(f for f in IDENTITY_HANDOFF_FIELDS
                       if (panel_identity or {}).get(f) is None
                       or body.get(f) is None)
        if blank:
            refuse(f"category=engine_identity_binding_incomplete "
                   f"fields={blank} — a provider-backed panel must compare a "
                   "populated builder identity against a populated one; "
                   "comparing absences is not a check")
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
    # Honest about what was actually compared. `panel_identity is not None`
    # was true for a dry run's all-`None` binding, so the record claimed an
    # identity comparison had happened when nine absences had been compared
    # to nine absences.
    #
    # ALL nine, not any. `engine_artifact_sha256` is populated even in a dry
    # run — the artifact is rebuilt either way — so `any()` reported a
    # comparison as having happened when one field of nine was present and
    # eight were `None` on both sides. A partial comparison is not a
    # comparison, and this field is read as though it were one.
    compared = bool(panel_identity) and all(
        panel_identity.get(f) is not None and body.get(f) is not None
        for f in IDENTITY_HANDOFF_FIELDS)
    return {"handoff": "verified",
            "plan_sha256": plan["plan_sha256"],
            "identity_compared": compared,
            "identity_required": require_identity,
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
            # WHICH unit, WHICH model, WHICH gate, and by how much. The engine
            # computes all of it per unit and the lane used to drop it on the
            # floor: a blocked run published `decision: blocked` and a stderr
            # line naming the GATE, and nothing that said which of 99 units
            # failed or which model objected.
            #
            # The write-separated D2 lane already carries the same two
            # structures (`d2runtime.py`), so this is parity with a lane that
            # decided the question, not a new disclosure.
            **per_unit_evidence(result["batch_results"]),
            "synthesis": result["synthesis"],
            "generation_ledger": result["generation_ledger"],
            "execution_preflight": result["execution_preflight"],
            "output_privacy": result["output_privacy"],
            # Named for what they are. Every call this transport can make goes
            # to the generation endpoint — the path allowlist is what makes
            # that true rather than a convention — so "provider calls" and
            # "generation calls" are the same number, and the evidence uses the
            # narrower name because it says more.
            "generation_calls": int(getattr(transport, "call_count", 0) or 0),
            # The structural binding from each raw provider reply to the
            # envelope the engine was actually given. Absent for a dry run,
            # which normalizes nothing because nothing provider-shaped arrives.
            "normalization": normalization_evidence(transport)}


#: Fields of a per-unit decision the lane retains. An ALLOWLIST, not a copy:
#: `unit_decisions` is engine-authored today and every value in it is
#: structural — a hash, a bool, a confidence enum, governed model ids,
#: integers, and `distinct_reasoning`, which is itself only
#: `{unit_sha256, gate_semantics, similarity_threshold_bp,
#: distinct_reasoning_models, distinct_reasoning_count}`. Verified field by
#: field before this was written. Naming them means a future engine that adds
#: a prose field does not silently start publishing it.
DECISION_FIELDS = ("unit_sha256", "approved", "approver_confidence",
                   "refuted_by", "distinct_other_approvers",
                   "distinct_reasoning", "block_code")

#: `distinct_reasoning` is a NESTED object, and a top-level allowlist that
#: copies one wholesale is not an allowlist. Its own test caught that: prose
#: planted one level down arrived in the record untouched.
#:
#: These are `verdicts.assert_distinct_reasoning`'s actual return fields,
#: read from the pinned engine. An engine that adds a prose field to it does
#: not start publishing it by existing.
DISTINCT_REASONING_FIELDS = ("unit_sha256", "gate_semantics",
                             "similarity_threshold_bp",
                             "distinct_reasoning_models",
                             "distinct_reasoning_count")

#: The most a single block reason may occupy. The reasons are engine-authored
#: and structural, but a bound costs nothing and an unbounded string in an
#: artifact is how a payload fragment would arrive if one ever could.
MAX_BLOCK_REASON_CHARS = 400

#: What replaces a reason that fails the checks below. The block still lands,
#: with its unit, its code and its category — a reason that cannot be shown is
#: not a reason to hide the block.
BLOCK_REASON_WITHHELD = "withheld: reason failed the retention checks"


def _retained_block_reason(reason, *, scan=None) -> str:
    """An engine block reason, or a stand-in — never a silent omission.

    ## Why this is not a straight copy

    `d2runtime` deliberately does NOT carry this field, and says why: "it is
    the one field that can quote the payload — a unit id prefix here, but the
    same field elsewhere carries a path or an atom fragment".

    That judgement is right about the field TYPE and over-broad for the four
    messages actually reachable here, all of which were read before this was
    written: `required_approver_absent`, `required_approver_veto`,
    `insufficient_corroboration` and the anti-canned pair, carrying governed
    model ids, a 16-character unit-hash prefix, a confidence enum and
    integers. `similarity_bp=9100 threshold_bp=8500` is exactly the number an
    operator acts on, and dropping it to honour a caution about a different
    message would be cargo-culting the caution instead of the reasoning.

    So the reason is carried, and defended rather than trusted: bounded,
    control characters refused, and scanned by the engine's own scanner when
    one is supplied. If a future engine ever does put a path in here, this
    degrades to the code and the category instead of publishing it."""
    if not isinstance(reason, str) or not reason:
        return BLOCK_REASON_WITHHELD
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in reason):
        return BLOCK_REASON_WITHHELD
    if len(reason) > MAX_BLOCK_REASON_CHARS:
        return BLOCK_REASON_WITHHELD
    if scan is not None and scan(reason):
        return BLOCK_REASON_WITHHELD
    return reason


def per_unit_evidence(batch_results, *, scan=None) -> dict:
    """Per-unit decisions and blocks, flattened across batches.

    Keyed by unit so a reader looks up the unit the merge gate named, rather
    than searching eight batch objects for it. The batch id is carried on each
    entry so the reverse lookup still works."""
    decisions: dict = {}
    blocks: list = []
    for result in batch_results or []:
        batch_id = result.get("batch_id")
        for unit_hash, decision in (result.get("unit_decisions") or {}).items():
            retained = {field: decision.get(field)
                        for field in DECISION_FIELDS}
            nested = retained.get("distinct_reasoning")
            if isinstance(nested, dict):
                retained["distinct_reasoning"] = {
                    field: nested.get(field)
                    for field in DISTINCT_REASONING_FIELDS if field in nested}
            elif nested is not None:
                # Not the shape this lane verified. Report the type rather
                # than the value: a shape nobody checked is exactly what must
                # not be copied through.
                retained["distinct_reasoning"] = {
                    "unexpected_shape": type(nested).__name__}
            decisions[unit_hash] = {**retained, "batch_id": batch_id}
        for block in result.get("unit_blocks") or []:
            blocks.append({
                "unit_sha256": block.get("unit_sha256"),
                "code": block.get("code"),
                # The engine's own `category=` token, extracted by the same
                # anchored, identifier-only rule the lane already applies to
                # engine refusals. Finer than `code`, and provably not a path.
                "category": engine_category_of(block.get("reason")),
                "reason": _retained_block_reason(block.get("reason"),
                                                 scan=scan),
                "batch_id": batch_id,
            })
    return {
        "unit_decisions": dict(sorted(decisions.items())),
        "unit_blocks": sorted(
            blocks, key=lambda b: (str(b["unit_sha256"]), str(b["code"]))),
    }


def engine_category_of(reason) -> str | None:
    """The `category=` identifier from an engine message, or None.

    One rule, one implementation: delegates to
    `trustedlane.enginebridge.engine_category`, which is anchored, admits
    `[a-z_][a-z0-9_]*` only, bounds the length and requires termination. A
    second copy of that regex here is how the two would come to disagree."""
    from trustedlane.enginebridge import engine_category

    class _Carrier:
        message = reason

    return engine_category(_Carrier())


def normalization_evidence(transport) -> dict:
    """What the transport normalized, as digests and counts only.

    Reads the transport's own record rather than reaching into its attributes,
    so a transport that reports nothing produces an explicit "none" instead of
    a silent empty dict that reads like "nothing needed normalizing"."""
    record = None
    inner = getattr(transport, "inner", None) or transport
    reporter = getattr(inner, "record", None)
    if callable(reporter):
        record = reporter()
    if not isinstance(record, dict) or "normalization_records" not in record:
        return {"normalized": False,
                "honest_scope": "this transport performs no normalization; a "
                                "dry run's stand-in returns the engine's own "
                                "envelope and never a provider document"}
    records = record.get("normalization_records") or []
    return {
        "normalized": True,
        "normalization_version": record.get("normalization_version"),
        "normalization_records_sha256": record.get(
            "normalization_records_sha256"),
        "normalizations": len(records),
        "per_model": sorted(
            ({"model": r.get("requested_model"),
              "attempt": r.get("attempt"),
              "raw_response_sha256": r.get("raw_response_sha256"),
              "raw_response_bytes": r.get("raw_response_bytes"),
              "payload_sha256": r.get("payload_sha256"),
              "normalized_envelope_sha256": r.get(
                  "normalized_envelope_sha256"),
              "normalized_verdicts_sha256": r.get(
                  "normalized_verdicts_sha256")}
             for r in records),
            key=lambda r: (r["attempt"], str(r["model"]))),
        "honest_scope": "digests and counts. No raw body, no output text, no "
                        "provider identifiers and no headers are carried here "
                        "or persisted anywhere",
    }


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
            # THREE branches, not two. The engine blocks a unit for the role and
            # corroboration gates as well as for a refutation — the required
            # approver has no valid vote, too few distinct models corroborate,
            # two approvals are near-identical — and in every one of those
            # `refuted_unit_count` is zero. The two-branch version emitted "the
            # engine's synthesis refuted 0 unit(s)", which the readable review
            # now publishes verbatim under "Why this blocked": a sentence that
            # contradicts itself, beside a decision the reader is asked to
            # trust.
            "reason": _engine_gate_reason(engine_blocked, synthesis),
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


def _engine_gate_reason(blocked: bool, synthesis: dict) -> str:
    """What the engine's gate actually decided, in words that match the counts."""
    if not blocked:
        return "every unit approved under the governed role gate"
    refuted = int(synthesis.get("refuted_unit_count") or 0)
    if refuted:
        return f"the engine's synthesis refuted {refuted} unit(s)"
    approved = int(synthesis.get("approved_unit_count") or 0)
    return ("no unit was refuted; the engine's role and corroboration gates "
            f"cleared {approved} unit(s) and blocked the rest — the required "
            "approver had no valid vote, too few distinct models corroborated, "
            "or two approvals were near-identical")


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
