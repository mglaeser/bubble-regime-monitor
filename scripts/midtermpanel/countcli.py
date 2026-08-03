"""`python -m midtermpanel.countcli` — count every model/batch pair, spend nothing else.

The first entry point permitted to hold the provider key. It publishes `pending`
before the first provider call so that a run which dies mid-count leaves a
visible unfinished check rather than nothing at all, and it publishes a terminal
state on every path out.
"""

from __future__ import annotations

import json
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


def perform(environ: dict, *, core, transport, opener, engine=None,
            engine_identity=None, skeleton=None, governed_policies=None,
            authorizations=None, challenge: str = None,
            repository_path: str = None) -> dict:
    """Package the engine's core result, persist it, publish terminal status.

    `core` is what `enginebridge.prepare_review_plan_core` returned. This
    function does not count — it packages what the engine counted. See the
    module docstring of `midtermpanel.count` for why that separation is the
    whole point.

    Takes the whole result of `count_through_engine` so the CLI can hand it
    over verbatim; the extra members travel into the evidence rather than being
    re-derived here."""
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
                                transport=transport, engine=engine)
    plan = executable_plan(
        counted=counted, core=core, skeleton=skeleton,
        repository_numeric_id=REPOSITORY_NUMERIC_ID, candidate_head_sha=head,
        candidate_base_sha=base, engine_digest=env["MIDTERM_ENGINE_DIGEST"],
        policy_digest=env["MIDTERM_POLICY_DIGEST"], challenge=challenge)

    record = count_evidence(
        repository_numeric_id=REPOSITORY_NUMERIC_ID, candidate_head_sha=head,
        candidate_base_sha=base, engine_digest=env["MIDTERM_ENGINE_DIGEST"],
        policy_digest=env["MIDTERM_POLICY_DIGEST"], run_id=run_id,
        run_attempt=attempt,
        body={"request_semantics_digest": counted["request_semantics_digest"],
              "final_units": counted["final_units"],
              "batches": counted["batches"],
              "total_input_tokens": counted["total_input_tokens"],
              "provider_calls": counted["provider_calls"],
              "generation_calls": 0,
              "transport_source": counted["transport_source"],
              "count_ledger_sha256": counted["count_ledger_sha256"],
              "preflight_manifest_sha256":
                  counted["preflight_manifest_sha256"],
              "engine_provenance": (engine_identity or {}).get("provenance"),
              "engine_artifact_sha256":
                  (engine_identity or {}).get("engine_artifact_sha256"),
              "approved_engine_source_sha":
                  (engine_identity or {}).get("approved_engine_source_sha"),
              "governed_policies": governed_policies,
              "literal_authorizations": authorizations,
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
        description=(f"counted {counted['final_units']} units in "
                     f"{counted['batches']} batches across 3 models "
                     "(mid-term, not write-separated)"),
        target_url=target, run_id=run_id, run_attempt=attempt,
        evidence_sha256=record["evidence_sha256"]),
        opener=opener, token=env["GITHUB_TOKEN"]))
    return {"published": published, "evidence": record, "plan": plan,
            "counted": counted, "paths": paths, "skeleton": skeleton,
            "repository_path": repository_path}


def count_through_engine(environ: dict, *, mode: str, opener,
                         transport_factory) -> dict:
    """Load the approved engine, build the skeleton, count. One transport.

    Written as one function taking its capabilities as parameters so the
    fake-provider vertical runs THIS code rather than a copy of it. The only
    difference between a dry run and a provider-backed run is `mode` and what
    `transport_factory` returns."""
    from trustedlane import enginebridge

    from . import inputs
    from .engine import load_engine_for_mode, resolve_release_config

    release = resolve_release_config(environ)
    loaded = inputs.load_count_inputs(environ)
    temp = _runner_temp(environ)
    workspace = environ.get("GITHUB_WORKSPACE", ".")

    opened = load_engine_for_mode(
        mode=mode, release=release,
        reviewed_candidate_head_sha=environ["CANDIDATE_HEAD_SHA"],
        artifact_path=os.path.join(temp, "midterm", "engine-count.tar.gz"),
        # Job-specific. `artifactload.extract` refuses a non-empty destination,
        # and it is right to: a mixture of an approved engine and whatever a
        # previous job left there is not one. Two jobs sharing a directory
        # would have worked on separate runners and failed the moment anything
        # ran them in one place — which is exactly what the vertical does.
        extract_to=os.path.join(temp, "midterm", "engine-count"),
        repository_numeric_id=REPOSITORY_NUMERIC_ID,
        candidate_paths=[loaded["repository_path"]], cwd=workspace)
    engine = opened["engine"]

    # Stage 1 in the ENGINE, from the repository's objects. The skeleton is
    # produced here rather than handed in: a skeleton from outside describes a
    # candidate nobody in this job derived.
    skeleton = enginebridge.build_skeleton(
        engine, target_base_sha=environ["CANDIDATE_BASE_SHA"],
        candidate_head_sha=environ["CANDIDATE_HEAD_SHA"],
        repository_path=loaded["repository_path"])
    authorizations, authorization_record = inputs.load_authorizations(
        environ, engine=engine, skeleton=skeleton)
    # The engine's own honest constructor, which labels values this repository
    # authored as TEST_FIXTURE_UNAUTHORIZED rather than as an approval.
    pin_record = inputs.build_pin_record(engine=engine,
                                         pins=loaded["pin_values"])

    governed = enginebridge.governed_policy_digests(
        engine, pin_values=pin_record["pins"])

    # ONE transport, for the whole count path. §8: the previous version built a
    # fresh one for the core and another for the accounting, so the totals the
    # evidence reported were a different object's totals from the ones that were
    # spent — and they agreed with each other, which is why nothing noticed.
    transport = transport_factory(engine)
    core = enginebridge.prepare_review_plan_core(
        engine, skeleton=skeleton, repository_path=loaded["repository_path"],
        pin_record=pin_record, transport=transport,
        authorizations=authorizations, challenge=loaded["challenge"])
    governed_match = enginebridge.assert_core_used_the_governed_policies(
        core, governed=governed)
    return {"engine": engine, "engine_identity": opened, "core": core,
            "skeleton": skeleton, "transport": transport,
            "governed_policies": governed_match,
            "authorizations": authorization_record,
            "challenge": loaded["challenge"],
            "repository_path": loaded["repository_path"], "opener": opener}


def main() -> None:
    """Obtain the engine, count through it, package the result.

    Every dangerous capability is obtained here and injected downward, so
    `perform` stays drivable by a stand-in provider and a fake opener."""
    import urllib.request

    from . import dryrun
    from .engine import MODE_DRY_RUN, MODE_PROVIDER
    from .transport import live_count_transport, read_provider_key

    environ = dict(os.environ)

    if dryrun.is_dry_run(environ):
        # The SAME function, the same engine, the same core call. Two objects
        # differ: a stand-in transport that cannot reach a socket, and a status
        # opener that writes a file. Everything the provider path does between
        # them is the code that runs here.
        dryrun.assert_no_credential_is_present(environ)
        sink = dryrun.status_sink(environ)
        counted = count_through_engine(
            environ, mode=MODE_DRY_RUN, opener=sink,
            transport_factory=dryrun.count_transport_factory(environ))
        environ.setdefault("MIDTERM_ENGINE_DIGEST",
                           counted["engine_identity"]["engine_source_digest"])
        outcome = perform(environ, **counted)
        proof = dryrun.record(environ, sink=sink,
                              transport=counted["transport"])
        print(json.dumps({"dry_run": proof,
                          "counted": outcome["counted"],
                          "plan_sha256": outcome["plan"]["plan_sha256"]},
                         indent=2, sort_keys=True, default=str))
        return

    key = read_provider_key(environ)
    ceiling = require_positive_int(environ, "MIDTERM_AUTHORIZED_INPUT_TOKENS")

    counted = count_through_engine(
        environ, mode=MODE_PROVIDER, opener=urllib.request.urlopen,
        transport_factory=lambda engine: live_count_transport(
            engine, opener=urllib.request.urlopen, key=key,
            authorized_input_tokens=ceiling))
    environ.setdefault("MIDTERM_ENGINE_DIGEST",
                       counted["engine_identity"]["engine_source_digest"])
    perform(environ, **counted)


def require_positive_int(environ: dict, name: str) -> int:
    """The operator's ceiling, as an integer, or a named refusal.

    `int(environ[name])` would raise `KeyError` or `ValueError` in the job
    holding the credential, and neither says which variable or who writes it."""
    raw = str(environ.get(name) or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        raise PanelRefusal(
            "MIDTERM_PANEL_REFUSED",
            f"category=ceiling_not_a_positive_integer variable={name} — the "
            "count transport refuses to exist without one, because a transport "
            "with no budget spends against a number nobody approved")
    return int(raw)


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
