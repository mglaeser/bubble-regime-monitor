"""`python -m midtermpanel.panelcli` — execute the counted plan, publish the review.

Consumes the artifact the count job uploaded IN THIS RUN. It does not rebuild the
plan: the plan carries the provider's counts, and a rebuild without calling the
provider has nothing to compare them against.
"""

from __future__ import annotations

import os
import sys

from . import (
    COUNT_EVIDENCE_CLASS,
    PANEL_EVIDENCE_CLASS,
    REPOSITORY_NUMERIC_ID,
    REVIEW_STATUS,
)
from .clibase import require_env, run, self_test_report, self_test_requested
from .count import assert_plan_is_executable
from .errors import PanelRefusal
from .evidence import panel_evidence, strict_load, write_atomic
from .panel import (
    aggregate,
    anti_copy_tripwire,
    assert_synthesis_cannot_clear_a_refutation,
    verify_handoff,
)
from .status import pending, publish, status_request

REQUIRED = ("GITHUB_TOKEN", "CANDIDATE_HEAD_SHA", "CANDIDATE_BASE_SHA",
            "MIDTERM_ENGINE_DIGEST", "MIDTERM_POLICY_DIGEST",
            "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")

#: Where the same-run download-artifact step places the count job's output.
INPUT_DIRNAME = "count-input"



def _runner_temp(environ: dict) -> str:
    """The runner's private temp directory. Refused when absent.

    Deliberately not defaulted to `/tmp`. The files written here are the private
    plan and the private verdicts — the candidate's code and the reviewer's
    questions — and a world-readable fallback is the wrong place for them. A
    missing `RUNNER_TEMP` means this is not running where it thinks it is, which
    is worth a refusal rather than a guess."""
    temp = str(environ.get("RUNNER_TEMP") or "").strip()
    if not temp:
        raise PanelRefusal(
            "MIDTERM_PANEL_REFUSED",
            "category=runner_temp_absent — private evidence must be written to "
            "the runner's own temp directory; falling back to /tmp would put "
            "the candidate's code and the reviewer's questions somewhere "
            "world-readable")
    return temp

def input_paths(temp: str) -> dict:
    base = os.path.join(temp, "midterm", INPUT_DIRNAME)
    return {"evidence": os.path.join(base, "count-evidence.json"),
            "plan": os.path.join(base, "executable-plan.json")}


def perform(environ: dict, *, execute_fn, opener, votes_challenge=None) -> dict:
    """Verify the handoff, execute, aggregate, publish. Injected seams only."""
    import json

    env = require_env(environ, REQUIRED, where="panelcli")
    head, base = env["CANDIDATE_HEAD_SHA"], env["CANDIDATE_BASE_SHA"]
    run_id, attempt = int(env["GITHUB_RUN_ID"]), int(env["GITHUB_RUN_ATTEMPT"])
    target = environ.get("MIDTERM_RUN_URL") or (
        "https://github.com/mglaeser/bubble-regime-monitor/actions/runs/"
        f"{run_id}")

    paths = input_paths(_runner_temp(environ))
    count_record = strict_load(
        paths["evidence"], expected_class=COUNT_EVIDENCE_CLASS,
        expected_head=head, expected_base=base,
        expected_engine_digest=env["MIDTERM_ENGINE_DIGEST"],
        expected_policy_digest=env["MIDTERM_POLICY_DIGEST"])
    with open(paths["plan"], "rb") as handle:
        plan = json.loads(handle.read().decode("utf-8"))
    assert_plan_is_executable(plan)
    verify_handoff(count_record=count_record, plan=plan, expected_head=head,
                   expected_base=base,
                   expected_engine_digest=env["MIDTERM_ENGINE_DIGEST"],
                   expected_policy_digest=env["MIDTERM_POLICY_DIGEST"])

    published = [publish(
        pending(candidate_head_sha=head, context=REVIEW_STATUS,
                target_url=target, run_id=run_id, run_attempt=attempt),
        opener=opener, token=env["GITHUB_TOKEN"])]

    executed = execute_fn(plan=plan)
    verdict = aggregate(votes=executed["votes"], challenge=votes_challenge)
    tripwire = anti_copy_tripwire(executed["votes"])

    state = "success" if verdict["decision"] == "approved" else "failure"
    assert_synthesis_cannot_clear_a_refutation(verdict, proposed_state=state)

    record = panel_evidence(
        repository_numeric_id=REPOSITORY_NUMERIC_ID, candidate_head_sha=head,
        candidate_base_sha=base, engine_digest=env["MIDTERM_ENGINE_DIGEST"],
        policy_digest=env["MIDTERM_POLICY_DIGEST"], run_id=run_id,
        run_attempt=attempt,
        body={"decision": verdict["decision"],
              "models_voting": verdict["models_voting"],
              "required_approver": verdict["required_approver"],
              "strict_any_refutation": verdict["strict_any_refutation"],
              "votes": verdict["votes"],
              "generation_calls": executed["generation_calls"],
              "anti_copy": tripwire,
              "plan_sha256": plan["plan_sha256"]})
    write_atomic(record, os.path.join(_runner_temp(environ),
                                      "midterm", "panel-evidence.json"))

    published.append(publish(status_request(
        repository_numeric_id=REPOSITORY_NUMERIC_ID, candidate_head_sha=head,
        context=REVIEW_STATUS, state=state,
        description=(f"panel {verdict['decision']}: "
                     f"{len(verdict['models_voting'])} models "
                     "(mid-term, not write-separated)"),
        target_url=target, run_id=run_id, run_attempt=attempt,
        evidence_sha256=record["evidence_sha256"] if state == "success" else None
        ) if state == "success" else status_request(
        repository_numeric_id=REPOSITORY_NUMERIC_ID, candidate_head_sha=head,
        context=REVIEW_STATUS, state=state,
        description=(f"panel {verdict['decision']} — see run summary "
                     "(mid-term, not write-separated)"),
        target_url=target, run_id=run_id, run_attempt=attempt),
        opener=opener, token=env["GITHUB_TOKEN"]))
    return {"published": published, "verdict": verdict, "evidence": record}


def main() -> None:
    import urllib.request

    from trustedlane import enginebridge

    from .engine import (
        assert_provenance_permits_real_panel,
        build_test_only_artifact,
        open_engine,
    )
    from .panel import execute
    from .transport import LiveProviderTransport, read_provider_key

    environ = dict(os.environ)
    key = read_provider_key(environ)
    roles = {"protected_trusted_lane": environ["MIDTERM_ENGINE_PROTECTED_SHA"],
             "candidate_verifier": environ["MIDTERM_ENGINE_CANDIDATE_SHA"]}
    temp = _runner_temp(environ)
    built = build_test_only_artifact(
        destination=os.path.join(temp, "midterm", "engine.tar.gz"), roles=roles,
        repository_numeric_id=REPOSITORY_NUMERIC_ID,
        cwd=environ.get("GITHUB_WORKSPACE", "."))
    assert_provenance_permits_real_panel(built["provenance"])
    engine = open_engine(os.path.join(temp, "midterm", "engine.tar.gz"),
                         destination=os.path.join(temp, "midterm", "engine"),
                         expected_sha256=built["engine_artifact_sha256"])
    transport = LiveProviderTransport(key=key)
    perform(environ,
            execute_fn=lambda *, plan: execute(engine=engine, plan=plan,
                                               transport=transport),
            opener=urllib.request.urlopen,
            votes_challenge=environ.get("MIDTERM_CHALLENGE"))
    _ = enginebridge


def _self_test() -> int:
    return self_test_report("panelcli", {
        "module imports": True,
        "perform is callable": callable(perform),
        "reads the same-run artifact directory":
            INPUT_DIRNAME in input_paths("/x")["plan"],
        "panel evidence class is the mid-term one":
            PANEL_EVIDENCE_CLASS.startswith("MIDTERM_"),
    })


if __name__ == "__main__":
    if self_test_requested(sys.argv[1:]):
        raise SystemExit(_self_test())
    raise SystemExit(run(main, name="panelcli"))
