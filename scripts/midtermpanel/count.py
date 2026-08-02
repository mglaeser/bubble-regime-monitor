"""Count through the approved engine, and package the result as mid-term evidence.

## This module is an adapter. That is the whole design.

The first draft of this file implemented batching, request assembly and counting
itself. That was wrong, and wrong in the specific way the mandate names: the
engine already does all of it. `enginebridge.prepare_review_plan_core` returns
the final units, the global preflight manifest, all three model counts, the
batches, the exact request hashes, the coverage proof and the cost gates — every
one computed by `scripts/verifier`, which is the package that actually implements
them and the package that took four macro-cycles to get right.

A second implementation would not have been a second implementation. It would
have been a FIRST implementation of subtly different rules, producing evidence
that looked identical and disagreed under load. So everything below either calls
the engine or packages what the engine returned.

What is genuinely mid-term-specific, and therefore lives here:

  * the evidence CLASS and the four honesty fields;
  * the executable plan's mid-term envelope;
  * the request-semantics digest that `dedupe` binds on;
  * the refusal that a count job must never have generated.

## The seam

`prepare_review_plan_core` takes a `transport`. That is what makes a fake-provider
vertical run possible without reimplementing anything: the same engine code path
runs, and only the thing at the end of the wire changes.
"""

from __future__ import annotations

from . import PANEL_MODELS
from .errors import refuse
from .evidence import digest_of

#: Keys the engine's core result must carry before this module will package it.
#:
#: Checked rather than assumed. The core is evidence-NEUTRAL by design — it
#: computes review semantics and decides nothing about trust — and a future
#: change to its shape must fail here rather than produce a plan with a missing
#: half that nothing notices.
REQUIRED_CORE_KEYS = ("units", "batches", "counts", "request_hashes")


def assert_core_is_countable(core: dict) -> dict:
    """The engine returned something this module can package.

    Deliberately not a schema validator — `enginebridge.assert_core_is_evidence_neutral`
    already checks the properties that matter for trust. This checks only that
    the fields this adapter is about to read are present, so a missing one is a
    named refusal rather than a `KeyError` in a credential-bearing job."""
    if not isinstance(core, dict):
        refuse("category=engine_core_not_a_mapping")
    missing = [k for k in REQUIRED_CORE_KEYS if k not in core]
    if missing:
        refuse(f"category=engine_core_missing_keys keys={missing} — the "
               "adapter reads exactly these; a core that stopped returning one "
               "would otherwise produce a plan with a silently empty half")
    if not core.get("units"):
        refuse("category=engine_core_returned_no_units — a review of nothing "
               "counts zero, costs nothing and aggregates to green")
    return {"units": len(core["units"]), "batches": len(core["batches"])}


def request_semantics_digest(core: dict, *, policy_digest: str) -> str:
    """What the models will actually be ASKED, as one digest.

    Over the engine's own request hashes rather than over anything recomputed
    here — the point is to bind to the requests that will be executed, and the
    engine is what produced them.

    Note this is NOT the candidate SHA. Two runs on the same head with a
    different engine or policy ask different questions, and `dedupe` binds on
    this so that a rerun in that situation performs a new review rather than
    reporting the previous answer."""
    return digest_of({
        "request_hashes": sorted(str(h) for h in core["request_hashes"]),
        "models": sorted(PANEL_MODELS),
        "policy_digest": policy_digest,
    })


def counted_from_core(core: dict, *, policy_digest: str, transport) -> dict:
    """Package the engine's core result. Counts nothing itself.

    `generation_calls` is read from the transport rather than from the core,
    because the property being asserted is about what this PROCESS did, and a
    core that mistakenly generated would be exactly the case where its own
    self-report could not be trusted."""
    assert_core_is_countable(core)
    generation_calls = int(getattr(transport, "generation_call_count", 0) or 0)
    if generation_calls:
        refuse(f"category=count_job_made_a_generation_call "
               f"calls={generation_calls} — the count job counts; generation is "
               "a separate job behind a separate approval")
    return {
        "units": len(core["units"]),
        "batches": len(core["batches"]),
        "counts": core["counts"],
        "request_hashes": [str(h) for h in core["request_hashes"]],
        "request_semantics_digest": request_semantics_digest(
            core, policy_digest=policy_digest),
        "provider_calls": int(getattr(transport, "call_count", 0) or 0),
        "generation_calls": 0,
        "preflight_manifest_present": bool(core.get("preflight_manifest")),
        "coverage_proof_present": bool(core.get("coverage")),
    }


def executable_plan(*, counted: dict, core: dict, repository_numeric_id: int,
                    candidate_head_sha: str, candidate_base_sha: str,
                    engine_digest: str, policy_digest: str) -> dict:
    """The private plan the panel must execute — the engine's requests, sealed.

    Private because the engine's requests carry the exact prompt bodies: the
    candidate's code and the reviewer's questions. Only the digest is ever
    published."""
    plan = {
        "schema_version": 1,
        "plan_kind": "MIDTERM_EXECUTABLE_REVIEW_PLAN",
        "repository_numeric_id": repository_numeric_id,
        "candidate_head_sha": candidate_head_sha,
        "candidate_base_sha": candidate_base_sha,
        "engine_digest": engine_digest,
        "policy_digest": policy_digest,
        "request_semantics_digest": counted["request_semantics_digest"],
        # The engine's own request hashes, in the engine's order. The panel
        # executes these and nothing else.
        "execution_request_hashes": list(counted["request_hashes"]),
        "batches": counted["batches"],
        "units": counted["units"],
        "counts": counted["counts"],
        "write_separated": False,
        "provider_secret_scope": "repository",  # pragma: allowlist secret
        "human_merge_required": True,
        "trusted_evidence_claim": False,
    }
    plan["plan_sha256"] = digest_of(plan)
    return plan


def assert_plan_is_executable(plan: dict) -> dict:
    """Refuse a plan whose self-digest, kind, content or honesty fields are wrong."""
    claimed = plan.get("plan_sha256")
    without = {k: v for k, v in plan.items() if k != "plan_sha256"}
    if not isinstance(claimed, str) or digest_of(without) != claimed:
        refuse("category=plan_self_digest_mismatch — the plan that arrived is "
               "not the plan that was written")
    if plan.get("plan_kind") != "MIDTERM_EXECUTABLE_REVIEW_PLAN":
        refuse(f"category=plan_wrong_kind kind={plan.get('plan_kind')!r}")
    if not plan.get("execution_request_hashes"):
        refuse("category=plan_has_no_execution_requests — an empty plan would "
               "execute nothing and aggregate to green")
    for field, required in (("write_separated", False),
                            ("trusted_evidence_claim", False),
                            ("human_merge_required", True),
                            ("provider_secret_scope", "repository")):
        if plan.get(field) != required:
            refuse(f"category=plan_honesty_field_altered field={field} "
                   f"got={plan.get(field)!r} required={required!r}")
    return {"execution_requests": len(plan["execution_request_hashes"]),
            "plan_sha256": claimed}
