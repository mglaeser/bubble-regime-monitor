"""The sixteen operator prerequisites, defaulting to NONE satisfied."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .errors import refuse


@dataclass(frozen=True)
class Prerequisite:
    key: str
    statement: str
    verifiable_by_code: bool


OPERATOR_PREREQUISITES = (
    Prerequisite("delete_failed_run", "workflow run 30214247762 is deleted",
                 False),
    Prerequisite("verify_run_404", "that run's API endpoint returns 404", True),
    Prerequisite("rotate_probe_key", "the old probe key is rotated or deleted",
                 False),
    Prerequisite("review_key_usage",
                 "usage for that specific key has been reviewed", False),
    Prerequisite("install_environment_key",
                 "the replacement key exists ONLY as an environment secret",
                 False),
    Prerequisite("no_repository_or_org_fallback",
                 "no repository-level or organization-level secret satisfies "
                 "the same name", False),
    Prerequisite("protect_trusted_environment",
                 "the trusted environment and default ref are protected",
                 False),
    Prerequisite("authorize_twelve_pins", "all twelve operator PINs are "
                 "authorized", False),
    Prerequisite("authorize_capability_policy",
                 "the capability policy source and version are authorized",
                 False),
    Prerequisite("approve_literal_authorizations",
                 "exact occurrence and category literal authorizations are "
                 "approved", False),
    Prerequisite("approve_count_spending",
                 "count spending and unknown-billing treatment are approved",
                 False),
    Prerequisite("approve_review_request_policy_v2",
                 "ReviewRequestPolicy v2 is approved", False),
    Prerequisite("approve_artifact_retention",
                 "private artifact retention is approved", False),
    Prerequisite("approve_engine_identity",
                 "the trusted engine identity and artifact are approved",
                 False),
    Prerequisite("approve_bootstrap_branch",
                 "the trusted-lane bootstrap branch or service is approved",
                 False),
    Prerequisite("approve_generation_separately",
                 "real model generation is approved as its own decision",
                 False),
)

_KEYS = frozenset(p.key for p in OPERATOR_PREREQUISITES)


def prerequisite_status(satisfied=None) -> dict:
    """What an operator TOLD this process. It verifies none of it."""
    granted = set(satisfied or ())
    unknown = sorted(granted - _KEYS)
    if unknown:
        refuse(f"category=unknown_prerequisite_key keys={unknown}")
    outstanding = [p.key for p in OPERATOR_PREREQUISITES
                   if p.key not in granted]
    record = {
        "prerequisite_count": len(OPERATOR_PREREQUISITES),
        "satisfied": sorted(granted),
        "outstanding": outstanding,
        "all_satisfied": not outstanding,
        "real_calls_authorized": False,
        "generation_authorized": False,
        "honest_scope": "this record states what an operator asserted; it "
                        "verifies none of it and cannot",
    }
    blob = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    record["prerequisite_status_sha256"] = hashlib.sha256(blob).hexdigest()
    return record


def assert_real_calls_authorized(status: dict) -> None:
    """Always refuses in D0, whatever the record says.

    The record is writable by whoever runs this. Treating it as authority
    would make the sixteen-item list a formality that any caller satisfies by
    passing a set literal."""
    refuse(
        f"category=real_calls_not_authorized_in_D0 outstanding="
        f"{len(status.get('outstanding', []))} — D0 holds no credential, so "
        "there is nothing for an authorization to unlock; D1 requires a "
        "protected deployment this branch is not")


def assert_generation_authorized(status: dict) -> None:
    """Generation is a SEPARATE decision from counting, and also refused."""
    refuse(
        "category=generation_not_authorized_in_D0 — real model generation is "
        "approved on its own, after trusted counting exists and its cost is "
        "known; it is never implied by count approval")
