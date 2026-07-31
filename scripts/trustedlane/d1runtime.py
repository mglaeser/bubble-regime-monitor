"""D1 — the trusted count lane, end to end.

EX3-R01. The template's credential-bearing step was

    python - <<'PROBE'
    from trustedlane import phases
    phases.assert_phase_permitted(phases.D1)
    PROBE

which refuses, correctly, and is not a lane. This module is the lane: the whole
ordered sequence, with every gate in front of the credential rather than beside
it.

**The order is the design.** Each step can only refuse things the steps before
it have already established, so the sequence is not stylistic:

     1  protected state          is this repository configured such that a
                                 credential is reachable only from a protected
                                 ref? Asked FIRST, because every later answer is
                                 worthless if the answer here is no.
     2  engine artifact          is the code about to hold the key the code the
                                 operator approved, by digest?
     3  operator records         did a human authorize this, recently, for this
                                 scope, with expiry?
     4  literal authorization    what exact number did they write? Not a
                                 default, not a formula.
     5  challenge seed           minted before anything is sent, so every
                                 request carries it.
     6  inert fetch              the candidate arrives as data, --no-checkout,
                                 never executed.
     7  skeleton rebuild         the trusted engine rebuilds the plan, through
                                 `verifier.plan.build_skeleton`. The
                                 candidate's own plan is an input to compare
                                 against, never the thing acted on.
     8  status: pending          published BEFORE any provider work, so a run
                                 that dies mid-flight leaves a visible
                                 unfinished status rather than none.
     9  engine core              the WHOLE of Stage 2, in the engine: global
                                 secret preflight over the exact assembled
                                 bytes, all three governed models, recursive
                                 fitting, batch packing, exact request hashes,
                                 the coverage proof and every PIN gate.
    10  ceiling + closure        the operator's input-token ceiling applied to
                                 what was actually counted, and zero generation
                                 calls asserted on the transport.
    11  signed evidence          TRUSTED_COUNT_EVIDENCE, signed with the key
                                 the protected environment holds.
    12  status: terminal         success only after the signature exists.

**Step 9 used to be this lane's own loop**, and that loop was the finding.
It built one request per unit for ONE model named by a `model=` parameter,
called `countledger.preflight` — which compares a token estimate to a budget
and scans nothing — and kept its own ledger. So the trusted lane counted a
single-model review, transmitted bytes no secret scanner had seen, and enforced
none of the twelve PINs. The engine already implements all of it correctly, so
the lane calls it: `enginebridge.prepare_review_plan_core` with
`counttransport.TrustedCountTransport`.

**Everything dangerous is injected.** `opener`, `credential`, `signing_key` and
`publisher` are parameters. In the workflow they are built by the phase-gated
constructors — `transport.open_https`, `transport.read_credential`,
`signing.read_signing_key` — which refuse outside D1/D2. Here they are
parameters so the entire sequence, including every refusal, runs against a fake
server in D0 with no credential in the process. The gate is on obtaining the
capability, not on calling the function that uses it, because obtaining it is
what a candidate branch cannot do.

**On failure, the status still lands.** A run that refuses at step 9 has
already published `pending`, and leaving it there would block the pull request
with no explanation forever. So the terminal publication happens in a `finally`
path: refusals become a `failure` status carrying the refusal category, and the
original refusal is re-raised rather than swallowed.
"""

from __future__ import annotations

import hashlib
import json

from . import (
    artifactload,
    challenge,
    countledger,
    counttransport,
    enginebridge,
    enginepolicy,
    evidencewire,
    protectedstate,
    signing,
    statuspublish,
    trustedverifier,
)
from .errors import LaneRefusal, refuse

PRODUCER = "trusted-lane-d1-count"
TRUSTED_CONTEXT = "trusted-verifier-count"
EVIDENCE_CLASS = "TRUSTED_COUNT_EVIDENCE"


def _publish(publisher, request: dict, sink: list) -> dict:
    """Publish one status and record it, so ordering can be checked at the end.

    `publisher` is injected for the same reason the opener is: publishing needs
    an installation token, and `statuspublish.publish` refuses precisely
    because this branch must not hold one."""
    if not callable(publisher):
        refuse("category=status_publisher_not_callable")
    publisher(request)
    sink.append(request)
    return request


def run(*, observations: dict, operator_claims, lane_verifier, engine: dict,
        engine_artifact: dict,
        candidate: dict, plan: dict, opener, credential: str, signing_key,
        publisher, trusted_run: dict, observed_now: str, produced_at: str,
        fetch) -> dict:
    """Run the count lane. Returns the signed evidence, or refuses.

    Long parameter list on purpose: every capability and every observation is
    named at the call site, so a reader of the workflow can see exactly what
    the runtime was given. A convenience wrapper that assembled half of these
    internally would be the place where one of them quietly became a default.

    Two parameters are GONE, and their absence is the point. `model` named one
    model, so the trusted lane counted a one-model review while the governed
    policy is a three-model panel — the panel now comes from the engine and no
    caller can choose it. `skeleton_rebuild` was an injected callable, so what
    "the trusted engine rebuilt" meant depended on what the runner passed; it
    is now `verifier.plan.build_skeleton`, through the bridge.
    """
    steps = []
    publications: list = []

    # 1. Protected state. Asked first: every later answer is worthless if a
    #    credential is reachable from an unprotected ref.
    steps.append(("protected_state", protectedstate.assert_ready_for_credential(
        **observations)))

    # 2. The engine that is about to hold the key, by digest.
    steps.append(("engine_artifact", artifactload.inspect_archive(
        engine_artifact["path"],
        expected_sha256=engine_artifact["expected_sha256"])))
    steps.append(("engine_root", artifactload.assert_engine_root_is_not_the_candidate(
        engine_artifact["root"])))
    # `engine_root` is supplied, so this refuses a `verifier` module loaded from
    # ANYWHERE ELSE rather than refusing the name outright. The engine is
    # `scripts/verifier` inside the approved artifact — the lane authenticates
    # PIN and literal claims through it — so a name check here would mean either
    # that this step or that authentication had to go.
    steps.append(("no_candidate_import", enginepolicy.assert_no_candidate_import(
        search_path=engine_artifact.get("search_path"),
        engine_root=engine_artifact.get("root"))))

    # 3. Did a human authorize this, recently, for this occurrence?
    #
    #    AUTHENTICATED, not parsed. This used to be
    #
    #        verified_records = operatorrecord.verify_records(...)
    #
    #    whose own return payload says `authorized: False`: it checks shape,
    #    anchor KIND, digest, scope syntax and expiry, and never checks that
    #    the anchor exists or that anything is signed. A well-shaped
    #    branch-written record authorized real spending. `authenticate_record_set`
    #    puts every claim through `LaneTrustedVerifier`, which verifies the
    #    anchor against a key the operator supplied out of band.
    record_set = trustedverifier.authenticate_record_set(
        operator_claims, verifier=lane_verifier, phase="D1")
    steps.append(("operator_records", record_set.to_record()))

    # 4. The exact number they wrote, for THIS occurrence, out of a record set
    #    whose TYPE is the authority — a dict wearing a verified label cannot
    #    satisfy `assert_authenticated`.
    authorization = countledger.authorize(record_set=record_set)
    steps.append(("authorization", authorization))

    # 5. Minted before anything is sent, so every request carries it.
    seed = challenge.mint_seed(seed=candidate.get("challenge_seed"))

    # 6. The candidate arrives as DATA. `fetch` is a parameter rather than a
    #    direct `candidatefetch.fetch_candidate` call so the sequence stays
    #    testable, but it is required rather than optional: an earlier version
    #    of this function listed "inert fetch" in its docstring and never
    #    fetched anything, leaving the property to whatever `skeleton_rebuild`
    #    happened to do. A step that only exists in a comment is not a step.
    if not callable(fetch):
        refuse("category=candidate_fetch_not_callable — the candidate must be "
               "fetched by this lane, as data, rather than assumed to be "
               "already present in whatever state the runner left it")
    fetched = fetch(destination=candidate["checkout_destination"],
                    head_sha=candidate["candidate_head_sha"],
                    target_base_sha=candidate["target_base_sha"])
    _assert_fetch_was_inert(fetched)
    steps.append(("inert_fetch", fetched))

    # 7. The TRUSTED engine rebuilds the plan, with the REAL planner. The
    #    candidate's own plan is compared against, never acted on.
    skeleton = enginebridge.build_skeleton(
        engine, target_base_sha=candidate["target_base_sha"],
        candidate_head_sha=candidate["candidate_head_sha"],
        repository_path=fetched["destination"])
    planned_units = _assert_rebuilt_plan_matches({"units": skeleton["units"]},
                                                 plan)
    steps.append(("skeleton_rebuild",
                  {"units": len(planned_units),
                   "review_skeleton_sha256": skeleton["review_skeleton_sha256"],
                   "requested_model_ids": list(
                       enginebridge.model_panel(engine)),
                   "source": "TRUSTED_ENGINE_REBUILD"}))

    head = candidate["candidate_head_sha"]
    base = candidate["target_base_sha"]

    # 8. Pending BEFORE any provider work.
    pending = statuspublish.status_request(
        repository_numeric_id=candidate["repository_numeric_id"],
        candidate_head_sha=head, context=TRUSTED_CONTEXT, state="pending",
        description="trusted count in progress",
        target_url=trusted_run["url"], trusted_run_id=trusted_run["id"],
        trusted_run_attempt=trusted_run["attempt"])
    _publish(publisher, pending, publications)

    try:
        # 9. THE ENGINE does Stage 2. Every request is assembled, scanned and
        #    hashed before any is sent; the panel is governed policy; the
        #    twelve PINs are the authenticated ones; the literal clearances are
        #    the verified set. None of that is reimplemented here.
        run_challenge = challenge.run_token(
            seed=seed, candidate_head_sha=head,
            trusted_run_id=trusted_run["id"],
            trusted_run_attempt=trusted_run["attempt"])
        count_transport = counttransport.bind(
            engine, opener=opener, credential=credential, phase="D1",
            authorized_input_tokens=authorization["authorized_input_tokens"])
        core = enginebridge.prepare_review_plan_core(
            engine, skeleton=skeleton,
            repository_path=fetched["destination"],
            pin_record=record_set.operator_pin_record(),
            transport=count_transport,
            authorizations=record_set.literal_authorizations,
            challenge=run_challenge)

        # 10. The operator's ceiling, applied to what was ACTUALLY counted, and
        #     zero generation asserted on the transport rather than inferred
        #     from the plan: the plan says what was intended, the transport
        #     says what was sent.
        total = _total_input_tokens(core)
        steps.append(("authorized_ceiling",
                      count_transport.assert_within_authorized_tokens(total)))
        steps.append(("zero_generation",
                      count_transport.assert_zero_generation()))
        payload = _count_payload(
            core, skeleton=skeleton, engine=engine, total_input_tokens=total,
            run_challenge=run_challenge, transport=count_transport,
            record_set=record_set,
            repository_numeric_id=candidate["repository_numeric_id"],
            candidate_head_sha=head, trusted_run=trusted_run)
        steps.append(("ledger", {"total_input_tokens": total,
                                 "trusted_plan_sha256":
                                 payload["trusted_plan_sha256"]}))

        # 11. Signed evidence. Unsigned, this record does not validate at all.
        unsigned = evidencewire.trusted_envelope(
            evidence_class=EVIDENCE_CLASS, payload=payload, produced_by=PRODUCER,
            repository_numeric_id=candidate["repository_numeric_id"],
            workflow_run_id=trusted_run["id"],
            workflow_run_attempt=trusted_run["attempt"],
            ref=observations["observed_ref"], target_base_sha=base,
            candidate_head_sha=head, produced_at=produced_at)
        evidence = signing.sign_envelope(unsigned, key=signing_key)
        evidencewire.validate_envelope(evidence)

        # 12. Success only after the signature exists.
        terminal = statuspublish.status_request(
            repository_numeric_id=candidate["repository_numeric_id"],
            candidate_head_sha=head, context=TRUSTED_CONTEXT, state="success",
            description=(f"trusted count complete: "
                         f"{payload['total_input_tokens']} input tokens"),
            target_url=trusted_run["url"], trusted_run_id=trusted_run["id"],
            trusted_run_attempt=trusted_run["attempt"],
            evidence_sha256=evidence["envelope_sha256"])
        _publish(publisher, terminal, publications)
    except LaneRefusal as refusal:
        # The pending status is already on the pull request. Leaving it there
        # would block the candidate with no explanation forever, so the failure
        # is published and the refusal is re-raised rather than swallowed.
        _publish(publisher, statuspublish.status_request(
            repository_numeric_id=candidate["repository_numeric_id"],
            candidate_head_sha=head, context=TRUSTED_CONTEXT, state="failure",
            description=_short_reason(refusal), target_url=trusted_run["url"],
            trusted_run_id=trusted_run["id"],
            trusted_run_attempt=trusted_run["attempt"]), publications)
        raise

    statuspublish.assert_pending_preceded_terminal(publications)
    return {
        "phase": "D1_TRUSTED_COUNT",
        "evidence": evidence,
        "payload": payload,
        "steps": dict(steps),
        "publications": publications,
        "generation_calls": 0,
        "honest_scope": ("a signed count over a plan the trusted engine "
                         "rebuilt, under a ceiling an operator wrote, "
                         "published on the exact reviewed head. It is not a "
                         "review: no verdict was requested and none was given"),
    }


def _total_input_tokens(core: dict) -> int:
    """What the provider actually counted, summed over every count record.

    Read from the engine's own records rather than recomputed from the batches:
    the records are what came back, and the batches are what was asked."""
    records = core.get("count_records")
    if not isinstance(records, list) or not records:
        refuse("category=engine_core_counted_nothing — a run that counted no "
               "request has no cost to report and no plan to sign")
    total = 0
    for index, record in enumerate(records):
        tokens = record.get("input_tokens") if isinstance(record, dict) else None
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            refuse(f"category=count_record_tokens_not_an_integer entry={index}")
        total += tokens
    return total


def trusted_plan_digest(core: dict, *, skeleton: dict,
                        run_challenge: str) -> str:
    """The identity of the plan that was counted, and that D2 may execute.

    Over the batch digests AND the exact per-model request hashes AND the
    challenge, not over unit ids. An earlier version of this idea in
    `d2runtime` hashed only `sorted(unit_sha256)`, and nothing anywhere
    recomputes a unit id from its content — so the prompt bytes that would
    actually be sent were outside the digest that identifies "the plan D1
    counted". Swapping a unit's prompt while keeping its id still matched.

    The challenge is inside it because a plan counted under one challenge is
    not the plan that would be sent under another: the challenge is in the
    request bytes, so it is in the token counts."""
    batches = core.get("batches")
    if not isinstance(batches, list) or not batches:
        refuse("category=engine_core_produced_no_batches")
    entries = []
    for index, batch in enumerate(batches):
        missing = [f for f in ("batch_sha256", "unit_sha256_in_order",
                               "request_hashes_by_model")
                   if not batch.get(f)]
        if missing:
            refuse(f"category=batch_record_incomplete entry={index} "
                   f"fields={missing}")
        entries.append({
            "batch_sha256": batch["batch_sha256"],
            "unit_sha256_in_order": list(batch["unit_sha256_in_order"]),
            "request_hashes_by_model": dict(batch["request_hashes_by_model"]),
        })
    entries.sort(key=lambda e: e["batch_sha256"])
    blob = json.dumps(
        {"review_skeleton_sha256": skeleton["review_skeleton_sha256"],
         "execution_challenge_sha256": hashlib.sha256(
             run_challenge.encode()).hexdigest(),
         "batches": entries},
        sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"trusted-count-plan-v1\x00" + blob).hexdigest()


def _count_payload(core: dict, *, skeleton: dict, engine: dict,
                   total_input_tokens: int, run_challenge: str, transport,
                   record_set, repository_numeric_id: int,
                   candidate_head_sha: str, trusted_run: dict) -> dict:
    """What the signature covers.

    Everything a reader would have to take on trust otherwise: which panel,
    which approver, which skeleton, which literal set, which PIN authorization,
    how many sockets, and the plan digest D2 must match. The challenge itself
    is NOT in it — only its digest — because the evidence record is published
    and a published challenge is a challenge the next run's models could
    echo."""
    return {
        "schema_version": 2,
        "repository_numeric_id": repository_numeric_id,
        "candidate_head_sha": candidate_head_sha,
        "trusted_run_id": trusted_run["id"],
        "trusted_run_attempt": trusted_run["attempt"],
        "review_skeleton_sha256": skeleton["review_skeleton_sha256"],
        "trusted_plan_sha256": trusted_plan_digest(
            core, skeleton=skeleton, run_challenge=run_challenge),
        "execution_challenge_sha256": hashlib.sha256(
            run_challenge.encode()).hexdigest(),
        "requested_model_ids": list(enginebridge.model_panel(engine)),
        "required_approver": enginebridge.required_approver(engine),
        "policy_pin_names": list(enginebridge.pin_names(engine)),
        "operator_pin_record_sha256": record_set.require(
            "authorize_twelve_pins").candidate_claim_self_sha256,
        "literal_authorization_set_sha256": (
            record_set.literal_authorizations.digest()
            if record_set.literal_authorizations is not None else None),
        "operator_record_set_sha256": record_set.record_set_sha256,
        "preflight_manifest_sha256": core["preflight_manifest"][
            "preflight_manifest_sha256"],
        "count_ledger_sha256": core["count_ledger"]["count_ledger_sha256"],
        "batch_count": len(core["batches"]),
        "final_unit_count": len(core["final_units"]),
        "total_input_tokens": total_input_tokens,
        "count_attempts": transport.calls,
        "generation_calls": 0,
        "worst_case_total_micro_usd": core["cost_plan"][
            "worst_case_total_micro_usd"],
        "engine_core_version": core["core_version"],
        "honest_scope": ("exact input-token counts from the count endpoint, "
                         "for every request in a plan the trusted engine "
                         "rebuilt and scanned in full before sending any of "
                         "it. No verdict was requested and none was given"),
    }


def _short_reason(refusal: LaneRefusal) -> str:
    """A status description is 140 characters and a reader sees only it.

    The category is the useful half — it names which gate refused — and the
    prose after the dash is what does not fit. Truncating from the front would
    lose exactly the part worth reading."""
    reason = getattr(refusal, "reason", "") or "refused"
    category = reason.split(" ", 1)[0]
    return f"trusted count refused: {category}"[:statuspublish.MAX_DESCRIPTION]


def _assert_fetch_was_inert(fetched) -> dict:
    """The candidate is data. Nothing from it may have been checked out or run.

    `candidatefetch.verify_clone` establishes this; the check is repeated here
    because `fetch` is injected, and an injected function that quietly returned
    a working tree would put candidate code on disk beside a live credential."""
    if not isinstance(fetched, dict):
        refuse("category=candidate_fetch_result_not_an_object")
    if fetched.get("checked_out") is not False:
        refuse("category=candidate_was_checked_out — the candidate is fetched "
               "--no-checkout, as inert data; a working tree is candidate code "
               "on the machine that holds the credential")
    if not fetched.get("head_sha"):
        refuse("category=candidate_fetch_named_no_head — a fetch that cannot "
               "say what it fetched cannot be compared to what was reviewed")
    return {"checked_out": False, "head_sha": fetched["head_sha"]}


def _assert_rebuilt_plan_matches(rebuilt, declared_plan: dict) -> list:
    """The candidate's plan is an input to COMPARE against, never acted on.

    A rebuild that silently agreed with whatever the candidate proposed would
    make the rebuild decorative. A rebuild that disagreed and proceeded on its
    own answer would be reviewing something the operator did not approve the
    cost of. So a disagreement is a refusal, and the operator sees it."""
    if not isinstance(rebuilt, dict):
        refuse("category=skeleton_rebuild_not_a_result")
    units = rebuilt.get("units")
    if not isinstance(units, list) or not units:
        refuse("category=skeleton_rebuild_produced_no_units — a plan over "
               "nothing costs nothing and reviews nothing")
    # Identity only. The prompt bytes used to be checked here, because the
    # lane assembled them; the engine assembles them now, from the atoms it
    # reconstructs out of the commits, and a lane-side requirement that a
    # skeleton unit carry `instructions` would be this module having an opinion
    # about a shape the engine owns.
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            refuse(f"category=skeleton_unit_not_an_object index={index}")
        if not unit.get("unit_sha256"):
            refuse(f"category=skeleton_unit_incomplete index={index} "
                   "fields=['unit_sha256']")
    digests = [unit["unit_sha256"] for unit in units]
    if len(set(digests)) != len(digests):
        refuse("category=skeleton_units_duplicated")

    declared = declared_plan.get("unit_sha256s")
    if declared is None:
        refuse("category=candidate_plan_declares_no_units — with nothing to "
               "compare against, the rebuild is unchecked and the comparison "
               "is theatre")
    if sorted(declared) != sorted(digests):
        only_candidate = sorted(set(declared) - set(digests))
        only_trusted = sorted(set(digests) - set(declared))
        refuse(f"category=rebuilt_plan_differs_from_candidate_plan "
               f"only_in_candidate={len(only_candidate)} "
               f"only_in_rebuild={len(only_trusted)} — proceeding on the "
               "rebuild would count something the operator did not approve, "
               "and proceeding on the candidate's plan would let the reviewed "
               "branch choose what gets reviewed")
    return units
