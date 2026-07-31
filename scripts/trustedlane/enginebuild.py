"""Build the engine identity CANDIDATE package — and label it as a candidate.

`enginepolicy` says what a trusted engine identity must contain and refuses to
call any of it authenticated. This module produces the half of that record a
candidate branch can honestly compute, and leaves the other half empty on
purpose.

The split is not arbitrary. Three of the thirteen identity fields are facts
about *source*, derivable by anyone holding the tree, and reproducible by a
reader who clones the same commit:

    source_commit, code_cutoff_sha, source_tree_sha256, dependency_lock_sha256,
    sbom_sha256

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
import os

from .errors import refuse

CANDIDATE_STATE = "UNVERIFIED_ENGINE_IDENTITY_CANDIDATE"

#: The engine is the trusted lane itself — the code that will run inside D1/D2
#: holding a credential. Deliberately NOT the whole repository: the engine's
#: identity should not change because an unrelated app file changed, or the
#: operator would be re-approving it constantly and would stop reading.
ENGINE_SOURCE_ROOTS = ("scripts/trustedlane",)

#: Files that define the dependency closure of a D1/D2 run. The D0/D1/D2
#: workflows install exactly `pytest` and `PyYAML`, so the lock surface is small
#: and is recorded rather than described.
ENGINE_RUNTIME_DEPENDENCIES = ("pytest", "PyYAML")

#: Fields this branch can honestly compute.
COMPUTABLE_FIELDS = (
    "source_commit",
    "code_cutoff_sha",
    "source_tree_sha256",
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
        "same as build_workflow_run_id",
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


def _tracked_engine_files(*, root: str = ".") -> list:
    """Every file under the engine roots, sorted, as repo-relative paths.

    Walked from disk rather than asked of git: the digest must describe the tree
    that would be shipped, and `git ls-files` would silently omit an untracked
    file sitting in the engine directory — which is exactly the file worth
    noticing."""
    found = []
    for engine_root in ENGINE_SOURCE_ROOTS:
        base = os.path.join(root, engine_root)
        if not os.path.isdir(base):
            refuse(f"category=engine_source_root_missing root={engine_root}")
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for filename in sorted(filenames):
                if filename.endswith(".pyc"):
                    continue
                absolute = os.path.join(dirpath, filename)
                found.append(os.path.relpath(absolute, root))
    if not found:
        refuse("category=engine_source_empty — a digest over nothing is a "
               "digest that matches any engine")
    return sorted(found)


def source_tree_digest(*, root: str = ".") -> dict:
    """A digest over the engine tree: paths AND contents, both bound.

    Path and content are hashed together because a digest over contents alone
    is identical if two files swap names, and a rename is a real change to what
    runs. Each entry is length-prefixed so that no combination of path and
    content can be reinterpreted as a different combination."""
    files = _tracked_engine_files(root=root)
    hasher = hashlib.sha256()
    hasher.update(b"trusted-engine-tree-v1\x00")
    per_file = {}
    for relative in files:
        with open(os.path.join(root, relative), "rb") as handle:
            blob = handle.read()
        digest = hashlib.sha256(blob).hexdigest()
        per_file[relative] = digest
        encoded = relative.encode("utf-8")
        hasher.update(len(encoded).to_bytes(4, "big"))
        hasher.update(encoded)
        hasher.update(bytes.fromhex(digest))
    return {
        "source_tree_sha256": hasher.hexdigest(),
        "file_count": len(files),
        "files": per_file,
        "roots": list(ENGINE_SOURCE_ROOTS),
    }


def dependency_lock(*, root: str = ".") -> dict:
    """The runtime dependency surface of a trusted run, recorded exactly.

    Deliberately NOT a pip freeze of this machine: that would record the
    development container, which is not what a D1/D2 runner installs. It records
    the names the workflows actually install, and says plainly that pinning them
    to versions and hashes is an open item — because claiming a reproducible
    lock while installing unpinned names would be the same class of overclaim
    this programme keeps finding."""
    names = sorted(ENGINE_RUNTIME_DEPENDENCIES)
    payload = json.dumps({"install": names, "pinned": False},
                         sort_keys=True, separators=(",", ":")).encode()
    return {
        "dependency_lock_sha256": hashlib.sha256(payload).hexdigest(),
        "declared_dependencies": names,
        "pinned_to_versions": False,
        "pinned_to_hashes": False,
        "honest_scope": ("these are the names the D0/D1/D2 workflows install, "
                         "not resolved versions; a genuinely reproducible lock "
                         "needs `pip install --require-hashes` against a pinned "
                         "requirements file, which is an open item"),
    }


def sbom(*, root: str = ".") -> dict:
    """A minimal SBOM over what the engine is and what it loads.

    Every stdlib import the engine makes is listed alongside the third-party
    ones, because "engine depends only on the standard library plus PyYAML" is a
    claim worth being able to check rather than assert."""
    import ast as _ast

    files = _tracked_engine_files(root=root)
    imported = set()
    unparsed = []
    for relative in files:
        if not relative.endswith(".py"):
            continue
        try:
            with open(os.path.join(root, relative), "rb") as handle:
                tree = _ast.parse(handle.read())
        except (SyntaxError, ValueError):
            unparsed.append(relative)
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, _ast.ImportFrom):
                if node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
    if unparsed:
        refuse(f"category=engine_source_unparsable files={unparsed} — an SBOM "
               "that skips the file it could not read is an SBOM with a hole "
               "exactly where the interesting thing would be")
    components = sorted(imported)
    payload = json.dumps({"components": components},
                         sort_keys=True, separators=(",", ":")).encode()
    return {
        "sbom_sha256": hashlib.sha256(payload).hexdigest(),
        "components": components,
        "third_party": sorted(set(components) & {"yaml", "pytest"}),
        "honest_scope": ("top-level import names resolved by AST over the "
                         "engine tree; it does not resolve transitive "
                         "dependencies of third-party packages"),
    }


def candidate_package(*, source_commit: str, code_cutoff_sha: str,
                      root: str = ".") -> dict:
    """The full candidate package — computable fields filled, the rest empty.

    `source_commit` and `code_cutoff_sha` are parameters rather than read from
    git here, because reading them from the working tree would let an unclean
    tree describe itself as a commit. The caller passes what it observed, and
    the tree digest is computed independently so the two can disagree loudly."""
    from .identity import assert_commit_sha

    assert_commit_sha(source_commit, field="source_commit")
    assert_commit_sha(code_cutoff_sha, field="code_cutoff_sha")

    tree = source_tree_digest(root=root)
    lock = dependency_lock(root=root)
    bill = sbom(root=root)

    record = {
        "state": CANDIDATE_STATE,
        "source_commit": source_commit,
        "code_cutoff_sha": code_cutoff_sha,
        "source_tree_sha256": tree["source_tree_sha256"],
        "dependency_lock_sha256": lock["dependency_lock_sha256"],
        "sbom_sha256": bill["sbom_sha256"],
    }
    for field in UNAVAILABLE_FIELDS:
        record[field] = None
    record["unavailable_because"] = dict(UNAVAILABLE_FIELDS)
    record["detail"] = {"tree": tree, "dependency_lock": lock, "sbom": bill}

    binding = {k: record[k] for k in COMPUTABLE_FIELDS}
    blob = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    record["candidate_package_sha256"] = hashlib.sha256(
        b"engine-identity-candidate-v1\x00" + blob).hexdigest()
    record["honest_scope"] = (
        "computed BY the candidate branch, over the candidate branch. Every "
        "digest here is reproducible by anyone with the same tree, which is "
        "exactly why none of it is evidence of anything: reproducibility is "
        "not authority. It cannot unlock D1, and assert_engine_approved refuses "
        "it along with every other identity.")
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
