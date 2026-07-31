"""D2 — the trusted generation lane.

EX3-R01. Same shape as D1 and deliberately not the same code path, because the
two phases differ in exactly the ways that matter and a shared "run the lane"
function with a `generate=True` flag would put those differences inside a
branch nobody reads.

What D2 has that D1 does not:

* **A separate operator approval.** Prerequisite 16, its own record, its own
  environment. An approval to count has never been an approval to generate,
  and reusing D1's environment would make it one by accident.
* **A plan it did not choose.** D2 executes the plan D1 counted, identified by
  digest. It does not rebuild — rebuilding here would let D2 review something
  whose cost was never counted or approved.
* **Verdicts to check.** Every response is normalized by the trusted adapter,
  and every verdict must echo this run's challenge for its own unit.

What both have: protected state first, engine by digest, records before the
credential, pending before any provider work, and a terminal status whatever
happens.

The generation send path is NOT `transport.count_once` with a different URL.
`transport.exchange` refuses any endpoint but the count endpoint, and that
refusal is one of the things keeping D1 honest, so it stays exactly as it is.
D2 goes through `transport.exchange_generation`, a separate function that
refuses the COUNT endpoint symmetrically. Both share only the private
credential-joining step, which asserts nothing about the URL and is unreachable
except through one of the two.

A flag — `exchange(request, allow_generation=True)` — would have been shorter
and is the thing to avoid: it puts the difference between "counts" and
"generates" into a parameter, where getting it wrong looks like configuration
rather than like generating from the count lane.
"""

from __future__ import annotations

import hashlib
import json

from . import (
    adapter,
    artifactload,
    challenge,
    enginebridge,
    enginepolicy,
    evidencewire,
    protectedstate,
    signing,
    statuspublish,
    transport,
    trustedverifier,
)
from .errors import LaneRefusal, refuse

PRODUCER = "trusted-lane-d2-generation"
TRUSTED_CONTEXT = "trusted-cross-vendor-review"
EVIDENCE_CLASS = "TRUSTED_EXECUTION_EVIDENCE"
GENERATION_PATH = "/responses"

#: Operator prerequisite 16, by its canonical key in `prerequisites.py` rather
#: than a shorter name invented here. A key that does not match the registry is
#: a record nobody can write, and the refusal for "no such approval" would look
#: identical to the refusal for "approval not granted".
GENERATION_PREREQUISITE = "approve_generation_separately"


def build_generation_request(*, model: str, instructions: str,
                             input_text: str) -> dict:
    """The generation request, with no credential in it.

    Structurally the same promise as the count request: the record a caller
    holds and digests carries no key, because the key is joined to a copy of
    the headers inside `exchange`."""
    for label, value in (("model", model), ("instructions", instructions),
                         ("input_text", input_text)):
        if not isinstance(value, str) or not value:
            refuse(f"category=generation_request_field_missing field={label}")
    payload = {"model": model, "instructions": instructions,
               "input": input_text, "truncation": "disabled"}
    body = json.dumps(payload, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return {
        "method": "POST",
        "url": f"{transport.BASE_URL}{GENERATION_PATH}",
        "headers": {"content-type": "application/json",
                    "accept": "application/json"},
        "body": body,
        "payload_sha256": hashlib.sha256(body).hexdigest(),
        "carries_credential": False,
    }


def assert_request_is_generation(request: dict) -> dict:
    """The mirror of the count lane's endpoint check, not a hole in it.

    `transport.assert_request_is_count_only` refuses everything but the count
    endpoint, which is what stops D1 generating. D2 needs the opposite
    assertion rather than a relaxation of that one, so neither lane's gate is
    weakened to let the other through."""
    if not isinstance(request, dict):
        refuse("category=request_not_object")
    if request.get("method") != "POST":
        refuse(f"category=request_method_not_permitted "
               f"method={request.get('method')!r}")
    url = request.get("url")
    if url != f"{transport.BASE_URL}{GENERATION_PATH}":
        refuse(f"category=request_endpoint_not_the_generation_endpoint "
               f"url={url!r}")
    return {"endpoint": url, "generation_call": True}


def _publish(publisher, request: dict, sink: list) -> dict:
    if not callable(publisher):
        refuse("category=status_publisher_not_callable")
    publisher(request)
    sink.append(request)
    return request


def assert_plan_is_the_counted_one(plan: dict, *, expected_sha256: str) -> dict:
    """D2 executes the plan D1 counted — identified by digest, not by name.

    Rebuilding here would let D2 review something whose cost was never counted
    or approved, and accepting a plan that merely claims to be the counted one
    is the same thing with an extra step."""
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        refuse("category=trusted_plan_digest_malformed")
    units = plan.get("units")
    if not isinstance(units, list) or not units:
        refuse("category=trusted_plan_has_no_units")
    digests = [u.get("unit_sha256") for u in units]
    if any(not isinstance(d, str) or len(d) != 64 for d in digests):
        refuse("category=trusted_plan_unit_sha_malformed")
    if len(set(digests)) != len(digests):
        refuse("category=trusted_plan_units_duplicated")
    observed = plan_digest(units)
    if observed != expected_sha256:
        refuse(f"category=trusted_plan_digest_mismatch observed={observed} "
               f"expected={expected_sha256} — this is not the plan D1 counted, "
               "so its cost was never approved")
    return {"trusted_plan_sha256": observed, "units": len(units)}


def plan_digest(units) -> str:
    """The digest D1 publishes and D2 requires — over the PROMPT BYTES.

    It used to hash only `sorted(unit_sha256)`. Nothing anywhere recomputes a
    unit id from its content, so the instructions and input text D2 actually
    sends to the generation endpoint were outside the digest that is supposed
    to identify "the plan D1 counted": swap a unit's prompt while keeping its
    id and the digest still matched. Found by adversarial review.

    Sorted by unit id, so the same plan in a different order is the same plan
    and a different plan cannot hide behind reordering."""
    entries = []
    for unit in units:
        if not isinstance(unit, dict):
            refuse("category=trusted_plan_unit_not_an_object")
        missing = [f for f in ("unit_sha256", "instructions", "input_text")
                   if not unit.get(f)]
        if missing:
            refuse(f"category=trusted_plan_unit_incomplete fields={missing} — "
                   "a digest that skips the prompt does not identify what was "
                   "counted")
        entries.append({
            "unit_sha256": unit["unit_sha256"],
            "instructions_sha256": hashlib.sha256(
                unit["instructions"].encode("utf-8")).hexdigest(),
            "input_text_sha256": hashlib.sha256(
                unit["input_text"].encode("utf-8")).hexdigest(),
        })
    blob = json.dumps({"units": sorted(entries,
                                       key=lambda e: e["unit_sha256"])},
                      sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"trusted-plan-v2\x00" + blob).hexdigest()


def run(*, observations: dict, operator_claims, lane_verifier,
        engine: dict, engine_artifact: dict,
        candidate: dict, plan: dict, trusted_plan_sha256: str, opener,
        credential: str, signing_key, publisher, trusted_run: dict,
        observed_now: str, produced_at: str,
        engine_source_sha256: str) -> dict:
    """Run the generation lane. Returns signed execution evidence, or refuses."""
    steps = []
    publications: list = []

    steps.append(("protected_state", protectedstate.assert_ready_for_credential(
        **observations)))
    steps.append(("engine_artifact", artifactload.inspect_archive(
        engine_artifact["path"],
        expected_sha256=engine_artifact["expected_sha256"])))
    steps.append(("engine_root",
                  artifactload.assert_engine_root_is_not_the_candidate(
                      engine_artifact["root"])))
    # `engine_root` is supplied, so this refuses a `verifier` module loaded from
    # ANYWHERE ELSE rather than refusing the name outright. The engine is
    # `scripts/verifier` inside the approved artifact — the lane authenticates
    # PIN and literal claims through it — so a name check here would mean either
    # that this step or that authentication had to go.
    steps.append(("no_candidate_import", enginepolicy.assert_no_candidate_import(
        search_path=engine_artifact.get("search_path"),
        engine_root=engine_artifact.get("root"))))

    # AUTHENTICATED, not parsed — see the note in d1runtime. D2 requires the
    # same fifteen records D1 does PLUS prerequisite 16, and phase="D2" is what
    # makes `authenticate_record_set` demand it.
    record_set = trustedverifier.authenticate_record_set(
        operator_claims, verifier=lane_verifier, phase="D2")
    steps.append(("operator_records", record_set.to_record()))

    # Prerequisite 16, its own record. An approval to count has never been an
    # approval to generate, so this is a DIFFERENT key from D1's.
    _assert_generation_approved(record_set, candidate=candidate)

    steps.append(("plan", assert_plan_is_the_counted_one(
        plan, expected_sha256=trusted_plan_sha256)))

    head = candidate["candidate_head_sha"]
    base = candidate["target_base_sha"]
    seed = challenge.mint_seed(seed=candidate.get("challenge_seed"))
    adapter_identity = adapter.builtin_adapter_identity(
        engine_source_sha256=engine_source_sha256)

    pending = statuspublish.status_request(
        repository_numeric_id=candidate["repository_numeric_id"],
        candidate_head_sha=head, context=TRUSTED_CONTEXT, state="pending",
        description="trusted review in progress", target_url=trusted_run["url"],
        trusted_run_id=trusted_run["id"],
        trusted_run_attempt=trusted_run["attempt"])
    _publish(publisher, pending, publications)

    try:
        # THE PANEL, from the engine. It used to be a `model=` parameter, so a
        # check named "cross-vendor review" asked ONE model and a caller chose
        # which — the finding stated as a signature. `verifier.policy` owns the
        # panel and `verifier.reviewpolicy` owns which member must approve.
        panel = enginebridge.model_panel(engine)
        approver = enginebridge.required_approver(engine)
        steps.append(("review_panel", {"requested_model_ids": list(panel),
                                       "required_approver": approver,
                                       "minimum_other_approvers":
                                       MINIMUM_OTHER_APPROVERS}))

        by_unit: dict = {}
        generation_calls = 0
        for unit in plan["units"]:
            token = challenge.token_for_unit(
                seed=seed, unit_sha256=unit["unit_sha256"],
                candidate_head_sha=head, trusted_run_id=trusted_run["id"],
                trusted_run_attempt=trusted_run["attempt"])
            for model_id in panel:
                request = build_generation_request(
                    model=model_id,
                    instructions=(f"{unit['instructions']}\n"
                                  f"{challenge.instruction_line(token)}"),
                    input_text=unit["input_text"])
                assert_request_is_generation(request)
                reply = transport.exchange_generation(
                    request, opener=opener, credential=credential)
                generation_calls += 1
                raw = bytes(reply["body"])
                normalized = adapter.normalize(
                    raw, adapter_identity=adapter_identity,
                    http_status=reply["status"],
                    raw_binding={
                        "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                        "raw_response_bytes": len(raw),
                        "http_status": reply["status"], "model_id": model_id,
                        "request_semantics_sha256": request["payload_sha256"],
                        "attempt": trusted_run["attempt"]})
                challenge.assert_all_echoed(
                    normalized["verdicts"], seed=seed, candidate_head_sha=head,
                    trusted_run_id=trusted_run["id"],
                    trusted_run_attempt=trusted_run["attempt"])
                _assert_verdicts_are_for_this_unit(normalized["verdicts"], unit)
                for verdict in normalized["verdicts"]:
                    by_unit.setdefault(verdict["unit_sha256"], {})[
                        model_id] = verdict

        decisions = {unit["unit_sha256"]: _assert_panel_decided(
            by_unit.get(unit["unit_sha256"], {}), unit_sha256=unit["unit_sha256"],
            panel=panel, approver=approver)
            for unit in plan["units"]}

        # EX4-R18. The ANTI-COPY tripwire, and it is the ENGINE's — every
        # approving model's reasoning is compared with every other's, after
        # normalization that folds homoglyphs and strips the challenge, the
        # model id and the lens name. Without it, three models returning one
        # canned sentence read as three corroborations.
        #
        # Only where the panel approved: a refutation is a finding, and two
        # models independently describing the same real defect in the same
        # words is agreement, not collusion.
        steps.append(("distinct_reasoning", _assert_distinct_reasoning(
            by_unit, engine=engine, decisions=decisions, panel=panel,
            approver=approver, seed=seed, head=head,
            trusted_run=trusted_run)))
        payload = _finalize(decisions, by_unit, plan=plan, head=head,
                            generation_calls=generation_calls,
                            trusted_plan_sha256=trusted_plan_sha256,
                            panel=panel, approver=approver)
        steps.append(("verdicts", {"count": payload["verdict_count"],
                                   "units": payload["unit_count"],
                                   "generation_calls": generation_calls}))

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
                        f"({payload['verdict_count']} units)",
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


def _assert_verdicts_are_for_this_unit(verdicts, unit: dict):
    """A response may only answer the unit it was asked about.

    Without this, one response carrying verdicts for other units would let a
    single call decide questions it was never asked — and the challenge check
    would pass, because those units' tokens are derivable by whoever holds the
    instructions for them."""
    for verdict in verdicts:
        if verdict.get("unit_sha256") != unit["unit_sha256"]:
            refuse(f"category=verdict_for_a_different_unit "
                   f"asked={unit['unit_sha256'][:12]} "
                   f"answered={str(verdict.get('unit_sha256'))[:12]} — one "
                   "response deciding questions it was not asked is one call "
                   "standing in for a batch")
    if len(verdicts) != 1:
        refuse(f"category=unit_answered_more_than_once count={len(verdicts)}")


def _assert_distinct_reasoning(by_unit: dict, *, engine: dict, decisions: dict,
                               panel, approver: str, seed: bytes, head: str,
                               trusted_run: dict) -> dict:
    """Call the engine's tripwire, per approved unit.

    Two shape translations and nothing else. The engine indexes
    `by_model[model][unit]`; this lane collects `by_unit[unit][model]`. And the
    engine reads `refuted`, which the lane's normalized verdict does not carry
    because it records a three-valued `decision` — a model that ABSTAINED has
    not approved, so for this gate it is refuted.

    Deliberately not reimplemented. Similarity scoring, Unicode folding, the
    charset policy and the governed threshold all live in
    `verifier.verdicts`, and a lane-side second version is exactly the
    duplication that produced a length check standing in for this gate the
    first time."""
    verdicts = engine["modules"]["verifier.verdicts"]
    reviewpolicy = engine["modules"]["verifier.reviewpolicy"]
    blocking = _blocking_error(engine)
    corroborators = [m for m in panel if m != approver]
    lens_ids = tuple(reviewpolicy.lens_id(m) for m in panel)

    checked = []
    for unit_sha256, decision in sorted(decisions.items()):
        if decision != "approve":
            continue
        by_model = {
            model: {unit_sha256: {**verdict,
                                  "refuted": verdict.get("decision") != "approve"}}
            for model, verdict in by_unit[unit_sha256].items()}
        challenge_token = challenge.token_for_unit(
            seed=seed, unit_sha256=unit_sha256, candidate_head_sha=head,
            trusted_run_id=trusted_run["id"],
            trusted_run_attempt=trusted_run["attempt"])
        try:
            checked.append(verdicts.assert_distinct_reasoning(
                by_model, unit_hash=unit_sha256, approver=approver,
                challenge=challenge_token, corroborators=corroborators,
                model_ids=tuple(panel), lens_ids=lens_ids))
        except blocking as exc:
            refuse(f"category=panel_reasoning_not_distinct "
                   f"unit={unit_sha256[:12]} "
                   f"code={getattr(exc, 'code', 'UNKNOWN')} — two approvals "
                   "that say the same thing are one review reported twice, "
                   "and independent corroboration is the property this panel "
                   "exists to provide")
    return {"units_checked": len(checked),
            "gate": "verifier.verdicts.assert_distinct_reasoning",
            "honest_scope": ("this catches copies. It does not prove "
                             "independent reasoning: prose cannot be checked "
                             "for thought, and semantic independence stays a "
                             "trust assumption")}


def _blocking_error(engine: dict):
    return engine["modules"]["verifier.errors"].BlockingError


#: How many models BESIDES the required approver must also approve. One is the
#: governed minimum: an approval that only the required approver gave is one
#: model's opinion wearing a panel's name.
MINIMUM_OTHER_APPROVERS = 1


def _assert_panel_decided(verdicts_by_model: dict, *, unit_sha256: str,
                          panel, approver: str) -> str:
    """One unit's decision, from the whole panel rather than from one model.

    Three separate refusals, because they are three different failures:

    * a model that did not answer is not a model that abstained. A panel with a
      silent member is a smaller panel, and reporting it as the governed one is
      the finding this closes;
    * the REQUIRED approver must have approved. Its approval is not
      interchangeable with another model's — that is what "required" means;
    * enough others must also approve. An approval only the required approver
      gave is one model's opinion wearing a panel's name.

    Anything short of all three is not an approval. It is not a rejection
    either, and the distinction is kept: `abstain` says the panel declined to
    decide, which a reader must not read as "passed"."""
    missing = sorted(m for m in panel if m not in verdicts_by_model)
    if missing:
        refuse(f"category=panel_member_did_not_answer unit={unit_sha256[:12]} "
               f"models={missing} — a panel with a silent member is a smaller "
               "panel, and a silent model has not approved anything")
    decisions = {m: verdicts_by_model[m].get("decision") for m in panel}
    if decisions.get(approver) != "approve":
        if decisions.get(approver) == "reject":
            return "reject"
        return "abstain"
    others = [m for m in panel
              if m != approver and decisions.get(m) == "approve"]
    if len(others) < MINIMUM_OTHER_APPROVERS:
        return "abstain"
    if any(d == "reject" for d in decisions.values()):
        return "reject"
    return "approve"


def _finalize(decisions: dict, by_unit: dict, *, plan: dict, head: str,
              generation_calls: int, trusted_plan_sha256: str, panel,
              approver: str) -> dict:
    planned = sorted(u["unit_sha256"] for u in plan["units"])
    answered = sorted(decisions)
    if answered != planned:
        refuse(f"category=verdict_coverage_incomplete planned={len(planned)} "
               f"answered={len(answered)} — a partial review reported as a "
               "review is the reader believing the unanswered units passed")
    expected_calls = len(planned) * len(panel)
    if generation_calls != expected_calls:
        refuse(f"category=generation_call_count_mismatch "
               f"calls={generation_calls} expected={expected_calls} "
               f"units={len(planned)} panel={len(panel)} — every unit is asked "
               "of every governed model, exactly once")
    verdicts = [v for unit in sorted(by_unit)
                for _model, v in sorted(by_unit[unit].items())]
    return {
        "trusted_plan_sha256": trusted_plan_sha256,
        "candidate_head_sha": head,
        "requested_model_ids": list(panel),
        "required_approver": approver,
        "minimum_other_approvers": MINIMUM_OTHER_APPROVERS,
        "verdict_count": len(verdicts),
        "unit_count": len(planned),
        "generation_calls": generation_calls,
        "decisions": dict(sorted(decisions.items())),
        "decisions_by_model": {
            unit: {model: verdict.get("decision")
                   for model, verdict in sorted(by_unit[unit].items())}
            for unit in sorted(by_unit)},
        "all_approved": all(d == "approve" for d in decisions.values()),
        "abstained": sorted(u for u, d in decisions.items() if d == "abstain"),
        "verdicts": verdicts,
        "honest_scope": ("every planned unit was asked of every governed "
                         "model exactly once. A unit counts as approved only "
                         "when the required approver approved it and at least "
                         f"{MINIMUM_OTHER_APPROVERS} other panel member did "
                         "too; anything less is recorded as abstain, because a "
                         "panel that declined to decide has not approved "
                         "anything"),
    }


def _short_reason(refusal: LaneRefusal) -> str:
    reason = getattr(refusal, "reason", "") or "refused"
    category = reason.split(" ", 1)[0]
    return f"trusted review refused: {category}"[:statuspublish.MAX_DESCRIPTION]
