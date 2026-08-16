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

import hashlib
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



# ------------------------------------------------------------- helpers ------


def outside_spans(body: str) -> str:
    """The body with every fenced span blanked out.

    This is the surface where Markdown is LIVE. Untrusted text is published
    inside a code span, so the question worth asking is never "does this
    character appear" — it legitimately does, as literal text — but "does any of
    it appear out here, where GitHub would act on it"."""
    import re
    return re.sub(r"(`+)(?:(?!\1).)*?\1", " ", body, flags=re.DOTALL)


def fenced_only(body: str, *needles) -> None:
    """Every needle appears in the body, and NONE of it outside a fence."""
    live = outside_spans(body)
    for needle in needles:
        assert needle in body, f"{needle!r} was dropped entirely"
        assert needle not in live, f"{needle!r} reached live Markdown"


def rendered_html(body: str) -> str:
    """The body as GitHub would render it. Skipped where cmarkgfm is absent.

    The empirical backstop, and it has earned its place: it is what caught
    `&#64;` rendering a live `mailto:` anchor after three rounds of source-level
    assertions had passed over it. The structural checks above are what run
    everywhere; this is what proves they mean what they claim."""
    cmarkgfm = pytest.importorskip("cmarkgfm")
    from cmarkgfm.cmark import Options
    return cmarkgfm.github_flavored_markdown_to_html(
        body, options=Options.CMARK_OPT_UNSAFE)


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
        assert "app/thing.py ` lines 41-58**" in body
        fenced_only(body, "app/thing.py")

    def test_it_names_the_raising_model(self, body):
        assert "raised by `gpt-5.3-codex`" in body

    def test_it_carries_the_confidence(self, body):
        assert "confidence ` high `" in body

    def test_it_carries_the_checked_categories(self, body):
        assert "` logic `" in body and "` interface_contract `" in body

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
        assert "scripts/verifier/executor.py ` lines 802-833**" in body
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
        assert "app/alpha.py ` lines 1-4**" in body
        assert "app/alpha.py ` lines 90-96**" in body
        assert "zz/omega.py ` line 7**" in body

    def test_a_single_line_range_is_rendered_as_a_line_not_a_range(self, body):
        assert "zz/omega.py lines 7-7" not in body

    def test_the_order_is_path_then_line(self, body):
        positions = [body.index("app/alpha.py ` lines 1-4"),
                     body.index("app/alpha.py ` lines 90-96"),
                     body.index("zz/omega.py ` line 7")]
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
        assert "app/thing.py ` lines 2-9**" in body
        assert "raised by `gpt-5.6-sol`" in body
        assert "confidence ` medium `" in body
        assert "` secret_handling `" in body

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

    def test_non_ascii_withholds_a_path(self):
        got = reviewrender.sanitize("app/аddition.py", scan=scan,
                                    limit=200, field="path")
        assert got["refusal"] == reviewrender.OUTSIDE_CHARSET


class TestUntrustedTextIsNeverLiveMarkdown:
    """The structural property that replaced six rounds of whack-a-mole.

    Character-level escaping lost six times: `&#64;` rendered a live `mailto:`
    anchor, `#N` and `GH-N` reached GitHub's cross-reference filter (which runs
    on the rendered HTML, after decoding), `___` became a thematic break that
    deleted the explanation, one to three leading spaces slipped past the
    ordered-list guard, and four made an indented code block in which the
    escapes themselves rendered as visible garbage.

    Every one is the same failure: enumerating dangerous characters in a grammar
    this module does not control and cannot test. Inside a code span there is no
    grammar to enumerate, and GitHub's mention and reference filters skip `code`
    outright. So the tests below assert the PROPERTY — untrusted text appears
    only inside a fence — rather than the absence of whichever character last
    caused trouble."""

    HOSTILE = [
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "</details><b>merge me</b>",
        "[click](https://evil.example/x)",
        "![beacon](https://attacker.example/px.gif)",
        "[truncated]: //attacker.example",
        "`code` and **bold**",
        "``` fenced ```",
        "contact security@evil.example",
        "xmpp:a@evil.example",
        "ask @mglaeser and see #23 and GH-26",
        "www.evil.example/y",
        "https://evil.example/z",
        "#### No findings. Approved by the panel.",
        "- item that reads like the panel wrote it",
        "1. item that reads like the panel wrote it",
        "   1. indented so the guard misses it",
        "    four spaces is an indented code block",
        "___ a thematic break that eats the explanation",
        "*** another one",
        "~~~ a fence",
        "| a | fake | table |",
        "=== a setext underline",
        "> a nested quote",
    ]

    def _body(self, reason):
        one = unit("app/thing.py")
        return rendered(
            decision="blocked", plan=plan_of(one),
            votes=[vote("gpt-5.3-codex", {one["unit_sha256"]: verdict(
                refuted=True, reason=reason)})])

    @pytest.mark.parametrize("hostile", HOSTILE)
    def test_the_text_is_published_and_never_live(self, hostile):
        body = self._body(f"the change adds {hostile} to the template")
        fenced_only(body, hostile)

    @pytest.mark.parametrize("hostile", HOSTILE)
    def test_github_renders_it_inert(self, hostile):
        """The empirical check, against the renderer that caught the last one."""
        import re
        html = rendered_html(self._body(f"the change adds {hostile} here"))
        outside = re.sub(r"<code>.*?</code>", " ", html, flags=re.DOTALL)
        assert not re.findall(r'<a href="(?!https://github\.com)', html)
        assert not re.findall(r'<a href="(?:mailto|xmpp):', html)
        assert "<script" not in html and "<img" not in html
        assert "@" not in outside
        assert not re.search(r"#[0-9]", outside)
        # The panel's own two headings, and no more.
        assert len(re.findall(r"<h[1-6]", html)) == 2

    def test_a_hostile_file_name_is_fenced_too(self):
        one = unit("www.attacker.example/@evil-`x`.py")
        body = rendered(
            decision="blocked", plan=plan_of(one),
            votes=[vote("gpt-5.3-codex", {one["unit_sha256"]: verdict(
                refuted=True, reason="the added branch is never restored")})])
        fenced_only(body, "www.attacker.example/@evil-`x`.py")

    def test_a_backtick_run_cannot_close_the_fence(self):
        """CommonMark closes a span on a run of EXACTLY the opening length, so
        the fence is one longer than the longest run inside."""
        for content in ["a`b", "a``b", "a```b", "```", "`" * 12]:
            span = reviewrender.code_span(content)
            fence = span[:len(span) - len(span.lstrip("`"))]
            assert content in span
            assert fence + fence not in span or content.count(fence) == 0
            assert len(fence) > max(
                (len(r) for r in __import__("re").findall(r"`+", content)),
                default=0)

    def test_the_modules_own_markers_cannot_become_link_labels(self):
        """`[redacted]` and the truncation mark are spliced into untrusted text,
        so they used to supply a link label an attacker only had to follow with
        a destination. Inside a fence a bracket is a bracket."""
        got = reviewrender.sanitize("x" * 300 + "(//attacker.example)",
                                    scan=scan, limit=80, field="reason")
        assert got["truncated"] is True
        body = self._body("(//attacker.example) follows the token")
        fenced_only(body, "(//attacker.example)")

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
        # The real name reaches the reader, and none of it is live. Asserting
        # the ABSENCE of characters was the old shape and it was wrong twice:
        # the name is legitimately present as literal text inside a fence.
        fenced_only(body, hostile)

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
        assert got["published_chars"] <= 120

    def test_the_bound_counts_source_characters(self):
        """Escaping is gone — untrusted text is published raw inside a fence —
        so the published length is the source length plus the fence. The BODY
        bound, which `render` applies to each finished block, is what keeps the
        document inside the limit."""
        got = reviewrender.sanitize("<" * 400, scan=scan, limit=120,
                                    field="reason")
        assert got["published_chars"] <= 120
        assert got["text"] == "<" * (120 - len(reviewrender.TRUNCATION_MARK)) \
            + reviewrender.TRUNCATION_MARK
        assert got["code_span"].startswith("` ")

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
        assert "app/thing.py ` lines 10-14**" in outcome["published_body"]
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


class TestAStatusFaultDoesNotSwallowTheFinding:
    """`status.publish` refuses on any HTTP fault, and that refusal propagates.

    With the review published after it, a single GitHub hiccup on the terminal
    status POST would have aborted the run with the finding never rendered,
    retained or shown — requirement 9 defeated by the machine channel being the
    thing that failed. So the review goes out first."""

    def test_the_review_is_published_even_when_the_status_post_fails(
            self, tmp_path):
        outcome = _run_perform(tmp_path, decision="blocked",
                               terminal_status_fails=True)
        assert isinstance(outcome["raised"], PanelRefusal)
        assert "status_publish" in outcome["raised"].reason
        assert outcome["published_body"] is not None
        assert "raised by `gpt-5.6-sol`" in outcome["published_body"]
        assert outcome["markdown"].exists()

    def test_the_pending_status_is_still_published_first(self, tmp_path):
        outcome = _run_perform(tmp_path, decision="blocked",
                               terminal_status_fails=True)
        assert outcome["status_states"][0] == "pending"


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
        # Published raw inside a fence, so the reader sees the sentence exactly
        # as the model wrote it — hyphen and all — and none of it is live.
        fenced_only(body, "the handler drops the fail-closed default")
        # `[redacted]` is this module's own marker. It used to be a ready-made
        # link label: `<challenge>(//attacker.example)` became a live
        # `[redacted](//attacker.example)` in a bot-authored comment. Inside a
        # fence a bracket is a bracket.
        fenced_only(body, reviewrender.REDACTED)

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

    def test_a_block_that_LOST_a_refutation_is_refused(self):
        """The precise property: the rendering may not DELETE a finding.

        Not "a block must always have findings" — the engine's role gate blocks
        with no `refuted: true` verdict anywhere, and refusing that case is what
        made the publisher silently publish nothing at all."""
        with pytest.raises(PanelRefusal) as raised:
            reviewrender.assert_rendering_did_not_change_the_decision(
                findings=[], decision="blocked",
                aggregate_record={"decision": "blocked"},
                refutations_exist=True)
        assert "block_lost_its_refutation" in raised.value.reason

    def test_an_approval_over_a_refutation_is_refused(self):
        with pytest.raises(PanelRefusal) as raised:
            reviewrender.assert_rendering_did_not_change_the_decision(
                findings=[], decision="approved",
                aggregate_record={"decision": "approved"},
                refutations_exist=True)
        assert "approval_over_a_refutation" in raised.value.reason

    def test_a_rendered_decision_disagreeing_with_the_aggregate_is_refused(self):
        with pytest.raises(PanelRefusal) as raised:
            reviewrender.assert_rendering_did_not_change_the_decision(
                findings=[], decision="approved",
                aggregate_record={"decision": "blocked"},
                refutations_exist=False)
        assert "disagrees_with_aggregate" in raised.value.reason

    def test_a_publication_failure_does_not_change_the_outcome(self, tmp_path):
        outcome = _run_perform(tmp_path, decision="blocked", publish_ok=False)
        assert isinstance(outcome["raised"], PanelRefusal)
        assert "category=panel_blocked" in outcome["raised"].reason
        stored = json.loads(outcome["json"].read_text(encoding="utf-8"))
        assert stored["review"]["decision"] == "blocked"


class TestARoleGateBlockIsStillReadable:
    """The worst of the second review round, and the one that hid inside a
    check meant to prevent exactly it.

    The engine's role gate blocks a unit when the required approver has no
    valid vote, when too few distinct models corroborate, or when two approvals
    are near-identical — and in every one of those the per-model verdicts
    contain NO `refuted: true` at all. The first version refused any blocked
    review with an empty findings list, `publish_readable_review` caught that
    refusal, and the result was a blocked panel that published and retained
    nothing. A blocked review with no readable output is precisely the defect
    this publisher exists to remove, reached from the inside."""

    @pytest.fixture
    def body(self):
        one = unit("app/thing.py")
        record = {**aggregate_of("blocked"),
                  "engine_gate": {
                      "block": True,
                      "reason": "the engine's synthesis refuted 1 unit(s)",
                      "refuted_unit_count": 1, "approved_unit_count": 0},
                  "strict_gate": {"block": False,
                                  "reason": "strict mode: no model refuted any "
                                            "unit",
                                  "refuting_models": []}}
        review = reviewrender.build_review(
            decision="blocked", candidate_head_sha=HEAD,
            candidate_base_sha=BASE, plan=plan_of(one),
            # Every model APPROVED. The block came from the role gate.
            votes=[vote(model, {one["unit_sha256"]: verdict(
                refuted=False, reason=f"{model} sees nothing wrong here")})
                for model in PANEL_MODELS],
            aggregate_record=record, scan=scan, run_url=RUN_URL, run_id=7,
            evidence_sha256="e" * 64, challenge=CHALLENGE)
        return reviewrender.render(review, challenge=CHALLENGE)

    def test_it_publishes_rather_than_refusing(self, body):
        assert "**blocked**" in body

    def test_it_never_claims_there_were_no_findings(self, body):
        """`No actionable findings were reported` beside `Decision: blocked`
        reads as a malfunction and invites the override this exists to
        prevent."""
        assert reviewrender.NO_FINDINGS not in body

    def test_it_says_why_it_blocked_in_the_aggregates_own_words(self, body):
        assert "#### Why this blocked" in body
        assert "engine gate" in body
        assert "refuted 1 unit" in body

    def test_a_gate_that_did_not_block_is_not_quoted(self, body):
        assert "no model refuted any unit" not in body

    def test_a_block_naming_no_gate_still_says_something(self):
        one = unit("app/thing.py")
        review = reviewrender.build_review(
            decision="blocked", candidate_head_sha=HEAD,
            candidate_base_sha=BASE, plan=plan_of(one),
            votes=[vote(m, {one["unit_sha256"]: verdict(
                refuted=False, reason=f"{m} sees nothing wrong here")})
                for m in PANEL_MODELS],
            aggregate_record=aggregate_of("blocked"), scan=scan,
            run_url=RUN_URL, run_id=7, evidence_sha256="e" * 64)
        body = reviewrender.render(review)
        assert "named no gate" in body
        assert reviewrender.NO_FINDINGS not in body


class TestTheChallengeSurvivesNeitherCaseNorSplitting:
    """The engine's own scanner deliberately SKIPS a 32-hex token — it reads as
    a content digest, which is the correct default — so redaction is the only
    thing between the execution challenge and the comment. An exact
    `str.replace` was not enough."""

    #: A synthetic 32-hex string in the exact shape of an execution challenge
    #: (`trustedlane.challenge.TOKEN_HEX`). Built at run time from a fixed
    #: seed rather than written as a literal: the repository's own secret gate
    #: correctly flags a hex run of this length, and a fixture whose only job
    #: is to look like a run token should not need the ratchet's permission to
    #: exist. Deterministic, so the assertions below are stable.
    TOKEN = hashlib.sha256(b"readable-review-fixture-challenge").hexdigest()[:32]

    def _reason_body(self, reason):
        one = unit("app/thing.py")
        review = reviewrender.build_review(
            decision="blocked", candidate_head_sha=HEAD,
            candidate_base_sha=BASE, plan=plan_of(one),
            votes=[vote("gpt-5.6-sol", {one["unit_sha256"]: verdict(
                refuted=True, reason=reason)})],
            aggregate_record=aggregate_of("blocked"), scan=scan,
            run_url=RUN_URL, run_id=7, evidence_sha256="e" * 64,
            challenge=self.TOKEN)
        return reviewrender.render(review, challenge=self.TOKEN)

    def test_an_exact_echo_is_redacted(self):
        body = self._reason_body(
            f"{self.TOKEN} the fail closed default is dropped")
        assert self.TOKEN not in body
        assert reviewrender.REDACTED in body

    def test_a_shouted_echo_is_redacted_too(self):
        body = self._reason_body(
            f"{self.TOKEN.upper()} the fail closed default is dropped")
        assert self.TOKEN.upper() not in body
        assert self.TOKEN not in body.lower()

    @pytest.mark.parametrize("shape", [
        lambda t: t[:20],
        lambda t: " ".join(t[i:i + 8] for i in range(0, 32, 8)),
        lambda t: " ".join(t[i:i + 4] for i in range(0, 32, 4)),
        lambda t: "-".join(t[i:i + 4] for i in range(0, 32, 4)),
        lambda t: "".join(chr(ord(c) + 0xFEE0) if c.isalnum() else c for c in t),
    ])
    def test_a_reshaped_echo_withholds_the_field(self, shape):
        """Truncated, split, hyphenated, or written in fullwidth digits.

        None of those is a substring of the token and every one is a complete
        disclosure of it. The old rule — exact match plus a sweep for hex runs
        of sixteen or more — published three of these five, and the charset
        split that lets prose keep its em dashes is what made the fullwidth form
        reachable. The comparison folds NFKC, drops every non-alphanumeric and
        casefolds before looking."""
        reshaped = shape(self.TOKEN)
        body = self._reason_body(
            f"the run token began {reshaped} and the default is dropped")
        folded = reviewrender._fold_for_token_match
        assert folded(self.TOKEN) not in folded(body)
        assert reviewrender.WITHHELD in body
        assert "| Actionable findings | 1 |" in body

    def test_the_field_records_which_rule_withheld_it(self):
        got = reviewrender.sanitize(f"leaked {self.TOKEN[:24]} here", scan=scan,
                                    limit=600, field="reason",
                                    redact=(self.TOKEN,),
                                    charset=reviewrender.CHARSET_TEXT)
        assert got["refusal"] == reviewrender.CARRIES_RUN_TOKEN

    def test_the_body_gate_catches_a_shouted_challenge(self):
        with pytest.raises(PanelRefusal) as raised:
            reviewrender.assert_publishable(
                f"{reviewrender.MARKER}\ntoken {self.TOKEN.upper()}",
                challenge=self.TOKEN)
        assert "execution_challenge" in raised.value.reason

    @pytest.mark.parametrize("shape", [
        lambda t: t[:24],
        lambda t: t.upper(),
        lambda t: " ".join(t[i:i + 4] for i in range(0, 32, 4)),
        lambda t: "".join(chr(ord(c) + 0xFEE0) if c.isalnum() else c for c in t),
    ])
    def test_the_body_gate_catches_every_reshaping(self, shape):
        with pytest.raises(PanelRefusal) as raised:
            reviewrender.assert_publishable(
                f"{reviewrender.MARKER}\ntoken {shape(self.TOKEN)}",
                challenge=self.TOKEN)
        assert "execution_challenge" in raised.value.reason

    def test_the_window_is_half_the_token(self):
        assert reviewrender.MIN_DISCLOSED_TOKEN_CHARS == 16
        assert reviewrender.discloses_token(self.TOKEN[:16], self.TOKEN)
        assert not reviewrender.discloses_token(self.TOKEN[:15], self.TOKEN)

    def test_an_unrelated_digest_is_not_mistaken_for_the_challenge(self):
        """A 64-hex content digest is ordinary in this codebase's prose and
        must not trip the sweep."""
        reviewrender.assert_publishable(
            f"{reviewrender.MARKER}\nevidence " + "b" * 64,
            challenge=self.TOKEN)


class TestNoRawPathIsRetainedInThePrivateArtifact:
    """The ordering key carries the RAW candidate path — unsanitized and
    unbounded, because ordering has to compare the real thing. It had done its
    job and was still being serialized into `panel-review.json`, which is what
    `MAX_PATH_CHARS` exists to prevent."""

    def test_the_sort_key_does_not_survive_into_the_review(self):
        long_path = "app/" + ("x" * 400) + ".py"
        one = unit(long_path)
        review = build(
            decision="blocked", plan=plan_of(one),
            votes=[vote("gpt-5.6-sol", {one["unit_sha256"]: verdict(
                refuted=True, reason="the fail closed default is dropped")})])
        serialized = json.dumps(review)
        assert "sort_key" not in serialized
        assert "x" * 300 not in serialized

    def test_the_ordering_still_works_without_it(self):
        first = unit("app/alpha.py", lines=(1, 4), tag="1")
        second = unit("app/zeta.py", lines=(1, 4), tag="2")
        body = rendered(
            decision="blocked", plan=plan_of(second, first),
            votes=[vote("gpt-4.1-mini", {
                u["unit_sha256"]: verdict(
                    refuted=True, reason=f"unit at {u['new_line_range']} fails "
                                         "its stated invariant")
                for u in (first, second)})])
        assert body.index("app/alpha.py") < body.index("app/zeta.py")


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


def _run_perform(tmp_path, *, decision: str, publish_ok: bool = True,
                 terminal_status_fails: bool = False) -> dict:
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

    states = []

    def status_opener(request, timeout=None):
        body = json.loads(request.data.decode("utf-8"))
        states.append(body["state"])
        if terminal_status_fails and body["state"] != "pending":
            raise OSError("connection reset by peer")
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
    return {"raised": raised, "order": order, "status_states": states,
            "published_body": captured.get("body"),
            "markdown": Path(paths["markdown"]), "json": Path(paths["json"]),
            "evidence": runner / "midterm" / "panel-evidence.json"}


# ------------------------------------------- round three: the last sweep ----


class TestTheHeadlineNeverClaimsWhatTheBodyDenies:
    """The loudest line in the document asserted "at least one governed model
    refuted a change" in exactly the case where none had — the role-gate block,
    which is also the case where the findings list is empty. A reader scanning
    the heading and the count would have seen the two contradict each other."""

    def _blocked_without_refutation(self):
        one = unit("app/thing.py")
        record = {**aggregate_of("blocked"),
                  "engine_gate": {"block": True,
                                  "reason": "the required approver has no valid "
                                            "vote on 1 unit(s)",
                                  "refuted_unit_count": 0},
                  "strict_gate": {"block": False, "reason": "x",
                                  "refuting_models": []}}
        review = reviewrender.build_review(
            decision="blocked", candidate_head_sha=HEAD, candidate_base_sha=BASE,
            plan=plan_of(one),
            votes=[vote(m, {one["unit_sha256"]: verdict(
                refuted=False, reason=f"{m} sees nothing wrong here")})
                for m in PANEL_MODELS],
            aggregate_record=record, scan=scan, run_url=RUN_URL, run_id=7,
            evidence_sha256="e" * 64)
        return reviewrender.render(review)

    def test_a_role_gate_block_does_not_claim_a_refutation(self):
        body = self._blocked_without_refutation()
        headline = next(line for line in body.splitlines()
                        if line.startswith("### Mid-term panel review:"))
        assert "refuted" not in headline
        assert "role and corroboration gates were not met" in headline
        # And the body says plainly that nobody refuted, which is the fact the
        # headline used to contradict.
        assert "No governed model refuted a change." in body

    def test_a_real_refutation_still_says_so(self):
        one = unit("app/thing.py")
        body = rendered(
            decision="blocked", plan=plan_of(one),
            votes=[vote("gpt-5.6-sol", {one["unit_sha256"]: verdict(
                refuted=True, reason="the fail closed default is dropped")})])
        assert "blocked — a governed model refuted a change" in body


class TestProseKeepsItsPunctuationAndPathsDoNot:
    """The charset split is about STAKES.

    A path is ACTED ON — a reader follows it to a file, and a homoglyph sends
    them to the wrong one. Prose is READ. Refusing an em dash in a refutation
    replaced the single most important sentence in the document with the
    withheld notice, for a confusable that misleads nobody."""

    def test_a_refutation_may_use_ordinary_punctuation(self):
        one = unit("app/thing.py")
        body = rendered(
            decision="blocked", plan=plan_of(one),
            votes=[vote("gpt-5.6-sol", {one["unit_sha256"]: verdict(
                refuted=True,
                reason="the handler — added in this change — drops "
                       "the “fail closed” default")})])
        assert reviewrender.WITHHELD not in body
        assert "—" in body and "“fail closed”" in body

    def test_an_invisible_character_still_withholds_prose(self):
        got = reviewrender.sanitize("before‮after", scan=scan, limit=600,
                                    field="reason",
                                    charset=reviewrender.CHARSET_TEXT)
        assert got["refusal"] == reviewrender.OUTSIDE_CHARSET

    def test_a_zero_width_joiner_still_withholds_prose(self):
        got = reviewrender.sanitize("a‍b", scan=scan, limit=600,
                                    field="reason",
                                    charset=reviewrender.CHARSET_TEXT)
        assert got["refusal"] == reviewrender.OUTSIDE_CHARSET

    def test_a_path_stays_ascii_only(self):
        got = reviewrender.sanitize("app/аdmin.py", scan=scan, limit=200,
                                    field="path")
        assert got["refusal"] == reviewrender.OUTSIDE_CHARSET

    def test_a_homoglyph_path_withholds_the_location_not_the_finding(self):
        one = unit("app/аdmin.py")
        body = rendered(
            decision="blocked", plan=plan_of(one),
            votes=[vote("gpt-5.6-sol", {one["unit_sha256"]: verdict(
                refuted=True, reason="the fail closed default is dropped")})])
        assert "path withheld by output-privacy policy" in body
        assert "| Actionable findings | 1 |" in body


class TestADeletionOnlyUnitSaysWhichSideItsLinesAreOn:
    """`new_line_range` is None for a deletion-only unit, and the old numbers
    were rendered unlabelled — sending a reader to those lines in the file as it
    is NOW, which is a different place entirely, with the panel behind it."""

    def test_old_side_numbers_are_labelled_old(self):
        one = unit("app/thing.py")
        one["new_line_range"] = None
        one["old_line_range"] = [41, 58]
        body = rendered(
            decision="blocked", plan=plan_of(one),
            votes=[vote("gpt-5.3-codex", {one["unit_sha256"]: verdict(
                refuted=True, reason="the removed guard was load bearing")})])
        assert "app/thing.py ` old lines 41-58**" in body

    def test_new_side_numbers_are_not_relabelled(self):
        body = rendered(
            decision="blocked", plan=plan_of(unit("app/thing.py", lines=(1, 2))),
            votes=[vote("gpt-5.3-codex", {
                unit("app/thing.py", lines=(1, 2))["unit_sha256"]: verdict(
                    refuted=True, reason="the added branch is not restored")})])
        assert "old lines" not in body
        assert "app/thing.py ` lines 1-2**" in body


class TestARedIsNeverRoutedIntoTheRetryPath:
    """The ordering inside both gates, as a property.

    With "absent" and "still running" checked first, a check that had FINISHED
    AND FAILED was masked by a sibling that had not arrived yet: the run polled
    for thirty seconds and refused under a category naming the wrong problem."""

    def test_a_failed_check_beats_an_absent_sibling(self):
        from midtermpanel import checkruns, observation
        runs = [{"name": "test (3.12)", "status": "completed",
                 "conclusion": "failure", "completed_at": "2026-08-15T22:00:00Z",
                 "head_sha": HEAD, "id": 1}]
        with pytest.raises(PanelRefusal) as caught:
            checkruns.assert_contexts_are_green(
                runs, head_sha=HEAD, contexts=("test (3.12)", "image"),
                where="t")
        assert "check_not_successful" in caught.value.reason
        assert not observation.is_retryable(caught.value)

    def test_a_failed_job_beats_an_incomplete_sibling(self):
        from midtermpanel import observation, preflight
        jobs = [{"name": "test (3.12)", "status": "completed",
                 "conclusion": "failure"},
                {"name": "image", "status": "in_progress", "conclusion": None}]
        with pytest.raises(PanelRefusal) as caught:
            preflight.assert_triggering_ci_jobs_are_green(jobs, run_id=1)
        assert "triggering_run_job_not_successful" in caught.value.reason
        assert not observation.is_retryable(caught.value)

    def test_a_completed_check_with_no_conclusion_is_waited_on(self):
        from midtermpanel import checkruns, observation
        runs = [{"name": name, "status": "completed", "conclusion": None,
                 "completed_at": "2026-08-15T22:00:00Z", "head_sha": HEAD,
                 "id": index + 1}
                for index, name in enumerate(("test (3.12)", "image"))]
        with pytest.raises(PanelRefusal) as caught:
            checkruns.assert_contexts_are_green(
                runs, head_sha=HEAD, contexts=("test (3.12)", "image"),
                where="t")
        assert "check_conclusion_not_written" in caught.value.reason
        assert observation.is_retryable(caught.value)

    def test_a_completed_job_with_no_conclusion_is_waited_on(self):
        from midtermpanel import observation, preflight
        jobs = [{"name": "test (3.12)", "status": "completed",
                 "conclusion": None},
                {"name": "image", "status": "completed", "conclusion": "success"}]
        with pytest.raises(PanelRefusal) as caught:
            preflight.assert_triggering_ci_jobs_are_green(jobs, run_id=1)
        assert "triggering_run_job_conclusion_not_written" in caught.value.reason
        assert observation.is_retryable(caught.value)


class TestAFloodedThreadDegradesToNoiseNotSilence:
    """`parse_api_json` caps a response at 2 MiB and a GitHub comment may be
    65 536 CHARACTERS — 256 KiB in astral-plane UTF-8 — so eight legal comments
    exceed the cap and no page size fixes it. With the refusal propagating,
    anyone who could comment could make every future review vanish."""

    def test_an_unreadable_comment_list_still_publishes(self):
        class Flooded(Recorder):
            def __call__(self, request, timeout=None):
                if request.get_method() == "GET" and "/pulls/" not in \
                        request.full_url:
                    raise OSError("body too large")
                return super().__call__(request, timeout)

        opener = Flooded()
        got = reviewpublish.publish_review(
            body=a_body(), pr_number=46, candidate_head_sha=HEAD, token="t",  # noqa: S106
            opener=opener)
        assert got["outcome"] == reviewpublish.CREATED
        assert got["published"] is True
        assert got["lookup_degraded"] is True

    def test_a_readable_list_is_not_marked_degraded(self):
        opener = Recorder()
        got = reviewpublish.publish_review(
            body=a_body(), pr_number=46, candidate_head_sha=HEAD, token="t",  # noqa: S106
            opener=opener)
        assert got["lookup_degraded"] is False


class TestTheScalarPermissionShorthandIsRefusedByName:
    """`permissions: write-all` is the single most dangerous value that could
    appear in a job block, and it was the one shape that produced an
    AttributeError instead of a refusal."""

    @pytest.mark.parametrize("shorthand", ["write-all", "read-all", []])
    def test_it_refuses_rather_than_crashing(self, shorthand):
        from midtermpanel import privilegedworkflow
        document = {"permissions": dict(privilegedworkflow.REQUIRED_PERMISSIONS),
                    "jobs": {"panel": {"permissions": shorthand}}}
        with pytest.raises(PanelRefusal) as caught:
            privilegedworkflow.assert_permissions(document)
        assert "permissions_not_a_mapping" in caught.value.reason


# ------------------------------- round four: the frozen-head confirmation ----


class TestTheChallengeSurvivesNoUnicodeReshaping:
    """NFKC COMPOSES, and that was a live bypass.

    `a` followed by a combining acute becomes `á` under NFKC — which is not
    `a` — so a challenge written with a combining mark on every character
    folded to nothing recoverable and was published in full. NFKD decomposes it
    back, and dropping every non-alphanumeric removes the mark along with the
    spaces, hyphens and zero-width characters the other reshapings use."""

    import hashlib as _hashlib
    TOKEN = _hashlib.sha256(b"frozen-head-fixture").hexdigest()[:32]

    @pytest.mark.parametrize("shape,label", [
        (lambda t: "".join(c + "́" for c in t), "combining acute on each"),
        (lambda t: "".join(c + "‍" for c in t), "zero-width joiner between"),
        (lambda t: "".join(chr(ord(c) + 0xFEE0) if c.isalnum() else c for c in t),
         "fullwidth"),
        (lambda t: " ".join(t[i:i + 2] for i in range(0, 32, 2)), "split in pairs"),
        (lambda t: ".".join(t[i:i + 4] for i in range(0, 32, 4)), "dotted"),
        (lambda t: t[8:], "tail only"),
    ])
    def test_no_reshaping_reaches_the_reader(self, shape, label):
        got = reviewrender.sanitize(
            f"the run token was {shape(self.TOKEN)} here", scan=scan, limit=600,
            field="reason", redact=(self.TOKEN,),
            charset=reviewrender.CHARSET_TEXT)
        assert got["published"] is False, label
        assert got["refusal"] == reviewrender.CARRIES_RUN_TOKEN, label

    def test_the_fold_decomposes_rather_than_composing(self):
        marked = "á"
        assert reviewrender._fold_for_token_match(marked) == "a"


class TestTheOneLiveLinkTargetIsPinned:
    """`[{run_id}]({run_url})` is the single place in the body where a string
    becomes a live target. Everything else untrusted is inside a code span,
    where a destination cannot exist — so this one is pinned by pattern rather
    than sanitized, and it arrives from the environment."""

    def _build(self, url):
        one = unit("app/thing.py")
        return reviewrender.build_review(
            decision="approved", candidate_head_sha=HEAD, candidate_base_sha=BASE,
            plan=plan_of(one),
            votes=[vote(m, {one["unit_sha256"]: verdict(
                refuted=False, reason=f"{m} sees nothing objectionable")})
                for m in PANEL_MODELS],
            aggregate_record=aggregate_of("approved"), scan=scan, run_url=url,
            run_id=7, evidence_sha256="e" * 64)

    @pytest.mark.parametrize("hostile", [
        "https://evil.example/x",
        "javascript:alert(1)",
        "https://github.com.evil.example/mglaeser/bubble-regime-monitor/actions/runs/7",
        "https://github.com/other/repo/actions/runs/7",
        "https://github.com/mglaeser/bubble-regime-monitor/settings",
        "",
        None,
    ])
    def test_anything_but_an_actions_run_here_is_refused(self, hostile):
        with pytest.raises(PanelRefusal) as raised:
            self._build(hostile)
        assert "run_url_not_an_actions_run" in raised.value.reason

    def test_the_real_run_url_is_accepted(self):
        review = self._build(RUN_URL)
        assert review["run_url"] == RUN_URL


class TestTheDeadEscaperIsGone:
    """It lost six times and then stopped being called at all.

    A dead function that looks like the defence is how a later edit routes text
    back through it believing it is protected — the same shape as the two
    self-test guards that reported properties they never checked."""

    def test_the_module_no_longer_exports_it(self):
        assert not hasattr(reviewrender, "escape_markup")
        assert "escape_markup" not in reviewrender.__all__

    def test_there_is_exactly_one_defence_and_it_is_the_fence(self):
        source = (ROOT / "scripts" / "midtermpanel"
                  / "reviewrender.py").read_text(encoding="utf-8")
        assert "def code_span(" in source
        assert "def escape_markup(" not in source
