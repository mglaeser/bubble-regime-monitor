"""The mid-term panel: its privileged surface, its choke points, its seam.

The mutation tests are the load-bearing ones. A validator that passes the real
workflow proves only that today's file is acceptable; it says nothing about
whether the validator would notice tomorrow's edit. So each forbidden shape is
introduced INTO THE REAL FILE, in memory, and the refusal is asserted — which is
the difference between "the check ran" and "the check works".

`scripts/` goes on the path the way every other suite here does it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import midtermpanel as mp  # noqa: E402
from midtermpanel import privilegedworkflow, statuspublish, transport  # noqa: E402
from midtermpanel.errors import PanelRefusal  # noqa: E402
from trustedlane import statusnames  # noqa: E402
from trustedlane.errors import LaneRefusal  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / mp.WORKFLOW_FILENAME
POLICY = ROOT / "governance" / "midterm-panel-policy.json"
# A git commit id, not a credential. A bare 40-hex literal is
# indistinguishable from a token to an entropy detector, which is the correct
# default; the pragma states what it is.
HEAD = "c8ba2a727d46347904ed072422a11ab68c5b2e74"  # pragma: allowlist secret
DIGEST = "e" * 64


def _document() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _on(document: dict) -> dict:
    """The trigger block, surviving YAML 1.1 folding `on:` into the boolean True.

    The test suite walked into the exact trap the modules it tests document:
    `document["on"]` raises on every real workflow file, so a mutation written
    that way never lands and the test fails for the wrong reason. Resolved the
    same way the validator resolves it, so both agree about where triggers are."""
    for key in ("on", True):
        if key in document:
            return document[key]
    raise AssertionError("workflow has no on-block")


def _validate(document: dict, tmp_path):
    """Run the real validator over a mutated copy, from a real directory.

    Written out to disk rather than passed as an object because `validate()`
    reads the file — and a test that bypassed the read would be testing a
    different function from the one CI runs."""
    live = tmp_path / ".github" / "workflows"
    live.mkdir(parents=True, exist_ok=True)
    (live / mp.WORKFLOW_FILENAME).write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return privilegedworkflow.validate(root=str(tmp_path))


class TestTheRealWorkflowSatisfiesItsOwnPolicy:

    def test_the_committed_workflow_validates(self):
        record = privilegedworkflow.validate(root=str(ROOT))
        assert record["name"] == mp.WORKFLOW_NAME
        assert record["tree_materialising_steps"] == 0
        assert record["candidate_execution_steps"] == 0
        assert record["artifact_or_cache_steps"] == 0

    def test_only_count_and_panel_hold_the_secret(self):
        record = privilegedworkflow.validate(root=str(ROOT))
        assert record["secret_bearing_jobs"] == ["count", "panel"]
        assert record["named_secrets"] == [mp.SECRET_NAME]

    def test_preflight_and_finalize_never_see_a_credential(self):
        """The two jobs that decide and that always run.

        preflight decides whether to proceed; finalize runs on every failure
        path. A credential in scope while doing either is a credential in scope
        during the code paths least likely to have been exercised."""
        document = _document()
        for job in ("preflight", "finalize"):
            rendered = yaml.safe_dump(document["jobs"][job])
            assert "secrets." not in rendered, job

    def test_the_workflow_is_not_pr_controlled(self):
        """The whole architecture rests on this one fact."""
        from trustedlane import livepolicy
        assert livepolicy.is_pr_controlled(_document()) is False


class TestTheCandidateTreeIsNeverMaterialised:
    """Each mutation is a real way this has been broken in real workflows."""

    def test_a_checkout_ref_is_refused(self, tmp_path):
        document = _document()
        document["jobs"]["count"]["steps"][0]["with"]["ref"] = (
            "${{ github.event.workflow_run.head_sha }}")
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "privileged_checkout_names_a_ref" in caught.value.reason

    def test_even_a_literal_checkout_ref_is_refused(self, tmp_path):
        """No expression, still refused.

        The rule is the SHAPE, not the value. A literal is exactly what the next
        laundering step would resolve to, and a policy that permitted literals
        would be one `env:` indirection away from permitting everything."""
        document = _document()
        document["jobs"]["count"]["steps"][0]["with"]["ref"] = "main"
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "privileged_checkout_names_a_ref" in caught.value.reason

    @pytest.mark.parametrize("command", [
        "git checkout FETCH_HEAD",
        "git merge $CANDIDATE_HEAD_SHA",
        "git apply /tmp/candidate.patch",
        "gh run download 123",
        "git worktree add /tmp/c $SHA",
    ])
    def test_shell_that_produces_a_worktree_is_refused(self, command, tmp_path):
        """A checkout spelled in shell is still a checkout."""
        document = _document()
        document["jobs"]["count"]["steps"].append(
            {"name": "sneak", "run": command})
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "materialises_a_tree" in caught.value.reason

    def test_git_fetch_stays_allowed(self, tmp_path):
        """The object/worktree distinction IS the safety argument.

        If fetching were refused too, the panel could not read the candidate at
        all and the policy would have banned the architecture rather than the
        hazard."""
        document = _document()
        document["jobs"]["count"]["steps"].append(
            {"name": "fetch as inert objects",
             "run": "git fetch --no-checkout origin $CANDIDATE_HEAD_SHA"})
        assert _validate(document, tmp_path)["tree_materialising_steps"] == 0

    @pytest.mark.parametrize("action", [
        "actions/download-artifact@634f93cb2916e3fdff6788551b99b062d0335ce0",
        "actions/cache@abcabcabcabcabcabcabcabcabcabcabcabcabca",
    ])
    def test_consuming_the_triggering_runs_output_is_refused(self, action, tmp_path):
        """A zip is a tree; a restored cache is a tree someone else chose."""
        document = _document()
        document["jobs"]["panel"]["steps"].append({"uses": action})
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "consumes_candidate_artifacts" in caught.value.reason

    def test_a_local_action_is_refused(self, tmp_path):
        document = _document()
        document["jobs"]["panel"]["steps"].append({"uses": "./.github/actions/x"})
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "uses_untrusted_action" in caught.value.reason

    def test_a_job_level_reusable_workflow_is_refused(self, tmp_path):
        document = _document()
        document["jobs"]["panel"]["uses"] = "./.github/workflows/other.yml"
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "uses_untrusted_action" in caught.value.reason

    def test_installing_from_the_candidate_is_refused(self, tmp_path):
        document = _document()
        document["jobs"]["count"]["steps"].append(
            {"name": "deps", "run": "pip install -r requirements.txt"})
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "executes_candidate_content" in caught.value.reason


class TestTheTriggerCannotBeChosenByACandidate:

    def test_a_pull_request_trigger_is_refused(self, tmp_path):
        document = _document()
        _on(document)["pull_request"] = {}
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "trigger_not_permitted" in caught.value.reason

    def test_an_unfiltered_workflow_run_is_refused(self, tmp_path):
        """Fires on EVERY workflow's completion, including one a PR just added."""
        document = _document()
        _on(document)["workflow_run"].pop("workflows")
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "does_not_filter_by_workflow" in caught.value.reason

    def test_watching_the_wrong_workflow_is_refused(self, tmp_path):
        document = _document()
        _on(document)["workflow_run"]["workflows"] = ["ci", "something-else"]
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "filters_wrong_workflow" in caught.value.reason


class TestCapabilitySurface:

    def test_extra_permissions_are_refused(self, tmp_path):
        document = _document()
        document["permissions"]["contents"] = "write"
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "permissions_mismatch" in caught.value.reason

    def test_missing_permissions_are_refused_too(self, tmp_path):
        """Exactly, not at-most.

        A workflow that silently lost `statuses: write` would pass a subset
        check and then fail at run time — after the provider budget was spent."""
        document = _document()
        document["permissions"].pop("statuses")
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "permissions_mismatch" in caught.value.reason

    def test_a_secret_in_preflight_is_refused(self, tmp_path):
        document = _document()
        document["jobs"]["preflight"]["steps"][0].setdefault("env", {})
        document["jobs"]["preflight"]["steps"][0]["env"]["K"] = (
            "${{ secrets.TRUSTED_VERIFIER_OPENAI_KEY }}")
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "secret_in_unexpected_job" in caught.value.reason

    def test_a_workflow_level_secret_is_refused(self, tmp_path):
        """Workflow-level env is inherited by every job, including finalize."""
        document = _document()
        document["env"] = {"K": "${{ secrets.TRUSTED_VERIFIER_OPENAI_KEY }}"}
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "workflow_level_secret" in caught.value.reason


class TestStatusesAreTheOtherLanesConcern:

    def test_the_panel_cannot_publish_a_trusted_context(self):
        """The reason this package has its own publisher at all."""
        for context in ("trusted-verifier-count", "trusted-cross-vendor-review"):
            with pytest.raises(PanelRefusal) as caught:
                statuspublish.assert_publishable_context(context)
            assert "attempted_reserved_context" in caught.value.reason

    def test_the_trusted_publisher_cannot_publish_a_midterm_context(self):
        """And the reverse, which is what makes them two disjoint choke points
        rather than one widened one."""
        from trustedlane import statuspublish as trusted
        for context in mp.PANEL_STATUSES:
            with pytest.raises(LaneRefusal):
                trusted.assert_publishable_context(context)

    def test_the_inactive_context_is_refused_by_name(self):
        with pytest.raises(PanelRefusal) as caught:
            statuspublish.assert_publishable_context("independent-verify-inactive")
        assert "attempted_reserved_context" in caught.value.reason

    @pytest.mark.parametrize("value", [
        "HEAD", "refs/heads/main", "main", "c8ba2a7", "",
        HEAD.upper(),  # uppercase is refused: GitHub SHAs are lowercase
    ])
    def test_a_status_must_land_on_an_exact_commit(self, value):
        """Each of these resolves to something, which is the danger."""
        with pytest.raises(PanelRefusal) as caught:
            statuspublish.assert_candidate_sha(value)
        assert "not_a_commit_sha" in caught.value.reason

    def test_success_requires_an_evidence_digest(self):
        with pytest.raises(PanelRefusal) as caught:
            statuspublish.status_request(
                repository_numeric_id=mp.REPOSITORY_NUMERIC_ID,
                candidate_head_sha=HEAD, context=mp.REVIEW_STATUS,
                state="success", description="green",
                target_url="https://example.invalid/run", run_id=1, run_attempt=1)
        assert "success_status_without_evidence_digest" in caught.value.reason

    def test_success_with_a_digest_is_accepted(self):
        request = statuspublish.status_request(
            repository_numeric_id=mp.REPOSITORY_NUMERIC_ID,
            candidate_head_sha=HEAD, context=mp.REVIEW_STATUS, state="success",
            description="panel green", target_url="https://example.invalid/run",
            run_id=1, run_attempt=1, evidence_sha256=DIGEST)
        assert request["authority"].endswith("NOT_WRITE_SEPARATED")

    def test_pending_is_publishable_without_evidence(self):
        """Pending must be publishable BEFORE anything is spent, so it cannot
        require a digest of work that has not happened."""
        request = statuspublish.pending(
            candidate_head_sha=HEAD, context=mp.COUNT_STATUS,
            target_url="https://example.invalid/run", run_id=1, run_attempt=1)
        assert request["state"] == "pending"

    def test_a_left_pending_status_is_refused(self):
        published = [
            {"context": mp.COUNT_STATUS, "state": "success"},
            {"context": mp.REVIEW_STATUS, "state": "pending"},
        ]
        with pytest.raises(PanelRefusal) as caught:
            statuspublish.assert_no_pending_left(published)
        assert "left_pending" in caught.value.reason

    def test_terminal_on_every_context_passes(self):
        published = [
            {"context": mp.COUNT_STATUS, "state": "success"},
            {"context": mp.REVIEW_STATUS, "state": "failure"},
        ]
        assert statuspublish.assert_no_pending_left(published)["all_terminal"]


class TestTheProviderSeam:

    def test_a_missing_key_is_refused_not_degraded(self):
        """A panel that degrades to 'no key, no findings' publishes a green
        review having reviewed nothing."""
        with pytest.raises(PanelRefusal) as caught:
            transport.read_provider_key({})
        assert "provider_key_absent" in caught.value.reason

    def test_the_refusal_does_not_echo_the_key(self):
        with pytest.raises(PanelRefusal) as caught:
            transport.read_provider_key({transport.PROVIDER_KEY_ENV: "   "})
        assert "sk-" not in caught.value.reason

    def test_the_key_env_var_is_not_named_after_the_trusted_secret(self):
        """A mid-term process exporting the trusted secret's NAME into its own
        environment would look, to a trusted runtime-binding check, like a
        trusted runner holding a trusted credential."""
        assert transport.PROVIDER_KEY_ENV != mp.SECRET_NAME

    def test_an_ungoverned_model_is_refused(self):
        for model in ("gpt-5.6-solaris", "gpt-4o", "gpt-5.6"):
            with pytest.raises(PanelRefusal) as caught:
                transport.assert_model_is_governed(model)
            assert "model_not_governed" in caught.value.reason

    def test_every_governed_model_is_accepted(self):
        for model in mp.PANEL_MODELS:
            assert transport.assert_model_is_governed(model) == model

    def test_the_no_provider_transport_refuses_and_records(self):
        """Phase A's proof: not 'no call was made' but 'no call could be'."""
        no_provider = transport.NoProviderTransport()
        with pytest.raises(PanelRefusal) as caught:
            no_provider.post(model="gpt-5.6-sol", system="s", user="u")
        assert "provider_call_attempted_in_no_provider_mode" in caught.value.reason
        assert no_provider.attempts == ["gpt-5.6-sol"]

    def test_zero_call_assertion_catches_the_attempt(self):
        no_provider = transport.NoProviderTransport()
        with pytest.raises(PanelRefusal):
            no_provider.post(model="gpt-5.6-sol", system="s", user="u")
        with pytest.raises(PanelRefusal) as caught:
            transport.assert_no_provider_calls(no_provider)
        assert "provider_calls_were_made" in caught.value.reason

    def test_an_untouched_dry_run_proves_zero(self):
        assert transport.assert_no_provider_calls(
            transport.NoProviderTransport())["provider_calls"] == 0

    def test_a_transport_that_cannot_account_is_refused(self):
        """'It had no counter, so we could not prove a call was made' is not a
        proof of zero."""
        class Opaque:
            pass
        with pytest.raises(PanelRefusal) as caught:
            transport.assert_no_provider_calls(Opaque())
        assert "cannot_account_for_calls" in caught.value.reason

    def test_the_fake_transport_opens_no_socket_and_counts(self):
        fake = transport.FakeProviderTransport()
        for model in mp.PANEL_MODELS:
            fake.post(model=model, system="s", user="u")
        assert fake.call_count == 3
        assert fake.models_called() == sorted(mp.PANEL_MODELS)


class TestNamingCannotDrift:

    def test_the_secret_name_collision_is_exact(self):
        """Not similar — identical. If these diverge, this package's warning
        about trusted-lane prerequisite 6 becomes a false alarm."""
        from trustedlane.runtimebinding import PROVIDER_SECRET_NAME
        assert mp.SECRET_NAME == PROVIDER_SECRET_NAME

    def test_the_repository_id_matches_the_trusted_lane(self):
        import trustedlane
        assert mp.REPOSITORY_NUMERIC_ID == trustedlane.REPOSITORY_NUMERIC_ID

    def test_midterm_and_trusted_status_names_are_disjoint(self):
        assert statusnames.assert_midterm_and_trusted_never_collide()["collisions"] == 0

    def test_no_midterm_name_claims_to_be_trusted(self):
        for name in mp.PANEL_STATUSES:
            assert not name.lower().startswith("trusted")

    def test_the_panel_job_checks_are_never_requirable(self):
        """They land on main's commit, not on the candidate head."""
        for job in statusnames.MIDTERM_JOB_STATUSES:
            with pytest.raises(LaneRefusal):
                statusnames.assert_only_requirable_statuses([job])

    def test_the_panel_contexts_are_requirable(self):
        assert statusnames.assert_only_requirable_statuses(
            list(mp.PANEL_STATUSES))["classes"] == ["MIDTERM"]

    def test_forbidden_evidence_classes_are_not_prefix_derived(self):
        """`WRITE_SEPARATED_REVIEW_EVIDENCE` contains no `TRUSTED` and is
        exactly the claim this architecture cannot make."""
        assert "WRITE_SEPARATED_REVIEW_EVIDENCE" in mp.FORBIDDEN_EVIDENCE_CLASSES
        assert not all(c.startswith("TRUSTED_")
                       for c in mp.FORBIDDEN_EVIDENCE_CLASSES)

    def test_no_midterm_evidence_class_is_a_trusted_one(self):
        assert not set(mp.MIDTERM_EVIDENCE_CLASSES) & set(
            mp.FORBIDDEN_EVIDENCE_CLASSES)


class TestThePolicyDocumentCannotDriftFromTheCode:
    """A governance file that only describes the code is a comment with a
    digest. Every constant below is asserted against the module."""

    @staticmethod
    def _policy() -> dict:
        return json.loads(POLICY.read_text(encoding="utf-8"))

    def test_models_match(self):
        assert tuple(self._policy()["panel"]["models"]) == mp.PANEL_MODELS

    def test_required_approver_matches(self):
        assert self._policy()["panel"]["required_approver"] == mp.REQUIRED_APPROVER

    def test_strict_refutation_matches(self):
        assert self._policy()["panel"]["strict_any_refutation"] is mp.STRICT_ANY_REFUTATION

    def test_statuses_match(self):
        policy = self._policy()["statuses"]
        assert tuple(policy["published_on_candidate_head"]) == mp.PANEL_STATUSES
        assert tuple(policy["never_publish"]) == mp.FORBIDDEN_STATUSES

    def test_forbidden_evidence_classes_match(self):
        assert tuple(self._policy()["evidence"]["may_never_emit"]) == \
            mp.FORBIDDEN_EVIDENCE_CLASSES

    def test_permissions_match_the_validator(self):
        assert self._policy()["permissions"] == privilegedworkflow.REQUIRED_PERMISSIONS

    def test_high_risk_prefixes_match(self):
        assert tuple(self._policy()["high_risk_workflow_change"]["path_prefixes"]) \
            == mp.HIGH_RISK_PATH_PREFIXES

    def test_the_policy_records_the_prerequisite_6_collision(self):
        """The collision must be stated in the governance record, not only in a
        docstring someone has to go looking for."""
        assert self._policy()["secret"]["collides_with_trusted_lane_prerequisite"] == 6

    def test_the_policy_never_claims_write_separation(self):
        policy = self._policy()
        assert policy["architecture"]["write_separated"] is False
        assert policy["architecture"]["production_eligible"] is False


class TestTheTrustedLaneIsUntouched:

    def test_the_trusted_phase_is_still_d0(self):
        """The whole reason this package exists separately. If a future edit
        raises this to make one function return, D1 and D2 activate across the
        trusted lane on the strength of an architecture that has none of their
        preconditions."""
        from trustedlane import phases
        assert phases.IMPLEMENTED_PHASE == phases.D0

    def test_the_midterm_package_never_calls_a_phase_gated_capability(self):
        """Source-level, over the whole package, via AST rather than grep.

        The first version of this test text-searched the source and failed on
        this package's own docstrings — which name these functions precisely in
        order to explain that they are NOT called. That is the proxy-question
        defect this repository has hit before and already has a recorded answer
        for (`P1.2: AST-based credential scan`): grep asks "does the string
        appear", and the real question is "is the capability referenced".

        An AST walk answers the real one. A name inside a docstring is a
        `Constant`, not an `Attribute` or an `alias`, so prose costs nothing and
        an actual call is caught wherever it hides."""
        import ast

        forbidden = {"read_credential", "read_signing_key",
                     "read_installation_token", "IMPLEMENTED_PHASE",
                     "assert_phase_permitted"}
        offenders = []
        for path in sorted((ROOT / "scripts" / "midtermpanel").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in forbidden:
                    offenders.append(f"{path.name}:attribute:{node.attr}")
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        if alias.name in forbidden:
                            offenders.append(f"{path.name}:import:{alias.name}")
        assert offenders == [], offenders

    def test_d1_and_d2_are_still_templates(self):
        live = {p.name for p in (ROOT / ".github" / "workflows").glob("*.yml")}
        assert not any(n.startswith("d1-") or n.startswith("d2-") for n in live)
