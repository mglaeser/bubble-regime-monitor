"""Stage 2: the online finalizer (MC3 §13/§36-§39).

Order matters, and every step is a gate:

  1. strict-parse the supplied skeleton (recomputing all derived claims);
  2. prove the recorded commits still exist;
  3. REBUILD the skeleton from those commits under the CURRENT execution
     policy and compare canonical bytes — drift means a stale plan, and no
     count call happens (MC2-F11);
  4. validate the complete operator PIN record against the capability policy;
  5. re-derive unit content from the commits (the skeleton stores no content);
  6. secret preflight on the EXACT assembled request bodies;
  7. exact per-model token counts through the injected transport, with
     recursive atom-boundary splitting for any unit that does not fit;
  8. deterministic batch packing, re-counted per batch and per model;
  9. context and cost gates under the operator PINs;
 10. a strict, private ExecutableReviewPlan.

ZERO generation calls happen here, ever. `executable` is true only when the
counts came from a real provider transport: a mock count may never be
represented as a real one.
"""

from __future__ import annotations

import os
import sys

from . import (
    artifact,
    atoms,
    batching,
    capabilities,
    counting,
    coverage,
    gitdiff,
    policy,
    preflight,
    providerreq,
    rawchange,
    repostate,
)
from . import (
    pins as pinsmod,
)
from . import (
    plan as planmod,
)
from .canon import canonical_json, digest
from .errors import (
    CHUNK_COUNT_EXHAUSTED,
    COST_CAP_EXCEEDED,
    EXECUTABLE_PLAN_INVALID,
    MODEL_CONTEXT_EXCEEDED,
    MODEL_CONTEXT_EXCEEDED_UNSPLITTABLE,
    STALE_REVIEW_PLAN,
    TOKEN_COUNT_DRIFT,
    BlockingError,
)

MICRO_PER_MILLION = 1_000_000


def _rebuild_and_compare(skeleton: dict, *, cwd) -> dict:
    """MC2-F11: recompute the plan from the recorded commits and refuse any
    drift BEFORE the first network call."""
    state = skeleton["repository_state"]
    for sha in (state["target_base_sha"], state["diff_base_sha"],
                state["head_sha"]):
        repostate.resolve_commit(sha, cwd=cwd)
    rebuilt = planmod.build_skeleton(state["target_base_sha"],
                                     state["head_sha"], cwd=cwd)
    if canonical_json(rebuilt) != canonical_json(skeleton):
        raise BlockingError(
            STALE_REVIEW_PLAN,
            "category=skeleton_rebuild_mismatch — the supplied plan does not "
            "match a rebuild from its own commits under the current execution "
            "policy; a fresh skeleton is required before any provider call")
    return rebuilt


def atom_texts(skeleton: dict, *, cwd) -> dict[str, str]:
    """Re-derive each ATOM's exact changed content from the commits.

    Content is kept keyed by atom, never as one joined blob per unit: a unit
    split must be able to hand each child exactly its own atoms' content. A
    joined blob can only be re-split by guessing (e.g. by line index), which
    silently misattributes content whenever an atom spans more than one line
    — and every hash downstream would still agree."""
    state = skeleton["repository_state"]
    mb, head = state["diff_base_sha"], state["head_sha"]
    raw = rawchange.raw_changes(mb, head, cwd=cwd)
    repo_id = skeleton["identities"]["repository_change_sha256"]
    contents: dict[str, bytes] = {}
    # A file may legitimately yield no reviewable text (binary, submodule
    # pointer, unrenderable mode change). Skipping is NOT silent: any atom the
    # skeleton claims but that no file produced is caught below as
    # STALE_REVIEW_PLAN, so a swallowed error can never shrink coverage.
    unrenderable = 0
    for change in raw:
        entry = change.as_changed_file()
        try:
            body = gitdiff.file_diff(mb, entry, head=head, cwd=cwd,
                                     attr_source=mb)
        except gitdiff.DiffError:
            unrenderable += 1
            continue
        if not body.strip():
            continue
        try:
            result = atoms.atomize_file_change(
                body, path=change.path, original_path=change.orig_path,
                git_status=entry.status, repository_change_sha256=repo_id)
        except atoms.AtomError:
            unrenderable += 1
            continue
        contents.update(result.contents)
    decoded = {a: b.decode("utf-8", "surrogateescape")
               for a, b in contents.items()}
    for unit in skeleton["units"]:
        missing = [a for a in unit["atom_ids"] if a not in decoded]
        if missing:
            raise BlockingError(
                STALE_REVIEW_PLAN,
                f"category=unit_content_unavailable unit="
                f"{unit['unit_sha256'][:16]} missing_atoms={len(missing)} "
                f"unrenderable_files={unrenderable}")
    return decoded


def text_for(unit: dict, atom_map: dict[str, str]) -> str:
    """A unit's request text is exactly its own atoms, in its own order."""
    return "\n".join(atom_map[a] for a in unit["atom_ids"])


def unit_texts(skeleton: dict, *, cwd) -> dict[str, str]:
    """unit_sha256 -> request text, built from the per-atom content map."""
    atom_map = atom_texts(skeleton, cwd=cwd)
    return {u["unit_sha256"]: text_for(u, atom_map)
            for u in skeleton["units"]}


def _derive_subunit(parent: dict, atom_ids: list[str]) -> dict:
    """A child unit after an atom-boundary split. Identity is recomputed."""
    index = {a: i for i, a in enumerate(parent["atom_ids"])}
    ordinals = [parent["atom_ordinals"][index[a]] for a in atom_ids]
    child = dict(parent)
    child["atom_ids"] = list(atom_ids)
    child["atom_ordinals"] = ordinals
    child["min_patch_ordinal"] = min(ordinals)
    child["max_patch_ordinal"] = max(ordinals)
    child["split_strategies"] = [*parent["split_strategies"], "token_bisect"]
    child["split_depth"] = parent["split_depth"] + 1
    child.pop("unit_sha256", None)
    child["unit_sha256"] = coverage.unit_hash(child)
    return child


def _request_for(model_id: str, unit_records: list[dict], texts: list[str],
                 pin_values: dict):
    effort = pin_values["VERIFIER_REASONING_EFFORT_BY_MODEL"][model_id]
    return providerreq.build_request(
        model_id, unit_records, texts, reasoning_effort=effort,
        max_output_tokens=pin_values["VERIFIER_MAX_OUTPUT_TOKENS"])


def _fits(model_id: str, input_tokens: int, pin_values: dict) -> tuple[bool, int]:
    cap = capabilities.capability(model_id)
    needed = (input_tokens + pin_values["VERIFIER_MAX_OUTPUT_TOKENS"]
              + pin_values["VERIFIER_CONTEXT_MARGIN_TOKENS"])
    return needed <= cap.context_window_tokens, (
        cap.context_window_tokens - needed)


class _Counter:
    """Counts through the transport while enforcing the call cap."""

    def __init__(self, transport, pin_values):
        self.transport = transport
        self.pins = pin_values
        self.calls = 0
        self.records: list[dict] = []

    def count(self, request, *, label: str) -> counting.CountResult:
        cap = self.pins["VERIFIER_MAX_COUNT_CALLS"]
        if self.calls >= cap:
            raise BlockingError(
                CHUNK_COUNT_EXHAUSTED,
                f"category=count_call_cap_exceeded cap={cap} label={label}")
        self.calls += 1
        result = counting.count_input_tokens(
            request, transport=self.transport,
            max_retries=self.pins["VERIFIER_COUNT_MAX_RETRIES"])
        self.records.append({
            "label": label,
            "model_id": request.model_id,
            "input_tokens": result.input_tokens,
            "source": result.source,
            "attempts": result.attempts,
            **request.hashes(),
        })
        return result


def _fit_unit(unit: dict, atom_map: dict[str, str], model_ids: list[str],
              pin_values: dict, counter: _Counter,
              preflight_entries: list[dict], texts_by_unit: dict[str, str],
              allowlist: frozenset) -> list[tuple[dict, dict]]:
    """Return [(unit, counts_by_model)] after recursive exact-token fitting.

    Counts are per MODEL and never reused across models or estimated for a
    child from a parent (MC3 §36). Each child's text is rebuilt from ITS OWN
    atoms, so a split can never hand a unit content it does not own."""
    text = text_for(unit, atom_map)
    counts: dict[str, int] = {}
    oversize: list[str] = []
    for model_id in model_ids:
        request = _request_for(model_id, [unit], [text], pin_values)
        preflight_entries.append(preflight.preflight_request(
            request.transmitted_text(),
            label=f"unit:{unit['unit_sha256'][:16]}:{model_id}",
            allowlist=allowlist))
        result = counter.count(request, label=f"unit:{unit['unit_sha256'][:16]}")
        counts[model_id] = result.input_tokens
        if not _fits(model_id, result.input_tokens, pin_values)[0]:
            oversize.append(model_id)
    if not oversize:
        texts_by_unit[unit["unit_sha256"]] = text
        return [(unit, counts)]

    atom_ids = unit["atom_ids"]
    if len(atom_ids) == 1:
        raise BlockingError(
            MODEL_CONTEXT_EXCEEDED_UNSPLITTABLE,
            f"category=single_atom_exceeds_context models={sorted(oversize)} "
            f"unit={unit['unit_sha256'][:16]} — splitting cannot help")
    mid = len(atom_ids) // 2
    out: list[tuple[dict, dict]] = []
    for child_ids in (atom_ids[:mid], atom_ids[mid:]):
        child = _derive_subunit(unit, child_ids)
        out.extend(_fit_unit(child, atom_map, model_ids, pin_values,
                             counter, preflight_entries, texts_by_unit,
                             allowlist))
    return out


def _pack_batches(fitted: list[tuple[dict, dict]], texts_by_unit: dict,
                  model_ids: list[str], pin_values: dict, counter: _Counter,
                  preflight_entries: list[dict],
                  allowlist: frozenset) -> list[dict]:
    """Greedy, order-preserving packing within one review class."""
    batches: list[dict] = []
    current: list[dict] = []
    # Each unit's own exact count, measured alone. A batch body strictly
    # CONTAINS every member unit's section, so a batch may never count fewer
    # tokens than its largest member — beyond the operator's tolerance. This
    # is what makes VERIFIER_TOKEN_DRIFT_TOLERANCE an enforced bound rather
    # than a recorded intention.
    solo_counts = {u["unit_sha256"]: c for u, c in fitted}
    tolerance = pin_values["VERIFIER_TOKEN_DRIFT_TOLERANCE"]

    def check_drift(units: list[dict], model_id: str, measured: int,
                    label: str):
        floor = max(solo_counts[u["unit_sha256"]][model_id] for u in units)
        if measured + tolerance < floor:
            raise BlockingError(
                TOKEN_COUNT_DRIFT,
                f"category=batch_count_below_member_floor model={model_id} "
                f"label={label} measured={measured} member_floor={floor} "
                f"tolerance={tolerance} — a batch cannot cost less than the "
                "largest unit it contains")

    def close(units: list[dict]):
        if not units:
            return
        counts, headroom, hashes = {}, {}, {}
        for model_id in model_ids:
            request = _request_for(
                model_id, units, [texts_by_unit[u["unit_sha256"]]
                                  for u in units], pin_values)
            preflight_entries.append(preflight.preflight_request(
                request.transmitted_text(),
                label=f"batch:{len(batches)}:{model_id}",
                allowlist=allowlist))
            result = counter.count(request, label=f"batch:{len(batches)}")
            check_drift(units, model_id, result.input_tokens,
                        f"batch:{len(batches)}")
            fits, room = _fits(model_id, result.input_tokens, pin_values)
            if not fits:
                raise BlockingError(
                    MODEL_CONTEXT_EXCEEDED,
                    f"category=batch_exceeds_context model={model_id}")
            counts[model_id] = result.input_tokens
            headroom[model_id] = room
            hashes[model_id] = request.hashes()
        batches.append(batching.batch_record(
            f"batch-{len(batches):04d}", units, counts, headroom, hashes))

    for unit, _counts in fitted:
        if current and (batching.review_class(current[0])
                        != batching.review_class(unit)):
            close(current)
            current = []
        candidate = [*current, unit]
        ok = True
        for model_id in model_ids:
            request = _request_for(
                model_id, candidate,
                [texts_by_unit[u["unit_sha256"]] for u in candidate],
                pin_values)
            preflight_entries.append(preflight.preflight_request(
                request.transmitted_text(),
                label=f"probe:{len(batches)}:{model_id}",
                allowlist=allowlist))
            result = counter.count(request, label=f"probe:{len(batches)}")
            if not _fits(model_id, result.input_tokens, pin_values)[0]:
                ok = False
                break
        if ok:
            current = candidate
        else:
            close(current)
            current = [unit]
    close(current)
    return batches


def _cost_plan(batches: list[dict], model_ids: list[str],
               pin_values: dict) -> dict:
    """Integer micro-USD cost exposure. No float ever enters this ledger."""
    per_model = {}
    total_input, total_output = 0, 0
    for model_id in model_ids:
        cap = capabilities.capability(model_id)
        input_micros = 0
        output_micros = 0
        for batch in batches:
            tokens = batch["input_tokens_by_model"][model_id]
            rate = cap.input_micro_usd_per_million
            out_rate = cap.output_micro_usd_per_million
            if (cap.long_context_threshold_input_tokens is not None
                    and tokens > cap.long_context_threshold_input_tokens):
                rate = rate * cap.above_threshold_input_multiplier_bp // 10_000
                out_rate = (out_rate
                            * cap.above_threshold_output_multiplier_bp // 10_000)
            input_micros += -(-tokens * rate // MICRO_PER_MILLION)
            output_micros += -(-pin_values["VERIFIER_MAX_OUTPUT_TOKENS"]
                               * out_rate // MICRO_PER_MILLION)
        retries = pin_values["VERIFIER_GENERATION_MAX_RETRIES"]
        per_model[model_id] = {
            "planned_input_micro_usd": input_micros,
            "worst_case_output_micro_usd": output_micros,
            "worst_case_total_micro_usd":
                (input_micros + output_micros) * (1 + retries),
            "long_context_surcharge_applies":
                cap.long_context_threshold_input_tokens is not None,
        }
        total_input += input_micros
        total_output += output_micros
    retries = pin_values["VERIFIER_GENERATION_MAX_RETRIES"]
    worst_case = (total_input + total_output) * (1 + retries)
    return {
        "per_model": per_model,
        "planned_generation_calls": len(batches) * len(model_ids),
        "worst_case_generation_calls":
            len(batches) * len(model_ids) * (1 + retries),
        "planned_input_micro_usd": total_input,
        "worst_case_output_micro_usd": total_output,
        "worst_case_total_micro_usd": worst_case,
        "count_call_billing_state": counting.COUNT_BILLING_STATE,
        "money_unit": "integer micro-USD",
    }


def finalize(skeleton: dict, *, cwd, operator_pins: dict, transport,
             secret_allowlist: frozenset = frozenset()) -> dict:
    """Produce a strict, private ExecutableReviewPlan. Zero generation."""
    artifact.validate_strict(skeleton)
    _rebuild_and_compare(skeleton, cwd=cwd)

    model_ids = list(skeleton["requested_model_ids"])
    capability_policy = capabilities.policy_record(model_ids)
    pin_record = pinsmod.pin_record(operator_pins, model_ids)
    pin_values = pin_record["pins"]

    if len(skeleton["units"]) > pin_values["VERIFIER_MAX_REVIEW_UNITS"]:
        raise BlockingError(
            CHUNK_COUNT_EXHAUSTED,
            f"category=review_unit_cap_exceeded units={len(skeleton['units'])} "
            f"cap={pin_values['VERIFIER_MAX_REVIEW_UNITS']}")

    atom_map = atom_texts(skeleton, cwd=cwd)
    counter = _Counter(transport, pin_values)
    preflight_entries: list[dict] = []
    texts_by_unit: dict[str, str] = {}

    fitted: list[tuple[dict, dict]] = []
    for unit in skeleton["units"]:
        fitted.extend(_fit_unit(unit, atom_map, model_ids, pin_values,
                                counter, preflight_entries, texts_by_unit,
                                secret_allowlist))
    final_units = [u for u, _ in fitted]

    # the disposition partition must still hold over the FINAL unit set
    coverage.prove_exact_dispositions(
        [a["atom_id"] for a in skeleton["atoms"]],
        skeleton["required_control_atom_ids"],
        [u["atom_ids"] for u in final_units],
        [list(r["covered_generated_atom_disposition"])
         for r in skeleton["generated_relationships"]],
        skeleton["atom_dispositions"]["blocked_unreviewable"])

    batches = _pack_batches(fitted, texts_by_unit, model_ids, pin_values,
                            counter, preflight_entries, secret_allowlist)
    batching.prove_batch_partition(final_units, batches)

    cost = _cost_plan(batches, model_ids, pin_values)
    if cost["worst_case_total_micro_usd"] > pin_values["VERIFIER_COST_CAP_MICRO_USD"]:
        raise BlockingError(
            COST_CAP_EXCEEDED,
            f"category=cost_cap_exceeded worst_case_micro_usd="
            f"{cost['worst_case_total_micro_usd']} "
            f"cap_micro_usd={pin_values['VERIFIER_COST_CAP_MICRO_USD']} — zero "
            "generation calls will be made")
    if cost["planned_generation_calls"] > pin_values[
            "VERIFIER_MAX_GENERATION_CALLS"]:
        raise BlockingError(
            CHUNK_COUNT_EXHAUSTED,
            "category=generation_call_cap_exceeded planned="
            f"{cost['planned_generation_calls']} "
            f"cap={pin_values['VERIFIER_MAX_GENERATION_CALLS']}")

    count_source = counter.records[0]["source"] if counter.records else None
    provider_counts = count_source == counting.SOURCE_PROVIDER
    pending: list[dict] = []
    if not provider_counts:
        pending.append({
            "code": "COUNTS_ARE_NOT_PROVIDER_EVIDENCE",
            "reason": "token counts came from a deterministic local transport "
                      f"({count_source}); a plan may not be executable on "
                      "mock counts",
            "path_bytes_b64": None,
        })

    state = skeleton["repository_state"]
    executable_plan = {
        "schema_version": skeleton["schema_version"],
        "artifact": "executable-review-plan",
        "stage": "finalized-plan",
        "executable_plan_sha256": None,
        "publication_class": "private",
        "review_skeleton_sha256": skeleton["review_skeleton_sha256"],
        "repository_state": state,
        "identities": skeleton["identities"],
        "disposition_root": skeleton["coverage"]["disposition_root"],
        "git_execution_policy_sha256": skeleton["git_execution_policy"][
            "policy_sha256"],
        "capability_policy": capability_policy,
        "operator_pin_record": pin_record,
        "model_resolution": counting.resolve_models(model_ids,
                                                    transport=transport),
        "final_units": final_units,
        "batches": batches,
        "count_evidence": counting.evidence_record(
            counter.records,
            counting.resolve_models(model_ids, transport=transport)),
        "secret_preflight": {
            **preflight.evidence_record(preflight_entries),
            "reviewed_allowlist_size": len(secret_allowlist),
        },
        "cost_plan": cost,
        "count_calls_performed": counter.calls,
        "generation_calls_performed": 0,
        "pending_requirements": pending,
        "executable": provider_counts and not pending,
    }
    executable_plan["executable_plan_sha256"] = plan_digest(executable_plan)
    return executable_plan


def plan_digest(record: dict) -> str:
    stripped = {k: v for k, v in record.items()
                if k != "executable_plan_sha256"}
    return digest(b"executable-review-plan-v1", canonical_json(stripped))


_PLAN_KEYS = (
    "schema_version", "artifact", "stage", "executable_plan_sha256",
    "publication_class", "review_skeleton_sha256", "repository_state",
    "identities", "disposition_root", "git_execution_policy_sha256",
    "capability_policy", "operator_pin_record", "model_resolution",
    "final_units", "batches", "count_evidence", "secret_preflight",
    "cost_plan", "count_calls_performed", "generation_calls_performed",
    "pending_requirements", "executable",
)


def _plan_fail(reason: str):
    raise BlockingError(EXECUTABLE_PLAN_INVALID, reason)


def validate_plan_strict(record: dict) -> dict:
    """Load a finalized plan by RECOMPUTING every derived claim.

    A digest alone proves only that a file is internally consistent — and an
    attacker who edits a claim can recompute the digest. So nothing derived is
    read from the file and believed: the cost ledger is recomputed from the
    batches, the batch partition is re-proved against the units, and
    `executable` is re-derived from the count source. A file may state only
    what its own inputs support."""
    try:
        if not isinstance(record, dict):
            _plan_fail("category=plan_not_object")
        missing = [k for k in _PLAN_KEYS if k not in record]
        if missing:
            _plan_fail(f"category=plan_field_missing fields={missing}")
        extra = sorted(set(record) - set(_PLAN_KEYS))
        if extra:
            _plan_fail(f"category=plan_field_unknown fields={extra}")
        if record["artifact"] != "executable-review-plan":
            _plan_fail("category=plan_artifact_tag_wrong")
        if record["publication_class"] != "private":
            _plan_fail("category=plan_publication_class_wrong")

        if plan_digest(record) != record["executable_plan_sha256"]:
            _plan_fail("category=plan_digest_mismatch")

        pin_record = record["operator_pin_record"]
        if pinsmod.pin_digest(pin_record) != pin_record["pin_record_sha256"]:
            _plan_fail("category=pin_record_digest_mismatch")
        policy = record["capability_policy"]
        if capabilities.policy_digest(policy) != policy[
                "capability_policy_sha256"]:
            _plan_fail("category=capability_policy_digest_mismatch")

        model_ids = list(record["model_resolution"]["requested_model_ids"])
        pin_values = pinsmod.validate_pins(pin_record["pins"], model_ids)

        # the batches must still partition the final units, in order
        batching.prove_batch_partition(record["final_units"],
                                       record["batches"])
        for batch in record["batches"]:
            if batching.batch_digest(batch) != batch["batch_sha256"]:
                _plan_fail(f"category=batch_digest_mismatch "
                           f"batch={batch['batch_id']}")

        recomputed_cost = _cost_plan(record["batches"], model_ids, pin_values)
        if canonical_json(recomputed_cost) != canonical_json(
                record["cost_plan"]):
            _plan_fail("category=cost_plan_not_reproducible")
        if recomputed_cost["worst_case_total_micro_usd"] > pin_values[
                "VERIFIER_COST_CAP_MICRO_USD"]:
            _plan_fail("category=cost_cap_exceeded_in_loaded_plan")

        if record["generation_calls_performed"] != 0:
            _plan_fail("category=generation_calls_recorded")

        count_source = record["count_evidence"]["model_resolution"][
            "count_source"]
        if count_source != record["model_resolution"]["count_source"]:
            _plan_fail("category=count_source_disagreement")
        expected = (count_source == counting.SOURCE_PROVIDER
                    and not record["pending_requirements"])
        if record["executable"] is not expected:
            _plan_fail(
                f"category=executable_claim_not_derived count_source="
                f"{count_source} pending={len(record['pending_requirements'])} "
                f"claimed={record['executable']} derived={expected}")
        if count_source != counting.SOURCE_PROVIDER and record["executable"]:
            _plan_fail("category=executable_on_non_provider_counts")
    except BlockingError:
        raise
    except Exception as exc:                    # any failure is a refusal
        raise BlockingError(
            EXECUTABLE_PLAN_INVALID,
            f"category=plan_validation_failed "
            f"exception_class={type(exc).__name__}") from exc
    return record


def main(argv: list[str]) -> int:
    """`--finalize`: skeleton in, finalized plan out. Zero generation calls.

    Without an operator-authorized provider transport this runs on the
    LABELLED local mock, and the resulting plan is necessarily
    non-executable. That is the honest outcome, not a degraded one."""
    import argparse

    parser = argparse.ArgumentParser(prog="independent_verify.py --finalize")
    parser.add_argument("--finalize", action="store_true", required=True)
    parser.add_argument("--skeleton", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--public-output")
    parser.add_argument("--allowlist",
                        help="newline-delimited operator-REVIEWED literals "
                             "that preflight may clear")
    args = parser.parse_args(argv)

    with open(args.skeleton, "rb") as handle:
        skeleton = artifact.parse_strict(handle.read())
    allowlist: frozenset = frozenset()
    if args.allowlist:
        with open(args.allowlist, encoding="utf-8") as handle:
            allowlist = frozenset(line.rstrip("\n") for line in handle
                                  if line.strip())
    try:
        plan_record = finalize(skeleton, cwd=None,
                               operator_pins=_pins_from_environment(),
                               transport=counting.MockCountTransport(),
                               secret_allowlist=allowlist)
    except BlockingError as exc:
        print(f"FINALIZE BLOCKED: {exc}", file=sys.stderr)
        return 2
    validate_plan_strict(plan_record)
    artifact.write_atomic(plan_record, args.output)
    if args.public_output:
        artifact.write_atomic(public_plan_summary(plan_record),
                              args.public_output)
    print(f"units={len(plan_record['final_units'])} "
          f"batches={len(plan_record['batches'])} "
          f"count_calls={plan_record['count_calls_performed']} "
          f"generation_calls={plan_record['generation_calls_performed']} "
          f"count_source={plan_record['model_resolution']['count_source']} "
          f"executable={plan_record['executable']}")
    for item in plan_record["pending_requirements"]:
        print(f"PENDING {item['code']}: {item['reason']}")
    print(f"wrote {args.output}")
    return 0


def _pins_from_environment() -> dict:
    """Read the twelve PINs from the environment. There are NO defaults: an
    absent PIN is UNSET_POLICY_PIN, which blocks before any call."""
    values: dict = {}
    for name in policy.POLICY_PIN_NAMES:
        raw = os.environ.get(name)
        if raw is None:
            continue
        if name == "VERIFIER_REASONING_EFFORT_BY_MODEL":
            values[name] = {k: (None if v == "" else v) for k, v in
                            (pair.split("=", 1)
                             for pair in raw.split(",") if pair)}
            continue
        try:
            values[name] = int(raw)
        except ValueError:
            values[name] = raw
    return values


def public_plan_summary(executable_plan: dict) -> dict:
    """Publishable finalized-plan view: no unit paths, no request bodies."""
    return {
        "artifact": "public-finalized-plan-summary",
        "publication_class": "public",
        "head_sha": executable_plan["repository_state"]["head_sha"],
        "diff_base_sha": executable_plan["repository_state"]["diff_base_sha"],
        "review_skeleton_sha256": executable_plan["review_skeleton_sha256"],
        "executable_plan_sha256": executable_plan["executable_plan_sha256"],
        "disposition_root": executable_plan["disposition_root"],
        "final_unit_count": len(executable_plan["final_units"]),
        "batch_count": len(executable_plan["batches"]),
        "count_calls_performed": executable_plan["count_calls_performed"],
        "generation_calls_performed": 0,
        "cost_plan": executable_plan["cost_plan"],
        "count_source": executable_plan["count_evidence"]["model_resolution"][
            "count_source"],
        "executable": executable_plan["executable"],
        "pending_codes": [p["code"]
                          for p in executable_plan["pending_requirements"]],
    }
