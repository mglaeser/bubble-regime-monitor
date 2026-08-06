"""The versioned ReviewRequestPolicy (MC4 §33) and its output budget (§25).

Macro-Cycle 3 sent every model the same sentence and asked for `refuted` plus
a free-text `reason`. That is a material regression from the legacy panel,
which required a named approver, distinct corroborating models, a per-run
challenge, and a proof that the reviewer actually looked. Those controls exist
because a model that answers "not refuted" to everything is indistinguishable,
on those two fields alone, from a model that reviewed carefully.

This module freezes what a review request IS, so the question is part of the
hashed request rather than a property of whoever assembled it:

  * one lens per model, so three reviews are three perspectives rather than
    three samples of one;
  * a per-run challenge the reviewer must echo, minted by the trusted lane
    and unpredictable to the reviewed branch;
  * a required approver and a minimum number of distinct corroborators;
  * bounded reason and proof fields, because unbounded output is how a batch
    that "fits" silently truncates its last verdicts.

The output budget is the part that is easy to get wrong. Batch packing in MC3
checked that INPUT fit the context window and assumed the fixed 8,000-token
output allowance would cover however many verdicts the batch needed. It does
not: verdicts scale with unit count. `max_units_per_batch()` inverts the
per-unit output cost so capacity is planned, not hoped for.
"""

from __future__ import annotations

from .canon import canonical_json, digest
from .errors import UNSET_POLICY_PIN, BlockingError

POLICY_VERSION = "review-request-policy-v2"

# ------------------------------------------------------------- lenses v2 -----
#
# MC4-R08: a lens is only real if the EVIDENCE can distinguish it.
#
# v1 gave each model a different instruction string and accepted
# `checked_categories` as arbitrary bounded text. Every model could return
# ["logic", "state"] — the mock did — so nothing in the evidence showed that
# Sol performed a security/data-flow review or that Mini checked invariants.
# Different instructions are an input; lens diversity is a claim about output,
# and it needs the output to carry it.
#
# v2 gives each model a lens_id and a CLOSED category vocabulary, requires at
# least one category from that model's own required group, and generates the
# response schema per model so the enum is enforced by the structured-output
# contract rather than checked afterwards.

LENS_IDS: dict[str, str] = {
    "gpt-5.3-codex": "adversarial-implementer-v2",
    "gpt-5.6-sol": "security-data-flow-v2",
    "gpt-4.1-mini": "invariant-fail-closed-v2",
}

#: Canonical identifiers, not free text. A category is a claim about what was
#: inspected, so it has to mean the same thing in every verdict.
LENS_CATEGORIES: dict[str, tuple[str, ...]] = {
    "gpt-5.3-codex": (
        "logic", "state_transition", "error_path", "operability",
        "algorithm", "interface_contract",
    ),
    "gpt-5.6-sol": (
        "authentication", "authorization", "data_flow", "secret_handling",
        "injection", "deserialization", "privacy", "trust_boundary",
    ),
    "gpt-4.1-mini": (
        "fail_closed", "coverage_denominator", "provenance",
        "bypass_resistance", "claim_consistency", "test_load_bearing",
    ),
}

#: At least one of these must appear, so a model cannot satisfy its own enum
#: with only its most peripheral category.
LENS_REQUIRED_GROUPS: dict[str, tuple[str, ...]] = {
    "gpt-5.3-codex": ("logic", "state_transition", "error_path"),
    "gpt-5.6-sol": ("data_flow", "secret_handling", "authorization",
                    "trust_boundary"),
    "gpt-4.1-mini": ("fail_closed", "coverage_denominator",
                     "bypass_resistance"),
}


def lens_id(model_id: str) -> str:
    if model_id not in LENS_IDS:
        _fail(f"category=model_without_lens_id model={model_id}")
    return LENS_IDS[model_id]


def lens_categories(model_id: str) -> tuple[str, ...]:
    if model_id not in LENS_CATEGORIES:
        _fail(f"category=model_without_category_enum model={model_id}")
    return LENS_CATEGORIES[model_id]


def lens_required_group(model_id: str) -> tuple[str, ...]:
    if model_id not in LENS_REQUIRED_GROUPS:
        _fail(f"category=model_without_required_group model={model_id}")
    return LENS_REQUIRED_GROUPS[model_id]


def lens_record(model_id: str) -> dict:
    """The lens as hashable policy, not as a prose string."""
    return {
        "lens_id": lens_id(model_id),
        "lens_sha256": digest(b"review-lens-v2", LENSES[model_id].encode()),
        "allowed_categories": list(lens_categories(model_id)),
        "required_category_group": list(lens_required_group(model_id)),
    }

# In-repository copy. Executable evidence requires the trusted lane's copy;
# this one is for local planning and tests, and says so.
STATUS_PROVISIONAL = "PROVISIONAL_IN_REPOSITORY"

CONFIDENCE_VALUES = ("low", "medium", "high")

# The governed required approver, carried over from the legacy panel. Sol is
# the security/data-flow lens; a change that ships without its approval ships
# without the review that would have caught an authorization or injection
# defect. There is NO finalizer default: MC4's first attempt defaulted to
# model_ids[0], which is gpt-5.3-codex, so the wrong model silently became the
# approver (A2-F08).
GOVERNED_REQUIRED_APPROVER = "gpt-5.6-sol"

# One lens per model. Distinct perspectives catch failure modes that three
# runs of the same prompt cannot.
LENSES: dict[str, str] = {
    "gpt-5.3-codex": (
        "Review as an adversarial implementer. Look for logic that is wrong "
        "on some input, state that is mutated unexpectedly, error paths that "
        "swallow failures, and operations whose failure mode is silent. For "
        "each unit decide whether the CHANGED lines introduce a defect."
    ),
    "gpt-5.6-sol": (
        "Review as a security and data-flow analyst. Look for untrusted input "
        "reaching a sensitive sink, credentials or secrets in code or logs, "
        "authorization that fails open, injection, and unsafe deserialization. "
        "For each unit decide whether the CHANGED lines introduce a defect."
    ),
    "gpt-4.1-mini": (
        "Review as an invariant checker. Look for broken fail-closed "
        "behaviour, coverage that silently shrinks, a check that can be "
        "bypassed, and a claim in a comment or message that the code does not "
        "support. For each unit decide whether the CHANGED lines introduce a "
        "defect."
    ),
}

# Output budget, in tokens, measured per verdict. These are policy decisions,
# not measurements: they bound what a reviewer may return so the batch size
# can be derived from them.
REASON_MIN_CHARS = 24
REASON_MAX_CHARS = 600
PROOF_MIN_CHARS = 16
PROOF_MAX_CHARS = 240
MIN_CHECKED_CATEGORIES = 1
MAX_CHECKED_CATEGORIES = 6
CATEGORY_MAX_CHARS = 48
# C4-F18: there is deliberately no per-unit findings budget.
#
# The output projection used to reserve MAX_FINDINGS_PER_UNIT * 96 characters
# for a structured findings list. The verdict schema has no findings field, so
# the model was never permitted to return one — the reservation shrank every
# batch to make room for output that could not exist. Budgeting an output
# field the schema forbids is a disagreement between two policies, not
# conservatism, so the reservation is gone rather than the field added: a new
# wire field is a change to what reviewers must produce and belongs to a
# governed policy version, not to a capacity calculation.

# Conservative characters-per-token for budget arithmetic. Deliberately LOW
# (pessimistic) so the projection over-reserves rather than under-reserves.
#
# HONEST SCOPE: this is a POLICY PROJECTION, not a measurement. No provider
# has been asked how many tokens a verdict costs. Every number this module
# produces — including max_units_per_batch — follows from the bounded field
# sizes above and this constant, so it is a structural capacity bound under
# provisional review-policy v1 and nothing more. It must be validated against
# actual usage.output_tokens once trusted execution evidence exists.
_CHARS_PER_TOKEN = 3

# Fixed overhead per response: the challenge echo, the enclosing object, and
# the JSON scaffolding around the verdict map.
_RESPONSE_OVERHEAD_TOKENS = 256


def _fail(reason: str):
    raise BlockingError(UNSET_POLICY_PIN, reason)


def per_unit_output_tokens() -> int:
    """Worst-case tokens one unit's verdict may occupy.

    Every bounded field is counted at its maximum, plus the JSON keys and the
    64-character unit hash that identifies it."""
    chars = (
        64                                  # unit_sha256 key
        + REASON_MAX_CHARS
        + PROOF_MAX_CHARS
        + MAX_CHECKED_CATEGORIES * 32
        + 160                               # keys, quoting, punctuation
    )
    return -(-chars // _CHARS_PER_TOKEN)    # ceil


def max_units_per_batch(max_output_tokens: int) -> int:
    """How many units can actually RETURN a verdict within the output budget.

    MC3 planned input capacity and left output to chance. A batch whose input
    fits but whose verdicts cannot fit produces a truncated response — which,
    with a strict schema, is a hard failure late, and without one would be a
    silently short answer."""
    usable = max_output_tokens - _RESPONSE_OVERHEAD_TOKENS
    if usable <= 0:
        _fail(f"category=output_budget_below_overhead "
              f"max_output_tokens={max_output_tokens} "
              f"overhead={_RESPONSE_OVERHEAD_TOKENS}")
    per_unit = per_unit_output_tokens()
    units = usable // per_unit
    if units < 1:
        _fail(f"category=output_budget_cannot_fit_one_verdict "
              f"max_output_tokens={max_output_tokens} "
              f"per_unit_tokens={per_unit}")
    return units


def worst_case_output_tokens(unit_count: int) -> int:
    return _RESPONSE_OVERHEAD_TOKENS + unit_count * per_unit_output_tokens()


def assert_output_capacity(unit_count: int, max_output_tokens: int,
                           *, where: str) -> int:
    needed = worst_case_output_tokens(unit_count)
    if needed > max_output_tokens:
        _fail(f"category=output_capacity_exceeded where={where} "
              f"units={unit_count} needed_tokens={needed} "
              f"max_output_tokens={max_output_tokens} — a batch whose "
              "verdicts cannot fit must be split, never truncated")
    return needed


def policy_record(model_ids: list[str], *, required_approver: str,
                  minimum_other_approvers: int,
                  max_output_tokens: int, status: str = STATUS_PROVISIONAL,
                  external_anchor: dict | None = None,
                  similarity_threshold_bp: int = 8500) -> dict:
    """The complete, hashed review policy bound into every request.

    `minimum_other_approvers` counts approvals from models OTHER than the
    required approver. MC4's first attempt used `minimum_distinct_corroborators`
    without stating whether the required approver counted toward it (A2-F08),
    so "2" was ambiguous. The name now says exactly what it means: with a
    required approver plus one other approver, total approvals are two."""
    # C4-F17: the panel is an ORDERED SET. A repeated model id produces a
    # duplicate request, collapses in every by-model dict, and inflates the
    # panel size the corroborator arithmetic is computed against — so "two
    # distinct other approvers" could be one model counted twice.
    if len(model_ids) != len(set(model_ids)):
        duplicated = sorted({m for m in model_ids
                             if model_ids.count(m) > 1})
        _fail(f"category=duplicate_model_in_panel models={duplicated} — a "
              "panel is a set of distinct reviewers; a repeated id is one "
              "reviewer counted twice")
    unknown = [m for m in model_ids if m not in LENSES]
    if unknown:
        _fail(f"category=model_without_review_lens models={unknown}")
    if required_approver not in model_ids:
        _fail(f"category=required_approver_not_in_panel "
              f"approver={required_approver}")
    if minimum_other_approvers < 1:
        _fail("category=minimum_other_approvers_below_one")
    if minimum_other_approvers > len(model_ids) - 1:
        _fail(f"category=minimum_other_approvers_above_panel_size "
              f"required={minimum_other_approvers} "
              f"other_models={len(model_ids) - 1}")
    record = {
        "policy_version": POLICY_VERSION,
        "status": status,
        "model_ids": list(model_ids),
        "lenses": {m: LENSES[m] for m in model_ids},
        "lens_sha256": {m: digest(b"review-lens-v1", LENSES[m].encode())
                        for m in model_ids},
        "lens_ids": {m: lens_id(m) for m in model_ids},
        "lenses_v2": {m: lens_record(m) for m in model_ids},
        "required_approver": required_approver,
        "minimum_other_approvers": minimum_other_approvers,
        "minimum_total_approvals": minimum_other_approvers + 1,
        "confidence_values": list(CONFIDENCE_VALUES),
        "reason_min_chars": REASON_MIN_CHARS,
        "reason_max_chars": REASON_MAX_CHARS,
        "proof_min_chars": PROOF_MIN_CHARS,
        "proof_max_chars": PROOF_MAX_CHARS,
        "min_checked_categories": MIN_CHECKED_CATEGORIES,
        "max_checked_categories": MAX_CHECKED_CATEGORIES,
        "category_max_chars": CATEGORY_MAX_CHARS,
        "findings_field_in_schema": False,
        "max_output_tokens": max_output_tokens,
        "per_unit_output_tokens": per_unit_output_tokens(),
        "response_overhead_tokens": _RESPONSE_OVERHEAD_TOKENS,
        "max_units_per_batch": max_units_per_batch(max_output_tokens),
        "capacity_basis": "POLICY_PROJECTION_NOT_PROVIDER_MEASUREMENT",
        "capacity_honest_scope": "derived from the bounded field sizes in "
                                 "this policy and a conservative 3 "
                                 "characters-per-token model; no provider was "
                                 "consulted, and it is unvalidated against "
                                 "actual output usage",
        "tools_policy": "no tools, no function calling, no web/file/computer "
                        "access, no background mode",
        "truncation": "disabled",
        "reason_charset_policy": "ASCII_PRINTABLE_ONLY_POLICY_V2",
        "anti_canned_gate_semantics":
            "ANTI_COPY_TRIPWIRE_NOT_PROOF_OF_INDEPENDENT_REASONING",
        "anti_canned_similarity_threshold_bp": similarity_threshold_bp,
        "synthesis_policy": "cross-unit synthesis may ADD findings; it may "
                            "never clear or downgrade a unit refutation",
    }
    if external_anchor is not None:
        record["external_anchor"] = external_anchor
    record["review_request_policy_sha256"] = policy_digest(record)
    return record


def policy_digest(record: dict) -> str:
    stripped = {k: v for k, v in record.items()
                if k != "review_request_policy_sha256"}
    return digest(b"review-request-policy-v1", canonical_json(stripped))


def validate_policy(record: dict, model_ids: list[str]) -> dict:
    if not isinstance(record, dict):
        _fail("category=review_policy_not_object")
    if record.get("policy_version") != POLICY_VERSION:
        _fail("category=review_policy_version_unknown")
    if list(record.get("model_ids", [])) != list(model_ids):
        _fail("category=review_policy_model_set_mismatch")
    for model_id in model_ids:
        expected = digest(b"review-lens-v1", LENSES[model_id].encode())
        if record["lens_sha256"].get(model_id) != expected:
            _fail(f"category=review_lens_digest_mismatch model={model_id}")
    if record["max_units_per_batch"] != max_units_per_batch(
            record["max_output_tokens"]):
        _fail("category=max_units_per_batch_not_derived")
    if policy_digest(record) != record["review_request_policy_sha256"]:
        _fail("category=review_policy_digest_mismatch")
    return record
