"""Which code may hold the credential, and which may never.

The precursor package is the artifact under review. A credential-bearing
process that imports it lets the reviewed branch choose what its own reviewer
does — including what the reviewer says about it. Two refusals, because there
are two ways to make the mistake: by path, and by already-imported module."""

from __future__ import annotations

import os
import sys

from .errors import refuse

#: Distribution shapes, in the order the mandate prefers.
PREFERRED = "SEPARATE_PROTECTED_VERIFIER_REPOSITORY"
ACCEPTABLE = "PROTECTED_MAIN_WORKFLOW_SIGNED_ARTIFACT_BY_DIGEST"
REFUSED = "IMPORT_FROM_CANDIDATE_CHECKOUT"

#: Path fragments that identify the candidate package. A credential-bearing
#: process must not load an engine from any of them.
#:
#: These stay refused even after the precursor package merges to a protected
#: default branch. Merging changes who vouched for the code, and a lane that
#: silently starts importing `scripts/verifier` because the path happens to be
#: protected now is a lane whose containment moved without anyone deciding.
#: Relaxing this is a phase change with its own approval, not an edit — and
#: `test_candidate_path_markers_are_a_ratchet` reddens if the set shrinks.
CANDIDATE_PATH_MARKERS = (
    os.path.join("scripts", "verifier"),
    os.path.join("candidate", "scripts"),
)

#: Top-level module names the candidate package owns. Checked as *reachability*
#: from each `sys.path` entry, never by importing: `import verifier` to find out
#: whether importing `verifier` is possible would already have run it.
CANDIDATE_PACKAGE_NAMES = ("verifier",)

#: Module prefixes the candidate package owns.
CANDIDATE_MODULE_PREFIXES = ("verifier.", "verifier")

ENGINE_IDENTITY_FIELDS = (
    "source_commit",
    "code_cutoff_sha",
    "source_tree_sha256",
    "artifact_sha256",
    "dependency_lock_sha256",
    "sbom_sha256",
    "runner_image_digest",
    "build_workflow_run_id",
    "build_workflow_run_attempt",
    "signer_identity",
    "signature",
    "provenance",
    "independent_approval_record",
)


def assert_not_candidate_checkout(engine_path: str) -> str:
    """Refuse an engine path that is (or is inside) the candidate checkout."""
    if not isinstance(engine_path, str) or not engine_path:
        refuse("category=engine_path_missing")
    normalized = os.path.normpath(engine_path)
    for marker in CANDIDATE_PATH_MARKERS:
        if marker in normalized:
            refuse(
                f"category=engine_from_candidate_checkout distribution="
                f"{REFUSED} marker={marker} — the candidate package is the "
                "artifact under review and must never be the engine that "
                f"reviews it; use {PREFERRED} or {ACCEPTABLE}")
    return normalized


def _reachable_candidate_packages(search_path) -> list:
    """Candidate packages an `import` from this path would find.

    The parent directory is the realistic hazard, not the package directory: a
    process with `scripts/` on `sys.path` can `import verifier` even though no
    path entry contains the string `scripts/verifier`. Probed with `isfile`
    rather than `find_spec`, because a meta-path finder or a `.pth` file can run
    code during a lookup."""
    hits = []
    for entry in search_path:
        base = os.path.normpath(entry or ".")
        for name in CANDIDATE_PACKAGE_NAMES:
            package = os.path.join(base, name)
            # A BARE DIRECTORY is enough. Requiring `__init__.py` was the bug:
            # PEP 420 namespace packages have none, and `import verifier.executor`
            # resolves from a plain directory just fine — which the review
            # demonstrated by importing it. Checking for the marker file asked
            # the wrong question; the right one is "would an import find this".
            if (os.path.isdir(package)
                    or os.path.isfile(package + ".py")):
                hits.append(package)
    return hits


def assert_no_candidate_import(*, modules=None, search_path=None) -> dict:
    """Refuse if the candidate package is imported, or merely importable.

    Checked at runtime rather than by reading the source: a transitive import,
    a `conftest.py`, or a stray `sys.path` entry can pull the candidate in
    without any line of this module mentioning it.

    `modules`/`search_path` exist so the refusals can be driven deterministically
    from a test instead of depending on the ambient interpreter. The trusted
    runner calls this with no arguments, so what it inspects is the real process;
    the parameters let trusted code describe a hypothetical, they do not let
    untrusted code describe itself as clean."""
    modules = sys.modules if modules is None else modules
    search_path = sys.path if search_path is None else search_path
    imported = sorted(
        name for name in modules
        if any(name == p or name.startswith(p + ".")
               for p in CANDIDATE_PACKAGE_NAMES))
    if imported:
        refuse(f"category=candidate_module_imported count={len(imported)} — "
               "the candidate package is loaded in this process; a "
               "credential-bearing run must not be able to call it")
    on_path = [entry for entry in search_path
               if any(marker in os.path.normpath(entry or ".")
                      for marker in CANDIDATE_PATH_MARKERS)]
    if on_path:
        refuse(f"category=candidate_path_importable count={len(on_path)} — "
               "the candidate package is on sys.path, so any import could "
               "reach it")
    reachable = _reachable_candidate_packages(search_path)
    if reachable:
        # The paths are named. They are the lane's own filesystem, not candidate
        # content, and an unnamed count is undiagnosable: the first real hit was
        # an EMPTY `scripts/verifier/` left behind by a branch switch — git
        # neither tracks nor removes empty directories — which is still a
        # namespace-package root that `import verifier` resolves to.
        refuse(f"category=candidate_package_reachable count={len(reachable)} "
               f"paths={sorted(reachable)} — a parent directory of the "
               "candidate package is on sys.path, so `import verifier` would "
               "load the artifact under review")
    return {"candidate_isolated": True,
            "checked": ["sys.modules", "sys.path", "package_reachability"]}


def validate_engine_identity_shape(record: dict) -> dict:
    """SHAPE only. Passing this is necessary and nowhere near sufficient."""
    if not isinstance(record, dict):
        refuse("category=engine_identity_not_object")
    missing = [f for f in ENGINE_IDENTITY_FIELDS if record.get(f) in (None, "")]
    if missing:
        refuse(f"category=engine_identity_incomplete missing={missing}")
    if record.get("distribution") not in (PREFERRED, ACCEPTABLE):
        refuse(f"category=engine_distribution_not_permitted "
               f"distribution={record.get('distribution')!r}")
    return {"shape_valid": True, "authenticated": False,
            "verification_status": "SHAPE_ONLY_NOT_AUTHENTICATED"}


def assert_engine_approved(record: dict) -> None:
    """The gate a credential-bearing runner applies before loading an engine.

    Always refuses in D0. Not because the identity package is unsatisfiable,
    but because verifying it needs a trusted key and an operator approval
    record, and neither exists in an unmerged branch. A version of this that
    returned successfully would be the branch approving its own engine."""
    refuse(
        "category=engine_not_approved_in_D0 — engine approval requires a "
        "trusted verification key and an operator approval record held "
        "outside this branch; D0 exists to make the refusal explicit rather "
        "than to work around it")
