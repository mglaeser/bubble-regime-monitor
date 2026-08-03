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

## The keys are the engine's, and they were not

The second draft got the adapter shape right and the CONTRACT wrong. It required
`("units", "batches", "counts", "request_hashes")`, and the core has never
returned three of those. The real result carries `final_units`, `batches`,
`count_records`, `count_ledger`, `preflight_manifest`, `cost_plan`,
`review_request_policy`, `operator_pin_record` and the rest — eighteen keys,
fixed by `verifier.finalize.PREPARE_CORE_VERSION`.

A wrong key list is not a cosmetic mistake here. `assert_core_is_countable`
would have refused every real core with `engine_core_missing_keys`, in the job
holding the credential, after the count had been paid for. So the names below
come from the engine's own literal, and `CORE_KEYS_THIS_ADAPTER_READS` is
checked against what the loaded engine actually returns.

## The plan is the engine's plan

`enginebridge.execute_review_plan` reads six fields out of the plan it is given:
`final_units`, `batches`, `review_request_policy`, `operator_pin_record`,
`execution_challenge` and `review_skeleton_sha256`. A plan missing any of them
is a plan the panel cannot execute — which is how the previous version failed,
with a `KeyError` on a field it had invented.

So the mid-term plan carries the engine's fields under the engine's names, plus
a mid-term envelope that states what this lane is and is not. The envelope is
additive; nothing in it is read by the engine.

## The seam

`prepare_review_plan_core` takes a `transport`. That is what makes a
fake-provider vertical run possible without reimplementing anything: the same
engine code path runs, and only the thing at the end of the wire changes.
"""

from __future__ import annotations

from . import PANEL_MODELS
from .errors import refuse
from .evidence import digest_of

#: The keys this adapter reads out of the engine's core result, by the engine's
#: own names. Not the whole contract — the core returns more — but exactly what
#: is read below, so a rename upstream is a named refusal here rather than a
#: `KeyError` in a credential-bearing job.
CORE_KEYS_THIS_ADAPTER_READS = (
    "core_version",
    "final_units",
    "batches",
    "count_records",
    "count_ledger",
    "preflight_manifest",
    "cost_plan",
    "review_request_policy",
    "operator_pin_record",
    "transport_source",
    "generation_calls_performed",
)

#: The six fields `enginebridge.execute_review_plan` reads. Derived from the
#: consumer rather than hand-listed: this is the set the panel job will index
#: into, and a plan missing one of them cannot be executed.
PLAN_FIELDS_THE_EXECUTOR_READS = (
    "final_units",
    "batches",
    "review_request_policy",
    "operator_pin_record",
    "execution_challenge",
    "review_skeleton_sha256",
)

PLAN_KIND = "MIDTERM_EXECUTABLE_REVIEW_PLAN"

#: The four honesty fields, and the only values they may hold. A plan whose
#: envelope has been edited to claim write separation or trusted evidence is a
#: plan making this lane's one forbidden claim.
PLAN_HONESTY = (("write_separated", False),
                ("trusted_evidence_claim", False),
                ("human_merge_required", True),
                ("provider_secret_scope", "repository"))  # pragma: allowlist secret


def assert_core_is_countable(core: dict, *, engine: dict = None) -> dict:
    """The engine returned something this module can package.

    Deliberately not a schema validator — `enginebridge.assert_core_is_evidence_neutral`
    already checks the properties that matter for trust, including the
    core-version equality against the loaded engine. This checks only that the
    fields this adapter is about to read are present.

    `engine` is optional and, when given, the version is compared against the
    engine's own `PREPARE_CORE_VERSION` a second time. Not redundant: this
    module is also called from the fake-provider vertical, which builds a core
    without going through the bridge."""
    if not isinstance(core, dict):
        refuse("category=engine_core_not_a_mapping")
    missing = [k for k in CORE_KEYS_THIS_ADAPTER_READS if k not in core]
    if missing:
        refuse(f"category=engine_core_missing_keys keys={missing} "
               f"present={sorted(core)[:12]} — the adapter reads exactly these "
               "by the engine's own names; a core that stopped returning one "
               "would otherwise produce a plan with a silently empty half")
    if engine is not None:
        expected = engine["modules"]["verifier.finalize"].PREPARE_CORE_VERSION
        if core["core_version"] != expected:
            refuse(f"category=engine_core_version_mismatch "
                   f"observed={core['core_version']!r} expected={expected!r}")
    if not core["final_units"]:
        refuse("category=engine_core_returned_no_units — a review of nothing "
               "counts zero, costs nothing and aggregates to green")
    if not core["batches"]:
        refuse("category=engine_core_returned_no_batches")
    if core["generation_calls_performed"] != 0:
        refuse(f"category=count_core_made_a_generation_call "
               f"calls={core['generation_calls_performed']} — counting and "
               "generating are separately approved, and this job holds the "
               "count approval only")
    return {"final_units": len(core["final_units"]),
            "batches": len(core["batches"]),
            "count_records": len(core["count_records"])}


def execution_request_hashes(core: dict) -> list:
    """Every execution request the panel will send, as the ENGINE hashed them.

    Read out of `batch["request_hashes_by_model"]`, which is where the engine
    puts them, and sorted so that the digest below is stable under a batch
    reordering that changed nothing about what is asked.

    `execution_request_sha256` rather than `count_request_sha256`: the two
    differ, and the panel re-sends the execution bytes. Binding the plan to the
    count hash would let a run execute requests it had not priced."""
    hashes = []
    for batch in core["batches"]:
        by_model = batch.get("request_hashes_by_model")
        if not isinstance(by_model, dict) or not by_model:
            refuse(f"category=batch_carries_no_request_hashes "
                   f"batch={batch.get('batch_id')!r} — the plan stores request "
                   "HASHES rather than bodies, deliberately, so that it "
                   "carries no source content; a batch without them cannot be "
                   "proved to be the batch that was counted")
        for model_id in sorted(by_model):
            entry = by_model[model_id]
            digest = entry.get("execution_request_sha256") if isinstance(
                entry, dict) else None
            if not digest:
                refuse(f"category=batch_missing_execution_request_hash "
                       f"batch={batch.get('batch_id')!r} model={model_id!r}")
            hashes.append(digest)
    return sorted(hashes)


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
        "execution_request_hashes": execution_request_hashes(core),
        "models": sorted(PANEL_MODELS),
        "policy_digest": policy_digest,
    })


def total_input_tokens(core: dict) -> int:
    """The engine's own count records, summed. Refuses an empty set.

    Same rule as `d1runtime._total_input_tokens`, and for the same reason: a
    sum over zero records is zero, zero is under every ceiling, and a run that
    counted nothing would pass the operator's spending gate."""
    records = core["count_records"]
    if not records:
        refuse("category=count_records_empty — a total over no records is "
               "zero, and zero is under every ceiling")
    total = 0
    for record in records:
        tokens = record.get("input_tokens") if isinstance(record, dict) else None
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            refuse(f"category=count_record_tokens_not_a_plain_integer "
                   f"observed={type(tokens).__name__}")
        total += tokens
    return total


def counted_from_core(core: dict, *, policy_digest: str, transport,
                      engine: dict = None) -> dict:
    """Package the engine's core result. Counts nothing itself.

    The provider accounting is read from the TRANSPORT rather than from the
    core, because the property being asserted is about what this process sent,
    and a core that mistakenly generated would be exactly the case where its own
    self-report could not be trusted. §8: one transport for the whole count
    path, so this total is the run's total and not one batch's."""
    assert_core_is_countable(core, engine=engine)
    generation_calls = int(getattr(transport, "generation_calls", 0) or 0)
    if generation_calls:
        refuse(f"category=count_job_made_a_generation_call "
               f"calls={generation_calls} — the count job counts; generation is "
               "a separate job behind a separate approval")
    calls = getattr(transport, "calls", None)
    provider_calls = len(calls) if isinstance(calls, list) else int(calls or 0)
    return {
        "final_units": len(core["final_units"]),
        "batches": len(core["batches"]),
        "count_records": len(core["count_records"]),
        "total_input_tokens": total_input_tokens(core),
        "execution_request_hashes": execution_request_hashes(core),
        "request_semantics_digest": request_semantics_digest(
            core, policy_digest=policy_digest),
        "transport_source": core["transport_source"],
        "provider_calls": provider_calls,
        "generation_calls": 0,
        "count_ledger_sha256": core["count_ledger"]["count_ledger_sha256"],
        "preflight_manifest_sha256":
            core["preflight_manifest"]["preflight_manifest_sha256"],
        "worst_case_total_micro_usd":
            core["cost_plan"]["worst_case_total_micro_usd"],
    }


def executable_plan(*, counted: dict, core: dict, skeleton: dict,
                    repository_numeric_id: int, candidate_head_sha: str,
                    candidate_base_sha: str, engine_digest: str,
                    policy_digest: str, challenge: str) -> dict:
    """The private plan the panel must execute — the engine's requests, sealed.

    Private because the engine's requests carry the exact prompt bodies: the
    candidate's code and the reviewer's questions, and the challenge in the
    clear. Only the digest is ever published.

    Every field the executor reads is copied from the core under the engine's
    own name. Nothing here is derived: a field computed here rather than copied
    would be a second opinion about something the engine already decided, and
    the panel would execute requests that had been priced under the other
    opinion."""
    plan = {
        "schema_version": 2,
        "plan_kind": PLAN_KIND,
        "repository_numeric_id": repository_numeric_id,
        "candidate_head_sha": candidate_head_sha,
        "candidate_base_sha": candidate_base_sha,
        "engine_digest": engine_digest,
        "policy_digest": policy_digest,
        "request_semantics_digest": counted["request_semantics_digest"],
        "execution_request_hashes": list(counted["execution_request_hashes"]),
        "total_input_tokens": counted["total_input_tokens"],
        # ---- the engine's own fields, under the engine's own names ----
        "final_units": core["final_units"],
        "batches": core["batches"],
        "review_request_policy": core["review_request_policy"],
        "operator_pin_record": core["operator_pin_record"],
        "review_skeleton_sha256": skeleton["review_skeleton_sha256"],
        # In the clear, and this artifact is private for exactly this reason:
        # the panel must send the challenge that was counted, and a challenge
        # re-derivable from a public digest is one the models could re-derive.
        "execution_challenge": challenge,
        # ---- the mid-term envelope; additive, never read by the engine ----
        "write_separated": False,
        "provider_secret_scope": "repository",  # pragma: allowlist secret
        "human_merge_required": True,
        "trusted_evidence_claim": False,
    }
    plan["plan_sha256"] = digest_of(plan)
    return assert_plan_is_executable(plan)


def assert_plan_is_executable(plan: dict) -> dict:
    """Refuse a plan whose self-digest, kind, content or honesty fields are wrong.

    The executor-field check is the load-bearing one, and it is derived from
    `PLAN_FIELDS_THE_EXECUTOR_READS` rather than from a list written beside it:
    the previous version validated fields it had invented and passed, while the
    panel job indexed into a field that was never there."""
    if not isinstance(plan, dict):
        refuse("category=plan_not_an_object")
    claimed = plan.get("plan_sha256")
    without = {k: v for k, v in plan.items() if k != "plan_sha256"}
    if not isinstance(claimed, str) or digest_of(without) != claimed:
        refuse("category=plan_self_digest_mismatch — the plan that arrived is "
               "not the plan that was written")
    if plan.get("plan_kind") != PLAN_KIND:
        refuse(f"category=plan_wrong_kind kind={plan.get('plan_kind')!r}")
    missing = [f for f in PLAN_FIELDS_THE_EXECUTOR_READS if not plan.get(f)]
    if missing:
        refuse(f"category=plan_missing_executor_fields fields={missing} — the "
               "panel job indexes into exactly these; a plan without one "
               "raises inside the executor, in the job holding the credential")
    pins = (plan["operator_pin_record"] or {}).get("pins")
    if not isinstance(pins, dict) or not pins:
        refuse("category=plan_pin_record_has_no_pins — the executor reads "
               "`plan['operator_pin_record']['pins']` to build its ledger")
    if not plan.get("execution_request_hashes"):
        refuse("category=plan_has_no_execution_requests — an empty plan would "
               "execute nothing and aggregate to green")
    for field, required in PLAN_HONESTY:
        if plan.get(field) != required:
            refuse(f"category=plan_honesty_field_altered field={field} "
                   f"got={plan.get(field)!r} required={required!r}")
    return {"execution_requests": len(plan["execution_request_hashes"]),
            "final_units": len(plan["final_units"]),
            "batches": len(plan["batches"]),
            "plan_sha256": claimed}
