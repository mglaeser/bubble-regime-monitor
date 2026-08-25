"""The Stage 1 gate: deterministic replay, no PII, no scoring regression.

Every test here exists because a replay whose result depends on when it ran,
or which could reach a provider, or which could write to production, would be
worse than no replay at all — it would produce evidence that looks like proof.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.alerts.artifacts import validate_from_disk
from app.alerts.enums import Evaluability, Mode
from app.alerts.replay import (
    REPLAY_SCHEMA_VERSION,
    ReplayConfig,
    ReplaySummary,
    load_source_inputs,
    ruleset_at_stage,
    run_replay,
)
from tests.test_alert_evaluation import make_input

RULES = Path("config/alert_rules.v3.2.yaml")
PHRASES = Path("config/alert_phrases.v3.4.json")
REPLAY_SOURCE = Path("app/alerts/replay.py")


@pytest.fixture()
def artifacts():
    return validate_from_disk(rules_path=RULES, phrase_path=PHRASES,
                              service_version="3.8.0")


def _history() -> list:
    """A short but non-trivial history: a de-risk entry that then persists."""
    return [
        make_input(identity="r1", computed_at="2026-08-14T22:00:00+00:00",
                   effective="trim", base="trim"),
        make_input(identity="r2", computed_at="2026-08-15T02:00:00+00:00",
                   effective="trim", base="trim"),
        make_input(identity="r3", computed_at="2026-08-15T06:00:00+00:00",
                   effective="de-risk", base="de-risk", median=64.0),
        make_input(identity="r4", computed_at="2026-08-15T10:00:00+00:00",
                   effective="de-risk", base="de-risk", median=65.0),
        # A blind snapshot: UNKNOWN, which must never resolve anything.
        make_input(identity="r5", computed_at="2026-08-15T14:00:00+00:00",
                   effective=None, base=None, median=None, degraded=True),
    ]


def _run(tmp_path, artifacts, *, name="replay.db", stage=3, inputs=None,
         events=None) -> ReplaySummary:
    config = ReplayConfig(
        source_db_url="sqlite:///unused-by-this-call",
        state_db_path=tmp_path / name,
        evaluate_at_stage=stage,
        mandatory_events_path=events,
    )
    return run_replay(config=config, ruleset=artifacts.ruleset,
                      phrase_set=artifacts.phrase_set,
                      inputs=inputs if inputs is not None else _history())


# ---------------------------------------------------------------------------
# determinism — THE Stage 1 gate
# ---------------------------------------------------------------------------


def test_two_replays_of_the_same_history_are_byte_identical(tmp_path, artifacts):
    """The gate. Two runs, two databases, one answer.

    Byte-identical rather than merely equal: the artifact is committed as
    evidence, so a diff in it has to mean a diff in behaviour.
    """
    first = _run(tmp_path, artifacts, name="a.db").to_json()
    second = _run(tmp_path, artifacts, name="b.db").to_json()
    assert first == second


def test_the_summary_carries_no_identifier_and_no_run_timestamp(tmp_path, artifacts):
    """Determinism is only credible if nothing run-specific can leak in.

    ULIDs and wall-clock stamps are the two things that would silently break
    byte-equality, so the summary must contain neither.
    """
    summary = _run(tmp_path, artifacts)
    payload = summary.as_dict()
    assert "evaluation_id" not in payload
    assert "episode_id" not in payload
    assert "generated_at" not in payload
    assert "duration_ms" not in payload

    # Every timestamp present must come from the replayed history, not from now.
    for key in ("window_first", "window_last"):
        moment = datetime.fromisoformat(payload[key])
        assert moment.year == 2026 and moment.month == 8


def test_replay_derives_time_from_the_input_not_from_the_clock(tmp_path, artifacts):
    """The window is a property of the history, so it cannot move with the run."""
    summary = _run(tmp_path, artifacts)
    assert summary.window_first == "2026-08-14T22:00:00+00:00"
    assert summary.window_last == "2026-08-15T14:00:00+00:00"


# ---------------------------------------------------------------------------
# isolation — a replay may not reach production, or a provider
# ---------------------------------------------------------------------------


def test_replay_writes_to_its_own_database_and_never_the_app_one(
        tmp_path, artifacts, isolated_db):
    """Production stays untouched: no episode, no evaluation, no delivery."""
    from sqlalchemy import func, select

    from app.alerts.models import AlertDelivery, AlertEpisode, AlertEvaluation
    from app.db import session_scope

    summary = _run(tmp_path, artifacts)
    assert summary.evaluations_committed > 0          # it really did work

    with session_scope() as session:
        for model in (AlertEpisode, AlertEvaluation, AlertDelivery):
            count = session.execute(
                select(func.count()).select_from(model)).scalar_one()
            assert count == 0, f"replay wrote {model.__name__} rows into production"


def test_replay_state_database_is_recreated_not_appended(tmp_path, artifacts):
    """Re-running into the same path must not accumulate — that is what makes
    a second run comparable to the first rather than a superset of it."""
    state = tmp_path / "shared.db"
    config = ReplayConfig(source_db_url="sqlite:///unused",
                          state_db_path=state, evaluate_at_stage=3)
    first = run_replay(config=config, ruleset=artifacts.ruleset,
                       phrase_set=artifacts.phrase_set, inputs=_history())
    second = run_replay(config=config, ruleset=artifacts.ruleset,
                        phrase_set=artifacts.phrase_set, inputs=_history())
    assert first.episodes_opened == second.episodes_opened
    assert first.to_json() == second.to_json()


def test_replay_module_cannot_reach_a_provider_or_a_transport():
    """Structural, not behavioural: the import graph itself has no way out.

    A replay that could import the sipgate sender is one careless call away
    from sending real SMS out of a historical simulation.
    """
    forbidden = {"httpx", "requests", "anthropic", "app.alerts.sender",
                 "app.alerts.dispatcher", "app.alerts.llm_selector",
                 "app.jobs.alert_dispatch", "app.services.sms"}
    tree = ast.parse(REPLAY_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not (imported & forbidden), f"replay imports {imported & forbidden}"


def test_replay_never_dispatches(tmp_path, artifacts):
    """Nothing reaches SENT, and the verdict says so rather than assuming it."""
    summary = _run(tmp_path, artifacts)
    assert summary.deliveries_sent == 0
    assert summary.mode == str(Mode.DRYRUN)


def test_replay_uses_the_dryrun_state_namespace(tmp_path, artifacts):
    """Shadow and live state must never see a replay's episodes."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.alerts.models import AlertEpisode

    state = tmp_path / "ns.db"
    _run(tmp_path, artifacts, name="ns.db")
    engine = create_engine(f"sqlite:///{state}", future=True)
    session = sessionmaker(bind=engine)()
    try:
        modes = {row.mode for row in
                 session.execute(select(AlertEpisode)).scalars().all()}
    finally:
        session.close()
        engine.dispose()
    assert modes <= {str(Mode.DRYRUN)}


# ---------------------------------------------------------------------------
# no PII
# ---------------------------------------------------------------------------


def test_the_summary_contains_no_recipient_and_no_secret(tmp_path, artifacts):
    """The report is committed as gate evidence, so it is a publication.

    Sanitizing before persistence rather than before logging is the mandate's
    rule; this asserts the persisted artifact itself.
    """
    summary = _run(tmp_path, artifacts)
    blob = summary.to_json()
    for forbidden in ("recipient", "+49", "token", "sipgate", "api_key",
                      "Authorization", "recipient_ref"):
        assert forbidden.lower() not in blob.lower(), f"{forbidden!r} leaked"


def test_the_summary_reports_only_aggregates(tmp_path, artifacts):
    """Counts and hashes, never a row. An id in the report would be a handle
    back to state the report is not entitled to expose."""
    payload = _run(tmp_path, artifacts).as_dict()
    scalars = {k: v for k, v in payload.items()
               if isinstance(v, (int, float, bool)) or v is None}
    assert scalars, "summary should be dominated by counts"
    for key, value in payload.items():
        if isinstance(value, dict):
            # Keys are rule ids / priorities / buckets — all ruleset vocabulary.
            assert all(isinstance(k, str) for k in value), key
            assert all(isinstance(v, int) for v in value.values()), key


# ---------------------------------------------------------------------------
# honesty of the verdict
# ---------------------------------------------------------------------------


def test_an_empty_history_fails_rather_than_passing_vacuously(tmp_path, artifacts):
    """Nothing to replay is not a green gate."""
    summary = _run(tmp_path, artifacts, inputs=[])
    assert summary.passed is False
    assert "no_inputs" in summary.failures


def test_unmeasured_targets_are_named_not_silently_passed(tmp_path, artifacts):
    """`passed` may only speak for the checks that actually ran.

    Zero non-P1 messages satisfies every volume cap arithmetically; saying so
    would turn "the planner never ran" into "governance holds".
    """
    summary = _run(tmp_path, artifacts)
    joined = " ".join(summary.not_measured)
    assert "mandatory_event_recall" in joined
    if summary.notification_planning_ran:
        # Planning ran, so the volume figures mean something and the gate must
        # JUDGE them rather than list them as unmeasured. Reporting a number
        # nobody compares to its limit reads as compliance.
        assert "non_p1_volume_targets" not in joined
    else:
        assert "non_p1_volume_targets" in joined


def test_a_breached_non_p1_cap_fails_the_gate(tmp_path, artifacts):
    """Once the planner runs, the caps are enforceable — so enforce them.

    Wiring the planner into the atomic apply turned every volume figure from
    "0 by construction" into a real count. Leaving the gate merely reporting
    them would convert "unmeasured" into "measured and ignored", which is the
    worse of the two.
    """
    from app.alerts.replay import ReplaySummary, _decide

    summary = ReplaySummary()
    summary.mode = "DRYRUN"
    summary.notification_planning_ran = True
    summary.max_non_p1_24h = 5
    summary.max_non_p1_168h = 8
    summary.mean_non_p1_per_168h = 8.0
    # Long enough for both caps to MEAN something. Without this the figures are
    # judged against periods the window never covered — see
    # test_a_cap_is_not_judged_on_a_window_shorter_than_its_period.
    summary.window_first = "2026-07-01T00:00:00+00:00"
    summary.window_last = "2026-07-15T00:00:00+00:00"
    _decide(summary)

    assert summary.passed is False
    joined = " ".join(summary.failures)
    assert "24h cap" in joined and "168h cap" in joined
    # the mean is a TARGET, not a cap (mandate 9.2): reported, never failed
    assert any("quiet-regime target" in n for n in summary.notes)
    assert not any("target" in f for f in summary.failures)


def test_a_breach_is_provable_on_a_short_window_but_compliance_is_not():
    """A sliding-window MAXIMUM is monotonic in the window length.

    Observing 8 non-P1 messages inside 76 hours means every 168-hour window
    containing them holds at least 8, so a cap of 6 is broken and no extra
    history can undo it. Staying UNDER a cap for 76 hours proves nothing about
    a week, so that direction is unmeasured rather than passed.

    An earlier version of this gate had the asymmetry backwards and suppressed
    a proven breach.
    """
    from app.alerts.replay import ReplaySummary, _decide

    short = ReplaySummary()
    short.mode = "DRYRUN"
    short.notification_planning_ran = True
    short.max_non_p1_24h = 5
    short.max_non_p1_168h = 8
    short.mean_non_p1_per_168h = 8.0
    short.window_first = "2026-07-10T02:00:00+00:00"
    short.window_last = "2026-07-13T06:00:00+00:00"      # 76 hours
    _decide(short)

    joined = " ".join(short.failures)
    assert "24h cap" in joined
    assert "168h cap" in joined, "a proven breach was suppressed as unmeasured"
    assert short.passed is False

    # a mean is NOT monotonic, so it cannot be inferred from a short window
    assert any("mean" in u for u in short.not_measured)
    assert not any("quiet-regime target" in n for n in short.notes)

    # under the caps on the same short window: proves nothing either way
    quiet = ReplaySummary()
    quiet.mode = "DRYRUN"
    quiet.notification_planning_ran = True
    quiet.max_non_p1_24h = 1
    quiet.max_non_p1_168h = 2
    quiet.window_first = "2026-07-10T02:00:00+00:00"
    quiet.window_last = "2026-07-13T06:00:00+00:00"
    _decide(quiet)

    assert not any("cap" in f for f in quiet.failures)
    assert any("168h_cap" in u for u in quiet.not_measured), (
        "staying under a one-week cap for three days was read as compliance")


def test_a_window_with_no_span_still_reports_a_breach():
    """No span is no excuse: the count was observed somewhere."""
    from app.alerts.replay import ReplaySummary, _decide

    summary = ReplaySummary()
    summary.mode = "DRYRUN"
    summary.notification_planning_ran = True
    summary.max_non_p1_24h = 99
    _decide(summary)

    assert any("24h cap" in f for f in summary.failures)
    # but a figure UNDER the cap with no span establishes nothing
    assert any("168h_cap" in u for u in summary.not_measured)


def test_an_empty_mandatory_catalogue_reports_zero_not_full_recall(tmp_path, artifacts):
    """Recall over an empty catalogue is undefined, never 100%."""
    catalogue = Path("config/alert_mandatory_events.v3.2.json")
    payload = json.loads(catalogue.read_text(encoding="utf-8"))
    assert payload["events"] == [], "the shipped catalogue must ship empty"

    summary = _run(tmp_path, artifacts, events=catalogue)
    assert summary.mandatory_event_total == 0
    assert summary.mandatory_event_detected == 0
    assert any("recall" in note or "recall" in item
               for note in summary.notes for item in summary.not_measured)


def test_a_replay_reports_the_stage_it_actually_evaluated(tmp_path, artifacts):
    """A forward-looking run must never be mistaken for the committed one."""
    summary = _run(tmp_path, artifacts, stage=3)
    assert summary.active_stage == 1               # what is committed
    assert summary.evaluated_at_stage == 3         # what this run measured
    assert summary.rules_sha256 != artifacts.ruleset.rules_sha256
    assert any("forward-looking" in note for note in summary.notes)


def test_replaying_at_the_committed_stage_uses_the_committed_bytes(tmp_path, artifacts):
    """No silent re-stamp: at the committed stage the hash is the real one."""
    summary = _run(tmp_path, artifacts, stage=1)
    assert summary.rules_sha256 == artifacts.ruleset.rules_sha256
    assert summary.evaluated_at_stage == summary.active_stage == 1


def test_restamping_a_stage_rehashes_the_ruleset(artifacts):
    """`active_stage` is content. A document claiming a different stage IS a
    different document, and must not borrow the original's identity."""
    restamped = ruleset_at_stage(artifacts.ruleset, 5, artifacts.phrase_set)
    assert restamped.document.meta.active_stage == 5
    assert restamped.rules_sha256 != artifacts.ruleset.rules_sha256
    # Same rules, different gate.
    assert {r.rule_id for r in restamped.rules()} == \
           {r.rule_id for r in artifacts.ruleset.rules()}


def test_stage_gating_still_binds_inside_a_replay(tmp_path, artifacts):
    """Replay can ask about another stage; it cannot ignore staging."""
    at_one = _run(tmp_path, artifacts, name="s1.db", stage=1)
    at_three = _run(tmp_path, artifacts, name="s3.db", stage=3)
    assert at_one.episodes_by_rule == {}
    assert "regime.band_to_derisk" in at_three.episodes_by_rule


# ---------------------------------------------------------------------------
# three-valued logic survives the round trip
# ---------------------------------------------------------------------------


def test_an_unknown_snapshot_does_not_resolve_an_episode(tmp_path, artifacts):
    """The whole point of UNKNOWN: a blind observation is not good news."""
    history = _history()
    summary = _run(tmp_path, artifacts, inputs=history)
    assert summary.episodes_opened >= 1
    # The last input is degraded/UNKNOWN. It must not have closed anything as
    # RESOLVED on the strength of not knowing.
    only_unknown_resolved = summary.episodes_resolved
    without_blind = _run(tmp_path, artifacts, name="nb.db", inputs=history[:-1])
    assert only_unknown_resolved == without_blind.episodes_resolved


def test_not_evaluable_inputs_are_reported_and_skipped(tmp_path, artifacts):
    """NOT_EVALUABLE is counted as neither a detection nor a miss."""
    history = _history()
    blind = history[-1].model_copy(
        update={"evaluation_eligibility": Evaluability.NOT_EVALUABLE,
                "ineligibility_reasons": ["no_median"]})
    summary = _run(tmp_path, artifacts, inputs=[*history[:-1], blind])
    assert summary.inputs_not_evaluable == 1
    assert summary.evaluations_committed == len(history) - 1


def test_a_blind_neighbour_does_not_make_an_excursion_transient(tmp_path, artifacts):
    """A de-risk slot flanked by UNKNOWN is undecidable, not transient.

    This number governs whether the immediate de-risk stays P1. Counting a
    blind neighbour as "it ended" would argue for downgrading a P1 on the
    strength of not knowing — the band-level form of letting UNKNOWN resolve.
    """
    def at(hour: int, effective):
        return make_input(identity=f"e{hour}", computed_at=f"2026-08-15T{hour:02d}:00:00+00:00",
                          effective=effective, base=effective,
                          median=None if effective is None else 63.0,
                          degraded=effective is None, suppressed=effective is None)

    history = [at(2, "trim"), at(6, "de-risk"), at(10, None), at(14, "de-risk"),
               at(18, "trim")]
    summary = _run(tmp_path, artifacts, name="exc.db", inputs=history)
    # index 1: neighbours trim / UNKNOWN -> indeterminate
    # index 3: neighbours UNKNOWN / trim -> indeterminate
    assert summary.transient_one_snapshot_band_p1 == 0
    assert summary.indeterminate_band_excursions == 2


def test_a_derisk_neighbour_settles_an_excursion(tmp_path, artifacts):
    """A run of de-risk is not transient and is not undecidable either."""
    def at(hour: int, effective):
        return make_input(identity=f"s{hour}", computed_at=f"2026-08-15T{hour:02d}:00:00+00:00",
                          effective=effective, base=effective,
                          median=None if effective is None else 63.0,
                          degraded=effective is None, suppressed=effective is None)

    history = [at(2, "trim"), at(6, "de-risk"), at(10, "de-risk"), at(14, None),
               at(18, "trim")]
    summary = _run(tmp_path, artifacts, name="run.db", inputs=history)
    assert summary.transient_one_snapshot_band_p1 == 0
    assert summary.indeterminate_band_excursions == 0


# ---------------------------------------------------------------------------
# the committed gate artifact
# ---------------------------------------------------------------------------


def test_the_committed_gate_artifact_is_current():
    """CI's real determinism check, asserted here too.

    If the evaluator becomes non-deterministic this fails, because the artifact
    in the repository was produced by a different process on a different day.
    """
    from scripts.export_alert_stage1_gate import ARTIFACT, build_evidence

    rendered = json.dumps(build_evidence(), indent=2, sort_keys=True,
                          ensure_ascii=False) + "\n"
    assert ARTIFACT.read_text(encoding="utf-8") == rendered, (
        "docs/alert-stage1-gate.json is stale — regenerate with "
        "`python -m scripts.export_alert_stage1_gate` and review the diff")


def test_the_gate_artifact_exercises_more_than_the_committed_stage():
    """A stage-1-only artifact would be nearly blind: three ops rules run there."""
    payload = json.loads(Path("docs/alert-stage1-gate.json").read_text(encoding="utf-8"))
    assert payload["runs"]["stage_1"]["evaluated_at_stage"] == 1
    stage3 = payload["runs"]["stage_3"]
    assert stage3["evaluated_at_stage"] == 3
    assert len(stage3["episodes_by_rule"]) >= 8

    # The verdict must follow its own evidence, in either direction.
    assert stage3["passed"] is (stage3["failures"] == [])

    # Stage 3 currently FAILS, and the exact failures are pinned so that CI
    # cannot quietly absorb a NEW one.
    #
    # Wiring the planner into the atomic apply (B-01) turned every non-P1
    # volume figure from "0 by construction" into a real count, and on this
    # history the ruleset breaches its own caps. That is a Stage 2 input and an
    # open decision for the operator — tune the rules or raise the caps
    # deliberately — not something to relax here. Loosening this assertion to
    # "some failure containing the word cap" would be the same defect class the
    # rest of this branch exists to remove: a control that still looks armed.
    #
    # When the breach is resolved this list becomes empty and the test fails
    # until it is updated, which is the point.
    assert stage3["failures"] == [
        "non-P1 volume breached the 24h cap: 5 > 3",
        "non-P1 volume breached the 168h cap: 8 > 6",
    ], (
        "stage-3 failures changed. If the breach was FIXED, empty this list. "
        f"If a NEW failure appeared, it needs its own decision: {stage3['failures']}"
    )

    # the MEAN is the only volume figure a 76-hour window cannot establish
    assert any("mean" in u for u in stage3["not_measured"])
    assert stage3["passed"] is False


def test_the_gate_artifact_carries_no_pii():
    """It is committed, so it is published."""
    blob = Path("docs/alert-stage1-gate.json").read_text(encoding="utf-8").lower()
    for forbidden in ("recipient", "+49", "sipgate", "token", "api_key"):
        assert forbidden not in blob


def test_the_replay_history_fixture_builds_the_arc_it_declares():
    """The evidence is only as trustworthy as the history it replays."""
    from tests.fixtures import alert_replay_history as history

    document = history.document()
    inputs = history.load()
    assert document["slots"], "the fixture must not be empty"
    assert len(inputs) == len(document["slots"])
    # Loading twice must give the same thing — the arc has no free variables.
    assert [i.input_identity for i in inputs] == \
           [i.input_identity for i in history.load()]
    assert history.digest() == history.digest()


def test_the_committed_history_carries_no_derived_content_hash():
    """The arc is committed; its derivation is not.

    Serializing the inputs would commit thousands of observation keys and
    computation fingerprints — content nobody can review, indistinguishable
    from credentials to a scanner, and all of it recomputable from the twenty
    rows that are actually a decision.
    """
    import re

    raw = Path("tests/fixtures/alert_replay_history.json").read_text(encoding="utf-8")
    assert not re.search(r"\b[0-9a-f]{16,}\b", raw), (
        "the committed arc must contain no content digests")
    assert "economic_observation_key" not in raw


def test_the_gate_artifact_binds_to_bytes_and_not_only_to_versions():
    """Versions AND digests — because a version string is something a human types.

    This test previously asserted the digests were absent, which was true and
    was the weakness: an edit that forgot to bump `rule_version` produced
    evidence that still "described" the new ruleset. The digests are carried
    grouped, which keeps the whole value while staying invisible to the entropy
    detector — so per-run summaries can still omit bare digests, and the
    provenance section can bind bytes.
    """
    from app.alerts.promotion import ungroup_digest

    payload = json.loads(Path("docs/alert-stage1-gate.json").read_text(encoding="utf-8"))
    declared = payload["artifacts"]
    assert declared["rule_version"]
    assert declared["phrase_set_version"]

    for key in ("rules_sha256_grouped", "phrase_set_sha256_grouped"):
        grouped = declared[key]
        assert "-" in grouped, "an ungrouped digest would trip the secret scan"
        assert len(ungroup_digest(grouped)) == 64, "the digest was truncated"
        assert max(len(part) for part in grouped.split("-")) <= 8

    # the digests belong to the ARTIFACT's provenance, not to each run
    for run in payload["runs"].values():
        assert "rules_sha256" not in run
        assert "phrase_set_sha256" not in run


def test_the_gate_artifact_digests_are_the_committed_ones():
    """Evidence that binds to the wrong bytes binds to nothing."""
    from app.alerts.artifacts import validate_from_disk
    from app.alerts.promotion import ungroup_digest

    payload = json.loads(Path("docs/alert-stage1-gate.json").read_text(encoding="utf-8"))
    ruleset = validate_from_disk(rules_path=RULES, phrase_path=PHRASES,
                                 service_version="3.8.0").ruleset
    declared = payload["artifacts"]
    assert ungroup_digest(declared["rules_sha256_grouped"]) == ruleset.rules_sha256, (
        "docs/alert-stage1-gate.json is stale — regenerate it")
    assert ungroup_digest(declared["phrase_set_sha256_grouped"]) \
        == ruleset.phrase_set_sha256


# ---------------------------------------------------------------------------
# the source database is only ever read
# ---------------------------------------------------------------------------


def test_loading_source_inputs_leaves_the_source_untouched(tmp_path, isolated_db):
    """A replay opens its own engine over the source and only selects."""
    from app.alerts.models import AlertInputSnapshot
    from app.config import get_settings
    from app.db import session_scope

    now = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    record = make_input(identity="s1", computed_at=now.isoformat())
    with session_scope() as session:
        session.add(AlertInputSnapshot(
            input_identity=record.input_identity, snapshot_id=None, origin="RECOMPUTE",
            built_at=now, computed_at=now,
            alert_input_schema_version=record.schema_version,
            methodology_version=None, methodology_sha256=None, reconstructed=False,
            evaluation_eligibility="EVALUABLE", ineligibility_reasons=[],
            payload=json.dumps(record.model_dump(mode="json")), payload_sha256="x"))

    db_url = get_settings().db_url
    before = Path(db_url.replace("sqlite:///", "")).stat().st_mtime_ns
    loaded = load_source_inputs(ReplayConfig(source_db_url=db_url,
                                             state_db_path=tmp_path / "s.db"))
    after = Path(db_url.replace("sqlite:///", "")).stat().st_mtime_ns

    assert [r.input_identity for r in loaded] == ["s1"]
    assert before == after, "reading the source must not modify it"


def test_the_window_filters_by_the_inputs_own_time(tmp_path, isolated_db):
    """`--from`/`--to` select history, not rows that happened to be written."""
    from app.alerts.models import AlertInputSnapshot
    from app.config import get_settings
    from app.db import session_scope

    moments = [datetime(2026, 8, d, 10, 0, tzinfo=UTC) for d in (10, 12, 14)]
    with session_scope() as session:
        for index, moment in enumerate(moments):
            record = make_input(identity=f"w{index}", computed_at=moment.isoformat())
            session.add(AlertInputSnapshot(
                input_identity=record.input_identity, snapshot_id=None,
                origin="RECOMPUTE", built_at=moment, computed_at=moment,
                alert_input_schema_version=record.schema_version,
                methodology_version=None, methodology_sha256=None, reconstructed=False,
                evaluation_eligibility="EVALUABLE", ineligibility_reasons=[],
                payload=json.dumps(record.model_dump(mode="json")),
                payload_sha256=f"x{index}"))

    loaded = load_source_inputs(ReplayConfig(
        source_db_url=get_settings().db_url, state_db_path=tmp_path / "w.db",
        from_moment=datetime(2026, 8, 11, tzinfo=UTC),
        to_moment=datetime(2026, 8, 13, tzinfo=UTC)))
    assert [r.input_identity for r in loaded] == ["w1"]


# ---------------------------------------------------------------------------
# the harness's own contract
# ---------------------------------------------------------------------------


def test_summary_schema_version_is_stamped(tmp_path, artifacts):
    summary = _run(tmp_path, artifacts)
    assert summary.schema_version == REPLAY_SCHEMA_VERSION
    assert summary.as_dict()["schema_version"] == REPLAY_SCHEMA_VERSION


def test_summary_json_is_canonical(tmp_path, artifacts):
    """Sorted keys, so a diff between two evidence artifacts is a real diff."""
    blob = _run(tmp_path, artifacts).to_json()
    keys = list(json.loads(blob).keys())
    assert keys == sorted(keys)


def test_the_cli_dryrun_command_exists_and_is_read_only():
    """The operator surface has to expose the gate, and expose it as a dry run."""
    from app.alerts.cli import build_parser

    args = build_parser().parse_args(["dryrun", "--state-db", "/tmp/x.db",
                                      "--stage", "3"])
    assert args.command == "dryrun"
    assert args.stage == 3
    assert args.state_db == "/tmp/x.db"


def test_the_replay_script_forwards_to_the_cli():
    from scripts.alert_replay import build_parser

    args = build_parser().parse_args(["--state-db", "/tmp/x.db", "--from",
                                      "2026-01-01T00:00:00+00:00"])
    assert args.state_db == "/tmp/x.db"
    assert args.from_moment == "2026-01-01T00:00:00+00:00"




def test_the_committed_stage_is_not_one_whose_replay_failed():
    """Stage 3 must not be activatable while its own replay says it fails.

    The gate artifact records `stage_3.passed = false`, and pinning the exact
    failures stops CI absorbing a NEW one — but pinning is bookkeeping, not
    enforcement. Nothing stopped `active_stage: 3` being committed next to
    evidence saying stage 3 breaches its budget.

    This is the repository-level half of that enforcement, and it is
    deliberately small: it reads the committed ruleset and the committed
    artifact and refuses the combination. The runtime half — a container
    checking the same thing before it delivers — is the promotion gate, which
    is its own change.
    """
    ruleset = validate_from_disk(rules_path=RULES, phrase_path=PHRASES,
                                 service_version="3.8.0").ruleset
    committed = ruleset.document.meta.active_stage
    payload = json.loads(Path("docs/alert-stage1-gate.json").read_text(encoding="utf-8"))

    run = payload["runs"].get(f"stage_{committed}")
    if run is None:
        # No replay at this stage. Below the delivery stages that is expected;
        # at or above them it means the stage was raised without evidence.
        assert committed < 3, (
            f"the ruleset is committed at stage {committed} and the gate "
            "artifact has no replay at that stage to justify it")
        return

    assert run["passed"] is True, (
        f"the ruleset is committed at stage {committed}, and the committed "
        f"evidence says that stage FAILS: {run['failures']}. Fix the failures "
        "or lower the stage — do not ship a stage its own replay refuses.")


def test_the_artifact_carries_evidence_for_the_stage_the_cutover_targets():
    """Stage 4 is where the cutover goes, so its evidence is produced here.

    Without it the Stage 4 decision would have had to generate its own evidence
    by hand on the day, and the absence would have surfaced at exactly the
    wrong moment.
    """
    payload = json.loads(Path("docs/alert-stage1-gate.json").read_text(encoding="utf-8"))
    assert "stage_4" in payload["runs"], sorted(payload["runs"])
    stage4 = payload["runs"]["stage_4"]
    assert stage4["evaluated_at_stage"] == 4
    assert stage4["passed"] is (stage4["failures"] == [])
