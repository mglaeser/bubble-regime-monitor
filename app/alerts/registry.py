"""Ruleset loading, validation, hashing and promotion.

A ruleset is an IMMUTABLE artifact addressed by the SHA-256 of its canonical
bytes. Every evaluation, every episode and every delivery names the exact hash
it ran under, which is what makes "this alert fired under those rules" a fact
rather than a reconstruction.

Validation is fail-closed and total: the loader rejects a ruleset outright
rather than dropping the offending rule, because a silently-dropped rule is a
mechanism that looks configured and never fires.

Promotion is explicit. A last-known-good fallback NEVER escalates the mode: if
the candidate is invalid the service keeps evaluating the promoted ruleset and
reports `ops.rules_invalid`; if there is no valid ruleset at all it reports
`ops.alerting_unavailable` and evaluates nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from app.alerts.canonical import canonical_json, sha256_hex, sha256_of
from app.alerts.errors import RulesetInvalid
from app.alerts.rulespec import (
    AUTHORITATIVE_SAFE_KINDS,
    RulesetDocument,
    RuleSpec,
    canonical_document,
    referenced_sources,
    walk,
)
from app.alerts.sources import (
    AUTHORITATIVE_SOURCES,
    FORBIDDEN_SOURCE_NAMES,
    KNOWN_SOURCES,
    SOURCE_REGISTRY,
)

VALIDATOR_VERSION = "1"


@dataclass(frozen=True)
class ValidatedRuleset:
    """A ruleset that passed every check, with its content identity."""

    document: RulesetDocument
    rules_sha256: str
    canonical_yaml: str
    rule_version: str
    phrase_set_version: str
    phrase_set_sha256: str
    warnings: tuple[str, ...] = ()
    _by_id: dict[str, RuleSpec] = field(default_factory=dict, compare=False)

    def rule(self, rule_id: str) -> RuleSpec | None:
        return self._by_id.get(rule_id)

    def rules(self) -> list[RuleSpec]:
        return self.document.all_rules()

    def active_rules(self, stage: int) -> list[RuleSpec]:
        """Rules that may actually run at a rollout stage.

        A rule enabled in the file but not listed for this stage is NOT active:
        `enabled_in_stages` is the rollout gate, and a stage may never be
        skipped by editing one flag.
        """
        return [r for r in self.rules() if r.enabled and stage in r.enabled_in_stages]


# ---------------------------------------------------------------------------
# instance identity
# ---------------------------------------------------------------------------


def instance_fingerprint(rule_id: str, identity_version: int, labels: dict[str, str]) -> str:
    """Stable identity of one rule INSTANCE across episodes AND rulesets.

    An episode id must never appear here: the fingerprint is what links
    yesterday's cooldown to today's firing, so it has to outlive both the
    episode and the ruleset that opened it.
    """
    return sha256_of(
        {
            "identity_version": identity_version,
            "rule_id": rule_id,
            "labels": {str(k): str(v) for k, v in sorted(labels.items())},
        }
    )


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _check_sources(rule: RuleSpec, problems: list[str]) -> None:
    for name in rule.source_fields:
        if name in FORBIDDEN_SOURCE_NAMES:
            problems.append(
                f"{rule.rule_id}: source {name!r} is forbidden — it is ambiguous between the "
                "Monte Carlo median and the deterministic point score"
            )
        elif name not in KNOWN_SOURCES:
            problems.append(f"{rule.rule_id}: unknown source {name!r}")

    declares_authoritative = any(
        name in AUTHORITATIVE_SOURCES for name in rule.source_fields if name in KNOWN_SOURCES
    )
    if rule.authoritative and not declares_authoritative:
        problems.append(
            f"{rule.rule_id}: declared authoritative but reads no authoritative source"
        )
    if declares_authoritative and not rule.authoritative:
        problems.append(
            f"{rule.rule_id}: reads a persisted decision but is not marked authoritative"
        )


def _check_condition_shapes(rule: RuleSpec, problems: list[str]) -> None:
    for node in walk(rule.condition):
        source_id = getattr(node, "source", None)
        if source_id is None or source_id not in SOURCE_REGISTRY:
            continue
        spec = SOURCE_REGISTRY[source_id]
        if spec.authoritative and node.kind not in AUTHORITATIVE_SAFE_KINDS:
            problems.append(
                f"{rule.rule_id}: condition kind {node.kind!r} on authoritative source "
                f"{source_id!r} would restate a frozen decision rule in the alert layer"
            )
        if getattr(node, "off_threshold", None) is not None and spec.authoritative:
            problems.append(
                f"{rule.rule_id}: hysteresis on authoritative source {source_id!r} — an "
                "authoritative band has exactly one edge"
            )
        allowed = spec.allowed_values
        if allowed:
            named = set()
            named.update(getattr(node, "from_states", []) or [])
            named.update(getattr(node, "to_states", []) or [])
            named.update(getattr(node, "values", []) or [])
            unknown = sorted(named - set(allowed))
            if unknown:
                problems.append(
                    f"{rule.rule_id}: source {source_id!r} cannot take states {unknown}; "
                    f"allowed: {sorted(allowed)}"
                )


def _check_confirmation_semantics(rule: RuleSpec, problems: list[str]) -> None:
    if rule.confirmation.basis == "distinct_source_revision":
        # Opt-in only: revision-sensitivity means a vendor restatement of the
        # SAME economic period advances confirmation, which is normally a bug.
        if "revision_sensitive" not in (rule.note or ""):
            problems.append(
                f"{rule.rule_id}: basis 'distinct_source_revision' requires an explicit "
                "'revision_sensitive' justification in `note` — a vendor revision of the same "
                "period must not confirm by accident"
            )
    for name in rule.hold_sources:
        if rule.freshness_requirements.get(name) in (None, ""):
            problems.append(f"{rule.rule_id}: hold source {name!r} has an empty freshness rule")


def _check_dominance(rules: list[RuleSpec], problems: list[str]) -> None:
    known = {rule.rule_id for rule in rules}
    graph: dict[str, list[str]] = {}
    for rule in rules:
        for target in rule.supersedes:
            if target not in known:
                problems.append(f"{rule.rule_id}: supersedes unknown rule {target!r}")
            if target == rule.rule_id:
                problems.append(f"{rule.rule_id}: supersedes itself")
        graph[rule.rule_id] = [t for t in rule.supersedes if t in known]

    # Explicit cycle detection: a dominance cycle means two rules each silence
    # the other and neither is ever delivered.
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(graph, WHITE)

    def visit(node: str, trail: list[str]) -> None:
        colour[node] = GREY
        for nxt in graph.get(node, ()):
            if colour.get(nxt) == GREY:
                cycle = " -> ".join([*trail, node, nxt])
                problems.append(f"dominance cycle: {cycle}")
            elif colour.get(nxt) == WHITE:
                visit(nxt, [*trail, node])
        colour[node] = BLACK

    for node in graph:
        if colour[node] == WHITE:
            visit(node, [])


def _check_stage_gating(document: RulesetDocument, rules: list[RuleSpec],
                        problems: list[str]) -> None:
    stage = document.meta.active_stage
    for rule in rules:
        if rule.enabled and not rule.enabled_in_stages:
            problems.append(f"{rule.rule_id}: enabled with an empty enabled_in_stages")
        if rule.enabled and stage in rule.enabled_in_stages and rule.priority in (1, 2):
            # An SMS-capable rule that is live must also be renderable.
            if not rule.phrase_set:
                problems.append(f"{rule.rule_id}: SMS-capable but names no phrase set")


def validate_ruleset(
    raw_yaml: str,
    *,
    phrase_set_version: str,
    phrase_set_sha256: str,
    methodology_version: str,
    methodology_manifest_sha256: str,
    service_version: str,
    available_calibrations: frozenset[str] = frozenset(),
) -> ValidatedRuleset:
    """Parse, validate and hash a candidate ruleset. Raises `RulesetInvalid`.

    Every problem is collected and reported together — fixing a ruleset one
    error per run is how a stage slips.
    """
    try:
        parsed = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise RulesetInvalid(f"ruleset is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RulesetInvalid("ruleset must be a YAML mapping")

    try:
        document = RulesetDocument.model_validate(parsed)
    except Exception as exc:                       # pydantic ValidationError
        raise RulesetInvalid(f"ruleset failed schema validation: {exc}") from exc

    problems: list[str] = []
    warnings: list[str] = []
    rules = document.all_rules()

    seen: set[str] = set()
    for rule in rules:
        if rule.rule_id in seen:
            problems.append(f"duplicate rule_id {rule.rule_id!r}")
        seen.add(rule.rule_id)
        _check_sources(rule, problems)
        _check_condition_shapes(rule, problems)
        _check_confirmation_semantics(rule, problems)
        if rule.calibration_id and rule.enabled and rule.calibration_id not in available_calibrations:
            problems.append(
                f"{rule.rule_id}: enabled but calibration artifact "
                f"{rule.calibration_id!r} is not available"
            )

    _check_dominance(rules, problems)
    _check_stage_gating(document, rules, problems)

    if document.meta.phrase_set != phrase_set_version:
        problems.append(
            f"ruleset names phrase set {document.meta.phrase_set!r} but "
            f"{phrase_set_version!r} was supplied"
        )
    if document.meta.methodology_version != methodology_version:
        problems.append(
            f"ruleset targets methodology {document.meta.methodology_version!r}, runtime is "
            f"{methodology_version!r}"
        )
    if document.meta.methodology_manifest_sha256 != methodology_manifest_sha256:
        problems.append(
            "ruleset methodology manifest hash does not match the running frozen artifact"
        )
    low, high = _version_tuple(document.meta.min_service_version), _version_tuple(
        document.meta.max_service_version)
    current = _version_tuple(service_version)
    if not low <= current <= high:
        problems.append(
            f"service version {service_version} is outside the ruleset's supported range "
            f"{document.meta.min_service_version}..{document.meta.max_service_version}"
        )

    unknown_phrase_sets = {r.phrase_set for r in rules} - {phrase_set_version}
    if unknown_phrase_sets:
        problems.append(f"rules reference unknown phrase sets {sorted(unknown_phrase_sets)}")

    for rule in rules:
        if not rule.enabled and rule.runtime_readiness == "READY" \
                and rule.policy_status == "APPROVED":
            warnings.append(
                f"{rule.rule_id}: ready and approved but disabled — check that this is "
                "a rollout decision, not an oversight"
            )

    if problems:
        raise RulesetInvalid("; ".join(sorted(problems)))

    canonical = canonical_json(canonical_document(document))
    return ValidatedRuleset(
        document=document,
        rules_sha256=sha256_hex(canonical),
        canonical_yaml=raw_yaml,
        rule_version=document.meta.rule_version,
        phrase_set_version=phrase_set_version,
        phrase_set_sha256=phrase_set_sha256,
        warnings=tuple(sorted(warnings)),
        _by_id=document.by_id(),
    )


def ruleset_summary(validated: ValidatedRuleset) -> dict[str, Any]:
    """Operator-facing summary. Never includes the raw bytes."""
    rules = validated.rules()
    by_status: dict[str, int] = {}
    for rule in rules:
        by_status[rule.policy_status] = by_status.get(rule.policy_status, 0) + 1
    return {
        "rules_sha256": validated.rules_sha256,
        "rule_version": validated.rule_version,
        "phrase_set_version": validated.phrase_set_version,
        "phrase_set_sha256": validated.phrase_set_sha256,
        "active_stage": validated.document.meta.active_stage,
        "evaluator_version": validated.document.meta.evaluator_version,
        "total_rules": len(rules),
        "enabled_rules": sum(1 for r in rules if r.enabled),
        "by_policy_status": by_status,
        "unpinned_rules": sorted(
            r.rule_id for r in rules
            if any(not t.is_pinned for t in r.thresholds)
        ),
        "warnings": list(validated.warnings),
    }


def unresolved_pins(validated: ValidatedRuleset) -> dict[str, list[str]]:
    """rule_id -> unresolved threshold names. The API reports null plus this,
    never the literal string '<PIN>'."""
    out: dict[str, list[str]] = {}
    for rule in validated.rules():
        names = sorted(t.name for t in rule.thresholds if not t.is_pinned)
        if names:
            out[rule.rule_id] = names
    return out


def rule_sources(rule: RuleSpec) -> set[str]:
    return referenced_sources(rule.condition)
