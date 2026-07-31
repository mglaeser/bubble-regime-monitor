"""The count ledger, and the preflight that runs BEFORE every request.

EX3-R01. D1's job is to find out what a review would cost, and the failure mode
that matters is not an inaccurate number — it is discovering the number by
spending it. A ledger that adds up what was spent is an accounting record; a
ledger that refuses the next call when the authorized total would be exceeded
is a control. This is the second kind.

**Global, not per-request.** The preflight compares the authorized ceiling
against the total already spent PLUS the worst case of the request about to go
out. Checking each request against the ceiling in isolation is the classic
version of this bug: a hundred requests that are each individually under budget
spend a hundred times the budget.

**The ceiling is a literal from an operator record.** Not a default, not a
constant in this file, not something computed from the plan. `authorize` takes
the exact value the operator wrote and refuses if there is not one, because a
budget the code chose is a budget nobody approved.

**Every entry names its unit.** A count that cannot be attributed to a unit
cannot be checked against the plan, and a duplicate unit means one of the two
counts is the one that is real and nothing says which.
"""

from __future__ import annotations

import hashlib
import json

from .errors import refuse

LEDGER_VERSION = "trusted-count-ledger-v1"

#: A single unit whose worst case exceeds this is refused rather than sent. It
#: is not a budget — it is the assertion that one unit of review is not the
#: whole budget, which is what a runaway splitter produces.
MAX_UNIT_INPUT_TOKENS = 1_000_000


def new_ledger(*, repository_numeric_id: int, candidate_head_sha: str,
               trusted_run_id, trusted_run_attempt) -> dict:
    from .identity import assert_commit_sha, assert_repository_numeric_id

    assert_repository_numeric_id(repository_numeric_id)
    assert_commit_sha(candidate_head_sha, field="candidate_head_sha")
    for label, value in (("trusted_run_id", trusted_run_id),
                         ("trusted_run_attempt", trusted_run_attempt)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            refuse(f"category=ledger_run_field_invalid field={label}")
    return {
        "ledger_version": LEDGER_VERSION,
        "repository_numeric_id": repository_numeric_id,
        "candidate_head_sha": candidate_head_sha,
        "trusted_run_id": trusted_run_id,
        "trusted_run_attempt": trusted_run_attempt,
        "entries": [],
        "total_input_tokens": 0,
        "generation_calls": 0,
    }


def authorize(*, record_set, prerequisite_key: str = "approve_count_spending",
              key: str = "max_input_tokens") -> dict:
    """Read the ceiling out of an AUTHENTICATED record set — never invent one.

    Two things changed here, and both are removals.

    **The type is the authority.** This used to take the output of
    `operatorrecord.verify_records`, whose own payload reports
    `authorized: False`. It now requires a `VerifiedOperatorRecordSet`, an
    instance of a real class produced only by
    `trustedverifier.authenticate_record_set`. A dict carrying
    `authority_class: VERIFIED_...` no longer satisfies it, because writing
    that string is free and passing `isinstance` is not.

    **The occurrence check is gone from here.** It used to compare a `scope`
    string. `LaneTrustedVerifier.assert_binds_this_review` now compares
    repository identity, target base, diff base and head against this review
    for EVERY record in the set, before any of them is promoted — so repeating
    a weaker version of that comparison in this function would be a second
    implementation of a policy that already lives one layer up, which is
    precisely the duplication the integration addendum forbids.

    **The ceiling comes out of a TYPED payload.** It used to be read from a
    free-form `exact_values` dictionary, so `max_input_tokens` could sit beside
    anything and the record for `approve_count_spending` was structurally the
    same record as the one for `delete_failed_run`. The typed schema for this
    prerequisite now names `max_input_tokens`, the two caps, the billing
    treatment and the authorized range, and the schema is closed both ways —
    the authenticator therefore covers a payload whose every field something
    reads.

    What remains is this function's actual job: find the value the operator
    wrote, and refuse if they did not write one."""
    from .trustedverifier import assert_authenticated

    assert_authenticated(record_set, phase="D1")
    envelope = record_set.require(prerequisite_key)
    ceiling = envelope.require_typed(key)
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 1:
        refuse(f"category=authorized_ceiling_not_a_positive_integer key={key}")
    return {"key": key, "authorized_input_tokens": ceiling,
            "prerequisite_key": prerequisite_key,
            "record_set_sha256": record_set.record_set_sha256,
            "authorization_subject_sha256":
                envelope.authorization_subject_sha256,
            "source": "AUTHENTICATED_OPERATOR_ENVELOPE_TYPED_VALUE"}


def preflight(ledger: dict, *, authorization: dict,
              worst_case_input_tokens: int) -> dict:
    """Refuse the next request if the authorized total would be exceeded.

    Called BEFORE the request, with the worst case rather than the actual — the
    actual is not known until the reply arrives, and by then it has been spent.
    """
    if isinstance(worst_case_input_tokens, bool) or not isinstance(
            worst_case_input_tokens, int) or worst_case_input_tokens < 1:
        refuse("category=preflight_worst_case_not_a_positive_integer — a "
               "preflight with no estimate authorizes an unknown amount")
    if worst_case_input_tokens > MAX_UNIT_INPUT_TOKENS:
        refuse(f"category=unit_worst_case_implausible "
               f"tokens={worst_case_input_tokens} max={MAX_UNIT_INPUT_TOKENS} — "
               "one unit of review is not the whole budget; this is what a "
               "runaway splitter produces")
    ceiling = authorization["authorized_input_tokens"]
    spent = ledger["total_input_tokens"]
    projected = spent + worst_case_input_tokens
    if projected > ceiling:
        refuse(f"category=authorized_input_tokens_would_be_exceeded "
               f"spent={spent} worst_case={worst_case_input_tokens} "
               f"projected={projected} authorized={ceiling} — checked against "
               "the RUNNING total, because a hundred requests that are each "
               "individually under budget spend a hundred times the budget")
    return {"cleared": True, "spent": spent, "projected": projected,
            "authorized": ceiling, "remaining": ceiling - projected}


def record_count(ledger: dict, *, unit_sha256: str, input_tokens: int,
                 payload_sha256: str, authorization: dict) -> dict:
    """Add one observed count, and refuse if it lands over the ceiling.

    The ceiling is checked again on the way in, not only in preflight. A reply
    reporting more than the worst case is either a different request's reply or
    an estimate that was wrong, and continuing on either reading spends money
    nobody authorized."""
    if not isinstance(unit_sha256, str) or len(unit_sha256) != 64:
        refuse("category=ledger_unit_sha_malformed — a count that cannot be "
               "attributed to a unit cannot be checked against the plan")
    if not isinstance(payload_sha256, str) or len(payload_sha256) != 64:
        refuse("category=ledger_payload_sha_malformed")
    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int):
        refuse("category=ledger_count_not_an_integer")
    if input_tokens < 0:
        refuse("category=ledger_count_negative")
    if any(entry["unit_sha256"] == unit_sha256 for entry in ledger["entries"]):
        refuse(f"category=ledger_unit_counted_twice unit={unit_sha256[:12]} — "
               "one of the two counts is the real one and nothing here says "
               "which")
    total = ledger["total_input_tokens"] + input_tokens
    ceiling = authorization["authorized_input_tokens"]
    if total > ceiling:
        refuse(f"category=authorized_input_tokens_exceeded observed_total={total} "
               f"authorized={ceiling} — the reply reports more than the "
               "preflight cleared, so either it answers a different request or "
               "the estimate was wrong; both spend money nobody authorized")
    updated = dict(ledger)
    updated["entries"] = [*ledger["entries"],
                          {"unit_sha256": unit_sha256,
                           "input_tokens": input_tokens,
                           "payload_sha256": payload_sha256}]
    updated["total_input_tokens"] = total
    return updated


def assert_zero_generation(ledger: dict) -> dict:
    """D1 counts. The phase says so; this makes the phase checkable."""
    if ledger.get("generation_calls") != 0:
        refuse(f"category=d1_made_a_generation_call "
               f"count={ledger.get('generation_calls')} — generation is D2 and "
               "a separate operator approval; an approval to count has never "
               "been an approval to generate")
    return {"generation_calls": 0, "phase_respected": True}


def assert_plan_fully_counted(ledger: dict, *, planned_units) -> dict:
    """Every planned unit counted, and no unit counted that was not planned.

    Both directions matter. A missing unit makes the total an underestimate
    that an operator would approve as if it were the whole cost; an extra unit
    is a request for something the plan does not describe."""
    planned = list(planned_units)
    if not planned:
        refuse("category=plan_has_no_units — a total over nothing is under "
               "every budget")
    if len(set(planned)) != len(planned):
        refuse("category=plan_units_duplicated")
    counted = [entry["unit_sha256"] for entry in ledger["entries"]]
    missing = sorted(set(planned) - set(counted))
    if missing:
        refuse(f"category=planned_units_not_counted count={len(missing)} — the "
               "total would be an underestimate that an operator approves as "
               "if it were the whole cost")
    extra = sorted(set(counted) - set(planned))
    if extra:
        refuse(f"category=counted_units_not_planned count={len(extra)} — a "
               "request for something the plan does not describe")
    return {"planned": len(planned), "counted": len(counted), "complete": True}


def ledger_digest(ledger: dict) -> str:
    payload = {
        "ledger_version": ledger["ledger_version"],
        "repository_numeric_id": ledger["repository_numeric_id"],
        "candidate_head_sha": ledger["candidate_head_sha"],
        "trusted_run_id": ledger["trusted_run_id"],
        "trusted_run_attempt": ledger["trusted_run_attempt"],
        "total_input_tokens": ledger["total_input_tokens"],
        "generation_calls": ledger["generation_calls"],
        # Sorted, so a run that counts the same units in a different order
        # produces the same digest and a run that counts DIFFERENT units
        # cannot hide behind reordering.
        "entries": sorted(ledger["entries"], key=lambda e: e["unit_sha256"]),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"trusted-count-ledger-v1\x00" + blob).hexdigest()


def finalize(ledger: dict, *, planned_units, authorization: dict) -> dict:
    """The payload D1's signed evidence is built over."""
    assert_zero_generation(ledger)
    coverage = assert_plan_fully_counted(ledger, planned_units=planned_units)
    return {
        **{k: ledger[k] for k in ("ledger_version", "repository_numeric_id",
                                  "candidate_head_sha", "trusted_run_id",
                                  "trusted_run_attempt", "total_input_tokens",
                                  "generation_calls")},
        "entries": sorted(ledger["entries"], key=lambda e: e["unit_sha256"]),
        "authorized_input_tokens": authorization["authorized_input_tokens"],
        "coverage": coverage,
        "ledger_sha256": ledger_digest(ledger),
        "honest_scope": ("every count is a validated integer from the count "
                         "endpoint, attributed to a planned unit, under a "
                         "ceiling an operator wrote. It says nothing about "
                         "what a generation run would cost"),
    }
