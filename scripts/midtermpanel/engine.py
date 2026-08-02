"""The one bridge to the approved verifier engine.

## Why there is exactly one of these

The mandate is explicit and it matches what this repository has already learned:
do not re-implement atomization, splitting, PIN validation, request assembly,
secret scanning, OriginMap, batching, response schemas, verdict validation or
panel aggregation. Every one of those took a macro-cycle to get right, and a
second implementation would not be a second implementation — it would be a first
implementation of subtly different rules, running against the same policy
document, producing evidence that looked the same.

So this module adapts; it does not decide. It resolves which engine to use,
loads it through `trustedlane.enginebridge`, and hands the caller the engine's
own functions. Anything that looks like review logic belongs on the other side
of this bridge.

## Provenance is part of the answer

There is no approved engine release yet — `GET /releases` returns `[]` and
operator prerequisite 14 is unrecorded. So during Phase A the panel builds the
artifact deterministically from two pinned commits, which is reproducible by
anyone and approved by nobody.

That distinction is carried in the record rather than assumed away:

    APPROVED_RELEASE                      an operator approved these digests
    REBUILT_FROM_PINNED_SOURCE_TEST_ONLY  reproducible, unapproved, Phase A only

`assert_provenance_permits_real_panel` refuses the second one for a run that
would actually spend money. A dry run may use it; a real panel may not. Without
that split, "we built it ourselves and it matched" would quietly become the
approval it is not.
"""

from __future__ import annotations

import os

from .errors import refuse

APPROVED_RELEASE = "APPROVED_RELEASE"
REBUILT_TEST_ONLY = "REBUILT_FROM_PINNED_SOURCE_TEST_ONLY"

#: The two source roles the engine artifact is built from. Names, not values:
#: the values are refs resolved at call time and recorded in the evidence.
PROTECTED_ROLE = "protected_trusted_lane"
CANDIDATE_ROLE = "candidate_verifier"


def source_roles(*, protected_sha: str, candidate_sha: str) -> dict:
    """The engine's identity, as the two commits it is built from."""
    from .status import assert_candidate_sha
    return {
        PROTECTED_ROLE: assert_candidate_sha(protected_sha,
                                             field=PROTECTED_ROLE),
        CANDIDATE_ROLE: assert_candidate_sha(candidate_sha,
                                             field=CANDIDATE_ROLE),
    }


def engine_digest(*, root: str = ".", roles: dict = None) -> str:
    """A cheap, stable identity for the engine this review is bound to.

    The digest of the two source commits, NOT of the built tarball. Preflight
    needs an engine identity to put in the dedupe binding and it must not build a
    multi-megabyte artifact to get one; the count job computes and records the
    real `engine_artifact_sha256` when it actually opens the artifact.

    These are different digests with different meanings, and conflating them is
    the mistake `runtimebinding` exists to catch — so they have different names
    here and the evidence carries both."""
    from .evidence import digest_of
    if roles is None:
        roles = _resolved_roles(root=root)
    return digest_of({"engine_source_roles": roles})


def _resolved_roles(*, root: str) -> dict:
    """Resolve the two roles from the environment, refusing a guess.

    Deliberately no defaults. The trusted-lane role is whatever protected commit
    the panel runs from and the candidate role is the head under review; a
    default for either would silently bind a review to an engine nobody chose."""
    protected = os.environ.get("MIDTERM_ENGINE_PROTECTED_SHA")
    candidate = os.environ.get("MIDTERM_ENGINE_CANDIDATE_SHA")
    if not protected or not candidate:
        refuse("category=engine_roles_not_resolved variables="
               "['MIDTERM_ENGINE_PROTECTED_SHA', "
               "'MIDTERM_ENGINE_CANDIDATE_SHA'] — the engine is built from two "
               "exact commits; defaulting either would bind the review to an "
               "engine nobody chose")
    return source_roles(protected_sha=protected, candidate_sha=candidate)


def assert_provenance_permits_real_panel(provenance: str) -> str:
    """A locally rebuilt artifact may not back a run that spends money.

    Reproducible is not approved. Anyone with the two commits can rebuild this
    artifact and get the same digest — that is determinism, and determinism is
    what makes approval MEANINGFUL rather than what replaces it. Operator
    prerequisite 14 is the approval, and it is unrecorded."""
    if provenance == APPROVED_RELEASE:
        return provenance
    if provenance == REBUILT_TEST_ONLY:
        refuse(f"category=engine_provenance_is_test_only provenance={provenance} "
               "— this artifact was rebuilt from pinned source, which is "
               "reproducible and approved by nobody. A dry run may use it; a "
               "panel that spends money may not. Approval of the five digests "
               "is operator prerequisite 14")
    refuse(f"category=engine_provenance_unknown provenance={provenance!r}")


def build_test_only_artifact(*, destination: str, roles: dict,
                             repository_numeric_id: int, cwd: str = ".") -> dict:
    """Build the artifact deterministically, labelled as unapproved.

    Thin adapter over `enginesource.build_engine_artifact` — the same function
    the protected build workflow calls, so the bytes are the bytes. The only
    thing added is the provenance label, which travels with the record."""
    from trustedlane import enginesource
    record = enginesource.build_engine_artifact(
        roles=roles, destination=destination,
        repository_numeric_id=repository_numeric_id, cwd=cwd)
    return {**record, "provenance": REBUILT_TEST_ONLY,
            "honest_scope": ("deterministic and reproducible; approved by "
                             "nobody. Not usable for a run that spends money")}


def open_engine(artifact_path: str, *, destination: str,
                expected_sha256: str) -> dict:
    """Extract and load the engine, returning its own callables.

    Loading goes through `enginebridge`, which imports the artifact under a
    namespace derived from its root so the artifact's modules can never be
    confused with an in-tree `verifier` package. That collision cost an entire
    exchange to diagnose; this bridge is the reason the mid-term lane does not
    get to repeat it."""
    from trustedlane import artifactload, enginebridge
    artifactload.extract(artifact_path, destination=destination,
                         expected_sha256=expected_sha256)
    return enginebridge.load_engine(destination)
