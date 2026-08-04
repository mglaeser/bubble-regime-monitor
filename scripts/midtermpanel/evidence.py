"""Mid-term evidence: what it must carry, and what it may never claim.

## The four fields every record carries

    write_separated        = false
    provider_secret_scope  = "repository"   # pragma: allowlist secret
    human_merge_required   = true
    trusted_evidence_claim = false

They are not commentary. They are required keys, asserted on construction AND on
load, because the failure this package must not have is an evidence file that
reads as a trusted review once it is separated from the document explaining that
it is not. A record travels; the paragraph does not.

`trusted_evidence_claim = false` is deliberately a field rather than an absence.
"This record does not claim trust" and "this record forgot to say" look identical
when the field is optional, and only one of them is a record somebody wrote on
purpose.

## Self-hash

Every record carries the digest of its own content, computed over the record
with the digest field removed, using the same canonical form the trusted lane
uses (`sort_keys=True, separators=(",", ":")`). `strict_load` recomputes it.

The point is the same-run handoff: the panel job downloads the artifact the count
job uploaded, and "the file I got is the file that was written" has to be
checkable without trusting the transport that carried it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile

from . import (
    COUNT_EVIDENCE_CLASS,
    FORBIDDEN_EVIDENCE_CLASSES,
    MIDTERM_EVIDENCE_CLASSES,
    PANEL_EVIDENCE_CLASS,
)
from .errors import refuse

EVIDENCE_SCHEMA_VERSION = 1

#: The four claims every mid-term record must carry, with their only legal values.
REQUIRED_HONESTY_FIELDS = {
    "write_separated": False,
    "provider_secret_scope": "repository",  # pragma: allowlist secret
    "human_merge_required": True,
    "trusted_evidence_claim": False,
}

#: Fields every record must bind, so that a record found on its own can be
#: checked against the world rather than believed.
REQUIRED_BINDING_FIELDS = (
    "repository_numeric_id", "candidate_head_sha", "candidate_base_sha",
    "engine_digest", "policy_digest", "run_id", "run_attempt",
)

_DIGEST_FIELD = "evidence_sha256"


def canonical_bytes(payload: dict) -> bytes:
    """The one canonical form. Same as the trusted lane's, on purpose.

    A second canonicalisation would produce a second digest for the same
    content, and the first time that mattered would be a same-run handoff
    failing for a reason nobody could reproduce."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_of(payload: dict) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def assert_not_trusted_class(evidence_class: str) -> str:
    """The mid-term lane may never emit a class reserved for a trusted one.

    Checked against the explicit forbidden tuple rather than a `TRUSTED_` prefix:
    `WRITE_SEPARATED_REVIEW_EVIDENCE` and
    `INDEPENDENT_THIRD_PARTY_ATTESTATION` contain no such prefix and are exactly
    the two claims this architecture cannot make."""
    if evidence_class in FORBIDDEN_EVIDENCE_CLASSES:
        refuse(f"category=midterm_emitted_a_reserved_evidence_class "
               f"evidence_class={evidence_class!r} — this panel is not "
               "write-separated, holds a repository-scoped secret in the "
               "repository it reviews, and rests on no recorded operator "
               "prerequisite")
    if evidence_class not in MIDTERM_EVIDENCE_CLASSES:
        refuse(f"category=evidence_class_not_a_midterm_class "
               f"evidence_class={evidence_class!r} "
               f"permitted={list(MIDTERM_EVIDENCE_CLASSES)}")
    return evidence_class


def _assert_honesty_fields(record: dict, *, where: str) -> None:
    for field, required in REQUIRED_HONESTY_FIELDS.items():
        if field not in record:
            refuse(f"category=evidence_missing_honesty_field field={field} "
                   f"where={where} — the record must carry what it is, because "
                   "a record travels and the document explaining it does not")
        if record[field] != required:
            refuse(f"category=evidence_honesty_field_altered field={field} "
                   f"got={record[field]!r} required={required!r} where={where}")


def _assert_binding_fields(record: dict, *, where: str) -> None:
    missing = [f for f in REQUIRED_BINDING_FIELDS if not record.get(f)]
    if missing:
        refuse(f"category=evidence_missing_binding_fields fields={missing} "
               f"where={where} — evidence that does not name what it is bound "
               "to can be replayed against anything")


def build(*, evidence_class: str, repository_numeric_id: int,
          candidate_head_sha: str, candidate_base_sha: str,
          engine_digest: str, policy_digest: str, run_id: int,
          run_attempt: int, body: dict) -> dict:
    """Assemble a record and stamp its own digest into it."""
    assert_not_trusted_class(evidence_class)
    record = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_class": evidence_class,
        "repository_numeric_id": repository_numeric_id,
        "candidate_head_sha": candidate_head_sha,
        "candidate_base_sha": candidate_base_sha,
        "engine_digest": engine_digest,
        "policy_digest": policy_digest,
        "run_id": run_id,
        "run_attempt": run_attempt,
        **REQUIRED_HONESTY_FIELDS,
        "body": body,
    }
    _assert_honesty_fields(record, where="build")
    _assert_binding_fields(record, where="build")
    record[_DIGEST_FIELD] = digest_of(record)
    return record


def write_atomic(record: dict, path: str) -> str:
    """Write via a temp file and rename.

    A half-written evidence file is worse than a missing one: the panel job's
    strict load would report a digest mismatch, and the operator would go looking
    for tampering rather than for a killed process."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=directory, suffix=".partial")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(canonical_bytes(record))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    return path


def strict_load(path: str, *, expected_class: str,
                expected_head: str = None, expected_base: str = None,
                expected_engine_digest: str = None,
                expected_policy_digest: str = None) -> dict:
    """Load a record and refuse it unless everything about it checks out.

    Order matters. The self-hash is verified BEFORE any field is trusted,
    because a record whose digest does not match is a record whose fields are
    not evidence of anything — including the fields you would otherwise use to
    decide how alarmed to be.

    Duplicate keys are refused for the reason `policystate.load_policy` records:
    a document declaring `candidate_head_sha` twice parses fine and silently
    keeps the last one."""
    if not os.path.exists(path):
        refuse(f"category=evidence_missing path={os.path.basename(path)}")

    def _no_duplicates(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                refuse(f"category=evidence_duplicate_key key={key!r}")
            seen[key] = value
        return seen

    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (OSError, UnicodeDecodeError) as exc:
        refuse(f"category=evidence_unreadable path={os.path.basename(path)} "
               f"exception_class={type(exc).__name__}")
    except json.JSONDecodeError as exc:
        refuse(f"category=evidence_not_json path={os.path.basename(path)} "
               f"line={exc.lineno}")
    if not isinstance(record, dict):
        refuse("category=evidence_not_a_mapping")

    claimed = record.get(_DIGEST_FIELD)
    if not isinstance(claimed, str):
        refuse("category=evidence_has_no_self_digest — a record that does not "
               "carry its own digest cannot be checked against the transport "
               "that delivered it")
    without = {k: v for k, v in record.items() if k != _DIGEST_FIELD}
    recomputed = digest_of(without)
    if recomputed != claimed:
        refuse(f"category=evidence_self_digest_mismatch "
               f"claimed={claimed[:16]} recomputed={recomputed[:16]} — the file "
               "that arrived is not the file that was written")

    assert_not_trusted_class(record.get("evidence_class", ""))
    if record.get("evidence_class") != expected_class:
        refuse(f"category=evidence_wrong_class "
               f"got={record.get('evidence_class')!r} "
               f"expected={expected_class!r}")
    _assert_honesty_fields(record, where="load")
    _assert_binding_fields(record, where="load")

    for field, expected in (("candidate_head_sha", expected_head),
                            ("candidate_base_sha", expected_base),
                            ("engine_digest", expected_engine_digest),
                            ("policy_digest", expected_policy_digest)):
        if expected is not None and record.get(field) != expected:
            refuse(f"category=evidence_binding_mismatch field={field} "
                   f"got={str(record.get(field))[:16]} "
                   f"expected={str(expected)[:16]} — this evidence describes a "
                   "different review than the one being performed")
    return record


def count_evidence(**kwargs) -> dict:
    return build(evidence_class=COUNT_EVIDENCE_CLASS, **kwargs)


def panel_evidence(**kwargs) -> dict:
    return build(evidence_class=PANEL_EVIDENCE_CLASS, **kwargs)


#: How much of the evidence self-digest travels in the public status
#: description. Sixteen hex characters: enough that a human comparing it to a
#: retained file is not guessing, short enough to leave room for a description
#: that still reads as English.
PUBLIC_DIGEST_PREFIX_CHARS = 16
PUBLIC_DIGEST_MARKER = "ev="

#: Largest private plan or evidence document these loaders will parse. The real
#: plan carries every final unit and batch, so this is generous; it exists so a
#: pathological file is a named refusal rather than a memory event in a job
#: holding a credential.
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024


def public_digest_marker(evidence_sha256: str) -> str:
    """The `ev=<16 hex>` fragment a terminal status carries.

    Without this the merge gate's evidence binding could not succeed at all:
    `status_request` accepted an `evidence_sha256` and `publish` never sent it,
    so the digest existed inside the process and nowhere a reader could see it.
    The gate then searched the published description for a string that was
    never put there, which meant a real successful run could not pass its own
    merge gate."""
    if not isinstance(evidence_sha256, str) or len(evidence_sha256) != 64:
        refuse("category=public_digest_marker_needs_a_full_digest")
    return f"{PUBLIC_DIGEST_MARKER}{evidence_sha256[:PUBLIC_DIGEST_PREFIX_CHARS]}"


def strict_load_plan(path: str, *, expected_head: str = None,
                     expected_base: str = None,
                     expected_engine_digest: str = None,
                     expected_policy_digest: str = None) -> dict:
    """The private executable plan, loaded with the same strictness as evidence.

    The plan was read with a plain `json.loads` and then validated as a mapping.
    Ordinary `json.loads` accepts duplicate keys and keeps the last, so a plan
    declaring `execution_challenge` twice — or `human_merge_required` twice —
    parses, validates, and carries whichever copy came last. Every field in this
    document is security-relevant: it decides which requests are sent, under
    whose challenge, against which pins.

    `count.assert_plan_is_executable` still owns the semantic rules. This owns
    getting the bytes into a mapping without losing anything on the way."""
    if not os.path.exists(path):
        refuse(f"category=plan_missing path={os.path.basename(path)}")

    def _no_duplicates(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                refuse(f"category=plan_duplicate_key key={key!r} — the plan "
                       "decides which requests are sent and under whose "
                       "challenge; a document declaring a field twice parses "
                       "and silently keeps the last one")
            seen[key] = value
        return seen

    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        refuse(f"category=plan_unreadable path={os.path.basename(path)} "
               f"exception_class={type(exc).__name__}")
    if len(raw) > MAX_EVIDENCE_BYTES:
        refuse(f"category=plan_oversized bytes={len(raw)} "
               f"limit={MAX_EVIDENCE_BYTES}")
    try:
        plan = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except UnicodeDecodeError:
        refuse("category=plan_not_utf8")
    except json.JSONDecodeError as exc:
        refuse(f"category=plan_not_json line={exc.lineno} column={exc.colno}")

    from .count import assert_plan_is_executable
    assert_plan_is_executable(plan)

    mismatches = []
    for field, expected in (("candidate_head_sha", expected_head),
                            ("candidate_base_sha", expected_base),
                            ("engine_digest", expected_engine_digest),
                            ("policy_digest", expected_policy_digest)):
        if expected is not None and plan.get(field) != expected:
            mismatches.append(f"{field}: plan={str(plan.get(field))[:16]} "
                              f"expected={str(expected)[:16]}")
    if mismatches:
        refuse(f"category=plan_is_for_another_review found={mismatches}")
    return plan
