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
    counting2,
    coverage,
    evidence,
    gitdiff,
    policy,
    preflight,
    providerreq,
    rawchange,
    repostate,
    reviewpolicy,
    unitpayload,
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
    SECRET_PREFLIGHT_FAILED,
    STALE_REVIEW_PLAN,
    STRUCTURAL_PLAN_BLOCKED,
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


def derive_unit_record(parent: dict, atom_ids: list[str],
                       atom_records: dict[str, dict],
                       atom_map: dict[str, str]) -> dict:
    """Build a child unit from EXACT child atoms, recomputing every field.

    MC3 copied the parent dict and replaced only the atom list, ordinals and
    hash. Everything else — changed_content_bytes, the old/new line ranges,
    the metadata-atom count, the context facts — stayed at the PARENT's
    values, and the child's digest then certified them. A split unit
    therefore carried a byte count for content it did not contain.

    Nothing is inherited here that can be derived. The parent supplies only
    structural bindings that are genuinely shared: path, status, modes,
    classification, and the split lineage."""
    ordinal_of = dict(zip(parent["atom_ids"], parent["atom_ordinals"],
                          strict=True))
    ordinals = [ordinal_of[a] for a in atom_ids]
    entries = [atom_records[a] for a in atom_ids]
    contents = [atom_map[a] for a in atom_ids]

    old_lines = [e["line_number"] for e in entries
                 if e["side"] == unitpayload.SIDE_OLD]
    new_lines = [e["line_number"] for e in entries
                 if e["side"] == unitpayload.SIDE_NEW]

    child = {
        # shared structural bindings
        "path_bytes_b64": parent["path_bytes_b64"],
        "original_path_bytes_b64": parent.get("original_path_bytes_b64"),
        "git_status": parent["git_status"],
        "old_mode": parent.get("old_mode"),
        "new_mode": parent.get("new_mode"),
        "classification": parent["classification"],
        # recomputed from the child's own atoms
        "atom_ids": list(atom_ids),
        "atom_ordinals": ordinals,
        "min_patch_ordinal": min(ordinals),
        "max_patch_ordinal": max(ordinals),
        "changed_content_bytes": sum(
            len(c.encode("utf-8", "surrogateescape")) for c in contents),
        "meta_atom_count": sum(1 for e in entries
                               if e["side"] == unitpayload.SIDE_META),
        "old_line_range": [min(old_lines), max(old_lines)] if old_lines else None,
        "new_line_range": [min(new_lines), max(new_lines)] if new_lines else None,
        # split lineage
        "split_strategies": [*parent["split_strategies"], "token_bisect"],
        "split_depth": parent["split_depth"] + 1,
    }
    child["unit_sha256"] = coverage.unit_hash(child)
    return child


def _derive_subunit(parent: dict, atom_ids: list[str]) -> dict:
    """Deprecated shim retained only for the MC3 split regression test."""
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


def structural_preconditions(skeleton: dict) -> None:
    """A structurally blocked plan must never reach a provider (MC4 §7).

    MC3 derived `executable` from the count source and the finalizer's own
    pending list, and never asked whether Stage 1 had said the change could
    be reviewed at all. A skeleton with blocked, unreviewable atoms could
    therefore be counted, batched and costed — spending money to prepare a
    review that was already known to be incomplete."""
    reasons = []
    if not skeleton.get("structurally_clean"):
        reasons.append("structurally_clean=false")
    if skeleton.get("blocking_reasons"):
        reasons.append(f"blocking_reasons={len(skeleton['blocking_reasons'])}")
    blocked = skeleton["atom_dispositions"].get("blocked_unreviewable") or []
    if blocked:
        reasons.append(f"blocked_unreviewable_atoms={len(blocked)}")
    if skeleton["coverage"].get("blocked_control_atom_count"):
        reasons.append("blocked_control_atoms="
                       f"{skeleton['coverage']['blocked_control_atom_count']}")
    if reasons:
        raise BlockingError(
            STRUCTURAL_PLAN_BLOCKED,
            "category=structural_plan_blocked reasons=" + ",".join(reasons)
            + " — zero count calls and zero generation calls will be made")


def _payload_for(unit: dict, atom_records: dict, atom_map: dict) -> dict:
    return unitpayload.structured_unit(unit, atom_records, atom_map)


def _request_for(model_id: str, payloads: list[dict], pin_values: dict,
                 review_policy: dict, challenge: str):
    effort = pin_values["VERIFIER_REASONING_EFFORT_BY_MODEL"][model_id]
    return providerreq.build_request(
        model_id, payloads, lens=review_policy["lenses"][model_id],
        challenge=challenge, reasoning_effort=effort,
        max_output_tokens=pin_values["VERIFIER_MAX_OUTPUT_TOKENS"])


class PreflightManifest:
    """Every request assembled BEFORE any is transmitted (MC4 §14/§36).

    MC3 built one request, scanned it, counted it, and moved on. A secret in
    the last unit therefore blocked only AFTER every earlier unit's content
    had already been sent to the endpoint — and the run still reported "zero
    count calls on secret failure", which was true only of the request that
    happened to contain the secret.

    Both payloads are scanned, separately. The count body and the execution
    body are not byte-identical (the latter carries max_output_tokens), so
    scanning one and calling the other the exact bytes was an evidence error
    as well as a coverage gap."""

    def __init__(self, authorizations, *, atom_records: dict):
        self.authorizations = authorizations
        self.atom_records = atom_records
        self.entries: list[dict] = []
        self.sealed = False

    def add(self, request, *, label: str, units: list[dict]) -> None:
        if self.sealed:
            raise BlockingError(
                SECRET_PREFLIGHT_FAILED,
                f"category=request_added_after_seal label={label}")
        count_bytes = canonical_json(request.count_payload()).decode(
            "utf-8", "surrogateescape")
        exec_bytes = request.transmitted_text()
        unit_hashes = [u["unit_sha256"] for u in units]
        cleared = self._cleared_hashes_for(units)
        count_entry = preflight.preflight_request(
            count_bytes, label=f"{label}:count", cleared_hashes=cleared)
        exec_entry = preflight.preflight_request(
            exec_bytes, label=f"{label}:execution", cleared_hashes=cleared)
        self.entries.append({
            "label": label,
            "model_id": request.model_id,
            "unit_sha256_in_order": list(unit_hashes),
            "count_request_sha256": request.count_request_sha256(),
            "execution_request_sha256": request.execution_request_sha256(),
            "count_payload_scan": count_entry,
            "execution_payload_scan": exec_entry,
        })

    def _cleared_hashes_for(self, units: list[dict]) -> frozenset:
        """Scoped clearances only — never a global value exemption.

        Scope is resolved from the request's ATOMS, not its unit hash: a
        recursive split mints a new unit identity for the same atoms in the
        same file, and a clearance the operator granted for those bytes must
        survive that. Resolving by unit hash would silently drop every
        clearance the moment a unit was split."""
        if self.authorizations is None:
            return frozenset()
        atom_ids: set[str] = set()
        for unit in units:
            atom_ids.update(unit["atom_ids"])
        paths = {self.atom_records[a]["path_bytes_b64"]
                 for a in atom_ids if a in self.atom_records}
        cleared: set[str] = set()
        for record in self.authorizations.records:
            if record["path_bytes_b64"] not in paths:
                continue
            if record["atom_id"] is not None and record["atom_id"] not in atom_ids:
                continue
            cleared.add(record["literal_sha256"])
        return frozenset(cleared)

    def seal(self) -> dict:
        self.sealed = True
        record = {
            "schema_version": 1,
            "request_count": len(self.entries),
            "scanned_payload_count": 2 * len(self.entries),
            "entries": self.entries,
            "authorization_set_sha256": (
                self.authorizations.digest() if self.authorizations else None),
            "authorization_authority_class": (
                self.authorizations.authority_class
                if self.authorizations else None),
            "provider_attempt_count_at_seal": 0,
            "honest_scope": "a denylist plus an entropy heuristic over the "
                            "exact count and execution bytes; it cannot prove "
                            "the absence of secrets",
        }
        record["preflight_manifest_sha256"] = digest(
            b"preflight-manifest-v1", canonical_json(record))
        return record


def _fits(model_id: str, input_tokens: int, pin_values: dict) -> tuple[bool, int]:
    cap = capabilities.capability(model_id)
    needed = (input_tokens + pin_values["VERIFIER_MAX_OUTPUT_TOKENS"]
              + pin_values["VERIFIER_CONTEXT_MARGIN_TOKENS"])
    return needed <= cap.context_window_tokens, (
        cap.context_window_tokens - needed)


def _fit_unit(unit: dict, atom_records: dict, atom_map: dict,
              model_ids: list[str], pin_values: dict, ledger,
              manifest: PreflightManifest, payloads_by_unit: dict,
              review_policy: dict, challenge: str) -> list[tuple[dict, dict]]:
    """Recursive exact-token fitting. Children are rebuilt, never inherited."""
    payload = _payload_for(unit, atom_records, atom_map)
    counts: dict[str, int] = {}
    oversize: list[str] = []
    requests = []
    for model_id in model_ids:
        request = _request_for(model_id, [payload], pin_values, review_policy,
                               challenge)
        manifest.add(request, label=f"unit:{unit['unit_sha256'][:16]}",
                     units=[unit])
        requests.append((model_id, request))
    for model_id, request in requests:
        result = ledger.count(request,
                              label=f"unit:{unit['unit_sha256'][:16]}")
        counts[model_id] = result.input_tokens
        if not _fits(model_id, result.input_tokens, pin_values)[0]:
            oversize.append(model_id)
    if not oversize:
        payloads_by_unit[unit["unit_sha256"]] = payload
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
        child = derive_unit_record(unit, child_ids, atom_records, atom_map)
        out.extend(_fit_unit(child, atom_records, atom_map, model_ids,
                             pin_values, ledger, manifest, payloads_by_unit,
                             review_policy, challenge))
    return out


def _pack_batches(fitted: list[tuple[dict, dict]], payloads_by_unit: dict,
                  model_ids: list[str], pin_values: dict, ledger,
                  manifest: PreflightManifest, review_policy: dict,
                  challenge: str) -> list[dict]:
    """Greedy order-preserving packing, bounded by BOTH input and output.

    MC3 bounded input only. A batch whose verdicts cannot fit the output
    budget was still accepted — the PR #23 plan packed 152 units into a batch
    whose 8,000-token allowance covers 14."""
    batches: list[dict] = []
    current: list[dict] = []
    solo = {u["unit_sha256"]: c for u, c in fitted}
    unit_cap = review_policy["max_units_per_batch"]

    def close(units: list[dict]):
        if not units:
            return
        reviewpolicy.assert_output_capacity(
            len(units), pin_values["VERIFIER_MAX_OUTPUT_TOKENS"],
            where=f"batch-{len(batches):04d}")
        payloads = [payloads_by_unit[u["unit_sha256"]] for u in units]
        counts, headroom, hashes = {}, {}, {}
        requests = []
        for model_id in model_ids:
            request = _request_for(model_id, payloads, pin_values,
                                   review_policy, challenge)
            manifest.add(request, label=f"batch:{len(batches)}",
                         units=units)
            requests.append((model_id, request))
        for model_id, request in requests:
            result = ledger.count(request, label=f"batch:{len(batches)}")
            floor = max(solo[u["unit_sha256"]][model_id] for u in units)
            counting2.assert_batch_not_below_member_floor(
                measured=result.input_tokens, floor=floor,
                model_id=model_id, label=f"batch-{len(batches):04d}")
            fits, room = _fits(model_id, result.input_tokens, pin_values)
            if not fits:
                raise BlockingError(
                    MODEL_CONTEXT_EXCEEDED,
                    f"category=batch_exceeds_context model={model_id}")
            counts[model_id] = result.input_tokens
            headroom[model_id] = room
            hashes[model_id] = request.hashes()
        record = batching.batch_record(f"batch-{len(batches):04d}", units,
                                       counts, headroom, hashes)
        record["worst_case_output_tokens"] = (
            reviewpolicy.worst_case_output_tokens(len(units)))
        record["batch_sha256"] = batching.batch_digest(record)
        batches.append(record)

    for unit, _counts in fitted:
        boundary = current and (batching.review_class(current[0])
                                != batching.review_class(unit))
        if boundary or len(current) >= unit_cap:
            close(current)
            current = []
        candidate = [*current, unit]
        ok = len(candidate) <= unit_cap
        if ok:
            for model_id in model_ids:
                request = _request_for(
                    model_id, [payloads_by_unit[u["unit_sha256"]]
                               for u in candidate],
                    pin_values, review_policy, challenge)
                manifest.add(request, label=f"probe:{len(batches)}",
                             units=candidate)
                result = ledger.count(request, label=f"probe:{len(batches)}")
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
             authorizations=None, challenge: str = "LOCAL-MOCK-CHALLENGE",
             required_approver: str | None = None,
             minimum_distinct_corroborators: int = 2) -> dict:
    """Produce a strict, PRIVATE mock-finalization report. Zero generation.

    Named for what it does locally. Nothing here can produce provider
    evidence, so nothing here can produce an executable plan."""
    artifact.validate_strict(skeleton)
    _rebuild_and_compare(skeleton, cwd=cwd)
    structural_preconditions(skeleton)

    model_ids = list(skeleton["requested_model_ids"])
    capability_policy = capabilities.policy_record(model_ids)
    pin_record = pinsmod.test_pin_record(operator_pins, model_ids)
    pin_values = pin_record["pins"]
    review_policy = reviewpolicy.policy_record(
        model_ids,
        required_approver=required_approver or model_ids[0],
        minimum_distinct_corroborators=minimum_distinct_corroborators,
        max_output_tokens=pin_values["VERIFIER_MAX_OUTPUT_TOKENS"])

    if len(skeleton["units"]) > pin_values["VERIFIER_MAX_REVIEW_UNITS"]:
        raise BlockingError(
            CHUNK_COUNT_EXHAUSTED,
            f"category=review_unit_cap_exceeded units={len(skeleton['units'])} "
            f"cap={pin_values['VERIFIER_MAX_REVIEW_UNITS']}")

    atom_map = atom_texts(skeleton, cwd=cwd)
    atom_records = unitpayload.index_atom_records(skeleton)
    ledger = counting2.CountLedger(transport, pin_values)
    manifest = PreflightManifest(authorizations, atom_records=atom_records)
    payloads_by_unit: dict[str, dict] = {}

    fitted: list[tuple[dict, dict]] = []
    for unit in skeleton["units"]:
        fitted.extend(_fit_unit(unit, atom_records, atom_map, model_ids,
                                pin_values, ledger, manifest,
                                payloads_by_unit, review_policy, challenge))
    final_units = [u for u, _ in fitted]

    coverage.prove_exact_dispositions(
        [a["atom_id"] for a in skeleton["atoms"]],
        skeleton["required_control_atom_ids"],
        [u["atom_ids"] for u in final_units],
        [list(r["covered_generated_atom_disposition"])
         for r in skeleton["generated_relationships"]],
        skeleton["atom_dispositions"]["blocked_unreviewable"])

    batches = _pack_batches(fitted, payloads_by_unit, model_ids, pin_values,
                            ledger, manifest, review_policy, challenge)
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

    source = counting.transport_source(transport)
    count_evidence = evidence.candidate_evidence_record(
        evidence.MOCK_TEST_EVIDENCE if source == counting.SOURCE_MOCK
        else evidence.UNTRUSTED_LOCAL_EVIDENCE,
        counts=[{**r, "count_request_sha256": r["count_request_sha256"],
                 "request_semantics_sha256": r["request_semantics_sha256"]}
                for r in ledger.records],
        logical_request_count=ledger.logical_requests,
        provider_attempt_count=ledger.provider_attempts,
        endpoint=counting.COUNT_PATH,
        billing_state=counting.COUNT_BILLING_STATE)

    pending = [{
        "code": "COUNTS_ARE_NOT_TRUSTED_EVIDENCE",
        "reason": f"counts came from a {count_evidence['evidence_class']} "
                  "transport inside the reviewed branch; only the trusted "
                  "lane can produce evidence that makes a plan executable",
        "path_bytes_b64": None,
    }]

    state = skeleton["repository_state"]
    executable_plan = {
        "schema_version": skeleton["schema_version"],
        "artifact": "mock-finalization-report",
        "stage": "mock-finalization",
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
        "review_request_policy": review_policy,
        "requested_model_acceptance_policy":
            counting.requested_model_acceptance_policy(model_ids,
                                                       transport=transport),
        "final_units": final_units,
        "batches": batches,
        "count_evidence": count_evidence,
        "count_ledger": ledger.record(),
        "preflight_manifest": manifest.seal(),
        "cost_plan": cost,
        "logical_count_requests": ledger.logical_requests,
        "provider_attempts_performed": ledger.provider_attempts,
        "count_cache_hits": ledger.cache_hits,
        "generation_calls_performed": 0,
        "pending_requirements": pending,
        "executable": evidence.is_executable_authority(count_evidence),
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
    "capability_policy", "operator_pin_record",
    "final_units", "batches", "count_evidence",
    "cost_plan", "count_ledger", "preflight_manifest",
    "logical_count_requests", "provider_attempts_performed",
    "count_cache_hits", "review_request_policy",
    "requested_model_acceptance_policy", "generation_calls_performed",
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
        # MC4-F37: the tag names the EVIDENCE class. A plan built on local
        # mock counts is a mock-finalization-report; the
        # trusted-executable-review-plan tag is reserved for trusted evidence
        # and is refused here, because this loader cannot verify an anchor
        # that only the trusted lane can produce.
        if record["artifact"] not in ("mock-finalization-report",
                                      "trusted-executable-review-plan"):
            _plan_fail(f"category=plan_artifact_tag_wrong "
                       f"tag={record['artifact']}")
        trusted_tag = record["artifact"] == "trusted-executable-review-plan"
        if record["publication_class"] != "private":
            _plan_fail("category=plan_publication_class_wrong")

        if plan_digest(record) != record["executable_plan_sha256"]:
            _plan_fail("category=plan_digest_mismatch")

        pin_record = record["operator_pin_record"]
        model_ids = list(record["requested_model_acceptance_policy"][
            "requested_model_ids"])
        pinsmod.validate_pin_authority(pin_record, model_ids)
        reviewpolicy.validate_policy(record["review_request_policy"],
                                     model_ids)
        policy = record["capability_policy"]
        if capabilities.policy_digest(policy) != policy[
                "capability_policy_sha256"]:
            _plan_fail("category=capability_policy_digest_mismatch")

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

        # MC4-F06/F07: executability is RE-DERIVED from the evidence class
        # and its external anchor, never read from the record. Flipping the
        # class and recomputing the digest does not survive this, because a
        # trusted class must also carry a complete anchor.
        evidence.validate_count_evidence(record["count_evidence"])
        derived = evidence.is_executable_authority(record["count_evidence"])
        if trusted_tag != derived:
            _plan_fail("category=plan_tag_does_not_match_evidence_class "
                       f"tag={record['artifact']} derived_trusted={derived}")
        if record["executable"] is not derived:
            _plan_fail(
                "category=executable_claim_not_derived evidence_class="
                f"{record['count_evidence']['evidence_class']} "
                f"claimed={record['executable']} derived={derived}")
        if derived and record["pending_requirements"]:
            _plan_fail("category=executable_with_pending_requirements")
        # attempt accounting must reconcile with the ledger
        ledger = record["count_ledger"]
        if ledger["provider_attempt_count"] != record[
                "provider_attempts_performed"]:
            _plan_fail("category=attempt_count_disagreement")
        if ledger["provider_attempt_count"] > ledger["max_attempts_cap"]:
            _plan_fail("category=attempt_cap_exceeded_in_loaded_plan")
        # every batch must have been output-capacity checked
        for batch in record["batches"]:
            reviewpolicy.assert_output_capacity(
                batch["unit_count"], pin_values["VERIFIER_MAX_OUTPUT_TOKENS"],
                where=batch["batch_id"])
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
        # Lines beginning "# " are comments, so the file can explain WHY each
        # literal was cleared. A literal that itself begins "# " therefore
        # cannot be allowlisted — a deliberate, stated limit, taken because an
        # undocumented list of cleared credentials is not reviewable.
        with open(args.allowlist, encoding="utf-8") as handle:
            allowlist = frozenset(
                line.rstrip("\n") for line in handle
                if line.strip() and not line.startswith("# "))
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
          f"logical_requests={plan_record['logical_count_requests']} "
          f"provider_attempts={plan_record['provider_attempts_performed']} "
          f"cache_hits={plan_record['count_cache_hits']} "
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
        "logical_count_requests": executable_plan["logical_count_requests"],
        "provider_attempts_performed":
            executable_plan["provider_attempts_performed"],
        "generation_calls_performed": 0,
        "cost_plan": executable_plan["cost_plan"],
        "count_evidence_class":
            executable_plan["count_evidence"]["evidence_class"],
        "executable": executable_plan["executable"],
        "pending_codes": [p["code"]
                          for p in executable_plan["pending_requirements"]],
    }
