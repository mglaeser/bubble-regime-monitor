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
from .evidence import (
    count_evidence,
    strict_load,
    strict_load_plan,
    write_atomic,
)
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
            repository_path: str = None, pin_profile=None,
            pin_authority=None, already_published=None) -> dict:
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

    # `pending` is NOT published here. `main` publishes it before the first
    # provider-capable operation, and this function runs after the count. The
    # module docstring used to claim pending came first while the executable
    # order was the reverse: a timeout during the first count left no visible
    # unfinished check, and money could be spent before the pull request said
    # counting had begun.
    published = list(already_published or ())

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
              # The BUILDER's identity record, not the approval label above.
              # Binding these into the evidence is what makes "which build
              # produced the engine that reviewed this, and under what
              # control" answerable from the evidence alone — previously the
              # identity document was never read, so the honest answer was
              # "nobody in this job knew".
              "engine_identity_sha256":
                  (engine_identity or {}).get("engine_identity_sha256"),
              "engine_identity_state":
                  (engine_identity or {}).get("engine_identity_state"),
              "engine_native_branch_protection":
                  (engine_identity or {}).get("native_branch_protection"),
              "engine_control_class":
                  (engine_identity or {}).get("control_class"),
              "engine_build_run_id":
                  (engine_identity or {}).get("engine_build_run_id"),
              "engine_build_run_attempt":
                  (engine_identity or {}).get("engine_build_run_attempt"),
              "engine_provenance_sha256":
                  (engine_identity or {}).get("engine_provenance_sha256"),
              "approved_engine_source_sha":
                  (engine_identity or {}).get("approved_engine_source_sha"),
              "governed_policies": governed_policies,
              # Which budget this run spent under, and on whose authority.
              "pin_profile": pin_profile,
              "pin_authority": pin_authority,
              # What ordinary CI actually tested, carried from preflight. The
              # merge gate compares these to the world at merge time, so a
              # panel that reviewed a combination CI never ran cannot be
              # merged on its own green.
              "triggering_ci_run_id": environ.get("MIDTERM_TRIGGERING_CI_RUN_ID"),
              "triggering_ci_run_attempt":
                  environ.get("MIDTERM_TRIGGERING_CI_RUN_ATTEMPT"),
              "tested_base_sha": environ.get("MIDTERM_TESTED_BASE_SHA"),
              "tested_head_sha": environ.get("MIDTERM_TESTED_HEAD_SHA"),
              "panel_run_id": run_id,
              "panel_run_attempt": attempt,
              "literal_authorizations": authorizations,
              "plan_sha256": plan["plan_sha256"]})

    paths = artifact_paths(_runner_temp(environ))
    write_atomic(record, paths["evidence"])
    write_atomic(plan, paths["plan"])
    # Read BOTH back through the strict loaders, here, before the artifact is
    # uploaded. A malformed count output should fail in the count job — where
    # the cause is one step away — rather than in the panel job, where it looks
    # like a corrupted handoff. Costs nothing: the bytes are in page cache.
    strict_load(paths["evidence"], expected_class=COUNT_EVIDENCE_CLASS,
                expected_head=head, expected_base=base)
    strict_load_plan(paths["plan"], expected_head=head, expected_base=base,
                     expected_engine_digest=env["MIDTERM_ENGINE_DIGEST"],
                     expected_policy_digest=env["MIDTERM_POLICY_DIGEST"])

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


def prepare_count_context(environ: dict, *, mode: str) -> dict:
    """Everything before the first provider-capable operation. NO transport.

    Split out of the old `count_through_engine` so that
    `midterm-panel-count = pending` can be published between preparation and
    the first count call. Nothing in here can reach a socket: the engine is
    built from local git objects, the skeleton is derived from the repository,
    and the policy digests are pure functions of the panel and the pins.

    That split is the whole point of the split — see `main`."""
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

    return {"engine": engine, "engine_identity": opened, "skeleton": skeleton,
            "pin_record": pin_record, "governed": governed,
            "authorizations": authorizations,
            "authorization_record": authorization_record,
            "pin_profile": loaded["pin_profile"],
            "pin_authority": loaded["pin_authority"],
            "transport_ceilings": loaded["transport_ceilings"],
            "challenge": loaded["challenge"],
            "repository_path": loaded["repository_path"]}


def execute_count_context(context: dict, *, transport_factory) -> dict:
    """The first provider-capable operation, and the only one in the count job.

    ONE transport, for the whole count path: an earlier version built a fresh
    one for the core and another for the accounting, so the totals the evidence
    reported were a different object's totals from the ones that were spent —
    and they agreed with each other, which is why nothing noticed."""
    from trustedlane import enginebridge

    transport = transport_factory(context["engine"])
    core = enginebridge.prepare_review_plan_core(
        context["engine"], skeleton=context["skeleton"],
        repository_path=context["repository_path"],
        pin_record=context["pin_record"], transport=transport,
        authorizations=context["authorizations"],
        challenge=context["challenge"])
    governed_match = enginebridge.assert_core_used_the_governed_policies(
        core, governed=context["governed"])
    return {"engine": context["engine"],
            "engine_identity": context["engine_identity"],
            "core": core, "skeleton": context["skeleton"],
            "transport": transport, "governed_policies": governed_match,
            "authorizations": context["authorization_record"],
            "pin_profile": context["pin_profile"],
            "pin_authority": context["pin_authority"],
            "challenge": context["challenge"],
            "repository_path": context["repository_path"]}


def count_through_engine(environ: dict, *, mode: str, opener,
                         transport_factory) -> dict:
    """Prepare, then execute. Kept for callers that do not publish a status.

    `opener` is accepted and returned so the CLI can hand the whole result to
    `perform` verbatim; nothing here uses it."""
    context = prepare_count_context(environ, mode=mode)
    return {**execute_count_context(context,
                                    transport_factory=transport_factory),
            "opener": opener}


def main() -> None:
    """Obtain the engine, count through it, package the result.

    The ORDER is the finding this function exists to fix. Every dangerous
    capability is still obtained here and injected downward, but `pending` is
    now published between preparation and the first provider-capable operation:

        validate env and identity
        prepare (engine, skeleton, pins, policies — no transport)
        PUBLISH count pending
        obtain the credential
        first provider count
        write and validate plan and evidence
        publish count terminal

    Before this, `perform` published pending after the engine had already
    counted. A timeout during the first count therefore left no visible
    unfinished check, and money could be spent before the pull request said
    counting had begun."""
    import urllib.request

    from . import dryrun
    from .engine import MODE_DRY_RUN, MODE_PROVIDER
    from .transport import live_count_transport, read_provider_key

    environ = dict(os.environ)
    dry = dryrun.is_dry_run(environ)
    mode = MODE_DRY_RUN if dry else MODE_PROVIDER
    if dry:
        dryrun.assert_no_credential_is_present(environ)
        sink = dryrun.status_sink(environ)
        opener = sink
    else:
        sink = None
        opener = urllib.request.urlopen

    # 1. Everything local. Nothing here can reach a socket.
    context = prepare_count_context(environ, mode=mode)
    environ.setdefault("MIDTERM_ENGINE_DIGEST",
                       context["engine_identity"]["engine_source_digest"])

    # 2. Pending, BEFORE any capability that could spend.
    env = require_env(environ, REQUIRED, where="countcli")
    head = env["CANDIDATE_HEAD_SHA"]
    run_id = int(env["GITHUB_RUN_ID"])
    attempt = int(env["GITHUB_RUN_ATTEMPT"])
    target = environ.get("MIDTERM_RUN_URL") or (
        "https://github.com/mglaeser/bubble-regime-monitor/actions/runs/"
        f"{run_id}")
    published = [publish(
        pending(candidate_head_sha=head, context=COUNT_STATUS,
                target_url=target, run_id=run_id, run_attempt=attempt),
        opener=opener, token=env["GITHUB_TOKEN"])]

    # 3. Only now is a provider-capable transport constructed. The ceilings
    #    come from the selected operator profile, never from a workflow literal.
    ceilings = context["transport_ceilings"]
    if dry:
        factory = dryrun.count_transport_factory(environ)
    else:
        key = read_provider_key(environ)

        def factory(engine):
            return live_count_transport(
                engine, opener=urllib.request.urlopen, key=key,
                authorized_input_tokens=ceilings[
                    "authorized_total_input_tokens"])

    counted = execute_count_context(context, transport_factory=factory)
    outcome = perform(environ, opener=opener, already_published=published,
                      **counted)

    if dry:
        proof = dryrun.record(environ, sink=sink,
                              transport=counted["transport"])
        print(json.dumps({"dry_run": proof,
                          "counted": outcome["counted"],
                          "pin_profile": context["pin_profile"],
                          "plan_sha256": outcome["plan"]["plan_sha256"]},
                         indent=2, sort_keys=True, default=str))


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
