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

from . import REPOSITORY_NUMERIC_ID, observation
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
    APPLICABLE,
    assert_head_is_unmoved,
    assert_ordinary_checks_green,
    assert_triggering_ci_tested_this_exact_combination,
    assert_triggering_run,
    classify_candidate,
    classify_changed_files,
    classify_triggering_run,
    resolve_pull_request,
    review_class_for,
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
PUBLIC_OUTPUTS = ("proceed", "applicability", "applicability_reason",
                  "provider_attempts", "generation_attempts",
                  "pr_number", "head_sha", "base_sha", "high_risk",
                  "workflow_change", "engine_digest", "policy_digest",
                  "triggering_ci_run_id", "triggering_ci_run_attempt",
                  "tested_base_sha", "tested_head_sha", "review_class")


#: The outputs the workflow's own `outputs:` block consumes. A subset of
#: `PUBLIC_OUTPUTS`, named separately so the self-test compares two things that
#: can actually disagree.
REQUIRED_PUBLIC_OUTPUTS = (
    "proceed", "pr_number", "head_sha", "base_sha", "high_risk",
    "workflow_change", "engine_digest", "policy_digest",
    "triggering_ci_run_id", "triggering_ci_run_attempt",
    "tested_base_sha", "tested_head_sha", "review_class")


def _policy_digest(root: str) -> str:
    from .evidence import digest_of
    from .policystate import load_policy
    return digest_of(load_policy(root=root))


def _nothing_to_review(applicability: dict) -> dict:
    """The one shape every not-applicable exit returns.

    Factored so a second not-applicable path cannot be added that forgets one
    of the declared outputs. An output the workflow's `outputs:` block names
    but this CLI never emits resolves to the empty string with no error at all
    — which is how two digests once arrived blank in a credential-bearing job
    while every test passed.

    The blank-filling exclusion is DERIVED from the dict of real values, not
    hand-written beside it. The first version spelled the five names twice —
    once as keys and once as a tuple of strings to skip — and a name dropped
    from the tuple would have been silently overwritten with `""` by the
    `**{...}` that followed. A helper whose whole purpose is that no output
    arrives unexpectedly blank must not have a way to blank one by typo.

    `explicit` is also merged LAST, so ordering cannot decide the outcome
    either."""
    explicit = {
        "proceed": False,
        "applicability": applicability["applicability"],
        "applicability_reason": applicability["reason"],
        "provider_attempts": 0,
        "generation_attempts": 0,
    }
    return {
        **{name: "" for name in PUBLIC_OUTPUTS if name not in explicit},
        **explicit,
        "_risk": {"high_risk": False, "high_risk_paths": [],
                  "marker": "", "warning": ""},
    }


def decide(environ: dict, *, api: ReadOnlyGitHub, root: str = ".",
           sleep=None) -> dict:
    """The whole decision, as data. No I/O beyond the injected client.

    `sleep` is threaded through to `observation.settle` so the tests run
    instantly and none of them can be made to pass by waiting. Production
    passes nothing and gets `time.sleep`."""
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
               "ref. To re-run the panel, re-run ordinary CI: "
               "`gh run rerun <CI_RUN_ID>`. The convenience workflow that did "
               "this was removed for the same selected-ref reason")
    if trigger not in PERMITTED_TRIGGERS:
        refuse(f"category=preflight_unexpected_trigger event={trigger!r} "
               f"permitted={list(PERMITTED_TRIGGERS)}")

    # The privileged workflow re-validates itself from the default-branch
    # checkout. Validating in ordinary CI proves the file was correct when the
    # suite last ran; validating here proves the file that is EXECUTING is.
    validate_workflow(root=root)
    assert_state_is_consistent_with_reality(root=root)

    env = require_env(environ, WORKFLOW_RUN_ENV, where="workflow_run")
    observed_run = {
        "name": env["RUN_WORKFLOW_NAME"], "event": env["RUN_EVENT"],
        "conclusion": env["RUN_CONCLUSION"], "head_sha": env["RUN_HEAD_SHA"]}
    # Is there anything to review at all? Asked before any candidate is
    # resolved and before any status is published, so a push run touches
    # nothing: no API reads about pull requests, no pending status left
    # behind, and count/panel skipped by `proceed == 'true'`.
    applicability = classify_triggering_run(observed_run)
    if not applicability["proceed"]:
        return _nothing_to_review(applicability)
    run_record = assert_triggering_run(observed_run)
    run_head = run_record["head_sha"]

    # Read the pull request list ONCE and ask both questions of the same
    # observation. Two reads would let the operational answer and the security
    # assertion describe two different moments — the exact class of defect the
    # settle work above exists to remove.
    pulls = api.open_pull_requests()
    # Is there still a candidate at this head? Merged, closed, and
    # pushed-again are ordinary development events, and the panel's answer to
    # them is "nothing to review", not a red run. No status is published on
    # either path, so a required panel check stays ABSENT rather than green.
    candidate = classify_candidate(pulls, run_head_sha=run_head)
    if not candidate["proceed"]:
        return _nothing_to_review(candidate)
    pull = resolve_pull_request(pulls, run_head_sha=run_head)
    assert_head_is_unmoved(run_head_sha=run_head,
                           current_head_sha=pull["head_sha"])
    # BOTH remaining reads go through `observation.settle`, and the reason is
    # measured rather than defensive: this panel fires within a second or two of
    # ordinary CI completing, GitHub is still writing the objects that describe
    # it, and three of the first five panel attempts refused on
    # `check_run_has_no_completed_at` — a record claiming to have completed with
    # no completion time. Nothing had failed. The gate asked slightly too early.
    #
    # No rule is relaxed by this. `settle` decides only WHEN to look; the
    # assertions below are the same functions, applied whole to a fresh read,
    # and only a complete observation can pass. A real red — a job that finished
    # badly — is refused on the first look and never waited on.
    #
    # This job holds NO credential, so the waiting costs nothing but wall clock.
    #
    # F-01. Read what ordinary CI ACTUALLY tested, out of the triggering run,
    # rather than comparing two values that both track the branch and therefore
    # move together.
    tested = observation.settle(
        read=lambda: (api.workflow_run(int(env["RUN_ID"])),
                      api.workflow_run_jobs(int(env["RUN_ID"]))),
        assertion=lambda pair:
            assert_triggering_ci_tested_this_exact_combination(
                pair[0], pair[1],
                event_run_id=int(env["RUN_ID"]), event_head_sha=run_head,
                current_head_sha=pull["head_sha"],
                current_base_sha=pull["base_sha"],
                main_head_sha=api.default_branch_head()),
        where="triggering-run-jobs", sleep=sleep)
    # Second, independent control: some run reported green on this exact
    # commit. A different question from "the run that triggered this panel was
    # green", and worth both.
    checks = observation.settle(
        read=lambda: api.check_runs(pull["head_sha"]),
        assertion=lambda runs: assert_ordinary_checks_green(
            runs, head_sha=pull["head_sha"]),
        where="ordinary-checks", sleep=sleep)

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
        "applicability": APPLICABLE,
        "applicability_reason": "ordinary CI passed on a pull request",
        "provider_attempts": 0,
        "generation_attempts": 0,
        "pr_number": pull["pr_number"],
        "head_sha": pull["head_sha"],
        "base_sha": pull["base_sha"],
        "high_risk": risk["high_risk"],
        "workflow_change": bool(risk["high_risk_paths"]),
        "engine_digest": engine_digest(roles=source_roles(release)),
        "policy_digest": _policy_digest(root),
        # Published so the count and panel jobs can bind them into evidence and
        # the human merge gate can compare them to the world at merge time.
        "triggering_ci_run_id": tested["triggering_ci_run_id"],
        "triggering_ci_run_attempt": tested["triggering_ci_run_attempt"],
        "tested_base_sha": tested["tested_base_sha"],
        "tested_head_sha": tested["tested_head_sha"],
        "review_class": review_class_for(pull["pr_number"]),
        "_trigger": run_record,
        # What the waiting cost, reported rather than hidden. A lane that
        # quietly retried would conceal the one number an operator would want:
        # whether GitHub's write lag is getting worse.
        "_observation": observation.summarise(tested, checks),
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
    settled = decision.get("_observation") or {}
    if settled.get("extra_observations"):
        # Surfaced in the job log, not only in the returned record. A retry
        # nobody can see is indistinguishable from a gate that never fired.
        print(f"::notice::ordinary-CI observation settled after "
              f"{settled['extra_observations']} extra read(s) over "
              f"{settled['waited_seconds']}s "
              f"({', '.join(settled['transient_categories'])}) — GitHub had "
              "not finished writing the check objects when this panel fired")
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
    # The module part AND the imported names, together. `or` made the second
    # a FALLBACK for the first, so for any `from <something> import ...` with a
    # real module name the imported symbols were never looked at — and
    # `from midtermpanel import transport`, the plain absolute spelling of the
    # relative form this guard does catch, sailed through while the self-test
    # printed `ok`. A guard that reports a property it cannot see is worse than
    # no guard, because it answers the question.
    imports_transport = any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        and "transport" in " ".join(
            [getattr(node, "module", "") or ""] + [a.name for a in node.names])
        for node in ast.walk(tree))
    return self_test_report("preflightcli", {
        "module imports": True,
        "decide is callable": callable(decide),
        "does not import the provider transport": not imports_transport,
        # AGAINST THE MODULE CONSTANT, not against a copy of the literal. The
        # old version compared a thirteen-name list to a byte-identical
        # thirteen-name list — `X <= X`, true for every possible state of the
        # program — under a label that said "eight" while `PUBLIC_OUTPUTS` has
        # seventeen. It named neither `PUBLIC_OUTPUTS` nor `decide`, so it could
        # not detect the exact failure the comment beside it describes: an
        # output the workflow declares and the CLI never emits, which resolves
        # to the empty string with no error.
        f"declares all {len(PUBLIC_OUTPUTS)} public outputs": set(
            REQUIRED_PUBLIC_OUTPUTS) <= set(PUBLIC_OUTPUTS),
    })


if __name__ == "__main__":
    if self_test_requested(sys.argv[1:]):
        raise SystemExit(_self_test())
    raise SystemExit(run(main, name="preflightcli"))
