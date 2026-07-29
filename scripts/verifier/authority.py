"""Authority records, and why this package cannot grant authority (MC4-A2 §3.1).

Macro-Cycle 3 inferred authority from the presence of a value: `pin_record()`
stamped OPERATOR_AUTHORIZED on any dict, and a plaintext allowlist cleared a
literal by value everywhere in the change.

Macro-Cycle 4's first attempt fixed the wrong half of that. It required a
TRUSTED_* record to carry an `external_anchor`, and then CHECKED THE SHAPE of
that anchor — three non-empty strings, a known kind, 64 hex characters. The
claim "nothing in this package can mint trusted authority" was false. A pull
request author controls this package: they can build the dictionary by hand,
or edit the validator that inspects it. Shape-checking an anchor inside the
code under review is not a trust boundary. It is a spelling test.

So this module no longer decides. It can express a claim and it can check that
a claim is well-formed, but promotion to trusted happens somewhere this code
cannot reach:

  CANDIDATE-CONSTRUCTIBLE                 TRUSTED-LANE ONLY
  ------------------------------------    -------------------------------
  TEST_FIXTURE_UNAUTHORIZED               VERIFIED_LITERAL_AUTHORIZATION
  UNVERIFIED_EXTERNAL_CLAIM               VERIFIED_OPERATOR_PIN_AUTHORIZATION

An `external_anchor` on a candidate-side record is DATA, not evidence. A
record carrying one is UNVERIFIED_EXTERNAL_CLAIM — "someone asserts a trusted
run stands behind this" — and stays that way until a TrustedVerifier says
otherwise. The verifier that ships here refuses every promotion, and it is
the default everywhere, so the fail-closed path is the one you get by writing
nothing.

The real verifier lives outside the reviewed branch. It authenticates a
protected workflow run or a signature: repository numeric id, exact trusted
ref, run id and attempt, environment ruleset, and a signature over the record
digest. None of that can be done from inside the artifact being reviewed.
"""

from __future__ import annotations

import re

from .canon import canonical_json, digest, sha256_hex
from .errors import UNSET_POLICY_PIN, BlockingError

# ------------------------------------------------------- authority classes ---

TEST_FIXTURE_UNAUTHORIZED = "TEST_FIXTURE_UNAUTHORIZED"
UNVERIFIED_EXTERNAL_CLAIM = "UNVERIFIED_EXTERNAL_CLAIM"

VERIFIED_LITERAL_AUTHORIZATION = "VERIFIED_LITERAL_AUTHORIZATION"
VERIFIED_OPERATOR_PIN_AUTHORIZATION = "VERIFIED_OPERATOR_PIN_AUTHORIZATION"

#: Everything this package is allowed to build. Note what is absent.
CANDIDATE_CONSTRUCTIBLE = frozenset({TEST_FIXTURE_UNAUTHORIZED,
                                     UNVERIFIED_EXTERNAL_CLAIM})

#: Classes that confer authority. Only a TrustedVerifier can return one.
VERIFIED_CLASSES = frozenset({VERIFIED_LITERAL_AUTHORIZATION,
                              VERIFIED_OPERATOR_PIN_AUTHORIZATION})

AUTHORITY_CLASSES = CANDIDATE_CONSTRUCTIBLE | VERIFIED_CLASSES

# An anchor is a POINTER to where verification could happen. Candidate code
# records it verbatim and draws no conclusion from it.
_ANCHOR_FIELDS = ("anchor_kind", "anchor_reference", "anchor_digest")

ANCHOR_KINDS = frozenset({
    "TRUSTED_WORKFLOW_RUN",
    "SIGNED_EVIDENCE_RECORD",
    "PROTECTED_SERVICE_RECORD",
})

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_SHA1 = re.compile(r"\A[0-9a-f]{40}\Z")


def _fail(reason: str):
    raise BlockingError(UNSET_POLICY_PIN, reason)


def _require(condition, reason: str):
    if not condition:
        _fail(reason)


# ----------------------------------------------------------- the verifier ----


class TrustedVerifier:
    """The interface that promotes a claim to an authorization.

    Candidate code may DEFINE this and may call it. It may not implement a
    version that says yes — an implementation that returns True is only
    meaningful when it runs outside the reviewed branch, holding credentials
    the reviewed branch never sees.
    """

    #: Identifies which engine did the verifying, for the evidence record.
    verifier_identity = "abstract"

    def verify_literal_authorization(self, record: dict) -> dict | None:
        raise NotImplementedError

    def verify_pin_authorization(self, record: dict) -> dict | None:
        raise NotImplementedError

    def verify_count_evidence(self, record: dict) -> dict | None:
        raise NotImplementedError


class RejectingVerifier(TrustedVerifier):
    """The default. Refuses every promotion, and says why.

    This is what candidate code gets when nobody passes a verifier, which is
    the case for every local run, every test, and every path a pull request
    can reach. Fail-closed is the default because the default is this."""

    verifier_identity = "CANDIDATE_REJECTING_VERIFIER"

    def _refuse(self, kind: str):
        _fail(f"category=trust_promotion_refused kind={kind} "
              "verifier=CANDIDATE_REJECTING_VERIFIER — authority is granted "
              "only by a verifier running outside the reviewed branch; code "
              "under review cannot authenticate its own anchors")

    def verify_literal_authorization(self, record: dict) -> dict | None:
        self._refuse("literal_authorization")

    def verify_pin_authorization(self, record: dict) -> dict | None:
        self._refuse("pin_authorization")

    def verify_count_evidence(self, record: dict) -> dict | None:
        self._refuse("count_evidence")


DEFAULT_VERIFIER = RejectingVerifier()


def describe_anchor(record: dict) -> dict:
    """Record an anchor as an unverified POINTER, drawing no conclusion.

    Shape is checked so a malformed pointer is caught early, but passing the
    shape check confers nothing: the result is still an unverified claim."""
    anchor = record.get("external_anchor")
    if anchor is None:
        return {"present": False, "verified": False,
                "verification_status": "NO_ANCHOR_SUPPLIED"}
    _require(isinstance(anchor, dict), "category=anchor_not_object")
    for field in _ANCHOR_FIELDS:
        _require(isinstance(anchor.get(field), str) and anchor[field],
                 f"category=anchor_field_missing field={field}")
    _require(anchor["anchor_kind"] in ANCHOR_KINDS,
             "category=anchor_kind_unknown")
    _require(bool(_SHA256.match(anchor["anchor_digest"])),
             "category=anchor_digest_malformed")
    return {
        "present": True,
        "verified": False,
        "verification_status": "SHAPE_ONLY_NOT_AUTHENTICATED",
        "anchor_kind": anchor["anchor_kind"],
        "honest_scope": "the shape of this pointer was checked; nothing was "
                        "fetched, no signature was verified, and no "
                        "repository or workflow identity was confirmed",
    }


# --------------------------------------------- reviewed literal authority ----

_LITERAL_FIELDS = (
    "schema_version", "authority_class", "repository_identity",
    "target_base_sha", "diff_base_sha", "head_sha", "path_bytes_b64",
    "atom_id", "occurrence_index", "literal_sha256", "literal_length",
    "literal_category", "reason", "reviewer_identity", "authorized_at",
    "authorization_source", "revoked",
)


def literal_claim(*, repository_identity: str, target_base_sha: str,
                  diff_base_sha: str, head_sha: str, path_bytes_b64: str,
                  atom_id: str | None, occurrence_index: int | None,
                  literal: str, literal_category: str, reason: str,
                  reviewer_identity: str, authorized_at: str,
                  authorization_source: str,
                  external_anchor: dict | None = None,
                  test_fixture: bool = False) -> dict:
    """Build ONE scoped reviewed-literal CLAIM.

    There is deliberately no `authority_class` parameter. A caller cannot ask
    for a verified class, because a verified class is not something this code
    is able to produce. Supplying an anchor makes the record an
    UNVERIFIED_EXTERNAL_CLAIM — a pointer for the trusted lane to check — and
    nothing more.

    `occurrence_index` scopes the clearance to one occurrence within the atom.
    None means "any occurrence in this atom", which is broader and should be
    used only when the reviewer genuinely inspected every one."""
    if test_fixture and external_anchor is not None:
        _fail("category=fixture_with_anchor — a test fixture has no anchor to "
              "verify; supplying one confuses a local convenience with a claim")
    authority_class = (TEST_FIXTURE_UNAUTHORIZED if test_fixture
                       else UNVERIFIED_EXTERNAL_CLAIM)
    record = {
        "schema_version": 2,
        "authority_class": authority_class,
        "repository_identity": repository_identity,
        "target_base_sha": target_base_sha,
        "diff_base_sha": diff_base_sha,
        "head_sha": head_sha,
        "path_bytes_b64": path_bytes_b64,
        "atom_id": atom_id,
        "occurrence_index": occurrence_index,
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
    record["anchor_status"] = describe_anchor(record)
    record["authorization_sha256"] = literal_authorization_digest(record)
    return record


def literal_authorization_digest(record: dict) -> str:
    stripped = {k: v for k, v in record.items()
                if k != "authorization_sha256"}
    return digest(b"reviewed-literal-authorization-v2",
                  canonical_json(stripped))


def validate_literal_authorization(record: dict, where: str = "") -> dict:
    """Check a claim's SHAPE. This never promotes and never confers trust.

    A candidate-parsed record that LABELS itself with a verified class is not
    an error — a trusted wire record must be representable as inert input for
    the external verifier. But candidate code refuses to READ that label as
    authority: the class is checked for membership only, and every consumer
    below treats a verified-labelled record exactly as an unverified claim
    (A2-F01). Authority is conferred by the external verifier's RETURN VALUE,
    never by a field on the input."""
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
        _require(bool(_SHA1.match(record[field])
                      or _SHA256.match(record[field])),
                 f"category=commit_sha_malformed where={where} field={field}")
    _require(isinstance(record["revoked"], bool),
             f"category=revoked_not_boolean where={where}")
    _require(not record["revoked"],
             f"category=authorization_revoked where={where}")
    _require(isinstance(record["literal_length"], int)
             and not isinstance(record["literal_length"], bool)
             and record["literal_length"] > 0,
             f"category=literal_length_invalid where={where}")
    index = record["occurrence_index"]
    _require(index is None or (isinstance(index, int)
                              and not isinstance(index, bool) and index >= 0),
             f"category=occurrence_index_invalid where={where}")
    # The plaintext must never appear in a record.
    for key, value in record.items():
        if isinstance(value, str) and key != "literal_sha256":
            if sha256_hex(value.encode("utf-8",
                                       "surrogateescape")) == record[
                    "literal_sha256"]:
                _fail(f"category=authorization_contains_plaintext "
                      f"where={where} field={key}")
    _require(literal_authorization_digest(record)
             == record["authorization_sha256"],
             f"category=authorization_digest_mismatch where={where}")
    return record


def promote_literal_authorizations(records: list[dict], *,
                                   verifier: TrustedVerifier | None = None
                                   ) -> list[dict]:
    """Ask an external verifier to turn claims into authorizations.

    The default verifier refuses, so the default outcome is that nothing is
    promoted. That is not a limitation to work around: for real provider
    calls it is the required behaviour of code under review."""
    verifier = verifier or DEFAULT_VERIFIER
    promoted = []
    for i, record in enumerate(records):
        validate_literal_authorization(record, where=f"entry[{i}]")
        result = verifier.verify_literal_authorization(record)
        if result is None:
            _fail(f"category=verifier_declined entry={i}")
        _require(result.get("authority_class") == VERIFIED_LITERAL_AUTHORIZATION,
                 f"category=verifier_returned_unverified_class entry={i}")
        promoted.append(result)
    return promoted


class LiteralAuthorizationSet:
    """The scoped clearances in force for exactly one range.

    Scope is enforced at LOOKUP, keyed by (path, atom, occurrence, hash) — a
    literal cleared for one atom is not cleared for the same bytes elsewhere,
    and, when an occurrence index is given, not even elsewhere in the same
    atom."""

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
                   record["occurrence_index"], record["literal_sha256"])
            _require(key not in self._index,
                     "category=duplicate_authorization_scope")
            self._index[key] = record

    @property
    def authority_class(self) -> str:
        """The candidate-side class, which is NEVER a verified class.

        A record may LABEL itself verified; candidate code parsing it does
        not get to agree. A set built here is a fixture set or an unverified
        claim set — a positive trust conclusion can only come from the
        external verifier, and this property never returns one (A2-F01)."""
        classes = {r["authority_class"] for r in self.records}
        if TEST_FIXTURE_UNAUTHORIZED in classes:
            return TEST_FIXTURE_UNAUTHORIZED
        if not self.records:
            return TEST_FIXTURE_UNAUTHORIZED
        return UNVERIFIED_EXTERNAL_CLAIM

    @property
    def confers_real_call_authority(self) -> bool:
        """Always False candidate-side. There is no field, class, or record
        a caller can construct here that makes this True — real-call
        authority is a property of the trusted lane's output, which this
        object is not (A2-F01)."""
        return False

    def clears_occurrence(self, *, literal_sha256: str, path_bytes_b64: str,
                          atom_id: str | None, occurrence_index: int,
                          literal_category: str | None = None) -> bool:
        """Is THIS occurrence, at THIS place, of THIS category, cleared?

        Checked most specific first. A record with occurrence_index=None
        covers any occurrence in its atom; a record with a file-wide null
        atom covers any atom in that file. Neither ever crosses a file.

        The CATEGORY is part of the match (C4-F01). An operator reviews "this
        literal, detected as an OpenAI project key, at this position" — a
        record cleared under one category does not clear the same bytes
        detected as something else, because the second detection is a
        different statement about what those bytes are."""
        for key in (
            (path_bytes_b64, atom_id, occurrence_index, literal_sha256),
            (path_bytes_b64, atom_id, None, literal_sha256),
            (path_bytes_b64, None, None, literal_sha256),
        ):
            record = self._index.get(key)
            if record is None:
                continue
            if (literal_category is not None
                    and record["literal_category"] != literal_category):
                continue
            return True
        return False

    def record(self) -> dict:
        record = {
            "schema_version": 2,
            "entry_count": len(self.records),
            "authority_class": self.authority_class,
            "confers_real_call_authority": self.confers_real_call_authority,
            "authorization_sha256_in_order": sorted(
                r["authorization_sha256"] for r in self.records),
        }
        record["authorization_set_sha256"] = digest(
            b"reviewed-literal-authorization-set-v2", canonical_json(record))
        return record

    def digest(self) -> str:
        return self.record()["authorization_set_sha256"]


def propose_local_fixture_authorizations(skeleton: dict, atom_map: dict,
                                         atom_records: dict, *,
                                         repository_identity: str
                                         ) -> LiteralAuthorizationSet:
    """Propose UNAUTHORIZED, occurrence-scoped clearances for local planning.

    Every record is TEST_FIXTURE_UNAUTHORIZED and carries the exact
    occurrence index within its atom, so the proposal a reviewer sees is as
    narrow as the clearance it would become."""
    from . import preflight  # local: avoids an import cycle

    state = skeleton["repository_state"]
    records: list[dict] = []
    seen: set[tuple] = set()
    for atom_id, text in atom_map.items():
        record = atom_records.get(atom_id)
        if record is None:
            continue
        for index, _hash, finding in preflight.occurrence_index_map(text):
            literal = text[finding["offset"]:
                           finding["offset"] + finding["length"]]
            key = (record["path_bytes_b64"], atom_id, index,
                   finding["value_sha256"])
            if key in seen:
                continue
            seen.add(key)
            records.append(literal_claim(
                repository_identity=repository_identity,
                target_base_sha=state["target_base_sha"],
                diff_base_sha=state["diff_base_sha"],
                head_sha=state["head_sha"],
                path_bytes_b64=record["path_bytes_b64"],
                atom_id=atom_id, occurrence_index=index, literal=literal,
                literal_category=finding["category"],
                reason="literal detected in changed content; NOT reviewed",
                reviewer_identity="UNREVIEWED_LOCAL_PROPOSAL",
                authorized_at="1970-01-01T00:00:00Z",
                authorization_source="LOCAL_PROPOSAL_NOT_AUTHORIZATION",
                test_fixture=True))
    return LiteralAuthorizationSet(
        records, repository_identity=repository_identity,
        target_base_sha=state["target_base_sha"],
        diff_base_sha=state["diff_base_sha"], head_sha=state["head_sha"])
