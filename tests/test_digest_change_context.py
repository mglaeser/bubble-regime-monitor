"""The digest's model is handed what the API serves, and the change.

The owner, 2026-10-04: every message the model writes is handed as much
information as the API serves (as the save-haven monitor gets it), references
included; it makes one point but explains it at more length, 350 characters
on iMessage; and when something changes it gives more context on the change.
No new route and no new design: more input, and the prompt that asks for it.
The model writes the daily digest alone (tests/test_message_engine.py,
TestRoundOneOn145), so the digest is what changes here.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings, get_settings
from app.db import session_scope
from app.indicators import v_vix
from app.message_engine import checks, composer, context
from app.message_engine.checks import Channel
from app.models import Snapshot
from app.references import FRAMEWORK, LEGS_SCIENCE
from app.services import compute, digest
from app.services import engine_delivery as service

pytestmark = pytest.mark.usefixtures("isolated_db")

T0 = datetime(2026, 10, 4, 6, 0, 49, tzinfo=UTC)


def _flag(state: str, distance: float) -> dict:
    return {"active": state == "ACTIVE", "state": state, "distance_to_threshold": distance}


def _snapshot(*, computed_at: datetime = T0, median: float = 60.51, action_band: str = "de-risk",
              s1: float = 0.98, d1: float = 0.88, rf4: bool = True, qqq: str = "IN",
              red_flag_count: int = 1, v_state: str = "contango", v_multiplier: float = 1.0) -> Snapshot:
    """A snapshot as production stored snapshot 392 (2026-10-04 18:00 UTC):
    flag 1 set aside as contested, so three of the four flags can fire. The
    trend states hold what compute stores: the rule's state at the last
    completed month-end (`qqq` sets QQQ's) beside the running month's
    preview, which stays IN with the latest close above its average."""
    return Snapshot(
        computed_at=computed_at, service_version="3.9.0", median=median, iqr_lo=58.5, iqr_hi=62.57,
        band5=55.74, band95=65.54, point_score=60.47, action_band=action_band, override_fired=False,
        red_flag_count=red_flag_count, red_flag_detail={}, v_multiplier=v_multiplier, v_state=v_state,
        block_s={"value": 0.743865, "indicators": {
            "s1": {"value": 41.38, "sub_score": s1}, "s2": {"value": 38.905092, "sub_score": 0.91},
            "s3": {"value": 111.072, "sub_score": 0.525}, "s4": {"value": 1.3181, "sub_score": 0.25},
            "s5": {"value": -0.061, "sub_score": 0.51}}},
        block_d={"value_raw": 0.491574, "value": 0.491574, "indicators": {
            "d1": {"value": 41.78499, "sub_score": d1}, "d2": {"value": 37.18981, "sub_score": 0.21},
            "d3": {"value": 0.738, "sub_score": 0.3}, "d4": {"value": 0.3208, "sub_score": 0.32}}},
        trend_states={"SPY": {"faber_10mo": "IN", "faber_distance_pct": 5.17517,
                              "faber_month_end_state": "IN", "faber_month_end_period": "2026-09"},
                      "QQQ": {"faber_10mo": "IN", "faber_distance_pct": 8.72458,
                              "faber_month_end_state": qqq, "faber_month_end_period": "2026-09"}},
        fast_alarm={}, judgment_call="A few giants carry the market.", judgment_stale=False,
        data_freshness={}, data_degraded=False, override_required_count=3,
        override_fireable_universe_count=3,
        red_flag_meta={"flags": {"rf1": _flag("BLOCKED", -0.0599), "rf2": _flag("INACTIVE", -38.928),
                                 "rf3": _flag("INACTIVE", -35.0),
                                 "rf4": _flag("ACTIVE" if rf4 else "INACTIVE", -8.2)}})


def _entry() -> dict:
    return composer.library()["prompts"]["daily_digest"]


class TestWhatTheApiServes:
    def test_the_digest_carries_what_the_score_api_serves(self):
        # The median is 60.51 and the headline its whole points, 60: the gaps
        # to the lines are 15 and 0.
        facts = digest.digest_facts(_snapshot())
        assert {name: facts[name] for name in (
            "range_lo", "range_hi", "trim_line", "derisk_line", "trim_line_gap", "derisk_line_gap",
            "override_required", "override_fireable", "override_floor", "rf1_state", "rf2_state",
            "rf3_state", "rf4_state", "rf1_distance", "rf2_distance", "rf3_distance", "rf4_distance", "spy_distance_pct",
            "qqq_distance_pct", "vol_state", "vol_multiplier", "fragility_block", "timing_block",
            "s1_value", "s2_value", "s3_value", "d1_value", "d2_value", "data_degraded")} == {
            "range_lo": 55, "range_hi": 65, "trim_line": 45, "derisk_line": 60, "trim_line_gap": 15,
            "derisk_line_gap": 0, "override_required": 3, "override_fireable": 3, "override_floor": 70,
            "rf1_state": "BLOCKED", "rf2_state": "INACTIVE", "rf3_state": "INACTIVE", "rf4_state": "ACTIVE",
            "rf1_distance": -0.06,
            "rf2_distance": -38.93, "rf3_distance": -35.0, "rf4_distance": -8.2, "spy_distance_pct": 5.2,
            "qqq_distance_pct": 8.7, "vol_state": "contango", "vol_multiplier": 1.0, "fragility_block": 74,
            "timing_block": 49, "s1_value": 41.4, "s2_value": 38.9, "s3_value": 111.1, "d1_value": 41.8,
            "d2_value": 37.2, "data_degraded": False}

    def test_the_band_lines_and_the_override_are_the_methodologys(self):
        from app import methodology

        assert (digest.TRIM_LINE, digest.DERISK_LINE, digest.OVERRIDE_FLOOR) == (
            methodology.get_path("action_bands", "trim_at_or_above"),
            methodology.get_path("action_bands", "derisk_at_or_above"),
            methodology.get_path("override", "target_score"))

    def test_a_reading_that_is_not_there_is_left_out(self):
        snap = _snapshot()
        snap.block_d = {"indicators": {"d1": {"value": None, "sub_score": None}}}
        snap.red_flag_meta = {}
        snap.trend_states = {"SPY": {"faber_month_end_state": "IN", "faber_distance_pct": float("nan")}}
        facts = digest.digest_facts(snap)
        assert facts["d1_value"] is None and facts["timing_block"] is None
        assert facts["rf4_state"] is None and facts["rf4_distance"] is None
        # a trend state that is not there is the monitor's word for it, not a "?"
        assert facts["spy_distance_pct"] is None and facts["qqq_trend"] == "unknown"

    def test_every_fact_is_declared_typed_and_named_in_the_data(self):
        """Owner decision D7 holds for every new fact: a number, a truth
        value or the monitor's own word, passed by the composer's types as
        it is; and the entry's DATA names each one but the template's own
        suffix."""
        facts = digest.digest_facts(_snapshot(), day=_snapshot(), week=_snapshot())
        entry = _entry()
        assert list(facts) == entry["grounding_fields"]
        assert composer.typed_facts(entry, facts) == facts and None not in facts.values()
        data = dict(composer._SECTION_RE.findall(entry["prompt"]))["DATA"]
        assert set(re.findall(r"\{(\w+)\}", data)) == set(facts) - {"override_suffix"}

    def test_a_flag_that_cannot_fire_is_not_called_inactive(self):
        """The flag contract keeps four states apart and counts the flags that
        can fire; the digest took the active bit alone, so a blocked and an
        unknown flag both read "active False" beside a distance. Production:
        flag 1 is BLOCKED in all 490 snapshots that carry the contract. Here:
        the contested statistic ABOVE its critical value, no credit reading,
        and breadth with no index series."""
        from app.engine.aggregate import RedFlags
        from app.engine.snapshot_contract import build_red_flag_meta

        meta = build_red_flag_meta(
            red_flags=RedFlags(), observed_at="2026-11-10T06:00:00+00:00", gsadf_stat=1.50, gsadf_cv95=1.48,
            gsadf_contested=True, gsadf_available=True, gsadf_as_of="2026-11-09", gsadf_stale=False,
            semi_runup_pp=70.0, semis_as_of="2026-11-09", semis_stale=False, hy_oas_bps=None,
            hy_oas_tight_bps=None, hy_oas_as_of=None, hy_oas_stale=None, breadth_pct=41.0,
            breadth_as_of="2026-11-09", breadth_stale=False, near_ath_available=False)
        snap = _snapshot(red_flag_count=0)
        snap.red_flag_meta, snap.override_fireable_universe_count = meta, meta["override_fireable_universe_count"]
        facts = digest.digest_facts(snap)
        assert [facts[f"rf{n}_state"] for n in (1, 2, 3, 4)] == ["BLOCKED", "INACTIVE", "UNKNOWN", "UNKNOWN"]
        assert (facts["rf1_distance"], facts["rf3_distance"], facts["rf4_distance"]) == (0.02, None, -9.0)
        assert (facts["override_fireable"], facts["override_required"]) == (1, 3)
        assert not any(name.endswith("_active") or name.endswith("_active_1d_ago") for name in facts)
        prompt = composer.prompt_for("daily_digest", _entry(), facts, Channel.IMESSAGE,
                                     Settings(_env_file=None, message_engine_enabled=True))
        assert "contested: BLOCKED; its reading minus its threshold: 0.02" in prompt
        assert "three-year low: UNKNOWN; reading minus threshold: - basis points" in prompt
        assert "warning flags active: 0 of 4; 1 of the 4 can fire today; at 3 or more" in prompt
        assert "active False" not in prompt and "  rf1_state = BLOCKED" in prompt
        # the DATA says what the two states that are not ACTIVE or INACTIVE mean
        legend = next(line for line in prompt.splitlines() if line.startswith("a flag's state is "))
        assert all(state in legend for state in ("ACTIVE", "INACTIVE", "BLOCKED", "UNKNOWN"))

    def test_the_flag_states_are_the_contracts(self):
        from app.engine import snapshot_contract as contract

        produced = {contract._flag_state(active=a, fireable=f, available=v)
                    for a in (False, True) for f in (False, True) for v in (False, True)}
        assert produced == {"ACTIVE", "INACTIVE", "UNKNOWN", "BLOCKED"}
        for name in [f"rf{n}_state{when}" for n in (1, 2, 3, 4) for when in ("", "_1d_ago")]:
            assert composer.WORDS[name] == produced
        assert composer.typed("rf1_state", "FIRING") is None

    def test_the_model_reads_the_whole_judgment(self):
        """The judgment's own cap is the composer's: at 180, 24 of
        production's last 60 notes reached the model cut mid-sentence."""
        from app.engine import judgment

        stored = judgment._clean_completion("Breadth is narrow while credit stays calm. " * 12)
        assert 180 < len(stored) <= 300 == composer.JUDGMENT_MAX
        assert composer.typed("judgment", stored) == stored

    def test_a_stale_judgment_is_not_handed_on_as_todays_note(self):
        """When the judgment's call fails the snapshot keeps the LAST
        successful text with judgment_stale = True (app/engine/judgment.py:
        "persist the last successful text with stale:true"): a note written
        for an earlier snapshot's numbers. The score API serves the flag
        beside the text; the digest dropped the flag and handed the text on
        as the "plain-language note you may draw on". Production: 58 of 615
        snapshots hold a stale text, in runs of up to 22 (2026-09-02 22:01 to
        09-06 10:01 UTC, one note for 3.5 days), and one of the 58 digest
        attempts was handed one: 2026-10-01 08:00, the 06:01 snapshot with
        the 02:00 note, which the digest gave as its "Größter Treiber"."""
        snap = _snapshot()
        assert digest.digest_facts(snap)["judgment"] == "A few giants carry the market."
        snap.judgment_stale = True
        facts = digest.digest_facts(snap)
        assert facts["judgment"] == "n/a"
        prompt = composer.prompt_for("daily_digest", _entry(), facts, Channel.IMESSAGE,
                                     Settings(_env_file=None, message_engine_enabled=True))
        assert "plain-language note you may draw on: n/a" in prompt and "A few giants" not in prompt
        # what the snapshot stores and the score API serves is untouched: the text with its flag
        assert (snap.judgment_call, snap.judgment_stale) == ("A few giants carry the market.", True)

    def test_the_volatility_states_are_the_producers(self):
        """v_vix.state's three states for a measured ratio, and the state
        compute_snapshot stores for a run without one: a fourth word, not one
        of the three, so the model is never told a curve nobody measured."""
        from tests.conftest import make_golden_raw_inputs

        measured = {v_vix.state(ratio) for ratio in [0.5, 0.94, 0.95, 0.97, 1.0, 1.01, 2.0]}
        raw = make_golden_raw_inputs()
        raw.vix_ratio = None
        no_ratio = compute.compute_snapshot(raw, mc_samples=2_000, mc_seed=1).v_state
        assert no_ratio not in measured
        assert measured | {no_ratio} == composer.WORDS["vol_state"]


#: The trend states production stored in snapshot 568 (2026-11-03 02:00 UTC),
#: the first of 48 in a row (to 2026-11-10 22:00) in which QQQ's running
#: month read OUT while the rule's state, at October's close, was IN.
PREVIEW_OUT = {
    "SPY": {"faber_10mo": "IN", "faber_live_preview": "IN", "faber_distance_pct": 2.7289757844301397,
            "faber_month_end_state": "IN", "faber_month_end_period": "2026-10"},
    "QQQ": {"faber_10mo": "OUT", "faber_live_preview": "OUT", "faber_distance_pct": -0.3471388086092497,
            "faber_month_end_state": "IN", "faber_month_end_period": "2026-10"}}


class TestTheTrendIsTheRulesState:
    """Leg 2 is Faber's rule, and the rule is read once a month: the state is
    the last COMPLETED month's close against its 10-month average
    (legs.month_end_faber, "the AUTHORITATIVE Faber state ... completed
    months only"), which the alert rules read (app/alerts/input_builder.py).
    `faber_10mo` stands the running month's latest close in for a month-end
    close: "a useful live preview, and the wrong thing to alert on" (the
    same docstring). The digest read the preview and worded it as the state.
    Production, 2026-11-03..11-10: QQQ's preview OUT, the rule's state IN, on
    all eight mornings; the digests said "QQQ von IN auf OUT: 0.3% unter dem
    10-Monats-Mittel; SPY bleibt IN." (11-03), "10-Monats-Trend unverändert:
    SPY IN, QQQ OUT." (11-04) and, as the template, "SPY IN, QQQ OUT."
    (11-09), while no trend alert fired, correctly."""

    def test_an_intramonth_dip_is_a_distance_not_a_state(self):
        snap = _snapshot()
        snap.trend_states = PREVIEW_OUT
        facts = digest.digest_facts(snap)
        assert (facts["spy_trend"], facts["qqq_trend"]) == ("IN", "IN")
        # the dip is still handed over, as what it is: the latest close's distance
        assert (facts["spy_distance_pct"], facts["qqq_distance_pct"]) == (2.7, -0.3)
        entry = _entry()
        template = composer.render_fallback(composer.template_for(entry, None), composer.typed_facts(entry, facts))
        assert "SPY IN, QQQ IN." in template

    def test_a_month_that_closed_below_its_average_is_out_whatever_todays_price(self):
        """The change the digest's task ranks first is the rule's: a day
        earlier IN, today OUT, once the month has closed below its average -
        with the running month's preview back above it."""
        day = _snapshot(computed_at=T0 - timedelta(days=1))
        today = _snapshot(qqq="OUT")
        assert today.trend_states["QQQ"]["faber_10mo"] == "IN"
        facts = digest.digest_facts(today, day=day)
        assert (facts["qqq_trend"], facts["qqq_trend_1d_ago"]) == ("OUT", "IN")
        assert (facts["spy_trend"], facts["spy_trend_1d_ago"]) == ("IN", "IN")

    @pytest.mark.parametrize("states", [
        pytest.param({"SPY": {"faber_10mo": "IN", "faber_month_end_state": None, "faber_month_end_period": None},
                      "QQQ": {"faber_10mo": "OUT", "faber_month_end_state": None, "faber_month_end_period": None}},
                     id="fewer_than_ten_completed_months"),
        pytest.param({"SPY": {"faber_10mo": "IN", "sma200": "IN"}, "QQQ": {"faber_10mo": "OUT"}},
                     id="a_snapshot_older_than_the_key"),
        pytest.param({"SPY": {"faber_10mo": "unknown", "sma200": "unknown"}}, id="no_price_series"),
        pytest.param({}, id="no_trend_states"),
    ])
    def test_without_a_month_end_state_the_trend_is_unknown(self, states):
        """legs.month_end_faber returns None without ten completed months, and
        production's snapshots before id 145 (2026-08-23) hold no such key.
        The preview is not read in its place."""
        snap = _snapshot()
        snap.trend_states = states
        facts = digest.digest_facts(snap, day=snap)
        assert {facts[name] for name in ("spy_trend", "qqq_trend", "spy_trend_1d_ago", "qqq_trend_1d_ago")} == {"unknown"}
        assert composer.typed_facts(_entry(), facts)["spy_trend"] == "unknown"

    def test_the_data_names_the_state_month_end_and_the_distance_a_preview(self):
        data = dict(composer._SECTION_RE.findall(_entry()["prompt"]))["DATA"]
        (line,) = [line for line in data.splitlines() if "{spy_trend}" in line]
        state, distance = line.split("{qqq_trend}")
        assert "{spy_trend}" in state and "month-end" in state and "completed month" in state
        assert "{spy_distance_pct}" in distance and "{qqq_distance_pct}" in distance and "preview" in distance
        assert "changes no state" in distance

    def test_the_trend_words_are_the_rules_states_and_unknown(self):
        """legs.month_end_faber yields IN, OUT or None, and the digest words
        None "unknown". The "?" the digest wrote for a missing preview is no
        trend value any more."""
        from app.engine import legs

        months = [(f"2025-{month:02d}-28", float(month)) for month in range(1, 13)]
        falling = [(day, 13.0 - close) for day, close in months]
        produced = {legs.month_end_faber(series, as_of_month="2026-01", feed_current=True)[0]
                    for series in (months, falling, months[:6])}
        assert produced == {"IN", "OUT", None}
        for name in ("spy_trend", "qqq_trend", "spy_trend_1d_ago", "qqq_trend_1d_ago"):
            assert composer.WORDS[name] == {"IN", "OUT", "unknown"}
        assert composer.typed("spy_trend", "?") is None


class TestTheChange:
    def test_the_change_since_a_day_and_a_week_earlier_is_carried(self):
        day = _snapshot(computed_at=T0 - timedelta(days=1), median=57.4, action_band="trim", s1=0.95,
                        d1=0.8, rf4=False, qqq="OUT", red_flag_count=0)
        week = _snapshot(computed_at=T0 - timedelta(days=7), median=55.2, action_band="trim", red_flag_count=0)
        facts = digest.digest_facts(_snapshot(), day=day, week=week)
        # Each change is the difference of the headlines shown: 60 (median
        # 60.51) against 57 (57.4) and 55 (55.2).
        assert {name: value for name, value in facts.items() if "_1d_" in name or "_7d_" in name} == {
            "median_1d_ago": 57, "median_1d_change": 3, "band_1d_ago": "trim", "override_fired_1d_ago": False,
            "red_flag_count_1d_ago": 0, "rf1_state_1d_ago": "BLOCKED", "rf2_state_1d_ago": "INACTIVE",
            "rf3_state_1d_ago": "INACTIVE", "rf4_state_1d_ago": "INACTIVE", "spy_trend_1d_ago": "IN",
            "qqq_trend_1d_ago": "OUT", "vol_state_1d_ago": "contango", "vol_multiplier_1d_ago": 1.0,
            "s1_1d_change": 0.03, "s2_1d_change": 0.0, "s3_1d_change": 0.0,
            "s4_1d_change": 0.0, "s5_1d_change": 0.0, "d1_1d_change": 0.08, "d2_1d_change": 0.0,
            "d3_1d_change": 0.0, "d4_1d_change": 0.0, "median_7d_ago": 55, "median_7d_change": 5,
            "band_7d_ago": "trim", "red_flag_count_7d_ago": 0}

    def test_a_changed_volatility_term_structure_is_carried(self):
        """The VIX multiplier scales the whole timing block (D = min(D_raw * V, 1),
        app/indicators/v_vix.py), so a change of state moves the score with no
        sub-score moving. Production: on 6 of the 37 digest mornings of
        2026-10-05..11-10 the state differed from the day-earlier slot, and
        the facts carried nothing to compare today's state with."""
        day = _snapshot(computed_at=T0 - timedelta(days=1))
        today = _snapshot(v_state="backwardation", v_multiplier=1.15)
        facts = digest.digest_facts(today, day=day)
        assert (facts["vol_state"], facts["vol_multiplier"]) == ("backwardation", 1.15)
        assert (facts["vol_state_1d_ago"], facts["vol_multiplier_1d_ago"]) == ("contango", 1.0)
        prompt = composer.prompt_for("daily_digest", _entry(), facts, Channel.IMESSAGE,
                                     Settings(_env_file=None, message_engine_enabled=True))
        earlier = next(line for line in prompt.splitlines() if line.startswith("a day earlier:"))
        assert earlier.endswith("volatility term structure contango, its multiplier 1.0")
        assert "  vol_state_1d_ago = contango" in prompt and "  vol_multiplier_1d_ago = 1.0" in prompt

    def test_the_earlier_volatility_state_is_one_of_the_producers_words(self):
        assert composer.WORDS["vol_state_1d_ago"] is composer.WORDS["vol_state"]
        day = _snapshot(computed_at=T0 - timedelta(days=1), v_state="calm")
        assert composer.typed_facts(_entry(), digest.digest_facts(_snapshot(), day=day))["vol_state_1d_ago"] is None

    def test_a_change_of_the_term_structure_can_be_the_point(self):
        """The TASK's first tier names it beside the band, the override, the
        flags and the trends, and the explanation is pointed at the earlier
        multiplier, not at the sub-score changes alone."""
        task = " ".join(dict(composer._SECTION_RE.findall(_entry()["prompt"]))["TASK"].split())
        first = task.split("Choose the point in this order:")[1].split("; else")[0]
        assert "the volatility term structure" in first
        explain = task.split("what changed, from what to what,")[1].split(";")[0]
        assert "the sub-score changes" in explain and "the multiplier a day earlier" in explain

    def test_with_no_earlier_snapshot_the_change_is_left_out(self, monkeypatch):
        facts = digest.digest_facts(_snapshot())
        changes = {name for name in facts if "_1d_" in name or "_7d_" in name}
        assert len(changes) == 26 and all(facts[name] is None for name in changes)
        prompts: list[str] = []
        monkeypatch.setattr(composer, "complete",
                            lambda *, user, **_kw: prompts.append(user) or type("C", (), {"text": "ok"})())
        composer.compose(trigger="daily_digest", channel=Channel.IMESSAGE, priority=3, facts=facts,
                         lib=composer.library(), settings=Settings(_env_file=None, message_engine_enabled=True))
        numbers = prompts[0].split("ALL NUMBERS (name = value):\n")[1].split("\n\n")[0]
        assert "median = 60" in numbers and not any(f"  {name} = " in numbers for name in changes)

    def test_the_digest_compares_with_yesterdays_and_last_weeks_same_slot(self, monkeypatch):
        """Six slots a day, each landing minutes after its hour: a day
        earlier is yesterday's same slot, not the one four hours later, and
        a week earlier is last week's - whichever minute each one landed."""
        rows = {0: 61.4, 4: 60.4, 20: 59.4, 24: 58.4, 28: 57.4, 164: 56.4, 168: 55.4, 172: 54.4}
        jitter = {24: timedelta(seconds=41), 168: timedelta(seconds=-30)}
        with session_scope() as s:
            for hours, median in rows.items():
                s.add(_snapshot(computed_at=T0 - timedelta(hours=hours) + jitter.get(hours, timedelta()),
                                median=median))
            s.commit()
        for key, value in {"MESSAGE_ENGINE_ENABLED": "true", "IMESSAGE_ENABLED": "true", "SMS_ENABLED": "false",
                           "IMESSAGE_API_BASE_URL": "https://messages.example.com",
                           "IMESSAGE_API_KEY": "imp_" + "A" * 40,  # pragma: allowlist secret
                           "IMESSAGE_RECIPIENT": "+491510000000"}.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        handed: list[dict] = []
        monkeypatch.setattr(service, "deliver", lambda *, facts, **_kw: handed.append(facts) or {"status": "sent"})
        try:
            digest.send_daily_digest()
        finally:
            get_settings.cache_clear()
        (facts,) = handed
        assert (facts["median"], facts["median_1d_ago"], facts["median_7d_ago"]) == (61, 58, 55)
        assert (facts["median_1d_change"], facts["median_7d_change"]) == (3, 6)

    def test_after_an_outage_no_older_reading_is_called_a_days(self):
        """The service was down from 2026-08-06 to 08-20. The first snapshot
        after such a gap has nothing a day or a week older within reach of
        its slot, and the change is left out rather than compared with a
        reading days older."""
        with session_scope() as s:
            today = _snapshot()
            s.add_all([today, _snapshot(computed_at=T0 - timedelta(hours=31), median=40.0),
                       _snapshot(computed_at=T0 - timedelta(hours=175), median=30.0)])
            s.commit()
            assert digest._earlier(s, today, digest.DAY_AGO) is None
            assert digest._earlier(s, today, digest.WEEK_AGO) is None
            s.add(_snapshot(computed_at=T0 - timedelta(hours=29, minutes=58), median=45.0))
            s.commit()
            assert digest._earlier(s, today, digest.DAY_AGO).median == 45.0


class TestTheLengthAndThePrompt:
    def test_the_imessage_digest_may_run_to_350_characters(self, monkeypatch):
        """The owner's number, 2026-10-04; SMS stays one GSM-7 segment."""
        settings = Settings(_env_file=None, message_engine_enabled=True)
        assert settings.message_engine_imessage_max_chars == 350 and settings.sms_max_len == 150
        assert composer.library()["channels"]["imessage"]["max_code_points"] == 350
        prompt = composer.prompt_for("daily_digest", _entry(), digest.digest_facts(_snapshot()),
                                     Channel.IMESSAGE, settings)
        assert "at most 350 characters" in prompt
        assert checks.basic_check("x" * 350, channel=Channel.IMESSAGE, max_chars=350) is None

    def test_the_model_is_asked_for_the_most_reasoning(self, monkeypatch):
        """The owner, 2026-10-04: "Please have reasoning on max"; shown that
        1 of 3 max digests sent the template and each took 4.5-10 minutes,
        the owner chose "max, as asked" (2026-10-05). The gateway asks for
        the reasoning summary with it, so the stream carries the thinking
        (tests/test_llm_gateway.py)."""
        asked: list[dict] = []
        monkeypatch.setattr(composer, "complete",
                            lambda **kw: asked.append(kw) or type("C", (), {"text": "61/100, de-risk."})())
        out = composer.compose(trigger="daily_digest", channel=Channel.IMESSAGE, priority=3,
                               facts=digest.digest_facts(_snapshot()), lib=composer.library(),
                               settings=Settings(_env_file=None, message_engine_enabled=True))
        assert out.source == "generated" and asked[0]["reasoning_effort"] == "max"

    def test_the_prompt_asks_for_one_point_chosen_by_what_changed(self):
        sections = dict(composer._SECTION_RE.findall(_entry()["prompt"]))
        task = " ".join(sections["TASK"].split())
        assert task.startswith("Write today's digest about one point, and explain it.")
        assert "a change since a day earlier in the action band, the override, a warning flag" in task
        assert "Never print the sub-scores or their changes" in task
        assert "makes one point and explains it" in composer._SYSTEM

    def test_the_prompt_carries_the_framework_the_methodology_endpoint_serves(self):
        prompt = composer.prompt_for("daily_digest", _entry(), digest.digest_facts(_snapshot()),
                                     Channel.IMESSAGE, Settings(_env_file=None, message_engine_enabled=True))
        assert f"FRAMEWORK - how the monitor reads its numbers:\n{FRAMEWORK}" in prompt
        for leg in LEGS_SCIENCE:
            assert f"- {leg['name']}: {leg['caveat']}" in prompt
        assert "Caveat: Siegel (2016)" in prompt and "(S1, literature-grounded)" in prompt

    def test_the_framework_block_is_only_registry_text_and_the_digests_alone(self):
        """Ground rule 1 on the new block: the framework paragraph, then each
        leg's own name and caveat - nothing else, no link - and no alert
        trigger is given it."""
        block = context.framework_for("daily_digest")
        legs = {f"- {leg['name']}: {leg['caveat']}" for leg in LEGS_SCIENCE}
        lines = block.splitlines()
        assert lines[0] == FRAMEWORK and set(lines[1:]) == legs and len(lines) == 1 + len(LEGS_SCIENCE)
        assert not any(checks._linked(line) or checks._dialable(line) for line in lines)
        assert all(context.framework_for(trigger) == "" for trigger in context.TRIGGER_INDICATORS
                   if trigger != "daily_digest")
