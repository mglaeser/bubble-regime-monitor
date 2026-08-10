"""Build the engine identity CANDIDATE package — and label it as a candidate.

`enginepolicy` says what a trusted engine identity must contain and refuses to
call any of it authenticated. This module produces the half of that record a
candidate branch can honestly compute, and leaves the other half empty on
purpose.

The split is not arbitrary. Three of the eleven identity fields are facts about
*source*, derivable by anyone holding the commits, and reproducible by a reader
who fetches the same two:

    engine_source_sha256, dependency_lock_sha256, sbom_sha256

The rest are facts about a *build that has not happened* and an *approval that
has not been given*:

    artifact_sha256, runner_image_digest, build_workflow_run_id,
    build_workflow_run_attempt, signer_identity, signature, provenance,
    independent_approval_record

Filling those in from this branch would be inventing them. A digest of a
tarball this branch built on this machine is not `artifact_sha256` in any sense
that matters — the field means "the artifact the protected build produced", and
no protected build has run. So they are `None`, and the package carries the
state name that says why:

    UNVERIFIED_ENGINE_IDENTITY_CANDIDATE

It cannot unlock D1. `assert_engine_approved` refuses every identity regardless
of completeness, and this package is deliberately incomplete on top of that.

What it IS good for: giving the operator something exact to approve. Operator
prerequisite 14 is "approve the trusted engine identity and artifact", and an
operator cannot approve a description. They can approve a digest.
"""

from __future__ import annotations

import hashlib
import json
import sys

from .candidatefetch import git
from .errors import refuse

CANDIDATE_STATE = "UNVERIFIED_ENGINE_IDENTITY_CANDIDATE"

#: Fields this branch can honestly compute. The source and lock membership lists
#: live in `enginesource.SOURCE_ROLES` and `runtimelock.PERMITTED_RUNTIME_PACKAGES`
#: rather than here, so there is one place to change what the engine is made of.
COMPUTABLE_FIELDS = (
    "engine_source_sha256",
    "dependency_lock_sha256",
    "sbom_sha256",
)

#: Fields that require a protected build or an operator, and are therefore left
#: empty. Each maps to why it is empty, so the package explains its own gaps
#: instead of merely having them.
UNAVAILABLE_FIELDS = {
    "artifact_sha256":
        "no protected build has produced an artifact; a tarball this branch "
        "built on this machine is not the artifact a protected build produced",
    "runner_image_digest":
        "no protected build has run, so no runner image was resolved",
    "build_workflow_run_id":
        "no protected build workflow exists yet; D1/D2 are .yml.template",
    "build_workflow_run_attempt":
        "an attempt number identifies a re-run of a build; with no build there "
        "is no first attempt to re-run",
    "signer_identity":
        "signing requires a key this branch must not hold",
    "signature":
        "see signer_identity; a signature this branch could produce would "
        "attest only that this branch produced it",
    "provenance":
        "provenance is a statement by the builder about the build; there has "
        "been no build",
    "independent_approval_record":
        "operator prerequisite 14, outstanding — and a record written here "
        "would be the candidate approving itself",
}


def sbom(*, roles, cwd: str = ".") -> dict:
    """A minimal SBOM over what the engine imports, read from git objects.

    Every stdlib import is listed alongside the third-party ones, because
    "the engine depends only on the standard library plus PyYAML" is a claim
    worth being able to check rather than assert — and it reaches that
    conclusion by enumeration, independently of the suite's import denylist.

    Read from the object database, not from disk, for the same reason as
    everything else here: a disk walk describes whatever is checked out."""
    import ast as _ast

    from .enginesource import SOURCE_ROLES, list_blobs_at

    imported = set()
    unparsed = []
    per_role = {}
    for role, commit in sorted(roles.items()):
        if role not in SOURCE_ROLES:
            refuse(f"category=engine_role_unknown role={role!r}")
        names = set()
        for prefix in SOURCE_ROLES[role]:
            for entry in list_blobs_at(commit, prefix, cwd=cwd):
                if not entry["path"].endswith(".py"):
                    continue
                blob = git(["cat-file", "blob", entry["oid"]], cwd=cwd,
                           operation="cat-file-blob")
                try:
                    tree = _ast.parse(blob)
                except (SyntaxError, ValueError):
                    unparsed.append(entry["path"])
                    continue
                for node in _ast.walk(tree):
                    if isinstance(node, _ast.Import):
                        for alias in node.names:
                            names.add(alias.name.split(".")[0])
                    elif isinstance(node, _ast.ImportFrom):
                        if node.level == 0 and node.module:
                            names.add(node.module.split(".")[0])
        per_role[role] = sorted(names)
        imported |= names
    if unparsed:
        refuse(f"category=engine_source_unparsable files={unparsed} — an SBOM "
               "that skips the file it could not read is an SBOM with a hole "
               "exactly where the interesting thing would be")
    components = sorted(imported)
    third_party = sorted(n for n in components
                         if n not in sys.stdlib_module_names)
    payload = json.dumps({"components": components, "per_role": per_role},
                         sort_keys=True, separators=(",", ":")).encode()
    return {
        "sbom_sha256": hashlib.sha256(payload).hexdigest(),
        "components": components,
        "per_role": per_role,
        "third_party": third_party,
        "honest_scope": ("top-level import names resolved by AST over the blobs "
                         "at each role's commit; it does not resolve transitive "
                         "dependencies of third-party packages"),
    }


def candidate_package(*, roles, repository_numeric_id: int,
                      cwd: str = ".") -> dict:
    """The engine identity CANDIDATE, bound to exact commits.

    There is no `root` parameter, and that absence is EX3-R05's fix. The old
    signature took `source_commit` and `root` separately, recorded the commit
    string and independently hashed whatever was under the root — demonstrated
    accepting a package that named main's commit while hashing a single planted
    `impostor.py`. Blobs now come from the git object database at the named
    commit, so the tree cannot disagree with the commit: it is the commit.

    `roles` maps each source role to its own commit, because the engine that
    reviews is the candidate verifier package plus the protected lane, and in
    the general case those are at different commits (EX3-R04)."""
    from .enginesource import multi_source_manifest
    from .runtimelock import load_lock

    manifest = multi_source_manifest(roles=roles,
                                     repository_numeric_id=repository_numeric_id,
                                     cwd=cwd)
    lock = load_lock(root=cwd)
    bill = sbom(roles=roles, cwd=cwd)

    record = {
        "state": CANDIDATE_STATE,
        "repository_numeric_id": repository_numeric_id,
        "engine_source_sha256": manifest["engine_source_sha256"],
        "source_roles": manifest["binding"],
        "dependency_lock_sha256": lock["lock_sha256"],
        "sbom_sha256": bill["sbom_sha256"],
    }
    for field in UNAVAILABLE_FIELDS:
        record[field] = None
    record["unavailable_because"] = dict(UNAVAILABLE_FIELDS)
    record["detail"] = {"manifest": manifest, "dependency_lock": lock,
                        "sbom": bill}

    binding = {k: record[k] for k in COMPUTABLE_FIELDS}
    blob = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    record["candidate_package_sha256"] = hashlib.sha256(
        b"engine-identity-candidate-v2\x00" + blob).hexdigest()
    record["honest_scope"] = (
        "every blob was read from the git object database at the named commit, "
        "and every runtime dependency is version-pinned and hashed. All of it "
        "is reproducible by anyone with the same commits, which is exactly why "
        "none of it is evidence: reproducibility is not authority. It cannot "
        "unlock D1, and assert_engine_approved refuses it along with every "
        "other identity.")
    return record


def assert_is_only_a_candidate(record: dict) -> dict:
    """Refuse a package that has been dressed up as approved.

    The realistic failure is not forgery, it is drift: someone fills in
    `signature` with something plausible during a later pass and the package
    stops announcing what it is. Every unavailable field must still be empty and
    the state string must be intact."""
    if not isinstance(record, dict):
        refuse("category=engine_candidate_not_object")
    if record.get("state") != CANDIDATE_STATE:
        refuse(f"category=engine_candidate_state_changed "
               f"state={record.get('state')!r} expected={CANDIDATE_STATE}")
    filled = sorted(f for f in UNAVAILABLE_FIELDS
                    if record.get(f) not in (None, ""))
    if filled:
        refuse(f"category=engine_candidate_claims_unavailable_fields "
               f"fields={filled} — these require a protected build or an "
               "operator; a value here was produced by the candidate branch "
               "and would be the candidate attesting itself")
    missing = sorted(f for f in COMPUTABLE_FIELDS
                     if record.get(f) in (None, ""))
    if missing:
        refuse(f"category=engine_candidate_incomplete fields={missing}")
    return {"state": CANDIDATE_STATE, "authenticated": False,
            "unlocks_d1": False,
            "honest_scope": "shape and emptiness verified; nothing attested"}


# --------------------------------------------------------------------------
# R10 — the PROTECTED build, which is the only thing that can fill in the
# fields `UNAVAILABLE_FIELDS` explains are empty.
#
# The candidate package above is honest about its gaps and useless for
# unlocking anything, which is correct and is also the problem: D1 and D2
# compare the operator's five approved digests against an identity record, and
# until something produces one, that comparison has nothing to read.
#
# What makes this build trustworthy is NOT that it runs different code — it
# runs the same `enginesource.build_engine_artifact`, deterministically, so
# anyone can reproduce the artifact. It holds no provider credential and
# records WHICH run produced the bytes. The provenance is a statement by the
# builder about the build, and this is the only place a builder exists.
#
# What it is NOT is a claim that the ref was protected. An earlier version of
# this module hardcoded `PROTECTED_ENGINE_IDENTITY` and said in its own
# `honest_scope` that "the workflow producing it runs only on a protected
# ref". The workflow proved `refs/heads/main` and nothing more, and the
# repository's `main` had no native protection, so every artifact it built
# carried a false provenance claim inside its own identity record. A
# governance note added later cannot repair a claim the artifact makes about
# itself, so the state is now DERIVED from a platform fact the build is told
# and must record.
#
#   ref == refs/heads/main        is not protection
#   being the default branch      is not protection
#   a human merged it             is not protection
#   GitHub signed the merge       is not protection
#
# Those are four different facts. Only `github.ref_protected` answers the
# question, and only the platform can answer it.
# --------------------------------------------------------------------------

#: Native branch/ruleset protection was observed active. Eligible for the
#: trusted lane's D1/D2 once every other prerequisite is also met.
PROTECTED_STATE = "PROTECTED_ENGINE_IDENTITY"

#: An exact default-branch SHA was selected by a human, with no native
#: protection behind it. Eligible ONLY for `MIDTERM_SINGLE_REPO_*` evidence,
#: never for `TRUSTED_*`. This is a weaker claim wearing its weakness in its
#: name, which is the whole point of having two states instead of one.
MIDTERM_STATE = "MIDTERM_OPERATOR_APPROVED_ENGINE_IDENTITY"

BUILT_STATES = (PROTECTED_STATE, MIDTERM_STATE)

#: What stands in for native protection when there isn't any. Recorded as a
#: named control rather than left as an absence, so a reader can tell "nobody
#: checked" from "somebody accepted a weaker control on purpose".
CONTROL_NATIVE_PROTECTED_REF = "NATIVE_PROTECTED_REF"
CONTROL_HUMAN_EXACT_HEAD = "HUMAN_EXACT_HEAD_COMPENSATING_CONTROL"

#: The state each control class produces. One mapping, so the state and the
#: control can never be set independently and disagree.
STATE_FOR_CONTROL = {CONTROL_NATIVE_PROTECTED_REF: PROTECTED_STATE,
                     CONTROL_HUMAN_EXACT_HEAD: MIDTERM_STATE}

#: The only two renderings of `${{ github.ref_protected }}` GitHub emits. A
#: real `bool` is also accepted because Python callers (and tests) have one.
#: Everything else — "True", "TRUE", "1", "yes", "", None — is refused rather
#: than coerced: `bool("false")` is `True`, and a lenient parser here would
#: turn an unprotected build into a protected claim silently, which is the
#: exact defect this module was corrected for.
PLATFORM_BOOLEANS = {"true": True, "false": False}


def assert_ref_protected(value) -> bool:
    """Parse the platform's protection fact. Fail closed, never coerce.

    Absent is refused rather than defaulted to `False`. A default would be the
    safe direction today, and would also mean a workflow that forgot to pass
    the input produced a mid-term identity that looked deliberate. The build
    should stop and say the input is missing."""
    if isinstance(value, bool):
        return value
    if value is None:
        refuse("category=build_ref_protection_not_supplied — the build must "
               "be told `${{ github.ref_protected }}`; it cannot observe from "
               "inside the runner whether the ref it was dispatched from is "
               "protected, and guessing is what produced a false claim before")
    if not isinstance(value, str) or value not in PLATFORM_BOOLEANS:
        refuse(f"category=build_ref_protection_malformed value={value!r} — "
               f"expected exactly one of {sorted(PLATFORM_BOOLEANS)}; any "
               "other spelling is an unrecognised platform rendering and is "
               "refused rather than coerced, because `bool(\"false\")` is True")
    return PLATFORM_BOOLEANS[value]

#: The five digests D1/D2 compare against the operator's approval, and the
#: names they are compared under. The lane's `ENGINE_IDENTITY_FIELDS` is the
#: consumer half; this is the producer half, and a test binds the two — a build
#: that emitted four of five would make the fifth unapprovable, and the failure
#: would look like operator error.
BUILT_IDENTITY_FIELDS = ("engine_artifact_sha256", "engine_source_sha256",
                         "runtime_lock_sha256", "sbom_sha256",
                         "provenance_sha256")

#: What the build must be told about itself, and cannot derive. A process
#: cannot verify which ref dispatched it or which image it is running in; the
#: platform supplies both, and the record says so rather than implying the
#: build checked.
PROVENANCE_FIELDS = ("build_workflow_run_id", "build_workflow_run_attempt",
                     "build_ref", "build_head_sha", "runner_image_digest",
                     "repository_numeric_id")

#: Deliberately NOT in `PROVENANCE_FIELDS`. That tuple is checked with `if not
#: context.get(f)`, and `build_ref_protected` is legitimately `False` on this
#: repository — a truthiness check would report the honest answer as a missing
#: field and refuse every unprotected build. It is validated by
#: `assert_ref_protected` instead, which distinguishes absent from False.
PROTECTION_FIELD = "build_ref_protected"

PROVENANCE_DIGEST_LABEL = b"trusted-engine-provenance-v1"


def provenance_record(context: dict, *, roles: dict) -> dict:
    """What the builder says about the build, and its digest.

    Includes the source roles by commit, so the provenance is about a specific
    pair of commits rather than about "a build that happened". A provenance
    that named only the run would be satisfied by any artifact that run
    produced."""
    if not isinstance(context, dict):
        refuse("category=build_context_not_an_object")
    missing = [f for f in PROVENANCE_FIELDS if not context.get(f)]
    if missing:
        refuse(f"category=build_context_incomplete fields={missing} — a "
               "provenance record with a hole in it is a builder declining to "
               "say which build this was")
    if not str(context["build_ref"]).startswith("refs/heads/"):
        refuse(f"category=build_ref_not_a_branch ref={context['build_ref']!r} "
               "— the engine is built from a branch; a tag can be moved to a "
               "different commit without changing its name. Being a branch is "
               "not by itself protection: that is `build_ref_protected`")
    protected = assert_ref_protected(context.get(PROTECTION_FIELD))
    control_class = (CONTROL_NATIVE_PROTECTED_REF if protected
                     else CONTROL_HUMAN_EXACT_HEAD)
    record = {f: context[f] for f in PROVENANCE_FIELDS}
    # Inside the digested blob, so flipping false to true in a published
    # record without rebuilding breaks `provenance_sha256` and the mismatch is
    # caught by `assert_built_record`. A protection claim that could be edited
    # after the fact would be worth nothing.
    record[PROTECTION_FIELD] = protected
    record["native_branch_protection"] = protected
    record["control_class"] = control_class
    record["source_roles"] = {role: commit for role, commit in sorted(
        roles.items())}
    record["builder"] = "trusted-engine-build"
    blob = json.dumps(record, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    record["provenance_sha256"] = hashlib.sha256(
        PROVENANCE_DIGEST_LABEL + b"\x00" + blob).hexdigest()
    supplied = ("the platform supplied run id, run attempt, ref, head, runner "
                "image and ref-protection status; none of them can be "
                "verified from inside the build. What this record proves is "
                "that a build SAID so. ")
    if protected:
        record["honest_scope"] = supplied + (
            f"Native branch protection was active on {context['build_ref']}, "
            "and the build held no provider credential.")
    else:
        record["honest_scope"] = supplied + (
            f"This build ran from exact default-branch SHA "
            f"{context['build_head_sha']} and held no provider credential. "
            "Native branch protection was NOT active. The artifact's "
            "authority for the mid-term lane comes only from deterministic "
            "reproduction, out-of-band operator approval of the exact five "
            "digests, and the accepted human exact-head compensating control. "
            "This record is not protected-ref or write-separated trusted "
            "evidence.")
    return record


#: Everything in a provenance record EXCEPT the two fields derived from it.
#: Recomputing over the rest is what makes the digest a seal rather than a
#: label that travels alongside the thing it describes.
PROVENANCE_DERIVED_FIELDS = ("provenance_sha256", "honest_scope")


def recomputed_provenance_digest(provenance: dict) -> str:
    """Re-seal the provenance from its own contents.

    Comparing `provenance["provenance_sha256"]` against the copy at the top of
    the identity record catches a truncated write and nothing else — an editor
    who flips `native_branch_protection` to `true` leaves both copies equal
    and both wrong. Recomputing is the only check that notices, which is why
    the protection fields are inside the digested blob."""
    if not isinstance(provenance, dict):
        refuse("category=engine_provenance_not_an_object")
    body = {k: v for k, v in provenance.items()
            if k not in PROVENANCE_DERIVED_FIELDS}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    return hashlib.sha256(PROVENANCE_DIGEST_LABEL + b"\x00" + blob).hexdigest()


def built_package(*, roles, repository_numeric_id: int, artifact_sha256: str,
                  context: dict, cwd: str = ".") -> dict:
    """The identity record a protected build publishes beside the artifact.

    Everything except the provenance is recomputed here from the same git
    blobs the artifact was built from, so the record cannot describe a
    different tree than the tarball. The artifact digest is passed IN rather
    than recomputed, because the caller is the one that wrote the file and
    hashing it again here would hash whatever is now at that path.

    `engine-identity.json` is what `d1cli.load_engine_identity` reads and what
    `enginebridge.assert_identity_is_this_engine` checks against the archive
    the run actually opened."""
    from .enginesource import multi_source_manifest
    from .runtimelock import load_lock

    if not isinstance(artifact_sha256, str) or len(artifact_sha256) != 64:
        refuse("category=engine_artifact_digest_malformed")
    manifest = multi_source_manifest(
        roles=roles, repository_numeric_id=repository_numeric_id, cwd=cwd)
    lock = load_lock(root=cwd)
    bill = sbom(roles=roles, cwd=cwd)
    provenance = provenance_record(context, roles=roles)
    if provenance["repository_numeric_id"] != repository_numeric_id:
        refuse("category=build_context_repository_mismatch — the provenance "
               "names one repository and the manifest was built for another")

    record = {
        # Derived from the platform fact the provenance recorded, never passed
        # in. A caller that could choose the state could choose the stronger
        # one, which is precisely the defect being corrected.
        "state": STATE_FOR_CONTROL[provenance["control_class"]],
        "build_ref_protected": provenance[PROTECTION_FIELD],
        "native_branch_protection": provenance["native_branch_protection"],
        "control_class": provenance["control_class"],
        "repository_numeric_id": repository_numeric_id,
        "engine_artifact_sha256": artifact_sha256,
        "engine_source_sha256": manifest["engine_source_sha256"],
        "runtime_lock_sha256": lock["lock_sha256"],
        "sbom_sha256": bill["sbom_sha256"],
        "provenance_sha256": provenance["provenance_sha256"],
        "source_roles": manifest["binding"],
        "provenance": provenance,
        "sbom": bill,
        "dependency_lock": lock,
        "honest_scope": (
            "the artifact is deterministic in the two source commits, so "
            "anyone with them can rebuild it and get this digest. That is "
            "reproducibility, not authority: what makes THIS record usable is "
            "that an operator approved these five digests out of band, and "
            "the lane compares their approval against the artifact it opened"
            + ("" if provenance["native_branch_protection"] else
               ". Native branch protection was not active on the source ref, "
               "so this record is eligible for MIDTERM_SINGLE_REPO_* evidence "
               "only and is refused by the trusted lane")),
    }
    return assert_built_record(record)


def assert_built_record(record: dict) -> dict:
    """Five digests, all present, all 64 hex. The consumer's half is
    `enginebridge.assert_identity_is_this_engine`, and this is the producer
    refusing to publish a record that one would reject."""
    if not isinstance(record, dict):
        refuse("category=engine_identity_not_an_object")
    if record.get("state") not in BUILT_STATES:
        refuse(f"category=engine_identity_wrong_state "
               f"state={record.get('state')!r} expected one of "
               f"{list(BUILT_STATES)}")
    # The protection fields are required, not optional-with-a-default: a
    # record that omits them is one a reader would have to guess about, and
    # the guess that lost us a build was the optimistic one.
    protected = assert_ref_protected(record.get(PROTECTION_FIELD))
    if record.get("native_branch_protection") is not protected:
        refuse("category=engine_identity_protection_fields_disagree — "
               "`build_ref_protected` and `native_branch_protection` must be "
               "the same fact")
    if record.get("control_class") not in STATE_FOR_CONTROL:
        refuse(f"category=engine_identity_control_class_unknown "
               f"control_class={record.get('control_class')!r} expected one "
               f"of {sorted(STATE_FOR_CONTROL)}")
    if STATE_FOR_CONTROL[record["control_class"]] != record["state"]:
        refuse(f"category=engine_identity_state_control_mismatch "
               f"state={record['state']!r} "
               f"control_class={record['control_class']!r} — the state is "
               "derived from the control class and these two disagree")
    if (record["control_class"] == CONTROL_NATIVE_PROTECTED_REF) is not \
            protected:
        refuse(f"category=engine_identity_control_class_contradicts_protection "
               f"control_class={record['control_class']!r} "
               f"build_ref_protected={protected} — a record claiming a native "
               "protected ref while the platform said the ref was "
               "unprotected is the false claim this state model exists to "
               "prevent")
    bad = [f for f in BUILT_IDENTITY_FIELDS
           if not isinstance(record.get(f), str) or len(record[f]) != 64]
    if bad:
        refuse(f"category=engine_identity_incomplete fields={bad} — the "
               "operator approves five digests; publishing fewer makes the "
               "missing one unapprovable and the failure look like operator "
               "error")
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        refuse("category=engine_provenance_missing — the identity record must "
               "carry the provenance its digest seals")
    if provenance.get("provenance_sha256") != record["provenance_sha256"]:
        refuse("category=engine_provenance_digest_mismatch — the record's "
               "provenance digest and the provenance it carries disagree")
    resealed = recomputed_provenance_digest(provenance)
    if resealed != record["provenance_sha256"]:
        refuse(f"category=engine_provenance_digest_does_not_reseal "
               f"recorded={record['provenance_sha256'][:16]} "
               f"recomputed={resealed[:16]} — the provenance content was "
               "edited after the build sealed it. Flipping "
               "`native_branch_protection` in a published record is exactly "
               "this, and it does not survive a rebuild of the digest")
    for field in (PROTECTION_FIELD, "native_branch_protection",
                  "control_class"):
        if provenance.get(field) != record.get(field):
            refuse(f"category=engine_identity_provenance_disagree "
                   f"field={field} — the identity header and the sealed "
                   "provenance state different things about protection")
    return record


#: An identity record is a few tens of kilobytes. The bound exists so that a
#: consumer pointed at something enormous refuses by name instead of reading
#: it into memory first.
IDENTITY_DOCUMENT_MAX_BYTES = 1 << 20


def _reject_duplicate_keys(pairs):
    """`json.loads` keeps the LAST value for a repeated key, silently.

    A document carrying `"state"` twice would therefore be read one way by a
    validator and another way by anything that looked at the raw text — and
    the second `"state"` is exactly where a relabelling would hide."""
    seen = set()
    for key, _ in pairs:
        if key in seen:
            refuse(f"category=engine_identity_duplicate_key key={key!r} — a "
                   "repeated key resolves to the last value, so the document "
                   "means different things to a parser and to a reader")
        seen.add(key)
    return dict(pairs)


def strict_load_built_identity(path: str) -> dict:
    """Read an identity record from disk and validate it completely.

    The single loader both lanes use. It exists because the strictness added
    to `assert_built_record` was worth nothing while the live consumer read
    the file with a plain `json.load` and checked five string lengths: a
    mid-term record could be relabelled `PROTECTED_ENGINE_IDENTITY` by editing
    four header fields, leaving all five approved digests and the sealed
    provenance untouched, and the lane-grade helper saw a self-consistent
    protected header and accepted it.

    The provenance reseal is the check that catches that edit, and a validator
    nobody calls is not a control."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(IDENTITY_DOCUMENT_MAX_BYTES + 1)
    except OSError as exc:
        refuse(f"category=engine_identity_unreadable "
               f"exception_class={type(exc).__name__} — the path is not "
               "reported; it can carry a runner temp directory layout")
    if len(raw) > IDENTITY_DOCUMENT_MAX_BYTES:
        refuse(f"category=engine_identity_too_large "
               f"limit_bytes={IDENTITY_DOCUMENT_MAX_BYTES}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        refuse("category=engine_identity_not_utf8")
    try:
        record = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        refuse(f"category=engine_identity_not_json "
               f"exception_class={type(exc).__name__}")
    if not isinstance(record, dict):
        refuse("category=engine_identity_not_an_object")
    return assert_built_record(record)


def assert_build_holds_no_provider_secret(environ) -> dict:
    """The build must not be able to reach the provider, at all.

    Checked in the build rather than only in review, because the workflow that
    produces what the lane trusts is exactly the workflow whose compromise
    would be worth the most — and a build that could spend the credential is a
    build that could be made to."""
    from .runtimebinding import PROVIDER_SECRET_NAME

    present = sorted(name for name in environ
                     if name in (PROVIDER_SECRET_NAME,
                                 "TRUSTED_EVIDENCE_SIGNING_KEY",
                                 "TRUSTED_OPERATOR_TRUST_STORE")
                     and environ.get(name))
    if present:
        refuse(f"category=engine_build_holds_a_capability names={present} — "
               "the build produces what the lane trusts; a build that can "
               "reach the provider or sign evidence is a build whose "
               "compromise is worth more than the lane's")
    return {"provider_secret_present": False,
            "signing_key_present": False,
            "checked_names": [PROVIDER_SECRET_NAME,
                              "TRUSTED_EVIDENCE_SIGNING_KEY",
                              "TRUSTED_OPERATOR_TRUST_STORE"],
            "honest_scope": ("this proves the NAMES are unset in this "
                             "process. It does not prove the workflow could "
                             "not have set them — that is what the job's "
                             "absent `environment:` and the review of this "
                             "file are for")}
