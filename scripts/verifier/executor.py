"""Stage 3: the mock review executor (MC4 PASS C §6/§38).

Stage 2 decided WHAT to ask and proved it could be asked without leaking a
secret or blowing a budget. Stage 3 would ask it — send each batch to a model,
collect a verdict per unit, and decide whether the change is approved. Here it
does that against a MOCK generation transport, so the whole review flow is
exercised and adversarially tested without a real model call.

The trust boundary is the same as everywhere else. This module runs
candidate-side, so its evidence is MOCK_TEST_EVIDENCE and its plans are never
executable. A real generation transport, a trusted plan, and trusted evidence
all belong to the write-separated lane; this module defines the wire schema
and the strict validation the trusted lane would reuse, but authenticates
nothing.

The integrity gates are the legacy panel's, restored and made exact:

  * every unit gets exactly one verdict, validated post-parse against the
    review policy (verdicts.py) — a canned "all green" fails the challenge;
  * the REQUIRED APPROVER (gpt-5.6-sol) must approve every unit; a refutation
    of any confidence is a veto;
  * at least `minimum_other_approvers` DISTINCT other models must approve
    each unit; a repeated model identity never counts twice;
  * cross-unit synthesis may ADD findings but can never clear or downgrade a
    refutation — a unit any panelist refuted stays refuted.
"""

from __future__ import annotations

from . import counting, evidence, finalize, verdicts
from .canon import canonical_json, digest
from .errors import (
    INSUFFICIENT_CORROBORATION,
    PROVIDER_RESPONSE_INVALID,
    REQUIRED_APPROVER_MISSING,
    REQUIRED_APPROVER_REFUTED,
    SYNTHESIS_REFUTED,
    BlockingError,
)


class MockGenerationTransport:
    """A DETERMINISTIC local stand-in for /v1/responses.

    It returns a well-formed, policy-valid verdict for every unit, echoing
    the request's challenge, so the executor's happy path can be exercised.
    It is not a model and its verdicts are not review evidence — every plan
    built on it stays MOCK_TEST_EVIDENCE and non-executable.

    `refute` lets a test make a specific model refute specific units, so the
    veto and corroboration gates can be driven."""

    source = counting.SOURCE_MOCK

    def __init__(self, refute: dict[str, set[str]] | None = None,
                 usage_input_tokens: int | None = None):
        # model_id -> set of unit hashes that model refutes
        self.refute = refute or {}
        self.usage_input_tokens = usage_input_tokens
        self.calls = 0
        self.last_timeout: int | None = None

    def post(self, path, body, *, timeout=None):
        import json
        self.calls += 1
        self.last_timeout = timeout
        payload = json.loads(body)
        model_id = payload["model"]
        challenge = _challenge_of(payload)
        unit_hashes = _unit_hashes_of(payload)
        refuted = self.refute.get(model_id, set())
        verdicts_by_unit = {
            unit_hash: {
                "refuted": unit_hash in refuted,
                "confidence": "high",
                "reason": f"{challenge} mock review of unit "
                          f"{unit_hash[:8]}: no behaviour change observed",
                "proof_of_check": f"{challenge} inspected every changed line",
                "checked_categories": ["logic", "state"],
            }
            for unit_hash in unit_hashes
        }
        usage = self.usage_input_tokens
        if usage is None:
            usage = _counted_input_tokens(payload)
        response = {
            "object": "response",
            "model": model_id,
            "usage": {"input_tokens": usage, "output_tokens": 128},
            "output_parsed": {"challenge": challenge,
                              "verdicts_by_unit": verdicts_by_unit},
        }
        return 200, json.dumps(response).encode()


def _challenge_of(payload: dict) -> str:
    schema = payload["text"]["format"]["schema"]
    return schema["properties"]["challenge"]["const"]


def _unit_hashes_of(payload: dict) -> list[str]:
    schema = payload["text"]["format"]["schema"]
    return list(schema["properties"]["verdicts_by_unit"]["required"])


def _counted_input_tokens(payload: dict) -> int:
    # The mock's own arithmetic, matching MockCountTransport, so a happy-path
    # execution agrees with its count under any drift tolerance.
    body = canonical_json({k: payload[k] for k in payload
                           if k != "max_output_tokens"})
    return -(-len(body) // 4)


REQUIRED_KEYS = ("object", "model", "usage", "output_parsed")


def _validate_response_envelope(status, body, *, model_id: str):
    if status != 200:
        raise BlockingError(PROVIDER_RESPONSE_INVALID,
                            f"category=generation_status status={status}")
    parsed = verdicts.parse_strict(body)     # rejects duplicate keys
    if not isinstance(parsed, dict) or set(parsed) - set(REQUIRED_KEYS):
        raise BlockingError(PROVIDER_RESPONSE_INVALID,
                            "category=generation_envelope_unexpected")
    if parsed.get("model") != model_id:
        raise BlockingError(
            PROVIDER_RESPONSE_INVALID,
            f"category=generation_model_mismatch "
            f"expected={model_id} got={parsed.get('model')}")
    usage = parsed.get("usage")
    if not isinstance(usage, dict) or not isinstance(
            usage.get("input_tokens"), int):
        raise BlockingError(PROVIDER_RESPONSE_INVALID,
                            "category=generation_usage_invalid")
    return parsed


def execute_batch(batch: dict, review_policy: dict, *, transport,
                  pin_values: dict, challenge: str,
                  counted_by_model: dict) -> dict:
    """Run one batch through the panel and decide each unit.

    Returns a per-unit decision record. Blocks on any integrity failure."""
    import json

    unit_hashes = list(batch["unit_sha256_in_order"])
    model_ids = list(review_policy["model_ids"])
    approver = review_policy["required_approver"]
    min_others = review_policy["minimum_other_approvers"]

    # model_id -> {unit_hash -> verdict}
    by_model: dict[str, dict] = {}
    for model_id in model_ids:
        request_body = _execution_body(batch, model_id, review_policy,
                                       challenge, unit_hashes)
        status, body = transport.post("/v1/responses",
                                      json.dumps(request_body).encode(),
                                      timeout=pin_values[
                                          "VERIFIER_GENERATION_TIMEOUT_SECONDS"])
        parsed = _validate_response_envelope(status, body, model_id=model_id)
        model_verdicts = verdicts.validate_verdicts(
            parsed["output_parsed"], unit_hashes=unit_hashes,
            challenge=challenge, review_policy=review_policy)
        # A2-F05/§38: usage drift is checked against the counted input.
        counted = counted_by_model.get(model_id)
        if counted is not None:
            finalize.counting2.assert_usage_within_tolerance(
                counted=counted,
                reported=parsed["usage"]["input_tokens"],
                tolerance=pin_values["VERIFIER_TOKEN_DRIFT_TOLERANCE"],
                where=f"{batch['batch_id']}:{model_id}")
        by_model[model_id] = model_verdicts

    decisions = {}
    for unit_hash in unit_hashes:
        decisions[unit_hash] = _decide_unit(
            unit_hash, by_model, approver=approver, model_ids=model_ids,
            min_others=min_others)
    return {
        "batch_id": batch["batch_id"],
        "unit_decisions": decisions,
        "per_model_refuted": {m: sorted(h for h, v in by_model[m].items()
                                        if v["refuted"])
                              for m in model_ids},
    }


def _decide_unit(unit_hash: str, by_model: dict, *, approver: str,
                 model_ids: list[str], min_others: int) -> dict:
    """Apply the role gates to one unit. Fail-closed."""
    if approver not in by_model:
        raise BlockingError(
            REQUIRED_APPROVER_MISSING,
            f"category=required_approver_absent approver={approver} "
            f"unit={unit_hash[:16]}")
    approver_verdict = by_model[approver][unit_hash]
    if approver_verdict["refuted"]:
        # A refutation of ANY confidence is a veto.
        raise BlockingError(
            REQUIRED_APPROVER_REFUTED,
            f"category=required_approver_veto approver={approver} "
            f"unit={unit_hash[:16]} confidence={approver_verdict['confidence']}")

    # DISTINCT other models that approve.
    others_approving = {m for m in model_ids
                        if m != approver and not by_model[m][unit_hash][
                            "refuted"]}
    if len(others_approving) < min_others:
        raise BlockingError(
            INSUFFICIENT_CORROBORATION,
            f"category=insufficient_corroboration unit={unit_hash[:16]} "
            f"distinct_other_approvers={len(others_approving)} "
            f"required={min_others}")

    # Any refutation at all means the unit is refuted (recorded, and the
    # decision is a block only for the required approver; corroborators'
    # refutations are surfaced but do not by themselves veto — they reduce
    # the corroboration count checked above).
    any_refuted = [m for m in model_ids if by_model[m][unit_hash]["refuted"]]
    return {
        "unit_sha256": unit_hash,
        "approved": not any_refuted,
        "approver_confidence": approver_verdict["confidence"],
        "refuted_by": sorted(any_refuted),
        "distinct_other_approvers": len(others_approving),
    }


def synthesize(batch_results: list[dict]) -> dict:
    """Cross-unit synthesis that CANNOT clear a refutation (§38).

    Synthesis may add findings across units, but a unit any panelist refuted
    stays refuted — the synthesis output is intersected with the per-unit
    decisions, never allowed to override them."""
    refuted_units = sorted({
        unit_hash
        for result in batch_results
        for unit_hash, decision in result["unit_decisions"].items()
        if not decision["approved"]
    })
    approved_units = sorted({
        unit_hash
        for result in batch_results
        for unit_hash, decision in result["unit_decisions"].items()
        if decision["approved"]
    })
    # A defensive re-check: if any code path tried to move a refuted unit into
    # the approved set, that is a synthesis override and blocks.
    overlap = set(refuted_units) & set(approved_units)
    if overlap:
        raise BlockingError(
            SYNTHESIS_REFUTED,
            f"category=synthesis_override_attempt units={len(overlap)} — "
            "synthesis may add findings but can never clear a refutation")
    record = {
        "approved_unit_count": len(approved_units),
        "refuted_unit_count": len(refuted_units),
        "refuted_unit_sha256": refuted_units,
        "overall_approved": not refuted_units,
        "synthesis_policy": "additive-only; a refutation is final",
    }
    record["synthesis_sha256"] = digest(b"synthesis-v1",
                                        canonical_json(record))
    return record


def _execution_body(batch: dict, model_id: str, review_policy: dict,
                    challenge: str, unit_hashes: list[str]) -> dict:
    """The exact /v1/responses body a batch would send for one model.

    Rebuilt from the batch's recorded request hashes' inputs is not possible
    here (the batch stores hashes, not payloads), so the mock executor builds
    a minimal, schema-bearing body carrying the challenge and unit set. A
    trusted executor would reconstruct the exact stored payload; this is the
    candidate mock path."""
    from . import providerreq
    schema = providerreq.verdict_schema(unit_hashes, challenge=challenge)
    return {
        "model": model_id,
        "instructions": "mock execution",
        "input": f"batch {batch['batch_id']} challenge {challenge}",
        "text": {"format": schema},
        "truncation": "disabled",
        "max_output_tokens": review_policy["max_output_tokens"],
    }


def execute_mock(plan_record: dict, *, transport, challenge: str) -> dict:
    """Run a whole mock-finalization report through the mock panel.

    Produces a mock-execution-report: MOCK_TEST_EVIDENCE, non-executable,
    zero real generation calls."""
    review_policy = plan_record["review_request_policy"]
    pin_values = plan_record["operator_pin_record"]["pins"]

    batch_results = []
    generation_calls = 0
    for batch in plan_record["batches"]:
        counted_by_model = dict(batch["input_tokens_by_model"])
        result = execute_batch(batch, review_policy, transport=transport,
                               pin_values=pin_values, challenge=challenge,
                               counted_by_model=counted_by_model)
        generation_calls += len(review_policy["model_ids"])
        batch_results.append(result)

    synthesis = synthesize(batch_results)
    execution_evidence = evidence.candidate_evidence_record(
        evidence.MOCK_TEST_EVIDENCE, counts=[],
        logical_request_count=generation_calls,
        provider_attempt_count=generation_calls,
        endpoint="/v1/responses",
        billing_state=counting.COUNT_BILLING_STATE)

    report = {
        "schema_version": 1,
        "artifact": "mock-execution-report",
        "publication_class": "private",
        "review_skeleton_sha256": plan_record["review_skeleton_sha256"],
        "executable_plan_sha256": plan_record["executable_plan_sha256"],
        "challenge": challenge,
        "batch_results": batch_results,
        "synthesis": synthesis,
        "execution_evidence": execution_evidence,
        "generation_attempts_performed": generation_calls,
        "executable_authority": False,
        "evidence_class": evidence.MOCK_TEST_EVIDENCE,
    }
    report["mock_execution_report_sha256"] = digest(
        b"mock-execution-report-v1", canonical_json(report))
    return report
