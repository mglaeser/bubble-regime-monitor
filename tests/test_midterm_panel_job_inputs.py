"""The panel job's inputs, and the step that must write each one.

## The defect this file exists because of

`TestEveryCountInputHasAWorkflowProducer` checks that every declared COUNT
input is exported by a named step in the `count` job. It has worked: every
count input has a producer, and the run that proved this lane could reach the
provider got through that job cleanly.

The panel job had no such declaration, so nothing compared it to the workflow.
`panelcli.selected_generation_attempt_cap` reads the generation cap out of the
operator's selected profile — deliberately, so a workflow literal cannot
contradict `VERIFIER_MAX_GENERATION_CALLS` — and that reader needs
`MIDTERM_PIN_RECORD_PATH`. Only the `count` job exported it, and `$GITHUB_ENV`
does not cross jobs.

Panel run 31844576977 is what that cost. Preflight passed, the count job made
its real provider calls and produced count evidence and an executable plan, and
the panel job then died at `MIDTERM_PANEL_UNEXPECTED: entry_point=panelcli
exception_class=KeyError`. `clibase` withholds the message and the traceback
there on purpose — that step holds the provider key — so the log named the
exception class and nothing else, after the count had already been paid for.

Two things were wrong and both are tested here:

1. the panel job did not materialise the pin record;
2. `load_pin_values` reached it with `environ[...]`, so its absence could not
   be reported by name. Every other input in that module is refused by name;
   this was the one that was indexed, which is why it was the one that could
   only crash.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from midtermpanel import inputs  # noqa: E402
from midtermpanel.errors import PanelRefusal  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "midterm-panel-review.yml"


def _document() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job(job_id: str) -> dict:
    return _document()["jobs"][job_id]


# --------------------------------------------- 1. the workflow provides it ---


class TestEveryPanelInputHasAWorkflowProducer:

    def test_every_required_input_is_exported_by_a_step(self):
        rendered = yaml.safe_dump(_job("panel"))
        for spec in inputs.PANEL_INPUTS:
            if not spec["required"]:
                continue
            assert f"{spec['variable']}=" in rendered, (
                f"{spec['variable']} has no producer in the panel job; its "
                f"declared one is {spec['producer']!r}")

    def test_the_named_producer_step_exists(self):
        names = {str(step.get("name") or "").lower()
                 for step in _job("panel")["steps"]}
        for spec in inputs.PANEL_INPUTS:
            if not spec["required"]:
                continue
            _, _, step_name = spec["producer"].partition("/")
            assert any(step_name.strip().lower() in name for name in names), (
                f"no step named like {spec['producer']!r}: {sorted(names)}")

    def test_the_pin_record_is_produced_before_the_key_bearing_step(self):
        """Order matters, not just presence.

        A materialisation step placed after `Execute the governed panel` would
        satisfy both checks above and change nothing about the failure."""
        names = [str(step.get("name") or "").lower()
                 for step in _job("panel")["steps"]]
        pins = next(i for i, n in enumerate(names)
                    if "materialise the operator pin record" in n)
        execute = next(i for i, n in enumerate(names)
                       if "execute the governed panel" in n)
        assert pins < execute, (
            f"the pin record is materialised at step {pins}, after the "
            f"credential-bearing step at {execute}")

    def test_the_pin_record_comes_from_the_trusted_checkout(self):
        """`governance/` with no `ref:` on any checkout is the default
        branch's copy. A pin record read from anywhere the candidate can write
        would let the reviewed change choose its own spending caps."""
        step = next(s for s in _job("panel")["steps"]
                    if "materialise the operator pin record"
                    in str(s.get("name") or "").lower())
        script = str(step.get("run") or "")
        assert "governance/midterm-panel-pins.json" in script
        for forbidden in ("git checkout", "git merge", "download-artifact",
                          "$GITHUB_WORKSPACE/../"):
            assert forbidden not in script, forbidden

    def test_the_panel_does_not_mint_its_own_challenge(self):
        """The panel's challenge comes from the plan the count job counted.

        A second `Materialise the run challenge` here would produce a value
        that could differ from the one the verdicts were written under, while
        `require_approvals` still passed against whichever one it was handed."""
        rendered = yaml.safe_dump(_job("panel"))
        assert "MIDTERM_CHALLENGE_PATH=" not in rendered
        assert "challenge_path" not in [s["name"] for s in inputs.PANEL_INPUTS]

    def test_the_count_job_still_produces_the_same_record(self):
        """The two jobs read the same operator record. If they ever diverge,
        the panel could be capped by a document the count never saw."""
        for job_id in ("count", "panel"):
            step = next(s for s in _job(job_id)["steps"]
                        if "materialise the operator pin record"
                        in str(s.get("name") or "").lower())
            assert "governance/midterm-panel-pins.json" in str(step.get("run"))


# ------------------------------------- 2. absence is refused, not crashed ---


class TestAMissingPinRecordPathIsRefusedByName:

    def test_absent_is_a_refusal_not_a_key_error(self):
        with pytest.raises(PanelRefusal) as caught:
            inputs.load_pin_values({})
        assert "count_input_absent" in caught.value.reason
        assert inputs.PIN_RECORD_PATH_ENV in caught.value.reason

    @pytest.mark.parametrize("value", ["", "   ", "\t\n", None])
    def test_empty_and_whitespace_are_absent_too(self, value):
        """An unset variable and one exported as the empty string are the same
        condition. `$GITHUB_ENV` writes can produce either."""
        with pytest.raises(PanelRefusal) as caught:
            inputs.load_pin_values({inputs.PIN_RECORD_PATH_ENV: value})
        assert "count_input_absent" in caught.value.reason

    def test_a_key_error_is_not_raised_for_any_absence(self):
        """The point of the change, stated as the thing that must not happen.

        A `KeyError` inside the credential-bearing step is reported as
        `exception_class=KeyError` with the message withheld, so it names
        nothing an operator can act on."""
        for environ in ({}, {inputs.PIN_RECORD_PATH_ENV: ""},
                        {"MIDTERM_REVIEW_CLASS": "ROUTINE_PR"}):
            try:
                inputs.load_pin_values(environ)
            except PanelRefusal:
                pass
            except KeyError as exc:  # pragma: no cover - the regression
                pytest.fail(f"KeyError instead of a named refusal: {exc!r}")

    def test_a_path_that_does_not_exist_is_still_refused_by_name(self, tmp_path):
        with pytest.raises(PanelRefusal) as caught:
            inputs.load_pin_values(
                {inputs.PIN_RECORD_PATH_ENV: str(tmp_path / "absent.json")})
        assert "pin_record" in caught.value.reason

    def test_the_real_record_still_loads(self, tmp_path):
        """The refusal must not have changed what a correct call does."""
        record = ROOT / "governance" / "midterm-panel-pins.json"
        loaded = inputs.load_pin_values({
            inputs.PIN_RECORD_PATH_ENV: str(record),
            inputs.REVIEW_CLASS_ENV: "ROUTINE_PR"})
        assert loaded["pins"]
        assert int(loaded["profile"]["generation_attempt_cap"]) > 0


# ----------------------------------------- 3. the reader the panel uses ------


class TestTheGenerationCapReadsThroughTheRefusal:

    def test_the_cap_is_refused_rather_than_crashing(self):
        from midtermpanel import panelcli
        with pytest.raises(PanelRefusal) as caught:
            panelcli.selected_generation_attempt_cap(
                {inputs.REVIEW_CLASS_ENV: "ROUTINE_PR"})
        assert inputs.PIN_RECORD_PATH_ENV in caught.value.reason

    def test_the_cap_is_the_profiles_and_not_a_literal(self):
        """The reason this reader exists at all: a workflow literal of 60 once
        contradicted the approved cap of 80."""
        from midtermpanel import panelcli
        record = ROOT / "governance" / "midterm-panel-pins.json"
        cap = panelcli.selected_generation_attempt_cap({
            inputs.PIN_RECORD_PATH_ENV: str(record),
            inputs.REVIEW_CLASS_ENV: "ROUTINE_PR"})
        document = yaml.safe_load(
            (ROOT / "governance" / "midterm-panel-pins.json")
            .read_text(encoding="utf-8"))
        expected = document["profiles"]["ROUTINE_PR"]["generation_attempt_cap"]
        assert cap == int(expected)

    def test_no_workflow_literal_sets_the_generation_cap(self):
        rendered = WORKFLOW.read_text(encoding="utf-8")
        for line in rendered.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "MIDTERM_GENERATION_ATTEMPT_CAP:" not in stripped, (
                "the cap comes from the operator profile, not the workflow")
