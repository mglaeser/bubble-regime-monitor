"""Deterministic review batching (MC3 §37).

233 candidate units sent independently to three models would be 699
generation requests before a single retry. A ReviewBatch groups COMPLETE
units so one request can carry several — never splitting an atom, never
merging unit identities, and never letting a batch-level answer stand in for
a missing per-unit verdict (the response schema pins one verdict per unit
hash).

Determinism rules:
  * global unit order is preserved — packing never reorders for efficiency;
  * only units in the same review class (control-bearing + effective risk)
    share a batch, so one lens/policy applies to everything inside it;
  * a unit is appended greedily while EVERY required model still fits;
  * a unit that cannot share is closed into its own batch;
  * every unit lands in exactly one batch (proven, not assumed).
"""

from __future__ import annotations

from .canon import canonical_json, digest
from .errors import DUPLICATE_ATOM_DISPOSITION, BlockingError


def review_class(unit_record: dict) -> tuple:
    """Units may share a batch only when their review policy is identical."""
    cls = unit_record["classification"]
    return (bool(cls.get("control_bearing")), int(cls.get("effective_risk", 0)))


def batch_record(batch_id: str, unit_records: list[dict],
                 counts_by_model: dict, headroom_by_model: dict,
                 request_hashes_by_model: dict) -> dict:
    record = {
        "batch_id": batch_id,
        "review_class": list(review_class(unit_records[0])),
        "unit_sha256_in_order": [u["unit_sha256"] for u in unit_records],
        "per_unit_atom_ids": {u["unit_sha256"]: list(u["atom_ids"])
                              for u in unit_records},
        "unit_count": len(unit_records),
        "atom_count": sum(len(u["atom_ids"]) for u in unit_records),
        "changed_content_bytes": sum(u["changed_content_bytes"]
                                     for u in unit_records),
        "input_tokens_by_model": counts_by_model,
        "context_headroom_by_model": headroom_by_model,
        "request_hashes_by_model": request_hashes_by_model,
        "required_verdict_slots": [u["unit_sha256"] for u in unit_records],
        "planned_generation_calls": len(counts_by_model),
    }
    record["batch_sha256"] = digest(b"review-batch-v1", canonical_json(record))
    return record


def batch_digest(record: dict) -> str:
    stripped = {k: v for k, v in record.items() if k != "batch_sha256"}
    return digest(b"review-batch-v1", canonical_json(stripped))


def prove_batch_partition(unit_records: list[dict],
                          batches: list[dict]) -> None:
    """Every unit appears in exactly one batch, in global order."""
    expected = [u["unit_sha256"] for u in unit_records]
    seen: list[str] = []
    for batch in batches:
        seen.extend(batch["unit_sha256_in_order"])
    if len(seen) != len(set(seen)):
        raise BlockingError(DUPLICATE_ATOM_DISPOSITION,
                            "category=unit_in_more_than_one_batch")
    if seen != expected:
        missing = set(expected) - set(seen)
        extra = set(seen) - set(expected)
        raise BlockingError(
            DUPLICATE_ATOM_DISPOSITION,
            f"category=batch_partition_mismatch missing={len(missing)} "
            f"extra={len(extra)} reordered={sorted(seen) == sorted(expected)}")
