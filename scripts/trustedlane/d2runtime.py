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
    enginepolicy,
    evidencewire,
    operatorrecord,
    protectedstate,
    signing,
    statuspublish,
    transport,
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
    blob = json.dumps({"units": sorted(digests)}, sort_keys=True,
                      separators=(",", ":")).encode()
    observed = hashlib.sha256(b"trusted-plan-v1\x00" + blob).hexdigest()
    if observed != expected_sha256:
        refuse(f"category=trusted_plan_digest_mismatch observed={observed} "
               f"expected={expected_sha256} — this is not the plan D1 counted, "
               "so its cost was never approved")
    return {"trusted_plan_sha256": observed, "units": len(units)}


def plan_digest(unit_sha256s) -> str:
    """The digest D1 publishes and D2 requires. Sorted, so the same plan in a
    different order is the same plan and a different plan cannot hide behind
    reordering."""
    blob = json.dumps({"units": sorted(unit_sha256s)}, sort_keys=True,
                      separators=(",", ":")).encode()
    return hashlib.sha256(b"trusted-plan-v1\x00" + blob).hexdigest()


def run(*, observations: dict, operator_records, engine_artifact: dict,
        candidate: dict, plan: dict, trusted_plan_sha256: str, opener,
        credential: str, signing_key, publisher, trusted_run: dict,
        observed_now: str, produced_at: str, model: str,
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
    steps.append(("no_candidate_import", enginepolicy.assert_no_candidate_import(
        search_path=engine_artifact.get("search_path"))))

    verified_records = operatorrecord.verify_records(
        operator_records, observed_now=observed_now)
    steps.append(("operator_records", verified_records))

    # Prerequisite 16, its own record. An approval to count has never been an
    # approval to generate, so this is a DIFFERENT key from D1's.
    _assert_generation_approved(verified_records, candidate=candidate)

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
        verdicts = []
        generation_calls = 0
        for unit in plan["units"]:
            token = challenge.token_for_unit(
                seed=seed, unit_sha256=unit["unit_sha256"],
                candidate_head_sha=head, trusted_run_id=trusted_run["id"],
                trusted_run_attempt=trusted_run["attempt"])
            request = build_generation_request(
                model=model,
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
                raw_binding={"raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                             "raw_response_bytes": len(raw),
                             "http_status": reply["status"], "model_id": model,
                             "request_semantics_sha256": request["payload_sha256"],
                             "attempt": trusted_run["attempt"]})
            challenge.assert_all_echoed(
                normalized["verdicts"], seed=seed, candidate_head_sha=head,
                trusted_run_id=trusted_run["id"],
                trusted_run_attempt=trusted_run["attempt"])
            _assert_verdicts_are_for_this_unit(normalized["verdicts"], unit)
            verdicts.extend(normalized["verdicts"])

        payload = _finalize(verdicts, plan=plan, head=head,
                            generation_calls=generation_calls,
                            trusted_plan_sha256=trusted_plan_sha256)
        steps.append(("verdicts", {"count": len(verdicts),
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


def _assert_generation_approved(verified_records: dict, *, candidate: dict):
    """Its own prerequisite, its own scope. Not D1's, reused."""
    from . import CANONICAL_REPOSITORY

    expected_scope = (f"{CANONICAL_REPOSITORY}@"
                      f"{candidate['candidate_head_sha']}")
    matching = [r for r in verified_records["records"]
                if r.get("prerequisite_key") == GENERATION_PREREQUISITE]
    if not matching:
        refuse("category=generation_not_separately_approved — operator "
               "prerequisite 16 is outstanding. An approval to count has never "
               "been an approval to generate, and D1's record does not carry "
               "over")
    if len(matching) > 1:
        refuse("category=generation_approval_duplicated")
    if matching[0].get("scope") != expected_scope:
        refuse(f"category=generation_approval_scope_mismatch "
               f"record_scope={matching[0].get('scope')!r} "
               f"expected={expected_scope!r}")
    return {"generation_approved_for": expected_scope}


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


def _finalize(verdicts, *, plan: dict, head: str, generation_calls: int,
              trusted_plan_sha256: str) -> dict:
    planned = sorted(u["unit_sha256"] for u in plan["units"])
    answered = sorted(v["unit_sha256"] for v in verdicts)
    if answered != planned:
        refuse(f"category=verdict_coverage_incomplete planned={len(planned)} "
               f"answered={len(answered)} — a partial review reported as a "
               "review is the reader believing the unanswered units passed")
    if generation_calls != len(planned):
        refuse(f"category=generation_call_count_mismatch "
               f"calls={generation_calls} units={len(planned)}")
    decisions = {v["unit_sha256"]: v["decision"] for v in verdicts}
    return {
        "trusted_plan_sha256": trusted_plan_sha256,
        "candidate_head_sha": head,
        "verdict_count": len(verdicts),
        "generation_calls": generation_calls,
        "decisions": dict(sorted(decisions.items())),
        "all_approved": all(d == "approve" for d in decisions.values()),
        "abstained": sorted(u for u, d in decisions.items() if d == "abstain"),
        "verdicts": sorted(verdicts, key=lambda v: v["unit_sha256"]),
        "honest_scope": ("every planned unit was asked exactly once and "
                         "answered exactly once. An abstain is counted as not "
                         "approved, because a reviewer that declined to decide "
                         "has not approved anything"),
    }


def _short_reason(refusal: LaneRefusal) -> str:
    reason = getattr(refusal, "reason", "") or "refused"
    category = reason.split(" ", 1)[0]
    return f"trusted review refused: {category}"[:statuspublish.MAX_DESCRIPTION]
