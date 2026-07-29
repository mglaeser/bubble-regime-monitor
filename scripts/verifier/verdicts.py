"""Strict post-parse verdict validation (MC4-A2 §A2-F09, PASS C §8).

A JSON Schema constrains SHAPE. It cannot say "the proof must begin with the
challenge this exact request minted", and — depending on how strictly a
provider enforces `strict: true` — it may not be the last line of defence.
The executor therefore re-validates every verdict against the review policy
after parsing, so a canned "all green" response that never saw the request
fails on a field it could not have known.

The parse itself is strict: duplicate JSON keys are rejected, because a
response with two entries for one unit hash is not a single verdict and JSON
object semantics would otherwise silently keep the last.
"""

from __future__ import annotations

import json
import re

from .canon import digest, sha256_hex
from .errors import PROVIDER_RESPONSE_INVALID, BlockingError


def _fail(reason: str):
    raise BlockingError(PROVIDER_RESPONSE_INVALID, reason)


def _no_duplicate_keys(pairs):
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            _fail(f"category=duplicate_json_key key={key[:32]} — a second "
                  "entry for one key is not a single verdict")
        seen[key] = value
    return seen


def parse_strict(body: bytes) -> dict:
    """Parse a response body, rejecting duplicate keys anywhere in it."""
    try:
        return json.loads(body, object_pairs_hook=_no_duplicate_keys)
    except BlockingError:
        raise
    except Exception as exc:
        _fail(f"category=response_unparseable "
              f"exception_class={type(exc).__name__}")


def validate_verdicts(parsed: dict, *, unit_hashes: list[str],
                      challenge: str, review_policy: dict) -> dict:
    """Every unit answered exactly once, within policy, echoing the challenge.

    Returns the verdicts_by_unit map on success; blocks otherwise. This is
    the check that does not trust the schema to have been enforced."""
    if not isinstance(parsed, dict):
        _fail("category=response_not_object")
    if set(parsed) != {"challenge", "verdicts_by_unit"}:
        _fail(f"category=response_top_level_keys keys={sorted(parsed)}")
    if parsed["challenge"] != challenge:
        _fail("category=response_challenge_mismatch — the reviewer did not "
              "echo the challenge this request minted; a canned response "
              "cannot have known it")

    verdicts = parsed["verdicts_by_unit"]
    if not isinstance(verdicts, dict):
        _fail("category=verdicts_by_unit_not_object")
    expected = set(unit_hashes)
    got = set(verdicts)
    if got != expected:
        _fail(f"category=verdict_unit_set_mismatch "
              f"missing={len(expected - got)} extra={len(got - expected)}")

    confidences = set(review_policy["confidence_values"])
    for unit_hash, verdict in verdicts.items():
        _validate_one(unit_hash, verdict, challenge, review_policy,
                      confidences)
    return verdicts


def _validate_one(unit_hash: str, verdict: dict, challenge: str,
                  review_policy: dict, confidences: set) -> None:
    where = f"unit={unit_hash[:16]}"
    if not isinstance(verdict, dict):
        _fail(f"category=verdict_not_object {where}")
    required = {"refuted", "confidence", "reason", "proof_of_check",
                "checked_categories"}
    if set(verdict) != required:
        _fail(f"category=verdict_keys {where} keys={sorted(verdict)}")
    if not isinstance(verdict["refuted"], bool):
        _fail(f"category=refuted_not_boolean {where}")
    if verdict["confidence"] not in confidences:
        _fail(f"category=confidence_not_in_enum {where}")

    reason = verdict["reason"]
    if not isinstance(reason, str) or not (
            review_policy["reason_min_chars"] <= len(reason)
            <= review_policy["reason_max_chars"]):
        _fail(f"category=reason_length_out_of_policy {where} "
              f"len={len(reason) if isinstance(reason, str) else 'n/a'}")
    if not reason.strip():
        _fail(f"category=reason_whitespace_only {where}")

    proof = verdict["proof_of_check"]
    if not isinstance(proof, str) or not (
            review_policy["proof_min_chars"] <= len(proof)
            <= review_policy["proof_max_chars"]):
        _fail(f"category=proof_length_out_of_policy {where} "
              f"len={len(proof) if isinstance(proof, str) else 'n/a'}")
    # The proof must OPEN with the challenge, so a reviewer that did not see
    # the request cannot have produced it.
    if not proof.startswith(challenge):
        _fail(f"category=proof_does_not_echo_challenge {where}")
    if not proof[len(challenge):].strip():
        _fail(f"category=proof_is_only_the_challenge {where} — echoing the "
              "challenge is necessary but not a proof of checking")

    categories = verdict["checked_categories"]
    if not isinstance(categories, list) or not (
            review_policy["min_checked_categories"] <= len(categories)
            <= review_policy["max_checked_categories"]):
        _fail(f"category=checked_categories_count_out_of_policy {where}")
    if len(set(categories)) != len(categories):
        _fail(f"category=checked_categories_not_unique {where}")
    for entry in categories:
        if not isinstance(entry, str) or not (
                1 <= len(entry) <= review_policy["category_max_chars"]):
            _fail(f"category=checked_category_out_of_policy {where}")


# ------------------------------------------------ anti-canned reasoning ------

_WHITESPACE = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^a-z0-9 ]+")


def normalize_reason(text: str, *, challenge: str) -> str:
    """Reduce a reason to what it actually SAYS.

    The challenge is stripped first: every model is required to echo it, so
    leaving it in makes two otherwise identical sentences look different by
    exactly the part they were told to copy."""
    stripped = text.replace(challenge, " ")
    lowered = _NON_WORD.sub(" ", stripped.lower())
    return _WHITESPACE.sub(" ", lowered).strip()


def assert_distinct_reasoning(by_model: dict, *, unit_hash: str,
                              approver: str, challenge: str,
                              corroborators: list[str]) -> dict:
    """Independent review must LOOK independent, per unit (C4-F12).

    The legacy panel required substantive, mutually distinct green reasons.
    That gate was lost: each reason was length-checked in isolation, so every
    model returning the same canned sentence — with the challenge dutifully
    echoed — passed all of them. Echoing a challenge proves the response was
    minted for this request; it proves nothing about two models having
    reviewed independently.

    Applied only to APPROVALS. A refutation is a finding, and two models
    independently describing the same real defect in the same words is
    agreement, not collusion — blocking that would penalise the case the
    panel exists to catch."""
    approver_verdict = by_model[approver][unit_hash]
    approver_reason = normalize_reason(approver_verdict["reason"],
                                       challenge=challenge)
    if not approver_reason:
        _fail(f"category=approver_reason_is_only_the_challenge "
              f"unit={unit_hash[:16]} approver={approver}")

    distinct = []
    for model_id in corroborators:
        verdict = by_model[model_id][unit_hash]
        if verdict["refuted"]:
            continue
        reason = normalize_reason(verdict["reason"], challenge=challenge)
        if not reason:
            _fail(f"category=corroborator_reason_is_only_the_challenge "
                  f"unit={unit_hash[:16]} model={model_id}")
        if reason == approver_reason:
            _fail(f"category=canned_identical_approval unit={unit_hash[:16]} "
                  f"model={model_id} approver={approver} — two approvals "
                  "whose reasons say the same thing are one review reported "
                  "twice; independent corroboration is the property this "
                  "panel exists to provide")
        proof = normalize_reason(verdict["proof_of_check"],
                                 challenge=challenge)
        approver_proof = normalize_reason(approver_verdict["proof_of_check"],
                                          challenge=challenge)
        if proof and proof == approver_proof:
            _fail(f"category=canned_identical_proof unit={unit_hash[:16]} "
                  f"model={model_id} — the same statement of what was "
                  "inspected, from two models, is one inspection")
        distinct.append(model_id)
    return {"unit_sha256": unit_hash,
            "distinct_reasoning_models": sorted(distinct),
            "distinct_reasoning_count": len(distinct)}


def verdict_evidence(verdicts: dict, *, model_id: str, batch_id: str,
                     challenge: str | None = None,
                     request_semantics_sha256: str | None = None,
                     usage: dict | None = None,
                     attempt: dict | None = None) -> dict:
    """The FULL validated verdict, retained (C4-F11).

    The earlier record kept a count and a list of refuted hashes and dropped
    the confidence, the reason, the proof and the categories — every model
    had explained itself, the validation had checked the explanation, and
    then the evidence threw it away. A reviewer of the final evidence could
    see THAT Sol approved and never see WHY, which is the one thing an
    approval record is for.

    This record is PRIVATE. Reasons and proofs are provider-controlled text;
    a public summary carries digests and counts only (C4-F22)."""
    refuted = sorted(h for h, v in verdicts.items() if v["refuted"])
    record = {
        "model_id": model_id,
        "batch_id": batch_id,
        "challenge": challenge,
        "request_semantics_sha256": request_semantics_sha256,
        "unit_count": len(verdicts),
        "refuted_unit_sha256": refuted,
        "refuted_count": len(refuted),
        "verdicts_by_unit": {
            unit_hash: {
                "refuted": verdict["refuted"],
                "confidence": verdict["confidence"],
                "reason": verdict["reason"],
                "proof_of_check": verdict["proof_of_check"],
                "checked_categories": list(verdict["checked_categories"]),
                "reason_sha256": sha256_hex(
                    verdict["reason"].encode("utf-8", "surrogateescape")),
                "proof_sha256": sha256_hex(
                    verdict["proof_of_check"].encode("utf-8",
                                                     "surrogateescape")),
            }
            for unit_hash, verdict in sorted(verdicts.items())
        },
        "usage": dict(usage or {}),
        "attempt": dict(attempt or {}),
        "publication_class": "private",
    }
    record["verdict_evidence_sha256"] = digest(b"verdict-evidence-v2",
                                               _canonical(record))
    return record


def _canonical(record: dict) -> bytes:
    from .canon import canonical_json
    return canonical_json(record)
