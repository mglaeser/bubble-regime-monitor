"""Explicit dominance: which firing rule silences which other firing rule.

Only what the ruleset declares in `supersedes`. There is no implicit "higher
priority wins" — an unwritten rule is one nobody can audit, and the loader
already rejected cycles and unknown targets, so what arrives here is a DAG.

Dominance produces a suppression REASON on the loser, never a change to its
condition state. The losing condition is still firing; it just is not the thing
worth interrupting somebody about.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.alerts.enums import SuppressionReason
from app.alerts.rulespec import RuleSpec


@dataclass(frozen=True)
class DominanceOutcome:
    winners: frozenset[str]
    #: loser rule_id -> the winner that silenced it
    suppressed: dict[str, str] = field(default_factory=dict)
    #: rule_ids whose UNSENT deliveries the winner asked to cancel
    cancel_unsent: frozenset[str] = frozenset()

    def reason_for(self, rule_id: str) -> str | None:
        return SuppressionReason.SUPERSEDED if rule_id in self.suppressed else None


def resolve(firing: list[RuleSpec],
            all_rules: dict[str, RuleSpec] | None = None) -> DominanceOutcome:
    """Resolve dominance among the rules firing on one evaluation.

    Transitive over the DECLARED graph, not over the firing set: if A supersedes
    B and B supersedes C, then C is suppressed even when B is not firing.
    Walking only the firing rules would mean "B happened not to fire" silently
    changes what reaches the phone — pass `all_rules` so the closure sees every
    declared edge.
    """
    firing_ids = {rule.rule_id for rule in firing}
    by_id = dict(all_rules or {})
    by_id.update({rule.rule_id: rule for rule in firing})

    def reachable(start: str, seen: set[str]) -> set[str]:
        rule = by_id.get(start)
        targets = set(rule.supersedes) if rule else set()
        for target in list(targets):
            if target in seen:
                continue
            seen.add(target)
            targets |= reachable(target, seen)
        return targets

    suppressed: dict[str, str] = {}
    cancel: set[str] = set()
    for rule in firing:
        for target in reachable(rule.rule_id, {rule.rule_id}):
            if target in firing_ids and target != rule.rule_id:
                # First winner wins deterministically: iterate in rule order and
                # keep the first claim, so the outcome does not depend on dict
                # ordering.
                suppressed.setdefault(target, rule.rule_id)
                if rule.cancel_unsent_superseded:
                    cancel.add(target)

    winners = firing_ids - set(suppressed)
    return DominanceOutcome(
        winners=frozenset(winners),
        suppressed=suppressed,
        cancel_unsent=frozenset(cancel),
    )


def group_key_for(rule: RuleSpec) -> str:
    """The bundling key. Rules sharing a root cause travel together.

    Defaults to the bucket, so a rule that forgets to declare one still bundles
    with its neighbours rather than arriving alone.
    """
    return rule.group_key or rule.bucket
