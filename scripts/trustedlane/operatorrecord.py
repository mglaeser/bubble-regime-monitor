"""Parse and verify an operator authorization record.

The contract's rule, verbatim: **a branch-local file is not authority.**

That sentence is the whole design. `prerequisites.py` already refuses to treat a
satisfied-flag as authorization, because the flag is writable by whoever runs
the lane. This module is the next question: given a record that claims an
operator authorized something, what has to be true of it before the lane will
even consider it, and what can the lane still not conclude?

Seven required fields, from the contract:

    operator identity, timestamp, exact repository/range or policy version,
    exact values/digests, reason, expiry/revocation where applicable,
    externally stored/anchored record

The last one is the load-bearing one and the only one that is not shape. An
anchor is what makes the record something the branch could not have written: a
signature over a key the branch does not hold, a run in a protected context, an
identifier in a system with its own access control. This module checks that an
anchor is DECLARED and well-formed. It cannot check that the anchor is real —
that needs the trusted key, which lives in D1/D2 — and it says so in every
return value rather than letting a caller infer otherwise.

The distinction that keeps mattering:

    parsed      the record is well-formed          (this module)
    admissible  its anchor is of a permitted kind  (this module)
    verified    the anchor was checked against a trusted key   (NOT this module)
"""

from __future__ import annotations

import hashlib
import json
import re

from .errors import refuse

REQUIRED_FIELDS = (
    "prerequisite_key",
    "operator_identity",
    "authorized_at",
    "scope",
    "exact_values",
    "reason",
    "anchor",
)

#: Anchors the lane will consider. Each is something the candidate branch cannot
#: produce on its own; the branch can *claim* any of them, which is why this is
#: an admissibility check and not a verification.
PERMITTED_ANCHOR_KINDS = (
    "SIGNED_BY_TRUSTED_KEY",
    "PROTECTED_WORKFLOW_RUN",
    "EXTERNAL_RECORD_SYSTEM",
)

#: Anchors that are refused by name rather than merely failing a shape check,
#: because each one is a plausible-looking thing someone will try, and a generic
#: "malformed" would not explain why it can never work.
REFUSED_ANCHOR_KINDS = {
    "BRANCH_LOCAL_FILE":
        "a file in the reviewed branch is written by whoever writes the branch",
    "COMMIT_MESSAGE":
        "a commit message is branch-local content with extra steps",
    "PR_COMMENT":
        "anyone who can comment can write one, and it is not bound to a digest",
    "CI_LOG_LINE":
        "the log is produced by the job; a job cannot authorize itself",
    "ENV_VAR":
        "an environment variable is set by whoever configured the run",
}

_TIMESTAMP = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")

#: Prerequisites whose authorization must name an expiry. Standing permission is
#: the failure mode for anything that authorizes spending or generation: an
#: approval given once for one range should not silently cover every future one.
EXPIRY_REQUIRED_KEYS = (
    "approve_count_spending",
    "approve_generation_separately",
    "approve_literal_authorizations",
)


def assert_anchor_is_admissible(anchor) -> dict:
    """The record must be anchored somewhere the branch cannot write."""
    if not isinstance(anchor, dict):
        refuse("category=operator_anchor_not_object")
    kind = anchor.get("kind")
    if kind in REFUSED_ANCHOR_KINDS:
        refuse(f"category=operator_anchor_is_branch_writable kind={kind!r} — "
               f"{REFUSED_ANCHOR_KINDS[kind]}; a branch-local file is not "
               "authority")
    if kind not in PERMITTED_ANCHOR_KINDS:
        refuse(f"category=operator_anchor_kind_not_permitted kind={kind!r} "
               f"permitted={list(PERMITTED_ANCHOR_KINDS)}")
    reference = anchor.get("reference")
    if not isinstance(reference, str) or not reference.strip():
        refuse(f"category=operator_anchor_reference_missing kind={kind} — an "
               "anchor with no reference names nothing that could be checked")
    digest = anchor.get("anchored_digest")
    if not isinstance(digest, str) or not _SHA256.match(digest):
        refuse(f"category=operator_anchor_digest_malformed kind={kind} — the "
               "anchor must bind a digest, or it attests that something was "
               "approved without saying what")
    return {"anchor_kind": kind, "anchor_reference": reference,
            "anchor_shape_valid": True, "anchor_verified": False}


def record_digest(record: dict) -> str:
    """The digest an anchor must be over: the record's authorizing content.

    Deliberately excludes the anchor itself — an anchor cannot cover its own
    bytes — and excludes nothing else, so that changing any authorizing field
    after the fact breaks the binding."""
    payload = {field: record.get(field)
               for field in REQUIRED_FIELDS if field != "anchor"}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str).encode()
    return hashlib.sha256(b"operator-record-v1\x00" + blob).hexdigest()


def parse_operator_record(record: dict) -> dict:
    """Validate shape and admissibility. Verifies nothing."""
    if not isinstance(record, dict):
        refuse("category=operator_record_not_object")
    missing = [f for f in REQUIRED_FIELDS if record.get(f) in (None, "", {}, [])]
    if missing:
        refuse(f"category=operator_record_incomplete missing={missing}")
    unknown = sorted(set(record) - set(REQUIRED_FIELDS) - {"expires_at",
                                                           "revoked_at"})
    if unknown:
        refuse(f"category=operator_record_unknown_field fields={unknown} — an "
               "adapter that tolerates unknown keys cannot notice the record "
               "format changed")

    key = record["prerequisite_key"]
    from .prerequisites import OPERATOR_PREREQUISITES
    known = {p.key for p in OPERATOR_PREREQUISITES}
    if key not in known:
        refuse(f"category=operator_record_unknown_prerequisite key={key!r} — "
               "an authorization for something that is not a prerequisite "
               "authorizes nothing and hides that it authorized nothing")

    if not _TIMESTAMP.match(str(record["authorized_at"])):
        refuse("category=operator_timestamp_malformed — an unparseable "
               "timestamp cannot be compared to an expiry")

    if not isinstance(record["exact_values"], dict) or not record["exact_values"]:
        refuse("category=operator_exact_values_not_a_nonempty_object — "
               "approving without naming what was approved is approving "
               "whatever turns up")

    if record.get("revoked_at") is not None:
        refuse(f"category=operator_record_revoked at={record['revoked_at']!r}")

    expires = record.get("expires_at")
    if key in EXPIRY_REQUIRED_KEYS and expires in (None, ""):
        refuse(f"category=operator_authorization_has_no_expiry key={key} — "
               "spending and generation approvals must not become standing "
               "permission; an approval given once for one range would "
               "otherwise cover every future one")
    if expires not in (None, "") and not _TIMESTAMP.match(str(expires)):
        refuse("category=operator_expiry_malformed")

    anchor = assert_anchor_is_admissible(record["anchor"])
    expected = record_digest(record)
    if record["anchor"]["anchored_digest"] != expected:
        refuse("category=operator_anchor_digest_mismatch — the anchor binds a "
               "different record than the one supplied; either the record was "
               "edited after anchoring or the anchor belongs to something else")

    return {
        "prerequisite_key": key,
        "operator_identity": record["operator_identity"],
        "authorized_at": record["authorized_at"],
        "expires_at": expires,
        "scope": record["scope"],
        "record_sha256": expected,
        **anchor,
        "state": "PARSED_AND_ADMISSIBLE_NOT_VERIFIED",
        "honest_scope": ("shape is valid, the anchor is of a permitted kind, "
                         "and the digest binds this exact record. Whether the "
                         "anchor is genuine was NOT checked — that needs the "
                         "trusted key, which this branch must not hold"),
    }


def assert_not_expired(parsed: dict, *, observed_now: str) -> dict:
    """Expiry against an observed clock, supplied rather than read.

    `observed_now` is a parameter because a lane that reads its own clock can be
    run on a machine whose clock says whatever is convenient. In D1/D2 it comes
    from the protected run; here it makes the check testable without one."""
    if not _TIMESTAMP.match(str(observed_now)):
        refuse("category=observed_now_malformed")
    expires = parsed.get("expires_at")
    if expires in (None, ""):
        return {"expiry_checked": False,
                "honest_scope": "this prerequisite declares no expiry"}
    if str(observed_now) >= str(expires):
        refuse(f"category=operator_authorization_expired expires_at={expires} "
               f"observed_now={observed_now}")
    return {"expiry_checked": True, "expires_at": expires}


def verify_records(records, *, observed_now: str) -> dict:
    """Parse a set of records and report which prerequisites they cover.

    Returns coverage, never authorization. `prerequisites.assert_real_calls_
    authorized` still refuses regardless of what this returns, because a record
    the lane cannot authenticate is not authority however well-formed it is."""
    materialised = list(records)
    parsed = []
    for record in materialised:
        one = parse_operator_record(record)
        assert_not_expired(one, observed_now=observed_now)
        parsed.append(one)
    keys = [p["prerequisite_key"] for p in parsed]
    duplicated = sorted({k for k in keys if keys.count(k) > 1})
    if duplicated:
        refuse(f"category=operator_record_duplicated keys={duplicated} — two "
               "authorizations for one prerequisite means one of them is stale "
               "and nothing here says which")
    from .prerequisites import OPERATOR_PREREQUISITES
    outstanding = sorted({p.key for p in OPERATOR_PREREQUISITES} - set(keys))
    return {
        "parsed_count": len(parsed),
        "covered": sorted(keys),
        "outstanding": outstanding,
        "records": parsed,
        "authorized": False,
        "honest_scope": ("these records are well-formed and admissibly "
                         "anchored; none of them has been verified against a "
                         "trusted key, so this is coverage, not authorization"),
    }
