"""`python -m midtermpanel.countcli` — count every model/batch pair, spend nothing else.

The first entry point permitted to hold the provider key. It publishes `pending`
before the first provider call so that a run which dies mid-count leaves a
visible unfinished check rather than nothing at all, and it publishes a terminal
state on every path out.
"""

from __future__ import annotations

import os
import sys

from . import COUNT_EVIDENCE_CLASS, COUNT_STATUS, REPOSITORY_NUMERIC_ID
from .clibase import require_env, run, self_test_report, self_test_requested
from .count import counted_from_core, executable_plan
from .errors import PanelRefusal
from .evidence import count_evidence, strict_load, write_atomic
from .status import pending, publish, status_request

REQUIRED = ("GITHUB_TOKEN", "CANDIDATE_HEAD_SHA", "CANDIDATE_BASE_SHA",
            "CANDIDATE_PR_NUMBER", "MIDTERM_ENGINE_DIGEST",
            "MIDTERM_POLICY_DIGEST", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")



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

def artifact_paths(temp: str) -> dict:
    base = os.path.join(temp, "midterm")
    return {"evidence": os.path.join(base, "count-evidence.json"),
            "plan": os.path.join(base, "executable-plan.json")}


def perform(environ: dict, *, core, transport, opener, root: str = ".") -> dict:
    """Package the engine's core result, persist it, publish terminal status.

    `core` is what `enginebridge.prepare_review_plan_core` returned. This
    function does not count — it packages what the engine counted. See the
    module docstring of `midtermpanel.count` for why that separation is the
    whole point."""
    env = require_env(environ, REQUIRED, where="countcli")
    head, base = env["CANDIDATE_HEAD_SHA"], env["CANDIDATE_BASE_SHA"]
    run_id, attempt = int(env["GITHUB_RUN_ID"]), int(env["GITHUB_RUN_ATTEMPT"])
    target = environ.get("MIDTERM_RUN_URL") or (
        "https://github.com/mglaeser/bubble-regime-monitor/actions/runs/"
        f"{run_id}")

    published = []
    published.append(publish(
        pending(candidate_head_sha=head, context=COUNT_STATUS,
                target_url=target, run_id=run_id, run_attempt=attempt),
        opener=opener, token=env["GITHUB_TOKEN"]))

    counted = counted_from_core(core, policy_digest=env["MIDTERM_POLICY_DIGEST"],
                                transport=transport)
    plan = executable_plan(
        counted=counted, core=core,
        repository_numeric_id=REPOSITORY_NUMERIC_ID, candidate_head_sha=head,
        candidate_base_sha=base, engine_digest=env["MIDTERM_ENGINE_DIGEST"],
        policy_digest=env["MIDTERM_POLICY_DIGEST"])

    record = count_evidence(
        repository_numeric_id=REPOSITORY_NUMERIC_ID, candidate_head_sha=head,
        candidate_base_sha=base, engine_digest=env["MIDTERM_ENGINE_DIGEST"],
        policy_digest=env["MIDTERM_POLICY_DIGEST"], run_id=run_id,
        run_attempt=attempt,
        body={"request_semantics_digest": counted["request_semantics_digest"],
              "units": counted["units"], "batches": counted["batches"],
              "provider_calls": counted["provider_calls"],
              "generation_calls": 0,
              "plan_sha256": plan["plan_sha256"]})

    paths = artifact_paths(_runner_temp(environ))
    write_atomic(record, paths["evidence"])
    write_atomic(plan, paths["plan"])
    # Read back what was just written. A file that cannot be strict-loaded here
    # is a handoff that fails in the panel job, and failing now costs nothing.
    strict_load(paths["evidence"], expected_class=COUNT_EVIDENCE_CLASS,
                expected_head=head, expected_base=base)

    published.append(publish(status_request(
        repository_numeric_id=REPOSITORY_NUMERIC_ID, candidate_head_sha=head,
        context=COUNT_STATUS, state="success",
        description=(f"counted {counted['units']} units in "
                     f"{counted['batches']} batches across 3 models "
                     "(mid-term, not write-separated)"),
        target_url=target, run_id=run_id, run_attempt=attempt,
        evidence_sha256=record["evidence_sha256"]),
        opener=opener, token=env["GITHUB_TOKEN"]))
    return {"published": published, "evidence": record, "plan": plan,
            "counted": counted, "paths": paths}


def main() -> None:
    """Obtain the engine, count through it, package the result.

    Every dangerous capability is obtained here and injected downward, so
    `perform` stays drivable by a fake provider and a fake opener."""
    import urllib.request

    from trustedlane import enginebridge

    from .engine import (
        assert_provenance_permits_real_panel,
        build_test_only_artifact,
        engine_digest,
        open_engine,
    )
    from .transport import LiveProviderTransport, read_provider_key

    environ = dict(os.environ)
    key = read_provider_key(environ)
    roles = {"protected_trusted_lane": environ["MIDTERM_ENGINE_PROTECTED_SHA"],
             "candidate_verifier": environ["MIDTERM_ENGINE_CANDIDATE_SHA"]}
    environ.setdefault("MIDTERM_ENGINE_DIGEST", engine_digest(roles=roles))

    temp = _runner_temp(environ)
    built = build_test_only_artifact(
        destination=os.path.join(temp, "midterm", "engine.tar.gz"), roles=roles,
        repository_numeric_id=REPOSITORY_NUMERIC_ID,
        cwd=environ.get("GITHUB_WORKSPACE", "."))
    # A locally rebuilt artifact is reproducible and approved by nobody. It may
    # back a dry run; it may not back a run that spends money.
    assert_provenance_permits_real_panel(built["provenance"])

    engine = open_engine(
        os.path.join(temp, "midterm", "engine.tar.gz"),
        destination=os.path.join(temp, "midterm", "engine"),
        expected_sha256=built["engine_artifact_sha256"])
    core = enginebridge.prepare_review_plan_core(
        engine, skeleton=environ["MIDTERM_SKELETON_PATH"],
        repository_path=environ.get("GITHUB_WORKSPACE", "."),
        pin_record=environ["MIDTERM_PIN_RECORD"],
        transport=LiveProviderTransport(key=key),
        authorizations=environ.get("MIDTERM_AUTHORIZATIONS"),
        challenge=environ["MIDTERM_CHALLENGE"])
    perform(environ, core=core,
            transport=LiveProviderTransport(key=key),
            opener=urllib.request.urlopen,
            root=environ.get("GITHUB_WORKSPACE", "."))


def _self_test() -> int:
    return self_test_report("countcli", {
        "module imports": True,
        "perform is callable": callable(perform),
        "artifact paths under RUNNER_TEMP": artifact_paths("/x")["plan"]
        .startswith("/x/midterm/"),
        "count evidence class is the mid-term one":
            COUNT_EVIDENCE_CLASS.startswith("MIDTERM_"),
    })


if __name__ == "__main__":
    if self_test_requested(sys.argv[1:]):
        raise SystemExit(_self_test())
    raise SystemExit(run(main, name="countcli"))
