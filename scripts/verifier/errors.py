"""Machine-readable blocking categories.

Distinct codes exist so a blocked review says WHICH invariant failed. The old
message — "raise VERIFIER_MAX_CHUNKS or split the pull request" — was actively
misleading for a single oversized file, because neither action can help when
one file exceeds the budget on its own: the packer's unit was the whole file.
That advice is retired; SINGLE_FILE_OVERSIZE_LEGACY exists only to name the
defect this package removes.
"""

from __future__ import annotations


class BlockingError(RuntimeError):
    """A fail-closed stop. `code` is the machine-readable category."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --- planning / diff integrity ---
DIFF_PARSE_FAILURE = "DIFF_PARSE_FAILURE"
MERGE_BASE_FAILURE = "MERGE_BASE_FAILURE"
PATH_IDENTITY_MISMATCH = "PATH_IDENTITY_MISMATCH"
PRIVACY_EXCLUDED_CONTROL = "PRIVACY_EXCLUDED_CONTROL"
SINGLE_FILE_OVERSIZE_LEGACY = "SINGLE_FILE_OVERSIZE_LEGACY"

UNRECOGNIZED_HUNKLESS_CHANGE = "UNRECOGNIZED_HUNKLESS_CHANGE"

# --- repository state (B2) ---
REPOSITORY_STATE_INVALID = "REPOSITORY_STATE_INVALID"
WORKTREE_NOT_CLEAN = "WORKTREE_NOT_CLEAN"
CODEOWNERS_UNREADABLE = "CODEOWNERS_UNREADABLE"

# --- hermetic git execution (MC1-F02/F04) ---
GIT_EXECUTION_UNSAFE = "GIT_EXECUTION_UNSAFE"
ATTRIBUTE_POLICY_UNSAFE = "ATTRIBUTE_POLICY_UNSAFE"
GIT_COMMAND_FAILED = "GIT_COMMAND_FAILED"
FILE_DIFF_ATTRIBUTION_FAILED = "FILE_DIFF_ATTRIBUTION_FAILED"
OBJECT_FORMAT_UNSUPPORTED = "OBJECT_FORMAT_UNSUPPORTED"

# --- splitter integrity (MC1-F17) ---
SPLITTER_PARTITION_FAILED = "SPLITTER_PARTITION_FAILED"

# --- coverage ---
INCOMPLETE_RANGE_COVERAGE = "INCOMPLETE_RANGE_COVERAGE"
DUPLICATE_RANGE_COVERAGE = "DUPLICATE_RANGE_COVERAGE"
OVERLAPPING_RANGE_COVERAGE = "OVERLAPPING_RANGE_COVERAGE"
UNKNOWN_ATOM_REFERENCE = "UNKNOWN_ATOM_REFERENCE"
PATCH_ROOT_MISMATCH = "PATCH_ROOT_MISMATCH"

# --- atom dispositions (MC1-F07) ---
MISSING_ATOM_DISPOSITION = "MISSING_ATOM_DISPOSITION"
DUPLICATE_ATOM_DISPOSITION = "DUPLICATE_ATOM_DISPOSITION"
INVALID_GENERATED_PROOF_REFERENCE = "INVALID_GENERATED_PROOF_REFERENCE"

# --- artifact integrity (MC1-F06) ---
ARTIFACT_SCHEMA_INVALID = "ARTIFACT_SCHEMA_INVALID"
ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"

# --- generated derivatives ---
GENERATED_DERIVATIVE_MISMATCH = "GENERATED_DERIVATIVE_MISMATCH"
OVERSIZED_SINGLE_ATOM = "OVERSIZED_SINGLE_ATOM"

# --- policy ---
UNSET_POLICY_PIN = "UNSET_POLICY_PIN"
COST_CAP_EXCEEDED = "COST_CAP_EXCEEDED"
CHUNK_COUNT_EXHAUSTED = "CHUNK_COUNT_EXHAUSTED"

# --- model capability / resolution ---
UNKNOWN_MODEL_CAPABILITY = "UNKNOWN_MODEL_CAPABILITY"
MODEL_RESOLUTION_MISMATCH = "MODEL_RESOLUTION_MISMATCH"
MODEL_CONTEXT_EXCEEDED = "MODEL_CONTEXT_EXCEEDED"
MODEL_CONTEXT_EXCEEDED_UNSPLITTABLE = "MODEL_CONTEXT_EXCEEDED_UNSPLITTABLE"

# --- token counting ---
# ruff S105 fires on these five because the NAMES contain "TOKEN"/"SECRET".
# They are blocking-category identifiers whose value is their own name; no
# credential is involved. Suppressed per-line rather than by relaxing S105 for
# the module, so a genuine hardcoded secret added here would still be caught.
TOKEN_COUNT_ENDPOINT_UNAVAILABLE = "TOKEN_COUNT_ENDPOINT_UNAVAILABLE"  # noqa: S105 -- error code, not a credential; pragma: allowlist secret
TOKEN_COUNT_RESPONSE_INVALID = "TOKEN_COUNT_RESPONSE_INVALID"  # noqa: S105 -- error code, not a credential; pragma: allowlist secret
TOKEN_COUNT_RETRY_EXHAUSTED = "TOKEN_COUNT_RETRY_EXHAUSTED"  # noqa: S105 -- error code, not a credential; pragma: allowlist secret
TOKEN_COUNT_DRIFT = "TOKEN_COUNT_DRIFT"  # noqa: S105 -- error code, not a credential; pragma: allowlist secret

# --- execution ---
SECRET_PREFLIGHT_FAILED = "SECRET_PREFLIGHT_FAILED"  # noqa: S105 -- error code, not a credential  # pragma: allowlist secret
STALE_REVIEW_PLAN = "STALE_REVIEW_PLAN"
EXECUTABLE_PLAN_INVALID = "EXECUTABLE_PLAN_INVALID"
STRUCTURAL_PLAN_BLOCKED = "STRUCTURAL_PLAN_BLOCKED"
PROVIDER_RESPONSE_INVALID = "PROVIDER_RESPONSE_INVALID"
REQUIRED_APPROVER_MISSING = "REQUIRED_APPROVER_MISSING"
REQUIRED_APPROVER_REFUTED = "REQUIRED_APPROVER_REFUTED"
INSUFFICIENT_CORROBORATION = "INSUFFICIENT_CORROBORATION"
UNIT_REFUTED = "UNIT_REFUTED"
SYNTHESIS_REFUTED = "SYNTHESIS_REFUTED"
