"""Point-in-time replay over persisted sidecars — the Stage 1 gate.

Three properties make a replay worth trusting, and all three are structural
here rather than a matter of care:

  **It reads history, not the world.** Replay consumes persisted
  `alert_input_snapshot` rows and archived ruleset/phrase bytes. It never asks
  a provider what anything looks like now. This module imports no provider, no
  HTTP client and no sipgate — `tests/test_alert_replay.py` asserts that by
  walking the import graph, so the guarantee cannot be quietly lost.

  **It cannot touch production state.** Everything is applied into an ISOLATED
  database. The source database is opened read-only and only sidecars are read
  out of it; no episode, evaluation, delivery or state row in production is
  created, updated or deleted by a replay.

  **It is deterministic.** Given the same sidecars and the same artifacts, two
  replays produce byte-identical summaries. That requires care: `now` is
  derived from each input's own `computed_at` rather than from a clock, and the
  summary contains no ULID, no timestamp-of-run and no wall-clock duration.
  Determinism is the Stage 1 gate, so it is asserted, not assumed.

Nothing here can send. The sender is `NullSender` by construction and the mode
is `DRYRUN`, which is its own state namespace.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.alerts.budgets import BUDGETED_KINDS
from app.alerts.dto import AlertInput
from app.alerts.enums import (
    EpisodeStatus,
    Evaluability,
    EvaluationRunStatus,
    Mode,
    PlanningState,
    Priority,
    SuppressionReason,
    TransportStatus,
)
from app.alerts.outbox import default_limits
from app.alerts.registry import ValidatedRuleset
from app.config import get_settings
from app.engine.snapshot_contract import BAND_DERISK, STATE_SUPPRESSED
from app.logging_conf import get_logger

log = get_logger(__name__)

#: Bumped when the SHAPE of the summary changes, so a stored gate artifact can
#: be told apart from one produced by a different harness.
REPLAY_SCHEMA_VERSION = 1

#: Rolling windows the load report uses.
WINDOW_24H = timedelta(hours=24)
WINDOW_168H = timedelta(hours=168)


# ---------------------------------------------------------------------------
# configuration and results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayConfig:
    """What to replay, and where to put the state it produces."""

    source_db_url: str
    state_db_path: Path
    from_moment: datetime | None = None
    to_moment: datetime | None = None
    live_profile: str = "replay"
    #: Frozen catalogue of events the system MUST detect (Stage 2 gate input).
    mandatory_events_path: Path | None = None
    #: Evaluate as though the rollout were at this stage. `None` means "the
    #: stage the committed ruleset declares". This is the reason replay exists:
    #: the evidence for advancing to stage N is what stage N *would have done*
    #: over real history, and that cannot be gathered by first advancing to it.
    #: It is confined to dry-run on purpose — see `ruleset_at_stage`.
    evaluate_at_stage: int | None = None


@dataclass
class ReplaySummary:
    """The dry-run report (mandate 23.4). Contains no identifiers and no PII."""

    schema_version: int = REPLAY_SCHEMA_VERSION
    rules_sha256: str = ""
    phrase_set_sha256: str = ""
    active_stage: int = 0
    #: The stage the rules were actually gated at. Differs from the committed
    #: ruleset's stage when this replay was a forward-looking calibration run,
    #: in which case `rules_sha256` is the re-stamped document's OWN hash —
    #: never the committed one, so evidence can never claim the wrong bytes.
    evaluated_at_stage: int = 0
    #: Recorded so the artifact proves WHICH state namespace was written. A
    #: replay is always `dryrun`; a summary claiming `live` is a bug, not a run.
    mode: str = str(Mode.DRYRUN)

    # -- inputs -----------------------------------------------------------
    inputs_total: int = 0
    inputs_evaluable: int = 0
    inputs_partial: int = 0
    inputs_not_evaluable: int = 0
    inputs_reconstructed: int = 0
    window_first: str | None = None
    window_last: str | None = None

    # -- evaluations ------------------------------------------------------
    evaluations_committed: int = 0
    evaluations_timed_out: int = 0
    evaluations_conflict: int = 0
    evaluations_failed: int = 0

    # -- episodes ---------------------------------------------------------
    episodes_opened: int = 0
    episodes_activated: int = 0
    episodes_resolved: int = 0
    episodes_cancelled_unconfirmed: int = 0
    episodes_cancelled_stale: int = 0
    episodes_by_rule: dict[str, int] = field(default_factory=dict)
    episodes_by_priority: dict[str, int] = field(default_factory=dict)
    episodes_by_bucket: dict[str, int] = field(default_factory=dict)
    suppressions_by_reason: dict[str, int] = field(default_factory=dict)

    # -- notifications ----------------------------------------------------
    deliveries_planned: int = 0
    #: MUST stay 0. A replay plans; it never dispatches. Asserted by `_decide`.
    deliveries_sent: int = 0
    deliveries_by_kind: dict[str, int] = field(default_factory=dict)
    held_quiet: int = 0
    held_budget: int = 0
    held_grouping: int = 0
    cancelled_superseded: int = 0
    unknown_blocks: int = 0
    p1_bypasses_of_unknown: int = 0
    digest_items: int = 0

    # -- load -------------------------------------------------------------
    #: False until the delivery planner runs inside a replay. While it is
    #: false every number below is structurally 0, and a 0 that means "nothing
    #: planned" must never be read as "the volume caps were satisfied" — see
    #: `not_measured`.
    notification_planning_ran: bool = False
    max_non_p1_24h: int = 0
    max_non_p1_168h: int = 0
    mean_non_p1_per_168h: float = 0.0
    #: The cap values the verdict was judged AGAINST. Without these the
    #: artifact says "passed" or "failed" while omitting the limits that
    #: decision used — and the planner enforces whatever the runtime settings
    #: say, so raising an env var would quietly run live under caps the
    #: evidence never saw. Recording them is what lets admission notice.
    budget_limits: dict[str, int] = field(default_factory=dict)
    p1_total: int = 0

    # -- governance metrics ----------------------------------------------
    transient_one_snapshot_band_p1: int = 0
    #: De-risk slots whose neighbours were blind, so "transient" cannot be
    #: decided either way. Reported, never folded into the count above.
    indeterminate_band_excursions: int = 0
    band_excursion_months: float = 0.0
    detection_times_slots: dict[str, int] = field(default_factory=dict)
    mandatory_event_total: int = 0
    mandatory_event_detected: int = 0
    mandatory_event_not_evaluable: int = 0

    # -- verdict ----------------------------------------------------------
    passed: bool = False
    failures: list[str] = field(default_factory=list)
    #: Acceptance targets this run could NOT evaluate. `passed` says the checks
    #: that ran all held; it never says the ones listed here did. Naming them
    #: is the difference between an unmeasured target and a met one.
    not_measured: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Canonical, sorted, id-free — safe to commit as gate evidence."""
        payload = {
            k: v for k, v in sorted(self.__dict__.items())
        }
        return payload

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# isolated state database
# ---------------------------------------------------------------------------


def _isolated_session_factory(state_db_path: Path):
    """A session factory over a FRESH database that is not production.

    Deliberately not `app.db.session_scope`: a replay that could reach the live
    engine would be one refactor away from writing episodes into it.
    """
    from contextlib import contextmanager

    from app.models import Base

    state_db_path.parent.mkdir(parents=True, exist_ok=True)
    if state_db_path.exists():
        state_db_path.unlink()
    engine = create_engine(f"sqlite:///{state_db_path}", future=True,
                           connect_args={"check_same_thread": False})

    from sqlalchemy import event as sa_event

    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    sa_event.listen(engine, "connect", _pragmas)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return scope, engine


def load_source_inputs(config: ReplayConfig) -> list[AlertInput]:
    """Read sidecars from the SOURCE database, oldest first. Read-only.

    Opening a separate engine rather than reusing the app's keeps the replay
    from ever holding a write handle on production.
    """
    from app.alerts.models import AlertInputSnapshot

    engine = create_engine(config.source_db_url, future=True,
                           connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session: Session = factory()
    try:
        stmt = select(AlertInputSnapshot).order_by(
            AlertInputSnapshot.built_at.asc(), AlertInputSnapshot.input_identity.asc())
        if config.from_moment is not None:
            stmt = stmt.where(AlertInputSnapshot.built_at >= config.from_moment)
        if config.to_moment is not None:
            stmt = stmt.where(AlertInputSnapshot.built_at <= config.to_moment)
        rows = session.execute(stmt).scalars().all()
        return [AlertInput.model_validate(json.loads(row.payload)) for row in rows]
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def _moment_of(alert_input: AlertInput) -> datetime:
    """The instant this input represents. NEVER a wall clock.

    Using the input's own timestamp is what makes a replay reproducible and
    what makes quiet-hours and TTL arithmetic reflect the historical moment
    rather than the moment somebody happened to run the report.
    """
    stamp = alert_input.computed_at or alert_input.built_at
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return datetime(1970, 1, 1, tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def ruleset_at_stage(ruleset: ValidatedRuleset, stage: int,
                     phrase_set: Any) -> ValidatedRuleset:
    """The same rules, re-stamped to a rollout stage, RE-HASHED honestly.

    A ruleset's `active_stage` is part of its content, so a document that says
    stage 3 is a different document with a different `rules_sha256`. Re-running
    the full validator rather than mutating the dataclass is what keeps that
    true: the re-stamped ruleset has to earn its hash the same way the
    committed one did, and it is rejected on the same grounds.

    This is confined to replay deliberately. Nothing in the production path may
    choose its own stage — `enabled_in_stages` is the rollout gate, and a stage
    is advanced by editing the committed file, never by passing an argument.
    """
    import yaml

    from app import methodology as _M
    from app.alerts.registry import validate_ruleset
    from app.config import get_settings

    document = yaml.safe_load(ruleset.canonical_yaml)
    document["meta"]["active_stage"] = int(stage)
    return validate_ruleset(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        phrase_set_version=phrase_set.version,
        phrase_set_sha256=phrase_set.sha256,
        methodology_version=_M.get_path("_meta", "methodology_version"),
        methodology_manifest_sha256=_M.frozen_sha256(),
        service_version=get_settings().service_version,
    )


def _load_mandatory_events(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    events = payload.get("events", [])
    return events if isinstance(events, list) else []


def run_replay(
    *,
    config: ReplayConfig,
    ruleset: ValidatedRuleset,
    phrase_set: Any,
    inputs: list[AlertInput] | None = None,
) -> ReplaySummary:
    """Replay every sidecar in the window into an isolated database.

    Returns the summary. Nothing is sent, nothing in production is written.
    """
    from app.alerts.artifacts import LoadedArtifacts
    from app.alerts.engine import run_evaluation
    from app.alerts.models import AlertDigestItem, AlertInputSnapshot
    from app.alerts.promotion_service import seed_replay_artifacts

    committed_stage = ruleset.document.meta.active_stage
    if config.evaluate_at_stage is not None \
            and config.evaluate_at_stage != committed_stage:
        ruleset = ruleset_at_stage(ruleset, config.evaluate_at_stage, phrase_set)

    summary = ReplaySummary(
        rules_sha256=ruleset.rules_sha256,
        phrase_set_sha256=ruleset.phrase_set_sha256,
        active_stage=committed_stage,
        evaluated_at_stage=ruleset.document.meta.active_stage,
    )
    if summary.evaluated_at_stage != committed_stage:
        summary.notes.append(
            f"forward-looking replay: rules gated at stage "
            f"{summary.evaluated_at_stage}, committed ruleset is at stage "
            f"{committed_stage}; production gating is unchanged"
        )

    records = inputs if inputs is not None else load_source_inputs(config)
    summary.inputs_total = len(records)
    if not records:
        summary.notes.append(
            "no sidecars in the window — capture has not been running, so there is "
            "nothing to replay yet"
        )
        summary.passed = False
        summary.failures.append("no_inputs")
        return summary

    scope, engine = _isolated_session_factory(config.state_db_path)
    try:
        # Seed the isolated database with the artifacts and the sidecars, so a
        # replay is fully self-contained and can be re-run from its state DB.
        with scope() as session:
            seed_replay_artifacts(
                session,
                LoadedArtifacts(ruleset=ruleset, phrase_set=phrase_set,
                                source="replay"),
                now=_moment_of(records[0]))
            for alert_input in records:
                if session.get(AlertInputSnapshot, alert_input.input_identity) is not None:
                    continue
                payload = json.dumps(alert_input.model_dump(mode="json"), sort_keys=True)
                session.add(AlertInputSnapshot(
                    input_identity=alert_input.input_identity,
                    snapshot_id=None,
                    origin=str(alert_input.origin),
                    built_at=_moment_of(alert_input),
                    computed_at=_moment_of(alert_input),
                    alert_input_schema_version=alert_input.schema_version,
                    methodology_version=alert_input.methodology_version,
                    methodology_sha256=alert_input.methodology_sha256,
                    reconstructed=alert_input.reconstructed,
                    evaluation_eligibility=str(alert_input.evaluation_eligibility),
                    ineligibility_reasons=list(alert_input.ineligibility_reasons),
                    payload=payload,
                    payload_sha256="replay",
                ))

        for alert_input in records:
            eligibility = alert_input.evaluation_eligibility
            if eligibility == Evaluability.EVALUABLE:
                summary.inputs_evaluable += 1
            elif eligibility == Evaluability.PARTIAL:
                summary.inputs_partial += 1
            else:
                summary.inputs_not_evaluable += 1
            if alert_input.reconstructed:
                summary.inputs_reconstructed += 1

            if eligibility == Evaluability.NOT_EVALUABLE:
                # NOT_EVALUABLE is reported, never counted as a detection.
                continue

            outcome = run_evaluation(
                scope,
                alert_input=alert_input,
                current=ruleset,
                mode=Mode.DRYRUN,
                live_profile=config.live_profile,
                now=_moment_of(alert_input),
            )
            if outcome.status == EvaluationRunStatus.COMMITTED:
                summary.evaluations_committed += 1
            elif outcome.status == EvaluationRunStatus.TIMED_OUT:
                summary.evaluations_timed_out += 1
            elif outcome.status == EvaluationRunStatus.CONFLICT:
                summary.evaluations_conflict += 1
            elif outcome.status == EvaluationRunStatus.FAILED:
                summary.evaluations_failed += 1

        summary.window_first = _moment_of(records[0]).isoformat()
        summary.window_last = _moment_of(records[-1]).isoformat()

        with scope() as session:
            _collect_episodes(session, summary, ruleset)
            _collect_deliveries(session, summary)
            summary.digest_items = len(
                session.execute(select(AlertDigestItem)).scalars().all())
            _collect_band_excursions(summary, records)
    finally:
        engine.dispose()

    _collect_mandatory_events(config, summary)
    _decide(summary)
    return summary


def _collect_episodes(session: Session, summary: ReplaySummary,
                      ruleset: ValidatedRuleset) -> None:
    from app.alerts.models import AlertEpisode

    by_rule: Counter[str] = Counter()
    by_priority: Counter[str] = Counter()
    by_bucket: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()

    for episode in session.execute(select(AlertEpisode)).scalars().all():
        summary.episodes_opened += 1
        by_rule[episode.rule_id] += 1
        by_priority[f"P{episode.priority}"] += 1
        rule = ruleset.rule(episode.rule_id)
        by_bucket[rule.bucket if rule else "unknown"] += 1
        if episode.activated_at is not None:
            summary.episodes_activated += 1
        if episode.episode_status == EpisodeStatus.RESOLVED:
            summary.episodes_resolved += 1
        elif episode.episode_status == EpisodeStatus.CANCELLED_UNCONFIRMED:
            summary.episodes_cancelled_unconfirmed += 1
        elif episode.episode_status == EpisodeStatus.CANCELLED_STALE:
            summary.episodes_cancelled_stale += 1
        for reason in episode.suppression_reasons or []:
            by_reason[str(reason)] += 1

    summary.episodes_by_rule = dict(sorted(by_rule.items()))
    summary.episodes_by_priority = dict(sorted(by_priority.items()))
    summary.episodes_by_bucket = dict(sorted(by_bucket.items()))
    summary.suppressions_by_reason = dict(sorted(by_reason.items()))
    summary.unknown_blocks = by_reason.get(str(SuppressionReason.UNKNOWN_BLOCK), 0)


def _collect_deliveries(session: Session, summary: ReplaySummary) -> None:
    """Delivery load, including the rolling-window peaks the budgets govern."""
    from app.alerts.models import AlertDelivery

    rows = session.execute(
        select(AlertDelivery).order_by(AlertDelivery.created_at.asc())).scalars().all()
    summary.deliveries_planned = len(rows)
    summary.notification_planning_ran = bool(rows)
    by_kind: Counter[str] = Counter()
    non_p1_moments: list[datetime] = []

    for row in rows:
        by_kind[str(row.delivery_kind)] += 1
        if row.planning_state == PlanningState.HELD_QUIET:
            summary.held_quiet += 1
        elif row.planning_state == PlanningState.HELD_BUDGET:
            summary.held_budget += 1
        elif row.planning_state == PlanningState.HELD_GROUPING:
            summary.held_grouping += 1
        if row.transport_status == TransportStatus.CANCELLED:
            summary.cancelled_superseded += 1
        elif row.transport_status == TransportStatus.SENT:
            summary.deliveries_sent += 1
        if row.duplicate_risk_acknowledged:
            summary.p1_bypasses_of_unknown += 1
        if row.priority == Priority.P1:
            summary.p1_total += 1
        elif str(row.delivery_kind) in {str(k) for k in BUDGETED_KINDS}:
            created = row.created_at
            if created is not None:
                non_p1_moments.append(
                    created if created.tzinfo else created.replace(tzinfo=UTC))

    summary.deliveries_by_kind = dict(sorted(by_kind.items()))
    summary.max_non_p1_24h = _max_in_window(non_p1_moments, WINDOW_24H)
    summary.max_non_p1_168h = _max_in_window(non_p1_moments, WINDOW_168H)
    if non_p1_moments:
        span = (max(non_p1_moments) - min(non_p1_moments)) or timedelta(hours=168)
        weeks = max(1.0, span / WINDOW_168H)
        summary.mean_non_p1_per_168h = round(len(non_p1_moments) / weeks, 3)


def _max_in_window(moments: list[datetime], window: timedelta) -> int:
    """The busiest rolling window — the number the caps actually govern."""
    if not moments:
        return 0
    ordered = sorted(moments)
    best = 0
    start = 0
    for end in range(len(ordered)):
        while ordered[end] - ordered[start] > window:
            start += 1
        best = max(best, end - start + 1)
    return best


def _band_unknown(state: str | None) -> bool:
    """No published band. Absent and suppressed are the same kind of silence."""
    return state is None or state == STATE_SUPPRESSED


def _collect_band_excursions(summary: ReplaySummary,
                             records: list[AlertInput]) -> None:
    """Transient one-snapshot entries into de-risk.

    The mandate keeps the immediate de-risk P1 only while replay shows at most
    one transient one-snapshot excursion per 24 months, so the harness has to
    measure it rather than assert it.

    A neighbour that is UNKNOWN — degraded coverage, a suppressed band, a blind
    slot — is NOT evidence that the excursion ended. Counting it as one would
    inflate the transient rate and argue for downgrading a P1 on the strength
    of not knowing, which is the same mistake as letting UNKNOWN resolve an
    episode. Those cases are reported separately as indeterminate.
    """
    states = [r.effective_action_state for r in records]
    transient = 0
    indeterminate = 0
    for i in range(1, len(states) - 1):
        if states[i] != BAND_DERISK:
            continue
        before, after = states[i - 1], states[i + 1]
        if before == BAND_DERISK or after == BAND_DERISK:
            # Settled: a de-risk neighbour means this was not one snapshot
            # long, whatever the other neighbour did or did not say.
            continue
        if _band_unknown(before) or _band_unknown(after):
            indeterminate += 1
        else:
            transient += 1
    summary.transient_one_snapshot_band_p1 = transient
    summary.indeterminate_band_excursions = indeterminate

    if len(records) >= 2:
        span = _moment_of(records[-1]) - _moment_of(records[0])
        summary.band_excursion_months = round(span.days / 30.44, 2)


def _collect_mandatory_events(config: ReplayConfig, summary: ReplaySummary) -> None:
    """Recall against the frozen catalogue of events that MUST be detected.

    An event whose window has no evaluable input is reported as NOT_EVALUABLE,
    never as a miss and never as a detection — inflating recall either way
    would make the Stage 2 gate meaningless.
    """
    events = _load_mandatory_events(config.mandatory_events_path)
    summary.mandatory_event_total = len(events)
    if not events:
        summary.notes.append(
            "mandatory-event catalogue is empty — recall is not yet measurable "
            "(Stage 2 gate input, requires operator-frozen fixtures)"
        )
        return
    detected = sum(1 for e in events
                   if e.get("rule_id") in summary.episodes_by_rule)
    summary.mandatory_event_detected = detected
    summary.mandatory_event_not_evaluable = summary.inputs_not_evaluable


def _window_hours(summary: ReplaySummary) -> float | None:
    """How long the replayed history actually covers, in hours."""
    first, last = summary.window_first, summary.window_last
    if not first or not last:
        return None
    try:
        start = datetime.fromisoformat(str(first))
        end = datetime.fromisoformat(str(last))
    except ValueError:
        return None
    return (end - start).total_seconds() / 3600.0


def _decide(summary: ReplaySummary) -> None:
    """The Stage 1 verdict. Fail-closed and explicit about WHY.

    Deliberately narrow. It decides the checks a Stage 1 replay can actually
    make — every evaluation reached a decision, nothing was dispatched, the
    state namespace was `dryrun` — and it records the acceptance targets it
    could NOT make in `not_measured` rather than passing them by default.
    """
    failures: list[str] = []
    unmeasured: list[str] = []
    if summary.evaluations_timed_out:
        failures.append(
            f"{summary.evaluations_timed_out} evaluation(s) timed out")
    if summary.evaluations_conflict:
        failures.append(f"{summary.evaluations_conflict} evaluation conflict(s)")
    if summary.evaluations_failed:
        failures.append(f"{summary.evaluations_failed} evaluation(s) failed")
    if summary.inputs_total and not summary.evaluations_committed:
        failures.append("no evaluation committed despite available inputs")
    if summary.deliveries_sent:
        # Structural, not stylistic: a replay that sent anything means the
        # dry-run harness reached a transport, which is a stop condition.
        failures.append(f"{summary.deliveries_sent} delivery/deliveries reached SENT "
                        "— a replay must never dispatch")
    if summary.mode != str(Mode.DRYRUN):
        failures.append(f"replay ran in mode {summary.mode!r}, not dryrun")

    if not summary.notification_planning_ran:
        # No delivery rule was active at this stage, so the volume figures are
        # structurally zero. Zero non-P1 messages trivially satisfies every cap
        # — which is exactly why it must not be reported as satisfying them.
        unmeasured += [
            "non_p1_volume_targets (24h cap, 168h cap, 168h mean) — no delivery "
            "was planned at this stage, so every count is 0 by construction "
            "rather than by governance",
            "quiet_hours_and_budget_holds — no delivery was planned to hold",
        ]
    else:
        # Planning ran, so the figures MEAN something and the gate must judge
        # them. Leaving them merely reported would turn "unmeasured" into
        # "measured and ignored", which is the worse of the two: a number on a
        # dashboard that no one compares to its limit reads as compliance.
        limits = default_limits(get_settings())
        summary.budget_limits = {"cap_24h": limits.cap_24h,
                                 "cap_168h": limits.cap_168h,
                                 "target_168h": limits.target_168h}
        span = _window_hours(summary)

        # A sliding-window MAXIMUM is monotonic in the window length, and that
        # asymmetry decides what a short history can and cannot establish.
        #
        # Observing 8 non-P1 messages inside 76 hours means every 168-hour
        # window containing them holds at least 8. The cap of 6 is therefore
        # BREACHED, and no amount of additional history can undo it — a longer
        # window only accumulates more. A breach is provable on any window.
        #
        # The converse is not. Staying under a cap for 76 hours says nothing
        # about a week, so a non-breach on a short window is UNMEASURED rather
        # than passed. My first attempt at this got the direction wrong and
        # suppressed a proven breach; the panel was right to refuse it.
        #
        # The MEAN is different again: it is not monotonic, and a per-168h mean
        # taken from 76 hours is an arithmetic accident rather than a rate.
        for label, observed, cap, period in (
            ("24h", summary.max_non_p1_24h, limits.cap_24h, 24.0),
            ("168h", summary.max_non_p1_168h, limits.cap_168h, 168.0),
        ):
            if observed > cap:
                failures.append(
                    f"non-P1 volume breached the {label} cap: {observed} > {cap}")
            elif span is None or span < period:
                unmeasured.append(
                    f"non_p1_volume_{label}_cap — the window spans "
                    f"{'no time' if span is None else f'{span:.1f}h'} and the "
                    f"cap is stated per {label}; staying under it here proves "
                    "nothing about a full period")

        if span is not None and span >= 168.0:
            if summary.mean_non_p1_per_168h > limits.target_168h:
                # A target, not a hard cap (mandate 9.2), so it is reported
                # rather than failed — but reported as a miss, with both
                # numbers.
                summary.notes.append(
                    f"non-P1 mean of {summary.mean_non_p1_per_168h} per 168h is "
                    f"above the quiet-regime target of {limits.target_168h}")
        else:
            unmeasured.append(
                "non_p1_mean_per_168h — a mean is not monotonic in the window "
                "length, so it cannot be inferred from a shorter history")

    if not summary.mandatory_event_total:
        unmeasured.append(
            "mandatory_event_recall — the catalogue is empty; recall over zero "
            "events is undefined, not 100%"
        )
    if summary.band_excursion_months < 24.0:
        unmeasured.append(
            f"transient_derisk_p1_rate — the window spans "
            f"{summary.band_excursion_months} months; the target is stated per "
            f"24 months and cannot be judged on a shorter history"
        )

    summary.failures = failures
    summary.not_measured = unmeasured
    summary.passed = not failures
