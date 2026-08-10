"""Activation: the exact approved release, and the rung the lane now stands on.

Two things changed and each needs a test that would notice if it were wrong.

The first is the BINDING. `governance/midterm-panel-engine-release.json` used to
carry two pinned commits and three nulls, and `provenance_of` correctly called
that REBUILT_FROM_PINNED_SOURCE_TEST_ONLY. It now names all seven fields of a
real release, so it authorises a run that spends money — and the digest it
records for those seven fields must be RECOMPUTED here rather than read, or the
file would be asserting its own correctness.

The second is the STATE. `STAGED_NO_PROVIDER_SECRET -> ACTIVE` used to be one
step and it spanned the two events that matter most: the key arriving, and the
first call being made. The window between them is exactly when a provider call
is an incident rather than a cost, and no state said so. There is now a rung
that does, and `STAGED_NO_PROVIDER_SECRET -> ACTIVE` is refused.

The mutation tests are the load-bearing ones throughout. A check that passes the
real document proves today's file is acceptable; introducing each forbidden edit
INTO the real document and asserting the refusal is what proves the check works.
"""

from __future__ import annotations

import ast
import json
import socket
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from midtermpanel import (  # noqa: E402
    engine,
    policystate,
    releasevalidation,
)
from midtermpanel.errors import PanelRefusal  # noqa: E402
from trustedlane import enginebuild  # noqa: E402

RELEASE_DOCUMENT = ROOT / "governance" / "midterm-panel-engine-release.json"
POLICY_DOCUMENT = ROOT / "governance" / "midterm-panel-policy.json"
VALIDATION_WORKFLOW = (ROOT / ".github" / "workflows"
                       / "midterm-engine-release-validation.yml")


def _occurrences(text: str, phrase: str):
    index = text.find(phrase)
    while index != -1:
        yield index
        index = text.find(phrase, index + 1)


def _release() -> dict:
    return json.loads(RELEASE_DOCUMENT.read_text(encoding="utf-8"))


def _policy() -> dict:
    return json.loads(POLICY_DOCUMENT.read_text(encoding="utf-8"))


# ------------------------------------------------- 1. the real binding -----

class TestTheCommittedBinding:
    """Against the real file. Not a fixture — the file is the approval."""

    def test_the_release_is_approved_not_merely_reproducible(self):
        assert engine.provenance_of(_release()) == engine.APPROVED_RELEASE

    def test_every_binding_field_is_named(self):
        release = _release()
        absent = sorted(field for field in engine.RELEASE_BINDING_FIELDS
                        if not engine._binding_member_is_present(
                            release.get(field)))
        assert absent == [], (
            "an approved release names all seven fields; approving a subset "
            "approves an engine the operator did not fully name")

    def test_the_recorded_binding_digest_is_reproduced_not_trusted(self):
        """The one test that makes the recorded digest worth recording."""
        report = releasevalidation.assert_binding_reproduces(_release())

        assert report["engine_release_binding_sha256"] == (
            _release()["engine_release_binding_sha256"])
        assert report["provenance"] == engine.APPROVED_RELEASE

    @pytest.mark.parametrize("field", engine.RELEASE_BINDING_FIELDS)
    def test_editing_any_one_binding_field_breaks_the_digest(self, field):
        """Seven separate mutations, so no field is silently outside the seal.

        `native_branch_protection` is `False` in the real document, so the
        mutation for it flips a boolean rather than editing a string — which is
        the case a truthiness-based check would have missed."""
        document = _release()
        current = document[field]
        document[field] = (not current if isinstance(current, bool)
                           else f"{current}-edited")

        with pytest.raises(PanelRefusal) as excinfo:
            releasevalidation.assert_binding_reproduces(document)

        assert ("engine_release_binding_digest_mismatch" in str(excinfo.value)
                or "protection_claim_contradicts" in str(excinfo.value)
                or "artifact_digest_malformed" in str(excinfo.value)
                or "control_class_unknown" in str(excinfo.value)), excinfo.value

    def test_a_document_with_no_recorded_digest_is_refused(self):
        document = _release()
        del document["engine_release_binding_sha256"]

        with pytest.raises(PanelRefusal) as excinfo:
            releasevalidation.assert_binding_reproduces(document)

        assert "binding_digest_not_recorded" in str(excinfo.value)
        # The refusal states the correct value, because an operator who has to
        # go and compute it by hand will paste something.
        assert _release()["engine_release_binding_sha256"] in str(excinfo.value)

    def test_a_partial_binding_is_refused_before_anything_is_downloaded(self):
        document = _release()
        document["approved_engine_identity_sha256"] = None

        with pytest.raises(PanelRefusal) as excinfo:
            releasevalidation.assert_binding_reproduces(document)

        assert "binding_is_not_approved" in str(excinfo.value)

    def test_the_document_still_refuses_to_claim_native_protection(self):
        claim = engine.assert_release_protection_claim(_release())

        assert claim["native_branch_protection"] is False
        assert claim["control_class"] == enginebuild.CONTROL_HUMAN_EXACT_HEAD

    def test_the_document_never_claims_protected_or_write_separated_evidence(self):
        """Named claims are absent; the ones that appear are DISCLAIMED.

        Two different questions, and a single substring search answers the
        wrong one — "third-party attestation" appears in this document only in
        the sentence saying it is not one, and a test that forbade the phrase
        would forbid the disclaimer while permitting a claim spelled any other
        way. So the evidence-class names are forbidden outright, and the
        prose phrases are required to be negated wherever they occur."""
        text = RELEASE_DOCUMENT.read_text(encoding="utf-8")
        assert enginebuild.PROTECTED_STATE not in text
        for forbidden in ("TRUSTED_COUNT_EVIDENCE", "TRUSTED_EXECUTION_EVIDENCE",
                          "WRITE_SEPARATED_REVIEW_EVIDENCE",
                          "INDEPENDENT_THIRD_PARTY_ATTESTATION"):
            assert forbidden not in text, forbidden
        for phrase in ("third-party attestation", "write separation",
                       "write-separated", "protected-ref provenance"):
            for index in _occurrences(text, phrase):
                preceding = text[max(0, index - 40):index]
                assert any(negation in preceding for negation in
                           ("NOT ", "not ", "no ", "never ")), (
                    f"{phrase!r} appears without a negation: "
                    f"{text[max(0, index - 60):index + len(phrase)]!r}")

    def test_duplicate_keys_are_refused_rather_than_last_one_wins(self, tmp_path):
        root = tmp_path / "repo"
        (root / "governance").mkdir(parents=True)
        (root / "governance" / "midterm-panel-engine-release.json").write_text(
            '{"approved_engine_release_tag": "a", '
            '"approved_engine_release_tag": "b"}', encoding="utf-8")

        with pytest.raises(PanelRefusal) as excinfo:
            releasevalidation.load_release_document(root=str(root))

        assert "duplicate_key" in str(excinfo.value)


# ------------------------------------------- 2. the released identity ------

def _identity_record(document: dict) -> dict:
    """An identity record carrying the digests governance restates.

    A fixture, and labelled as one. The real document is fetched by the hosted
    job; what is under test here is the comparison logic, and building the
    fixture out of the committed restatement means a test that passed while the
    restatement was wrong would be impossible."""
    digests = document["approved_engine_identity_digests"]
    build = document["approved_engine_build"]
    provenance = {
        "build_workflow_run_id": build["build_workflow_run_id"],
        "build_workflow_run_attempt": build["build_workflow_run_attempt"],
        "build_ref": build["build_ref"],
        "build_head_sha": build["build_head_sha"],
        "runner_image_digest": "Linux-X64-github-hosted",
        "repository_numeric_id": 1297332828,
        "build_ref_protected": build["build_ref_protected"],
        "native_branch_protection": False,
        "control_class": enginebuild.CONTROL_HUMAN_EXACT_HEAD,
        "source_roles": {
            "candidate_verifier": document["approved_engine_source_sha"],
            "protected_trusted_lane":
                document["approved_engine_protected_sha"]},
        "builder": "trusted-engine-build",
    }
    provenance["provenance_sha256"] = (
        enginebuild.recomputed_provenance_digest(provenance))
    record = {field: digests[field] for field in
              enginebuild.BUILT_IDENTITY_FIELDS}
    record["provenance_sha256"] = provenance["provenance_sha256"]
    record.update({
        "state": enginebuild.MIDTERM_STATE,
        "build_ref_protected": False,
        "native_branch_protection": False,
        "control_class": enginebuild.CONTROL_HUMAN_EXACT_HEAD,
        "provenance": provenance,
    })
    return record


class TestTheReleasedIdentityIsCompared:

    def test_the_restated_digests_are_compared_in_full(self):
        document = _release()
        record = _identity_record(document)
        record["provenance_sha256"] = document[
            "approved_engine_identity_digests"]["provenance_sha256"]

        matched = releasevalidation.assert_restated_digests_match(
            document, record)

        assert sorted(matched) == sorted(enginebuild.BUILT_IDENTITY_FIELDS)

    @pytest.mark.parametrize("field", enginebuild.BUILT_IDENTITY_FIELDS)
    def test_any_restated_digest_that_drifts_is_refused(self, field):
        document = _release()
        record = _identity_record(document)
        record["provenance_sha256"] = document[
            "approved_engine_identity_digests"]["provenance_sha256"]
        record[field] = "0" * 64

        with pytest.raises(PanelRefusal) as excinfo:
            releasevalidation.assert_restated_digests_match(document, record)

        assert field in str(excinfo.value)

    def test_a_document_that_restates_nothing_is_refused(self):
        document = _release()
        del document["approved_engine_identity_digests"]

        with pytest.raises(PanelRefusal) as excinfo:
            releasevalidation.assert_restated_digests_match(document, {})

        assert "digests_not_restated" in str(excinfo.value)

    def test_the_provenance_must_reseal_from_its_own_contents(self):
        record = _identity_record(_release())

        sealed = releasevalidation.assert_provenance_reseals(record)

        assert sealed["build_ref_protected"] is False
        assert sealed["build_workflow_run_id"] == 31427275863

    def test_flipping_protection_inside_the_provenance_breaks_the_seal(self):
        """The edit a governance note could never repair, caught by the seal.

        Both stored copies of the digest are left untouched, which is exactly
        what an editor would do — and is why comparing the two copies to each
        other catches nothing and recomputation catches it."""
        record = _identity_record(_release())
        record["provenance"]["native_branch_protection"] = True
        record["provenance"]["build_ref_protected"] = True

        with pytest.raises(PanelRefusal) as excinfo:
            releasevalidation.assert_provenance_reseals(record)

        assert "does_not_reseal" in str(excinfo.value)

    def test_the_build_facts_in_governance_must_match_the_seal(self):
        document = _release()
        record = _identity_record(document)
        sealed = releasevalidation.assert_provenance_reseals(record)

        assert releasevalidation.assert_build_facts_match(document, sealed)

    def test_a_governance_build_fact_that_drifts_is_refused(self):
        document = _release()
        record = _identity_record(document)
        sealed = releasevalidation.assert_provenance_reseals(record)
        document["approved_engine_build"]["build_ref_protected"] = True

        with pytest.raises(PanelRefusal) as excinfo:
            releasevalidation.assert_build_facts_match(document, sealed)

        assert "build_facts_differ" in str(excinfo.value)

    def test_a_protected_grade_record_is_refused_by_this_lane(self):
        """`main` is unprotected, so a protected record is from elsewhere."""
        document = _release()
        record = _identity_record(document)
        record["state"] = enginebuild.PROTECTED_STATE

        with pytest.raises(PanelRefusal) as excinfo:
            engine.assert_identity_is_midterm_grade(record, release=document)

        assert "claims_native_protection" in str(excinfo.value)

    def test_the_midterm_record_is_accepted_and_the_trusted_lane_refuses_it(self):
        from trustedlane import enginebridge
        from trustedlane.errors import LaneRefusal

        document = _release()
        record = _identity_record(document)

        grade = engine.assert_identity_is_midterm_grade(record,
                                                        release=document)
        assert grade["state"] == enginebuild.MIDTERM_STATE
        assert grade["native_branch_protection"] is False

        with pytest.raises(LaneRefusal):
            enginebridge.assert_identity_is_trusted_grade(record)


# ------------------------------------------------- 3. the egress guard -----

class TestTheEgressGuard:
    """`provider_calls: 0` has to be an observation, not a promise."""

    def test_a_permitted_host_is_recorded_and_allowed_through(self, monkeypatch):
        reached = []
        monkeypatch.setattr(socket, "create_connection",
                            lambda address, *a, **k: reached.append(address))

        with releasevalidation.EgressGuard() as guard:
            socket.create_connection(("api.github.com", 443))

        assert reached == [("api.github.com", 443)]
        assert guard.hosts_contacted == ["api.github.com"]
        assert guard.refused == []

    def test_an_unpermitted_host_is_refused_before_the_call_is_made(self,
                                                                   monkeypatch):
        reached = []
        monkeypatch.setattr(socket, "create_connection",
                            lambda address, *a, **k: reached.append(address))

        with releasevalidation.EgressGuard() as guard:
            with pytest.raises(releasevalidation.EgressRefused):
                socket.create_connection(("api.openai.com", 443))

        assert reached == [], "refused BEFORE the underlying call, not after"
        assert [e["host"] for e in guard.refused] == ["api.openai.com"]

    def test_the_raw_socket_path_is_guarded_too(self, monkeypatch):
        """A client that resolves the name itself must not walk around it."""
        monkeypatch.setattr(socket.socket, "connect",
                            lambda self, address: None, raising=False)

        with releasevalidation.EgressGuard():
            sock = socket.socket()
            try:
                with pytest.raises(releasevalidation.EgressRefused):
                    sock.connect(("api.openai.com", 443))
            finally:
                sock.close()

    def test_an_address_the_guard_cannot_decompose_is_refused(self):
        with releasevalidation.EgressGuard() as guard:
            with pytest.raises(releasevalidation.EgressRefused):
                socket.create_connection("/tmp/some.sock")

        assert guard.refused, "an unparseable address is refused, not ignored"

    def test_the_guard_is_proved_by_a_refused_provider_connection(self,
                                                                 monkeypatch):
        monkeypatch.setattr(socket, "create_connection",
                            lambda address, *a, **k: None)

        with releasevalidation.EgressGuard() as guard:
            proof = releasevalidation.prove_the_guard_refuses_the_provider(guard)

        assert proof["refused"] is True
        assert proof["provider_host_probed"] == "api.openai.com"

    def test_the_proof_fails_when_the_guard_is_not_in_force(self, monkeypatch):
        """The mutation that matters: an inspection would still pass here."""
        monkeypatch.setattr(socket, "create_connection",
                            lambda address, *a, **k: None)
        guard = releasevalidation.EgressGuard()   # never entered

        with pytest.raises(PanelRefusal) as excinfo:
            releasevalidation.prove_the_guard_refuses_the_provider(guard)

        assert "guard_did_not_refuse" in str(excinfo.value)

    def test_a_network_failure_is_not_accepted_as_a_refusal(self, monkeypatch):
        """"It failed" is not "the guard decided". A run whose egress control
        is only accidentally effective proves nothing about the run where the
        network is up."""
        def unreachable(address, *args, **kwargs):
            raise OSError("network is unreachable")

        monkeypatch.setattr(socket, "create_connection", unreachable)
        guard = releasevalidation.EgressGuard()

        with pytest.raises(PanelRefusal) as excinfo:
            releasevalidation.prove_the_guard_refuses_the_provider(guard)

        assert "guard_not_in_force" in str(excinfo.value)

    def test_the_guard_leaves_the_socket_module_as_it_found_it(self):
        before_create = socket.create_connection
        before_own = "connect" in socket.socket.__dict__

        with releasevalidation.EgressGuard():
            assert socket.create_connection is not before_create

        assert socket.create_connection is before_create
        assert ("connect" in socket.socket.__dict__) is before_own

    def test_the_permitted_hosts_are_the_release_asset_hosts_not_a_copy(self):
        from midtermpanel import releaseasset
        assert releasevalidation.PERMITTED_HOSTS is releaseasset.PERMITTED_HOSTS

    def test_no_provider_host_is_permitted(self):
        assert releasevalidation.PROVIDER_PROOF_HOST not in (
            releasevalidation.PERMITTED_HOSTS)


# ----------------------------------------- 4. the module cannot spend ------

class TestTheValidationCannotReachTheProvider:

    def _tree(self):
        path = ROOT / "scripts" / "midtermpanel" / "releasevalidation.py"
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_it_never_imports_the_provider_transport(self):
        """By AST, because an import is how reachability starts."""
        offenders = []
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.ImportFrom) and node.module == "transport":
                offenders.append(node.module)
            elif isinstance(node, ast.Import):
                offenders += [a.name for a in node.names
                              if a.name.endswith("transport")]
        assert offenders == []

    def test_it_never_names_a_live_transport_or_reads_a_key(self):
        forbidden = {"live_count_transport", "live_generation_transport",
                     "read_provider_key", "MidtermProviderTransport"}
        offenders = []
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Name) and node.id in forbidden:
                offenders.append(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in forbidden:
                offenders.append(node.attr)
        assert offenders == []

    def test_both_credential_names_are_checked(self, monkeypatch):
        """A guard that checked one name would read "no provider secret" with
        the key sitting right there under the other."""
        from midtermpanel import SECRET_NAME
        from midtermpanel.transport import PROVIDER_KEY_ENV

        checked = releasevalidation.assert_no_provider_credential_is_in_scope(
            {})["credential_variables_checked"]
        assert sorted(checked) == sorted((PROVIDER_KEY_ENV, SECRET_NAME))

        for name in (PROVIDER_KEY_ENV, SECRET_NAME):
            with pytest.raises(PanelRefusal):
                releasevalidation.assert_no_provider_credential_is_in_scope(
                    {name: "sk-not-a-real-key"})  # pragma: allowlist secret

    def test_the_module_actually_runs_when_invoked(self):
        """The missing `__main__` guard once made a step a green no-op: the
        module was imported, `main` was defined, nothing called it, exit 0."""
        source = (ROOT / "scripts" / "midtermpanel"
                  / "releasevalidation.py").read_text(encoding="utf-8")
        assert 'if __name__ == "__main__":' in source
        assert source.rstrip().endswith("main()")


# ----------------------------------------------- 5. the workflow shape -----

class TestTheValidationWorkflow:

    def _document(self):
        return yaml.safe_load(VALIDATION_WORKFLOW.read_text(encoding="utf-8"))

    def test_it_contains_no_secrets_expression_at_all(self):
        """Not "no provider secret" — no `secrets.` expression of any kind.

        Checked against the PARSED document rather than the raw bytes, because
        the file's own comments explain that there is no `secrets.` expression
        in it, and a substring search over the bytes cannot tell an explanation
        from an expression. YAML comments are not expressions and GitHub never
        evaluates them, so dropping them is not a weakening — it is the check
        asking about the thing that runs."""
        executable = yaml.safe_dump(self._document())
        assert "secrets." not in executable
        assert "${{ github.token }}" in executable

    def test_its_permissions_are_read_only(self):
        document = self._document()
        assert document["permissions"] == {"contents": "read"}

    def test_it_drives_the_shared_helper_not_its_own_copy(self):
        """A copied probe is how the D0 workflow drifted from its tests."""
        text = VALIDATION_WORKFLOW.read_text(encoding="utf-8")
        assert "python -m midtermpanel.releasevalidation" in text
        assert "materialise_release_identity(" not in text

    def test_the_credential_check_runs_before_anything_opens_a_socket(self):
        steps = self._document()["jobs"]["validate"]["steps"]
        names = [s.get("name", "") for s in steps]
        credential = next(i for i, n in enumerate(names)
                          if "no provider credential" in n)
        validation = next(i for i, n in enumerate(names)
                          if "no route to the provider" in n)
        assert credential < validation

    def test_the_token_reaches_only_the_validation_step(self):
        steps = self._document()["jobs"]["validate"]["steps"]
        holders = [s.get("name") for s in steps
                   if "GITHUB_TOKEN" in (s.get("env") or {})]
        assert len(holders) == 1, holders

    def test_it_asks_for_no_write_permission_anywhere(self):
        document = self._document()
        for scope, level in (document.get("permissions") or {}).items():
            assert level == "read", scope


# ------------------------------------------------- 6. the state ladder -----

class TestTheKeyPresentRung:

    def test_the_state_exists_and_is_known(self):
        assert policystate.STAGED_PROVIDER_KEY_PRESENT_NO_CALLS in (
            policystate.STATES)
        assert policystate.assert_known_state(
            policystate.STAGED_PROVIDER_KEY_PRESENT_NO_CALLS)

    def test_its_name_says_both_halves(self):
        name = policystate.STAGED_PROVIDER_KEY_PRESENT_NO_CALLS
        assert "PROVIDER_KEY_PRESENT" in name
        assert "NO_CALLS" in name

    def test_the_old_direct_jump_to_active_is_now_refused(self):
        """A lane that has never held a key cannot have spent one."""
        with pytest.raises(PanelRefusal) as excinfo:
            policystate.assert_transition(
                policystate.STAGED_NO_PROVIDER_SECRET, policystate.ACTIVE)

        assert "transition_forbidden" in str(excinfo.value)

    def test_active_is_reachable_only_through_the_key_present_rung(self):
        into_active = [state for state, targets
                       in policystate.PERMITTED_TRANSITIONS.items()
                       if policystate.ACTIVE in targets]
        assert into_active == [
            policystate.STAGED_PROVIDER_KEY_PRESENT_NO_CALLS]

    def test_the_two_forward_steps_are_permitted(self):
        assert policystate.assert_transition(
            policystate.STAGED_NO_PROVIDER_SECRET,
            policystate.STAGED_PROVIDER_KEY_PRESENT_NO_CALLS)["changed"]
        assert policystate.assert_transition(
            policystate.STAGED_PROVIDER_KEY_PRESENT_NO_CALLS,
            policystate.ACTIVE)["changed"]

    def test_retiring_the_key_walks_back_down_rather_than_sideways(self):
        assert policystate.assert_transition(
            policystate.STAGED_PROVIDER_KEY_PRESENT_NO_CALLS,
            policystate.STAGED_NO_PROVIDER_SECRET)["changed"]
        with pytest.raises(PanelRefusal):
            policystate.assert_transition(
                policystate.STAGED_PROVIDER_KEY_PRESENT_NO_CALLS,
                policystate.IMPLEMENTATION_IN_PROGRESS)

    def test_every_state_has_a_meaning_and_an_adjacency(self):
        for state in policystate.STATES:
            assert state in policystate.STATE_MEANS
            assert state in policystate.PERMITTED_TRANSITIONS

    def test_the_meaning_names_the_evidence_rather_than_the_mood(self):
        means = policystate.STATE_MEANS[
            policystate.STAGED_PROVIDER_KEY_PRESENT_NO_CALLS]
        for required in ("secret is installed", "engine release",
                         "no provider or generation call"):
            assert required in means, required


class TestTheRecordedWalk:
    """A state that was written rather than reached used to be unnoticeable."""

    def test_the_committed_history_is_a_legal_walk(self):
        report = policystate.assert_recorded_history_is_a_legal_walk(
            root=str(ROOT))

        assert report["state"] == (
            policystate.STAGED_PROVIDER_KEY_PRESENT_NO_CALLS)
        assert report["history"][0] == policystate.INITIAL_STATE
        assert report["transitions_taken"] == 2

    def test_the_declared_state_is_the_end_of_the_walk(self):
        policy = _policy()["architecture"]
        assert policy["ci_operational_state_history"][-1] == (
            policy["ci_operational_state"])

    def test_every_recorded_step_has_a_written_rationale(self):
        architecture = _policy()["architecture"]
        steps = len(architecture["ci_operational_state_history"]) - 1
        assert len(architecture["ci_operational_state_history_evidence"]) == steps

    def _with_history(self, tmp_path, history, declared=None):
        root = tmp_path / "repo"
        (root / "governance").mkdir(parents=True)
        policy = _policy()
        policy["architecture"]["ci_operational_state_history"] = history
        if declared is not None:
            policy["architecture"]["ci_operational_state"] = declared
        (root / "governance" / "midterm-panel-policy.json").write_text(
            json.dumps(policy), encoding="utf-8")
        return root

    def test_a_walk_that_skips_a_rung_is_refused(self, tmp_path):
        root = self._with_history(tmp_path, [
            policystate.IMPLEMENTATION_IN_PROGRESS,
            policystate.STAGED_NO_PROVIDER_SECRET,
            policystate.ACTIVE], declared=policystate.ACTIVE)

        with pytest.raises(PanelRefusal) as excinfo:
            policystate.assert_recorded_history_is_a_legal_walk(root=str(root))

        assert "transition_forbidden" in str(excinfo.value)

    def test_a_walk_that_starts_halfway_up_is_refused(self, tmp_path):
        root = self._with_history(tmp_path, [
            policystate.STAGED_NO_PROVIDER_SECRET,
            policystate.STAGED_PROVIDER_KEY_PRESENT_NO_CALLS])

        with pytest.raises(PanelRefusal) as excinfo:
            policystate.assert_recorded_history_is_a_legal_walk(root=str(root))

        assert "does_not_start_at_the_beginning" in str(excinfo.value)

    def test_a_walk_that_ends_somewhere_else_is_refused(self, tmp_path):
        root = self._with_history(tmp_path, [
            policystate.IMPLEMENTATION_IN_PROGRESS,
            policystate.STAGED_NO_PROVIDER_SECRET])

        with pytest.raises(PanelRefusal) as excinfo:
            policystate.assert_recorded_history_is_a_legal_walk(root=str(root))

        assert "does_not_reach_the_declared_state" in str(excinfo.value)

    def test_a_document_with_no_history_is_refused(self, tmp_path):
        root = tmp_path / "repo"
        (root / "governance").mkdir(parents=True)
        policy = _policy()
        del policy["architecture"]["ci_operational_state_history"]
        (root / "governance" / "midterm-panel-policy.json").write_text(
            json.dumps(policy), encoding="utf-8")

        with pytest.raises(PanelRefusal) as excinfo:
            policystate.assert_recorded_history_is_a_legal_walk(root=str(root))

        assert "has_no_state_history" in str(excinfo.value)

    def test_an_unknown_state_in_the_history_is_refused(self, tmp_path):
        root = self._with_history(tmp_path, [
            policystate.IMPLEMENTATION_IN_PROGRESS, "MIDTERM_TOTALLY_FINE"])

        with pytest.raises(PanelRefusal) as excinfo:
            policystate.assert_recorded_history_is_a_legal_walk(root=str(root))

        assert "state_unknown" in str(excinfo.value)


class TestTheFirstSpendingRunIsAnOperatorDecision:
    """The protection that used to exist by accident, made deliberate.

    While the engine release was unbound, `assert_provenance_permits` refused
    every provider-backed run — and that refusal meant two things at once: "no
    approved engine" and, incidentally, "no spending yet". This activation
    answers the first question. Nothing would have been left holding the
    second, so the first run that cost money would have been triggered by
    whoever next opened a pull request after the merge."""

    def _root(self, tmp_path, *, state=None, authorised=None, release=True):
        root = tmp_path / "repo"
        (root / "governance").mkdir(parents=True)
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".github" / "workflows"
         / "midterm-panel-review.yml").write_text("{}", encoding="utf-8")
        policy = _policy()
        if state is not None:
            policy["architecture"]["ci_operational_state"] = state
        if authorised is not None:
            policy["architecture"][
                policystate.FIRST_PROVIDER_RUN_FIELD] = authorised
        (root / "governance" / "midterm-panel-policy.json").write_text(
            json.dumps(policy), encoding="utf-8")
        if release:
            (root / "governance"
             / "midterm-panel-engine-release.json").write_text(
                RELEASE_DOCUMENT.read_text(encoding="utf-8"), encoding="utf-8")
        return str(root)

    def test_the_committed_policy_does_not_authorise_spending(self):
        """This activation PR must not be the thing that starts the meter."""
        assert _policy()["architecture"][
            policystate.FIRST_PROVIDER_RUN_FIELD] is False

        with pytest.raises(PanelRefusal) as excinfo:
            policystate.assert_provider_backed_run_is_authorised(root=str(ROOT))

        assert "first_provider_backed_run_not_authorised" in str(excinfo.value)

    def test_the_refusal_tells_the_operator_exactly_what_to_set(self):
        with pytest.raises(PanelRefusal) as excinfo:
            policystate.assert_provider_backed_run_is_authorised(root=str(ROOT))

        reason = str(excinfo.value)
        assert policystate.FIRST_PROVIDER_RUN_FIELD in reason
        assert "governance/midterm-panel-policy.json" in reason

    def test_an_explicit_true_authorises_it(self, tmp_path):
        record = policystate.assert_provider_backed_run_is_authorised(
            root=self._root(tmp_path, authorised=True))

        assert record["authorised"] is True

    @pytest.mark.parametrize("value", ["true", "True", 1, "yes", None, ""])
    def test_anything_that_is_not_the_json_literal_true_is_refused(
            self, tmp_path, value):
        """`bool("false")` is True. A lenient parser turns a hedge into a yes."""
        with pytest.raises(PanelRefusal) as excinfo:
            policystate.assert_provider_backed_run_is_authorised(
                root=self._root(tmp_path, authorised=value))

        assert "not_authorised" in str(excinfo.value)

    @pytest.mark.parametrize("state", [
        "MIDTERM_SINGLE_REPO_PANEL_IMPLEMENTATION_IN_PROGRESS",
        "MIDTERM_SINGLE_REPO_PANEL_STAGED_NO_PROVIDER_SECRET"])
    def test_a_state_below_the_key_present_rung_may_never_spend(
            self, tmp_path, state):
        """Even with the flag set. A lane that has not recorded holding a
        usable key must not be the lane that spends one."""
        with pytest.raises(PanelRefusal) as excinfo:
            policystate.assert_provider_backed_run_is_authorised(
                root=self._root(tmp_path, state=state, authorised=True))

        assert "forbidden_in_this_state" in str(excinfo.value)

    def test_an_active_lane_needs_no_further_authorisation(self, tmp_path):
        """The flag gates the FIRST run. Once ACTIVE is earned it gates
        nothing, and leaving it in force would mean re-authorising every
        review — a prompt people learn to click through."""
        record = policystate.assert_provider_backed_run_is_authorised(
            root=self._root(tmp_path, state=policystate.ACTIVE,
                            authorised=False))

        assert record["authorised"] is True

    def test_the_gate_is_reached_before_an_engine_is_built(self, tmp_path):
        """Through the real entry point both credential-bearing jobs use.

        Asserted by the refusal arriving with no artifact written: a gate that
        fired after the build would have done the expensive half of the work it
        exists to prevent."""
        artifact = tmp_path / "engine.tar.gz"
        release = dict(_release())
        release["provenance"] = engine.APPROVED_RELEASE

        with pytest.raises(PanelRefusal) as excinfo:
            engine.load_engine_for_mode(
                mode=engine.MODE_PROVIDER, release=release,
                reviewed_candidate_head_sha="d" * 40,
                artifact_path=str(artifact),
                extract_to=str(tmp_path / "extract"),
                repository_numeric_id=1297332828, cwd=str(ROOT))

        assert "first_provider_backed_run_not_authorised" in str(excinfo.value)
        assert not artifact.exists(), "refused before anything was built"

    def test_a_dry_run_is_not_gated_by_it(self, tmp_path):
        """The no-key vertical must keep working. This gate is about spending,
        and a dry run cannot spend — `assert_transport_suits_mode` refuses it a
        live transport regardless."""
        from midtermpanel import dryrun
        assert dryrun.is_dry_run({"MIDTERM_PANEL_MODE": engine.MODE_DRY_RUN})
        # The gate is asked only in provider mode; asserted at the source so a
        # future edit that made it unconditional reddens here rather than in a
        # hosted dry run.
        source = (ROOT / "scripts" / "midtermpanel"
                  / "engine.py").read_text(encoding="utf-8")
        assert "if mode == MODE_PROVIDER:" in source
        assert "assert_provider_backed_run_is_authorised(root=cwd)" in source


class TestTheStateMatchesTheTree:

    def test_the_committed_state_is_consistent_with_this_repository(self):
        report = policystate.assert_state_is_consistent_with_reality(
            root=str(ROOT))

        assert report["state"] == (
            policystate.STAGED_PROVIDER_KEY_PRESENT_NO_CALLS)
        assert report["workflow_live"] is True

    def test_the_key_present_state_needs_a_complete_engine_binding(self,
                                                                  tmp_path):
        """The half of the claim the tree CAN disprove.

        A lane declaring "the key is installed and an approved release is
        bound" while the binding names three nulls could not obtain an engine
        at all, and the state would be describing a different repository."""
        root = tmp_path / "repo"
        (root / "governance").mkdir(parents=True)
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".github" / "workflows"
         / "midterm-panel-review.yml").write_text("{}", encoding="utf-8")
        (root / "governance" / "midterm-panel-policy.json").write_text(
            POLICY_DOCUMENT.read_text(encoding="utf-8"), encoding="utf-8")
        release = _release()
        release["approved_engine_release_tag"] = None
        (root / "governance"
         / "midterm-panel-engine-release.json").write_text(
            json.dumps(release), encoding="utf-8")

        with pytest.raises(PanelRefusal) as excinfo:
            policystate.assert_state_is_consistent_with_reality(root=str(root))

        assert "claims_an_engine_release_it_lacks" in str(excinfo.value)

    def test_the_trusted_lane_is_still_locked_at_d0(self):
        from trustedlane import phases
        report = policystate.assert_trusted_lane_is_locked(root=str(ROOT))

        assert report["trusted_lane_locked"] is True
        assert report["implemented_phase"] == phases.D0

    def test_the_policy_still_refuses_every_trusted_label(self):
        policy = _policy()
        assert policy["architecture"]["write_separated"] is False
        assert policy["architecture"]["production_eligible"] is False
        assert policy["architecture"]["trusted_review_authority"] == (
            "NOT_WRITE_SEPARATED")
        assert "TRUSTED_COUNT_EVIDENCE" in policy["evidence"]["may_never_emit"]
