"""Authority records: who approved what, for exactly which bytes (MC4 §5/§6).

Macro-Cycle 3 had two false provenance claims, and both came from the same
mistake — inferring authority from the PRESENCE of a value:

  * `pin_record()` stamped source=OPERATOR_AUTHORIZED onto any dict a caller
    passed, including a test fixture;
  * a plaintext `--allowlist` file cleared a literal by VALUE, everywhere in
    the change, with no reviewer, no scope, and no expiry — so the same pull
    request could plant a secret and clear it in one commit.

Authority is not a string a caller can set. It is an external record about
specific bytes at a specific place in a specific range, and code that cannot
verify that record must refuse to act as though it exists.

Two authority classes, and the gap between them is the whole point:

  TEST_FIXTURE_UNAUTHORIZED
      A local fixture. Usable for mock planning. Can never make a plan
      executable and can never clear a literal for a real provider call.

  TRUSTED_OPERATOR_AUTHORIZATION
      An operator decision, externally anchored. Only the trusted lane can
      produce one; nothing in this package can mint one, and the validator
      refuses a record claiming it without an external anchor.

A reviewed-literal record NEVER stores the literal. It stores the literal's
SHA-256 plus the exact scope in which that hash is cleared. A committed
authorization file is therefore not itself a place secrets live — which is
also why the scanner can read it without finding anything.
"""

from __future__ import annotations

import re

from .canon import canonical_json, digest, sha256_hex
from .errors import UNSET_POLICY_PIN, BlockingError

# --------------------------------------------------------------- classes ----

TEST_FIXTURE_UNAUTHORIZED = "TEST_FIXTURE_UNAUTHORIZED"
TRUSTED_OPERATOR_AUTHORIZATION = "TRUSTED_OPERATOR_AUTHORIZATION"

AUTHORITY_CLASSES = frozenset({TEST_FIXTURE_UNAUTHORIZED,
                               TRUSTED_OPERATOR_AUTHORIZATION})

# An externally anchored record must name where the anchor lives. A record
# claiming operator authority without one is refused, so "trusted" can never
# be achieved by editing a field.
_ANCHOR_FIELDS = ("anchor_kind", "anchor_reference", "anchor_digest")

ANCHOR_KINDS = frozenset({
    "TRUSTED_WORKFLOW_RUN",     # a run on a protected ref
    "SIGNED_EVIDENCE_RECORD",   # a detached signature over the record digest
    "PROTECTED_SERVICE_RECORD",  # an external verifier service
})

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_SHA1 = re.compile(r"\A[0-9a-f]{40}\Z")


def _fail(reason: str):
    raise BlockingError(UNSET_POLICY_PIN, reason)


def _require(condition, reason: str):
    if not condition:
        _fail(reason)


def _check_anchor(record: dict, where: str) -> None:
    """A trusted record must carry a complete external anchor."""
    if record.get("authority_class") != TRUSTED_OPERATOR_AUTHORIZATION:
        return
    anchor = record.get("external_anchor")
    _require(isinstance(anchor, dict),
             f"category=trusted_record_without_anchor where={where}")
    for field in _ANCHOR_FIELDS:
        _require(isinstance(anchor.get(field), str) and anchor[field],
                 f"category=anchor_field_missing where={where} field={field}")
    _require(anchor["anchor_kind"] in ANCHOR_KINDS,
             f"category=anchor_kind_unknown where={where}")
    _require(bool(_SHA256.match(anchor["anchor_digest"])),
             f"category=anchor_digest_malformed where={where}")


# --------------------------------------------- reviewed literal authority ----

_LITERAL_FIELDS = (
    "schema_version", "authority_class", "repository_identity",
    "target_base_sha", "diff_base_sha", "head_sha", "path_bytes_b64",
    "atom_id", "literal_sha256", "literal_length", "literal_category",
    "reason", "reviewer_identity", "authorized_at", "authorization_source",
    "revoked",
)


def literal_authorization(*, authority_class: str, repository_identity: str,
                          target_base_sha: str, diff_base_sha: str,
                          head_sha: str, path_bytes_b64: str,
                          atom_id: str | None, literal: str,
                          literal_category: str, reason: str,
                          reviewer_identity: str, authorized_at: str,
                          authorization_source: str,
                          external_anchor: dict | None = None) -> dict:
    """Build ONE scoped reviewed-literal record.

    `literal` is hashed and discarded. The record that gets committed carries
    the hash, never the secret — so publishing the authorization does not
    publish the thing it authorizes."""
    record = {
        "schema_version": 1,
        "authority_class": authority_class,
        "repository_identity": repository_identity,
        "target_base_sha": target_base_sha,
        "diff_base_sha": diff_base_sha,
        "head_sha": head_sha,
        "path_bytes_b64": path_bytes_b64,
        "atom_id": atom_id,
        "literal_sha256": sha256_hex(literal.encode("utf-8",
                                                    "surrogateescape")),
        "literal_length": len(literal),
        "literal_category": literal_category,
        "reason": reason,
        "reviewer_identity": reviewer_identity,
        "authorized_at": authorized_at,
        "authorization_source": authorization_source,
        "revoked": False,
    }
    if external_anchor is not None:
        record["external_anchor"] = external_anchor
    record["authorization_sha256"] = literal_authorization_digest(record)
    return record


def literal_authorization_digest(record: dict) -> str:
    stripped = {k: v for k, v in record.items()
                if k != "authorization_sha256"}
    return digest(b"reviewed-literal-authorization-v1",
                  canonical_json(stripped))


def validate_literal_authorization(record: dict, where: str = "") -> dict:
    _require(isinstance(record, dict),
             f"category=authorization_not_object where={where}")
    missing = [f for f in _LITERAL_FIELDS if f not in record]
    _require(not missing,
             f"category=authorization_field_missing where={where} "
             f"fields={missing}")
    _require(record["authority_class"] in AUTHORITY_CLASSES,
             f"category=authority_class_unknown where={where}")
    _require(bool(_SHA256.match(record["literal_sha256"])),
             f"category=literal_hash_malformed where={where}")
    for field in ("target_base_sha", "diff_base_sha", "head_sha"):
        _require(bool(_SHA1.match(record[field]) or _SHA256.match(record[field])),
                 f"category=commit_sha_malformed where={where} field={field}")
    _require(isinstance(record["revoked"], bool),
             f"category=revoked_not_boolean where={where}")
    _require(not record["revoked"],
             f"category=authorization_revoked where={where}")
    _require(isinstance(record["literal_length"], int)
             and not isinstance(record["literal_length"], bool)
             and record["literal_length"] > 0,
             f"category=literal_length_invalid where={where}")
    # The plaintext must never appear in a record. Anything that hashes to the
    # recorded value would be the secret itself.
    for key, value in record.items():
        if isinstance(value, str) and key != "literal_sha256":
            if sha256_hex(value.encode("utf-8", "surrogateescape")) == record[
                    "literal_sha256"]:
                _fail(f"category=authorization_contains_plaintext "
                      f"where={where} field={key}")
    _check_anchor(record, where or "literal_authorization")
    _require(literal_authorization_digest(record)
             == record["authorization_sha256"],
             f"category=authorization_digest_mismatch where={where}")
    return record


class LiteralAuthorizationSet:
    """The scoped clearances in force for exactly one range.

    Scope is enforced on lookup, not on load: a literal cleared for one atom
    in one file is not cleared for the same bytes anywhere else. That is the
    difference between "this fixture was reviewed" and "this value is fine"."""

    def __init__(self, records: list[dict], *, repository_identity: str,
                 target_base_sha: str, diff_base_sha: str, head_sha: str):
        self.records = [validate_literal_authorization(r, where=f"entry[{i}]")
                        for i, r in enumerate(records)]
        for i, record in enumerate(self.records):
            _require(record["repository_identity"] == repository_identity,
                     f"category=authorization_repository_mismatch entry={i}")
            for field, expected in (("target_base_sha", target_base_sha),
                                    ("diff_base_sha", diff_base_sha),
                                    ("head_sha", head_sha)):
                _require(record[field] == expected,
                         f"category=authorization_range_mismatch entry={i} "
                         f"field={field}")
        self._index: dict[tuple, dict] = {}
        for record in self.records:
            key = (record["path_bytes_b64"], record["atom_id"],
                   record["literal_sha256"])
            _require(key not in self._index,
                     "category=duplicate_authorization_scope")
            self._index[key] = record

    @property
    def authority_class(self) -> str:
        """The WEAKEST class present. One unauthorized fixture in the set
        makes the whole set unauthorized — authority does not average."""
        if any(r["authority_class"] == TEST_FIXTURE_UNAUTHORIZED
               for r in self.records):
            return TEST_FIXTURE_UNAUTHORIZED
        return TRUSTED_OPERATOR_AUTHORIZATION

    def clears(self, literal: str, *, path_bytes_b64: str,
               atom_id: str | None) -> bool:
        literal_hash = sha256_hex(literal.encode("utf-8", "surrogateescape"))
        if (path_bytes_b64, atom_id, literal_hash) in self._index:
            return True
        # A file-scoped clearance (atom_id null) covers any atom in that file,
        # but never another file.
        return (path_bytes_b64, None, literal_hash) in self._index

    def record(self) -> dict:
        record = {
            "schema_version": 1,
            "entry_count": len(self.records),
            "authority_class": self.authority_class,
            "authorization_sha256_in_order": sorted(
                r["authorization_sha256"] for r in self.records),
        }
        record["authorization_set_sha256"] = digest(
            b"reviewed-literal-authorization-set-v1", canonical_json(record))
        return record

    def digest(self) -> str:
        return self.record()["authorization_set_sha256"]


def empty_set(*, repository_identity: str, target_base_sha: str,
              diff_base_sha: str, head_sha: str) -> LiteralAuthorizationSet:
    return LiteralAuthorizationSet(
        [], repository_identity=repository_identity,
        target_base_sha=target_base_sha, diff_base_sha=diff_base_sha,
        head_sha=head_sha)
