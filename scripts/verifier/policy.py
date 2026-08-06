"""Repository-owned policy constants shared by the builder and the loader.

These live in their own module so the strict loader can RECOMPUTE what the
builder claimed without importing the builder (which imports the loader).
Nothing here is an operator PIN: the PIN names are declared, every value
stays None until the operator pins it.
"""

from __future__ import annotations

# The COMPLETE operator-PIN inventory (MC1-F12). Names only.
POLICY_PIN_NAMES: tuple[str, ...] = (
    "VERIFIER_MAX_OUTPUT_TOKENS",
    "VERIFIER_CONTEXT_MARGIN_TOKENS",
    "VERIFIER_COST_CAP_MICRO_USD",
    "VERIFIER_REASONING_EFFORT_BY_MODEL",
    "VERIFIER_MAX_REVIEW_UNITS",
    "VERIFIER_MAX_GENERATION_CALLS",
    "VERIFIER_MAX_COUNT_CALLS",
    "VERIFIER_COUNT_TIMEOUT_SECONDS",
    "VERIFIER_COUNT_MAX_RETRIES",
    "VERIFIER_GENERATION_TIMEOUT_SECONDS",
    "VERIFIER_GENERATION_MAX_RETRIES",
    "VERIFIER_TOKEN_DRIFT_TOLERANCE",
)

# Mirrors DEFAULT_PANEL_MODELS in scripts/independent_verify.py (asserted
# equal by test). Requesting a model proves nothing about capability or cost.
REQUESTED_MODEL_IDS: tuple[str, ...] = (
    "gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini")

# Per-unit CHANGED-BYTES heuristic (MC1-F11): structural, PROVISIONAL, NOT a
# token limit and NOT the legacy prompt cap. Cycle-3 exact provider counts
# replace it for fitting; it survives only as the structural pre-split.
STRUCTURAL_UNIT_CHANGED_BYTES_HEURISTIC = 50_000
