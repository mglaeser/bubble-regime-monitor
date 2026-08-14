"""D2 — the trusted generation lane.

EX3-R01. Same shape as D1 and deliberately not the same code path, because the
two phases differ in exactly the ways that matter and a shared "run the lane"
function with a `generate=True` flag would put those differences inside a
branch nobody reads.

What D2 has that D1 does not:

* **A separate operator approval.** Prerequisite 16, its own record, its own
  environment. An approval to count has never been an approval to generate,
  and reusing D1's environment would make it one by accident.
* **A plan it did not choose.** D2 executes the plan D1 SIGNED — the document
  itself, not a digest of one. It does not rebuild the plan; rebuilding here
  would let D2 review something whose cost was never counted or approved.
* **Verdicts to check.** Every response is validated, decided and synthesized
  by `verifier.executor` — the same code the candidate engine uses — through
  `enginebridge.execute_review_plan`.

What both have: protected state first, engine by digest, records before the
credential, pending before any provider work, and a terminal status whatever
happens.

**This module no longer executes anything itself.** It used to reconstruct
requests, send them, normalize replies and decide the panel — a second, weaker
implementation of Stage 3. What remains here is the four things the engine
cannot do for itself: authenticate the operator's records, bind them to this
runtime, verify D1's two signatures, and hand the engine a real transport. The
deleted machinery is listed below with what each piece got wrong, because the
list is the argument for not writing it again.

The generation send path is NOT `transport.count_once` with a different URL.
`transport.exchange` refuses any endpoint but the count endpoint, and that
refusal is one of the things keeping D1 honest, so it stays exactly as it is.
`generationtransport.TrustedGenerationTransport` goes through
`transport.exchange_generation`, a separate function that refuses the COUNT
endpoint symmetrically. Both share only the private credential-joining step,
which asserts nothing about the URL and is unreachable except through one of
the two.

A flag — `exchange(request, allow_generation=True)` — would have been shorter
and is the thing to avoid: it puts the difference between "counts" and
"generates" into a parameter, where getting it wrong looks like configuration
rather than like generating from the count lane.
"""

from __future__ import annotations

from . import (
    adapter,
    artifactload,
    enginebridge,
    enginepolicy,
    evidencewire,
    executableplan,
    generationtransport,
    protectedstate,
    runtimebinding,
    signing,
    statuspublish,
    trustedverifier,
)
from .errors import LaneRefusal, refuse

PRODUCER = "trusted-lane-d2-generation"
TRUSTED_CONTEXT = "trusted-cross-vendor-review"
EVIDENCE_CLASS = "TRUSTED_EXECUTION_EVIDENCE"
#: What D1's count evidence is CLASSED as. Spelled out here rather than
#: imported from `d1runtime`, because D2 must be able to state what it requires
#: without importing the module that produces it — the two run in separate
#: environments and only one of them ever holds a generation approval.
D1_EVIDENCE_CLASS = "TRUSTED_COUNT_EVIDENCE"
#: Operator prerequisite 16, by its canonical key in `prerequisites.py` rather
#: than a shorter name invented here. A key that does not match the registry is
#: a record nobody can write, and the refusal for "no such approval" would look
#: identical to the refusal for "approval not granted".
GENERATION_PREREQUISITE = "approve_generation_separately"

# --------------------------------------------------------------------------
# WHAT USED TO BE HERE, AND WHY IT IS NOT
#
# `build_generation_request`, `assert_request_is_generation`, `plan_digest`,
# `assert_plan_is_the_counted_one`, `_assert_verdicts_are_for_this_unit`,
# `_assert_panel_decided`, `_assert_distinct_reasoning` and `_finalize`: a
# lane-local request builder, endpoint gate, plan digest, panel loop and
# finalizer. All eight were a second, weaker implementation of
# `verifier.executor`, and the mandate is explicit that it must not remain the
# final authority path.
#
# They were not merely redundant, they were WEAKER in ways that mattered:
#
# * `plan_digest` hashed the lane's own idea of a unit — id, instructions,
#   input text. The engine hashes the request SEMANTICS, and
#   `assert_request_matches_plan` requires hash equality against the counted
#   request, so a plan that satisfied the lane digest could still be a request
#   nobody priced;
# * `_assert_verdicts_are_for_this_unit` compared one response against one
#   unit, because the lane sent one request per unit. The engine batches, so
#   the same question is `validate_verdicts` over the whole batch's unit
#   hashes — a stronger check the lane version could not express;
# * `_assert_panel_decided` re-derived approve/reject/abstain from decisions
#   the engine had already made, and `_finalize` reported its own aggregate
#   next to the engine's additive-only synthesis. Two authorities for one
#   decision is how the two copies end up disagreeing.
#
# Deleting them rather than leaving them unused is the point. Dead code in a
# trust boundary reads like a live gate to the next reader, and the next reader
# is the one deciding whether the boundary holds. `test_d2_has_no_lane_local_
# execution_machinery` fails if any of them comes back.
# --------------------------------------------------------------------------


def _publish(publisher, request: dict, sink: list) -> dict:
    if not callable(publisher):
        refuse("category=status_publisher_not_callable")
    publisher(request)
    sink.append(request)
    return request


def _load_signed_plan(signed_plan, *, signing_key, count_evidence,
                      candidate: dict, trusted_run: dict) -> tuple:
    """R04/R13. Verify D1's two signatures, then read the plan.

    Order matters and is not cosmetic. The signature comes first, so nothing
    below is reading attacker-chosen structure; the plan's own digest comes
    next, so an edit inside a validly signed envelope is caught; then the two
    documents are compared, because a run whose public evidence and private
    plan disagree has a public record describing something other than what
    happened.

    D2 used to be handed a plan and a digest and check they matched. That is
    not a binding: the digest identifies a plan, and whoever hands D2 the plan
    chooses which plan gets identified."""
    from .signing import verify_envelope

    # Each D1 document is an ENVELOPE plus the payload it was digested over.
    # The envelope carries `payload_sha256`, not the payload — so verifying the
    # signature proves the envelope is D1's, and `assert_payload_matches` proves
    # the bytes in hand are the bytes it was built over. Either alone is a
    # well-formed statement about a document nobody compared it to.
    documents = {}
    for label, pair, expected_class in (
            ("count_evidence", count_evidence, D1_EVIDENCE_CLASS),
            ("executable_plan", signed_plan, executableplan.PLAN_CLASS)):
        if not isinstance(pair, dict) or "envelope" not in pair or (
                "payload" not in pair):
            refuse(f"category=d1_document_not_an_envelope_and_payload "
                   f"document={label}")
        envelope, payload = pair["envelope"], pair["payload"]
        evidencewire.validate_envelope(envelope)
        verify_envelope(envelope, key=signing_key)
        evidencewire.assert_payload_matches(envelope, payload)
        # The CLASS says which document this is. Without it the only thing
        # separating the plan from the count evidence is which argument slot it
        # arrived in, and the slot is chosen by whoever calls D2 — so a caller
        # could hand the same signed document twice and have both checks read
        # it as the other one.
        if envelope.get("evidence_class") != expected_class:
            refuse(f"category=d1_document_is_the_wrong_class document={label} "
                   f"class={envelope.get('evidence_class')!r} "
                   f"expected={expected_class}")
        if envelope.get("candidate_head_sha") != candidate[
                "candidate_head_sha"]:
            refuse(f"category=d1_document_is_for_a_different_head "
                   f"document={label} — evidence earned against another commit "
                   "is not evidence about this one, and a push is how the "
                   "reviewed thing changes")
        documents[label] = payload

    plan = executableplan.validate(documents["executable_plan"])
    executableplan.assert_plan_matches_evidence(
        plan, evidence_payload=documents["count_evidence"])
    if plan["trusted_run_id"] != documents["count_evidence"]["trusted_run_id"]:
        refuse("category=d1_documents_from_different_runs — the plan and the "
               "evidence were produced by two different D1 runs, and only one "
               "of them counted what is about to be sent")
    if plan["trusted_run_id"] == trusted_run["id"]:
        refuse("category=d1_and_d2_are_the_same_run — D2 executes a plan a "
               "SEPARATE run counted and a separate operator approval "
               "authorized; one run doing both is the separation collapsing")
    return plan, executableplan.assert_challenge_is_the_counted_one(plan)


def run(*, observations: dict, operator_claims, lane_verifier,
        engine: dict, engine_artifact: dict,
        engine_identity: dict, bootstrap: dict,
        candidate: dict, count_evidence: dict, signed_plan: dict, opener,
        fetch, credential: str, signing_key, publisher, trusted_run: dict,
        observed_now: str, produced_at: str,
        engine_source_sha256: str) -> dict:
    """Run the generation lane. Returns signed execution evidence, or refuses.

    `plan` and `trusted_plan_sha256` are GONE from this signature, and their
    absence is the fix. D2 took a plan and a digest and checked they matched —
    which is not a binding, because the digest identifies a plan and whoever
    hands D2 the plan chooses which plan gets identified. It now takes D1's two
    SIGNED documents and reads the plan out of one of them."""
    steps = []
    publications: list = []

    steps.append(("protected_state", protectedstate.assert_ready_for_credential(
        **observations)))
    steps.append(("engine_artifact", artifactload.inspect_archive(
        engine_artifact["path"],
        expected_sha256=engine_artifact["expected_sha256"])))
    steps.append(("engine_identity", enginebridge.assert_identity_is_this_engine(
        engine_identity, engine_artifact=engine_artifact)))
    steps.append(("engine_root",
                  artifactload.assert_engine_root_is_not_the_candidate(
                      engine_artifact["root"])))
    # The candidate package must be neither loaded nor reachable. Absolute
    # again (EX6-R02): the approved engine is loaded under its own namespace by
    # `enginebridge`, so a module named `verifier` in this process cannot be
    # the engine and there is nothing left to make an exception for.
    #
    # `loaded_modules` and `search_path` come off the artifact record so a
    # process where the candidate is present BY CONSTRUCTION — the candidate's
    # own test suite — can hand the runtime a modelled runner view. Both
    # default to the real process, which is what a real runner gets and what
    # `test_the_runtime_checks_the_real_process_by_default` proves.
    steps.append(("no_candidate_import", enginepolicy.assert_no_candidate_import(
        modules=engine_artifact.get("loaded_modules"),
        search_path=engine_artifact.get("search_path"))))

    # AUTHENTICATED, not parsed — see the note in d1runtime. D2 requires the
    # same fifteen records D1 does PLUS prerequisite 16, and phase="D2" is what
    # makes `authenticate_record_set` demand it.
    record_set = trustedverifier.authenticate_record_set(
        operator_claims, verifier=lane_verifier, phase="D2")
    steps.append(("operator_records", record_set.to_record()))

    # Prerequisite 16, its own record. An approval to count has never been an
    # approval to generate, so this is a DIFFERENT key from D1's.
    _assert_generation_approved(record_set, candidate=candidate)

    plan, counted_challenge = _load_signed_plan(
        signed_plan, signing_key=signing_key, count_evidence=count_evidence,
        candidate=candidate, trusted_run=trusted_run)
    trusted_plan_sha256 = plan["plan_sha256"]
    steps.append(("plan", executableplan.public_reference(plan)))

    # EX5-R21, with the D2-only half: prerequisite 16 names WHICH executable
    # plan was approved for generation. An approval to generate is not an
    # approval to generate anything.
    pin_values = record_set.operator_pin_record()["pins"]
    governed = enginebridge.governed_policy_digests(engine,
                                                    pin_values=pin_values)
    steps.append(("runtime_binding",
                  runtimebinding.assert_authorizations_match_runtime(
                      record_set, phase="D2", observation=observations,
                      engine_identity=engine_identity,
                      capability_policy_sha256=governed[
                          "capability_policy_sha256"],
                      review_request_policy_sha256=governed[
                          "review_request_policy_sha256"],
                      bootstrap=bootstrap, pin_values=pin_values,
                      candidate_range={
                          "target_base_sha": candidate["target_base_sha"],
                          "diff_base_sha": candidate["target_base_sha"],
                          "head_sha": candidate["candidate_head_sha"]},
                      executable_plan_sha256=trusted_plan_sha256)))

    head = candidate["candidate_head_sha"]
    base = candidate["target_base_sha"]
    adapter_identity = adapter.builtin_adapter_identity(
        engine_source_sha256=engine_source_sha256)

    # The candidate arrives as DATA, exactly as it does in D1. D2 needs the
    # repository because the plan stores request HASHES, not bodies — so the
    # prompt bytes have to be earned again from the commits, which also
    # re-proves that every unit's atoms still exist in the range the plan
    # claims.
    if not callable(fetch):
        refuse("category=candidate_fetch_not_callable")
    fetched = fetch(destination=candidate["checkout_destination"],
                    head_sha=head, target_base_sha=base)
    if fetched.get("checked_out") is not False:
        refuse("category=candidate_was_checked_out — the candidate is fetched "
               "--no-checkout, as inert data; a working tree is candidate code "
               "on the machine that holds the credential")
    steps.append(("inert_fetch", fetched))

    rebuilt = enginebridge.build_skeleton(
        engine, target_base_sha=base, candidate_head_sha=head,
        repository_path=fetched["destination"])
    steps.append(("skeleton_rebuild",
                  {"review_skeleton_sha256":
                   rebuilt["review_skeleton_sha256"],
                   "source": "TRUSTED_ENGINE_REBUILD"}))

    pending = statuspublish.status_request(
        repository_numeric_id=candidate["repository_numeric_id"],
        candidate_head_sha=head, context=TRUSTED_CONTEXT, state="pending",
        description="trusted review in progress", target_url=trusted_run["url"],
        trusted_run_id=trusted_run["id"],
        trusted_run_attempt=trusted_run["attempt"])
    _publish(publisher, pending, publications)

    try:
        # STAGE 3, IN THE ENGINE. The lane used to reconstruct requests, send
        # them, normalize replies and decide the panel for itself — a parallel,
        # weaker implementation of `verifier.executor`, which already does all
        # of it with the properties this lane needs: hash equality against the
        # counted request rather than token equality, a global scan of EVERY
        # execution payload before any is sent, strict response envelopes,
        # verdict validation, the anti-copy tripwire, and a synthesis that
        # cannot clear a refutation.
        #
        # The lane supplies the four things the engine cannot have: the
        # verified plan, a real transport, the verified literal set, and the
        # challenge D1 counted.
        generation_transport = generationtransport.bind(
            engine, opener=opener, credential=credential, phase="D2",
            generation_attempt_cap=record_set.require(
                GENERATION_PREREQUISITE).require_typed(
                    "generation_attempt_cap"),
            engine_source_sha256=engine_source_sha256)
        executed = enginebridge.execute_review_plan(
            engine, skeleton=rebuilt, plan=plan,
            repository_path=fetched["destination"],
            transport=generation_transport,
            authorizations=record_set.literal_authorizations,
            challenge=counted_challenge)
        steps.append(("execution_preflight", {
            "scanned_execution_payload_count":
                executed["execution_preflight"][
                    "scanned_execution_payload_count"],
            "execution_preflight_sha256":
                executed["execution_preflight"]["execution_preflight_sha256"]}))
        steps.append(("output_privacy", executed["output_privacy"]))
        steps.append(("generation_transport", generation_transport.record()))

        payload = _finalize_from_engine(
            executed, plan=plan, head=head,
            trusted_plan_sha256=trusted_plan_sha256,
            adapter_identity=adapter_identity)
        steps.append(("verdicts", {"units": payload["unit_count"],
                                   "generation_calls":
                                   payload["generation_calls"]}))

        unsigned = evidencewire.trusted_envelope(
            evidence_class=EVIDENCE_CLASS, payload=payload, produced_by=PRODUCER,
            repository_numeric_id=candidate["repository_numeric_id"],
            workflow_run_id=trusted_run["id"],
            workflow_run_attempt=trusted_run["attempt"],
            ref=observations["observed_ref"], target_base_sha=base,
            candidate_head_sha=head, produced_at=produced_at)
        evidence = signing.sign_envelope(unsigned, key=signing_key)
        evidencewire.validate_envelope(evidence)

        decision = "approve" if payload["all_approved"] else "reject"
        _publish(publisher, statuspublish.status_request(
            repository_numeric_id=candidate["repository_numeric_id"],
            candidate_head_sha=head, context=TRUSTED_CONTEXT,
            state="success" if payload["all_approved"] else "failure",
            description=f"trusted review: {decision} "
                        f"({payload['unit_count']} units)",
            target_url=trusted_run["url"], trusted_run_id=trusted_run["id"],
            trusted_run_attempt=trusted_run["attempt"],
            evidence_sha256=evidence["envelope_sha256"]), publications)
    except LaneRefusal as refusal:
        _publish(publisher, statuspublish.status_request(
            repository_numeric_id=candidate["repository_numeric_id"],
            candidate_head_sha=head, context=TRUSTED_CONTEXT, state="failure",
            description=_short_reason(refusal), target_url=trusted_run["url"],
            trusted_run_id=trusted_run["id"],
            trusted_run_attempt=trusted_run["attempt"]), publications)
        raise

    statuspublish.assert_pending_preceded_terminal(publications)
    return {
        "phase": "D2_TRUSTED_GENERATION",
        "evidence": evidence,
        "payload": payload,
        "steps": dict(steps),
        "publications": publications,
        "honest_scope": ("every verdict came from a response this run asked "
                         "for, normalized without a single transform, and "
                         "carrying this run's challenge for its own unit. It "
                         "does not show the model read the diff"),
    }


def _assert_generation_approved(record_set, *, candidate: dict):
    """Prerequisite 16, its own record, authenticated like every other.

    The scope comparison that used to live here is gone.
    `LaneTrustedVerifier.assert_binds_this_review` compares repository
    identity, target base, diff base and head against this review for EVERY
    record before promoting any of them, so a second, weaker version of that
    check here would be duplicated policy — and duplicated policy is how the
    two copies end up disagreeing.

    What remains is the question only this phase asks: is the SIXTEENTH record
    present. `assert_authenticated(phase="D2")` requires it by name, so this
    is a second, explicit assertion of the same fact rather than the only one:
    an approval to count has never been an approval to generate."""
    trustedverifier.assert_authenticated(record_set, phase="D2")
    matching = [e for e in record_set.records.values()
                if e.prerequisite_key == GENERATION_PREREQUISITE]
    if not matching:
        refuse("category=generation_not_separately_approved — operator "
               "prerequisite 16 is outstanding. An approval to count has never "
               "been an approval to generate, and D1's record set does not "
               "carry over")
    if len(matching) > 1:
        refuse("category=generation_approval_duplicated")
    return {"generation_approved": True,
            "record_set_sha256": record_set.record_set_sha256}


def _finalize_from_engine(executed: dict, *, plan: dict, head: str,
                          trusted_plan_sha256: str,
                          adapter_identity: dict) -> dict:
    """The evidence payload, assembled from what the ENGINE decided.

    Nothing here re-decides anything. `executor.execute_batch` produced the
    per-unit decisions, `executor.synthesize` produced the overall one — and
    that synthesis is additive-only, so a unit any panelist refuted stays
    refuted whatever the aggregate says.

    Every unit in the plan must appear. A partial review reported as a review
    is the reader believing the unanswered units passed."""
    decisions = {}
    # WHY a unit was not approved, by the engine's own code. `execute_batch`
    # deliberately does not raise for a validated refutation — a well-formed
    # veto is the most load-bearing result the panel can produce, and raising
    # would lose it before anything durable was written. Dropping the block
    # here would lose it again one layer up: "reject" with no code is a review
    # that says no and cannot say why.
    #
    # The engine's `reason` is NOT carried. It is the one field that can quote
    # the payload — a unit id prefix here, but the same field elsewhere carries
    # a path or an atom fragment — and the block CODE is what a reader acts on.
    blocks = []
    for result in executed["batch_results"]:
        for unit_hash, decision in result["unit_decisions"].items():
            decisions[unit_hash] = ("approve" if decision["approved"]
                                    else "reject")
        for block in result["unit_blocks"]:
            blocks.append({"unit_sha256": block["unit_sha256"],
                           "code": block["code"],
                           "batch_id": result["batch_id"]})
    planned = sorted(u["unit_sha256"] for u in plan["final_units"])
    if sorted(decisions) != planned:
        refuse(f"category=verdict_coverage_incomplete planned={len(planned)} "
               f"answered={len(decisions)} — a partial review reported as a "
               "review is the reader believing the unanswered units passed")
    synthesis = executed["synthesis"]
    ledger = executed["generation_ledger"]
    return {
        "trusted_plan_sha256": trusted_plan_sha256,
        "candidate_head_sha": head,
        "requested_model_ids": list(executed["requested_model_ids"]),
        "required_approver": executed["required_approver"],
        "minimum_other_approvers": executed["minimum_other_approvers"],
        "unit_count": len(planned),
        "generation_calls": ledger.get("attempts", ledger.get(
            "generation_attempts", 0)),
        "decisions": dict(sorted(decisions.items())),
        "unit_blocks": sorted(blocks, key=lambda b: (b["unit_sha256"],
                                                     b["code"])),
        "all_approved": synthesis["overall_approved"],
        "refuted_unit_sha256": list(synthesis["refuted_unit_sha256"]),
        "synthesis": synthesis,
        "execution_preflight_sha256":
            executed["execution_preflight"]["execution_preflight_sha256"],
        "generation_ledger": ledger,
        "output_privacy": executed["output_privacy"],
        "response_adapter": adapter_identity,
        "honest_scope": (
            "every unit in the counted plan was asked of every governed model, "
            "from requests whose hashes match the ones D1 counted, after every "
            "execution payload in the run had been scanned. A unit any "
            "panelist refuted stays refuted: synthesis is additive-only"),
    }


def _short_reason(refusal: LaneRefusal) -> str:
    reason = getattr(refusal, "reason", "") or "refused"
    category = reason.split(" ", 1)[0]
    return f"trusted review refused: {category}"[:statuspublish.MAX_DESCRIPTION]
