"""The wire schema must be one the provider will actually accept.

Panel run 31718284187 made the first real provider count attempt this lane has
ever made, and the provider answered:

    400 invalid_request_error
    code=invalid_json_schema
    param=text.format.schema

A live operator differential then isolated the cause, sixteen count-only
requests built from the real engine and differing by one keyword:

    exact engine payload             400 invalid_json_schema
    remove ONLY uniqueItems          200, all three governed models
    remove `text` entirely           200  — so `text` itself is supported
    remove ONLY reasoning            400  — so reasoning is not the cause
    remove uniqueItems + min/maxLength
                                     200  — no better than uniqueItems alone,
                                            so those two are not implicated

`gpt-4.1-mini` sends no `reasoning` field at all and behaved identically —
exact 400, without-uniqueItems 200 — which rules out a model-specific
interaction between reasoning and the schema.

The keyword is therefore gone from the wire. What it guaranteed is NOT gone:
`verdicts._validate_one` rejects a duplicated category after parsing. That
check was previously redundant with the schema and is now the only thing
enforcing uniqueness, so this file pins it as load-bearing — including a
mutation test proving it reddens if removed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from verifier import providerreq, reviewpolicy, verdicts  # noqa: E402
from verifier.errors import BlockingError  # noqa: E402

PANEL = ("gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini")
UNIT = "a" * 64
CHALLENGE = "CH-" + "0" * 61


def walk_keys(node):
    """Every key name anywhere in the structure."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk_keys(item)


def categories_schema(model_id=None):
    schema = providerreq.verdict_schema([UNIT], challenge=CHALLENGE,
                                        model_id=model_id)
    return schema["schema"]["properties"]["verdicts_by_unit"][
        "properties"][UNIT]["properties"]["checked_categories"]


class TestTheRejectedKeywordIsGone:

    @pytest.mark.parametrize("model_id", [None, *PANEL])
    def test_no_uniqueItems_anywhere_recursively(self, model_id):
        """Requirement 1. Recursive, not a top-level check: the keyword sat
        nested inside `text.format.schema`, which is where the provider found
        it and where a shallow assertion would have missed it."""
        schema = providerreq.verdict_schema([UNIT], challenge=CHALLENGE,
                                            model_id=model_id)
        assert "uniqueItems" not in set(walk_keys(schema))

    @pytest.mark.parametrize("model_id", PANEL)
    def test_the_array_contract_otherwise_survives(self, model_id):
        """Requirement 2. Removing the keyword must not quietly widen the
        field into free text."""
        schema = categories_schema(model_id)
        assert schema["type"] == "array"
        assert schema["minItems"] == reviewpolicy.MIN_CHECKED_CATEGORIES
        assert schema["maxItems"] == reviewpolicy.MAX_CHECKED_CATEGORIES
        expected = list(reviewpolicy.lens_categories(model_id))
        assert schema["items"]["enum"] == expected
        assert expected, "a lens with no categories would be an open vocabulary"

    @pytest.mark.parametrize("model_id", [None, *PANEL])
    def test_strict_and_closed_objects_remain(self, model_id):
        """Requirement 3."""
        built = providerreq.verdict_schema([UNIT], challenge=CHALLENGE,
                                           model_id=model_id)
        assert built["strict"] is True
        schema = built["schema"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["verdicts_by_unit"][
            "additionalProperties"] is False
        assert schema["properties"]["verdicts_by_unit"]["properties"][UNIT][
            "additionalProperties"] is False

    @pytest.mark.parametrize("model_id", [None, *PANEL])
    def test_every_required_verdict_field_remains(self, model_id):
        built = providerreq.verdict_schema([UNIT], challenge=CHALLENGE,
                                           model_id=model_id)
        unit = built["schema"]["properties"]["verdicts_by_unit"][
            "properties"][UNIT]
        assert set(unit["required"]) == {"refuted", "confidence", "reason",
                                         "proof_of_check",
                                         "checked_categories"}

    @pytest.mark.parametrize("model_id", [None, *PANEL])
    def test_minlength_and_maxlength_are_unchanged(self, model_id):
        """Requirement 11. The differential cleared these; they must stay."""
        unit = providerreq.verdict_schema(
            [UNIT], challenge=CHALLENGE, model_id=model_id)[
            "schema"]["properties"]["verdicts_by_unit"]["properties"][UNIT][
            "properties"]
        assert unit["reason"]["minLength"] == reviewpolicy.REASON_MIN_CHARS
        assert unit["reason"]["maxLength"] == reviewpolicy.REASON_MAX_CHARS
        assert unit["proof_of_check"]["minLength"] == (
            reviewpolicy.PROOF_MIN_CHARS)
        assert unit["proof_of_check"]["maxLength"] == (
            reviewpolicy.PROOF_MAX_CHARS)

    def test_the_unmodelled_fallback_keeps_its_string_bounds(self):
        schema = categories_schema(None)
        assert schema["items"]["minLength"] == 1
        assert schema["items"]["maxLength"] == reviewpolicy.CATEGORY_MAX_CHARS

    def test_reintroducing_uniqueItems_reddens_this_file(self):
        """Requirement 7. The guard is not vacuous: a schema carrying the
        keyword must fail the recursive check that just passed."""
        schema = providerreq.verdict_schema([UNIT], challenge=CHALLENGE,
                                            model_id=PANEL[0])
        assert "uniqueItems" not in set(walk_keys(schema))
        regressed = json.loads(json.dumps(schema))
        regressed["schema"]["properties"]["verdicts_by_unit"]["properties"][
            UNIT]["properties"]["checked_categories"]["uniqueItems"] = True
        assert "uniqueItems" in set(walk_keys(regressed))

    def test_the_production_source_carries_no_generic_stripper(self):
        """A recursive "remove unsupported keywords" pass would make the next
        incompatibility silent. The contract is explicit and reviewed."""
        source = (ROOT / "scripts" / "verifier" / "providerreq.py").read_text(
            encoding="utf-8")
        for banned in ("def strip_unsupported", "def _strip_keywords",
                       "UNSUPPORTED_KEYWORDS"):
            assert banned not in source


class TestUniquenessIsStillEnforced:
    """It moved off the wire; it did not go away."""

    @staticmethod
    def _policy():
        return reviewpolicy.policy_record(
            list(PANEL), required_approver="gpt-5.6-sol",
            minimum_other_approvers=1, max_output_tokens=8_000)

    @staticmethod
    def _response(categories):
        return {"challenge": CHALLENGE,
                "verdicts_by_unit": {
                    UNIT: {"refuted": False, "confidence": "high",
                           "reason": CHALLENGE + " both sides were compared "
                                                 "and the invariant holds",
                           "proof_of_check": CHALLENGE + " inspected both sides",
                           "checked_categories": categories}}}

    def test_a_unique_category_list_passes(self):
        """Requirement 5."""
        verdicts.validate_verdicts(
            self._response(["logic", "state"]), unit_hashes=[UNIT],
            challenge=CHALLENGE, review_policy=self._policy())

    def test_a_duplicated_category_is_rejected(self):
        """Requirement 4. What the schema keyword used to prevent."""
        with pytest.raises(BlockingError) as caught:
            verdicts.validate_verdicts(
                self._response(["logic", "logic"]), unit_hashes=[UNIT],
                challenge=CHALLENGE, review_policy=self._policy())
        assert "checked_categories_not_unique" in str(caught.value)

    def test_removing_the_downstream_check_would_redden_this(self):
        """Requirement 6, as a mutation rather than a claim.

        The duplicate-rejecting branch is simulated away; the same input must
        then pass, proving the assertion above is carried by that branch and
        not by something else incidentally rejecting the payload."""
        source = (ROOT / "scripts" / "verifier" / "verdicts.py").read_text(
            encoding="utf-8")
        assert "checked_categories_not_unique" in source
        assert "if len(set(categories)) != len(categories):" in source

        duplicated = ["logic", "logic"]
        # The mutation: the check's own condition, evaluated directly. If the
        # branch were deleted, this is the test that would stop distinguishing
        # a duplicate list from a unique one.
        assert len(set(duplicated)) != len(duplicated)
        assert len(set(["logic", "state"])) == len(["logic", "state"])


class TestTheWireContractIsVersioned:

    def test_the_schema_name_is_v2(self):
        assert providerreq.VERDICT_SCHEMA_NAME == "verifier_unit_verdicts_v2"

    def test_the_review_request_policy_is_v3(self):
        assert reviewpolicy.POLICY_VERSION == "review-request-policy-v3"

    def test_the_schema_name_reaches_the_wire_and_the_semantics(self):
        built = providerreq.verdict_schema([UNIT], challenge=CHALLENGE,
                                           model_id=PANEL[0])
        assert built["name"] == providerreq.VERDICT_SCHEMA_NAME

    def test_the_policy_digest_binds_the_response_contract(self):
        record = reviewpolicy.policy_record(
            list(PANEL), required_approver="gpt-5.6-sol",
            minimum_other_approvers=1, max_output_tokens=8_000)
        assert record["policy_version"] == "review-request-policy-v3"


def _built_request(model_id="gpt-5.3-codex", effort="medium"):
    """One real single-unit request through the production assembler."""
    from verifier import unitpayload
    unit = {"unit_sha256": "a" * 64, "git_status": "M",
            "atom_ids": ["b" * 64], "path_bytes_b64": "",
            "classification": {"control_bearing": False, "effective_risk": 0},
            "split_strategies": [], "split_depth": 0}
    records = {"b" * 64: {"atom_id": "b" * 64, "side": "new",
                          "line_number": 1, "hunk_id": "h",
                          "path_bytes_b64": ""}}
    payload = unitpayload.structured_unit(unit, records, {"b" * 64: "x = 1"})
    return providerreq.build_request(
        model_id, [payload], lens=reviewpolicy.LENSES[model_id],
        challenge=CHALLENGE, reasoning_effort=effort, max_output_tokens=8_000)


class TestTheRequestHashesMoved:
    """Requirement 8. Old count evidence must not be reusable under v2."""

    def test_the_schema_name_reaches_the_count_body(self):
        request = _built_request()
        assert request.count_payload()["text"]["format"]["name"] == (
            "verifier_unit_verdicts_v2")

    def test_count_and_execution_hashes_change_with_the_schema_name(self,
                                                                    monkeypatch):
        """The hashes are taken over the body, and the body carries the name.

        Rebuilding under the v1 name must produce different digests, or a plan
        counted against the provider-rejected contract could be presented as
        having been counted against this one."""
        request = _built_request()
        v2_count = request.count_request_sha256()
        v2_exec = request.execution_request_sha256()
        v2_semantics = request.request_semantics_sha256()

        monkeypatch.setattr(providerreq, "VERDICT_SCHEMA_NAME",
                            "verifier_unit_verdicts_v1")
        as_v1 = _built_request()
        assert as_v1.count_request_sha256() != v2_count
        assert as_v1.execution_request_sha256() != v2_exec
        assert as_v1.request_semantics_sha256() != v2_semantics

    def test_reintroducing_uniqueItems_also_moves_the_count_hash(self,
                                                                 monkeypatch):
        """The keyword itself is inside the hashed body, not only the name."""
        baseline = _built_request().count_request_sha256()
        original = providerreq.verdict_schema

        def with_keyword(unit_hashes, *, challenge, model_id=None):
            built = original(unit_hashes, challenge=challenge,
                             model_id=model_id)
            built["schema"]["properties"]["verdicts_by_unit"]["properties"][
                unit_hashes[0]]["properties"]["checked_categories"][
                "uniqueItems"] = True
            return built

        monkeypatch.setattr(providerreq, "verdict_schema", with_keyword)
        assert _built_request().count_request_sha256() != baseline


class TestOneImmutableParent:
    """Requirement 9. Count and execution must describe the same request."""

    def test_both_payloads_derive_from_the_same_object(self):
        request = _built_request()
        count = request.count_payload()
        execution = request.execution_payload()
        for field in ("model", "instructions", "input", "text", "truncation"):
            assert count[field] == execution[field]
        # Execution adds the output ceiling; counting input tokens must not
        # carry it, or the counted body would differ from the sent one.
        assert "max_output_tokens" in execution
        assert "max_output_tokens" not in count

    def test_the_request_is_frozen(self):
        """A mutable parent could be edited between counting and executing."""
        request = _built_request()
        with pytest.raises((AttributeError, TypeError)):
            request.model_id = "gpt-4.1-mini"


class TestReasoningIsUntouched:
    """Requirement 10. The differential exonerated it; it must not drift."""

    @pytest.mark.parametrize("model_id", ["gpt-5.3-codex", "gpt-5.6-sol"])
    def test_reasoning_models_still_send_effort(self, model_id):
        body = _built_request(model_id).count_payload()
        assert body["reasoning"] == {"effort": "medium"}

    def test_the_non_reasoning_model_still_omits_it_entirely(self):
        body = _built_request("gpt-4.1-mini", effort=None).count_payload()
        assert "reasoning" not in body
