"""The operator PIN record (MC3 §31).

All twelve values are OPERATOR decisions. There are no defaults: a missing
or malformed PIN is UNSET_POLICY_PIN and zero provider calls happen. Values
are validated against the capability policy — a max-output above what a
model supports, a reasoning effort outside a model's approved set, or a
reasoning effort supplied for a model with no reasoning step are all
refusals, not clamps.

Money is INTEGER micro-USD. A float never enters a cost ledger.
"""

from __future__ import annotations

from . import authority
from .canon import canonical_json, digest
from .capabilities import capability
from .errors import UNSET_POLICY_PIN, BlockingError
from .policy import POLICY_PIN_NAMES

_INT_PINS = (
    "VERIFIER_MAX_OUTPUT_TOKENS",
    "VERIFIER_CONTEXT_MARGIN_TOKENS",
    "VERIFIER_COST_CAP_MICRO_USD",              # integer MICRO-USD
    "VERIFIER_MAX_REVIEW_UNITS",
    "VERIFIER_MAX_GENERATION_CALLS",
    "VERIFIER_MAX_COUNT_CALLS",
    "VERIFIER_COUNT_TIMEOUT_SECONDS",
    "VERIFIER_COUNT_MAX_RETRIES",
    "VERIFIER_GENERATION_TIMEOUT_SECONDS",
    "VERIFIER_GENERATION_MAX_RETRIES",
    "VERIFIER_TOKEN_DRIFT_TOLERANCE",
)


def _fail(reason: str):
    raise BlockingError(UNSET_POLICY_PIN, reason)


def validate_pins(pins: dict, model_ids: list[str]) -> dict:
    """Validate the COMPLETE record before any provider call."""
    if not isinstance(pins, dict):
        _fail("category=pin_record_not_object")
    missing = set(POLICY_PIN_NAMES) - set(pins)
    if missing:
        _fail(f"category=unset_policy_pin missing={sorted(missing)}")
    unknown = set(pins) - set(POLICY_PIN_NAMES)
    if unknown:
        _fail(f"category=unknown_policy_pin unknown={sorted(unknown)}")
    for name in _INT_PINS:
        value = pins[name]
        if isinstance(value, bool) or not isinstance(value, int):
            _fail(f"category=pin_type_invalid pin={name} (integers only; "
                  "money is integer micro-USD, never a float)")
        if value < 0:
            _fail(f"category=pin_negative pin={name}")
    if pins["VERIFIER_MAX_OUTPUT_TOKENS"] <= 0:
        _fail("category=pin_must_be_positive pin=VERIFIER_MAX_OUTPUT_TOKENS")
    if pins["VERIFIER_MAX_REVIEW_UNITS"] <= 0:
        _fail("category=pin_must_be_positive pin=VERIFIER_MAX_REVIEW_UNITS")

    efforts = pins["VERIFIER_REASONING_EFFORT_BY_MODEL"]
    if not isinstance(efforts, dict):
        _fail("category=pin_type_invalid pin=VERIFIER_REASONING_EFFORT_BY_MODEL")
    if set(efforts) != set(model_ids):
        _fail("category=reasoning_effort_model_set_mismatch")
    for model_id in model_ids:
        cap = capability(model_id)
        if pins["VERIFIER_MAX_OUTPUT_TOKENS"] > cap.max_output_tokens_supported:
            _fail(f"category=max_output_exceeds_model_support "
                  f"model={model_id} supported={cap.max_output_tokens_supported}")
        effort = efforts[model_id]
        if not cap.supports_reasoning:
            if effort is not None:
                _fail(f"category=reasoning_effort_for_non_reasoning_model "
                      f"model={model_id}")
            continue
        if effort not in cap.reasoning_efforts:
            _fail(f"category=reasoning_effort_unsupported model={model_id}")
    return dict(pins)


def test_pin_record(pins: dict, model_ids: list[str]) -> dict:
    """PIN values for LOCAL MOCK planning. Not an operator decision.

    Validation proves the values are internally coherent; it proves nothing
    about who chose them. A plan built on this record can never be
    executable — that is the entire distinction from an operator
    authorization, and it is why the two are different functions rather than
    one function with a flag."""
    validated = validate_pins(pins, model_ids)
    record = {
        "schema_version": 1,
        "pins": validated,
        "money_unit": "integer micro-USD",
        "authority_class": authority.TEST_FIXTURE_UNAUTHORIZED,
        "executable_authority": False,
        "honest_scope": "values are internally valid; no operator approved "
                        "them, and no plan built on them may be executable",
    }
    record["pin_record_sha256"] = pin_digest(record)
    return record


def operator_pin_authorization(pins: dict, model_ids: list[str], *,
                               repository_identity: str, target_base_sha: str,
                               head_sha: str, operator_identity: str,
                               authorized_at: str, policy_version: str,
                               external_anchor: dict) -> dict:
    """An operator's approval of exactly these values for exactly this range.

    Nothing in this package can mint one of these without an external anchor,
    and the validator refuses a record that claims trusted authority without
    one. Authority arrives from outside or not at all."""
    validated = validate_pins(pins, model_ids)
    record = {
        "schema_version": 1,
        "pins": validated,
        "money_unit": "integer micro-USD",
        "authority_class": authority.TRUSTED_OPERATOR_AUTHORIZATION,
        "executable_authority": True,
        "repository_identity": repository_identity,
        "target_base_sha": target_base_sha,
        "head_sha": head_sha,
        "operator_identity": operator_identity,
        "authorized_at": authorized_at,
        "policy_version": policy_version,
        "external_anchor": external_anchor,
        "revoked": False,
    }
    authority._check_anchor(record, "operator_pin_authorization")
    record["pin_record_sha256"] = pin_digest(record)
    return record


def validate_pin_authority(record: dict, model_ids: list[str]) -> dict:
    """Recompute a PIN record's authority rather than reading its label."""
    if not isinstance(record, dict):
        _fail("category=pin_record_not_object")
    for field in ("pins", "authority_class", "executable_authority",
                  "pin_record_sha256"):
        if field not in record:
            _fail(f"category=pin_record_field_missing field={field}")
    if record["authority_class"] not in authority.AUTHORITY_CLASSES:
        _fail("category=pin_authority_class_unknown")
    validate_pins(record["pins"], model_ids)
    expected_executable = (record["authority_class"]
                           == authority.TRUSTED_OPERATOR_AUTHORIZATION)
    if record["executable_authority"] is not expected_executable:
        _fail("category=pin_executable_authority_not_derived "
              f"class={record['authority_class']} "
              f"claimed={record['executable_authority']}")
    if record.get("revoked"):
        _fail("category=pin_authorization_revoked")
    authority._check_anchor(record, "pin_record")
    if pin_digest(record) != record["pin_record_sha256"]:
        _fail("category=pin_record_digest_mismatch")
    return record


def pin_digest(record: dict) -> str:
    stripped = {k: v for k, v in record.items() if k != "pin_record_sha256"}
    return digest(b"verifier-pin-record-v2", canonical_json(stripped))
