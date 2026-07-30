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
import unicodedata

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
                      challenge: str, review_policy: dict,
                      model_id: str | None = None) -> dict:
    """Every unit answered exactly once, within policy, echoing the challenge.

    Returns the verdicts_by_unit map on success; blocks otherwise. This is
    the check that does not trust the schema to have been enforced."""
    if not isinstance(parsed, dict):
        _fail("category=response_not_object")
    expected_top = {"challenge", "verdicts_by_unit"}
    if model_id is not None:
        expected_top.add("lens_id")
    if set(parsed) != expected_top:
        _fail(f"category=response_top_level_keys keys={sorted(parsed)}")
    if model_id is not None:
        from . import reviewpolicy
        expected_lens = reviewpolicy.lens_id(model_id)
        if parsed.get("lens_id") != expected_lens:
            _fail(f"category=response_lens_id_mismatch "
                  f"expected={expected_lens} got={parsed.get('lens_id')!r} — "
                  "the reviewer did not answer under the lens it was assigned")
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
                      confidences, model_id=model_id)
    return verdicts


def _validate_one(unit_hash: str, verdict: dict, challenge: str,
                  review_policy: dict, confidences: set,
                  model_id: str | None = None) -> None:
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
    # MC4-R08: the categories must come from THIS model's closed vocabulary,
    # and at least one from its required group. Free text meant every model
    # could return the same two words and the evidence could not show that a
    # security lens had been applied.
    if model_id is not None:
        from . import reviewpolicy
        allowed = set(reviewpolicy.lens_categories(model_id))
        outside = sorted(set(categories) - allowed)
        if outside:
            _fail(f"category=checked_category_outside_lens_vocabulary "
                  f"{where} model={model_id} outside={outside}")
        required = set(reviewpolicy.lens_required_group(model_id))
        if not (set(categories) & required):
            _fail(f"category=no_required_lens_category {where} "
                  f"model={model_id} required_any_of={sorted(required)} — the "
                  "verdict does not show the lens this model was assigned was "
                  "actually applied")


# ------------------------------------------------ anti-canned reasoning ------

_WHITESPACE = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_NON_ASCII = re.compile(r"[^\x20-\x7e]")

#: Unicode categories that carry no visible content. A zero-width joiner or a
#: bidi mark changes the bytes and nothing a reader sees, so leaving them in
#: makes two identical sentences compare unequal (MC4-R09).
_IGNORABLE_CATEGORIES = frozenset({"Cf", "Cc", "Mn"})

#: What this gate IS. Naming it honestly matters: prose cannot be checked for
#: independent thought, and a name that claimed otherwise would license
#: treating a pass as assurance it is not.
GATE_SEMANTICS = "ANTI_COPY_TRIPWIRE_NOT_PROOF_OF_INDEPENDENT_REASONING"

#: Governed near-copy threshold, on token-set overlap of two normalized
#: approvals. Exact equality alone is defeated by a single reordered word.
#: Carried in the policy record so it is an operator decision, not a constant
#: buried here.
DEFAULT_SIMILARITY_THRESHOLD_BP = 8500       # 0.85 Jaccard, in basis points

#: Output for reason/proof is restricted to ASCII printable under policy v2.
#: The alternative — a UTS #39 confusable skeleton — needs a pinned
#: implementation and an approval record; until one exists, refusing the
#: character classes that make homoglyph attacks possible is the honest
#: fail-closed choice, and it is enforced rather than assumed.
REASON_CHARSET_POLICY = "ASCII_PRINTABLE_ONLY_POLICY_V2"


def assert_reason_charset(text: str, *, where: str) -> None:
    """Refuse non-ASCII in a field the distinctness gate compares.

    A Cyrillic 'а' reads as a Latin 'a' and hashes differently, so one
    substitution turns a canned sentence into a "distinct" one. Under policy
    v2 these fields are ASCII printable; anything else blocks rather than
    being silently folded, because folding needs a pinned confusable table
    this package does not have."""
    if _NON_ASCII.search(text):
        _fail(f"category=reason_charset_outside_policy {where} "
              f"policy={REASON_CHARSET_POLICY} — reason and proof are ASCII "
              "printable under review policy v2; a confusable character makes "
              "two identical sentences compare as distinct")


def normalize_reason(text: str, *, challenge: str,
                     model_ids=(), lens_ids=()) -> str:
    """Reduce a reason to what it actually SAYS.

    Removed, in order: Unicode compatibility differences (NFKC), invisible
    format and combining marks, the challenge every model is required to
    echo, the model and lens identifiers a template can interpolate, then
    case and punctuation. Each removal closes a way to make one canned
    sentence look like several (MC4-R09): the mock's own reasons differed
    only by an interpolated model id, which is exactly the bypass."""
    folded = unicodedata.normalize("NFKC", text)
    folded = "".join(c for c in folded
                     if unicodedata.category(c) not in _IGNORABLE_CATEGORIES)
    folded = folded.replace(challenge, " ")
    for token in sorted({*model_ids, *lens_ids}, key=len, reverse=True):
        if token:
            folded = folded.replace(token, " ")
    lowered = _NON_WORD.sub(" ", folded.casefold())
    return _WHITESPACE.sub(" ", lowered).strip()


def similarity_bp(left: str, right: str) -> int:
    """Token-set overlap of two normalized strings, in basis points.

    Jaccard over word sets: order-insensitive, so a reordered canned sentence
    scores 10000 rather than 0. Deterministic, with no external dependency."""
    a, b = set(left.split()), set(right.split())
    if not a and not b:
        return 10_000
    if not a or not b:
        return 0
    return (len(a & b) * 10_000) // len(a | b)


def assert_distinct_reasoning(by_model: dict, *, unit_hash: str,
                              approver: str, challenge: str,
                              corroborators: list[str],
                              model_ids=(), lens_ids=(),
                              similarity_threshold_bp: int = (
                                  DEFAULT_SIMILARITY_THRESHOLD_BP)) -> dict:
    """An ANTI-COPY TRIPWIRE on approvals, per unit (C4-F12, MC4-R09).

    Named for what it is. The legacy panel required substantive, mutually
    distinct green reasons, and that gate was lost — each reason was
    length-checked in isolation, so every model returning the same canned
    sentence passed. Restoring it catches copies. It does NOT prove
    independent reasoning: prose cannot be checked for thought, and the
    earlier docstring's implication that it could would license reading a
    pass as assurance it is not. Structural lens evidence — the per-model
    category vocabulary — is the stronger signal, and semantic independence
    remains a trust assumption.

    Four bypasses are closed here. Normalization folds Unicode compatibility
    forms and drops invisible characters, so a Cyrillic homoglyph or a
    zero-width joiner no longer makes one sentence into two — and the charset
    policy refuses non-ASCII in these fields outright rather than relying on
    the fold. Model and lens identifiers are removed, because a template that
    interpolates them turns one sentence into N (the mock did exactly that).
    Every approving model is compared with every other, not only with the
    approver. And exact equality is backed by a governed token-set similarity
    threshold, so a reordered or lightly paraphrased copy still trips.

    Applied only to APPROVALS. A refutation is a finding, and two models
    independently describing the same real defect in the same words is
    agreement, not collusion — blocking that would penalise the case the
    panel exists to catch."""
    approving = [approver] + [m for m in corroborators
                              if not by_model[m][unit_hash]["refuted"]]
    normalized: list[tuple[str, str, str]] = []
    for model_id in approving:
        verdict = by_model[model_id][unit_hash]
        where = f"unit={unit_hash[:16]} model={model_id}"
        assert_reason_charset(verdict["reason"], where=where)
        assert_reason_charset(verdict["proof_of_check"], where=where)
        reason = normalize_reason(verdict["reason"], challenge=challenge,
                                  model_ids=model_ids, lens_ids=lens_ids)
        if not reason:
            _fail(f"category=approval_reason_is_only_the_challenge {where} — "
                  "what remains after removing the challenge, the model id "
                  "and the lens name is nothing")
        proof = normalize_reason(verdict["proof_of_check"],
                                 challenge=challenge, model_ids=model_ids,
                                 lens_ids=lens_ids)
        normalized.append((model_id, reason, proof))

    for index, (model_id, reason, proof) in enumerate(normalized):
        for other_id, other_reason, other_proof in normalized[:index]:
            score = similarity_bp(reason, other_reason)
            if score >= similarity_threshold_bp:
                _fail(f"category=canned_identical_approval "
                      f"unit={unit_hash[:16]} model={model_id} "
                      f"matches={other_id} similarity_bp={score} "
                      f"threshold_bp={similarity_threshold_bp} — two "
                      "approvals that say the same thing are one review "
                      "reported twice; independent corroboration is the "
                      "property this panel exists to provide")
            if proof and other_proof:
                proof_score = similarity_bp(proof, other_proof)
                if proof_score >= similarity_threshold_bp:
                    _fail(f"category=canned_identical_proof "
                          f"unit={unit_hash[:16]} model={model_id} "
                          f"matches={other_id} similarity_bp={proof_score} — "
                          "the same statement of what was inspected, from two "
                          "models, is one inspection")

    distinct = [m for m in approving if m != approver]
    return {"unit_sha256": unit_hash,
            "gate_semantics": GATE_SEMANTICS,
            "similarity_threshold_bp": similarity_threshold_bp,
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
