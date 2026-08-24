"""The one path by which an artifact becomes PROMOTED.

`register(promote=True)` wrote `promoted_at` with no reference to the replay
evidence, and three production callers used it: the CLI, the admin route and
replay. That timestamp is what `delivery_admission_blockers` trusts when it
decides a queued delivery was authorised, which opened a laundering path:

  1. ruleset C fails its gate, and is marked promoted anyway;
  2. valid ruleset B later supersedes C;
  3. B clears deployment-level admission;
  4. a queued C delivery passes per-delivery admission, because C carries a
     historical `promoted_at` and is merely SUPERSEDED rather than unpromoted.

Nothing in that sequence involves a defect in C being noticed. Promotion has to
mean the evidence was checked, not that somebody called a function with a
keyword argument — so the state mutation lives here, behind the check, and
`register` no longer performs it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts.artifacts import LoadedArtifacts, register
from app.alerts.enums import RulesetStatus
from app.alerts.models import AlertRulesetRegistry
from app.alerts.promotion import EVIDENCE_PATH, load_evidence, promotion_blockers
from app.logging_conf import get_logger
from app.redaction import sanitize

log = get_logger(__name__)


@dataclass(frozen=True)
class PromotionDecision:
    """What was decided, and why. Returned whether or not it promoted."""

    rules_sha256: str
    phrase_set_sha256: str
    target_stage: int
    promoted: bool
    blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "rules_sha256": self.rules_sha256,
            "phrase_set_sha256": self.phrase_set_sha256,
            "target_stage": self.target_stage,
            "promoted": self.promoted,
            "blockers": list(self.blockers),
        }


def validate_register_and_promote(
    session: Session,
    artifacts: LoadedArtifacts,
    *,
    actor: str,
    evidence_path: str | Path | None = None,
    now: datetime | None = None,
) -> PromotionDecision:
    """Register the exact bytes, then promote them ONLY if the evidence allows.

    On refusal nothing about promotion changes: not `promoted_at`, not
    `promoted_by`, not the currently promoted row, not any supersession field.
    A refused promotion must leave the deployment exactly as it was, or the
    refusal itself becomes a way to disturb production.
    """
    now = now or datetime.now(UTC)
    ruleset = artifacts.ruleset

    # Registered as VALIDATED regardless: knowing the bytes exist is useful and
    # carries no authority. Promotion is the part that does.
    register(session, artifacts, now=now, registered_by=actor)

    stage = ruleset.document.meta.active_stage
    evidence = load_evidence(evidence_path)
    blockers: tuple[str, ...]
    if evidence is None:
        blockers = (f"the gate evidence at {evidence_path or EVIDENCE_PATH} is "
                    "missing or unreadable, so nothing can authorise this "
                    "promotion",)
    else:
        blockers = tuple(promotion_blockers(
            target_stage=stage,
            artifact=evidence,
            rule_version=ruleset.document.meta.rule_version,
            phrase_set_version=getattr(ruleset, "phrase_set_version", None),
            rules_sha256=ruleset.rules_sha256,
            phrase_set_sha256=ruleset.phrase_set_sha256,
        ))

    decision = PromotionDecision(
        rules_sha256=ruleset.rules_sha256,
        phrase_set_sha256=ruleset.phrase_set_sha256,
        target_stage=stage,
        promoted=not blockers,
        blockers=blockers,
    )

    if blockers:
        log.warning("alert_promotion_refused", rules_sha256=ruleset.rules_sha256[:12],
                    stage=stage, blockers=list(blockers))
        return decision

    _mark_promoted(session, ruleset.rules_sha256, actor=actor, now=now)
    log.info("alert_ruleset_promoted", rules_sha256=ruleset.rules_sha256[:12],
             rule_version=ruleset.rule_version, stage=stage, actor=actor)
    return decision


def seed_replay_artifacts(session: Session, artifacts: LoadedArtifacts, *,
                          now: datetime | None = None) -> None:
    """Make artifacts available to an ISOLATED replay engine.

    Replay used `promote=True`, which wrote operator-promotion metadata into a
    throwaway database — harmless there, and a bad shape: it made "promoted"
    reachable without an operator, and the same call was what production used.
    Replay needs the bytes readable, nothing more.
    """
    register(session, artifacts, now=now, registered_by="replay")


def _mark_promoted(session: Session, rules_sha256: str, *, actor: str,
                   now: datetime) -> None:
    """The raw state change. PRIVATE, and reached only past the evidence check."""
    for other in session.execute(
        select(AlertRulesetRegistry).where(
            AlertRulesetRegistry.status == RulesetStatus.PROMOTED,
            AlertRulesetRegistry.rules_sha256 != rules_sha256,
        )
    ).scalars().all():
        other.status = RulesetStatus.SUPERSEDED
        other.superseded_at = now

    row = session.get(AlertRulesetRegistry, rules_sha256)
    if row is None:                                # pragma: no cover - registered above
        raise LookupError(f"ruleset {rules_sha256[:12]} is not registered")
    row.status = RulesetStatus.PROMOTED
    row.promoted_at = now
    row.promoted_by = sanitize(actor)
    row.evidence_checked_at = now
    # A re-promoted row is not superseded any more. Leaving the old stamp made
    # the row say two things at once, and anything reading `superseded_at` as
    # "no longer current" would treat the CURRENT promotion as retired.
    row.superseded_at = None


def evidence_verdict(evidence_path: str | Path | None = None) -> dict[str, Any]:
    """The committed verdict, for surfaces that report rather than decide."""
    evidence = load_evidence(evidence_path)
    return evidence if evidence is not None else {}


__all__ = [
    "PromotionDecision",
    "evidence_verdict",
    "seed_replay_artifacts",
    "validate_register_and_promote",
]
