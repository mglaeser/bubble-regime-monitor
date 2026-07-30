"""Stage 3: the mock review executor (MC4 PASS C §6/§38, healed by C4-F05..F13).

Stage 2 decided WHAT to ask and proved it could be asked without leaking a
secret or blowing a budget. Stage 3 would ask it. Here it does that against a
MOCK generation transport, so the whole review flow is exercised and
adversarially tested without a real model call.

The first version of this module read the report and started sending. Six
separate things were wrong with that, and each is now a precondition:

  * it never strict-loaded or rebuilt the plan (C4-F05), so it would execute
    a report that no longer described its own commits;
  * it rebuilt requests and never compared their hashes to the ones that were
    COUNTED (C4-F06) — usage-token drift is an arithmetic check, not a
    statement that the same review question was asked;
  * it took the challenge as a caller argument (C4-F13), so the caller, not
    the plan, decided what the reviewer was answering;
  * it never preflighted the execution payloads (C4-F07) despite a comment
    saying it did, and it sent one request at a time, so a secret in the last
    batch blocked only after every earlier batch had gone out;
  * it accepted any transport object (C4-F08), so candidate-side code could
    transmit repository content and merely label the result untrusted;
  * its ledger incremented logical requests and attempts together, so
    `retries` was arithmetically always zero (C4-F09).

The trust boundary is unchanged. This module runs candidate-side, so its
evidence is MOCK_TEST_EVIDENCE and its plans are never executable. A real
generation transport, a trusted plan and trusted evidence all belong to the
write-separated lane; this module defines the wire schema and the strict
validation that lane would reuse, and authenticates nothing.
"""

from __future__ import annotations

import json

from . import (
    counting,
    counting2,
    evidence,
    finalize,
    origin,
    preflight,
    providerreq,
    reviewpolicy,
    unitpayload,
    verdicts,
)
from .canon import canonical_json, digest, sha256_hex
from .errors import (
    CHUNK_COUNT_EXHAUSTED,
    EXECUTABLE_PLAN_INVALID,
    INSUFFICIENT_CORROBORATION,
    PROVIDER_RESPONSE_INVALID,
    REQUIRED_APPROVER_MISSING,
    REQUIRED_APPROVER_REFUTED,
    SECRET_PREFLIGHT_FAILED,
    SYNTHESIS_REFUTED,
    BlockingError,
)

GENERATION_PATH = "/v1/responses"
MAX_GENERATION_RESPONSE_BYTES = 1024 * 1024

#: The internal envelope this code validates. A trusted transport normalizes
#: the official provider response into exactly this shape and records the
#: adapter it used; candidate code never assumes raw provider HTTP returns it
#: verbatim (C4-F21).
RESPONSE_ENVELOPE_VERSION = "internal-generation-envelope-v1"
ENVELOPE_KEYS = ("object", "model", "usage", "output_parsed")
USAGE_KEYS = ("input_tokens", "output_tokens")
EXPECTED_OBJECT = "response"

#: Wording the mock uses per lens. Deliberately NOT derived from the model id:
#: the normalizer strips model and lens identifiers, so a mock that
#: distinguished itself that way would look canned — which was the point of
#: MC4-R09. These read like three reviewers describing three different things.
_MOCK_PHRASING = {
    "gpt-5.3-codex": {
        "reason": "the changed branch preserves its precondition",
        "proof": "walked each altered statement and its error path",
    },
    "gpt-5.6-sol": {
        "reason": "no untrusted value reaches a sensitive sink here",
        "proof": "traced every write and boundary crossing",
    },
    "gpt-4.1-mini": {
        "reason": "the denominator and fail-closed default are unchanged",
        "proof": "recomputed the coverage arithmetic against the guard",
    },
}
_MOCK_CANNED_REASON = "looks fine, no issues found in this unit"
_MOCK_CANNED_PROOF = "read the diff"


# ------------------------------------------------------------- transport ----


class MockGenerationTransport:
    """A DETERMINISTIC local stand-in for /v1/responses.

    It returns a well-formed, policy-valid verdict for every unit, echoing
    the request's challenge, so the executor's happy path can be exercised.
    It is not a model and its verdicts are not review evidence — every plan
    built on it stays MOCK_TEST_EVIDENCE and non-executable.

    `refute` lets a test make a specific model refute specific units, so the
    veto and corroboration gates can be driven. Reasons are model-specific by
    construction, because the anti-canned gate requires corroborating
    approvals to say different things."""

    source = counting.SOURCE_MOCK
    generation_transport_class = "MOCK_NOT_PROVIDER"

    def __init__(self, refute: dict[str, set[str]] | None = None,
                 usage_input_tokens: int | None = None,
                 identical_reasons: bool = False):
        # model_id -> set of unit hashes that model refutes
        self.refute = refute or {}
        self.usage_input_tokens = usage_input_tokens
        self.identical_reasons = identical_reasons
        self.calls = 0
        self.last_timeout: int | None = None

    def configuration(self) -> dict:
        """The exact behaviour this stand-in was configured with (MC4-R15).

        Reconstruction re-runs the executor against a DEFAULT mock. Without
        this in the report, a result produced by a differently configured
        stand-in could be "reproduced" by a run that behaved differently —
        the digests would match because the report never said what the mock
        had been told to do."""
        record = {
            "transport_class": type(self).__name__,
            "source": self.source,
            "generation_transport_class": self.generation_transport_class,
            "refute": {model: sorted(units)
                       for model, units in sorted(self.refute.items())},
            "usage_input_tokens": self.usage_input_tokens,
            "identical_reasons": self.identical_reasons,
        }
        record["mock_transport_configuration_sha256"] = digest(
            b"mock-generation-transport-config-v1", canonical_json(record))
        return record

    def post(self, path, body, *, timeout=None):
        self.calls += 1
        self.last_timeout = timeout
        payload = json.loads(body)
        model_id = payload["model"]
        challenge = _challenge_of(payload)
        unit_hashes = _unit_hashes_of(payload)
        refuted = self.refute.get(model_id, set())
        # MC4-R08: model-specific categories from the model's own lens
        # vocabulary. Returning ["logic","state"] for every model was what
        # made the lens claim unfalsifiable from the evidence.
        categories = list(reviewpolicy.lens_required_group(model_id))[:2]
        # MC4-R09: distinctness must NOT come from interpolating the model id,
        # which the normalizer now strips. Each lens gets its own wording.
        phrasing = _MOCK_PHRASING[model_id]
        verdicts_by_unit = {
            unit_hash: {
                "refuted": unit_hash in refuted,
                "confidence": "high",
                "reason": (f"{challenge} {_MOCK_CANNED_REASON}"
                           if self.identical_reasons
                           else f"{challenge} {phrasing['reason']} for unit "
                                f"{unit_hash[:8]}"),
                "proof_of_check": (f"{challenge} {_MOCK_CANNED_PROOF}"
                                   if self.identical_reasons
                                   else f"{challenge} {phrasing['proof']}"),
                "checked_categories": categories,
            }
            for unit_hash in unit_hashes
        }
        usage = self.usage_input_tokens
        if usage is None:
            usage = _counted_input_tokens(payload)
        response = {
            "object": EXPECTED_OBJECT,
            "model": model_id,
            "usage": {"input_tokens": usage, "output_tokens": 128},
            "output_parsed": {"lens_id": reviewpolicy.lens_id(model_id),
                              "challenge": challenge,
                              "verdicts_by_unit": verdicts_by_unit},
        }
        return 200, json.dumps(response).encode()


def assert_mock_generation_transport(transport) -> None:
    """Candidate execution accepts ONLY the mock transport (C4-F08).

    Evidence authority and TRANSMISSION authority are different questions.
    A transport whose evidence would be untrusted can still open a socket and
    send repository content; labelling the result MOCK afterwards does not
    un-send it. Checked before any request is reconstructed, so a refused
    transport never sees a byte."""
    source = getattr(transport, "source", None)
    if source != counting.SOURCE_MOCK:
        raise BlockingError(
            SECRET_PREFLIGHT_FAILED,
            f"category=candidate_execution_requires_mock_transport "
            f"source={source!r} — candidate-side execution is local-only; a "
            "real generation transport belongs to the trusted lane and is "
            "refused here before any request is built")
    if getattr(transport, "generation_transport_class",
               None) != "MOCK_NOT_PROVIDER":
        raise BlockingError(
            SECRET_PREFLIGHT_FAILED,
            "category=generation_transport_class_undeclared — a transport "
            "must declare what it is; silence is never read as mock")
    for forbidden in ("connect", "sendall", "getaddrinfo", "urlopen",
                      "request", "session"):
        if callable(getattr(transport, forbidden, None)):
            raise BlockingError(
                SECRET_PREFLIGHT_FAILED,
                f"category=candidate_transport_has_network_method "
                f"method={forbidden}")
    counting.assert_timeout_protocol(transport)


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


# -------------------------------------------------- response validation -----


def _plain_nonneg_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_response_envelope(status, body, *, model_id: str) -> dict:
    """The strict internal envelope (C4-F10).

    Exact key set — not merely "no unknown keys", which accepted a response
    missing `usage` entirely. Exact object tag. Exact requested model. Usage
    as plain non-negative integers, with bool refused because `True` is an
    `int` in Python and would sail through an isinstance check. A bounded
    body, and duplicate JSON keys rejected."""
    if not isinstance(status, int) or isinstance(status, bool):
        raise BlockingError(PROVIDER_RESPONSE_INVALID,
                            "category=generation_status_not_integer")
    if status != 200:
        raise BlockingError(PROVIDER_RESPONSE_INVALID,
                            f"category=generation_status status={status}")
    if not isinstance(body, (bytes, bytearray)):
        raise BlockingError(PROVIDER_RESPONSE_INVALID,
                            "category=generation_body_not_bytes")
    if len(body) > MAX_GENERATION_RESPONSE_BYTES:
        raise BlockingError(
            PROVIDER_RESPONSE_INVALID,
            f"category=generation_body_oversized bytes={len(body)}")
    parsed = verdicts.parse_strict(body)     # rejects duplicate keys
    if not isinstance(parsed, dict):
        raise BlockingError(PROVIDER_RESPONSE_INVALID,
                            "category=generation_envelope_not_object")
    if set(parsed) != set(ENVELOPE_KEYS):
        raise BlockingError(
            PROVIDER_RESPONSE_INVALID,
            f"category=generation_envelope_key_set "
            f"missing={sorted(set(ENVELOPE_KEYS) - set(parsed))} "
            f"unexpected={len(set(parsed) - set(ENVELOPE_KEYS))}")
    if parsed["object"] != EXPECTED_OBJECT:
        raise BlockingError(PROVIDER_RESPONSE_INVALID,
                            "category=generation_object_tag_unexpected")
    if parsed["model"] != model_id:
        raise BlockingError(
            PROVIDER_RESPONSE_INVALID,
            f"category=generation_model_mismatch expected={model_id}")
    usage = parsed["usage"]
    if not isinstance(usage, dict) or set(usage) != set(USAGE_KEYS):
        raise BlockingError(PROVIDER_RESPONSE_INVALID,
                            "category=generation_usage_key_set")
    for key in USAGE_KEYS:
        if not _plain_nonneg_int(usage[key]):
            raise BlockingError(
                PROVIDER_RESPONSE_INVALID,
                f"category=generation_usage_not_plain_nonneg_int field={key}")
    return parsed


# ------------------------------------------------------------- ledger -------


class GenerationLedger:
    """Attempts, retries and usage for Stage 3 (C4-F09).

    The earlier version incremented `logical_requests` and `attempts` in the
    same call, so `retries = attempts - logical_requests` was zero by
    construction and the retry PIN was validated but never implemented. Here
    a logical request is opened once and each attempt is counted immediately
    before the transport call, so a request that failed twice and succeeded
    on the third reports one logical request and three attempts."""

    def __init__(self, pin_values: dict):
        self.pins = pin_values
        self.logical_requests = 0
        self.attempts = 0
        self.records: list[dict] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self._open_index: int | None = None

    @property
    def max_calls(self) -> int:
        return self.pins["VERIFIER_MAX_GENERATION_CALLS"]

    @property
    def max_retries(self) -> int:
        return self.pins["VERIFIER_GENERATION_MAX_RETRIES"]

    @property
    def _attempts_per_request(self) -> int:
        return 1 + self.max_retries

    def open_request(self, model_id: str, batch_id: str) -> None:
        """Reserve the whole retry budget before the first attempt.

        Same rule as the count ledger: a retry must never be the thing that
        pushes a run past the operator's cap, so a request that cannot afford
        its own retries is refused before it starts."""
        worst_case = self.attempts + self._attempts_per_request
        if worst_case > self.max_calls:
            raise BlockingError(
                CHUNK_COUNT_EXHAUSTED,
                f"category=generation_attempt_cap_would_be_exceeded "
                f"model={model_id} batch={batch_id} "
                f"attempts_so_far={self.attempts} worst_case={worst_case} "
                f"cap={self.max_calls}")
        self.logical_requests += 1
        self._open_index = len(self.records)

    def attempt(self, model_id: str, batch_id: str) -> int:
        if self.attempts >= self.max_calls:
            raise BlockingError(
                CHUNK_COUNT_EXHAUSTED,
                f"category=generation_attempt_cap_reached "
                f"attempts={self.attempts} cap={self.max_calls}")
        self.attempts += 1
        self.records.append({"model_id": model_id, "batch_id": batch_id,
                             "attempt_number": self.attempts,
                             "result_category": "pending"})
        return self.attempts

    def settle(self, category: str, *, input_tokens: int = 0,
               output_tokens: int = 0) -> None:
        if self.records:
            self.records[-1]["result_category"] = category
            self.records[-1]["input_tokens"] = input_tokens
            self.records[-1]["output_tokens"] = output_tokens
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def settle_failed(self, category: str) -> None:
        """Every attempt of this request that is still pending failed."""
        if self._open_index is None:
            return
        for record in self.records[self._open_index:]:
            if record["result_category"] == "pending":
                record["result_category"] = category

    def record(self) -> dict:
        record = {
            "logical_generation_requests": self.logical_requests,
            "generation_attempts": self.attempts,
            "retries": max(0, self.attempts - self.logical_requests),
            "max_generation_calls_cap": self.max_calls,
            "max_retries_per_request": self.max_retries,
            "attempts_per_request_worst_case": self._attempts_per_request,
            "timeout_seconds": self.pins["VERIFIER_GENERATION_TIMEOUT_SECONDS"],
            "total_input_tokens": self.input_tokens,
            "total_output_tokens": self.output_tokens,
            "attempts": self.records,
            "failed_attempt_count": sum(
                1 for a in self.records
                if str(a["result_category"]).startswith("failed")),
            "honest_scope": "mock arithmetic from a local transport; not "
                            "provider usage and not a spend estimate",
        }
        record["generation_ledger_sha256"] = digest(
            b"generation-ledger-v2", canonical_json(record))
        return record


_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def _post_with_retries(transport, body: bytes, *, model_id: str,
                       batch_id: str, ledger: GenerationLedger,
                       timeout: int):
    """One logical generation request, with the operator's retry policy.

    A deterministic failure is never retried: asking the same question again
    gets the same answer and spends another call to learn it."""
    ledger.open_request(model_id, batch_id)
    last: BlockingError | None = None
    for _ in range(ledger._attempts_per_request):
        ledger.attempt(model_id, batch_id)
        try:
            status, payload = transport.post(GENERATION_PATH, body,
                                             timeout=timeout)
        except Exception as exc:
            last = BlockingError(
                PROVIDER_RESPONSE_INVALID,
                f"category=generation_transport_exception "
                f"exception_class={type(exc).__name__}")
            ledger.settle("failed:transport_exception")
            continue
        if isinstance(status, int) and not isinstance(status, bool) and (
                status in _RETRYABLE_STATUSES):
            last = BlockingError(
                PROVIDER_RESPONSE_INVALID,
                f"category=generation_retryable_status status={status}")
            ledger.settle(f"failed:status_{status}")
            continue
        try:
            parsed = validate_response_envelope(status, payload,
                                                model_id=model_id)
        except BlockingError as exc:
            ledger.settle_failed(f"failed:{exc.code}")
            raise
        ledger.settle("ok",
                      input_tokens=parsed["usage"]["input_tokens"],
                      output_tokens=parsed["usage"]["output_tokens"])
        return parsed
    ledger.settle_failed("failed:retry_exhausted")
    raise BlockingError(
        PROVIDER_RESPONSE_INVALID,
        f"category=generation_retry_exhausted model={model_id} "
        f"batch={batch_id} attempts={ledger._attempts_per_request} "
        f"last={last.message if last else 'none'}")


# ------------------------------------------------- request reconstruction ---


def rebuild_payloads(skeleton: dict, plan_record: dict, *, cwd) -> dict:
    """Rebuild each final unit's EXACT structured payload from the commits.

    The report stores request HASHES, not bodies — deliberately, so it carries
    no source content. The executor therefore earns the payloads again the
    same way the finalizer did, which also re-proves that every unit's atoms
    still exist in the range the report claims."""
    atom_map = finalize.atom_texts(skeleton, cwd=cwd)
    atom_records = unitpayload.index_atom_records(skeleton)
    return {
        unit["unit_sha256"]: unitpayload.structured_unit(
            unit, atom_records, atom_map)
        for unit in plan_record["final_units"]
    }


def _path_identities(skeleton: dict, units) -> dict:
    atom_records = unitpayload.index_atom_records(skeleton)
    mapping = {}
    for unit in units:
        for atom_id in unit["atom_ids"]:
            record = atom_records.get(atom_id)
            if record and record.get("path_bytes_b64"):
                mapping[unit["unit_sha256"]] = record["path_bytes_b64"]
                break
    return mapping


def reconstruct_batch_requests(plan_record: dict, batch: dict, *,
                               payloads_by_unit: dict,
                               path_bytes_b64_by_unit: dict) -> dict:
    """model_id -> RequestAssembly, rebuilt exactly as the plan describes."""
    review_policy = plan_record["review_request_policy"]
    pin_values = plan_record["operator_pin_record"]["pins"]
    challenge = plan_record["execution_challenge"]
    payloads = [payloads_by_unit[h] for h in batch["unit_sha256_in_order"]]
    return {
        model_id: finalize._request_for(model_id, payloads, pin_values,
                                        review_policy, challenge,
                                        path_bytes_b64_by_unit)
        for model_id in review_policy["model_ids"]
    }


def assert_request_matches_plan(assembly, batch: dict, model_id: str) -> None:
    """The executed request must BE the counted request (C4-F06).

    Usage-token drift is arithmetic: two different review questions can cost
    the same number of tokens. Only hash equality says the reviewer is being
    asked what the plan priced and preflighted."""
    stored = (batch.get("request_hashes_by_model") or {}).get(model_id)
    if not stored:
        raise BlockingError(
            EXECUTABLE_PLAN_INVALID,
            f"category=batch_has_no_request_hashes batch={batch['batch_id']} "
            f"model={model_id} — nothing binds the executed request to the "
            "counted one")
    rebuilt = assembly.hashes()
    differing = sorted(k for k, v in stored.items() if rebuilt.get(k) != v)
    if differing:
        raise BlockingError(
            EXECUTABLE_PLAN_INVALID,
            f"category=execution_request_hash_mismatch "
            f"batch={batch['batch_id']} model={model_id} "
            f"differing={differing} — the request rebuilt for execution is "
            "not the request that was counted, preflighted and costed")


# ------------------------------------------------- execution preflight ------


class ExecutionPreflightManifest:
    """Every execution payload, scanned before ANY generation call (C4-F07).

    The comment in the earlier version said the payload was preflighted
    immediately before transmission. Nothing did. This restores the same
    discipline the count generation already had, and for the same reason: a
    secret in the last batch must block the first batch's call, not be
    discovered after it."""

    def __init__(self, skeleton: dict, plan_record: dict, *, cwd,
                 authorizations=None):
        self.manifest = finalize.PreflightGenerationManifest(
            authorizations,
            atom_records=unitpayload.index_atom_records(skeleton),
            atom_map=finalize.atom_texts(skeleton, cwd=cwd))
        for unit in plan_record["final_units"]:
            self.manifest._units_by_hash[unit["unit_sha256"]] = unit
        self.entries: list[dict] = []
        self.sealed = False

    def scan_all(self, assemblies_by_batch: dict, ledger: GenerationLedger
                 ) -> None:
        for batch_id, by_model in sorted(assemblies_by_batch.items()):
            for model_id, assembly in sorted(by_model.items()):
                entry = self.manifest._scan_payload(
                    assembly, payload_kind="execution",
                    label=f"exec:{batch_id}:{model_id}",
                    unit_count=len(assembly.unit_sha256_in_order))
                self.entries.append({
                    "batch_id": batch_id, "model_id": model_id,
                    **assembly.hashes(), "execution_payload_scan": entry,
                })
        if ledger.attempts != 0:
            raise BlockingError(
                SECRET_PREFLIGHT_FAILED,
                f"category=generation_attempt_during_preflight "
                f"attempts={ledger.attempts}")
        self.sealed = True

    def record(self) -> dict:
        record = {
            "schema_version": 1,
            "scanned_execution_payload_count": len(self.entries),
            "generation_attempts_at_seal": 0,
            "entries": self.entries,
        }
        record["execution_preflight_sha256"] = digest(
            b"execution-preflight-manifest-v1", canonical_json(record))
        return record


# ------------------------------------------------------------- panel --------


def execute_batch(batch: dict, review_policy: dict, *, transport,
                  pin_values: dict, challenge: str,
                  counted_by_model: dict,
                  assemblies: dict,
                  ledger: GenerationLedger) -> dict:
    """Run one batch through the panel and decide each unit.

    Every request here has already been hash-checked against the plan and
    preflighted as part of the whole generation."""
    unit_hashes = list(batch["unit_sha256_in_order"])
    model_ids = list(review_policy["model_ids"])
    approver = review_policy["required_approver"]
    min_others = review_policy["minimum_other_approvers"]

    by_model: dict[str, dict] = {}
    evidence_records: list[dict] = []
    for model_id in model_ids:
        assembly = assemblies[model_id]
        parsed = _post_with_retries(
            transport, assembly.execution_payload_bytes, model_id=model_id,
            batch_id=batch["batch_id"], ledger=ledger,
            timeout=pin_values["VERIFIER_GENERATION_TIMEOUT_SECONDS"])
        model_verdicts = verdicts.validate_verdicts(
            parsed["output_parsed"], unit_hashes=unit_hashes,
            challenge=challenge, review_policy=review_policy,
            model_id=model_id)
        counted = counted_by_model.get(model_id)
        if counted is not None:
            counting2.assert_usage_within_tolerance(
                counted=counted,
                reported=parsed["usage"]["input_tokens"],
                tolerance=pin_values["VERIFIER_TOKEN_DRIFT_TOLERANCE"],
                where=f"{batch['batch_id']}:{model_id}")
        by_model[model_id] = model_verdicts
        evidence_records.append(verdicts.verdict_evidence(
            model_verdicts, model_id=model_id, batch_id=batch["batch_id"],
            challenge=challenge,
            request_semantics_sha256=assembly.request_semantics_sha256(),
            usage=dict(parsed["usage"]),
            attempt=dict(ledger.records[-1]) if ledger.records else {}))

    # MC4-R07: decide every unit and COLLECT the blocks. Raising on the first
    # required-approver refutation lost the finding: the exception escaped
    # before this function returned its validated verdict evidence and before
    # `execute_mock` built a report, so the most load-bearing result the panel
    # can produce — Sol saying no, and why — left no durable artifact.
    decisions = {}
    blocks: list[dict] = []
    for unit_hash in unit_hashes:
        decision, block = decide_unit_or_block(
            unit_hash, by_model, approver=approver, model_ids=model_ids,
            min_others=min_others, challenge=challenge,
            review_policy=review_policy)
        decisions[unit_hash] = decision
        if block is not None:
            blocks.append(block)
    return {
        "batch_id": batch["batch_id"],
        "unit_decisions": decisions,
        "unit_blocks": blocks,
        "per_model_verdict_evidence": evidence_records,
        "per_model_refuted": {m: sorted(h for h, v in by_model[m].items()
                                        if v["refuted"])
                              for m in model_ids},
    }


def decide_unit_or_block(unit_hash: str, by_model: dict, *, approver: str,
                         model_ids: list[str], min_others: int,
                         challenge: str | None = None,
                         review_policy: dict | None = None):
    """(decision, block_or_None). Never raises for a VALIDATED verdict.

    A malformed response still aborts — there is nothing to retain. But a
    well-formed refutation is a finding, and a finding must survive as
    evidence before it stops the process."""
    try:
        return _decide_unit(unit_hash, by_model, approver=approver,
                            model_ids=model_ids, min_others=min_others,
                            challenge=challenge,
                            review_policy=review_policy), None
    except BlockingError as exc:
        approver_verdict = (by_model.get(approver) or {}).get(unit_hash) or {}
        any_refuted = sorted(m for m in model_ids
                             if (by_model.get(m) or {}).get(unit_hash, {})
                             .get("refuted"))
        decision = {
            "unit_sha256": unit_hash,
            "approved": False,
            "approver_confidence": approver_verdict.get("confidence"),
            "refuted_by": any_refuted,
            "distinct_other_approvers": len(
                [m for m in model_ids
                 if m != approver
                 and not (by_model.get(m) or {}).get(unit_hash, {})
                 .get("refuted", True)]),
            "distinct_reasoning": None,
            "block_code": exc.code,
        }
        return decision, {"unit_sha256": unit_hash, "code": exc.code,
                          "reason": exc.message}


def _decide_unit(unit_hash: str, by_model: dict, *, approver: str,
                 model_ids: list[str], min_others: int,
                 challenge: str | None = None,
                 review_policy: dict | None = None) -> dict:
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

    # C4-F12: corroboration must be independent REASONING, not a repeated
    # sentence. Applied to approvals only.
    distinct = None
    if challenge is not None:
        policy = review_policy or {}
        distinct = verdicts.assert_distinct_reasoning(
            by_model, unit_hash=unit_hash, approver=approver,
            challenge=challenge,
            corroborators=[m for m in model_ids if m != approver],
            model_ids=model_ids,
            lens_ids=list((policy.get("lens_ids") or {}).values()),
            similarity_threshold_bp=policy.get(
                "anti_canned_similarity_threshold_bp",
                verdicts.DEFAULT_SIMILARITY_THRESHOLD_BP))

    any_refuted = [m for m in model_ids if by_model[m][unit_hash]["refuted"]]
    return {
        "unit_sha256": unit_hash,
        "approved": not any_refuted,
        "approver_confidence": approver_verdict["confidence"],
        "refuted_by": sorted(any_refuted),
        "distinct_other_approvers": len(others_approving),
        "distinct_reasoning": distinct,
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


# ------------------------------------------------------ output privacy ------

OUTPUT_PRIVACY_FAILED = "OUTPUT_PRIVACY_FAILED"

#: EVERY provider-controlled text field in a verdict. Derived from the verdict
#: schema rather than listed by hand, so a field added there cannot be
#: forgotten here — which is exactly what happened to `checked_categories`
#: (PASS E, family C): six free-form 48-character strings per unit, validated
#: for length and uniqueness only, copied verbatim into the persisted evidence,
#: and scanned by nothing.
PROVIDER_TEXT_FIELDS = frozenset({"reason", "proof_of_check",
                                  "checked_categories"})


def _provider_text_fields(verdict: dict):
    """(field_name, text) for every provider-written string in a verdict.

    A list field yields one entry per element: scanning the joined list would
    let a secret hide across an element boundary, and scanning `str(list)`
    would scan Python's repr rather than the value."""
    for field in sorted(PROVIDER_TEXT_FIELDS):
        value = verdict.get(field)
        if isinstance(value, str):
            yield field, value
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, str):
                    yield f"{field}[{index}]", item


def assert_output_carries_no_secret(evidence_records: list[dict], *,
                                    path_identities: frozenset) -> dict:
    """Provider-controlled text is scanned before it is persisted (C4-F22).

    A reason or a proof is written by a model that has just been shown
    changed source. It can echo a secret, a path, or the prompt. The full
    text stays PRIVATE, and it is refused outright if it carries a literal or
    a path identity — a secret that leaves in a request and comes back in a
    verdict has still left twice."""
    scanned = 0
    for record in evidence_records:
        for unit_hash, verdict in record["verdicts_by_unit"].items():
            for field, text in _provider_text_fields(verdict):
                scanned += 1
                hits = preflight.distinct_occurrences(
                    preflight.scan_text(text))
                if hits:
                    raise BlockingError(
                        OUTPUT_PRIVACY_FAILED,
                        f"category=secret_in_provider_output "
                        f"model={record['model_id']} unit={unit_hash[:16]} "
                        f"field={field} finding_count={len(hits)}")
                for identity in path_identities:
                    if identity and identity in text:
                        raise BlockingError(
                            OUTPUT_PRIVACY_FAILED,
                            f"category=path_identity_in_provider_output "
                            f"model={record['model_id']} unit={unit_hash[:16]} "
                            f"field={field}")
    return {"scanned_field_count": scanned,
            "scanned_fields": sorted(PROVIDER_TEXT_FIELDS),
            "retention": "full verdict text is PRIVATE; public summaries "
                         "carry digests and counts only"}


# ------------------------------------------------------------ entry point ---


def execute_mock(plan_record: dict, *, transport, skeleton: dict, cwd,
                 operator_pins: dict, authorizations=None) -> dict:
    """Run a whole mock-finalization report through the mock panel.

    Preconditions, in order and all of them blocking: the transport is the
    mock one; the report reconstructs from its own commits; every execution
    request is rebuilt and hash-matched to the counted request; every
    execution payload is scanned as one sealed generation. Only then may a
    single call be made.

    There is no `challenge` parameter. The challenge is the plan's."""
    assert_mock_generation_transport(transport)

    # C4-F05: strict-load and REBUILD before reading anything from the report.
    finalize.validate_mock_finalization_strict(
        plan_record, skeleton=skeleton, cwd=cwd, operator_pins=operator_pins,
        authorizations=authorizations)

    review_policy = plan_record["review_request_policy"]
    pin_values = plan_record["operator_pin_record"]["pins"]
    challenge = plan_record["execution_challenge"]
    ledger = GenerationLedger(pin_values)

    payloads_by_unit = rebuild_payloads(skeleton, plan_record, cwd=cwd)
    paths = _path_identities(skeleton, plan_record["final_units"])

    # C4-F06: rebuild and hash-match EVERY request first.
    assemblies_by_batch: dict[str, dict] = {}
    for batch in plan_record["batches"]:
        assemblies = reconstruct_batch_requests(
            plan_record, batch, payloads_by_unit=payloads_by_unit,
            path_bytes_b64_by_unit=paths)
        for model_id, assembly in assemblies.items():
            assert_request_matches_plan(assembly, batch, model_id)
        assemblies_by_batch[batch["batch_id"]] = assemblies

    # C4-F07: scan them ALL before any call.
    preflight_manifest = ExecutionPreflightManifest(
        skeleton, plan_record, cwd=cwd, authorizations=authorizations)
    preflight_manifest.scan_all(assemblies_by_batch, ledger)

    batch_results = []
    for batch in plan_record["batches"]:
        result = execute_batch(
            batch, review_policy, transport=transport,
            pin_values=pin_values, challenge=challenge,
            counted_by_model=dict(batch["input_tokens_by_model"]),
            assemblies=assemblies_by_batch[batch["batch_id"]], ledger=ledger)
        batch_results.append(result)

    all_evidence = [e for r in batch_results
                    for e in r["per_model_verdict_evidence"]]
    privacy = assert_output_carries_no_secret(
        all_evidence, path_identities=frozenset(paths.values()))

    # MC4-R07: the artifact exists whatever the outcome. A blocked review is
    # a RESULT, not an absence of one.
    all_blocks = [b for r in batch_results for b in r["unit_blocks"]]
    synthesis = synthesize(batch_results)
    execution_evidence = evidence.candidate_evidence_record(
        evidence.MOCK_TEST_EVIDENCE, counts=[],
        logical_request_count=ledger.logical_requests,
        provider_attempt_count=ledger.attempts,
        endpoint=GENERATION_PATH,
        billing_state=counting.COUNT_BILLING_STATE)

    report = {
        "schema_version": 1,
        "artifact": "mock-execution-report",
        "publication_class": "private",
        "review_skeleton_sha256": plan_record["review_skeleton_sha256"],
        "mock_finalization_report_sha256": plan_record[
            "mock_finalization_report_sha256"],
        "execution_challenge": challenge,
        "execution_challenge_sha256": sha256_hex(challenge.encode()),
        "response_envelope_version": RESPONSE_ENVELOPE_VERSION,
        "batch_results": batch_results,
        "synthesis": synthesis,
        "execution_evidence": execution_evidence,
        "execution_preflight_manifest": preflight_manifest.record(),
        "generation_ledger": ledger.record(),
        "output_privacy_scan": privacy,
        "mock_transport_configuration": _transport_configuration(transport),
        "usage_drift_checked": True,
        "mock_generation_attempts": ledger.attempts,
        "unit_blocks": all_blocks,
        "block_codes": sorted({b["code"] for b in all_blocks}),
        "review_status": ("BLOCKED" if all_blocks
                          or not synthesis["overall_approved"] else "CLEAN"),
        "executable_authority": False,
        "evidence_class": evidence.MOCK_TEST_EVIDENCE,
    }
    report["mock_execution_report_sha256"] = mock_execution_digest(report)
    return report


def assert_review_clean(report: dict) -> dict:
    """Turn a BLOCKED report into a process failure — after it exists.

    Separating evidence production from process exit is the whole point of
    MC4-R07: the caller gets the artifact first, then this raises. A runner
    that skips this call still has the blocked report; it just does not stop,
    which is why every entry point calls it."""
    if report.get("review_status") != "BLOCKED":
        return report
    codes = report.get("block_codes") or []
    primary = codes[0] if codes else SYNTHESIS_REFUTED
    raise BlockingError(
        primary,
        f"category=review_blocked status=BLOCKED block_codes={codes} "
        f"refuted_units={report['synthesis']['refuted_unit_count']} "
        f"report_sha256={report['mock_execution_report_sha256'][:16]} — the "
        "full private verdict evidence for this block is retained in the "
        "report; this refusal is the process exit, not the finding")


def _transport_configuration(transport) -> dict:
    """The stand-in's configuration, or an explicit refusal to guess."""
    if hasattr(transport, "configuration"):
        return transport.configuration()
    return {"transport_class": type(transport).__name__,
            "source": getattr(transport, "source", None),
            "configuration_declared": False,
            "honest_scope": "this transport does not describe its own "
                            "behaviour, so the report cannot state what it "
                            "was configured to do"}


def mock_execution_digest(record: dict) -> str:
    stripped = {k: v for k, v in record.items()
                if k != "mock_execution_report_sha256"}
    return digest(b"mock-execution-report-v2", canonical_json(stripped))


# ------------------------------------------------------- strict loader ------


def validate_mock_execution_strict(report: dict, *, mock_finalization_report:
                                   dict, skeleton: dict, cwd,
                                   operator_pins: dict,
                                   authorizations=None,
                                   transport=None) -> dict:
    """RECONSTRUCT the execution report and compare it, whole (C4-F16).

    An outer digest proves internal consistency, and anyone who edits a claim
    can recompute it. So the whole execution is run again — deterministically,
    against the mock transport — and the canonical bytes must match. The
    inputs are required rather than optional for the same reason as the
    finalization loader: a validator with no repository context cannot verify
    a private report whose content is deliberately absent."""
    if not isinstance(report, dict):
        raise BlockingError(EXECUTABLE_PLAN_INVALID,
                            "category=execution_report_not_object")
    if report.get("artifact") != "mock-execution-report":
        raise BlockingError(
            EXECUTABLE_PLAN_INVALID,
            f"category=execution_artifact_tag_wrong "
            f"tag={report.get('artifact')}")
    if report.get("evidence_class") != evidence.MOCK_TEST_EVIDENCE:
        raise BlockingError(EXECUTABLE_PLAN_INVALID,
                            "category=execution_evidence_class_wrong")
    if report.get("executable_authority") is not False:
        raise BlockingError(EXECUTABLE_PLAN_INVALID,
                            "category=execution_claims_authority")
    if mock_execution_digest(report) != report.get(
            "mock_execution_report_sha256"):
        raise BlockingError(EXECUTABLE_PLAN_INVALID,
                            "category=execution_digest_mismatch")

    rebuilt = execute_mock(
        mock_finalization_report,
        transport=transport or MockGenerationTransport(),
        skeleton=skeleton, cwd=cwd, operator_pins=operator_pins,
        authorizations=authorizations)
    if canonical_json(rebuilt) != canonical_json(report):
        differing = sorted(
            key for key in set(rebuilt) | set(report)
            if canonical_json(rebuilt.get(key)) != canonical_json(
                report.get(key)))
        raise BlockingError(
            EXECUTABLE_PLAN_INVALID,
            f"category=execution_report_not_reproducible "
            f"differing_fields={differing}")
    return {"reconstructed": True,
            "generation_attempts": report["mock_generation_attempts"],
            "overall_approved": report["synthesis"]["overall_approved"]}


__all__ = [
    "ExecutionPreflightManifest",
    "GenerationLedger",
    "MockGenerationTransport",
    "assert_mock_generation_transport",
    "assert_output_carries_no_secret",
    "assert_request_matches_plan",
    "execute_batch",
    "execute_mock",
    "mock_execution_digest",
    "origin",
    "providerreq",
    "rebuild_payloads",
    "reconstruct_batch_requests",
    "synthesize",
    "validate_mock_execution_strict",
    "validate_response_envelope",
]
