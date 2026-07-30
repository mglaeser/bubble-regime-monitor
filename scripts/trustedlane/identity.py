"""Repository, range and ref identity checks.

Each takes what an operator PINNED and what the runtime OBSERVED, and refuses
on any difference. None of them reads a credential; all of them are pure
comparisons over values the caller supplies, which is what makes them testable
from a branch that must not have the real values."""

from __future__ import annotations

import re

from . import REPOSITORY_NUMERIC_ID
from .errors import refuse

_SHA1 = re.compile(r"\A[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


def assert_repository_numeric_id(observed) -> int:
    """The numeric id, not the full name.

    A fork carries the same `owner/repo` string in plenty of contexts; the
    numeric id is what distinguishes the repository an operator authorized
    from one that merely looks like it."""
    if isinstance(observed, bool) or not isinstance(observed, int):
        refuse("category=repository_id_not_integer")
    if observed != REPOSITORY_NUMERIC_ID:
        refuse(f"category=repository_id_mismatch expected="
               f"{REPOSITORY_NUMERIC_ID} observed={observed}")
    return observed


def assert_commit_sha(value, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA1.match(value):
        refuse(f"category=commit_sha_malformed field={field}")
    return value


def assert_range(*, target_base_sha: str, diff_base_sha: str, head_sha: str,
                 authorized: dict) -> dict:
    """The exact authorized range, all three endpoints."""
    observed = {"target_base_sha": target_base_sha,
                "diff_base_sha": diff_base_sha, "head_sha": head_sha}
    for field, value in observed.items():
        assert_commit_sha(value, field=field)
        expected = authorized.get(field)
        if expected is None:
            refuse(f"category=range_not_authorized field={field}")
        if value != expected:
            refuse(f"category=range_mismatch field={field}")
    return observed


#: Refs a credential-bearing run may execute from. A candidate branch is not
#: one of them, whatever it is called.
PROTECTED_REFS = ("refs/heads/main",)


def assert_protected_ref(ref: str) -> str:
    if ref not in PROTECTED_REFS:
        refuse(f"category=ref_not_protected ref={ref!r} allowed="
               f"{list(PROTECTED_REFS)} — a credential-bearing run executes "
               "only from a protected ref; a branch that merely holds trusted "
               "code is still a branch anyone can push to")
    return ref


def assert_environment_protected(environment: dict) -> dict:
    """An environment is protected when it SAYS so and the ref agrees."""
    if not isinstance(environment, dict):
        refuse("category=environment_not_object")
    for field in ("name", "protected", "allowed_refs"):
        if field not in environment:
            refuse(f"category=environment_field_missing field={field}")
    if environment["protected"] is not True:
        refuse("category=environment_not_protected")
    allowed = environment["allowed_refs"]
    if not isinstance(allowed, (list, tuple)) or not allowed:
        refuse("category=environment_allows_no_ref")
    outside = [r for r in allowed if r not in PROTECTED_REFS]
    if outside:
        refuse(f"category=environment_allows_unprotected_ref count="
               f"{len(outside)}")
    return dict(environment)


def assert_signature_present(record: dict, *, where: str = "") -> dict:
    """Shape only — and it says so.

    Verifying a signature needs a trusted public key. A key committed to the
    repository is a key the repository can change, so D0 checks that a
    signature is PRESENT and refuses to claim it checked one."""
    for field in ("signature", "signer_identity", "signed_digest"):
        if not record.get(field):
            refuse(f"category=signature_field_missing where={where} "
                   f"field={field}")
    if not _SHA256.match(str(record["signed_digest"])):
        refuse(f"category=signed_digest_malformed where={where}")
    return {"signature_present": True, "signature_verified": False,
            "honest_scope": "a signature is present and well-shaped; no key "
                            "was used and nothing was verified"}
