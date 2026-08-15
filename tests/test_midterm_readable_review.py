"""The readable panel review: rendering, sanitization, and publication.

## What this file is defending

The panel already refuses correctly. What it could not do was SAY anything: a
reader saw `midterm-panel-review — panel blocked` and had no way to learn which
line three models objected to. Publishing that means taking two classes of
attacker-influenced text — a file path the candidate chose and a reason a
provider wrote — and putting them somewhere GitHub renders as Markdown, in a
comment posted by a job that is holding a provider key.

So the tests below are mostly about the second half of that sentence. The
grouping and the wording are checked once each; the escaping, the bounds, the
withholding and the URL construction are checked from several directions,
because those are what turn a review into an incident.

## Zero provider calls, by construction

Nothing in this file has a transport. `scan` is the engine's own `scan_text`,
which is a pure function over a string; the publisher's opener is a recorder;
and `TestZeroProviderCalls` states the property as an assertion rather than
leaving it as a fact about how the file happens to be written.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from midtermpanel import (  # noqa: E402
    PANEL_MODELS,
    REPOSITORY_NUMERIC_ID,
    REQUIRED_APPROVER,
    reviewpublish,
    reviewrender,
)
from midtermpanel.errors import PanelRefusal  # noqa: E402
from verifier import preflight as verifier_preflight  # noqa: E402

HEAD = "a" * 40
BASE = "b" * 40
CHALLENGE = "midterm-vertical-challenge-" + "c" * 32
RUN_URL = "https://github.com/mglaeser/bubble-regime-monitor/actions/runs/7"


def scan(text):
    """The ENGINE's scanner, narrowed exactly as `enginebridge.secret_scanner`
    narrows it: no allowlist, no cleared hashes, nothing that can clear."""
    return verifier_preflight.scan_text(text)


# ------------------------------------------------------------- fixtures -----


def unit(path: str, *, lines=(10, 14), tag: str = "0") -> dict:
    """A plan unit carrying the two things the review is located by."""
    import base64
    import hashlib
    encoded = base64.b64encode(path.encode("utf-8")).decode("ascii")
    digest = hashlib.sha256(f"{path}:{lines}:{tag}".encode()).hexdigest()
    return {"unit_sha256": digest, "path_bytes_b64": encoded,
            "new_line_range": list(lines), "old_line_range": None,
            "git_status": "M"}


def plan_of(*units) -> dict:
    return {"final_units": list(units)}


def verdict(*, refuted: bool, reason: str, confidence: str = "high",
            categories=("data_flow", "secret_handling")) -> dict:
    return {"refuted": refuted, "confidence": confidence, "reason": reason,
            "checked_categories": list(categories)}


def vote(model: str, verdicts: dict) -> dict:
    return {"model": model, "v": {"model_id": model,
                                  "verdicts_by_unit": verdicts}}


def aggregate_of(decision: str) -> dict:
    return {"decision": decision, "models_voting": list(PANEL_MODELS),
            "required_approver": REQUIRED_APPROVER, "votes": 3,
            "strict_any_refutation": True}


def build(*, decision: str, votes: list, plan: dict, challenge=CHALLENGE):
    return reviewrender.build_review(
        decision=decision, candidate_head_sha=HEAD, candidate_base_sha=BASE,
        votes=votes, plan=plan, aggregate_record=aggregate_of(decision),
        scan=scan, run_url=RUN_URL, run_id=7, evidence_sha256="e" * 64,
        count_evidence_sha256="d" * 64, challenge=challenge)


def rendered(*, decision: str, votes: list, plan: dict,
             challenge=CHALLENGE) -> str:
    review = build(decision=decision, votes=votes, plan=plan,
                   challenge=challenge)
    return reviewrender.render(review, challenge=challenge)


# ------------------------------------------------------- 1. no findings -----


class TestAnApprovedReviewSaysSoExplicitly:
    """Requirement 8. The sentence is a constant, not a phrasing choice.

    Silence is the failure mode being closed. A comment that merely omitted a
    findings section would be indistinguishable from a renderer that crashed
    halfway, and the reader's correct response to those two is opposite."""

    @pytest.fixture
    def body(self):
        one = unit("app/thing.py")
        return rendered(
            decision="approved",
            votes=[vote(model, {one["unit_sha256"]: verdict(
                refuted=False,
                reason=f"{model} found the precondition preserved")})
                for model in PANEL_MODELS],
            plan=plan_of(one))

    def test_it_states_that_nothing_was_reported(self, body):
        assert reviewrender.NO_FINDINGS in body

    def test_it_names_the_decision_the_head_and_the_base(self, body):
        assert "**approved**" in body
        assert HEAD in body and BASE in body

    def test_it_names_every_governed_model_and_the_required_approver(self, body):
        for model in PANEL_MODELS:
            assert f"`{model}`" in body
        assert f"| Required approver | `{REQUIRED_APPROVER}` |" in body

    def test_it_references_the_actions_run_and_the_evidence(self, body):
        assert RUN_URL in body
        assert "ev=" + "e" * 16 in body

    def test_it_carries_the_marker_and_the_exact_head_binding(self, body):
        assert reviewrender.MARKER in body
        assert reviewrender.head_of(body) == HEAD

    def test_approvals_are_not_rendered_as_findings(self, body):
        assert "#### Findings" not in body


# ------------------------------------------------- 2/3/4. real findings -----


class TestOneModelRaisingAFinding:
    """Requirement 13's one-model case, and the whole field list of
    requirement 2 checked in one place."""

    @pytest.fixture
    def body(self):
        one = unit("app/thing.py", lines=(41, 58))
        raised = {one["unit_sha256"]: verdict(
            refuted=True,
            reason="the subtraction inverts the documented addition contract",
            confidence="high", categories=("logic", "interface_contract"))}
        quiet = {one["unit_sha256"]: verdict(
            refuted=False, reason="no untrusted value reaches a sink here")}
        return rendered(
            decision="blocked",
            votes=[vote("gpt-5.3-codex", raised),
                   vote("gpt-5.6-sol", quiet),
                   vote("gpt-4.1-mini", quiet)],
            plan=plan_of(one))

    def test_it_groups_the_finding_by_file_and_changed_line_range(self, body):
        assert "**app/thing.py lines 41-58**" in body

    def test_it_names_the_raising_model(self, body):
        assert "raised by `gpt-5.3-codex`" in body

    def test_it_carries_the_confidence(self, body):
        assert "confidence `high`" in body

    def test_it_carries_the_checked_categories(self, body):
        assert "`logic`" in body and "`interface_contract`" in body

    def test_it_carries_the_bounded_reason(self, body):
        assert "the subtraction inverts the documented addition contract" in body

    def test_the_decision_is_blocked(self, body):
        assert "**blocked**" in body
        assert "| Actionable findings | 1 |" in body

    def test_a_non_refuting_model_is_not_reported_as_raising_anything(self,
                                                                     body):
        assert "raised by `gpt-5.6-sol`" not in body


class TestTheRequiredApproverRefuting:
    """The approver's own refutation is a finding like any other.

    Worth its own test because it is the one case where the role gate and the
    strict gate agree for different reasons, and a renderer keyed on 'the
    corroborators disagreed' would have shown nothing."""

    def test_it_is_rendered_and_attributed_to_the_approver(self):
        one = unit("scripts/verifier/executor.py", lines=(802, 833))
        body = rendered(
            decision="blocked",
            votes=[vote(REQUIRED_APPROVER, {one["unit_sha256"]: verdict(
                       refuted=True,
                       reason="the scanned field set no longer covers the "
                              "reason text, so provider output reaches the "
                              "record unscanned",
                       categories=("data_flow", "secret_handling"))}),
                   vote("gpt-5.3-codex", {one["unit_sha256"]: verdict(
                       refuted=False, reason="control flow unchanged here")}),
                   vote("gpt-4.1-mini", {one["unit_sha256"]: verdict(
                       refuted=False, reason="the denominator is unchanged")})],
            plan=plan_of(one))
        assert f"raised by `{REQUIRED_APPROVER}`" in body
        assert "**scripts/verifier/executor.py lines 802-833**" in body
        assert "| Actionable findings | 1 |" in body


class TestMultipleFindingsGroupedByFileAndRange:
    """Requirement 13's grouping case, with a deterministic order.

    Order is asserted because two runs over the same verdicts must produce the
    same document — a sticky comment that reshuffles on every edit is one a
    reader cannot diff."""

    @pytest.fixture
    def body(self):
        first = unit("app/alpha.py", lines=(1, 4), tag="1")
        second = unit("app/alpha.py", lines=(90, 96), tag="2")
        third = unit("zz/omega.py", lines=(7, 7), tag="3")
        units = (first, second, third)
        raised = {u["unit_sha256"]: verdict(
            refuted=True, reason=f"unit {index} fails its stated invariant "
                                 "under the empty input")
            for index, u in enumerate(units)}
        return rendered(decision="blocked",
                        votes=[vote("gpt-4.1-mini", raised)],
                        plan=plan_of(*units))

    def test_every_finding_is_present(self, body):
        assert "| Actionable findings | 3 |" in body
        assert body.count("raised by `gpt-4.1-mini`") == 3

    def test_each_carries_its_own_file_and_range(self, body):
        assert "**app/alpha.py lines 1-4**" in body
        assert "**app/alpha.py lines 90-96**" in body
        assert "**zz/omega.py line 7**" in body

    def test_a_single_line_range_is_rendered_as_a_line_not_a_range(self, body):
        assert "zz/omega.py lines 7-7" not in body

    def test_the_order_is_path_then_line(self, body):
        positions = [body.index("app/alpha.py lines 1-4"),
                     body.index("app/alpha.py lines 90-96"),
                     body.index("zz/omega.py line 7")]
        assert positions == sorted(positions)


class TestTheLocationComesFromThePlanAndNotFromTheModel:
    """Requirement 3. The provider is never asked where it was looking.

    A path a model reports is a path a model can invent, and an invented path in
    a published review sends a reader to the wrong file with the panel's
    authority behind it."""

    def test_a_unit_absent_from_the_plan_is_reported_rather_than_dropped(self):
        stray = unit("app/thing.py")
        body = rendered(
            decision="blocked",
            votes=[vote("gpt-5.6-sol", {stray["unit_sha256"]: verdict(
                refuted=True, reason="this unit is not in the plan at all")})],
            plan=plan_of(unit("app/other.py", tag="other")))
        assert "location unavailable" in body
        assert "| Actionable findings | 1 |" in body

    def test_locations_carry_no_text_the_provider_supplied(self):
        one = unit("app/thing.py", lines=(3, 5))
        located = reviewrender.unit_locations(plan_of(one))
        assert located[one["unit_sha256"]]["path"] == "app/thing.py"
        assert located[one["unit_sha256"]]["new_line_range"] == [3, 5]


# ---------------------------------------------------- 5/6/7. sanitizing -----


class TestASecretShapedReasonIsWithheldAndTheFindingSurvives:
    """Requirement 6. The finding is the point; the prose is not.

    A renderer that dropped the whole finding would let a model suppress its own
    refutation by writing something unpublishable — which is a way to turn a
    block into silence using nothing but word choice."""

    #: A synthetic credential-SHAPED string. Not a credential and not derived
    #: from one; it exists so the engine's scanner has something to catch, and
    #: it carries the pragma because the repository's own secret gate correctly
    #: flags it — which is the behaviour this fixture is here to exercise.
    LEAKED = (
        "sk-proj-EXAMPLEuOAcW8H2p1QzR7vTn4KdYbXm9LsG3JfPq5NwZ"  # pragma: allowlist secret
    )

    @pytest.fixture
    def body(self):
        one = unit("app/thing.py", lines=(2, 9))
        leaked = f"the handler logs the value {self.LEAKED}"
        return rendered(
            decision="blocked",
            votes=[vote("gpt-5.6-sol", {one["unit_sha256"]: verdict(
                refuted=True, reason=leaked, confidence="medium",
                categories=("secret_handling",))})],
            plan=plan_of(one))

    def test_the_explanation_is_replaced_by_the_governed_sentence(self, body):
        assert reviewrender.WITHHELD in body

    def test_the_secret_shaped_token_does_not_appear(self, body):
        assert "sk-proj-" not in body

    def test_the_location_model_confidence_and_categories_still_appear(self,
                                                                      body):
        assert "**app/thing.py lines 2-9**" in body
        assert "raised by `gpt-5.6-sol`" in body
        assert "confidence `medium`" in body
        assert "`secret_handling`" in body

    def test_the_decision_still_blocks(self, body):
        assert "**blocked**" in body


class TestControlCharactersAndMarkupAreNeutralised:
    """Requirement 5. A reason is prose, and prose has no structure."""

    def test_a_newline_in_a_reason_withholds_it(self):
        got = reviewrender.sanitize(
            "looks fine\n### Approved by the panel\nmerge it",
            scan=scan, limit=600, field="reason")
        assert got["published"] is False
        assert got["refusal"] == reviewrender.CONTROL_CHARACTER

    def test_a_null_byte_withholds_it(self):
        got = reviewrender.sanitize("before\x00after", scan=scan, limit=600,
                                    field="reason")
        assert got["refusal"] == reviewrender.CONTROL_CHARACTER

    def test_non_ascii_withholds_it(self):
        got = reviewrender.sanitize("the аddition is wrong", scan=scan,
                                    limit=600, field="reason")
        assert got["refusal"] == reviewrender.OUTSIDE_CHARSET

    @pytest.mark.parametrize("raw,forbidden", [
        ("<script>alert(1)</script>", "<script>"),
        ("<img src=x onerror=alert(1)>", "<img"),
        ("</details><b>merge me</b>", "</details>"),
        ("[click](https://evil.example/x)", "https://evil.example"),
        ("`code` and **bold**", "`code`"),
    ])
    def test_markup_never_survives_into_the_body(self, raw, forbidden):
        one = unit("app/thing.py")
        body = rendered(
            decision="blocked",
            votes=[vote("gpt-5.3-codex", {one["unit_sha256"]: verdict(
                refuted=True, reason=f"the change adds {raw} to the template "
                                     "without escaping it")})],
            plan=plan_of(one))
        assert forbidden not in body

    def test_the_rendered_form_is_still_legible(self):
        escaped = reviewrender.escape_markup("<b>x</b>")
        assert escaped == "&lt;b&gt;x&lt;/b&gt;"


class TestMentionsAreNeutralised:
    """Requirement 4. A published review must not notify anyone.

    GitHub's autolinker reads the SOURCE text, before any entity is decoded, so
    a numeric character reference renders as `@` and does not notify. The body
    gate asserts the absence of a literal `@` rather than trusting the rewrite,
    because 'we escaped it somewhere' and 'there is none left' are different
    claims and only the second one is checkable."""

    def test_a_mention_in_a_reason_does_not_survive_as_a_mention(self):
        one = unit("app/thing.py")
        body = rendered(
            decision="blocked",
            votes=[vote("gpt-4.1-mini", {one["unit_sha256"]: verdict(
                refuted=True,
                reason="ask @mglaeser and @github/security to confirm the "
                       "fail-closed default here")})],
            plan=plan_of(one))
        assert "@" not in body
        assert "&#64;mglaeser" in body

    def test_a_mention_in_a_file_name_does_not_survive_either(self):
        one = unit("app/@mglaeser.py")
        body = rendered(
            decision="blocked",
            votes=[vote("gpt-4.1-mini", {one["unit_sha256"]: verdict(
                refuted=True, reason="the invariant is not restored on the "
                                     "error path")})],
            plan=plan_of(one))
        assert "@" not in body
        assert "&#64;mglaeser.py" in body

    def test_an_issue_cross_reference_does_not_survive(self):
        got = reviewrender.escape_markup("see #23 for context")
        assert "#23" not in got
        assert "&#35;23" in got

    def test_the_body_gate_refuses_a_live_mention_outright(self):
        with pytest.raises(PanelRefusal) as raised:
            reviewrender.assert_publishable(
                f"{reviewrender.MARKER}\nplease ask @someone")
        assert "live_mention" in raised.value.reason


class TestTheCandidateCannotSteerThePublisherOrItsOutput:
    """Requirement 12. The two strings a pull request actually controls.

    A candidate chooses its own file names and its own pull-request number.
    Everything else in the published document comes from governance constants,
    from the plan the trusted job built, or from verdicts that already passed
    the engine's gates."""

    @pytest.mark.parametrize("hostile", [
        "app/<script>alert(1)</script>.py",
        "app/@everyone.py",
        "app/a`b.py",
        "app/x\\y.py",
        'app/"quoted".py',
    ])
    def test_a_hostile_file_name_renders_inert(self, hostile):
        one = unit(hostile)
        body = rendered(
            decision="blocked",
            votes=[vote("gpt-5.3-codex", {one["unit_sha256"]: verdict(
                refuted=True, reason="the added branch never restores the "
                                     "invariant it suspends")})],
            plan=plan_of(one))
        # The heading line only. The footer legitimately contains `<sub>` —
        # markup this module wrote, not markup a candidate supplied — and an
        # assertion over the whole tail would have been testing the footer.
        heading = [line for line in body.splitlines()
                   if line.startswith("**app/")]
        assert len(heading) == 1
        for character in ("<", ">", "@", "`", "\\", '"'):
            assert character not in heading[0]
        assert "&" in heading[0]        # it was escaped, not deleted

    def test_a_file_name_with_a_control_character_withholds_the_location(self):
        one = unit("app/thing\x07.py")
        body = rendered(
            decision="blocked",
            votes=[vote("gpt-5.3-codex", {one["unit_sha256"]: verdict(
                refuted=True, reason="the added branch never restores the "
                                     "invariant it suspends")})],
            plan=plan_of(one))
        assert "path withheld by output-privacy policy" in body
        assert "| Actionable findings | 1 |" in body

    def test_the_marker_is_a_constant_and_not_derived_from_any_input(self):
        source = (ROOT / "scripts" / "midtermpanel"
                  / "reviewrender.py").read_text(encoding="utf-8")
        assignment = [line for line in source.splitlines()
                      if line.startswith("MARKER = ")]
        assert len(assignment) == 1
        assert assignment[0].endswith('-->"')

    @pytest.mark.parametrize("hostile", [
        "1/../../mglaeser/other/issues/1", "1?x=y", "https://evil.example",
        "0", "-3", "", None, True, 1.5,
    ])
    def test_a_hostile_pull_request_number_never_reaches_a_url(self, hostile):
        with pytest.raises(PanelRefusal):
            reviewpublish.comments_url(hostile)

    def test_every_url_is_built_from_the_pinned_repository_id(self):
        for url in (reviewpublish.comments_url(46),
                    reviewpublish.comment_url(12345),
                    reviewpublish.pull_request_url(46)):
            assert url.startswith(
                f"https://api.github.com/repositories/{REPOSITORY_NUMERIC_ID}/")

    def test_the_publisher_module_names_no_other_host(self):
        source = (ROOT / "scripts" / "midtermpanel"
                  / "reviewpublish.py").read_text(encoding="utf-8")
        assert 'API_ROOT = "https://api.github.com"' in source
        # Every `https://` in the file, comments included, is the same host.
        others = [line for line in source.splitlines()
                  if "https://" in line
                  and "https://api.github.com" not in line]
        assert others == []

    def test_an_ungoverned_model_is_refused_rather_than_rendered(self):
        one = unit("app/thing.py")
        with pytest.raises(PanelRefusal) as raised:
            build(decision="blocked", plan=plan_of(one),
                  votes=[vote("gpt-9-attacker", {one["unit_sha256"]: verdict(
                      refuted=True, reason="a voice nobody governs")})])
        assert "ungoverned_model" in raised.value.reason


# --------------------------------------------------------- 8. overflow ------


class TestOutputSizeOverflowIsSummarisedSafely:
    """Requirement 13. A comment GitHub refuses is a review nobody reads."""

    @pytest.fixture
    def many(self):
        units = [unit(f"app/module_{index:03d}.py", lines=(index, index + 3),
                      tag=str(index)) for index in range(400)]
        raised = {u["unit_sha256"]: verdict(
            refuted=True,
            reason=("this unit fails its stated invariant under the empty "
                    "input, and the error path leaves the partially applied "
                    "change in place rather than restoring it. " * 4)[:590])
            for u in units}
        return rendered(decision="blocked",
                        votes=[vote("gpt-4.1-mini", raised)],
                        plan=plan_of(*units))

    def test_the_body_stays_inside_the_bound(self, many):
        assert len(many) <= reviewrender.MAX_BODY_CHARS

    def test_the_omission_is_stated_rather_than_silent(self, many):
        assert "further finding(s) were omitted" in many
        assert "panel-review.json" in many

    def test_the_true_total_is_still_reported(self, many):
        assert "| Actionable findings | 400 |" in many

    def test_no_finding_is_cut_in_half(self, many):
        findings = many.split("#### Findings")[1]
        assert findings.count("raised by `") == findings.count("- unit `")

    def test_a_long_reason_is_bounded_and_marked(self):
        got = reviewrender.sanitize("x" * 5000, scan=scan, limit=120,
                                    field="reason")
        assert got["published"] is True
        assert got["truncated"] is True
        assert len(got["text"]) <= 120

    def test_the_gate_refuses_an_oversized_body_outright(self):
        with pytest.raises(PanelRefusal) as raised:
            reviewrender.assert_publishable(
                reviewrender.MARKER + "x" * reviewrender.MAX_BODY_CHARS)
        assert "unbounded" in raised.value.reason


# --------------------------------------------- 9/10. sticky publication -----


#: How GitHub reports the author of a comment written with `github.token`,
#: and how it reports a person. Both shapes are real response fragments.
BOT = {"login": "github-actions[bot]", "type": "Bot"}
HUMAN = {"login": "mglaeser", "type": "User"}


class Recorder:
    """A GitHub opener that records and answers. Opens nothing.

    It answers only the two reads the publisher makes — the pull request and its
    comments — because those are the two whose absence would make the write
    untestable. It is not a model of GitHub: nothing here depends on GitHub's
    behaviour beyond a status code and the shape of two documents."""

    def __init__(self, *, head=HEAD, comments=None, fail_on=None):
        self.head = head
        self.comments = list(comments or [])
        self.fail_on = fail_on
        self.calls = []

    def __call__(self, request, timeout=None):
        url, method = request.full_url, request.get_method()
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.calls.append({"url": url, "method": method, "body": body})
        if self.fail_on and self.fail_on in url and method != "GET":
            raise OSError("connection reset")
        if method == "GET" and "/pulls/" in url:
            return _Response(200, json.dumps({"head": {"sha": self.head}}))
        if method == "GET":
            return _Response(200, json.dumps(self.comments))
        if method == "POST":
            self.comments.append({"id": 900 + len(self.comments),
                                  "body": body["body"], "user": BOT})
            return _Response(201, "{}")
        for comment in self.comments:
            if str(comment["id"]) in url:
                comment["body"] = body["body"]
        return _Response(200, "{}")

    def writes(self):
        return [c for c in self.calls if c["method"] in ("POST", "PATCH")]


class _Response:
    def __init__(self, status, text):
        self.status = status
        self._text = text

    def read(self):
        return self._text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def a_body(head=HEAD) -> str:
    one = unit("app/thing.py")
    review = reviewrender.build_review(
        decision="approved", candidate_head_sha=head, candidate_base_sha=BASE,
        votes=[vote(m, {one["unit_sha256"]: verdict(
            refuted=False, reason=f"nothing objectionable from {m}")})
            for m in PANEL_MODELS],
        plan=plan_of(one), aggregate_record=aggregate_of("approved"),
        scan=scan, run_url=RUN_URL, run_id=7, evidence_sha256="e" * 64)
    return reviewrender.render(review)


class TestTheCommentIsStickyRatherThanRepeated:
    """Requirement 7. One comment per pull request, edited in place."""

    def test_the_first_run_creates_it(self):
        opener = Recorder()
        got = reviewpublish.publish_review(
            body=a_body(), pr_number=46, candidate_head_sha=HEAD,
            token="t", opener=opener)  # noqa: S106
        assert got["outcome"] == reviewpublish.CREATED
        assert got["published"] is True
        assert [w["method"] for w in opener.writes()] == ["POST"]

    def test_a_second_run_edits_the_first_instead_of_adding_one(self):
        opener = Recorder()
        reviewpublish.publish_review(body=a_body(), pr_number=46,
                                     candidate_head_sha=HEAD, token="t",  # noqa: S106
                                     opener=opener)
        got = reviewpublish.publish_review(body=a_body(), pr_number=46,
                                           candidate_head_sha=HEAD, token="t",  # noqa: S106
                                           opener=opener)
        assert got["outcome"] == reviewpublish.UPDATED
        assert [w["method"] for w in opener.writes()] == ["POST", "PATCH"]
        assert len(opener.comments) == 1

    def test_a_foreign_comment_is_never_edited(self):
        opener = Recorder(comments=[{"id": 1, "body": "looks good to me",
                                     "user": HUMAN}])
        got = reviewpublish.publish_review(body=a_body(), pr_number=46,
                                           candidate_head_sha=HEAD, token="t",  # noqa: S106
                                           opener=opener)
        assert got["outcome"] == reviewpublish.CREATED
        assert opener.comments[0]["body"] == "looks good to me"

    def test_when_two_of_ours_exist_the_oldest_is_the_one_kept(self):
        opener = Recorder(comments=[
            {"id": 7, "body": reviewrender.MARKER + "\nolder", "user": BOT},
            {"id": 9, "body": reviewrender.MARKER + "\nnewer", "user": BOT}])
        got = reviewpublish.publish_review(body=a_body(), pr_number=46,
                                           candidate_head_sha=HEAD, token="t",  # noqa: S106
                                           opener=opener)
        assert got["comment_id"] == 7

    def test_the_marker_is_what_finds_it(self):
        assert reviewpublish.find_sticky_comment(
            [{"id": 1, "body": "no marker here", "user": BOT}]) is None
        assert reviewpublish.find_sticky_comment(
            [{"id": 1, "body": reviewrender.MARKER, "user": BOT}])["id"] == 1

    def test_a_person_quoting_the_marker_is_never_overwritten(self):
        """Anyone who can comment on the pull request can paste the marker.

        Matching on the marker alone would have let them — or an honest person
        quoting the review in a reply — have their own text replaced by the next
        run. Two independent conditions close it: the comment must be written by
        a bot, and the marker must be the very first thing in the body."""
        quoted = [{"id": 1,
                   "body": "I disagree with this:\n" + reviewrender.MARKER,
                   "user": HUMAN}]
        assert reviewpublish.find_sticky_comment(quoted) is None
        impersonating = [{"id": 2, "body": reviewrender.MARKER + "\nhi",
                          "user": HUMAN}]
        assert reviewpublish.find_sticky_comment(impersonating) is None
        opener = Recorder(comments=list(quoted))
        got = reviewpublish.publish_review(
            body=a_body(), pr_number=46, candidate_head_sha=HEAD,
            token="t", opener=opener)  # noqa: S106
        assert got["outcome"] == reviewpublish.CREATED
        assert opener.comments[0]["body"].startswith("I disagree")

    def test_a_bot_is_recognised_by_either_field(self):
        assert reviewpublish.written_by_a_bot({"user": {"type": "Bot"}})
        assert reviewpublish.written_by_a_bot(
            {"user": {"login": "github-actions[bot]", "type": "User"}})
        assert not reviewpublish.written_by_a_bot({"user": {"login": "someone"}})
        assert not reviewpublish.written_by_a_bot({})


class TestAStaleHeadRefusesTheComment:
    """Requirement 7's binding, and the fail-open it closes.

    The panel starts when ordinary CI finishes and takes minutes. If someone
    pushes meanwhile, a comment saying `approved` would sit on a pull request
    whose head no model has seen — an approval next to unreviewed code, arrived
    at by the friendliest possible route."""

    def test_a_moved_head_refuses_the_write(self):
        opener = Recorder(head="f" * 40)
        got = reviewpublish.publish_review(body=a_body(), pr_number=46,
                                           candidate_head_sha=HEAD, token="t",  # noqa: S106
                                           opener=opener)
        assert got["outcome"] == reviewpublish.REFUSED_STALE_HEAD
        assert got["published"] is False
        assert opener.writes() == []

    def test_the_refusal_says_the_status_is_unaffected(self):
        opener = Recorder(head="f" * 40)
        got = reviewpublish.publish_review(body=a_body(), pr_number=46,
                                           candidate_head_sha=HEAD, token="t",  # noqa: S106
                                           opener=opener)
        assert "commit status" in got["refusal"]

    def test_the_body_records_the_head_it_was_built_for(self):
        assert reviewrender.head_of(a_body(head=BASE)) == BASE

    def test_an_api_fault_is_recorded_and_never_raised(self):
        opener = Recorder(fail_on="/comments")
        got = reviewpublish.publish_review(body=a_body(), pr_number=46,
                                           candidate_head_sha=HEAD, token="t",  # noqa: S106
                                           opener=opener)
        assert got["outcome"] == reviewpublish.REFUSED_API
        assert got["published"] is False

    def test_a_run_with_no_pull_request_is_a_normal_outcome(self):
        got = reviewpublish.publish_review(body=a_body(), pr_number="",
                                           candidate_head_sha=HEAD, token="t",  # noqa: S106
                                           opener=Recorder())
        assert got["outcome"] == reviewpublish.REFUSED_NO_PULL_REQUEST


# ---------------------------------------- 11/12. ordering and omissions -----


class TestTheReviewIsPublishedBeforeTheBlockedJobExits:
    """Requirement 9, driven through the real `panelcli.perform`.

    The ordering is the requirement: a finding that only becomes readable after
    a zero exit is a finding that is never readable, because the job that would
    have published it has already failed."""

    @pytest.fixture
    def outcome(self, tmp_path):
        return _run_perform(tmp_path, decision="blocked")

    def test_the_process_still_refuses(self, outcome):
        assert isinstance(outcome["raised"], PanelRefusal)
        assert "category=panel_blocked" in outcome["raised"].reason

    def test_the_review_was_published_first(self, outcome):
        assert outcome["published_body"] is not None
        assert outcome["order"].index("review") < outcome["order"].index("exit")

    def test_the_readable_finding_survived_the_block(self, outcome):
        assert "**app/thing.py lines 10-14**" in outcome["published_body"]
        assert "raised by `gpt-5.6-sol`" in outcome["published_body"]

    def test_both_private_artifacts_were_retained(self, outcome):
        assert outcome["markdown"].exists()
        assert outcome["json"].exists()
        stored = json.loads(outcome["json"].read_text(encoding="utf-8"))
        assert stored["review"]["decision"] == "blocked"
        assert stored["publication"]["published"] is True

    def test_the_cryptographic_evidence_is_still_written_too(self, outcome):
        assert outcome["evidence"].exists()


class TestAnApprovedRunPublishesAndReturns:
    def test_it_returns_normally_and_still_publishes(self, tmp_path):
        outcome = _run_perform(tmp_path, decision="approved")
        assert outcome["raised"] is None
        assert reviewrender.NO_FINDINGS in outcome["published_body"]


class TestNothingPrivateReachesTheComment:
    """Requirement 4, asserted against a body built from text that carries all
    three of the things that must never travel."""

    @pytest.fixture
    def body(self):
        one = unit("app/thing.py")
        return rendered(
            decision="blocked",
            votes=[vote("gpt-5.6-sol", {one["unit_sha256"]: verdict(
                refuted=True,
                reason=f"{CHALLENGE} the handler drops the fail-closed "
                       "default on the error path")})],
            plan=plan_of(one))

    def test_the_execution_challenge_is_absent(self, body):
        assert CHALLENGE not in body

    def test_the_challenge_is_removed_rather_than_suppressing_the_finding(
            self, body):
        assert "the handler drops the fail-closed default" in body
        assert reviewrender.REDACTED in body

    def test_no_proof_of_check_travels(self):
        one = unit("app/thing.py")
        review = build(
            decision="blocked", plan=plan_of(one),
            votes=[vote("gpt-5.6-sol", {one["unit_sha256"]: {
                **verdict(refuted=True, reason="the fail-closed default is "
                                               "dropped on the error path"),
                "proof_of_check": f"{CHALLENGE} read lines 10-14 of thing.py",
                "proof_sha256": "9" * 64}})])
        serialized = json.dumps(review)
        assert "proof_of_check" not in serialized
        assert "read lines 10-14" not in serialized

    def test_a_forbidden_field_anywhere_in_the_review_is_refused(self):
        with pytest.raises(PanelRefusal) as raised:
            reviewrender.assert_no_forbidden_fields(
                {"findings": [{"detail": {"proof_of_check": "x"}}]})
        assert "forbidden_field" in raised.value.reason

    def test_ordinary_prose_containing_a_forbidden_word_is_not_refused(self):
        """The first draft searched the rendered PROSE for `challenge`, so a
        model writing 'this change challenges the invariant' suppressed the
        whole review. The check is structural for exactly that reason."""
        one = unit("app/thing.py")
        body = rendered(
            decision="blocked",
            votes=[vote("gpt-4.1-mini", {one["unit_sha256"]: verdict(
                refuted=True, reason="this change challenges the documented "
                                     "authorization invariant")})],
            plan=plan_of(one))
        assert "challenges the documented authorization invariant" in body

    def test_the_body_gate_still_refuses_a_literal_challenge(self):
        with pytest.raises(PanelRefusal) as raised:
            reviewrender.assert_publishable(
                f"{reviewrender.MARKER}\nthe token was {CHALLENGE}",
                challenge=CHALLENGE)
        assert "execution_challenge" in raised.value.reason


# ------------------------------------------- 11. the decision is machine ----


class TestRenderingCanNeverChangeAVerdict:
    """Requirement 11, from both directions.

    The second direction is the one a summariser gets wrong. A renderer that
    decided a low-confidence refutation was not worth showing would have
    silently converted a block into an approval, and every digest in the
    evidence would still have matched."""

    def test_findings_beside_an_approval_are_refused(self):
        one = unit("app/thing.py")
        with pytest.raises(PanelRefusal) as raised:
            build(decision="approved", plan=plan_of(one),
                  votes=[vote("gpt-5.6-sol", {one["unit_sha256"]: verdict(
                      refuted=True, reason="this unit is not safe as written")})])
        assert "findings_under_an_approval" in raised.value.reason

    def test_a_block_with_nothing_readable_is_refused(self):
        one = unit("app/thing.py")
        with pytest.raises(PanelRefusal) as raised:
            build(decision="blocked", plan=plan_of(one),
                  votes=[vote("gpt-5.6-sol", {one["unit_sha256"]: verdict(
                      refuted=False, reason="nothing objectionable here")})])
        assert "block_with_nothing_to_read" in raised.value.reason

    def test_a_rendered_decision_disagreeing_with_the_aggregate_is_refused(self):
        with pytest.raises(PanelRefusal) as raised:
            reviewrender.assert_rendering_did_not_change_the_decision(
                findings=[], decision="approved",
                aggregate_record={"decision": "blocked"})
        assert "disagrees_with_aggregate" in raised.value.reason

    def test_a_publication_failure_does_not_change_the_outcome(self, tmp_path):
        outcome = _run_perform(tmp_path, decision="blocked", publish_ok=False)
        assert isinstance(outcome["raised"], PanelRefusal)
        assert "category=panel_blocked" in outcome["raised"].reason
        stored = json.loads(outcome["json"].read_text(encoding="utf-8"))
        assert stored["review"]["decision"] == "blocked"


# ------------------------------------------------- 13. no provider calls ----


class TestZeroProviderCalls:
    """Stated rather than assumed. Every fixture above is a pure function over
    strings and a recorder that opens nothing."""

    def test_the_renderer_has_no_transport_and_no_network_import(self):
        source = (ROOT / "scripts" / "midtermpanel"
                  / "reviewrender.py").read_text(encoding="utf-8")
        for forbidden in ("urllib", "socket", "requests", "http.client",
                          "transport"):
            assert forbidden not in source

    def test_the_publisher_reaches_only_the_github_api(self):
        source = (ROOT / "scripts" / "midtermpanel"
                  / "reviewpublish.py").read_text(encoding="utf-8")
        assert "openai" not in source.lower()
        assert "PROVIDER" not in source

    def test_neither_new_module_can_obtain_a_provider_capability(self):
        """Deliberately over the MODULES and not over this file.

        The obvious version — asserting that this test file never names a
        provider transport — is self-defeating: the assertion's own parameter
        list names them, so it fails on itself. The property that matters is
        about the code that ships, not about the file that checks it."""
        for name in ("reviewrender.py", "reviewpublish.py"):
            source = (ROOT / "scripts" / "midtermpanel"
                      / name).read_text(encoding="utf-8")
            for forbidden in ("read_provider_key", "live_generation_transport",
                              "provider_http_opener", "GENERATION_PATH"):
                assert forbidden not in source, name

    def test_the_engine_scanner_is_the_one_the_bridge_hands_out(self):
        from trustedlane import enginebridge
        stub = {"modules": {"verifier.preflight": verifier_preflight}}
        narrowed = enginebridge.secret_scanner(stub)
        assert narrowed("ordinary prose about a function") == []
        assert narrowed(
            TestASecretShapedReasonIsWithheldAndTheFindingSurvives.LEAKED)


# ------------------------------------------------- the perform harness ------


def _run_perform(tmp_path, *, decision: str, publish_ok: bool = True) -> dict:
    """Drive the real `panelcli.perform` with injected seams and no network.

    Everything the function reads off disk is built here through the SAME
    constructors the count job uses, so the strict loaders and `verify_handoff`
    are exercised rather than bypassed."""
    from midtermpanel import COUNT_EVIDENCE_CLASS, panelcli
    from midtermpanel.evidence import build as build_evidence
    from midtermpanel.evidence import digest_of, write_atomic

    one = unit("app/thing.py")
    unit_hash = one["unit_sha256"]
    refuted = decision == "blocked"
    verdicts = {unit_hash: verdict(
        refuted=refuted,
        reason=("the fail-closed default is dropped on the error path"
                if refuted else "the precondition survives the change"))}
    votes = [vote(m, verdicts if m == REQUIRED_APPROVER else {
        unit_hash: verdict(refuted=False,
                           reason=f"{m} sees nothing objectionable here")})
        for m in PANEL_MODELS]

    plan = {
        "plan_kind": "MIDTERM_EXECUTABLE_REVIEW_PLAN",
        "candidate_head_sha": HEAD, "candidate_base_sha": BASE,
        "engine_digest": "e" * 64, "policy_digest": "p" * 64,
        "final_units": [one],
        "batches": [{"batch_id": "batch-0"}],
        "review_request_policy": {"model_ids": list(PANEL_MODELS)},
        "operator_pin_record": {"pins": {"VERIFIER_MAX_GENERATION_CALLS": 3}},
        "execution_challenge": CHALLENGE,
        "review_skeleton_sha256": "5" * 64,
        "execution_request_hashes": ["r" * 64],
        "request_semantics_digest": "s" * 64,
        "write_separated": False, "trusted_evidence_claim": False,
        "human_merge_required": True,
        "provider_secret_scope": "repository",  # pragma: allowlist secret
    }
    plan["plan_sha256"] = digest_of(plan)

    count_record = build_evidence(
        evidence_class=COUNT_EVIDENCE_CLASS,
        repository_numeric_id=REPOSITORY_NUMERIC_ID, candidate_head_sha=HEAD,
        candidate_base_sha=BASE, engine_digest="e" * 64,
        policy_digest="p" * 64, run_id=7, run_attempt=1,
        body={"request_semantics_digest": "s" * 64,
              "plan_sha256": plan["plan_sha256"]})

    runner = tmp_path / "runner"
    base = runner / "midterm" / "count-input"
    base.mkdir(parents=True)
    write_atomic(count_record, str(base / "count-evidence.json"))
    (base / "executable-plan.json").write_text(
        json.dumps(plan), encoding="utf-8")

    environ = {"RUNNER_TEMP": str(runner), "GITHUB_TOKEN": "t",
               "CANDIDATE_HEAD_SHA": HEAD, "CANDIDATE_BASE_SHA": BASE,
               "CANDIDATE_PR_NUMBER": "46",
               "MIDTERM_ENGINE_DIGEST": "e" * 64,
               "MIDTERM_POLICY_DIGEST": "p" * 64,
               "MIDTERM_RUN_URL": RUN_URL,
               "GITHUB_RUN_ID": "7", "GITHUB_RUN_ATTEMPT": "1"}

    order, captured = [], {}

    def execute_fn(*, plan):
        return {"votes": votes, "coverage": {}, "generation_calls": 0,
                "normalization": {"normalized": False},
                "output_privacy": {"scanned_field_count": 6},
                "generation_ledger": {"generation_ledger_sha256": "g" * 64},
                "execution_preflight": {},
                "synthesis": {
                    "overall_approved": not refuted,
                    "refuted_unit_count": 1 if refuted else 0,
                    "approved_unit_count": 0 if refuted else 1,
                    "synthesis_sha256": "y" * 64}}

    def status_opener(request, timeout=None):
        return _Response(201, "{}")

    def publish_review_fn(*, body, pr_number, candidate_head_sha, token,
                          challenge):
        order.append("review")
        captured["body"] = body
        return {"published": publish_ok,
                "outcome": (reviewpublish.CREATED if publish_ok
                            else reviewpublish.REFUSED_API),
                "comment_id": 900, "http_status": 201 if publish_ok else None,
                "body_chars": len(body),
                "candidate_head_sha": candidate_head_sha,
                "refusal": None if publish_ok else "connection reset"}

    raised = None
    try:
        panelcli.perform(environ, execute_fn=execute_fn, opener=status_opener,
                         scan=scan, publish_review_fn=publish_review_fn)
    except PanelRefusal as exc:
        raised = exc
    order.append("exit")
    paths = panelcli.review_paths(str(runner))
    return {"raised": raised, "order": order,
            "published_body": captured.get("body"),
            "markdown": Path(paths["markdown"]), "json": Path(paths["json"]),
            "evidence": runner / "midterm" / "panel-evidence.json"}
