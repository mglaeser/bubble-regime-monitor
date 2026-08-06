"""Attempt-level count accounting, request caching, and the two-phase gate
(MC4 §14/§15/§17/§18/§39).

Three MC3 defects share this file because they share a cause — the finalizer
treated "a request" and "a provider attempt" as the same event:

  * the count cap counted LOGICAL requests while `count_input_tokens` could
    make 1 + VERIFIER_COUNT_MAX_RETRIES actual attempts, so a run could
    exceed the operator's cap by a factor of three and still report itself
    inside it;
  * VERIFIER_COUNT_TIMEOUT_SECONDS was validated and then never handed to a
    transport, so a hung call had no bound at all;
  * greedy batch probing re-counted byte-identical bodies, paying for the
    same question repeatedly.

The ledger below counts what is actually spent. The cap applies to ATTEMPTS,
and the budget for retries is reserved BEFORE the first attempt — a retry can
never be the thing that pushes a run past the operator's limit, because the
run refuses to start a request it cannot afford to retry.

The cache is keyed by (count_request_sha256, model_id). Identical bytes to
the same model is the same question; asking twice and getting two different
answers is PROVIDER_COUNT_INCONSISTENT, not something to average.
"""

from __future__ import annotations

from . import counting
from .canon import canonical_json, digest
from .errors import (
    CHUNK_COUNT_EXHAUSTED,
    TOKEN_COUNT_DRIFT,
    BlockingError,
)

PROVIDER_COUNT_INCONSISTENT = "PROVIDER_COUNT_INCONSISTENT"


class CountLedger:
    """Every provider attempt, counted before it is made.

    `logical_requests` is how many distinct questions were asked;
    `provider_attempts` is how many times a socket would actually have been
    used. The operator cap governs the second, which is the one that costs
    money and rate limit."""

    def __init__(self, transport, pin_values: dict):
        self.transport = transport
        self.pins = pin_values
        self.logical_requests = 0
        self.provider_attempts = 0
        self.cache_hits = 0
        self.records: list[dict] = []
        self.attempt_records: list[dict] = []
        self._cache: dict[tuple[str, str], int] = {}

    @property
    def max_attempts(self) -> int:
        return self.pins["VERIFIER_MAX_COUNT_CALLS"]

    @property
    def _attempts_per_request(self) -> int:
        return 1 + self.pins["VERIFIER_COUNT_MAX_RETRIES"]

    def _reserve(self, label: str) -> None:
        """Refuse a request whose worst case would breach the cap.

        Checking after the fact would let the final request's retries carry
        the run over the limit — the overspend would be detected only once it
        had already happened."""
        worst_case = self.provider_attempts + self._attempts_per_request
        if worst_case > self.max_attempts:
            raise BlockingError(
                CHUNK_COUNT_EXHAUSTED,
                f"category=count_attempt_cap_would_be_exceeded label={label} "
                f"attempts_so_far={self.provider_attempts} "
                f"worst_case={worst_case} cap={self.max_attempts} — the "
                "retry budget is reserved before the first attempt, so a "
                "retry can never push the run past the operator's cap")

    def _on_attempt(self, label: str, model_id: str):
        """Called BEFORE each transport call, so a failed attempt counts.

        MC4's first attempt added `result.attempts` only after a successful
        CountResult. A request that retried three times and then failed
        therefore reported zero attempts — the run had spent real calls and
        real rate limit, and the ledger said it had not."""
        def hook(attempt_number: int):
            if self.provider_attempts >= self.max_attempts:
                raise BlockingError(
                    CHUNK_COUNT_EXHAUSTED,
                    f"category=count_attempt_cap_reached label={label} "
                    f"attempts={self.provider_attempts} "
                    f"cap={self.max_attempts}")
            self.provider_attempts += 1
            self.attempt_records.append({
                "label": label,
                "model_id": model_id,
                "attempt_number": attempt_number,
                "result_category": "pending",
            })
        return hook

    def _settle(self, category: str, *, first_index: int):
        """Settle EVERY attempt made for this logical request.

        Marking only the last one would report two of three failed attempts
        as still pending, which understates exactly the number the ledger
        exists to state."""
        for record in self.attempt_records[first_index:]:
            if record["result_category"] == "pending":
                record["result_category"] = category

    def count(self, request, *, label: str) -> counting.CountResult:
        key = (request.count_request_sha256(), request.model_id)
        if key in self._cache:
            self.cache_hits += 1
            return counting.CountResult(input_tokens=self._cache[key],
                                        source=counting.transport_source(
                                            self.transport),
                                        attempts=0)
        self._reserve(label)
        self.logical_requests += 1
        first_index = len(self.attempt_records)
        hook = self._on_attempt(label, request.model_id)
        try:
            result = counting.count_input_tokens(
                request, transport=self.transport,
                max_retries=self.pins["VERIFIER_COUNT_MAX_RETRIES"],
                timeout_seconds=self.pins["VERIFIER_COUNT_TIMEOUT_SECONDS"],
                on_attempt=hook)
        except BlockingError as exc:
            # The attempts already happened. They stay in the ledger with a
            # sanitized category and no provider text.
            self._settle(f"failed:{exc.code}", first_index=first_index)
            raise
        # Earlier attempts of a request that eventually succeeded were still
        # retries: they are recorded as such, not folded into the success.
        self._settle("retried", first_index=first_index)
        self.attempt_records[-1]["result_category"] = "ok"
        self._cache[key] = result.input_tokens
        self.records.append({
            "label": label,
            "model_id": request.model_id,
            "input_tokens": result.input_tokens,
            "attempts": result.attempts,
            "result_category": "ok",
            "count_request_sha256": key[0],
            "request_semantics_sha256": request.request_semantics_sha256(),
        })
        return result

    def assert_consistent(self, request, observed: int) -> None:
        """Identical bytes must yield an identical count."""
        key = (request.count_request_sha256(), request.model_id)
        cached = self._cache.get(key)
        if cached is not None and cached != observed:
            raise BlockingError(
                PROVIDER_COUNT_INCONSISTENT,
                f"category=same_request_two_counts model={request.model_id} "
                f"first={cached} second={observed} — the same bytes cannot "
                "cost two different amounts; this is not drift to tolerate")

    def record(self) -> dict:
        record = {
            "logical_request_count": self.logical_requests,
            "provider_attempt_count": self.provider_attempts,
            "cache_hits": self.cache_hits,
            "unique_count_requests": len(self._cache),
            "max_attempts_cap": self.max_attempts,
            "attempts_per_request_worst_case": self._attempts_per_request,
            "timeout_seconds": self.pins["VERIFIER_COUNT_TIMEOUT_SECONDS"],
            "billing_state": counting.COUNT_BILLING_STATE,
            "mock_count_algorithm": (
                counting.MOCK_COUNT_ALGORITHM
                if counting.transport_source(self.transport)
                == counting.SOURCE_MOCK else None),
            "counts": self.records,
            "attempts": self.attempt_records,
            "failed_attempt_count": sum(
                1 for a in self.attempt_records
                if a["result_category"].startswith("failed")),
        }
        record["count_ledger_sha256"] = digest(b"count-ledger-v1",
                                               canonical_json(record))
        return record


def assert_usage_within_tolerance(*, counted: int, reported: int,
                                  tolerance: int, where: str) -> None:
    """The invariant VERIFIER_TOKEN_DRIFT_TOLERANCE was always meant for.

    MC3 applied this PIN to "a batch counted fewer tokens than its largest
    member unit" — a structural impossibility, not operator-tunable drift.
    The real question is whether the input count the finalizer paid for
    matches `usage.input_tokens` on the actual execution response."""
    if abs(reported - counted) > tolerance:
        raise BlockingError(
            TOKEN_COUNT_DRIFT,
            f"category=execution_usage_drift where={where} "
            f"counted={counted} reported={reported} tolerance={tolerance} — "
            "the plan was costed against a count the execution did not "
            "agree with")


def assert_batch_not_below_member_floor(*, measured: int, floor: int,
                                        model_id: str, label: str) -> None:
    """A deterministic sanity rule, with NO operator drift semantics.

    A batch body strictly contains each member unit's section, so counting
    fewer tokens than the largest member is impossible rather than merely
    surprising. There is nothing here for an operator to tune."""
    if measured < floor:
        raise BlockingError(
            TOKEN_COUNT_DRIFT,
            f"category=batch_count_below_member_floor model={model_id} "
            f"label={label} measured={measured} member_floor={floor} — a "
            "batch cannot cost less than the largest unit it contains; this "
            "is a structural impossibility, not tunable drift")
