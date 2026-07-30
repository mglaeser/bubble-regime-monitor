"""The external code-cutoff / final-head closure record.

Every local report is produced against a code cutoff, and the final candidate
head adds evidence and workflow files after it. Something has to bind "this
evidence, produced by this engine, describes this repository at this head, and
the delta since the cutoff is evidence-only" — and it cannot be a digest
computed on the candidate branch, because the branch is what is being
described.

D0 defines the record and produces it EMPTY. Populating it requires a protected
run and a signature, so the template is the honest artifact."""

from __future__ import annotations

import hashlib
import json

from .errors import refuse

CLOSURE_FIELDS = (
    "repository_numeric_id",
    "repository_identity",
    "target_base_sha",
    "final_candidate_head_sha",
    "verifier_code_cutoff_sha",
    "evidence_only_delta_sha256",
    "trusted_engine_digest",
    "private_artifact_sha256_in_order",
    "public_summary_sha256",
    "workflow_run_id",
    "workflow_run_attempt",
    "protected_ref",
    "signature",
    "signer_identity",
    "produced_at",
)

OPEN = "OPEN_PENDING_PROTECTED_RUN"


def closure_template() -> dict:
    record = {field: None for field in CLOSURE_FIELDS}
    record["closure_state"] = OPEN
    record["honest_scope"] = (
        "a template, not a closure; a digest computed on the candidate branch "
        "cannot attest the candidate branch")
    blob = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    record["closure_template_sha256"] = hashlib.sha256(blob).hexdigest()
    return record


def assert_closure_complete(record: dict) -> None:
    """Refuse an incomplete closure, and refuse to sign one from D0."""
    missing = [f for f in CLOSURE_FIELDS if record.get(f) in (None, "")]
    if missing:
        refuse(f"category=closure_incomplete missing={missing}")
    refuse("category=closure_cannot_be_signed_in_D0 — a closure is signed by "
           "a protected run with a key this branch does not hold")


def evidence_only_delta(paths) -> dict:
    """Classify the delta between the code cutoff and the final head.

    A closure may only claim "evidence-only" when every changed path is an
    evidence or workflow path. A source change after the cutoff means the
    evidence describes code that is no longer what shipped.

    `paths` is materialised once, first. It used to be walked three times, and
    a generator is exhausted by the first walk — so a caller passing one got a
    correct `source_changed_paths` alongside a `changed_path_count` of 0 and a
    `delta_sha256` that was the digest of the EMPTY list. The closure binding
    would have been a digest of nothing while the record looked populated.
    Found by the external review panel on the bootstrap PR."""
    materialised = list(paths)
    if any(not isinstance(p, str) for p in materialised):
        refuse("category=closure_delta_path_not_a_string")
    # `.github/workflows/` is deliberately NOT here. A workflow is executable
    # code with credential reach — counting one as "evidence" would let a
    # credential-bearing lane be added after the code cutoff while the closure
    # still claimed the delta changed nothing that runs. Found by the bootstrap
    # PR review.
    evidence_prefixes = ("audit/", "artifacts/", "governance/", "docs/")
    source = sorted(p for p in materialised
                    if not any(p.startswith(prefix)
                               for prefix in evidence_prefixes))
    blob = json.dumps(sorted(materialised), separators=(",", ":")).encode()
    return {
        "changed_path_count": len(materialised),
        "source_changed_paths": source,
        "evidence_only": not source,
        "delta_sha256": hashlib.sha256(blob).hexdigest(),
    }
