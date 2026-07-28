"""Provisional model-capability policy (MC3 §30).

These records are OFFICIAL provider facts transcribed into the repository.
That makes them PROVISIONAL_IN_REPOSITORY evidence: the reviewed repository
can modify them, which is exactly why V-TRUST stays open. Nothing here is
inferred — an unlisted setting is not supported, and an unavailable
configured model blocks rather than falling back.

Money is INTEGER micro-USD per million tokens; no float ever enters a cost
ledger. The long-context surcharge is expressed in basis points so the
multiplier is exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .canon import canonical_json, digest
from .errors import UNKNOWN_MODEL_CAPABILITY, BlockingError

STATUS = "PROVISIONAL_IN_REPOSITORY"
RETRIEVED = "2026-07-28"          # supplied by the operator in the mandate


@dataclass(frozen=True)
class ModelCapability:
    model_id: str
    context_window_tokens: int
    max_output_tokens_supported: int
    input_micro_usd_per_million: int
    cached_input_micro_usd_per_million: int
    output_micro_usd_per_million: int
    reasoning_efforts: tuple[str, ...]      # () means: omit the field entirely
    supports_reasoning: bool
    responses_api: bool
    structured_outputs: bool
    source_url: str
    long_context_threshold_input_tokens: int | None = None
    above_threshold_input_multiplier_bp: int | None = None
    above_threshold_output_multiplier_bp: int | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_record(self) -> dict:
        return {
            "model_id": self.model_id,
            "context_window_tokens": self.context_window_tokens,
            "max_output_tokens_supported": self.max_output_tokens_supported,
            "input_micro_usd_per_million": self.input_micro_usd_per_million,
            "cached_input_micro_usd_per_million":
                self.cached_input_micro_usd_per_million,
            "output_micro_usd_per_million": self.output_micro_usd_per_million,
            "reasoning_efforts": list(self.reasoning_efforts),
            "supports_reasoning": self.supports_reasoning,
            "responses_api": self.responses_api,
            "structured_outputs": self.structured_outputs,
            "source_url": self.source_url,
            "long_context_threshold_input_tokens":
                self.long_context_threshold_input_tokens,
            "above_threshold_input_multiplier_bp":
                self.above_threshold_input_multiplier_bp,
            "above_threshold_output_multiplier_bp":
                self.above_threshold_output_multiplier_bp,
            "notes": list(self.notes),
        }


CAPABILITIES: dict[str, ModelCapability] = {
    "gpt-5.3-codex": ModelCapability(
        model_id="gpt-5.3-codex",
        context_window_tokens=400_000,
        max_output_tokens_supported=128_000,
        input_micro_usd_per_million=1_750_000,
        cached_input_micro_usd_per_million=175_000,
        output_micro_usd_per_million=14_000_000,
        reasoning_efforts=("low", "medium", "high", "xhigh"),
        supports_reasoning=True,
        responses_api=True,
        structured_outputs=True,
        source_url="https://developers.openai.com/api/docs/models/gpt-5.3-codex",
    ),
    "gpt-5.6-sol": ModelCapability(
        model_id="gpt-5.6-sol",
        context_window_tokens=1_050_000,
        max_output_tokens_supported=128_000,
        input_micro_usd_per_million=5_000_000,
        cached_input_micro_usd_per_million=500_000,
        output_micro_usd_per_million=30_000_000,
        reasoning_efforts=("none", "low", "medium", "high", "xhigh", "max"),
        supports_reasoning=True,
        responses_api=True,
        structured_outputs=True,
        source_url="https://developers.openai.com/api/docs/models/gpt-5.6-sol",
        long_context_threshold_input_tokens=272_000,
        above_threshold_input_multiplier_bp=20_000,     # 2.0x
        above_threshold_output_multiplier_bp=15_000,    # 1.5x
    ),
    "gpt-4.1-mini": ModelCapability(
        model_id="gpt-4.1-mini",
        context_window_tokens=1_047_576,
        max_output_tokens_supported=32_768,
        input_micro_usd_per_million=400_000,
        cached_input_micro_usd_per_million=100_000,
        output_micro_usd_per_million=1_600_000,
        reasoning_efforts=(),
        supports_reasoning=False,
        responses_api=True,
        structured_outputs=True,
        source_url="https://developers.openai.com/api/docs/models/gpt-4.1-mini",
        notes=("no reasoning step; the reasoning field is omitted entirely",),
    ),
}


def capability(model_id: str) -> ModelCapability:
    """The record for one model. An unknown model BLOCKS — there is no
    generic fallback and no alias substitution."""
    record = CAPABILITIES.get(model_id)
    if record is None:
        raise BlockingError(
            UNKNOWN_MODEL_CAPABILITY,
            f"category=unknown_model_capability model_id_len={len(model_id)} "
            "— no generic fallback model exists")
    return record


def policy_record(model_ids: list[str]) -> dict:
    """The capability policy bound into a finalized plan."""
    record = {
        "status": STATUS,
        "retrieved": RETRIEVED,
        "models": [capability(m).to_record() for m in model_ids],
        "trust_note": "official provider facts transcribed INTO the reviewed "
                      "repository; the repository can modify them, so this is "
                      "provisional evidence and V-TRUST remains open",
    }
    record["capability_policy_sha256"] = digest(
        b"capability-policy-v1", canonical_json(record))
    return record


def policy_digest(record: dict) -> str:
    stripped = {k: v for k, v in record.items()
                if k != "capability_policy_sha256"}
    return digest(b"capability-policy-v1", canonical_json(stripped))
