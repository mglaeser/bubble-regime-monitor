"""Raw `/v1/responses` bodies become the engine's internal envelope, or refuse.

## The defect

`TrustedGenerationTransport.post` returned the provider's document unchanged.
`verifier.executor.validate_response_envelope` takes an INTERNAL envelope of
exactly `object`, `model`, `usage`, `output_parsed`, so every successful
generation was rejected as `PROVIDER_RESPONSE_INVALID`.

Panel run 31846462179: count succeeded, one real generation request was sent
and answered, and the panel refused while validating the reply —
`category=engine_refused where=execute_batch code=PROVIDER_RESPONSE_INVALID`,
`generation_attempts=1`.

The first two classes below reproduce that with no fix applied anywhere: a
realistic raw document straight into the executor, and the transport handing one
over. They are the red tests, and they stay in the suite as the statement of
what the adapter is for.

## Why v1 could not simply be called

`adapter.normalize` parses the obsolete `{verdicts: [...]}` payload while the
engine emits `challenge`/`lens_id`/`verdicts_by_unit`, and it requires the
output array to hold exactly one item while a reasoning model returns reasoning
items beside the message. Both are asserted here, so "just call the existing
adapter" cannot come back as a suggestion without a failing test.

No test in this file opens a socket. The transport tests drive a fake opener.
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from trustedlane import adapter, adapterv2, generationtransport  # noqa: E402
from trustedlane.errors import LaneRefusal  # noqa: E402
from verifier import executor, reviewpolicy, verdicts  # noqa: E402
from verifier.errors import BlockingError  # noqa: E402

MODEL = "gpt-5.6-sol"
CHALLENGE = "c" * 32
ENGINE_DIGEST = "a" * 64
PAYLOAD_DIGEST = "b" * 64


def identity() -> dict:
    return adapterv2.builtin_adapter_identity(engine_source_sha256=ENGINE_DIGEST)


#: One distinctly-worded reason and proof per reviewer, because independent
#: corroboration is the property the panel exists to provide and the anti-canned
#: tripwire measures it by similarity.
_REASONS = {
    "gpt-5.6-sol":
        "no credential, boundary or data-flow surface is touched; the range "
        "adds prose to a runbook and nothing reads it at runtime",
    "gpt-5.3-codex":
        "four added lines, one file, no imports and no call sites changed, so "
        "there is nothing here that can execute or alter control flow",
    "gpt-4.1-mini":
        "the wording matches what the workflow actually does and introduces "
        "no claim the surrounding document contradicts",
}
_PROOFS = {
    "gpt-5.6-sol": "traced every added line for reachable sinks and found none",
    "gpt-5.3-codex": "diffed the file against its parent and read both revisions",
    "gpt-4.1-mini": "compared the new sentences with the steps they describe",
}


def unit_verdict(*, model: str = MODEL, refuted: bool = False,
                 reason: str | None = None) -> dict:
    """A verdict in THIS model's closed category vocabulary.

    The categories are per-lens and at least one must come from the required
    group, so a fixture that hard-codes two generic words is refused by the
    engine — correctly. Read from `reviewpolicy` rather than copied, so a lens
    change breaks the fixture instead of silently making it unrepresentative."""
    allowed = list(reviewpolicy.lens_categories(model))
    required = list(reviewpolicy.lens_required_group(model))
    categories = [required[0]]
    for entry in allowed:
        if entry not in categories:
            categories.append(entry)
            break
    return {
        "refuted": refuted,
        "confidence": "high",
        # Genuinely distinct per model, not one sentence with the model name
        # substituted. Three approvals that paraphrase each other score 10000
        # basis points of similarity and the engine blocks them as canned —
        # which it did to the first version of this fixture, correctly.
        "reason": reason or _REASONS[model],
        "proof_of_check": f"{CHALLENGE} {_PROOFS[model]}",
        "checked_categories": categories,
    }


def payload(*, model: str = MODEL, units=("u1",), refuted: bool = False,
            challenge: str = CHALLENGE) -> dict:
    return {
        "challenge": challenge,
        "lens_id": reviewpolicy.lens_id(model),
        "verdicts_by_unit": {u: unit_verdict(model=model, refuted=refuted)
                             for u in units},
    }


def raw_response(*, model: str = MODEL, body=None, output=None,
                 status: str = "completed", usage=None,
                 incomplete_details=None, reasoning: bool = True) -> bytes:
    """A realistic Responses API document, reasoning items and all."""
    if output is None:
        text = json.dumps(body if body is not None else payload(model=model))
        output = []
        if reasoning:
            output.append({"type": "reasoning", "id": "rs_abc", "summary": []})
        output.append({"type": "message", "id": "msg_abc", "status": "completed",
                       "role": "assistant",
                       "content": [{"type": "output_text", "text": text,
                                    "annotations": []}]})
    document = {
        "id": "resp_abc", "object": "response", "created_at": 1,
        "status": status, "incomplete_details": incomplete_details,
        "model": model, "output": output,
        "usage": usage if usage is not None else {
            "input_tokens": 1200, "output_tokens": 340, "total_tokens": 1540,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 64}},
    }
    return json.dumps(document).encode("utf-8")


def normalize(raw: bytes, *, model: str = MODEL, http_status: int = 200,
              attempt: int = 1):
    return adapterv2.normalize_generation_response(
        raw, http_status=http_status, requested_model=model,
        payload_sha256=PAYLOAD_DIGEST, attempt=attempt,
        adapter_identity=identity())


# ============================================ 1-2. the defect, reproduced ===


class TestTheRawResponseIsRejectedByTheEngine:
    """D1 and D2. These fail against the ENGINE, not against the adapter, and
    they would pass identically before this PR."""

    def test_a_raw_responses_document_reproduces_provider_response_invalid(self):
        with pytest.raises(BlockingError) as caught:
            executor.validate_response_envelope(
                200, raw_response(), model_id=MODEL)
        assert caught.value.code == "PROVIDER_RESPONSE_INVALID"
        assert "generation_envelope_key_set" in str(caught.value)

    def test_the_transports_old_return_shape_reproduces_it(self):
        """What `post` used to return, fed to the validator that receives it."""
        reply_status, reply_body = 200, raw_response()
        with pytest.raises(BlockingError) as caught:
            executor.validate_response_envelope(
                reply_status, reply_body, model_id=MODEL)
        assert caught.value.code == "PROVIDER_RESPONSE_INVALID"

    def test_the_normalized_envelope_is_accepted_by_the_same_validator(self):
        """The other half of the same statement, so the pair is a diagnosis."""
        out = normalize(raw_response())
        parsed = executor.validate_response_envelope(
            200, out["envelope_bytes"], model_id=MODEL)
        assert set(parsed) == set(executor.ENVELOPE_KEYS)
        assert parsed["usage"] == {"input_tokens": 1200, "output_tokens": 340}


# ================================================= 3-5. the happy paths =====


class TestOneCompletedResponseNormalizes:

    def test_the_envelope_has_exactly_the_four_internal_keys(self):
        out = normalize(raw_response())
        assert sorted(out["envelope"]) == sorted(executor.ENVELOPE_KEYS)
        assert out["envelope"]["object"] == "response"
        assert out["envelope"]["model"] == MODEL

    def test_usage_is_extracted_to_exactly_two_fields(self):
        """The provider's usage carries totals and detail objects. The internal
        envelope is two fields, and the executor refuses any other key set."""
        out = normalize(raw_response())
        assert sorted(out["envelope"]["usage"]) == ["input_tokens",
                                                    "output_tokens"]

    def test_a_reasoning_item_does_not_become_the_answer(self):
        """D4. The message is chosen by TYPE. Selecting `output[0]` here would
        pick the reasoning block, which is why v1's single-output rule is wrong
        for a reasoning model rather than merely strict."""
        raw = raw_response(reasoning=True)
        document = json.loads(raw)
        assert document["output"][0]["type"] == "reasoning"
        out = normalize(raw)
        assert sorted(out["envelope"]["output_parsed"]) == [
            "challenge", "lens_id", "verdicts_by_unit"]

    def test_several_reasoning_items_are_still_one_answer(self):
        document = json.loads(raw_response())
        document["output"].insert(
            0, {"type": "reasoning", "id": "rs_2", "summary": []})
        out = normalize(json.dumps(document).encode())
        assert out["envelope"]["output_parsed"]["challenge"] == CHALLENGE

    def test_a_non_reasoning_model_with_one_message_is_accepted(self):
        """D5. gpt-4.1-mini returns no reasoning item at all."""
        model = "gpt-4.1-mini"
        out = normalize(raw_response(model=model, reasoning=False), model=model)
        assert out["envelope"]["model"] == model
        assert out["envelope"]["output_parsed"]["lens_id"] == (
            reviewpolicy.lens_id(model))

    def test_every_governed_model_normalizes(self):
        from verifier import policy
        for model in policy.REQUESTED_MODEL_IDS:
            out = normalize(raw_response(model=model), model=model)
            assert out["envelope"]["model"] == model


# ================================================ 6-12. what must block =====


class TestWhatTheAdapterRefuses:

    def _refuses(self, raw, *, model=MODEL, http_status=200):
        with pytest.raises(LaneRefusal) as caught:
            normalize(raw, model=model, http_status=http_status)
        return caught.value.reason

    def test_more_than_one_assistant_message_blocks(self):
        document = json.loads(raw_response())
        document["output"].append(json.loads(json.dumps(
            document["output"][-1])))
        reason = self._refuses(json.dumps(document).encode())
        assert "response_message_count_not_one" in reason
        assert "count=2" in reason

    def test_zero_assistant_messages_blocks(self):
        document = json.loads(raw_response())
        document["output"] = [{"type": "reasoning", "id": "r", "summary": []}]
        assert "response_message_count_not_one" in self._refuses(
            json.dumps(document).encode())

    @pytest.mark.parametrize("kind", ["function_call", "tool_call",
                                      "computer_call", "web_search_call",
                                      "something_new"])
    def test_tool_and_function_output_blocks(self, kind):
        """D7. An allowlist, so the type invented next year blocks too."""
        document = json.loads(raw_response())
        document["output"].insert(0, {"type": kind, "id": "x"})
        reason = self._refuses(json.dumps(document).encode())
        assert "response_output_item_type_not_permitted" in reason
        assert kind not in reason, "the provider-controlled type was echoed"

    def test_a_provider_refusal_blocks_and_its_text_is_not_logged(self):
        """D8. The refusal string is provider-controlled text leaving a job
        that holds the credential."""
        secret = "I will not comply because SENTINEL_TEXT_9137"
        document = json.loads(raw_response())
        document["output"][-1]["content"] = [
            {"type": "refusal", "refusal": secret}]
        reason = self._refuses(json.dumps(document).encode())
        assert "response_is_a_refusal" in reason
        assert "SENTINEL" not in reason
        assert secret not in reason

    @pytest.mark.parametrize("status", ["incomplete", "in_progress", "failed"])
    def test_a_non_completed_status_blocks(self, status):
        assert "response_status_not_completed" in self._refuses(
            raw_response(status=status))

    def test_incomplete_details_blocks_even_when_completed(self):
        raw = raw_response(incomplete_details={"reason": "max_output_tokens"})
        assert "response_declares_incomplete_details" in self._refuses(raw)

    def test_duplicate_keys_in_the_raw_response_block(self):
        raw = b'{"object":"response","object":"response","status":"completed"}'
        assert "duplicate_key_in_provider_document" in self._refuses(raw)

    def test_duplicate_keys_in_the_output_text_block(self):
        text = '{"challenge":"a","challenge":"b","lens_id":"l",' \
               '"verdicts_by_unit":{}}'
        document = json.loads(raw_response())
        document["output"][-1]["content"][0]["text"] = text
        assert "duplicate_key_in_provider_document" in self._refuses(
            json.dumps(document).encode())

    @pytest.mark.parametrize("usage", [
        None, {}, {"input_tokens": 1}, {"input_tokens": 1, "output_tokens": -1},
        {"input_tokens": True, "output_tokens": 2},
        {"input_tokens": 1.0, "output_tokens": 2},
        {"input_tokens": "1", "output_tokens": 2},
    ])
    def test_missing_or_malformed_usage_blocks(self, usage):
        document = json.loads(raw_response())
        if usage is None:
            del document["usage"]
        else:
            document["usage"] = usage
        reason = self._refuses(json.dumps(document).encode())
        assert "usage" in reason

    def test_a_bool_input_token_count_is_not_an_integer(self):
        """`True` is an `int` in Python and would reach the drift comparison as
        a number nobody metered."""
        document = json.loads(raw_response())
        document["usage"] = {"input_tokens": True, "output_tokens": 0}
        assert "not_plain_nonneg_int" in self._refuses(
            json.dumps(document).encode())

    def test_a_model_mismatch_blocks_without_echoing_the_other_model(self):
        raw = raw_response(model="gpt-4.1-mini")
        reason = self._refuses(raw, model=MODEL)
        assert "response_model_mismatch" in reason
        assert f"expected={MODEL}" in reason
        assert "gpt-4.1-mini" not in reason

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
    def test_a_non_200_http_status_blocks(self, status):
        assert "response_status_not_ok" in self._refuses(
            raw_response(), http_status=status)

    def test_a_markdown_fenced_payload_is_not_repaired(self):
        document = json.loads(raw_response())
        document["output"][-1]["content"][0]["text"] = (
            "```json\n" + json.dumps(payload()) + "\n```")
        assert "verdict_payload_not_json" in self._refuses(
            json.dumps(document).encode())

    def test_two_content_parts_block(self):
        document = json.loads(raw_response())
        document["output"][-1]["content"].append(
            {"type": "output_text", "text": "{}"})
        assert "response_content_not_single" in self._refuses(
            json.dumps(document).encode())

    def test_an_unknown_payload_key_blocks_and_is_counted_not_named(self):
        body = payload()
        body["SENTINEL_KEY_4471"] = "x"
        reason = self._refuses(raw_response(body=body))
        assert "verdict_payload_key_set" in reason
        assert "unexpected=1" in reason
        assert "SENTINEL" not in reason

    def test_a_missing_payload_key_blocks(self):
        body = payload()
        del body["lens_id"]
        assert "missing=['lens_id']" in self._refuses(raw_response(body=body))

    def test_an_empty_verdict_map_blocks(self):
        body = payload()
        body["verdicts_by_unit"] = {}
        assert "verdicts_by_unit_empty" in self._refuses(
            raw_response(body=body))

    def test_an_oversized_body_blocks_before_it_is_parsed(self):
        raw = b'{"object":"response"' + b" " * (
            adapterv2.MAX_RESPONSE_BYTES + 1) + b"}"
        assert "raw_response_oversized" in self._refuses(raw)

    def test_a_str_body_blocks(self):
        assert "raw_response_not_bytes" in self._refuses(
            raw_response().decode("utf-8"))

    def test_invalid_utf8_blocks(self):
        assert "raw_response_not_utf8" in self._refuses(b"\xff\xfe{}")


# ================================== 13-14. the two payload contracts ========


class TestTheCurrentContractReachesTheEngineUnchanged:

    def test_the_parsed_payload_validates_against_the_engine_validator(self):
        """D13. The object the adapter produces is handed to the REAL verdict
        validator with nothing in between."""
        out = normalize(raw_response())
        policy = panel_policy()
        validated = verdicts.validate_verdicts(
            out["envelope"]["output_parsed"], unit_hashes=["u1"],
            challenge=CHALLENGE, review_policy=policy, model_id=MODEL)
        assert set(validated) == {"u1"}
        assert validated["u1"]["refuted"] is False

    def test_the_adapter_builds_no_intermediate_representation(self):
        """The parsed payload is the SAME object, not a rebuilt copy: there is
        nowhere for a field to be renamed."""
        out = normalize(raw_response())
        assert out["envelope"]["output_parsed"] == payload()

    def test_the_obsolete_v1_payload_shape_is_refused_by_name(self):
        """D14. `{verdicts: [...]}` means something is running v1's parser."""
        with pytest.raises(LaneRefusal) as caught:
            normalize(raw_response(body={"verdicts": [{"unit_sha256": "u1"}]}))
        assert "obsolete_v1_shape" in caught.value.reason

    def test_v1_cannot_read_the_current_contract(self):
        """Why a v2 exists at all, stated as a test rather than as prose."""
        raw = raw_response(reasoning=False)
        binding = {"raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                   "raw_response_bytes": len(raw), "http_status": 200,
                   "model_id": MODEL, "request_semantics_sha256": PAYLOAD_DIGEST,
                   "attempt": 1}
        with pytest.raises(LaneRefusal) as caught:
            adapter.normalize(
                raw, http_status=200, raw_binding=binding,
                adapter_identity=adapter.builtin_adapter_identity(
                    engine_source_sha256=ENGINE_DIGEST))
        assert "verdict_payload_unknown_field" in caught.value.reason

    def test_v1_cannot_read_a_reasoning_model_response(self):
        raw = raw_response(reasoning=True)
        binding = {"raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                   "raw_response_bytes": len(raw), "http_status": 200,
                   "model_id": MODEL, "request_semantics_sha256": PAYLOAD_DIGEST,
                   "attempt": 1}
        with pytest.raises(LaneRefusal) as caught:
            adapter.normalize(
                raw, http_status=200, raw_binding=binding,
                adapter_identity=adapter.builtin_adapter_identity(
                    engine_source_sha256=ENGINE_DIGEST))
        assert "raw_output_not_single" in caught.value.reason

    def test_v1_and_v2_are_different_versions(self):
        assert adapter.NORMALIZATION_VERSION != adapterv2.NORMALIZATION_VERSION
        assert adapterv2.NORMALIZATION_VERSION.endswith("-v2")


# ============================== 15-16. the transport never returns raw ======


class FakeOpener:
    """The provider, as a function. Records what it was asked."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "body": body})
        if not self._replies:
            raise AssertionError("the transport made more calls than planned")
        return self._replies.pop(0)


def build_transport(replies, *, cap: int = 80):
    return generationtransport.TrustedGenerationTransport(
        opener=FakeOpener(replies), credential="k" * 20, phase="TEST",
        engine_generation_path=executor.GENERATION_PATH,
        generation_attempt_cap=cap, adapter_identity=identity())


def request_bytes(model: str = MODEL) -> bytes:
    return json.dumps({"model": model, "input": "..."}).encode("utf-8")


class TestTheTransportReturnsTheNormalizedEnvelope:

    def test_post_returns_the_internal_envelope_and_not_raw_bytes(self):
        """D15. The exact statement: what comes back is not the provider's."""
        raw = raw_response()
        transport = build_transport([(200, raw)])
        status, body = transport.post(executor.GENERATION_PATH,
                                      request_bytes(), timeout=30)
        assert status == 200
        assert body != raw
        parsed = json.loads(body)
        assert sorted(parsed) == sorted(executor.ENVELOPE_KEYS)

    def test_what_post_returns_passes_the_engines_validator(self):
        transport = build_transport([(200, raw_response())])
        status, body = transport.post(executor.GENERATION_PATH,
                                      request_bytes(), timeout=30)
        assert executor.validate_response_envelope(
            status, body, model_id=MODEL)["model"] == MODEL

    def test_the_expected_model_comes_from_the_request_not_the_reply(self):
        """A check whose expected value is read out of the thing being checked
        cannot fail. The request names sol; the reply claims mini."""
        transport = build_transport([(200, raw_response(model="gpt-4.1-mini"))])
        with pytest.raises(LaneRefusal) as caught:
            transport.post(executor.GENERATION_PATH, request_bytes(MODEL),
                           timeout=30)
        assert "response_model_mismatch" in caught.value.reason

    def test_a_normalization_record_is_kept_per_call(self):
        transport = build_transport([(200, raw_response())])
        transport.post(executor.GENERATION_PATH, request_bytes(), timeout=30)
        assert len(transport.normalization_records) == 1
        record = transport.normalization_records[0]
        for field in adapterv2.RECORD_FIELDS:
            assert record.get(field) not in (None, ""), field

    def test_the_record_carries_no_body_no_text_and_no_provider_ids(self):
        """C. Structural evidence only."""
        transport = build_transport([(200, raw_response())])
        transport.post(executor.GENERATION_PATH, request_bytes(), timeout=30)
        blob = json.dumps(transport.record())
        for forbidden in ("resp_abc", "msg_abc", "rs_abc", "output_text",
                          "the change is documentation only", "authorization",
                          "Bearer", "k" * 20):
            assert forbidden not in blob, forbidden

    def test_the_transport_record_digests_every_normalization(self):
        transport = build_transport([(200, raw_response()),
                                     (200, raw_response())])
        for _ in range(2):
            transport.post(executor.GENERATION_PATH, request_bytes(),
                           timeout=30)
        record = transport.record()
        assert record["normalizations" if "normalizations" in record
                      else "generation_attempts"] == 2
        assert len(record["normalization_records_sha256"]) == 64
        assert record["normalization_version"] == (
            adapterv2.NORMALIZATION_VERSION)

    def test_the_digest_changes_when_an_answer_changes(self):
        first = build_transport([(200, raw_response())])
        first.post(executor.GENERATION_PATH, request_bytes(), timeout=30)
        second = build_transport(
            [(200, raw_response(body=payload(refuted=True)))])
        second.post(executor.GENERATION_PATH, request_bytes(), timeout=30)
        assert (first.record()["normalization_records_sha256"]
                != second.record()["normalization_records_sha256"])

    def test_a_transport_built_without_a_trusted_adapter_is_refused(self):
        with pytest.raises(LaneRefusal) as caught:
            generationtransport.TrustedGenerationTransport(
                opener=FakeOpener([]), credential="k" * 20, phase="TEST",
                engine_generation_path=executor.GENERATION_PATH,
                generation_attempt_cap=80,
                adapter_identity={
                    "normalization_version": adapterv2.NORMALIZATION_VERSION,
                    "adapter_source": "CANDIDATE_PACKAGE",
                    "adapter_sha256": ENGINE_DIGEST})
        assert "adapter_from_candidate" in caught.value.reason

    def test_a_v1_adapter_identity_is_refused_at_construction(self):
        with pytest.raises(LaneRefusal) as caught:
            generationtransport.TrustedGenerationTransport(
                opener=FakeOpener([]), credential="k" * 20, phase="TEST",
                engine_generation_path=executor.GENERATION_PATH,
                generation_attempt_cap=80,
                adapter_identity=adapter.builtin_adapter_identity(
                    engine_source_sha256=ENGINE_DIGEST))
        assert "adapter_version_mismatch" in caught.value.reason


class TestTheSourceCannotReturnTheUnnormalizedBody:
    """D16. An AST guard, because a comment saying "normalized" is not one."""

    def _post_body(self):
        source = (ROOT / "scripts" / "trustedlane"
                  / "generationtransport.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef)
                   and n.name == "TrustedGenerationTransport")
        return next(n for n in cls.body
                    if isinstance(n, ast.FunctionDef) and n.name == "post")

    def test_post_never_returns_the_exchange_reply_body(self):
        for node in ast.walk(self._post_body()):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            for sub in ast.walk(node.value):
                if (isinstance(sub, ast.Subscript)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == "reply"
                        and isinstance(sub.slice, ast.Constant)
                        and sub.slice.value == "body"):
                    raise AssertionError(
                        "post() returns reply['body'] — the raw provider "
                        "document, which the engine refuses as "
                        "PROVIDER_RESPONSE_INVALID")

    def test_post_returns_a_normalized_envelope(self):
        returns = [n for n in ast.walk(self._post_body())
                   if isinstance(n, ast.Return) and n.value is not None]
        assert returns, "post() returns nothing"
        rendered = " ".join(ast.dump(n) for n in returns)
        assert "envelope_bytes" in rendered

    def test_the_module_calls_the_v2_adapter(self):
        source = (ROOT / "scripts" / "trustedlane"
                  / "generationtransport.py").read_text(encoding="utf-8")
        assert "adapterv2.normalize_generation_response" in source


# ================================ 17-19. the provider-shaped vertical =======


class Assembly:
    """The one thing `execute_batch` needs from an assembled request."""

    def __init__(self, model_id: str):
        self.execution_payload_bytes = request_bytes(model_id)

    def request_semantics_sha256(self) -> str:
        return hashlib.sha256(self.execution_payload_bytes).hexdigest()


PANEL = ("gpt-5.6-sol", "gpt-5.3-codex", "gpt-4.1-mini")


def panel_policy() -> dict:
    return reviewpolicy.policy_record(
        list(PANEL), required_approver="gpt-5.6-sol",
        minimum_other_approvers=1, max_output_tokens=8000)


def run_panel(*, refuted_by=(), units=("u1",), counted=None,
              tolerance=10000):
    """count -> raw fixture -> normalize -> execute, through the real objects.

    The transport is the real `TrustedGenerationTransport`; only the socket is
    fake. Every raw document goes through `adapterv2`, `validate_response_envelope`
    and `validate_verdicts` exactly as it does in the hosted job."""
    policy = panel_policy()
    replies = [(200, raw_response(model=m, body=payload(
        model=m, units=units, refuted=m in refuted_by))) for m in PANEL]
    transport = build_transport(replies)
    ledger = executor.GenerationLedger({
        "VERIFIER_MAX_GENERATION_CALLS": 80,
        "VERIFIER_GENERATION_MAX_RETRIES": 2,
        "VERIFIER_GENERATION_TIMEOUT_SECONDS": 60,
        "VERIFIER_TOKEN_DRIFT_TOLERANCE": tolerance})
    result = executor.execute_batch(
        {"batch_id": "b1", "unit_sha256_in_order": list(units)},
        policy, transport=transport,
        pin_values={"VERIFIER_GENERATION_TIMEOUT_SECONDS": 60,
                    "VERIFIER_TOKEN_DRIFT_TOLERANCE": tolerance},
        challenge=CHALLENGE,
        counted_by_model=counted or {},
        assemblies={m: Assembly(m) for m in PANEL},
        ledger=ledger)
    return result, transport


class TestTheProviderShapedVertical:

    def test_all_three_models_vote_and_the_unit_is_decided(self):
        result, transport = run_panel()
        assert len(result["per_model_verdict_evidence"]) == 3
        assert {r["model_id"] for r in result["per_model_verdict_evidence"]} == (
            set(PANEL))
        assert result["unit_decisions"]["u1"]["approved"] is True
        assert not result["unit_blocks"]
        assert len(transport.normalization_records) == 3

    def test_one_normalization_record_per_model(self):
        _, transport = run_panel()
        models = [r["requested_model"]
                  for r in transport.normalization_records]
        assert sorted(models) == sorted(PANEL)
        assert len({r["normalized_envelope_sha256"]
                    for r in transport.normalization_records}) == 3

    def test_the_required_approver_refutation_is_a_veto(self):
        """D18. Sol says no, and no amount of other approval clears it."""
        result, _ = run_panel(refuted_by=("gpt-5.6-sol",))
        assert result["unit_decisions"]["u1"]["approved"] is False
        assert result["unit_blocks"]
        assert any("REQUIRED_APPROVER_REFUTED" in json.dumps(b)
                   or "required_approver" in json.dumps(b)
                   for b in result["unit_blocks"])

    def test_a_corroborator_refutation_is_preserved_not_averaged(self):
        """A non-approver's "no" survives as a NAMED refutation.

        `execute_batch` records who refuted rather than blocking here — the
        strict-any veto is applied a level up, in `panel.aggregate`. What
        matters at this level is that the dissent is carried per model and not
        folded into a score, so the assertion is on `refuted_by` and not on a
        block that belongs to the other layer."""
        result, _ = run_panel(refuted_by=("gpt-4.1-mini",))
        decision = result["unit_decisions"]["u1"]
        assert decision["approved"] is False
        assert decision["refuted_by"] == ["gpt-4.1-mini"]
        assert decision["distinct_other_approvers"] == 1

    def test_the_distinct_corroborator_is_counted(self):
        """Two models other than the required approver approved, which is what
        `minimum_other_approvers=1` is measured against."""
        result, _ = run_panel()
        decision = result["unit_decisions"]["u1"]
        assert decision["distinct_other_approvers"] == 2
        assert decision["approver_confidence"] == "high"

    def test_usage_reaches_the_token_drift_comparison(self):
        """The extracted `input_tokens` is a real number the engine compares.

        The fixture reports 1200 and the count says 1200, so the drift check
        passes with a tolerance of zero — which proves the number arrived
        rather than that the tolerance was generous."""
        result, _ = run_panel(counted={m: 1200 for m in PANEL}, tolerance=0)
        assert len(result["per_model_verdict_evidence"]) == 3

    def test_a_wildly_wrong_count_still_blocks_through_the_envelope(self):
        """If normalization dropped or invented `input_tokens`, this could not
        block — so it is also a test that the real number is carried."""
        with pytest.raises(BlockingError):
            run_panel(counted={m: 1 for m in PANEL}, tolerance=10)

    def test_no_socket_is_opened_anywhere_in_this_file(self):
        """D20. The opener is a callable this module defines."""
        _, transport = run_panel()
        assert isinstance(transport._opener, FakeOpener)
        assert len(transport._opener.calls) == 3


class TestNormalizationFailureSpendsNothingExtra:
    """D19. A bad response must not become a retry loop."""

    def test_a_malformed_reply_makes_no_further_attempts(self):
        transport = build_transport(
            [(200, raw_response(status="incomplete"))])
        with pytest.raises(LaneRefusal):
            transport.post(executor.GENERATION_PATH, request_bytes(),
                           timeout=30)
        assert transport.calls == 1
        assert len(transport._opener.calls) == 1
        assert transport.normalization_records == []

    def test_the_attempt_that_produced_it_is_still_accounted_for(self):
        """The refusal must not erase the fact that a call was made and paid
        for — that is the accounting defect PR #44 exists to have fixed."""
        transport = build_transport([(200, raw_response(status="incomplete"))])
        with pytest.raises(LaneRefusal):
            transport.post(executor.GENERATION_PATH, request_bytes(),
                           timeout=30)
        assert len(transport.attempts) == 1
        assert transport.attempts[0]["attempt"] == 1
        assert transport.record()["generation_attempts"] == 1

    def test_the_attempt_cap_still_counts_a_failed_normalization(self):
        transport = build_transport(
            [(200, raw_response(status="incomplete"))], cap=1)
        with pytest.raises(LaneRefusal):
            transport.post(executor.GENERATION_PATH, request_bytes(),
                           timeout=30)
        with pytest.raises(LaneRefusal) as caught:
            transport.post(executor.GENERATION_PATH, request_bytes(),
                           timeout=30)
        assert "generation_attempt_cap_reached" in caught.value.reason


# =========================================== the engine contract agrees =====


class TestTheEnvelopeContractIsCheckedAgainstTheEngine:

    def test_the_adapter_and_the_engine_declare_the_same_shape(self):
        assert set(adapterv2.ENVELOPE_KEYS) == set(executor.ENVELOPE_KEYS)
        assert set(adapterv2.USAGE_KEYS) == set(executor.USAGE_KEYS)
        assert adapterv2.ENVELOPE_OBJECT == executor.EXPECTED_OBJECT

    def test_a_disagreeing_engine_is_refused_at_bind_time(self):
        class Executor:
            GENERATION_PATH = executor.GENERATION_PATH
            ENVELOPE_KEYS = ("object", "model", "usage", "output_parsed",
                             "fifth")
            USAGE_KEYS = executor.USAGE_KEYS
            EXPECTED_OBJECT = "response"

        with pytest.raises(LaneRefusal) as caught:
            generationtransport.assert_envelope_contract_agrees(Executor)
        assert "envelope_contract_disagrees_with_engine" in caught.value.reason

    def test_an_engine_declaring_nothing_is_refused(self):
        class Executor:
            GENERATION_PATH = executor.GENERATION_PATH

        with pytest.raises(LaneRefusal) as caught:
            generationtransport.assert_envelope_contract_agrees(Executor)
        assert "declares_no_envelope_keys" in caught.value.reason

    def test_the_real_engine_agrees(self):
        assert generationtransport.assert_envelope_contract_agrees(
            executor)["envelope_contract_agrees"] is True
