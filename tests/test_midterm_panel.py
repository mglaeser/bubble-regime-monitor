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
from midtermpanel import engine, privilegedworkflow, transport  # noqa: E402
from midtermpanel import status as statuspublish  # noqa: E402
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
            no_provider.post(transport.COUNT_PATH, b"{}")
        assert "provider_call_attempted_in_no_provider_mode" in caught.value.reason
        assert no_provider.attempts == [{"path": transport.COUNT_PATH,
                                         "bytes": 2}]

    def test_zero_call_assertion_catches_the_attempt(self):
        no_provider = transport.NoProviderTransport()
        with pytest.raises(PanelRefusal):
            no_provider.post(transport.GENERATION_PATH, b"{}")
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


class TestCountingAndGeneratingAreDifferentCapabilities:
    """§7. Counting and generating are separately approved and separately
    priced, so the object that can do one may not do the other.

    The live halves are the trusted lane's own transports — the mid-term lane
    binds them rather than writing a second pair. These tests therefore drive
    the wrapper against a stand-in inner, and a separate test proves the real
    binders hand back the real classes."""

    class Inner:
        """Shaped exactly like the trusted transports: engine signature,
        declared source, its own record()."""

        source = transport.SOURCE_PROVIDER

        def __init__(self, permitted, *, reply=(200, b"{}")):
            self.permitted = permitted
            self.reply = reply
            self.seen = []
            self.gate_calls = []

        def post(self, path, body, *, timeout=None):
            if path != self.permitted:
                raise AssertionError("the wrapper must refuse before this")
            self.seen.append((path, timeout))
            return self.reply

        def record(self):
            return {"transport_class": "TRUSTED_LANE_COUNT_TRANSPORT",
                    "count_attempts": len(self.seen)}

        def assert_zero_generation(self):
            self.gate_calls.append("assert_zero_generation")
            return {"generation_calls": 0}

    def _count(self, **kwargs):
        return transport.MidtermProviderTransport(
            self.Inner(transport.COUNT_PATH, **kwargs),
            capability="COUNT_TRANSPORT",
            permitted_paths=(transport.COUNT_PATH,))

    def _generation(self, **kwargs):
        return transport.MidtermProviderTransport(
            self.Inner(transport.GENERATION_PATH, **kwargs),
            capability="GENERATION_TRANSPORT",
            permitted_paths=(transport.GENERATION_PATH,))

    def test_the_count_transport_cannot_reach_the_generation_endpoint(self):
        live = self._count()
        with pytest.raises(PanelRefusal) as caught:
            live.post(transport.GENERATION_PATH, b"{}")
        assert "transport_path_not_permitted" in caught.value.reason
        assert live._inner.seen == [], "the refusal must precede the send"

    def test_the_generation_transport_cannot_reach_the_count_endpoint(self):
        live = self._generation()
        with pytest.raises(PanelRefusal) as caught:
            live.post(transport.COUNT_PATH, b"{}")
        assert "transport_path_not_permitted" in caught.value.reason
        assert live._inner.seen == []

    def test_the_allowlist_is_exact_and_not_a_prefix(self):
        """`/v1/responses/input_tokens` starts with `/v1/responses`, so a prefix
        test on the count transport would permit the endpoint that costs."""
        assert transport.COUNT_PATH.startswith(transport.GENERATION_PATH)
        with pytest.raises(PanelRefusal):
            self._count().post(transport.GENERATION_PATH, b"{}")

    def test_each_transport_reaches_its_own_endpoint_and_accounts(self):
        for live, path in ((self._count(), transport.COUNT_PATH),
                           (self._generation(), transport.GENERATION_PATH)):
            assert live.post(path, b"{}", timeout=7) == (200, b"{}")
            assert live._inner.seen == [(path, 7)]
            assert live.call_count == 1
            assert live.paths_called() == [path]

    def test_the_engine_signature_is_what_is_implemented(self):
        """The engine calls `post(path, body, timeout=...)` positionally and
        reads `.source`. A transport with a different shape fails at the first
        real call, in the job holding the credential."""
        import inspect
        for cls in (transport.MidtermProviderTransport,
                    transport.NoProviderTransport,
                    transport.EngineStandInTransport):
            parameters = list(
                inspect.signature(cls.post).parameters.values())[1:]
            assert [p.name for p in parameters] == ["path", "body", "timeout"]
            assert parameters[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            assert parameters[2].kind is inspect.Parameter.KEYWORD_ONLY

    def test_the_trusted_transports_have_the_same_signature(self):
        """The wrapper delegates positionally, so a trusted-lane rename would
        otherwise surface as a TypeError inside the job holding the key."""
        import inspect

        from trustedlane.counttransport import TrustedCountTransport
        from trustedlane.generationtransport import TrustedGenerationTransport
        for cls in (TrustedCountTransport, TrustedGenerationTransport):
            parameters = list(
                inspect.signature(cls.post).parameters.values())[1:]
            assert [p.name for p in parameters] == ["path", "body", "timeout"]

    def test_every_transport_declares_a_source_the_engine_accepts(self):
        for cls in (transport.MidtermProviderTransport,
                    transport.NoProviderTransport,
                    transport.EngineStandInTransport):
            assert cls.source in (transport.SOURCE_PROVIDER,
                                  transport.SOURCE_MOCK)
        assert (transport.MidtermProviderTransport.source
                == transport.SOURCE_PROVIDER)
        assert transport.NoProviderTransport.source == transport.SOURCE_MOCK

    def _engine_stub(self, *, count_path=None, generation_path=None,
                     provider="PROVIDER", mock="MOCK_NOT_PROVIDER"):
        class Counting:
            COUNT_PATH = count_path or transport.COUNT_PATH
            SOURCE_PROVIDER = provider
            SOURCE_MOCK = mock

        class Executor:
            GENERATION_PATH = generation_path or transport.GENERATION_PATH

        return {"modules": {"verifier.counting": Counting,
                            "verifier.executor": Executor}}

    def test_the_paths_are_checked_against_the_loaded_engine(self):
        """A lane constant that has drifted does not fail loudly: the allowlist
        would permit a path the engine never sends and refuse the one it does,
        which reads as a transport error rather than a configuration mistake."""
        assert transport.assert_paths_match_engine(
            self._engine_stub())["paths_match_engine"] is True
        for drift in ({"count_path": "/v2/count"},
                      {"generation_path": "/v2/responses"}):
            with pytest.raises(PanelRefusal) as caught:
                transport.assert_paths_match_engine(self._engine_stub(**drift))
            assert "paths_disagree_with_engine" in caught.value.reason

    def test_the_source_labels_are_checked_against_the_loaded_engine(self):
        """The engine decides what may be represented as provider evidence by
        comparing these exact strings."""
        with pytest.raises(PanelRefusal) as caught:
            transport.assert_paths_match_engine(
                self._engine_stub(provider="PROVIDER_BACKED"))
        assert "source_labels_disagree_with_engine" in caught.value.reason

    def test_the_lane_labels_are_the_real_engines_labels(self):
        """Against the engine source this lane is pinned to, not a stub."""
        import subprocess
        source = subprocess.run(
            ["git", "show", f"{HEAD}:scripts/verifier/counting.py"],
            capture_output=True, text=True, check=True, cwd=str(ROOT)).stdout
        assert f'SOURCE_PROVIDER = "{transport.SOURCE_PROVIDER}"' in source
        assert f'SOURCE_MOCK = "{transport.SOURCE_MOCK}"' in source
        assert f'COUNT_PATH = "{transport.COUNT_PATH}"' in source

    def test_a_non_bytes_body_is_refused(self):
        """The engine hashes the exact bytes it counted and re-sends those
        bytes; encoding at the transport would make the two differ."""
        with pytest.raises(PanelRefusal) as caught:
            self._count().post(transport.COUNT_PATH, "{}")
        assert "transport_body_not_bytes" in caught.value.reason

    def test_an_inner_that_does_not_declare_provider_is_refused(self):
        class Undeclared:
            def post(self, path, body, *, timeout=None):
                return 200, b"{}"

        with pytest.raises(PanelRefusal) as caught:
            transport.MidtermProviderTransport(
                Undeclared(), capability="COUNT_TRANSPORT",
                permitted_paths=(transport.COUNT_PATH,))
        assert "does_not_declare_provider" in caught.value.reason

    def test_the_record_never_says_trusted(self):
        """A mid-term run writing `TRUSTED_LANE_*` into evidence would be
        claiming a lane whose operator prerequisites are unrecorded."""
        live = self._count()
        live.post(transport.COUNT_PATH, b"{}")
        record = live.record()
        assert record["transport_class"] == "MIDTERM_SINGLE_REPO_COUNT_TRANSPORT"
        assert record["wrapped_transport_class"] == "Inner"
        assert record["provider_calls"] == 1
        assert "not a trusted-lane phase" in record["honest_scope"]
        assert not str(record["transport_class"]).startswith("TRUSTED")

    def test_the_wrapped_gates_are_forwarded_not_reimplemented(self):
        """`assert_zero_generation` and `assert_within_authorized_tokens` live
        on the trusted class; a second copy here is a second opinion."""
        live = self._count()
        assert live.assert_zero_generation() == {"generation_calls": 0}
        assert live._inner.gate_calls == ["assert_zero_generation"]
        assert not hasattr(transport.MidtermProviderTransport,
                           "assert_zero_generation")

    def test_the_phase_label_is_not_a_trusted_lane_phase(self):
        from trustedlane import phases
        assert transport.PHASE_LABEL not in (phases.D0, phases.D1, phases.D2)
        assert transport.PHASE_LABEL.startswith("MIDTERM")

    def test_a_stand_in_that_declares_provider_is_refused(self):
        """A wrapper cannot make an undeclared transport safe."""
        class Pretender:
            source = transport.SOURCE_PROVIDER

            def post(self, path, body, *, timeout=None):
                return 200, b"{}"

        with pytest.raises(PanelRefusal) as caught:
            transport.EngineStandInTransport(
                Pretender(), permitted_paths=(transport.COUNT_PATH,))
        assert "stand_in_transport_not_declared_mock" in caught.value.reason

    def test_the_stand_in_wrapper_restricts_paths_and_accounts(self):
        class Inner:
            source = transport.SOURCE_MOCK

            def __init__(self):
                self.seen = []

            def post(self, path, body, *, timeout=None):
                self.seen.append(path)
                return 200, transport.scripted_count_body(11)

        inner = Inner()
        wrapped = transport.EngineStandInTransport(
            inner, permitted_paths=(transport.COUNT_PATH,))
        status, body = wrapped.post(transport.COUNT_PATH, b"{}")
        assert (status, json.loads(body)["input_tokens"]) == (200, 11)
        with pytest.raises(PanelRefusal):
            wrapped.post(transport.GENERATION_PATH, b"{}")
        assert inner.seen == [transport.COUNT_PATH]
        assert wrapped.call_count == 1
        assert wrapped.paths_called() == [transport.COUNT_PATH]

    def test_accounting_refuses_a_bare_count(self):
        class BareCount:
            calls = 4

        with pytest.raises(PanelRefusal) as caught:
            transport.assert_provider_calls_all_went_through(
                BareCount(), expected_paths=(transport.COUNT_PATH,))
        assert "accounting_is_a_bare_count" in caught.value.reason

    def test_accounting_refuses_a_call_on_an_unexpected_path(self):
        live = self._generation()
        live.post(transport.GENERATION_PATH, b"{}")
        with pytest.raises(PanelRefusal) as caught:
            transport.assert_provider_calls_all_went_through(
                live, expected_paths=(transport.COUNT_PATH,))
        assert "provider_calls_on_unexpected_paths" in caught.value.reason

    def test_accounting_accepts_the_expected_path(self):
        live = self._count()
        live.post(transport.COUNT_PATH, b"{}")
        record = transport.assert_provider_calls_all_went_through(
            live, expected_paths=(transport.COUNT_PATH,))
        assert record == {"provider_calls": 1,
                          "paths": [transport.COUNT_PATH],
                          "declared_source": transport.SOURCE_PROVIDER}


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
                     "read_installation_token", "assert_phase_permitted"}
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

    def test_the_package_never_assigns_the_trusted_phase(self):
        """READING `IMPLEMENTED_PHASE` is required; WRITING it is the hazard.

        The first version of the test above forbade the name entirely and caught
        `policystate.assert_trusted_lane_is_locked`, which reads the constant in
        order to verify it is still `D0`. That is the mode lock doing its job —
        the exact opposite of raising the phase — and forbidding it would have
        meant deleting the check that keeps D1/D2 shut.

        So the two are separated. Reading is allowed anywhere. Assigning to it,
        anywhere in this package, is refused: that is the single edit that would
        activate a credential-bearing lane whose sixteen preconditions are all
        still open."""
        import ast

        offenders = []
        for path in sorted((ROOT / "scripts" / "midtermpanel").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    targets = [node.target]
                for target in targets:
                    name = getattr(target, "attr", None) or getattr(
                        target, "id", None)
                    if name == "IMPLEMENTED_PHASE":
                        offenders.append(f"{path.name}:assigns:{name}")
        assert offenders == [], offenders

    def test_the_mode_lock_reads_the_phase_and_would_refuse_a_raised_one(self):
        """The lock is live, and it is not vacuous.

        Asserting only that it passes today would pass equally well if it never
        checked anything. This drives the refusing branch with a stubbed phase,
        so the test fails if the comparison is ever removed."""
        from midtermpanel import policystate
        from trustedlane import phases

        record = policystate.assert_trusted_lane_is_locked(root=str(ROOT))
        assert record["trusted_lane_locked"] is True
        assert record["implemented_phase"] == phases.D0

        original = phases.IMPLEMENTED_PHASE
        try:
            phases.IMPLEMENTED_PHASE = phases.D1
            with pytest.raises(PanelRefusal) as caught:
                policystate.assert_trusted_lane_is_locked(root=str(ROOT))
            assert "persistent_secret_mode" in caught.value.reason
        finally:
            phases.IMPLEMENTED_PHASE = original
        assert phases.IMPLEMENTED_PHASE == phases.D0

    def test_d1_and_d2_are_still_templates(self):
        live = {p.name for p in (ROOT / ".github" / "workflows").glob("*.yml")}
        assert not any(n.startswith("d1-") or n.startswith("d2-") for n in live)


class TestWorkflowOutputsHaveProducers:
    """Every value the workflow reads is a value the workflow declares.

    GitHub does not error on an undeclared job output. `needs.preflight.outputs.x`
    where `x` was never declared resolves to the EMPTY STRING, and the step
    receives `x=""` and carries on. That is how `engine_digest` and
    `policy_digest` came to be arriving blank in both credential-bearing jobs
    while every test passed: nothing in the system treats a silent empty as an
    error, so the only way to catch it is to compare the two sets directly.
    """

    @staticmethod
    def _text() -> str:
        return WORKFLOW.read_text(encoding="utf-8")

    @staticmethod
    def _declared() -> set:
        document = _document()
        declared = set()
        for job in (document.get("jobs") or {}).values():
            for name in ((job or {}).get("outputs") or {}):
                declared.add(str(name))
        return declared

    def _consumed(self) -> set:
        import re
        return set(re.findall(r"needs\.\w+\.outputs\.(\w+)", self._text()))

    def test_every_consumed_output_is_declared(self):
        missing = sorted(self._consumed() - self._declared())
        assert missing == [], (
            f"consumed but never declared: {missing} — these resolve to the "
            "empty string at run time, silently")

    def test_every_step_output_reference_has_a_producing_step(self):
        """A job output must name a step id that exists in that job."""
        import re
        document = _document()
        offenders = []
        for job_id, job in (document.get("jobs") or {}).items():
            step_ids = {str(s.get("id")) for s in ((job or {}).get("steps") or [])
                        if isinstance(s, dict) and s.get("id")}
            for name, expression in (((job or {}).get("outputs")) or {}).items():
                for referenced in re.findall(r"steps\.(\w+)\.outputs",
                                             str(expression)):
                    if referenced not in step_ids:
                        offenders.append(f"{job_id}.{name} -> steps.{referenced}")
        assert offenders == [], offenders

    def test_the_cli_emits_exactly_what_the_workflow_declares(self):
        """The producer side, checked against the consumer side.

        `preflightcli.decide()` builds the dict that becomes the job outputs.
        If it stops emitting one the workflow declares, the declaration silently
        yields empty — the same failure from the other end."""
        from midtermpanel import preflightcli
        emitted = set(preflightcli.PUBLIC_OUTPUTS)
        declared = self._declared()
        assert self._consumed() <= emitted, sorted(self._consumed() - emitted)
        assert declared <= emitted, sorted(declared - emitted)

    def test_deleting_a_declaration_would_redden_this(self):
        """The guard is not vacuous.

        Asserting a subset relation passes trivially when the consumed set is
        empty. This proves the consumed set is real and that the comparison has
        something to catch."""
        consumed = self._consumed()
        assert "engine_digest" in consumed
        assert "policy_digest" in consumed
        assert len(consumed) >= 7


class TestTheReviewerIsNotTheReviewed:
    """§2. The previous wiring resolved the engine's source from
    `MIDTERM_ENGINE_CANDIDATE_SHA`, which the workflow set to the head of the
    pull request under review — so a pull request could edit `scripts/verifier`
    and its own edited code would be the code that reviewed it, in a job holding
    the provider key."""

    HEAD = "1111111111111111111111111111111111111111"   # pragma: allowlist secret
    ENGINE = "2222222222222222222222222222222222222222"  # pragma: allowlist secret
    LANE = "3333333333333333333333333333333333333333"    # pragma: allowlist secret

    def _release(self, **overrides):
        release = {"approved_engine_source_sha": self.ENGINE,
                   "approved_engine_protected_sha": self.LANE,
                   "approved_engine_artifact_sha256": None,
                   "approved_engine_release_tag": None}
        release.update(overrides)
        release["provenance"] = engine.provenance_of(release)
        return release

    def test_the_engine_source_may_not_be_the_reviewed_head(self):
        with pytest.raises(PanelRefusal) as caught:
            engine.assert_engine_source_is_not_the_reviewed_candidate(
                release=self._release(approved_engine_source_sha=self.HEAD),
                reviewed_candidate_head_sha=self.HEAD)
        assert "engine_source_is_the_reviewed_candidate" in caught.value.reason
        assert "approved_engine_source_sha" in caught.value.reason

    def test_the_protected_half_may_not_be_the_reviewed_head_either(self):
        """An engine whose `trustedlane` source came from the head under review
        is just as much candidate code holding the credential."""
        with pytest.raises(PanelRefusal) as caught:
            engine.assert_engine_source_is_not_the_reviewed_candidate(
                release=self._release(approved_engine_protected_sha=self.HEAD),
                reviewed_candidate_head_sha=self.HEAD)
        assert "approved_engine_protected_sha" in caught.value.reason

    def test_distinct_identities_are_permitted_and_reported_separately(self):
        record = engine.assert_engine_source_is_not_the_reviewed_candidate(
            release=self._release(), reviewed_candidate_head_sha=self.HEAD)
        assert record["reviewed_candidate_head_sha"] == self.HEAD
        assert record["approved_engine_source_sha"] == self.ENGINE
        assert record["approved_engine_protected_sha"] == self.LANE
        assert record["identities_are_distinct"] is True

    def test_the_retired_candidate_variable_is_refused_not_ignored(self):
        """A workflow still exporting it is a workflow that still believes the
        head selects the engine; reading a different variable silently would
        leave that belief shipping and passing."""
        with pytest.raises(PanelRefusal) as caught:
            engine.resolve_release_config({
                "MIDTERM_ENGINE_CANDIDATE_SHA": self.HEAD,
                engine.ENGINE_SOURCE_ENV: self.ENGINE,
                engine.ENGINE_PROTECTED_ENV: self.LANE})
        assert "retired_engine_variables_present" in caught.value.reason
        assert "MIDTERM_ENGINE_CANDIDATE_SHA" in caught.value.reason

    def test_there_is_no_default_engine(self):
        with pytest.raises(PanelRefusal) as caught:
            engine.resolve_release_config({})
        assert "approved_engine_release_not_configured" in caught.value.reason

    def test_the_engine_digest_has_no_environment_fallback(self, monkeypatch):
        """The old signature defaulted `roles` to a value read from
        `MIDTERM_ENGINE_*`, so a caller could get a digest for an engine it had
        never chosen and would never load."""
        monkeypatch.setenv("MIDTERM_ENGINE_CANDIDATE_SHA", self.HEAD)
        monkeypatch.setenv("MIDTERM_ENGINE_PROTECTED_SHA", self.LANE)
        with pytest.raises(TypeError):
            engine.engine_digest()
        with pytest.raises(PanelRefusal):
            engine.engine_digest(roles={"whatever": self.ENGINE})

    def test_the_digest_is_over_the_two_approved_commits(self):
        release = self._release()
        first = engine.engine_digest(roles=engine.source_roles(release))
        second = engine.engine_digest(roles=engine.source_roles(release))
        other = engine.engine_digest(roles=engine.source_roles(
            self._release(approved_engine_source_sha=self.HEAD)))
        assert first == second != other

    def test_no_module_still_reads_the_retired_variable(self):
        """AST, not grep: the docstrings name these variables in order to
        explain that they are no longer read, and a text search would count
        that explanation as the defect it describes."""
        import ast
        offenders = []
        for path in sorted(Path("scripts/midtermpanel").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(
                        node.value, str) and node.value in (
                            "MIDTERM_ENGINE_CANDIDATE_SHA",
                            "MIDTERM_ENGINE_PROTECTED_SHA"):
                    if path.name != "engine.py":
                        offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], (
            "only engine.py may name the retired variables, and only to refuse "
            f"them: {offenders}")


class TestModeSelectsWhichWayTheGatesPoint:
    """§6. Provider mode refuses an unapproved artifact; dry-run mode refuses a
    transport that could spend. Neither can be satisfied by inspecting the
    environment harder, because the environment is what the caller controls."""

    def test_provider_mode_refuses_a_rebuilt_artifact(self):
        with pytest.raises(PanelRefusal) as caught:
            engine.assert_provenance_permits(engine.REBUILT_TEST_ONLY,
                                             mode=engine.MODE_PROVIDER)
        assert "engine_provenance_is_test_only" in caught.value.reason

    def test_a_dry_run_may_use_a_rebuilt_artifact(self):
        assert engine.assert_provenance_permits(
            engine.REBUILT_TEST_ONLY,
            mode=engine.MODE_DRY_RUN) == engine.REBUILT_TEST_ONLY

    def test_an_unknown_provenance_is_refused_in_both_modes(self):
        for mode in engine.MODES:
            with pytest.raises(PanelRefusal) as caught:
                engine.assert_provenance_permits("LOOKS_FINE", mode=mode)
            assert "engine_provenance_unknown" in caught.value.reason

    def test_an_unknown_mode_is_refused_rather_than_defaulted(self):
        with pytest.raises(PanelRefusal) as caught:
            engine.assert_mode("PROVIDER")
        assert "engine_mode_unknown" in caught.value.reason

    def test_approved_needs_both_the_bytes_and_the_release(self):
        """A digest without a tag is a digest somebody computed; a tag without a
        digest is a name with no bytes behind it."""
        digest = "a" * 64
        assert engine.provenance_of(
            {"approved_engine_artifact_sha256": digest,
             "approved_engine_release_tag": "v1"}) == engine.APPROVED_RELEASE
        for partial in ({"approved_engine_artifact_sha256": digest},
                        {"approved_engine_release_tag": "v1"}, {}):
            assert engine.provenance_of(partial) == engine.REBUILT_TEST_ONLY

    def _live(self, capability="COUNT_TRANSPORT", path=None):
        class Inner:
            source = transport.SOURCE_PROVIDER

            def post(self, path, body, *, timeout=None):
                return 200, b"{}"

        return transport.MidtermProviderTransport(
            Inner(), capability=capability,
            permitted_paths=(path or transport.COUNT_PATH,))

    def test_a_dry_run_may_not_hold_a_transport_that_could_spend(self):
        live = self._live("GENERATION_TRANSPORT", transport.GENERATION_PATH)
        with pytest.raises(PanelRefusal) as caught:
            engine.assert_transport_suits_mode(live, mode=engine.MODE_DRY_RUN)
        assert "dry_run_holds_a_provider_transport" in caught.value.reason

    def test_provider_mode_may_not_hold_a_stand_in(self):
        """Evidence that reads as a real panel and is not one."""
        with pytest.raises(PanelRefusal) as caught:
            engine.assert_transport_suits_mode(transport.NoProviderTransport(),
                                               mode=engine.MODE_PROVIDER)
        assert "provider_mode_holds_a_stand_in_transport" in caught.value.reason

    def test_each_mode_accepts_its_own_transport(self):
        live = self._live()
        assert engine.assert_transport_suits_mode(
            live, mode=engine.MODE_PROVIDER)["declared_source"] == "PROVIDER"
        assert engine.assert_transport_suits_mode(
            transport.NoProviderTransport(),
            mode=engine.MODE_DRY_RUN)["mode"] == engine.MODE_DRY_RUN

    def test_a_source_download_is_not_an_engine_artifact(self):
        for path in ("/tmp/repo-main.zip", "https://codeload.github.com/x/y",
                     "/runner/_temp/zipball/head"):
            with pytest.raises(PanelRefusal) as caught:
                engine.assert_not_the_github_zip_digest(artifact_path=path)
            assert "looks_like_a_source_download" in caught.value.reason
        assert engine.assert_not_the_github_zip_digest(
            artifact_path="/runner/_temp/engine.tar.gz")


class TestRuntimeProvenance:
    """§12. The static controls prove the workflow never checks the candidate
    out. These prove it from the interpreter's own bookkeeping, because the
    static argument is about a file and the thing that matters is a process."""

    def test_a_candidate_directory_on_sys_path_is_refused(self, tmp_path,
                                                          monkeypatch):
        candidate = tmp_path / "candidate"
        (candidate / "pkg").mkdir(parents=True)
        monkeypatch.syspath_prepend(str(candidate))
        with pytest.raises(PanelRefusal) as caught:
            engine.assert_no_candidate_path_is_importable(
                candidate_paths=[str(candidate)])
        assert "candidate_path_is_importable" in caught.value.reason

    def test_a_clean_sys_path_passes_and_says_what_it_checked(self, tmp_path):
        record = engine.assert_no_candidate_path_is_importable(
            candidate_paths=[str(tmp_path / "absent")])
        assert record["sys_path_clean"] is True
        assert record["sys_path_entries_checked"] >= 1

    def test_a_module_loaded_from_candidate_bytes_is_refused(self, tmp_path):
        candidate = tmp_path / "candidate"
        candidate.mkdir()
        planted = candidate / "planted_module.py"
        planted.write_text("VALUE = 1\n", encoding="utf-8")
        import importlib.util
        spec = importlib.util.spec_from_file_location("planted_module",
                                                      str(planted))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules["planted_module"] = module
        try:
            with pytest.raises(PanelRefusal) as caught:
                engine.assert_no_module_came_from_candidate_data(
                    candidate_paths=[str(candidate)])
            assert "module_loaded_from_candidate_data" in caught.value.reason
            assert "planted_module" in caught.value.reason
        finally:
            del sys.modules["planted_module"]

    def test_a_clean_process_passes_and_counts_what_it_looked_at(self, tmp_path):
        record = engine.assert_no_module_came_from_candidate_data(
            candidate_paths=[str(tmp_path / "candidate")])
        assert record["modules_from_candidate"] == []
        assert record["modules_with_a_file_checked"] > 10

    def test_a_sibling_directory_is_not_treated_as_inside(self, tmp_path):
        """`/a/bcd` does not live under `/a/bc`; an unanchored prefix test would
        say it does and block a run for no reason."""
        assert engine._is_within(str(tmp_path / "bc"), str(tmp_path / "bc"))
        assert not engine._is_within(str(tmp_path / "bcd"),
                                     str(tmp_path / "bc"))

    def test_a_bare_string_of_paths_is_refused(self):
        """Iterated character by character, it would check nothing."""
        with pytest.raises(PanelRefusal) as caught:
            engine.assert_no_module_came_from_candidate_data(
                candidate_paths="/some/path")
        assert "candidate_paths_not_a_sequence" in caught.value.reason


class TestEveryCountInputHasAWorkflowProducer:
    """§5. The previous wiring passed `environ["MIDTERM_SKELETON_PATH"]` into a
    parameter that takes a skeleton dict, and no workflow step wrote a file at
    that path or exported that variable. The count job could not have run once,
    and it would have failed inside the engine rather than at the seam."""

    def _count_job_env_and_steps(self):
        document = _document()
        job = document["jobs"]["count"]
        rendered = yaml.safe_dump(job)
        return job, rendered

    def test_every_required_input_is_exported_by_a_step(self):
        from midtermpanel import inputs
        _, rendered = self._count_job_env_and_steps()
        for spec in inputs.COUNT_INPUTS:
            if not spec["required"]:
                continue
            assert f"{spec['variable']}=" in rendered, (
                f"{spec['variable']} has no producer; its declared one is "
                f"{spec['producer']!r}")

    def test_the_named_producer_step_exists(self):
        from midtermpanel import inputs
        job, _ = self._count_job_env_and_steps()
        names = {str(step.get("name") or "").lower() for step in job["steps"]}
        for spec in inputs.COUNT_INPUTS:
            if not spec["required"]:
                continue
            _, _, step_name = spec["producer"].partition("/")
            assert any(step_name.strip().lower() in name for name in names), (
                f"no step named like {spec['producer']!r}: {sorted(names)}")

    def test_the_repository_step_produces_no_worktree(self):
        """The object/worktree distinction is the safety argument, so the
        producer step has to be the one shape that keeps it.

        This is the STRING-level half. It is deliberately negative — it says
        which commands must not appear — because the positive half ("the fetch
        works") cannot be established by looking at a string, and the previous
        version of this test tried to. It asserted
        `"git fetch --no-checkout origin" in rendered`, which passed, while the
        command it pinned exits 129 with "unknown option". A test that asserts
        a literal proves the literal is present.

        `tests/test_midterm_fetch_shell.py` runs the command."""
        job, rendered = self._count_job_env_and_steps()
        assert "git fetch" in rendered
        for producing in ("git checkout", "git merge", "git switch",
                          "git worktree", "git apply"):
            assert producing not in rendered, producing

    def test_the_invalid_fetch_option_is_gone_from_every_workflow(self):
        """`--no-checkout` is a clone option. `git fetch` refuses it."""
        for path in sorted(
                (ROOT / ".github" / "workflows").glob("midterm-*.yml")):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue    # the comment explaining the removal may name it
                assert "fetch --no-checkout" not in stripped, (
                    f"{path.name}: {stripped}")

    def test_the_challenge_is_a_file_and_not_an_exported_value(self):
        """A challenge in the environment of every later step is a challenge a
        `set -x` can print, and a printed challenge can be replayed."""
        _, rendered = self._count_job_env_and_steps()
        assert "MIDTERM_CHALLENGE_PATH=" in rendered
        assert "MIDTERM_CHALLENGE=" not in rendered

    def test_the_retired_engine_variables_appear_in_no_job(self):
        document = _document()
        for name, job in document["jobs"].items():
            rendered = yaml.safe_dump(job)
            for retired in ("MIDTERM_ENGINE_CANDIDATE_SHA:",
                            "MIDTERM_ENGINE_PROTECTED_SHA:"):
                assert retired not in rendered, f"{name} still sets {retired}"

    def test_the_engine_release_is_read_from_governance_not_the_candidate(self):
        document = _document()
        for job in ("preflight", "count", "panel"):
            rendered = yaml.safe_dump(document["jobs"][job])
            assert "governance/midterm-panel-engine-release.json" in rendered, job
            assert "MIDTERM_APPROVED_ENGINE_SOURCE_SHA" in rendered, job

    def test_the_governance_documents_parse_and_declare_their_authority(self):
        release = json.loads(
            (ROOT / "governance" / "midterm-panel-engine-release.json")
            .read_text(encoding="utf-8"))
        pins = json.loads(
            (ROOT / "governance" / "midterm-panel-pins.json")
            .read_text(encoding="utf-8"))
        assert release["approved_engine_artifact_sha256"] is None
        assert release["approved_engine_release_tag"] is None
        assert engine.provenance_of(release) == engine.REBUILT_TEST_ONLY
        assert pins["authority_class"] == (
            "OPERATOR_APPROVED_MIDTERM_POLICY_ATTESTATION")
        assert pins["write_separated"] is False
        assert pins["approved_by"] and pins["approved_at"]
        assert len(pins["pins"]) == 12
        assert sorted(pins["profiles"]) == ["pr-23", "pr-29", "synthetic"]

    def test_the_pinned_engine_source_is_not_this_branchs_head(self, tmp_path):
        """The document that decides who reviews must not name the thing being
        reviewed. Checked against the real file, not a fixture."""
        import subprocess
        release = json.loads(
            (ROOT / "governance" / "midterm-panel-engine-release.json")
            .read_text(encoding="utf-8"))
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True, cwd=str(ROOT)).stdout.strip()
        assert release["approved_engine_source_sha"] != head
        assert release["approved_engine_protected_sha"] != head

    def test_all_inputs_missing_reports_the_whole_set_at_once(self):
        """One refusal per attempt turns a misconfigured workflow into as many
        failed runs as there are missing inputs, each costing a job that may
        hold a credential."""
        from midtermpanel import inputs
        with pytest.raises(PanelRefusal) as caught:
            inputs.assert_all_present({})
        for spec in inputs.COUNT_INPUTS:
            if spec["required"]:
                assert spec["variable"] in caught.value.reason

    def test_a_short_challenge_is_refused_by_name(self, tmp_path):
        from midtermpanel import inputs
        path = tmp_path / "challenge.txt"
        path.write_text("tooshort\n", encoding="utf-8")
        with pytest.raises(PanelRefusal) as caught:
            inputs.load_challenge({"MIDTERM_CHALLENGE_PATH": str(path)})
        assert "challenge_too_short" in caught.value.reason

    def test_a_directory_that_is_not_a_repository_is_refused(self, tmp_path):
        from midtermpanel import inputs
        with pytest.raises(PanelRefusal) as caught:
            inputs.load_repository_path(
                {"MIDTERM_REPOSITORY_PATH": str(tmp_path)})
        assert "not_a_git_repository" in caught.value.reason

    def test_absent_authorizations_are_recorded_as_absent(self):
        """Not synthesised. A locally built set would let the preflight manifest
        record a clearance nobody authorized."""
        from midtermpanel import inputs
        loaded, record = inputs.load_authorizations({}, engine=None,
                                                    skeleton=None)
        assert loaded is None
        assert record["confers_real_call_authority"] is False
        assert "no operator envelope authority" in record["honest_scope"]


class TestThePrivilegedWorkflowHasOneTrigger:
    """External review, finding 1. A manually dispatched workflow runs against
    a SELECTED REF, and every checkout here omits `ref:`. Under `workflow_run`
    that omission means the default branch; under a dispatch it would mean
    whichever branch was chosen, with the provider key in scope."""

    def test_only_workflow_run_is_declared(self):
        triggers = sorted(_on(_document()))
        assert triggers == ["workflow_run"]

    def test_the_policy_constant_permits_only_workflow_run(self):
        assert privilegedworkflow.PERMITTED_TRIGGERS == ("workflow_run",)

    def test_adding_a_dispatch_trigger_reddens(self, tmp_path):
        document = _document()
        _on(document)["workflow_dispatch"] = {"inputs": {}}
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "trigger" in caught.value.reason

    def test_preflight_refuses_a_dispatch_even_if_one_reappeared(self):
        """Refused in code as well as removed from the file. A dispatch path
        that still worked is a path somebody could re-enable in the workflow
        without noticing the code was ready to serve it."""
        from midtermpanel import preflightcli
        with pytest.raises(PanelRefusal) as caught:
            preflightcli.decide({"TRIGGER_EVENT": "workflow_dispatch"},
                                api=None, root=str(ROOT))
        assert "dispatch_trigger_removed" in caught.value.reason

    def test_the_rerun_dispatcher_holds_no_secret_and_runs_no_panel_code(self):
        path = ROOT / ".github" / "workflows" / "midterm-panel-rerun.yml"
        text = path.read_text(encoding="utf-8")
        assert "secrets." not in text
        assert "midtermpanel" not in text.replace(
            "midterm-panel-rerun", "").replace("midterm-panel-review", "")
        document = yaml.safe_load(text)
        assert sorted(_on(document)) == ["workflow_dispatch"]
        assert document["permissions"]["contents"] == "read"

    def test_the_policy_document_records_the_removal_and_why(self):
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        assert policy["triggers"]["permitted_events"] == ["workflow_run"]
        manual = policy["triggers"]["manual"]
        assert manual["event"] is None
        assert "SELECTED REF" in " ".join(manual["why"])


class TestTheCostProfileIsSelectedNotDefaulted:
    """External review, finding 5. The three profiles differ by more than an
    order of magnitude in cost cap."""

    def _document(self):
        return json.loads(
            (ROOT / "governance" / "midterm-panel-pins.json")
            .read_text(encoding="utf-8"))

    def test_an_unnamed_review_target_is_refused(self):
        from midtermpanel import inputs
        with pytest.raises(PanelRefusal) as caught:
            inputs.select_profile(self._document(), environ={})
        assert "review_target_not_named" in caught.value.reason

    def test_a_target_outside_the_allowlist_is_refused(self):
        from midtermpanel import inputs
        with pytest.raises(PanelRefusal) as caught:
            inputs.select_profile(self._document(),
                                  environ={"MIDTERM_REVIEW_TARGET": "pr-99"})
        assert "not_uniquely_allowlisted" in caught.value.reason

    def test_matching_is_exact_and_not_substring(self):
        """`pr-2` must not select `pr-29`'s budget."""
        from midtermpanel import inputs
        with pytest.raises(PanelRefusal):
            inputs.select_profile(self._document(),
                                  environ={"MIDTERM_REVIEW_TARGET": "pr-2"})

    def test_each_profile_selects_its_own_caps(self):
        from midtermpanel import inputs
        expected = {"synthetic": (100, 0, 5000000),
                    "pr-29": (1200, 80, 25000000),
                    "pr-23": (2500, 200, 60000000)}
        for target, (count, generation, cost) in expected.items():
            pins, profile = inputs.select_profile(
                self._document(),
                environ={"MIDTERM_REVIEW_TARGET": target})
            assert pins["VERIFIER_MAX_COUNT_CALLS"] == count, target
            assert pins["VERIFIER_MAX_GENERATION_CALLS"] == generation, target
            assert pins["VERIFIER_COST_CAP_MICRO_USD"] == cost, target
            assert profile["profile_digest"]

    def test_the_synthetic_profile_cannot_generate_at_all(self):
        from midtermpanel import inputs
        pins, _ = inputs.select_profile(
            self._document(),
            environ={"MIDTERM_REVIEW_TARGET": "synthetic"})
        assert pins["VERIFIER_MAX_GENERATION_CALLS"] == 0

    def test_a_profile_missing_an_overridden_pin_is_refused(self):
        """Otherwise the run silently spends under another profile's cap."""
        from midtermpanel import inputs
        document = self._document()
        del document["profiles"]["pr-29"]["VERIFIER_COST_CAP_MICRO_USD"]
        with pytest.raises(PanelRefusal) as caught:
            inputs.select_profile(document,
                                  environ={"MIDTERM_REVIEW_TARGET": "pr-29"})
        assert "profile_missing_an_overridden_pin" in caught.value.reason

    def test_the_operator_approved_values_are_the_ones_recorded(self):
        pins = self._document()["pins"]
        assert pins["VERIFIER_MAX_OUTPUT_TOKENS"] == 8000
        assert pins["VERIFIER_CONTEXT_MARGIN_TOKENS"] == 4000
        assert pins["VERIFIER_MAX_REVIEW_UNITS"] == 512
        assert pins["VERIFIER_COUNT_TIMEOUT_SECONDS"] == 30
        assert pins["VERIFIER_COUNT_MAX_RETRIES"] == 2
        assert pins["VERIFIER_GENERATION_TIMEOUT_SECONDS"] == 180
        assert pins["VERIFIER_GENERATION_MAX_RETRIES"] == 1
        assert pins["VERIFIER_TOKEN_DRIFT_TOLERANCE"] == 64
        assert pins["VERIFIER_REASONING_EFFORT_BY_MODEL"] == {
            "gpt-5.3-codex": "medium", "gpt-5.6-sol": "medium",
            "gpt-4.1-mini": None}

    def test_the_workflow_names_a_review_target(self):
        rendered = yaml.safe_dump(_document()["jobs"]["count"])
        assert "MIDTERM_REVIEW_TARGET" in rendered
