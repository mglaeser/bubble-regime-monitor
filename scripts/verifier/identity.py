"""The two patch identities (B2 6.7; MC1-F01).

repository_change_sha256 — "did the repository change differently?" Binds
base/head and EVERY raw record: status, score, both modes, both object IDs,
both raw paths, in git's deterministic emission order. Excluded and binary
changes participate fully: this identity is computed from the raw records,
NEVER from privacy-filtered patch text, so a change to bytes no provider may
see still invalidates every plan built on the old state.

provider_visible_material_sha256 (MC1-F01) — "did the transmittable material
change?" Binds ONLY the canonical material a provider could be shown: file
order, provider-safe path/status metadata, content-policy marker, included
body digest, and the generated-proof marker. It embeds NO endpoint SHA —
that is what lets two REAL commit ranges with identical provider-visible
material but different excluded-file bytes share this digest. The endpoints
and repository_change_sha256 are bound separately by the plan root.
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
                           body_sha256: str | None,
                           generated_covered: bool = False) -> dict:
    """One file's provider-visible record. The body hash is REQUIRED for
    included content and FORBIDDEN otherwise — an included file with no body
    proof and an excluded file with a lingering body hash are both lies.

    `generated_covered` marks a file whose changed content is covered by a
    validated generated-proof relationship rather than shown for review; it
    is bound so that flipping a file from direct review to generated proof
    moves the provider-visible identity (MC1-F07)."""
    if content_policy == INCLUDED and body_sha256 is None:
        raise ValueError("included content requires a body hash")
    if content_policy != INCLUDED and body_sha256 is not None:
        raise ValueError("withheld content must not carry a body hash")
    record = change.to_record()
    # Object IDs and modes are repository facts, not provider-visible
    # material: an excluded file's oid/mode change must not move THIS
    # identity. Ordinal likewise is a repository-order fact.
    for field in ("old_oid", "new_oid", "old_mode", "new_mode", "ordinal"):
        record.pop(field, None)
    record["content_policy"] = content_policy
    record["body_sha256"] = body_sha256
    record["generated_covered"] = generated_covered
    return record


def provider_visible_material_sha256(file_records: list[dict]) -> str:
    """Binds ONLY transmittable material — NO endpoint SHAs (MC1-F01).

    File order is significant and bound (the caller supplies records in the
    canonical repository order)."""
    payload = {"schema": SCHEMA, "files": file_records}
    return digest(b"provider-visible-material-v1", canonical_json(payload))
