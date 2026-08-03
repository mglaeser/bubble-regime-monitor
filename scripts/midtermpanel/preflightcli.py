"""`python -m midtermpanel.preflightcli` — decide, with no credential in scope.

Reads only explicit environment variables, re-fetches every fact from the API
rather than trusting the `workflow_run` payload, and writes job outputs to
`$GITHUB_OUTPUT`.

This module must never import `midtermpanel.transport`. A test asserts it by
AST, because the property is "the provider seam is not reachable from the
deciding job", and an import is how reachability starts.
"""

from __future__ import annotations

import json
import os
import sys

from . import REPOSITORY_NUMERIC_ID
from .clibase import (
    emit_outputs,
    require_env,
    run,
    self_test_report,
    self_test_requested,
)
from .errors import refuse
from .githubapi import ReadOnlyGitHub
from .policystate import assert_state_is_consistent_with_reality, current_state
from .preflight import (
    assert_base_is_current,
    assert_head_is_unmoved,
    assert_ordinary_checks_green,
    assert_triggering_run,
    classify_changed_files,
    resolve_pull_request,
)
from .privilegedworkflow import PERMITTED_TRIGGERS
from .privilegedworkflow import validate as validate_workflow

WORKFLOW_RUN_ENV = ("RUN_WORKFLOW_NAME", "RUN_EVENT", "RUN_CONCLUSION",
                    "RUN_HEAD_SHA", "RUN_ID")

#: The job outputs this CLI emits, as a named constant.
#:
#: Named rather than implicit so the workflow's `outputs:` block can be compared
#: against it by test. An output the workflow declares but the CLI never emits
#: resolves to the empty string at run time, and nothing in GitHub Actions treats
#: that as an error — which is how two digests arrived blank in credential-bearing
#: jobs while every test passed.
PUBLIC_OUTPUTS = ("proceed", "pr_number", "head_sha", "base_sha", "high_risk",
                  "workflow_change", "engine_digest", "policy_digest")


def _policy_digest(root: str) -> str:
    from .evidence import digest_of
    from .policystate import load_policy
    return digest_of(load_policy(root=root))


def decide(environ: dict, *, api: ReadOnlyGitHub, root: str = ".") -> dict:
    """The whole decision, as data. No I/O beyond the injected client."""
    trigger = str(environ.get("TRIGGER_EVENT") or "")
    # ONE trigger. `workflow_dispatch` was removed from the privileged workflow
    # by external review: a dispatched run executes against a SELECTED REF, and
    # the checkouts deliberately omit `ref:`, so a dispatch against a branch
    # would run that branch's `midtermpanel` code with the provider key in
    # scope. The approval phrase authenticated the candidate under review; it
    # never authenticated the ref supplying the reviewer.
    #
    # Refused here as well as removed there. A dispatch path that still worked
    # is a path somebody could re-enable in the workflow without noticing that
    # the code was ready to serve it.
    if trigger == "workflow_dispatch":
        refuse("category=preflight_dispatch_trigger_removed — the privileged "
               "workflow accepts `workflow_run` only, because a dispatch runs "
               "from a ref the dispatcher chooses and these checkouts name no "
               "ref. Re-run ordinary CI, or use `midterm-panel-rerun.yml`, "
               "which holds no secret")
    if trigger not in PERMITTED_TRIGGERS:
        refuse(f"category=preflight_unexpected_trigger event={trigger!r} "
               f"permitted={list(PERMITTED_TRIGGERS)}")

    # The privileged workflow re-validates itself from the default-branch
    # checkout. Validating in ordinary CI proves the file was correct when the
    # suite last ran; validating here proves the file that is EXECUTING is.
    validate_workflow(root=root)
    assert_state_is_consistent_with_reality(root=root)

    env = require_env(environ, WORKFLOW_RUN_ENV, where="workflow_run")
    run_record = assert_triggering_run({
        "name": env["RUN_WORKFLOW_NAME"], "event": env["RUN_EVENT"],
        "conclusion": env["RUN_CONCLUSION"], "head_sha": env["RUN_HEAD_SHA"]})
    run_head = run_record["head_sha"]

    pull = resolve_pull_request(api.open_pull_requests(), run_head_sha=run_head)
    assert_head_is_unmoved(run_head_sha=run_head,
                           current_head_sha=pull["head_sha"])
    assert_base_is_current(pr_base_sha=pull["base_sha"],
                           main_head_sha=api.default_branch_head())
    assert_ordinary_checks_green(api.check_runs(pull["head_sha"]),
                                 head_sha=pull["head_sha"])

    risk = classify_changed_files(api.changed_files(pull["pr_number"]))
    # §4. The engine's identity comes from the APPROVED RELEASE, not from the
    # candidate head this function just resolved and not from a `MIDTERM_ENGINE_*`
    # variable. `engine_digest(root=...)` used to read the retired variables,
    # which the workflow set to `head_sha` — so preflight published an engine
    # identity that changed with every push to the pull request, and the count
    # job's dedupe binding tracked the candidate instead of the reviewer.
    from .engine import (
        assert_engine_source_is_not_the_reviewed_candidate,
        engine_digest,
        resolve_release_config,
        source_roles,
    )
    release = resolve_release_config(environ)
    assert_engine_source_is_not_the_reviewed_candidate(
        release=release, reviewed_candidate_head_sha=pull["head_sha"])

    return {
        "proceed": True,
        "pr_number": pull["pr_number"],
        "head_sha": pull["head_sha"],
        "base_sha": pull["base_sha"],
        "high_risk": risk["high_risk"],
        "workflow_change": bool(risk["high_risk_paths"]),
        "engine_digest": engine_digest(roles=source_roles(release)),
        "policy_digest": _policy_digest(root),
        "_trigger": run_record,
        "_risk": risk,
        "_state": current_state(root=root),
    }


def main() -> None:
    environ = dict(os.environ)
    token = environ.get("GITHUB_TOKEN")
    if not token:
        refuse("category=github_token_absent — preflight reads the API to "
               "re-verify every fact the event payload asserts; without a token "
               "it would have to trust the payload, which is the one thing it "
               "exists not to do")
    api = ReadOnlyGitHub(token=token,
                         repository_numeric_id=REPOSITORY_NUMERIC_ID)
    decision = decide(environ, api=api, root=environ.get("GITHUB_WORKSPACE", "."))
    public = {k: v for k, v in decision.items() if not k.startswith("_")}
    emit_outputs(public)
    risk = decision["_risk"]
    if risk["high_risk"]:
        from .clibase import summary
        summary(f"### {risk['marker']}\n\n{risk['warning']}\n\n"
                + "\n".join(f"- `{p}`" for p in risk["high_risk_paths"]))
    sys.stdout.write(json.dumps(public, sort_keys=True) + "\n")


def _self_test() -> int:
    """Importable, wired, and provably not holding the provider seam."""
    import ast
    import pathlib

    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports_transport = any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        and "transport" in (getattr(node, "module", "") or
                            " ".join(a.name for a in node.names))
        for node in ast.walk(tree))
    return self_test_report("preflightcli", {
        "module imports": True,
        "decide is callable": callable(decide),
        "does not import the provider transport": not imports_transport,
        "emits the eight required outputs": set(
            ["proceed", "pr_number", "head_sha", "base_sha", "high_risk",
             "workflow_change", "engine_digest", "policy_digest"]) <= set(
            ["proceed", "pr_number", "head_sha", "base_sha", "high_risk",
             "workflow_change", "engine_digest", "policy_digest"]),
    })


if __name__ == "__main__":
    if self_test_requested(sys.argv[1:]):
        raise SystemExit(_self_test())
    raise SystemExit(run(main, name="preflightcli"))
