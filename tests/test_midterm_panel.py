"""The mid-term panel: its privileged surface, its choke points, its seam.

The mutation tests are the load-bearing ones. A validator that passes the real
workflow proves only that today's file is acceptable; it says nothing about
whether the validator would notice tomorrow's edit. So each forbidden shape is
introduced INTO THE REAL FILE, in memory, and the refusal is asserted — which is
the difference between "the check ran" and "the check works".

`scripts/` goes on the path the way every other suite here does it.
"""

from __future__ import annotations

import inspect
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


def _scratch_journal() -> str:
    """A writable attempt-journal path for a transport built outside a run.

    A live transport now refuses to exist without a durable ledger — a provider
    call whose pre-attempt record could not be written is a call the run could
    never account for. These tests are about the endpoint allowlist and the
    mode gates, not about accounting, so they get a real throwaway ledger
    rather than an exemption from needing one."""
    import tempfile
    return str(Path(tempfile.mkdtemp()) / "midterm" / "provider-attempts.jsonl")

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

    @pytest.mark.parametrize("command", [
        "git cat-file -e ${CANDIDATE_HEAD_SHA}^{commit}",
        "git diff --name-only $CANDIDATE_BASE_SHA $CANDIDATE_HEAD_SHA",
        "git show --stat $CANDIDATE_HEAD_SHA",
        "git ls-tree -r $CANDIDATE_HEAD_SHA",
        "git rev-parse $CANDIDATE_HEAD_SHA",
    ])
    def test_reading_candidate_objects_stays_allowed(self, command, tmp_path):
        """The object/worktree distinction IS the safety argument.

        These commands read the object database and produce no worktree, which
        is exactly the capability the lane needs. If they were refused too, the
        panel could not read the candidate at all and the policy would have
        banned the architecture rather than the hazard.

        (The previously permitted example here was `git fetch`. It is now
        refused — see `TestTheCredentialBoundaryCannotBeEdittedAway` — because
        the credential model, not the object model, ruled it out.)"""
        document = _document()
        document["jobs"]["count"]["steps"].append(
            {"name": "read the candidate as data", "run": command})
        record = _validate(document, tmp_path)
        assert record["tree_materialising_steps"] == 0
        assert record["post_checkout_network_git_steps"] == 0

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
            permitted_paths=(transport.COUNT_PATH,),
            journal=_scratch_journal())

    def _generation(self, **kwargs):
        return transport.MidtermProviderTransport(
            self.Inner(transport.GENERATION_PATH, **kwargs),
            capability="GENERATION_TRANSPORT",
            permitted_paths=(transport.GENERATION_PATH,),
            journal=_scratch_journal())

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
        yields empty — the same failure from the other end.

        Two producers now, not one: `count` and `panel` declare the provider
        attempt counts, which `attemptscli` emits. The union is checked rather
        than preflight alone, because an output produced by either step is
        produced — and an output produced by neither is the defect this
        catches."""
        from midtermpanel import attemptscli, preflightcli
        emitted = set(preflightcli.PUBLIC_OUTPUTS) | set(
            attemptscli.PUBLIC_OUTPUTS)
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

    def test_approved_needs_the_whole_release_binding(self):
        """Digest plus tag used to be enough. It is not.

        Those two identify bytes and a name. They say nothing about the
        identity document that carries the runtime lock, SBOM, provenance
        digest, build run and control class — so a run could call itself
        approved while the operator had never bound the document those facts
        live in. Every member of the binding is now required."""
        complete = {"approved_engine_source_sha": "c" * 40,
                    "approved_engine_protected_sha": "e" * 40,
                    "approved_engine_artifact_sha256": "a" * 64,
                    "approved_engine_release_tag": "v1",
                    "approved_engine_identity_sha256": "b" * 64,
                    "native_branch_protection": False,
                    "control_class": "HUMAN_EXACT_HEAD_COMPENSATING_CONTROL"}
        assert engine.provenance_of(complete) == engine.APPROVED_RELEASE

        # Every single-field omission drops it back to test-only, including
        # the two that used to be the whole rule.
        for field in engine.RELEASE_BINDING_FIELDS:
            partial = {k: v for k, v in complete.items() if k != field}
            assert engine.provenance_of(partial) == engine.REBUILT_TEST_ONLY, (
                f"omitting {field} must not still count as approved")
        assert engine.provenance_of({}) == engine.REBUILT_TEST_ONLY

    def _live(self, capability="COUNT_TRANSPORT", path=None):
        class Inner:
            source = transport.SOURCE_PROVIDER

            def post(self, path, body, *, timeout=None):
                return 200, b"{}"

        return transport.MidtermProviderTransport(
            Inner(), capability=capability,
            permitted_paths=(path or transport.COUNT_PATH,),
            journal=_scratch_journal())

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

        This is the STRING-level half, and it is deliberately negative: it says
        which commands must not appear. The positive half — "the step actually
        works" — cannot be established by looking at a string, and the version
        of this test that tried asserted
        `"git fetch --no-checkout origin" in rendered`. It passed, while the
        command it pinned exits 129 with "unknown option".

        `tests/test_midterm_candidate_objects.py` runs the committed step."""
        job, rendered = self._count_job_env_and_steps()
        assert "git cat-file -e" in rendered
        for producing in ("git checkout", "git merge", "git switch",
                          "git worktree", "git apply"):
            assert producing not in rendered, producing
        for networked in ("git fetch", "git pull", "git clone", "git push"):
            assert networked not in rendered, networked

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
        # The release now EXISTS, so these assertions flipped — and the flip is
        # the point of the activation change. What used to be checked was that
        # the document honestly reported having no approval; what is checked
        # now is that its approval is COMPLETE, because `provenance_of` returns
        # APPROVED_RELEASE only when all seven binding fields are named and a
        # partial binding would authorise a run the operator never sanctioned.
        for field in engine.RELEASE_BINDING_FIELDS:
            assert engine._binding_member_is_present(release[field]), field
        assert engine.provenance_of(release) == engine.APPROVED_RELEASE
        assert release["native_branch_protection"] is False
        assert pins["authority_class"] == (
            "OPERATOR_APPROVED_MIDTERM_POLICY_ATTESTATION")
        assert pins["write_separated"] is False
        assert pins["approved_by"] and pins["approved_at"]
        assert len(pins["pins"]) == 12
        assert sorted(pins["profiles"]) == [
            "HISTORICAL_PR25", "LARGE_PR23", "ROUTINE_PR", "SYNTHETIC"]

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

    def test_no_midterm_workflow_can_be_dispatched(self):
        """F-07. The convenience rerun workflow is gone.

        It held no secret in its committed form and only asked ordinary CI to
        run again. But a `workflow_dispatch` workflow runs a BRANCH-SELECTED
        copy, so with a persistent repository secret in the repository, a
        branch version of it could reference that secret — the same selected-ref
        hazard that removed the dispatch trigger from the privileged panel,
        reached through a convenience.

        Rerunning ordinary CI through the Actions UI, or `gh run rerun`, needs
        no repository workflow at all."""
        workflows = ROOT / ".github" / "workflows"
        assert not (workflows / "midterm-panel-rerun.yml").exists()
        for path in sorted(workflows.glob("midterm-*.yml")):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            triggers = sorted(_on(document))
            if path.name == "midterm-panel-dry-run.yml":
                # The no-key self-test may be dispatched: it holds no secret,
                # refuses to run if one is present under either name, and its
                # branch copy could not obtain a credential to misuse.
                #
                # Checked on the PARSED jobs, not on the file's text: the text
                # contains the words `secrets.` inside the comment explaining
                # that it uses none, and a grep would count that explanation as
                # the thing it describes.
                assert "workflow_dispatch" in triggers
                assert "secrets." not in yaml.safe_dump(document["jobs"])
                continue
            assert "workflow_dispatch" not in triggers, path.name

    def test_no_workflow_holds_actions_write(self):
        """`actions: write` was the removed dispatcher's one elevated scope.
        Nothing in this lane needs it."""
        for path in sorted(
                (ROOT / ".github" / "workflows").glob("midterm-*.yml")):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for scope in (document.get("permissions") or {},):
                assert scope.get("actions") != "write", path.name

    def test_the_policy_document_records_the_removal_and_why(self):
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        assert policy["triggers"]["permitted_events"] == ["workflow_run"]
        manual = policy["triggers"]["manual"]
        assert manual["event"] is None
        assert "SELECTED REF" in " ".join(manual["why"])


class TestTheReviewClassIsClosedAndTotal:
    """F-05. The previous design allowlisted `pr-23`, `pr-25` and `pr-29` while
    the workflow emitted `pr-<number>`, so PR #35 and every routine pull request
    after it would have refused before a panel could run — which contradicts the
    requirement this lane exists for."""

    def _document(self):
        return json.loads(
            (ROOT / "governance" / "midterm-panel-pins.json")
            .read_text(encoding="utf-8"))

    def test_every_pull_request_number_maps_to_a_class(self):
        """Total. A new pull request reviews rather than refusing."""
        from midtermpanel.preflight import REVIEW_CLASSES, review_class_for
        for number in (1, 23, 25, 29, 35, 100, 99999):
            assert review_class_for(number) in REVIEW_CLASSES, number

    def test_the_two_special_pull_requests_keep_their_own_classes(self):
        from midtermpanel.preflight import review_class_for
        assert review_class_for(25) == "HISTORICAL_PR25"
        assert review_class_for(23) == "LARGE_PR23"

    def test_everything_else_is_routine(self):
        from midtermpanel.preflight import review_class_for
        assert review_class_for(29) == "ROUTINE_PR"
        assert review_class_for(35) == "ROUTINE_PR"

    def test_a_malformed_pull_request_number_is_refused(self):
        from midtermpanel.preflight import review_class_for
        for bad in (0, -1, True, "29", None):
            with pytest.raises(PanelRefusal) as caught:
                review_class_for(bad)
            assert "not_a_positive_integer" in caught.value.reason

    def test_every_class_the_code_can_produce_has_a_profile(self):
        """Otherwise a real pull request refuses at spend time."""
        from midtermpanel.preflight import REVIEW_CLASSES
        assert sorted(self._document()["profiles"]) == sorted(REVIEW_CLASSES)

    def test_an_unnamed_class_is_refused(self):
        from midtermpanel import inputs
        with pytest.raises(PanelRefusal) as caught:
            inputs.select_profile(self._document(), environ={})
        assert "review_class_not_named" in caught.value.reason

    def test_a_class_that_is_not_one_of_the_four_is_refused(self):
        from midtermpanel import inputs
        with pytest.raises(PanelRefusal) as caught:
            inputs.select_profile(
                self._document(),
                environ={"MIDTERM_REVIEW_CLASS": "GENEROUS_PR"})
        assert "review_class_unknown" in caught.value.reason

    def test_each_class_selects_its_own_caps(self):
        from midtermpanel import inputs
        expected = {"SYNTHETIC": (100, 0, 5000000),
                    "HISTORICAL_PR25": (100, 0, 5000000),
                    "ROUTINE_PR": (1200, 80, 25000000),
                    "LARGE_PR23": (2500, 200, 60000000)}
        for name, (count, generation, cost) in expected.items():
            pins, profile = inputs.select_profile(
                self._document(), environ={"MIDTERM_REVIEW_CLASS": name})
            assert pins["VERIFIER_MAX_COUNT_CALLS"] == count, name
            assert pins["VERIFIER_MAX_GENERATION_CALLS"] == generation, name
            assert pins["VERIFIER_COST_CAP_MICRO_USD"] == cost, name
            assert profile["profile_digest"]

    def test_the_routine_profile_is_operator_approved(self):
        """It authorises spend on every future pull request, so it may not be
        a value this repository chose for itself."""
        routine = self._document()["profiles"]["ROUTINE_PR"]
        assert routine["approved_by"] == "mglaeser"
        assert routine["approved_at"]
        assert routine["VERIFIER_MAX_COUNT_CALLS"] == 1200
        assert routine["VERIFIER_MAX_GENERATION_CALLS"] == 80
        assert routine["VERIFIER_COST_CAP_MICRO_USD"] == 25000000
        assert routine["authorized_total_input_tokens"] == 2000000

    def test_the_synthetic_class_cannot_generate_at_all(self):
        from midtermpanel import inputs
        pins, _ = inputs.select_profile(
            self._document(), environ={"MIDTERM_REVIEW_CLASS": "SYNTHETIC"})
        assert pins["VERIFIER_MAX_GENERATION_CALLS"] == 0

    def test_a_profile_missing_an_overridden_pin_is_refused(self):
        from midtermpanel import inputs
        document = self._document()
        del document["profiles"]["ROUTINE_PR"]["VERIFIER_COST_CAP_MICRO_USD"]
        with pytest.raises(PanelRefusal) as caught:
            inputs.select_profile(
                document, environ={"MIDTERM_REVIEW_CLASS": "ROUTINE_PR"})
        assert "profile_missing_an_overridden_pin" in caught.value.reason


class TestTransportCeilingsLiveInTheProfile:
    """F-06. `MIDTERM_GENERATION_ATTEMPT_CAP=60` was a free-standing workflow
    literal that contradicted the approved cap of 80. A PR-29 plan projecting
    ~78 attempts would have been priced for 80 and stopped at 60, under a limit
    no operator wrote, reported as an exhausted budget."""

    def _document(self):
        return json.loads(
            (ROOT / "governance" / "midterm-panel-pins.json")
            .read_text(encoding="utf-8"))

    def test_the_ceilings_come_from_the_selected_profile(self):
        from midtermpanel import inputs
        _, profile = inputs.select_profile(
            self._document(), environ={"MIDTERM_REVIEW_CLASS": "ROUTINE_PR"})
        assert profile["generation_attempt_cap"] == 80
        assert profile["authorized_total_input_tokens"] == 2000000

    def test_a_cap_below_the_approved_generation_calls_is_refused(self):
        """The exact defect: priced for 80, stopped at 60."""
        from midtermpanel import inputs
        document = self._document()
        document["profiles"]["ROUTINE_PR"]["generation_attempt_cap"] = 60
        with pytest.raises(PanelRefusal) as caught:
            inputs.select_profile(
                document, environ={"MIDTERM_REVIEW_CLASS": "ROUTINE_PR"})
        assert "transport_cap_below_approved_generation_calls" in \
            caught.value.reason
        assert "attempt_cap=60" in caught.value.reason
        assert "approved_calls=80" in caught.value.reason

    def test_a_missing_ceiling_is_refused(self):
        from midtermpanel import inputs
        document = self._document()
        del document["profiles"]["ROUTINE_PR"]["authorized_total_input_tokens"]
        with pytest.raises(PanelRefusal) as caught:
            inputs.select_profile(
                document, environ={"MIDTERM_REVIEW_CLASS": "ROUTINE_PR"})
        assert "profile_transport_ceiling_missing_or_invalid" in \
            caught.value.reason

    def test_a_zero_generation_profile_may_have_a_positive_cap(self):
        """The one permitted asymmetry, in the safe direction: zero approved
        calls can never generate whatever the transport allows."""
        from midtermpanel import inputs
        _, profile = inputs.select_profile(
            self._document(), environ={"MIDTERM_REVIEW_CLASS": "SYNTHETIC"})
        assert profile["max_generation_calls"] == 0
        assert profile["generation_attempt_cap"] >= 1

    def test_the_workflow_carries_no_budget_literals(self):
        rendered = yaml.safe_dump(_document()["jobs"])
        for retired in ("MIDTERM_AUTHORIZED_INPUT_TOKENS:",
                        "MIDTERM_GENERATION_ATTEMPT_CAP:"):
            assert retired not in rendered, retired

    def test_the_workflow_passes_the_review_class_preflight_derived(self):
        rendered = yaml.safe_dump(_document()["jobs"]["count"])
        assert "MIDTERM_REVIEW_CLASS" in rendered
        assert "review_class" in rendered


class TestOrdinaryCIMustHaveTestedThisExactCombination:
    """F-01. The previous check was `assert_base_is_current(pr_base_sha=...,
    main_head_sha=...)` and its docstring said "main must not have moved past
    the base ordinary CI actually tested" — while reading neither. Both values
    described the world now: the PR object's `base.sha` tracks the branch, so
    when main advanced from B1 to B2 the PR's base became B2, current main was
    B2, and the comparison passed. The scenario it was written to catch was the
    one scenario it could not catch."""

    RUN_ID = 30857566024
    HEAD_A = "a" * 40
    BASE_1 = "1" * 40
    BASE_2 = "2" * 40

    def _run(self, **overrides):
        run = {"id": self.RUN_ID, "name": "ci", "event": "pull_request",
               "conclusion": "success", "run_attempt": 1,
               "pull_requests": [{"head": {"sha": self.HEAD_A},
                                  "base": {"sha": self.BASE_1}}]}
        run.update(overrides)
        return run

    def _jobs(self, **conclusions):
        return [{"name": name, "status": "completed",
                 "conclusion": conclusions.get(name, "success")}
                for name in ("test (3.12)", "image")]

    def _assert(self, run=None, jobs=None, **overrides):
        from midtermpanel.preflight import (
            assert_triggering_ci_tested_this_exact_combination,
        )
        arguments = {"event_run_id": self.RUN_ID,
                     "event_head_sha": self.HEAD_A,
                     "current_head_sha": self.HEAD_A,
                     "current_base_sha": self.BASE_1,
                     "main_head_sha": self.BASE_1}
        arguments.update(overrides)
        return assert_triggering_ci_tested_this_exact_combination(
            run if run is not None else self._run(),
            jobs if jobs is not None else self._jobs(), **arguments)

    def test_the_matching_combination_passes_and_reports_what_was_tested(self):
        record = self._assert()
        assert record["tested_head_sha"] == self.HEAD_A
        assert record["tested_base_sha"] == self.BASE_1
        assert record["triggering_ci_run_id"] == self.RUN_ID
        assert record["triggering_ci_run_attempt"] == 1

    def test_main_moving_after_ci_blocks_even_though_both_now_agree(self):
        """The exact defect. CI tested B1; main is B2; the PR object's base has
        followed main to B2. The old check compared B2 to B2 and passed."""
        with pytest.raises(PanelRefusal) as caught:
            self._assert(current_base_sha=self.BASE_2,
                         main_head_sha=self.BASE_2)
        assert "ordinary_ci_tested_a_different_combination" in \
            caught.value.reason
        assert "tested_base" in caught.value.reason

    def test_main_moving_while_the_pr_base_lags_also_blocks(self):
        with pytest.raises(PanelRefusal) as caught:
            self._assert(main_head_sha=self.BASE_2)
        assert "ordinary_ci_tested_a_different_combination" in \
            caught.value.reason

    def test_a_push_since_ci_blocks(self):
        with pytest.raises(PanelRefusal) as caught:
            self._assert(current_head_sha="b" * 40)
        assert "ordinary_ci_tested_a_different_combination" in \
            caught.value.reason

    def test_the_run_read_must_be_the_run_that_fired(self):
        with pytest.raises(PanelRefusal) as caught:
            self._assert(run=self._run(id=999))
        assert "triggering_run_id_mismatch" in caught.value.reason

    def test_a_run_for_another_pull_request_blocks(self):
        other = self._run(pull_requests=[{"head": {"sha": "c" * 40},
                                          "base": {"sha": self.BASE_1}}])
        with pytest.raises(PanelRefusal) as caught:
            self._assert(run=other)
        assert "ordinary_ci_tested_a_different_combination" in \
            caught.value.reason

    def test_a_run_belonging_to_no_pull_request_blocks(self):
        """Zero means the tested base cannot be recovered at all."""
        with pytest.raises(PanelRefusal) as caught:
            self._assert(run=self._run(pull_requests=[]))
        assert "pull_request_not_unique" in caught.value.reason

    def test_a_run_belonging_to_several_pull_requests_blocks(self):
        pulls = [{"head": {"sha": self.HEAD_A}, "base": {"sha": self.BASE_1}},
                 {"head": {"sha": self.HEAD_A}, "base": {"sha": self.BASE_2}}]
        with pytest.raises(PanelRefusal) as caught:
            self._assert(run=self._run(pull_requests=pulls))
        assert "pull_request_not_unique" in caught.value.reason

    def test_a_push_event_run_blocks(self):
        """A push run tests the branch tip, not the merge a pull request would
        produce."""
        with pytest.raises(PanelRefusal) as caught:
            self._assert(run=self._run(event="push"))
        assert "triggering_run_wrong_event" in caught.value.reason

    def test_a_run_of_another_workflow_blocks(self):
        with pytest.raises(PanelRefusal) as caught:
            self._assert(run=self._run(name="something-else"))
        assert "triggering_run_wrong_workflow" in caught.value.reason

    def test_a_failed_ci_run_blocks(self):
        with pytest.raises(PanelRefusal) as caught:
            self._assert(run=self._run(conclusion="failure"))
        assert "triggering_run_not_successful" in caught.value.reason

    def test_test_green_but_image_red_blocks(self):
        with pytest.raises(PanelRefusal) as caught:
            self._assert(jobs=self._jobs(image="failure"))
        assert "triggering_run_job_not_successful" in caught.value.reason

    def test_test_green_but_image_absent_blocks(self):
        only_test = [j for j in self._jobs() if j["name"] == "test (3.12)"]
        with pytest.raises(PanelRefusal) as caught:
            self._assert(jobs=only_test)
        assert "triggering_run_missing_required_job" in caught.value.reason

    def test_an_incomplete_job_is_not_a_green_one(self):
        """Still refused — under its OWN category now, and that split matters.

        "The job has not finished" and "the job finished badly" were reported
        as one thing: `triggering_run_job_not_successful jobs=['test
        (3.12)=incomplete']`, which reads as a failure. They are now separate
        because `observation.settle` may wait for the first and must never wait
        for the second; a retry loop that could not tell them apart would be a
        loop that eventually reported the answer it wanted."""
        jobs = self._jobs()
        jobs[0]["status"] = "in_progress"
        jobs[0]["conclusion"] = None
        with pytest.raises(PanelRefusal) as caught:
            self._assert(jobs=jobs)
        assert "triggering_run_job_incomplete" in caught.value.reason
        assert "triggering_run_job_not_successful" not in caught.value.reason

    def test_the_two_job_refusals_land_on_opposite_sides_of_the_retry_rule(self):
        from midtermpanel import observation
        assert "triggering_run_job_incomplete" in (
            observation.RETRYABLE_CATEGORIES)
        assert "triggering_run_job_not_successful" in observation.NEVER_RETRIED
        assert not (observation.RETRYABLE_CATEGORIES
                    & observation.NEVER_RETRIED)

    def test_the_workflow_publishes_what_ci_tested(self):
        document = _document()
        outputs = document["jobs"]["preflight"]["outputs"]
        for name in ("triggering_ci_run_id", "triggering_ci_run_attempt",
                     "tested_base_sha", "tested_head_sha", "review_class"):
            assert name in outputs, name
        for job in ("count", "panel"):
            rendered = yaml.safe_dump(document["jobs"][job])
            assert "MIDTERM_TESTED_BASE_SHA" in rendered, job
            assert "MIDTERM_TRIGGERING_CI_RUN_ID" in rendered, job


# A commit id, not a credential — same reason as HEAD above.
CANDIDATE_HEAD = "9" * 40
CANDIDATE_BASE = "8" * 40
APPROVED_SOURCE = "7" * 40
APPROVED_PROTECTED = "6" * 40
CI_RUN_ID = 30991234567


def _pull(**overrides):
    """An open, same-repository pull request as the API returns one."""
    pull = {
        "number": 34,
        "state": "open",
        "merged_at": None,
        "head": {"sha": CANDIDATE_HEAD,
                 "repo": {"id": mp.REPOSITORY_NUMERIC_ID,
                          "full_name": "mglaeser/bubble-regime-monitor"}},
        "base": {"ref": "main", "sha": CANDIDATE_BASE},
    }
    pull.update(overrides)
    return pull


class _RecordingApi:
    """A read-only client that records WHICH reads happened, in order.

    The order is the point. "The fork is refused" and "the fork is refused
    before anything else is read" are different claims, and only the second one
    says the refusal happens before the job that holds the provider key could
    have started."""

    def __init__(self, *, pulls=None, run=None, jobs=None, main_head=None,
                 files=None):
        self.calls = []
        self._pulls = [_pull()] if pulls is None else pulls
        self._run = run if run is not None else {
            "id": CI_RUN_ID, "name": "ci", "event": "pull_request",
            "conclusion": "success", "run_attempt": 1,
            "pull_requests": [{"head": {"sha": CANDIDATE_HEAD},
                               "base": {"sha": CANDIDATE_BASE}}]}
        self._jobs = jobs if jobs is not None else [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in ("test (3.12)", "image")]
        self._main_head = main_head or CANDIDATE_BASE
        self._files = files if files is not None else ["app/compute.py"]

    def open_pull_requests(self):
        self.calls.append("pulls")
        return self._pulls

    def workflow_run(self, run_id):
        self.calls.append("workflow-run")
        return self._run

    def workflow_run_jobs(self, run_id):
        self.calls.append("workflow-run-jobs")
        return self._jobs

    def default_branch_head(self):
        self.calls.append("branches/main")
        return self._main_head

    def check_runs(self, head_sha):
        self.calls.append("check-runs")
        from midtermpanel.preflight import REQUIRED_ORDINARY_CHECKS
        return [{"name": name, "status": "completed", "conclusion": "success",
                 "head_sha": head_sha, "id": 100 + index,
                 "completed_at": f"2026-08-04T10:0{index}:00Z"}
                for index, name in enumerate(REQUIRED_ORDINARY_CHECKS)]

    def changed_files(self, pr_number):
        self.calls.append("pull-files")
        return self._files


def _preflight_environ(**overrides):
    environ = {
        "TRIGGER_EVENT": "workflow_run",
        "RUN_WORKFLOW_NAME": "ci",
        "RUN_EVENT": "pull_request",
        "RUN_CONCLUSION": "success",
        "RUN_HEAD_SHA": CANDIDATE_HEAD,
        "RUN_ID": str(CI_RUN_ID),
        "MIDTERM_APPROVED_ENGINE_SOURCE_SHA": APPROVED_SOURCE,
        "MIDTERM_APPROVED_ENGINE_PROTECTED_SHA": APPROVED_PROTECTED,
    }
    environ.update(overrides)
    return environ


class TestTheCandidateMustLiveInThisRepository:
    """§2.1. The precondition the whole acquisition model rests on.

    `persist-credentials: false` means no later step can authenticate to this
    private origin, so the candidate's objects must already be present. They
    are — `fetch-depth: 0` brings every BRANCH in — and a fork's head is not a
    branch here. That gap is refused explicitly, in preflight, before any job
    that holds the provider key starts, rather than being half-supported and
    discovered by a count that dies with the key already read."""

    def _assert(self, pull):
        from midtermpanel.preflight import assert_candidate_is_same_repository
        return assert_candidate_is_same_repository(pull)

    def test_a_same_repository_pull_request_passes_and_names_the_id(self):
        record = self._assert(_pull())
        assert record["candidate_repository_numeric_id"] == \
            mp.REPOSITORY_NUMERIC_ID
        assert record["candidate_is_same_repository"] is True

    def test_a_fork_is_refused(self):
        forked = _pull(head={"sha": CANDIDATE_HEAD,
                             "repo": {"id": 999999,
                                      "full_name": "someone/fork"}})
        with pytest.raises(PanelRefusal) as caught:
            self._assert(forked)
        assert "candidate_repository_not_the_reviewed_repository" in \
            caught.value.reason

    def test_a_matching_name_with_a_different_id_is_still_refused(self):
        """The reason the check reads the NUMERIC id.

        A repository can be renamed and the name reused, so a fork sitting at
        `mglaeser/bubble-regime-monitor` is a thing that can exist. The id
        cannot be reused, which is why the trusted lane pins it too."""
        impostor = _pull(head={
            "sha": CANDIDATE_HEAD,
            "repo": {"id": mp.REPOSITORY_NUMERIC_ID + 1,
                     "full_name": "mglaeser/bubble-regime-monitor"}})
        with pytest.raises(PanelRefusal) as caught:
            self._assert(impostor)
        assert "candidate_repository_not_the_reviewed_repository" in \
            caught.value.reason
        assert str(mp.REPOSITORY_NUMERIC_ID) in caught.value.reason

    @pytest.mark.parametrize("head", [
        {"sha": CANDIDATE_HEAD},                       # no repo key at all
        {"sha": CANDIDATE_HEAD, "repo": None},         # deleted fork source
        {"sha": CANDIDATE_HEAD, "repo": "mglaeser/bubble-regime-monitor"},
    ])
    def test_an_unidentifiable_head_repository_is_refused(self, head):
        with pytest.raises(PanelRefusal) as caught:
            self._assert(_pull(head=head))
        assert "candidate_head_repository_absent" in caught.value.reason

    @pytest.mark.parametrize("bad_id", [
        str(mp.REPOSITORY_NUMERIC_ID),   # the right value, wrong type
        None,
        True,                            # bool is an int in Python; not here
        1.0,
    ])
    def test_a_malformed_repository_id_is_refused(self, bad_id):
        """Refused rather than coerced. `int("1297332828")` would pass and
        `int(True)` is 1, and a check that repairs its input is a check that
        has stopped reading it."""
        with pytest.raises(PanelRefusal) as caught:
            self._assert(_pull(head={"sha": CANDIDATE_HEAD,
                                     "repo": {"id": bad_id}}))
        assert "candidate_head_repository_id_malformed" in caught.value.reason

    def test_resolve_pull_request_publishes_the_repository_it_verified(self):
        from midtermpanel.preflight import resolve_pull_request
        record = resolve_pull_request([_pull()], run_head_sha=CANDIDATE_HEAD)
        assert record["candidate_is_same_repository"] is True
        assert record["candidate_repository_numeric_id"] == \
            mp.REPOSITORY_NUMERIC_ID
        assert record["head_sha"] == CANDIDATE_HEAD
        assert record["base_sha"] == CANDIDATE_BASE

    def test_resolve_pull_request_refuses_a_fork(self):
        from midtermpanel.preflight import resolve_pull_request
        forked = _pull(head={"sha": CANDIDATE_HEAD, "repo": {"id": 42}})
        with pytest.raises(PanelRefusal):
            resolve_pull_request([forked], run_head_sha=CANDIDATE_HEAD)


class TestPreflightStillDecidesEverythingElseCorrectly:
    """The identity check is new; it must not have displaced what was there."""

    def test_the_happy_path_proceeds_and_publishes_what_ci_tested(self):
        from midtermpanel import preflightcli
        api = _RecordingApi()
        decision = preflightcli.decide(_preflight_environ(), api=api,
                                       root=str(ROOT))
        assert decision["proceed"] is True
        assert decision["pr_number"] == 34
        assert decision["head_sha"] == CANDIDATE_HEAD
        assert decision["base_sha"] == CANDIDATE_BASE
        assert decision["tested_head_sha"] == CANDIDATE_HEAD
        assert decision["tested_base_sha"] == CANDIDATE_BASE
        assert decision["triggering_ci_run_id"] == CI_RUN_ID
        assert decision["review_class"] == "ROUTINE_PR"

    def test_every_declared_output_is_actually_produced(self):
        """An undeclared output resolves to the empty string and errors
        nothing. So does a declared one the CLI forgets to emit."""
        from midtermpanel import preflightcli
        decision = preflightcli.decide(_preflight_environ(),
                                       api=_RecordingApi(), root=str(ROOT))
        public = {k: v for k, v in decision.items() if not k.startswith("_")}
        assert set(public) == set(preflightcli.PUBLIC_OUTPUTS)
        for name, value in public.items():
            assert value != "", name

    def test_the_engine_identity_is_the_approved_release_not_the_candidate(self):
        from midtermpanel import preflightcli
        from midtermpanel.engine import engine_digest, source_roles
        decision = preflightcli.decide(_preflight_environ(),
                                       api=_RecordingApi(), root=str(ROOT))
        assert decision["engine_digest"] == engine_digest(roles=source_roles({
            "approved_engine_source_sha": APPROVED_SOURCE,
            "approved_engine_protected_sha": APPROVED_PROTECTED}))

    def test_an_engine_pinned_to_the_candidate_head_still_blocks(self):
        from midtermpanel import preflightcli
        with pytest.raises(PanelRefusal) as caught:
            preflightcli.decide(
                _preflight_environ(
                    MIDTERM_APPROVED_ENGINE_SOURCE_SHA=CANDIDATE_HEAD),
                api=_RecordingApi(), root=str(ROOT))
        assert "engine_source_is_the_reviewed_candidate" in caught.value.reason


class TestTheForkRefusalHappensBeforeAnyCredentialCouldBeRead:
    """§2.1, the ordering half.

    A refusal that happens after the count job started is not a refusal that
    protects anything: by then the provider key has been read into a process."""

    def _fork_api(self):
        return _RecordingApi(pulls=[_pull(
            head={"sha": CANDIDATE_HEAD,
                  "repo": {"id": 24680, "full_name": "someone/fork"}})])

    def test_the_decision_refuses(self):
        from midtermpanel import preflightcli
        with pytest.raises(PanelRefusal) as caught:
            preflightcli.decide(_preflight_environ(), api=self._fork_api(),
                                root=str(ROOT))
        assert "candidate_repository_not_the_reviewed_repository" in \
            caught.value.reason

    def test_it_refuses_on_the_first_read_and_makes_no_others(self):
        from midtermpanel import preflightcli
        api = self._fork_api()
        with pytest.raises(PanelRefusal):
            preflightcli.decide(_preflight_environ(), api=api, root=str(ROOT))
        assert api.calls == ["pulls"], (
            "the fork must be refused as soon as the pull request is read, "
            f"before CI or file reads: {api.calls}")

    def test_the_deciding_job_holds_no_secret_at_all(self):
        """Structural companion: the refusal ordering only matters because the
        job it happens in never had a credential to lose."""
        document = _document()
        assert "secrets." not in yaml.safe_dump(document["jobs"]["preflight"])
        record = privilegedworkflow.validate(root=str(ROOT))
        assert record["secret_bearing_jobs"] == ["count", "panel"]


class TestTheCredentialBoundaryCannotBeEdittedAway:
    """§2.2. Static guards over the privileged workflow, each mutated INTO the
    real file so the assertion is "the validator would notice", not "the
    validator ran"."""

    def test_the_committed_workflow_satisfies_both_new_guards(self):
        record = privilegedworkflow.validate(root=str(ROOT))
        assert record["post_checkout_network_git_steps"] == 0
        assert record["checkouts_persisting_credentials"] == 0
        assert record["credential_reintroduction_steps"] == 0

    @pytest.mark.parametrize("command", [
        'git fetch origin "$CANDIDATE_HEAD_SHA"',
        "git fetch --no-tags --no-recurse-submodules origin $SHA:refs/x",
        "git pull origin main",
        "git clone https://github.com/mglaeser/bubble-regime-monitor /tmp/c",
        "git remote add candidate https://example.invalid/x.git",
        "git ls-remote origin",
        "git submodule update --init",
        "git push origin HEAD",
    ])
    def test_a_post_checkout_network_git_command_is_refused(self, command,
                                                            tmp_path):
        document = _document()
        document["jobs"]["count"]["steps"].append(
            {"name": "reach the network", "run": command})
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "fetches_after_checkout" in caught.value.reason

    @pytest.mark.parametrize("job", ["preflight", "count", "panel",
                                     "finalize"])
    def test_persisting_the_checkout_credential_is_refused(self, job,
                                                           tmp_path):
        document = _document()
        document["jobs"][job]["steps"][0]["with"]["persist-credentials"] = True
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "persists_credentials" in caught.value.reason

    def test_omitting_persist_credentials_is_refused_too(self, tmp_path):
        """The default is to KEEP the token, so silence is the hazard.

        A guard that only rejected the literal `true` would pass a checkout
        that simply stopped saying anything, which is the shape an edit
        actually takes."""
        document = _document()
        document["jobs"]["count"]["steps"][0]["with"].pop("persist-credentials")
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "persists_credentials" in caught.value.reason

    def test_dropping_the_with_block_entirely_is_refused(self, tmp_path):
        document = _document()
        document["jobs"]["count"]["steps"][0].pop("with")
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "persists_credentials" in caught.value.reason

    @pytest.mark.parametrize("command", [
        'git config --global credential.helper "!f() { echo password=$T; }; f"',
        'git config http.extraheader "AUTHORIZATION: basic $B64"',
        'git config --add http.https://github.com/.extraheader "x"',
        'export GIT_ASKPASS=/tmp/askpass.sh',
        'git remote set-url origin https://x-access-token:$T@github.com/o/r',
    ])
    def test_writing_a_credential_back_into_git_is_refused(self, command,
                                                           tmp_path):
        """The other way to get a usable token: not asking checkout to keep
        one, but putting one back afterwards."""
        document = _document()
        document["jobs"]["panel"]["steps"].append(
            {"name": "re-credential", "run": command})
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert ("reintroduces_a_git_credential" in caught.value.reason
                or "fetches_after_checkout" in caught.value.reason)

    @pytest.mark.parametrize("command", [
        "git checkout $CANDIDATE_HEAD_SHA",
        "git switch --detach $CANDIDATE_HEAD_SHA",
        "git merge $CANDIDATE_HEAD_SHA",
        "git worktree add /tmp/candidate $CANDIDATE_HEAD_SHA",
        "git apply /tmp/candidate.patch",
    ])
    def test_turning_the_candidate_into_a_worktree_is_still_refused(
            self, command, tmp_path):
        document = _document()
        document["jobs"]["count"]["steps"].append(
            {"name": "materialise", "run": command})
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "materialises_a_tree" in caught.value.reason

    def test_the_guards_are_wired_into_validate_and_not_merely_defined(self):
        """A written check nobody calls is the failure mode this whole lane
        keeps finding. Asserted by AST over `validate()` rather than by
        searching the file, because the module also NAMES these functions in
        prose."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(privilegedworkflow.validate))
        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name)}
        assert "assert_no_post_checkout_network_git" in called
        assert "assert_no_credential_reintroduction" in called


class TestTheseGuardsWouldReddenIfRemoved:
    """§2.4. Necessity, not just sufficiency: each guard is disabled and the
    forbidden shape is shown to become acceptable.

    Without this, a guard that silently stopped matching — a renamed key, a
    tuple that lost an entry — would leave every test above green."""

    def test_without_the_identity_check_a_fork_is_accepted(self, monkeypatch):
        from midtermpanel import preflight
        forked = _pull(head={"sha": CANDIDATE_HEAD,
                             "repo": {"id": 13579, "full_name": "o/fork"}})
        with pytest.raises(PanelRefusal):
            preflight.resolve_pull_request([forked],
                                           run_head_sha=CANDIDATE_HEAD)
        monkeypatch.setattr(preflight, "assert_candidate_is_same_repository",
                            lambda pull: {})
        accepted = preflight.resolve_pull_request([forked],
                                                  run_head_sha=CANDIDATE_HEAD)
        assert accepted["head_sha"] == CANDIDATE_HEAD
        assert "candidate_is_same_repository" not in accepted

    def test_without_the_command_list_a_fetch_is_accepted(self, monkeypatch,
                                                          tmp_path):
        document = _document()
        document["jobs"]["count"]["steps"].append(
            {"name": "fetch", "run": 'git fetch origin "$SHA"'})
        with pytest.raises(PanelRefusal):
            _validate(document, tmp_path)
        monkeypatch.setattr(privilegedworkflow,
                            "POST_CHECKOUT_NETWORK_COMMANDS", ())
        assert _validate(document, tmp_path)[
            "post_checkout_network_git_steps"] == 0

    def test_without_the_persistence_check_a_kept_token_is_accepted(
            self, monkeypatch, tmp_path):
        """Note WHICH function is disabled here: `assert_no_candidate_checkout`.

        The persistence rule lives there, with the other rules about how a
        checkout is shaped, and it lives there ONLY. This round nearly added a
        second copy in a new function — a duplicated rule is a rule that can
        disagree with itself, which is how the panel's `aggregate` once blocked
        every review by contradicting the engine's role gate. Pointing this
        test at the real owner is what keeps that collapse from silently
        undoing itself."""
        document = _document()
        document["jobs"]["count"]["steps"][0]["with"]["persist-credentials"] = True
        with pytest.raises(PanelRefusal):
            _validate(document, tmp_path)
        monkeypatch.setattr(privilegedworkflow, "assert_no_candidate_checkout",
                            lambda document: {
                                "checkout_steps": ["disabled"],
                                "checkouts_persisting_credentials": 0})
        assert _validate(document, tmp_path)[
            "checkouts_persisting_credentials"] == 0

    def test_the_persistence_rule_has_exactly_one_implementation(self):
        """Stated as a test so the collapse cannot quietly reverse.

        Counted over each function's non-docstring string literals, not over
        the file's text and not over its whole AST: this module explains the
        rule in prose in three places. A grep counts each explanation as
        another implementation, and so does `ast.dump`, which includes the
        docstring — the same "the comment describing the defect is read as the
        defect" trap this lane keeps walking into."""
        import ast
        import inspect

        def code_strings(function):
            doc = None
            first = function.body[0] if function.body else None
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                doc = first.value
            for node in ast.walk(function):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and node is not doc):
                    yield node.value

        tree = ast.parse(inspect.getsource(privilegedworkflow))
        owners = [node.name for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef)
                  and any("persist-credentials" in text
                          for text in code_strings(node))]
        assert owners == ["assert_no_candidate_checkout"], owners

    def test_without_the_marker_list_a_re_credentialled_git_is_accepted(
            self, monkeypatch, tmp_path):
        document = _document()
        document["jobs"]["panel"]["steps"].append(
            {"name": "re-credential",
             "run": 'git config http.extraheader "AUTHORIZATION: basic $B"'})
        with pytest.raises(PanelRefusal):
            _validate(document, tmp_path)
        monkeypatch.setattr(privilegedworkflow,
                            "CREDENTIAL_REINTRODUCTION_MARKERS", ())
        assert _validate(document, tmp_path)[
            "credential_reintroduction_steps"] == 0


class TestEveryWorkflowStepNamesSomethingThatExists:
    """A step that runs a file which is not there fails on the runner, minutes
    into a job, for a reason that has nothing to do with what it was testing.

    This exists because renaming `tests/test_midterm_fetch_shell.py` to
    `tests/test_midterm_candidate_objects.py` left the dry-run workflow calling
    the old path. The local suite stayed green — it does not read workflow
    files looking for pytest invocations — and the break would have surfaced
    only as a red hosted job."""

    def _midterm_workflows(self):
        return sorted((ROOT / ".github" / "workflows").glob("midterm-*.yml"))

    def test_every_pytest_target_exists(self):
        import re
        pattern = re.compile(r"(tests/[\w./-]+\.py)")
        seen = 0
        for path in self._midterm_workflows():
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in (document.get("jobs") or {}).values():
                for step in (job or {}).get("steps") or []:
                    for target in pattern.findall(str(step.get("run") or "")):
                        seen += 1
                        assert (ROOT / target).is_file(), (
                            f"{path.name} runs {target}, which does not exist")
        assert seen > 0, (
            "no pytest target was found in any midterm workflow; this guard "
            "must not be able to pass by covering nothing")

    def test_every_referenced_governance_document_exists(self):
        import re
        pattern = re.compile(r"(governance/[\w./-]+\.json)")
        seen = 0
        for path in self._midterm_workflows():
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in (document.get("jobs") or {}).values():
                for step in (job or {}).get("steps") or []:
                    for target in pattern.findall(str(step.get("run") or "")):
                        seen += 1
                        assert (ROOT / target).is_file(), (
                            f"{path.name} reads {target}, which does not exist")
        assert seen > 0

    def test_every_run_block_is_valid_bash(self):
        """Syntax, checked by bash itself.

        `git fetch --no-checkout` was accepted by every reader in this
        repository because it IS valid shell — but the class of defect it
        belongs to (a committed command nobody executed) includes plain syntax
        errors, and those cost a hosted run to discover."""
        import subprocess
        checked = 0
        for path in self._midterm_workflows():
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job_id, job in (document.get("jobs") or {}).items():
                for index, step in enumerate((job or {}).get("steps") or []):
                    script = str(step.get("run") or "")
                    if not script.strip():
                        continue
                    checked += 1
                    result = subprocess.run(
                        ["bash", "-n"], input=script, text=True,
                        capture_output=True)
                    assert result.returncode == 0, (
                        f"{path.name}:{job_id}.steps[{index}] is not valid "
                        f"bash:\n{result.stderr}")
        assert checked > 0


class TestPreflightCanActuallyReadTheChecksItVerifies:
    """The first real privileged run refused with

        category=github_api_error where=check-runs http_status=403

    `assert_ordinary_checks_green` reads `GET /commits/{sha}/check-runs`, which
    needs `checks: read`. The workflow granted `contents`, `actions`,
    `pull-requests` and `statuses` — and an explicit `permissions:` block sets
    every UNLISTED scope to `none`, so the omission did not fall back to a
    default, it REVOKED the scope. Preflight could never have passed.

    Nothing caught it because the privileged workflow had never run: the static
    validator compared the block to a constant that was itself missing the
    scope, so the file and the rule agreed with each other and both were
    wrong."""

    def test_the_workflow_grants_checks_read(self):
        document = _document()
        assert document["permissions"]["checks"] == "read"

    def test_every_job_that_reads_check_runs_grants_it(self):
        """Job-level blocks override the workflow default entirely."""
        document = _document()
        for job in ("preflight", "count", "panel", "finalize"):
            block = document["jobs"][job].get("permissions")
            if block is None:
                continue
            assert block.get("checks") == "read", job

    def test_the_required_set_names_every_scope_the_code_calls(self):
        """The constant and the file must not simply agree with each other.

        Derived from what `ReadOnlyGitHub` actually requests: a reader that
        hits `/check-runs` needs `checks`, one that hits `/actions/runs` needs
        `actions`, `/pulls` needs `pull-requests`."""
        from midtermpanel import githubapi
        source = inspect.getsource(githubapi)
        needed = set()
        if "check-runs" in source:
            needed.add("checks")
        if "/actions/runs" in source:
            needed.add("actions")
        if "/pulls" in source:
            needed.add("pull-requests")
        if "/commits/" in source and "statuses" in source:
            needed.add("statuses")
        granted = set(privilegedworkflow.REQUIRED_PERMISSIONS)
        assert needed <= granted, sorted(needed - granted)

    def test_dropping_checks_read_is_refused(self, tmp_path):
        document = _document()
        del document["permissions"]["checks"]
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "permissions_mismatch" in caught.value.reason

    def test_checks_write_is_refused(self, tmp_path):
        """A read the panel needs must not become a write it does not."""
        document = _document()
        document["permissions"]["checks"] = "write"
        with pytest.raises(PanelRefusal) as caught:
            _validate(document, tmp_path)
        assert "permissions_mismatch" in caught.value.reason

    def test_statuses_write_is_still_the_only_write(self):
        writes = [k for k, v in
                  privilegedworkflow.REQUIRED_PERMISSIONS.items()
                  if v != "read"]
        assert writes == ["statuses"]
        assert privilegedworkflow.REQUIRED_PERMISSIONS["statuses"] == "write"
