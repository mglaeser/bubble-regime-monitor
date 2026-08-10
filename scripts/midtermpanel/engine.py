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

## The reviewer is not the reviewed

The previous version of this file resolved the engine's source from
`MIDTERM_ENGINE_CANDIDATE_SHA`, and the workflow set that variable to the head
of the pull request under review. Both real entry points then built an artifact
from that commit and imported it — inside a job holding the provider key.

That is not a configuration mistake, it is an inverted trust direction. A pull
request could edit `scripts/verifier`, and its own edited code would be the code
that reviewed it, next to the credential. Every other control in this lane rests
on one sentence — *the candidate is data, never code* — and that wiring made the
candidate code.

So the engine's identity and the candidate's identity are now separate values
with separate names, and they are never read from the same place:

    approved_engine_source_sha        an operator pinned this; the reviewer
    approved_engine_protected_sha     an operator pinned this; the lane half
    approved_engine_artifact_sha256   an operator approved these bytes
    reviewed_candidate_head_sha       the pull request; DATA, an input

`assert_engine_source_is_not_the_reviewed_candidate` refuses when they coincide.
There is deliberately no bootstrap exception and no "unless the operator sets
this other variable" escape: an exception that lets the candidate be the engine
is the defect, spelled with an extra step. If the reviewer must learn about a
change to `scripts/verifier`, that change is merged first and the operator pins
a new engine release — which is what "approved" means.

## Provenance is part of the answer

An approved engine release now exists: `midterm-panel-engine-2026-08-10`, whose
seven binding fields are recorded in
`governance/midterm-panel-engine-release.json`. Before it did, this lane could
only build the artifact deterministically from the two pinned commits — which is
reproducible by anyone and approved by nobody, and is still exactly what a dry
run does.

That distinction is carried in the record rather than assumed away:

    APPROVED_RELEASE                      an operator approved these digests
    REBUILT_FROM_PINNED_SOURCE_TEST_ONLY  reproducible, unapproved, Phase A only

`assert_provenance_permits` refuses the second one in provider mode. A dry run
may use it; a run that spends money may not. Without that split, "we built it
ourselves and it matched" would quietly become the approval it is not.

## Mode is a parameter, not a discovered fact

`load_engine_for_mode` is the only way to obtain an engine, and it takes the
mode. The two gates it applies point in opposite directions on purpose:

    provider mode  refuses an unapproved artifact  (don't spend on a guess)
    dry-run mode   refuses a live transport        (don't spend at all)

Neither can be satisfied by inspecting the environment harder, because the
environment is what the caller controls.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

from .errors import refuse

MODE_PROVIDER = "PROVIDER_BACKED"
MODE_DRY_RUN = "DRY_RUN_NO_PROVIDER"
MODES = (MODE_PROVIDER, MODE_DRY_RUN)

APPROVED_RELEASE = "APPROVED_RELEASE"
REBUILT_TEST_ONLY = "REBUILT_FROM_PINNED_SOURCE_TEST_ONLY"

#: The two build roles `trustedlane.enginesource` accepts. The names are
#: upstream's and they are historical: `candidate_verifier` means "the commit
#: `scripts/verifier` is taken from", which in the upstream promotion lane
#: happened to be a candidate. In THIS lane it is the approved engine source and
#: has nothing to do with the pull request under review — that conflation is
#: exactly what this module now refuses, so the name is used only where the
#: upstream API demands it and never as an identity in our own records.
ENGINE_SOURCE_ROLE = "candidate_verifier"
PROTECTED_ROLE = "protected_trusted_lane"

#: Where the approved release is configured. Read once, in one function, so
#: that "which engine is this" has a single answer with a single provenance.
ENGINE_SOURCE_ENV = "MIDTERM_APPROVED_ENGINE_SOURCE_SHA"
ENGINE_PROTECTED_ENV = "MIDTERM_APPROVED_ENGINE_PROTECTED_SHA"
ENGINE_ARTIFACT_ENV = "MIDTERM_APPROVED_ENGINE_ARTIFACT_SHA256"
ENGINE_RELEASE_TAG_ENV = "MIDTERM_APPROVED_ENGINE_RELEASE_TAG"

#: The builder's own identity record, materialised from the exact release.
#: Rebuilding the artifact from pinned commits proves reproducibility and
#: nothing else: it says the bytes can be produced, not which build produced
#: the approved ones, under what control, or whether the ref was protected.
#: Only the identity document carries that, and only if something reads it.
ENGINE_IDENTITY_PATH_ENV = "MIDTERM_APPROVED_ENGINE_IDENTITY_PATH"
ENGINE_IDENTITY_DIGEST_ENV = "MIDTERM_APPROVED_ENGINE_IDENTITY_SHA256"
ENGINE_PROTECTION_ENV = "MIDTERM_APPROVED_ENGINE_NATIVE_BRANCH_PROTECTION"
ENGINE_CONTROL_CLASS_ENV = "MIDTERM_APPROVED_ENGINE_CONTROL_CLASS"

#: Retired names. Their presence is refused rather than ignored: a workflow
#: still exporting `MIDTERM_ENGINE_CANDIDATE_SHA` is a workflow that still
#: believes the head under review selects the engine, and silently reading a
#: different variable would leave that belief in place and untested.
RETIRED_ENGINE_ENV = ("MIDTERM_ENGINE_CANDIDATE_SHA",
                      "MIDTERM_ENGINE_PROTECTED_SHA")

#: A GitHub source archive is not an engine artifact. The digests are the same
#: shape and an operator copying the wrong one from a release page would produce
#: a run that verified a digest nobody meant to approve.
GITHUB_ZIP_MARKERS = (".zip", "/zipball/", "/tarball/", "codeload.github.com")


def assert_mode(mode: str) -> str:
    """Modes are named, not inferred. An unknown one is refused."""
    if mode not in MODES:
        refuse(f"category=engine_mode_unknown mode={mode!r} "
               f"permitted={sorted(MODES)} — the mode selects which way the "
               "gates point, so guessing it disables one of them")
    return mode


def resolve_release_config(environ=None) -> dict:
    """The approved engine release, from configuration the candidate cannot set.

    Deliberately no defaults for the two source commits. A default would bind
    the review to an engine nobody chose, and the failure mode of a wrong
    default here is not a crash — it is a review that ran, produced evidence and
    named the wrong reviewer."""
    from .status import assert_candidate_sha
    env = os.environ if environ is None else environ

    present_retired = [name for name in RETIRED_ENGINE_ENV if env.get(name)]
    if present_retired:
        refuse(f"category=retired_engine_variables_present "
               f"variables={sorted(present_retired)} — these selected the "
               "engine from the pull request head under review. They are not "
               "read any more, and a caller still setting them is a caller "
               "whose workflow was not updated; ignoring them would leave that "
               "workflow shipping and passing")

    source = env.get(ENGINE_SOURCE_ENV)
    protected = env.get(ENGINE_PROTECTED_ENV)
    if not source or not protected:
        refuse(f"category=approved_engine_release_not_configured "
               f"variables=['{ENGINE_SOURCE_ENV}', '{ENGINE_PROTECTED_ENV}'] — "
               "the engine is built from two exact operator-pinned commits; "
               "there is no default reviewer")

    artifact_sha256 = env.get(ENGINE_ARTIFACT_ENV) or None
    release_tag = env.get(ENGINE_RELEASE_TAG_ENV) or None
    if artifact_sha256 is not None:
        artifact_sha256 = assert_artifact_digest(artifact_sha256)

    config = {
        "approved_engine_source_sha": assert_candidate_sha(
            source, field="approved_engine_source_sha"),
        "approved_engine_protected_sha": assert_candidate_sha(
            protected, field="approved_engine_protected_sha"),
        "approved_engine_artifact_sha256": artifact_sha256,
        "approved_engine_release_tag": release_tag,
        "approved_engine_identity_path":
            env.get(ENGINE_IDENTITY_PATH_ENV) or None,
        "approved_engine_identity_sha256":
            env.get(ENGINE_IDENTITY_DIGEST_ENV) or None,
    }
    if config["approved_engine_identity_sha256"] is not None:
        config["approved_engine_identity_sha256"] = assert_artifact_digest(
            config["approved_engine_identity_sha256"])
    # The protection restatement, carried only when configured. Absent is not
    # defaulted to False here: a run that needs it refuses for the missing
    # input, which is a different and more useful failure than one that
    # silently assumed the weaker lane.
    protection = env.get(ENGINE_PROTECTION_ENV)
    control_class = env.get(ENGINE_CONTROL_CLASS_ENV)
    if protection is not None or control_class is not None:
        config["native_branch_protection"] = protection
        config["control_class"] = control_class
        claim = assert_release_protection_claim(config)
        config.update(claim)
    config["provenance"] = provenance_of(config)
    return config


def assert_artifact_digest(value) -> str:
    """64 lower-case hex. Same discipline as a commit sha, different length."""
    if not isinstance(value, str) or len(value) != 64 or any(
            c not in "0123456789abcdef" for c in value):
        refuse(f"category=approved_engine_artifact_digest_malformed "
               f"length={len(value) if isinstance(value, str) else 'n/a'} — "
               "expected 64 lower-case hex characters")
    return value


#: Everything an operator must have named for a release to be APPROVED. The
#: set is deliberately larger than "digest and tag": those two identify bytes
#: and a name, and say nothing about the runtime lock, the SBOM, the build run
#: or the control class under which the operator accepted it. All of that
#: lives in the identity document, so the document's own digest has to be part
#: of what is approved — otherwise a strict, internally sealed document that
#: the operator never saw satisfies every check.
RELEASE_BINDING_FIELDS = ("approved_engine_source_sha",
                          "approved_engine_protected_sha",
                          "approved_engine_artifact_sha256",
                          "approved_engine_release_tag",
                          "approved_engine_identity_sha256",
                          "native_branch_protection",
                          "control_class")

RELEASE_BINDING_DIGEST_LABEL = b"midterm-engine-release-binding-v1"


def _binding_member_is_present(value) -> bool:
    """`False` is a present value. Absent is `None` or empty string.

    Spelled out because `native_branch_protection` is legitimately `False` on
    this repository, and a truthiness test would treat the honest answer as a
    missing field — the same trap as `enginebuild.PROVENANCE_FIELDS`."""
    return value is not None and value != ""


def release_binding(config: dict) -> dict:
    """The complete operator approval, as one canonical digest.

    Carrying seven separate fields through preflight, count, plan and panel
    invites a comparison that checks six of them. One digest over all seven
    cannot be partially compared."""
    values = {field: config.get(field) for field in RELEASE_BINDING_FIELDS}
    missing = sorted(f for f, v in values.items()
                     if not _binding_member_is_present(v))
    if missing:
        refuse(f"category=engine_release_binding_incomplete fields={missing} "
               "— a provider-backed run is authorised by the whole binding; "
               "approving a subset approves an engine the operator did not "
               "fully name")
    blob = json.dumps(values, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    return {**values, "engine_release_binding_sha256": hashlib.sha256(
        RELEASE_BINDING_DIGEST_LABEL + b"\x00" + blob).hexdigest()}


def provenance_of(config: dict) -> str:
    """Approved means the operator named the WHOLE release, not part of it.

    This used to return APPROVED_RELEASE for an artifact digest plus a release
    tag. Those identify bytes and a name; they do not identify the identity
    document that carries the runtime lock, SBOM, provenance digest, build run
    and control class. A run could therefore call itself approved while the
    operator had never bound the document those facts live in, and the
    honest label for that is the one that refuses to spend."""
    if all(_binding_member_is_present(config.get(field))
           for field in RELEASE_BINDING_FIELDS):
        return APPROVED_RELEASE
    return REBUILT_TEST_ONLY


def assert_identity_is_midterm_grade(record, *, release: dict) -> dict:
    """What this lane will accept from a published engine identity record.

    The mirror of `enginebridge.assert_identity_is_trusted_grade`, pointing
    the other way. This lane accepts the mid-term state — an exact
    default-branch SHA, no native protection, a human exact-head compensating
    control — because that is honestly what backs it, and it emits only
    `MIDTERM_SINGLE_REPO_*` evidence.

    It also refuses a PROTECTED record, which looks strange until you ask what
    accepting one would mean: this repository's `main` is unprotected, so a
    record arriving here claiming native protection is either from a different
    repository or is lying, and neither is something to run on. When `main`
    does get native protection the build will emit the protected state and
    this refusal is the thing that makes someone look at the change rather
    than inherit it silently.

    Exact operator approval is required on top: the state says what kind of
    build it was, the release configuration says which exact bytes an operator
    approved, and neither substitutes for the other."""
    from trustedlane.enginebuild import (
        CONTROL_HUMAN_EXACT_HEAD,
        MIDTERM_STATE,
        PROTECTED_STATE,
        assert_ref_protected,
    )

    from . import COUNT_EVIDENCE_CLASS, PANEL_EVIDENCE_CLASS

    if not isinstance(record, dict):
        refuse("category=midterm_engine_identity_not_an_object")
    state = record.get("state")
    if state == PROTECTED_STATE:
        refuse("category=midterm_engine_identity_claims_native_protection — "
               "this lane's repository has no native branch protection, so a "
               "record claiming it describes a build this lane did not make. "
               "If protection was genuinely enabled, the mid-term acceptance "
               "rule is what should be revisited, deliberately")
    if state != MIDTERM_STATE:
        refuse(f"category=midterm_engine_identity_unknown_state "
               f"state={state!r} expected={MIDTERM_STATE}")
    if assert_ref_protected(record.get("build_ref_protected")):
        refuse("category=midterm_engine_identity_protection_contradicts_state")
    if record.get("control_class") != CONTROL_HUMAN_EXACT_HEAD:
        refuse(f"category=midterm_engine_identity_wrong_control "
               f"control_class={record.get('control_class')!r} "
               f"expected={CONTROL_HUMAN_EXACT_HEAD}")
    if provenance_of(release) != APPROVED_RELEASE:
        refuse("category=midterm_engine_identity_without_exact_approval — the "
               "identity record is the right kind; the release configuration "
               "still has to name the exact artifact digest and release tag "
               "an operator approved")
    approved = release.get("approved_engine_artifact_sha256")
    if record.get("engine_artifact_sha256") != approved:
        refuse("category=midterm_engine_identity_is_for_a_different_artifact "
               "— the identity record names one artifact and the operator "
               "approved another")
    return {"state": MIDTERM_STATE, "native_branch_protection": False,
            "control_class": CONTROL_HUMAN_EXACT_HEAD,
            "emits_evidence_classes": [COUNT_EVIDENCE_CLASS,
                                       PANEL_EVIDENCE_CLASS],
            "honest_scope": (
                "native branch protection was not active; the exact head was "
                "selected by a human and the exact five digests were approved "
                "out of band. This backs MIDTERM_SINGLE_REPO_* evidence only "
                "and is refused by the trusted lane")}


def load_release_engine_identity(*, release: dict, mode: str,
                                 rebuilt_artifact_sha256: str) -> dict:
    """Load and enforce the builder's identity record for the exact release.

    This is the check the mid-term lane was missing entirely. `build_pinned_
    artifact` rebuilds the engine from the two pinned commits and compares the
    digest to the operator's approval, which proves the bytes are reproducible
    and says nothing at all about which build produced the approved ones, what
    ref it ran from, whether that ref was protected, or under which control the
    operator accepted it. Only the identity document carries those facts, and
    for as long as nothing read it, the corrected producer-side state was
    decoration.

    Provider mode requires the document. Dry runs may proceed without one —
    there is no approved release to have an identity for — but if a path IS
    configured it is validated, so a broken document fails the cheap run
    rather than first surfacing on the run that spends money."""
    from trustedlane import enginebuild

    assert_mode(mode)
    path = release.get("approved_engine_identity_path")
    if not path:
        if mode == MODE_PROVIDER:
            refuse(f"category=approved_engine_identity_not_configured "
                   f"variable='{ENGINE_IDENTITY_PATH_ENV}' — a provider-backed "
                   "run must read the identity record the build published for "
                   "the exact approved release. Rebuilding the artifact proves "
                   "reproducibility, which is not provenance and not approval")
        return {"engine_identity": None, "state": None,
                "honest_scope": ("no identity document was configured and this "
                                 "is a dry run; nothing about the builder, its "
                                 "ref or its control class has been checked")}

    if mode == MODE_PROVIDER and not release.get(
            "approved_engine_identity_sha256"):
        refuse(f"category=approved_engine_identity_digest_not_configured "
               f"variable='{ENGINE_IDENTITY_DIGEST_ENV}' — in provider mode "
               "the comparison is unconditional. A document that is strict, "
               "internally sealed and self-consistent is still not the "
               "document the operator approved, and 'compare it only if a "
               "digest happens to be configured' makes the approval optional")

    try:
        record = enginebuild.strict_load_built_identity(path)
    except Exception as exc:  # noqa: BLE001 — re-raised as a panel refusal
        refuse(f"category=approved_engine_identity_rejected "
               f"exception_class={type(exc).__name__} — the trusted-lane "
               "loader refused the release identity document")

    grade = assert_identity_is_midterm_grade(record, release=release)

    # The identity must describe THIS engine: the same two source commits the
    # operator pinned, and the same bytes both the operator approved and this
    # run just rebuilt. Any one of those alone is satisfiable by a document
    # from a different build.
    roles = record.get("source_roles") or {}
    expected_roles = source_roles(release)
    observed = {role: (roles.get(role) or {}).get("source_commit")
                if isinstance(roles.get(role), dict) else roles.get(role)
                for role in expected_roles}
    if observed != expected_roles:
        refuse(f"category=approved_engine_identity_source_roles_differ "
               f"expected={ {k: v[:12] for k, v in expected_roles.items()} } "
               f"observed={ {k: (v or '')[:12] for k, v in observed.items()} } "
               "— the identity record describes a build of different commits "
               "than the ones the operator pinned")
    if record["engine_artifact_sha256"] != rebuilt_artifact_sha256:
        refuse("category=approved_engine_identity_is_for_a_different_artifact "
               "— the identity record names one artifact and this run rebuilt "
               "another, so the record describes bytes that did not load")

    configured_digest = release.get("approved_engine_identity_sha256")
    with open(path, "rb") as handle:
        document_sha256 = hashlib.sha256(handle.read()).hexdigest()
    if configured_digest and configured_digest != document_sha256:
        refuse("category=approved_engine_identity_document_digest_mismatch — "
               "the operator approved one identity document and this run read "
               "another")
    # The whole approval, as one number, so downstream comparisons cannot
    # check six fields out of seven.
    binding = (release_binding(release) if mode == MODE_PROVIDER
               else {"engine_release_binding_sha256": None})

    provenance = record.get("provenance") or {}
    return {
        "engine_identity": record,
        "engine_identity_sha256": document_sha256,
        "engine_release_binding_sha256":
            binding["engine_release_binding_sha256"],
        "state": grade["state"],
        "native_branch_protection": grade["native_branch_protection"],
        "control_class": grade["control_class"],
        "emits_evidence_classes": grade["emits_evidence_classes"],
        "build_workflow_run_id": provenance.get("build_workflow_run_id"),
        "build_workflow_run_attempt":
            provenance.get("build_workflow_run_attempt"),
        "engine_provenance_sha256": record["provenance_sha256"],
        "honest_scope": grade["honest_scope"],
    }


def assert_release_protection_claim(config: dict) -> dict:
    """The governance file may not upgrade a build it did not perform.

    `native_branch_protection` and `control_class` in the release
    configuration are a RESTATEMENT of what the build recorded, for a human
    reading the repository without downloading an artifact. Restatements
    drift, so this refuses the combinations that would be a lie rather than a
    typo — in particular a configuration claiming native protection while
    naming the human compensating control, which is how a note added after
    the fact would try to launder an unprotected build."""
    from trustedlane.enginebuild import (
        CONTROL_HUMAN_EXACT_HEAD,
        CONTROL_NATIVE_PROTECTED_REF,
        assert_ref_protected,
    )

    if not isinstance(config, dict):
        refuse("category=engine_release_config_not_an_object")
    if "native_branch_protection" not in config:
        refuse("category=engine_release_config_omits_protection — the file "
               "must state whether native branch protection backed the build "
               "it approves; silence here is what let a false protected-ref "
               "claim stand")
    protected = assert_ref_protected(config.get("native_branch_protection"))
    control = config.get("control_class")
    if control not in (CONTROL_NATIVE_PROTECTED_REF, CONTROL_HUMAN_EXACT_HEAD):
        refuse(f"category=engine_release_config_control_class_unknown "
               f"control_class={control!r}")
    if (control == CONTROL_NATIVE_PROTECTED_REF) is not protected:
        refuse(f"category=engine_release_config_protection_claim_contradicts "
               f"native_branch_protection={protected} control_class="
               f"{control!r} — a configuration cannot describe an unprotected "
               "build as protected")
    return {"native_branch_protection": protected, "control_class": control}


def assert_engine_source_is_not_the_reviewed_candidate(
        *, release: dict, reviewed_candidate_head_sha: str) -> dict:
    """The reviewer may not be the reviewed. The central invariant, checked.

    Both halves are checked, not just the verifier half: an engine whose
    `trustedlane` source came from the head under review is just as much
    candidate code holding the credential."""
    from .status import assert_candidate_sha
    head = assert_candidate_sha(reviewed_candidate_head_sha,
                                field="reviewed_candidate_head_sha")
    for field in ("approved_engine_source_sha",
                  "approved_engine_protected_sha"):
        if release.get(field) == head:
            refuse(f"category=engine_source_is_the_reviewed_candidate "
                   f"field={field} — the engine would be built from the commit "
                   "under review and imported in a job holding the provider "
                   "key, so the candidate's own code would decide whether the "
                   "candidate passes. The engine is pinned by the operator and "
                   "lags the candidate by design; there is no exception")
    return {"reviewed_candidate_head_sha": head,
            "approved_engine_source_sha":
                release["approved_engine_source_sha"],
            "approved_engine_protected_sha":
                release["approved_engine_protected_sha"],
            "identities_are_distinct": True}


def source_roles(release: dict) -> dict:
    """The engine's identity as the two commits it is built from.

    Keyed by the upstream build-role names because `enginesource` requires
    exactly those keys; the values come only from the approved release."""
    return {
        ENGINE_SOURCE_ROLE: release["approved_engine_source_sha"],
        PROTECTED_ROLE: release["approved_engine_protected_sha"],
    }


def engine_digest(*, roles: dict) -> str:
    """A cheap, stable identity for the engine this review is bound to.

    The digest of the two source commits, NOT of the built tarball. Preflight
    needs an engine identity to put in the dedupe binding and it must not build
    a multi-megabyte artifact to get one; the count job computes and records the
    real `engine_artifact_sha256` when it actually opens the artifact.

    These are different digests with different meanings, and conflating them is
    the mistake `runtimebinding` exists to catch — so they have different names
    here and the evidence carries both.

    `roles` is a required keyword with no environment fallback. The previous
    signature defaulted it to a value read from `MIDTERM_ENGINE_*`, which meant
    a caller could get a digest for an engine it had not chosen and would never
    load."""
    from .evidence import digest_of
    if not isinstance(roles, dict) or set(roles) != {ENGINE_SOURCE_ROLE,
                                                     PROTECTED_ROLE}:
        refuse(f"category=engine_roles_malformed "
               f"observed={sorted(roles) if isinstance(roles, dict) else type(roles).__name__} "
               f"expected={sorted((ENGINE_SOURCE_ROLE, PROTECTED_ROLE))}")
    return digest_of({"engine_source_roles": roles})


def assert_provenance_permits(provenance: str, *, mode: str) -> str:
    """A locally rebuilt artifact may not back a run that spends money.

    Reproducible is not approved. Anyone with the two commits can rebuild this
    artifact and get the same digest — that is determinism, and determinism is
    what makes approval MEANINGFUL rather than what replaces it. Operator
    prerequisite 14 is the approval, and it is unrecorded."""
    assert_mode(mode)
    if provenance == APPROVED_RELEASE:
        return provenance
    if provenance != REBUILT_TEST_ONLY:
        refuse(f"category=engine_provenance_unknown provenance={provenance!r}")
    if mode == MODE_DRY_RUN:
        return provenance
    refuse(f"category=engine_provenance_is_test_only provenance={provenance} "
           f"mode={mode} — this artifact was rebuilt from pinned source, which "
           "is reproducible and approved by nobody. A dry run may use it; a "
           "panel that spends money may not. Approval of the five digests is "
           "operator prerequisite 14")


def assert_transport_suits_mode(transport, *, mode: str) -> dict:
    """A dry run may not hold a transport that can reach the provider.

    The gate is on the OBJECT, not on a flag beside it. A run configured as a
    dry run while carrying a live transport is one missing branch away from
    spending, and the missing branch is the thing under test."""
    from . import transport as transport_module
    assert_mode(mode)
    is_live = isinstance(transport, transport_module.LiveTransportBase)
    declared = getattr(transport, "source", None)
    if mode == MODE_DRY_RUN and (is_live or declared == "PROVIDER"):
        refuse(f"category=dry_run_holds_a_provider_transport "
               f"transport={type(transport).__name__} source={declared!r} — a "
               "dry run proves the lane works without spending; holding a "
               "transport that can spend makes that proof one branch deep")
    if mode == MODE_PROVIDER and not is_live:
        refuse(f"category=provider_mode_holds_a_stand_in_transport "
               f"transport={type(transport).__name__} — a provider-backed run "
               "whose transport is a stand-in produces evidence that reads as "
               "a real panel and is not one")
    return {"mode": mode, "transport_class": type(transport).__name__,
            "declared_source": declared}


def assert_not_the_github_zip_digest(*, artifact_path: str) -> str:
    """The artifact is a tarball this lane built, not a download.

    `git archive`, a release zipball and a `codeload` tarball all produce a file
    with a sha256, and an operator approving 'the digest from the release page'
    would approve bytes whose member layout, mtimes and content this lane never
    determined. Refusing by path is crude on purpose: it catches the mistake at
    the point where it is still obvious."""
    lowered = str(artifact_path).lower()
    hit = [marker for marker in GITHUB_ZIP_MARKERS if marker in lowered]
    if hit:
        refuse(f"category=engine_artifact_looks_like_a_source_download "
               f"markers={hit} path={artifact_path!r} — the engine artifact is "
               "the deterministic tarball built from exact git blobs by "
               "`trustedlane.enginesource`. A GitHub source archive has the "
               "same digest shape and different bytes, so approving one would "
               "approve an engine nobody built")
    return artifact_path


def build_pinned_artifact(*, destination: str, release: dict,
                          repository_numeric_id: int, cwd: str = ".") -> dict:
    """Build the artifact deterministically from the APPROVED commits.

    Thin adapter over `enginesource.build_engine_artifact` — the same function
    the protected build workflow calls, so the bytes are the bytes. The only
    thing added is the provenance label, which travels with the record.

    Named for what it builds from. The old name said `test_only`, which was true
    of the label and not of the callers: both real entry points called it."""
    from trustedlane import enginesource
    assert_not_the_github_zip_digest(artifact_path=destination)
    record = enginesource.build_engine_artifact(
        roles=source_roles(release), destination=destination,
        repository_numeric_id=repository_numeric_id, cwd=cwd)
    provenance = release.get("provenance") or provenance_of(release)
    approved = release.get("approved_engine_artifact_sha256")
    observed = record["engine_artifact_sha256"]
    if approved and observed and approved != observed:
        refuse(f"category=rebuilt_artifact_is_not_the_approved_one "
               f"approved={approved[:16]} observed={str(observed)[:16]} — the "
               "two pinned commits did not reproduce the bytes the operator "
               "approved, so either the pins or the approval names a different "
               "engine")
    return {**record, "provenance": provenance,
            "honest_scope": (
                "deterministic and reproducible; approved by nobody"
                if provenance == REBUILT_TEST_ONLY else
                "reproduced the artifact digest an operator approved")}


def open_engine(artifact_path: str, *, destination: str,
                expected_sha256: str) -> dict:
    """Extract and load the engine, returning its own callables.

    Loading goes through `enginebridge`, which imports the artifact under a
    namespace derived from its root so the artifact's modules can never be
    confused with an in-tree `verifier` package. That collision cost an entire
    exchange to diagnose; this bridge is the reason the mid-term lane does not
    get to repeat it."""
    from trustedlane import artifactload, enginebridge
    assert_not_the_github_zip_digest(artifact_path=artifact_path)
    # `extract` requires the destination to exist and be a directory; it
    # deliberately does not create one, because a function that creates its own
    # extraction root cannot be pointed at a prepared, empty, private one.
    os.makedirs(destination, exist_ok=True)
    artifactload.extract(artifact_path, destination=destination,
                         expected_sha256=expected_sha256)
    return enginebridge.load_engine(destination)


def load_engine_for_mode(*, mode: str, release: dict,
                         reviewed_candidate_head_sha: str,
                         artifact_path: str, extract_to: str,
                         repository_numeric_id: int,
                         candidate_paths=(), cwd: str = ".") -> dict:
    """The only supported way to obtain an engine. Every gate, in order.

    The order is the argument. Identity is checked before bytes are built,
    because building from the candidate head is already the harm; provenance is
    checked before the artifact is opened, because a run that may not spend
    should not get as far as a loaded engine that could; and the runtime
    provenance check happens last, after import, because it asks a question only
    the imported modules can answer."""
    assert_mode(mode)
    identities = assert_engine_source_is_not_the_reviewed_candidate(
        release=release, reviewed_candidate_head_sha=reviewed_candidate_head_sha)
    # FIRST, and only in provider mode. `assert_provenance_permits` below used
    # to be what kept this lane from spending, and it was doing that job by
    # accident: it refuses an unbound engine release, and the release happened
    # to be unbound. Binding it answered that question and would silently have
    # answered "may we spend yet?" too. This asks the second question on its
    # own, of a committed operator decision, before anything else happens.
    authorisation = None
    if mode == MODE_PROVIDER:
        from .policystate import assert_provider_backed_run_is_authorised
        authorisation = assert_provider_backed_run_is_authorised(root=cwd)
    provenance = assert_provenance_permits(
        release.get("provenance") or provenance_of(release), mode=mode)
    assert_no_candidate_path_is_importable(candidate_paths=candidate_paths)

    built = build_pinned_artifact(
        destination=artifact_path, release=release,
        repository_numeric_id=repository_numeric_id, cwd=cwd)
    # By exact key, with no fallback. `.get("artifact_sha256") or
    # .get("sha256")` — neither of which `build_engine_artifact` returns —
    # produced `None`, and `artifactload.extract` then refused with
    # "expected_engine_digest_malformed", which reads as an operator mistake
    # and was a caller mistake.
    artifact_sha256 = built["engine_artifact_sha256"]
    # Before the artifact is opened. The identity record decides whether this
    # lane may use this KIND of engine at all, and a run that imports first
    # and asks afterwards has already done the thing the answer governs.
    identity = load_release_engine_identity(
        release=release, mode=mode, rebuilt_artifact_sha256=artifact_sha256)
    engine = open_engine(artifact_path, destination=extract_to,
                         expected_sha256=artifact_sha256)
    assert_no_module_came_from_candidate_data(candidate_paths=candidate_paths)

    roles = source_roles(release)
    return {
        "engine": engine,
        "mode": mode,
        "provenance": provenance,
        "provider_run_authorisation": authorisation,
        "engine_identity_record": identity,
        "engine_identity_sha256": identity.get("engine_identity_sha256"),
        "engine_release_binding_sha256":
            identity.get("engine_release_binding_sha256"),
        "engine_identity_state": identity.get("state"),
        "native_branch_protection": identity.get("native_branch_protection"),
        "control_class": identity.get("control_class"),
        "engine_build_run_id": identity.get("build_workflow_run_id"),
        "engine_build_run_attempt":
            identity.get("build_workflow_run_attempt"),
        "engine_provenance_sha256": identity.get("engine_provenance_sha256"),
        "engine_artifact_sha256": artifact_sha256,
        "engine_source_digest": engine_digest(roles=roles),
        "engine_source_roles": roles,
        "artifact_record": built,
        **identities,
    }


# ------------------------------------------------- runtime provenance ------
#
# The static controls prove the workflow never checks the candidate out. These
# two prove it at runtime, from the interpreter's own bookkeeping, because the
# static argument is about a file and the thing that matters is a process.


def assert_no_candidate_path_is_importable(*, candidate_paths=()) -> dict:
    """No candidate directory may sit on the import path.

    Checked BEFORE the engine loads. `sys.path` decides what `import` finds, so
    a candidate directory on it means the next import of any name the candidate
    also defines resolves to candidate code — including imports the engine makes
    of its own dependencies."""
    resolved = _resolved_candidate_paths(candidate_paths)
    if not resolved:
        return {"candidate_paths": [], "sys_path_clean": True}
    entries = [os.path.realpath(entry) for entry in sys.path if entry]
    hits = sorted({entry for entry in entries
                   if any(_is_within(entry, root) for root in resolved)})
    if hits:
        refuse(f"category=candidate_path_is_importable entries={hits} — a "
               "candidate directory on sys.path makes the candidate code, and "
               "every argument in this lane rests on it being data")
    return {"candidate_paths": sorted(resolved), "sys_path_clean": True,
            "sys_path_entries_checked": len(entries)}


def assert_no_module_came_from_candidate_data(*, candidate_paths=()) -> dict:
    """No loaded module may have come from candidate-controlled bytes.

    Asked of `sys.modules` after the engine is imported, by `__file__`, because
    that is where the interpreter records where each module actually came from
    rather than where we believe it came from. Modules without a `__file__` —
    builtins, namespace packages — are counted and skipped; a module whose file
    lives under a candidate root is refused by name so the report says which."""
    resolved = _resolved_candidate_paths(candidate_paths)
    if not resolved:
        return {"candidate_paths": [], "modules_from_candidate": []}
    offenders = []
    fileless = 0
    checked = 0
    for name, module in list(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if not origin:
            fileless += 1
            continue
        checked += 1
        real = os.path.realpath(origin)
        if any(_is_within(real, root) for root in resolved):
            offenders.append(name)
    if offenders:
        refuse(f"category=module_loaded_from_candidate_data "
               f"modules={sorted(offenders)[:8]} count={len(offenders)} — the "
               "candidate's bytes are executing in this process. The static "
               "workflow controls prove the tree was never checked out; this "
               "is the same claim asked of the interpreter, and it disagrees")
    return {"candidate_paths": sorted(resolved), "modules_from_candidate": [],
            "modules_with_a_file_checked": checked,
            "modules_without_a_file": fileless}


def _resolved_candidate_paths(candidate_paths) -> list:
    if isinstance(candidate_paths, (str, bytes)):
        refuse("category=candidate_paths_not_a_sequence — a bare string would "
               "be iterated character by character and check nothing")
    return [os.path.realpath(path) for path in (candidate_paths or []) if path]


def _is_within(path: str, root: str) -> bool:
    """True when `path` is `root` or lives under it. Separator-anchored so that
    `/a/bcd` is not treated as living under `/a/bc`."""
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)
