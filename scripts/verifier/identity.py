"""The two patch identities (B2, mandate 6.7).

repository_change_sha256 — "did the repository change differently?" Binds
base/head and EVERY raw-change record: status, score, both modes, both
object IDs, both raw paths, in git's deterministic emission order. Excluded
and binary changes participate fully: this identity is computed from the raw
records, NEVER from privacy-filtered patch text, so a change to bytes no
provider may see still invalidates every plan built on the old state.

reviewable_content_sha256 — "did the provider-visible material change?"
Binds the per-file bodies that may be shown, plus each file's path/status
metadata and an EXPLICIT exclusion marker for withheld content. An
excluded-only change moves the repository identity but may leave this one
unchanged — that asymmetry is the point, and it is proven by test.
"""

from __future__ import annotations

from .canon import canonical_json, digest
from .contentpolicy import INCLUDED
from .rawchange import RawChange

SCHEMA = 1


def repository_change_sha256(base_sha: str, head_sha: str,
                             changes: list[RawChange]) -> str:
    payload = {
        "schema": SCHEMA,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "changes": [c.to_record() for c in changes],
    }
    return digest(b"repository-change-v1", canonical_json(payload))


def reviewable_file_record(change: RawChange, content_policy: str, *,
                           body_sha256: str | None) -> dict:
    """One file's provider-visible record. The body hash is REQUIRED for
    included content and FORBIDDEN otherwise — an included file with no body
    proof and an excluded file with a lingering body hash are both lies."""
    if content_policy == INCLUDED and body_sha256 is None:
        raise ValueError("included content requires a body hash")
    if content_policy != INCLUDED and body_sha256 is not None:
        raise ValueError("withheld content must not carry a body hash")
    record = change.to_record()
    # Object IDs are repository facts, not provider-visible material: an
    # excluded file's oid change must not move THIS identity.
    record.pop("old_oid")
    record.pop("new_oid")
    record.pop("old_mode")
    record.pop("new_mode")
    record["content_policy"] = content_policy
    record["body_sha256"] = body_sha256
    return record


def reviewable_content_sha256(base_sha: str, head_sha: str,
                              file_records: list[dict]) -> str:
    payload = {
        "schema": SCHEMA,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "files": file_records,
    }
    return digest(b"reviewable-content-v1", canonical_json(payload))
