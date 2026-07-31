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
