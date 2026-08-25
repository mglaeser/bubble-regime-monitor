"""Canonical silence matching shared by planning and dispatch.

A silence is a delivery decision. It never changes condition truth or closes
an episode, and its semantics must not depend on whether it arrived before or
after a delivery was queued.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.alerts.enums import SilenceMatcherKind


@dataclass(frozen=True)
class ActiveSilences:
    """All silence matchers active at one instant."""

    rule_ids: frozenset[str] = frozenset()
    instance_fingerprints: frozenset[str] = frozenset()
    buckets: frozenset[str] = frozenset()
    all: bool = False

    @classmethod
    def from_matchers(
        cls,
        matchers: Iterable[tuple[str | SilenceMatcherKind, str]],
    ) -> ActiveSilences:
        grouped: dict[SilenceMatcherKind, set[str]] = {
            kind: set() for kind in SilenceMatcherKind
        }
        for raw_kind, value in matchers:
            kind = SilenceMatcherKind(raw_kind)
            canonical = value.lower() if kind == SilenceMatcherKind.INSTANCE_FINGERPRINT \
                else value
            grouped[kind].add(canonical)
        return cls(
            rule_ids=frozenset(grouped[SilenceMatcherKind.RULE_ID]),
            instance_fingerprints=frozenset(
                grouped[SilenceMatcherKind.INSTANCE_FINGERPRINT]
            ),
            buckets=frozenset(grouped[SilenceMatcherKind.BUCKET]),
            all=bool(grouped[SilenceMatcherKind.ALL]),
        )


def matches_silence(
    active: ActiveSilences,
    *,
    instance_fingerprint: str,
    rule_id: str,
    bucket: str | None,
) -> bool:
    """Return whether one delivery member is currently silenced."""

    return (
        active.all
        or instance_fingerprint.lower() in active.instance_fingerprints
        or rule_id in active.rule_ids
        or (bucket is not None and bucket in active.buckets)
    )
