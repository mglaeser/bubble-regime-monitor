"""Loading and promoting the immutable artifacts.

Promotion is an OPERATOR action. Nothing here promotes anything as a side
effect of a boot, a deploy or a validation run — `promote=True` only ever comes
from the CLI.

Fallback never escalates. If the candidate ruleset on disk is invalid, the
service keeps evaluating the ruleset that was already promoted and reports
`ops.rules_invalid`. If nothing valid is available at all it reports
`ops.alerting_unavailable` and evaluates nothing — it does not "do its best".

Queued work resolves rules and phrases from the REGISTRY, not from the files:
a delivery planned before a deploy must render with the bytes it was planned
against, and those bytes live in the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import methodology as _M
from app.alerts.enums import RulesetStatus
from app.alerts.errors import AlertingUnavailable, RulesetInvalid, sanitize
from app.alerts.models import AlertPhraseSetRegistry, AlertRulesetRegistry
from app.alerts.phrase_registry import (
    PHRASE_VALIDATOR_VERSION,
    ValidatedPhraseSet,
    validate_phrase_set,
)
from app.alerts.registry import ValidatedRuleset, validate_ruleset
from app.config import get_settings
from app.logging_conf import get_logger

log = get_logger(__name__)

#: Shipped defaults. An operator overrides them with ALERTS_RULES_PATH /
#: ALERTS_PHRASE_PATH; the shipped copies are what CI validates.
REPO_RULES = Path(__file__).resolve().parents[2] / "config" / "alert_rules.v3.2.yaml"
REPO_PHRASES = Path(__file__).resolve().parents[2] / "config" / "alert_phrases.v3.2.json"


@dataclass(frozen=True)
class LoadedArtifacts:
    ruleset: ValidatedRuleset
    phrase_set: ValidatedPhraseSet
    source: str                    # "candidate" | "last_known_good" | "registry"
    fallback_reason: str | None = None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _candidate_paths() -> tuple[Path, Path, Path]:
    settings = get_settings()
    return (
        Path(settings.alerts_rules_path),
        Path(settings.alerts_lkg_path),
        Path(settings.alerts_phrase_path),
    )


def validate_from_disk(
    *,
    rules_path: Path | None = None,
    phrase_path: Path | None = None,
    service_version: str | None = None,
    available_calibrations: frozenset[str] = frozenset(),
) -> LoadedArtifacts:
    """Validate the candidate artifacts. Raises on an invalid pair."""
    settings = get_settings()
    rules_path = rules_path or Path(settings.alerts_rules_path)
    phrase_path = phrase_path or Path(settings.alerts_phrase_path)

    raw_phrases = _read(phrase_path) or _read(REPO_PHRASES)
    if raw_phrases is None:
        raise AlertingUnavailable(f"no phrase set at {phrase_path} or {REPO_PHRASES}")
    phrase_set = validate_phrase_set(raw_phrases)

    raw_rules = _read(rules_path) or _read(REPO_RULES)
    if raw_rules is None:
        raise AlertingUnavailable(f"no ruleset at {rules_path} or {REPO_RULES}")

    ruleset = validate_ruleset(
        raw_rules,
        phrase_set_version=phrase_set.version,
        phrase_set_sha256=phrase_set.sha256,
        methodology_version=_M.get_path("_meta", "methodology_version"),
        methodology_manifest_sha256=_M.frozen_sha256(),
        service_version=service_version or settings.service_version,
        available_calibrations=available_calibrations,
    )
    return LoadedArtifacts(ruleset=ruleset, phrase_set=phrase_set, source="candidate")


def load_active(session: Session, *, service_version: str | None = None) -> LoadedArtifacts:
    """The artifacts evaluation should actually use, with the LKG fallback.

    Order: the candidate on disk, then the last-known-good file, then whatever
    is already PROMOTED in the registry. A fallback is reported, never silent.

    THIS FUNCTION IS MODE-BLIND ON PURPOSE, and takes no `mode` argument.
    Live mode must additionally receive the PROMOTED artifact (audit B-04), and
    that check lives in `load_active_for_mode`. An earlier version of this took
    a `mode` keyword and ignored it, which is worse than not offering one: a
    caller passing mode="live" would read as guarded and receive an unpromoted
    candidate. A parameter that looks like a control and is not is the exact
    shape this system keeps removing.
    """
    rules_path, lkg_path, phrase_path = _candidate_paths()
    try:
        return validate_from_disk(rules_path=rules_path, phrase_path=phrase_path,
                                  service_version=service_version)
    except (RulesetInvalid, AlertingUnavailable) as exc:
        candidate_error = sanitize(exc)
        log.warning("alert_rules_invalid", path=str(rules_path), error=candidate_error)

    try:
        loaded = validate_from_disk(rules_path=lkg_path, phrase_path=phrase_path,
                                    service_version=service_version)
        return LoadedArtifacts(ruleset=loaded.ruleset, phrase_set=loaded.phrase_set,
                               source="last_known_good", fallback_reason=candidate_error)
    except (RulesetInvalid, AlertingUnavailable) as exc:
        log.warning("alert_lkg_invalid", path=str(lkg_path), error=sanitize(exc))

    promoted = load_promoted(session, service_version=service_version)
    if promoted is None:
        raise AlertingUnavailable(
            "no valid ruleset: candidate, last-known-good and registry all unusable"
        )
    return LoadedArtifacts(ruleset=promoted.ruleset, phrase_set=promoted.phrase_set,
                           source="registry", fallback_reason=candidate_error)


def load_active_for_mode(session: Session, *, mode: str,
                         service_version: str | None = None) -> LoadedArtifacts:
    """`load_active`, plus the live-mode admission check.

    Kept as its own entry point so the check cannot be skipped by forgetting a
    keyword: a caller that wants mode-aware loading has to say which mode.
    """
    artifacts = load_active(session, service_version=service_version)
    if str(mode).lower() != "live":
        return artifacts

    promoted = load_promoted(session, service_version=service_version)
    if promoted is None:
        raise AlertingUnavailable(
            "live mode requires a PROMOTED ruleset and the registry has none"
        )
    if promoted.ruleset.rules_sha256 != artifacts.ruleset.rules_sha256:
        raise AlertingUnavailable(
            "live mode refuses an unpromoted ruleset: loaded "
            f"{artifacts.ruleset.rules_sha256[:12]} from {artifacts.source}, "
            f"promoted is {promoted.ruleset.rules_sha256[:12]}. Promote it "
            "deliberately or correct the file."
        )
    # The rules decide WHETHER to alert; the phrase set decides what the alert
    # SAYS, and it is the half that reaches the phone. Binding only the rules
    # would admit a ruleset whose rules were promoted while its text was not,
    # which is the more dangerous of the two to get wrong unnoticed.
    if promoted.ruleset.phrase_set_sha256 != artifacts.ruleset.phrase_set_sha256:
        raise AlertingUnavailable(
            "live mode refuses an unpromoted phrase set: loaded "
            f"{artifacts.ruleset.phrase_set_sha256[:12]} from {artifacts.source}, "
            f"promoted is {promoted.ruleset.phrase_set_sha256[:12]}. The rules "
            "match, so this is a text change that was never promoted."
        )
    return artifacts


def load_promoted(session: Session, *,
                  service_version: str | None = None) -> LoadedArtifacts | None:
    """The most recently promoted ruleset, rebuilt from the registry bytes."""
    row = session.execute(
        select(AlertRulesetRegistry)
        .where(AlertRulesetRegistry.status == RulesetStatus.PROMOTED)
        .order_by(AlertRulesetRegistry.promoted_at.desc())
        .limit(1)
    ).scalars().first()
    if row is None:
        return None
    return load_by_hash(session, row.rules_sha256, service_version=service_version)


def load_by_hash(session: Session, rules_sha256: str, *,
                 service_version: str | None = None) -> LoadedArtifacts | None:
    """Rebuild an archived ruleset from its stored canonical bytes.

    This is how an open episode keeps being evaluated under the ruleset that
    opened it after a promotion — the bytes are in the database, so the file on
    disk having moved on is irrelevant.
    """
    row = session.get(AlertRulesetRegistry, rules_sha256)
    if row is None:
        return None
    phrase_row = session.get(AlertPhraseSetRegistry, row.phrase_set_version)
    if phrase_row is None:
        return None
    phrase_set = validate_phrase_set(phrase_row.canonical_json)
    ruleset = validate_ruleset(
        row.canonical_yaml,
        phrase_set_version=phrase_set.version,
        phrase_set_sha256=phrase_set.sha256,
        methodology_version=row.methodology_version,
        methodology_manifest_sha256=row.methodology_manifest_sha256,
        service_version=service_version or get_settings().service_version,
    )
    return LoadedArtifacts(ruleset=ruleset, phrase_set=phrase_set, source="registry")


def register(session: Session, artifacts: LoadedArtifacts, *, now: datetime | None = None,
             promote: bool = False, promoted_by: str | None = None) -> str:
    """Persist the artifacts and optionally PROMOTE them.

    Registering is cheap and idempotent — the bytes are addressed by their
    hash, and re-registering the same hash is a no-op. Promotion is the part an
    operator has to mean.
    """
    now = now or datetime.now(UTC)
    phrase = artifacts.phrase_set
    if session.get(AlertPhraseSetRegistry, phrase.version) is None:
        session.add(AlertPhraseSetRegistry(
            phrase_set_version=phrase.version,
            phrase_set_sha256=phrase.sha256,
            canonical_json=phrase.canonical_json,
            validator_version=PHRASE_VALIDATOR_VERSION,
            validated_at=now,
            worst_case_test_sha256=phrase.worst_case_test_sha256,
            created_by=sanitize(promoted_by) if promoted_by else None,
        ))
        session.flush()

    ruleset = artifacts.ruleset
    row = session.get(AlertRulesetRegistry, ruleset.rules_sha256)
    if row is None:
        row = AlertRulesetRegistry(
            rules_sha256=ruleset.rules_sha256,
            rule_version=ruleset.rule_version,
            canonical_yaml=ruleset.canonical_yaml,
            phrase_set_version=phrase.version,
            phrase_set_sha256=phrase.sha256,
            alert_input_schema_version=ruleset.document.meta.alert_input_schema_version,
            methodology_version=ruleset.document.meta.methodology_version,
            methodology_manifest_sha256=ruleset.document.meta.methodology_manifest_sha256,
            min_service_version=ruleset.document.meta.min_service_version,
            max_service_version=ruleset.document.meta.max_service_version,
            validated_at=now,
            status=RulesetStatus.VALIDATED,
        )
        session.add(row)
        session.flush()

    if promote:
        for other in session.execute(
            select(AlertRulesetRegistry).where(
                AlertRulesetRegistry.status == RulesetStatus.PROMOTED,
                AlertRulesetRegistry.rules_sha256 != ruleset.rules_sha256,
            )
        ).scalars().all():
            other.status = RulesetStatus.SUPERSEDED
            other.superseded_at = now
        row.status = RulesetStatus.PROMOTED
        row.promoted_at = now
        row.promoted_by = sanitize(promoted_by) if promoted_by else None
        log.info("alert_ruleset_promoted", rules_sha256=row.rules_sha256,
                 rule_version=row.rule_version)
    return row.rules_sha256


def archived_rulesets(session: Session, hashes: list[str], *,
                      service_version: str | None = None) -> dict[str, ValidatedRuleset]:
    """Rebuild every archived ruleset that still owns an open episode."""
    out: dict[str, ValidatedRuleset] = {}
    for rules_sha in hashes:
        loaded = load_by_hash(session, rules_sha, service_version=service_version)
        if loaded is not None:
            out[rules_sha] = loaded.ruleset
        else:
            log.error("alert_archived_ruleset_missing", rules_sha256=rules_sha)
    return out
