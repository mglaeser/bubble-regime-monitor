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

from .canon import digest
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


def verdict_evidence(verdicts: dict, *, model_id: str, batch_id: str) -> dict:
    refuted = sorted(h for h, v in verdicts.items() if v["refuted"])
    record = {
        "model_id": model_id,
        "batch_id": batch_id,
        "unit_count": len(verdicts),
        "refuted_unit_sha256": refuted,
        "refuted_count": len(refuted),
    }
    record["verdict_evidence_sha256"] = digest(b"verdict-evidence-v1",
                                               _canonical(record))
    return record


def _canonical(record: dict) -> bytes:
    from .canon import canonical_json
    return canonical_json(record)
