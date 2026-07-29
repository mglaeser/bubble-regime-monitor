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
    origin,
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
from .canon import canonical_json, digest, sha256_hex
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
                       atom_records: dict, atom_map: dict) -> dict:
    """Recursive splitting goes through the Stage-1 constructor (A2-F06)."""
    from . import units
    return units.child_unit_record(
        parent, atom_ids, atom_records, atom_map,
        budget=policy.STRUCTURAL_UNIT_CHANGED_BYTES_HEURISTIC)


def structural_preconditions(skeleton: dict) -> None:
    """A structurally blocked plan must never reach a transport (MC4 §7).

    MC3 derived `executable` from the count source and the finalizer's own
    pending list, and never asked whether Stage 1 had said the change could
    be reviewed at all. A skeleton with blocked, unreviewable atoms could
    therefore be counted, batched and costed — spending calls to prepare a
    review already known to be incomplete."""
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
                 review_policy: dict, challenge: str,
                 path_bytes_b64_by_unit: dict | None = None):
    """One assembled request: semantics, exact bytes, and both origin maps.

    Everything downstream — preflight, counting, execution — consumes the
    assembly, so the scanned document and the sent document are the same
    object rather than two builds that agree by convention (C4-F03)."""
    effort = pin_values["VERIFIER_REASONING_EFFORT_BY_MODEL"][model_id]
    return providerreq.assemble_request(
        model_id, payloads, lens=review_policy["lenses"][model_id],
        challenge=challenge, reasoning_effort=effort,
        max_output_tokens=pin_values["VERIFIER_MAX_OUTPUT_TOKENS"],
        path_bytes_b64_by_unit=path_bytes_b64_by_unit)


def _fits(model_id: str, input_tokens: int, pin_values: dict) -> tuple[bool, int]:
    cap = capabilities.capability(model_id)
    needed = (input_tokens + pin_values["VERIFIER_MAX_OUTPUT_TOKENS"]
              + pin_values["VERIFIER_CONTEXT_MARGIN_TOKENS"])
    return needed <= cap.context_window_tokens, (
        cap.context_window_tokens - needed)


class RequestGeneration:
    """Every request that must be assembled before ANY of them is sent.

    MC4's first attempt built, scanned and counted one request at a time. A
    secret in the last unit therefore blocked only after every earlier unit
    had already been transmitted, and the "zero calls" claim was true only of
    the request that happened to contain the secret. A generation makes the
    property structural: `count()` is unreachable until `seal()` has scanned
    the whole set."""

    def __init__(self, name: str):
        self.name = name
        self._requests: list[tuple[str, object, tuple]] = []
        self._sealed_snapshot: tuple | None = None
        self._generation_sha256: str | None = None

    @property
    def sealed(self) -> bool:
        return self._sealed_snapshot is not None

    def add(self, request, *, label: str, units: list[dict],
            registry: dict | None = None) -> None:
        if self.sealed:
            raise BlockingError(
                SECRET_PREFLIGHT_FAILED,
                f"category=request_added_after_seal generation={self.name} "
                f"label={label}")
        # units are frozen into a tuple of hashes: a later mutation of the
        # caller's list cannot change what was scanned. The full records are
        # registered separately so scope resolution can recover atom sets.
        if registry is not None:
            for unit in units:
                registry[unit["unit_sha256"]] = unit
        self._requests.append(
            (label, request, tuple(u["unit_sha256"] for u in units)))

    def entries(self):
        """The requests, whether sealed or not. Read-only view."""
        return tuple(self._requests)

    def seal(self) -> str:
        """Freeze to an immutable snapshot and bind its digest (A2-F03).

        MC4's first attempt left `requests` a public mutable list, so a
        caller could append after sealing and `count_generation` would count
        a request that was never scanned. The snapshot is a tuple, the
        digest is over the ordered (label, count-request-hash, unit-hashes),
        and counting re-verifies the digest before it runs."""
        if self.sealed:
            return self._generation_sha256
        self._sealed_snapshot = tuple(
            (label, request, unit_hashes)
            for label, request, unit_hashes in self._requests)
        binding = [
            {"label": label,
             **assembly.hashes(),
             "unit_sha256_in_order": list(unit_hashes)}
            for label, assembly, unit_hashes in self._sealed_snapshot
        ]
        self._generation_sha256 = digest(b"request-generation-v1",
                                         canonical_json(binding))
        return self._generation_sha256

    def sealed_snapshot(self) -> tuple:
        if not self.sealed:
            raise BlockingError(
                SECRET_PREFLIGHT_FAILED,
                f"category=snapshot_before_seal generation={self.name}")
        # Re-verify the digest: if anything were swapped between seal and use,
        # the recomputed binding would not match.
        binding = [
            {"label": label,
             **assembly.hashes(),
             "unit_sha256_in_order": list(unit_hashes)}
            for label, assembly, unit_hashes in self._sealed_snapshot
        ]
        recomputed = digest(b"request-generation-v1", canonical_json(binding))
        if recomputed != self._generation_sha256:
            raise BlockingError(
                SECRET_PREFLIGHT_FAILED,
                f"category=generation_digest_changed generation={self.name}")
        return self._sealed_snapshot

    @property
    def generation_sha256(self) -> str | None:
        return self._generation_sha256

    def __len__(self) -> int:
        return len(self._requests)


class PreflightGenerationManifest:
    """Scans a whole generation, then permits counting.

    Every finding in the exact transmitted bytes must resolve to ONE exact
    reviewed source occurrence: same atom, same occurrence index, same
    literal digest, same detection category. `origin.resolve_finding` does
    the resolution; this class supplies the raw scan of each span's source
    and aggregates the refusals.

    Three earlier designs failed here and are described in `origin.py`. The
    most recent cleared an ATOM wholly once its raw findings were authorized,
    which accepted any transmitted finding inside that atom — including one
    that exists only in the serialized form — and treated an atom with no raw
    finding as cleared, so a pattern manufactured by escaping or by the
    verifier's own line prefix passed silently (C4-F01)."""

    def __init__(self, authorizations, *, atom_records: dict,
                 atom_map: dict):
        self.authorizations = authorizations
        self.atom_records = atom_records
        self.atom_map = atom_map
        self.entries: list[dict] = []
        self.generations_sealed: list[str] = []
        self._path_b64_cache: frozenset | None = None
        self._source_scan_cache: dict[str, list] = {}
        # unit hash -> unit record, so a sealed snapshot (which carries only
        # hashes) can recover each unit's atom set for scope resolution.
        self._units_by_hash: dict[str, dict] = {}

    def _source_findings(self, span):
        """The raw scan of ONE span's source text, cached by content digest.

        Keyed by the span's source digest rather than by atom id: the same
        bytes scan identically wherever they appear, and a cache keyed by
        identity could otherwise serve one atom's findings for another."""
        key = span.source_content_sha256 or ""
        cached = self._source_scan_cache.get(key)
        if cached is None:
            cached = list(preflight.occurrence_index_map(span.source_text
                                                         or ""))
            self._source_scan_cache[key] = cached
        return cached

    def path_bytes_b64_by_unit(self, units) -> dict:
        """Each unit's Base64 path identity, for authorization lookup only."""
        mapping = {}
        for unit in units:
            for atom_id in unit["atom_ids"]:
                record = self.atom_records.get(atom_id)
                if record and record.get("path_bytes_b64"):
                    mapping[unit["unit_sha256"]] = record["path_bytes_b64"]
                    break
        return mapping

    def _path_identities(self) -> frozenset:
        """Every changed path's Base64 identity."""
        if self._path_b64_cache is None:
            self._path_b64_cache = frozenset(
                record["path_bytes_b64"]
                for record in self.atom_records.values()
                if record.get("path_bytes_b64"))
        return self._path_b64_cache

    def _assert_no_synthesized_path_identity(self, text: str, *, label: str,
                                             origin_map) -> None:
        """A path identity may only reach a provider as REVIEWED CONTENT.

        `unitpayload.structured_unit` hashes the path on the grounds that a
        path can itself be sensitive, but nothing checked the assembled body,
        and the metadata-atom descriptor carried `path_bytes_b64` verbatim —
        so every added or renamed file shipped its full path anyway (A2-F21).

        The rule is about what the VERIFIER adds, not about censoring the
        repository. A changed source line that genuinely contains a Base64
        path is content the reviewer is meant to read, and blocking it would
        be an unclearable false positive — this repository has exactly such a
        line, in the fixture for this check. So the check is scoped to spans
        that are not reviewed atom content."""
        for identity in sorted(self._path_identities()):
            at = text.find(identity)
            while at != -1:
                span = origin_map.locate(at, len(identity))
                if span is None or span.kind != origin.ATOM_CONTENT:
                    where = "scaffolding" if span is None else span.kind
                    raise BlockingError(
                        SECRET_PREFLIGHT_FAILED,
                        f"category=raw_path_identity_in_payload label={label} "
                        f"origin={where} — the verifier put a path's Base64 "
                        "bytes into the request; paths travel as sha256 only, "
                        "and a synthesized path is not reviewed content, so "
                        "no source authorization can clear it")
                at = text.find(identity, at + 1)

    def _scan_payload(self, assembly, *, payload_kind: str, label: str,
                      unit_count: int) -> dict:
        """Scan ONE exact payload and resolve every finding to its source.

        The text and the map both come from the same assembly, so there is no
        step here that could describe a different document from the one that
        would be sent."""
        text = (assembly.execution_text() if payload_kind == "execution"
                else assembly.count_text())
        origin_map = assembly.origin_map_for(payload_kind)
        self._assert_no_synthesized_path_identity(text, label=label,
                                                  origin_map=origin_map)
        findings = preflight.distinct_occurrences(preflight.scan_text(text))
        resolutions = [
            origin.resolve_finding(finding, origin_map=origin_map,
                                   authorizations=self.authorizations,
                                   source_findings=self._source_findings)
            for finding in findings
        ]
        origin.assert_all_cleared(resolutions, label=label)
        map_record = origin_map.record()
        return {
            "label": label,
            "payload_kind": payload_kind,
            "scanned_chars": len(text),
            # A2-F11: the EXACT canonical payload hash is bound into evidence,
            # so a strict loader can prove the scanned bytes were the request
            # bytes.
            "payload_sha256": sha256_hex(text.encode("utf-8",
                                                     "surrogateescape")),
            # A2-F11/C4-F02: the span map is bound too, so the attribution
            # that cleared these findings is itself evidence rather than an
            # unrecorded step.
            "origin_map_sha256": map_record["origin_map_sha256"],
            "origin_mapping_version": map_record["mapping_version"],
            "span_count_by_kind": map_record["span_count_by_kind"],
            "finding_count": len(findings),
            "cleared_occurrence_count": sum(1 for r in resolutions
                                            if r["cleared"]),
            "unit_count": unit_count,
        }

    def seal(self, generation, ledger) -> str:
        """Scan every request in a SEALED snapshot. Only then may counting run.

        The generation is frozen first, so the set scanned here is exactly the
        set counted later — nothing can be inserted between (A2-F03)."""
        generation_sha256 = generation.seal()
        snapshot = generation.sealed_snapshot()
        attempts_before = ledger.provider_attempts
        for label, assembly, unit_hashes in snapshot:
            count_entry = self._scan_payload(
                assembly, payload_kind="count", label=f"{label}:count",
                unit_count=len(unit_hashes))
            exec_entry = self._scan_payload(
                assembly, payload_kind="execution",
                label=f"{label}:execution", unit_count=len(unit_hashes))
            self.entries.append({
                "generation": generation.name,
                "generation_sha256": generation_sha256,
                "label": label,
                "model_id": assembly.model_id,
                "unit_sha256_in_order": list(unit_hashes),
                **assembly.hashes(),
                "count_payload_scan": count_entry,
                "execution_payload_scan": exec_entry,
            })
        if ledger.provider_attempts != attempts_before:
            raise BlockingError(
                SECRET_PREFLIGHT_FAILED,
                f"category=attempt_during_preflight generation="
                f"{generation.name}")
        self.generations_sealed.append(generation.name)
        return generation_sha256

    def count_generation(self, generation, ledger) -> dict:
        """Count a SEALED generation snapshot. Refuses an unsealed one."""
        snapshot = generation.sealed_snapshot()      # re-verifies the digest
        results: dict[tuple[str, str], int] = {}
        for label, assembly, _unit_hashes in snapshot:
            result = ledger.count(assembly, label=label)
            results[(label, assembly.model_id)] = result.input_tokens
        return results

    def record(self) -> dict:
        record = {
            "schema_version": 2,
            "generations_sealed": list(self.generations_sealed),
            "request_count": len(self.entries),
            "scanned_payload_count": 2 * len(self.entries),
            "entries": self.entries,
            "authorization_set_sha256": (
                self.authorizations.digest() if self.authorizations else None),
            "authorization_authority_class": (
                self.authorizations.authority_class
                if self.authorizations else None),
            "confers_real_call_authority": (
                self.authorizations.confers_real_call_authority
                if self.authorizations else False),
            "honest_scope": "a denylist plus an entropy heuristic over the "
                            "exact count and execution bytes of every request "
                            "in each generation, scanned before any is sent; "
                            "it cannot prove the absence of secrets",
        }
        record["preflight_manifest_sha256"] = digest(
            b"preflight-generation-manifest-v2", canonical_json(record))
        return record


def _fit_generation(units: list[dict], atom_records: dict, atom_map: dict,
                    model_ids: list[str], pin_values: dict, ledger,
                    manifest: PreflightGenerationManifest,
                    payloads_by_unit: dict, review_policy: dict,
                    challenge: str, generation_index: int
                    ) -> tuple[list[tuple[dict, dict]], list[dict]]:
    """One generation: build ALL, preflight ALL, seal, then count.

    Returns (fitted, oversized). Nothing is transmitted until every request
    in the generation has been scanned, so a secret in the last unit stops
    the first unit from being sent."""
    generation = RequestGeneration(f"solo-{generation_index}")
    payloads: dict[str, dict] = {}
    for unit in units:
        payload = _payload_for(unit, atom_records, atom_map)
        payloads[unit["unit_sha256"]] = payload
        for model_id in model_ids:
            request = _request_for(
                model_id, [payload], pin_values, review_policy, challenge,
                manifest.path_bytes_b64_by_unit([unit]))
            generation.add(request,
                           label=f"unit:{unit['unit_sha256'][:16]}:{model_id}",
                           units=[unit], registry=manifest._units_by_hash)

    manifest.seal(generation, ledger)          # scans everything
    counts = manifest.count_generation(generation, ledger)

    fitted: list[tuple[dict, dict]] = []
    oversized: list[dict] = []
    for unit in units:
        per_model = {m: counts[(f"unit:{unit['unit_sha256'][:16]}:{m}", m)]
                     for m in model_ids}
        too_big = [m for m in model_ids
                   if not _fits(m, per_model[m], pin_values)[0]]
        if not too_big:
            payloads_by_unit[unit["unit_sha256"]] = payloads[
                unit["unit_sha256"]]
            fitted.append((unit, per_model))
            continue
        if len(unit["atom_ids"]) == 1:
            raise BlockingError(
                MODEL_CONTEXT_EXCEEDED_UNSPLITTABLE,
                f"category=single_atom_exceeds_context "
                f"models={sorted(too_big)} "
                f"unit={unit['unit_sha256'][:16]} — splitting cannot help")
        oversized.append(unit)
    return fitted, oversized


def _fit_all(units: list[dict], atom_records: dict, atom_map: dict,
             model_ids: list[str], pin_values: dict, ledger,
             manifest: PreflightGenerationManifest, payloads_by_unit: dict,
             review_policy: dict, challenge: str) -> list[tuple[dict, dict]]:
    """Fit every unit, one whole generation of splits at a time."""
    fitted: list[tuple[dict, dict]] = []
    pending = list(units)
    generation_index = 0
    while pending:
        done, oversized = _fit_generation(
            pending, atom_records, atom_map, model_ids, pin_values, ledger,
            manifest, payloads_by_unit, review_policy, challenge,
            generation_index)
        fitted.extend(done)
        # Derive EVERY child for the next generation before counting any of
        # them, so a secret in the last child blocks the first child's call.
        children: list[dict] = []
        for unit in oversized:
            atom_ids = unit["atom_ids"]
            mid = len(atom_ids) // 2
            for child_ids in (atom_ids[:mid], atom_ids[mid:]):
                children.append(derive_unit_record(unit, child_ids,
                                                   atom_records, atom_map))
        pending = children
        generation_index += 1
    # Restore the skeleton's global order: generations complete out of order.
    order = {u["unit_sha256"]: i for i, (u, _c) in enumerate(fitted)}
    fitted.sort(key=lambda pair: (pair[0]["min_patch_ordinal"],
                                  order[pair[0]["unit_sha256"]]))
    return fitted


def _pack_batches(fitted: list[tuple[dict, dict]], payloads_by_unit: dict,
                  model_ids: list[str], pin_values: dict, ledger,
                  manifest: PreflightGenerationManifest, review_policy: dict,
                  challenge: str) -> list[dict]:
    """Deterministic packing, bounded by input context AND output capacity.

    Batch candidates are proposed for a whole packing step, preflighted
    together, and only then counted — the same generation discipline as unit
    fitting, for the same reason."""
    unit_cap = review_policy["max_units_per_batch"]
    solo = {u["unit_sha256"]: c for u, c in fitted}

    # Deterministic grouping first: review class, then the output-capacity
    # bound. No counting is needed to decide either.
    groups: list[list[dict]] = []
    current: list[dict] = []
    for unit, _counts in fitted:
        if current and (batching.review_class(current[0])
                        != batching.review_class(unit)):
            groups.append(current)
            current = []
        if len(current) >= unit_cap:
            groups.append(current)
            current = []
        current.append(unit)
    if current:
        groups.append(current)

    batches: list[dict] = []
    step = 0
    while groups:
        generation = RequestGeneration(f"batch-{step}")
        for index, units in enumerate(groups):
            reviewpolicy.assert_output_capacity(
                len(units), pin_values["VERIFIER_MAX_OUTPUT_TOKENS"],
                where=f"group-{index}")
            payloads = [payloads_by_unit[u["unit_sha256"]] for u in units]
            for model_id in model_ids:
                request = _request_for(
                    model_id, payloads, pin_values, review_policy, challenge,
                    manifest.path_bytes_b64_by_unit(units))
                generation.add(request, label=f"group:{index}:{model_id}",
                               units=units, registry=manifest._units_by_hash)
        manifest.seal(generation, ledger)
        counts = manifest.count_generation(generation, ledger)

        keep: list[list[dict]] = []
        for index, units in enumerate(groups):
            per_model = {m: counts[(f"group:{index}:{m}", m)]
                         for m in model_ids}
            fits = True
            for model_id in model_ids:
                floor = max(solo[u["unit_sha256"]][model_id] for u in units)
                counting2.assert_batch_not_below_member_floor(
                    measured=per_model[model_id], floor=floor,
                    model_id=model_id, label=f"group-{index}")
                if not _fits(model_id, per_model[model_id], pin_values)[0]:
                    fits = False
            if fits:
                headroom = {m: _fits(m, per_model[m], pin_values)[1]
                            for m in model_ids}
                hashes = {}
                for model_id in model_ids:
                    request = _request_for(
                        model_id,
                        [payloads_by_unit[u["unit_sha256"]] for u in units],
                        pin_values, review_policy, challenge,
                        manifest.path_bytes_b64_by_unit(units))
                    hashes[model_id] = request.hashes()
                record = batching.batch_record(
                    f"batch-{len(batches) + len(keep):04d}", units, per_model,
                    headroom, hashes)
                record["worst_case_output_tokens"] = (
                    reviewpolicy.worst_case_output_tokens(len(units)))
                record["batch_sha256"] = batching.batch_digest(record)
                batches.append(record)
            elif len(units) == 1:
                raise BlockingError(
                    MODEL_CONTEXT_EXCEEDED,
                    "category=single_unit_batch_exceeds_context")
            else:
                half = len(units) // 2
                keep.append(units[:half])
                keep.append(units[half:])
        groups = keep
        step += 1
    # Batches complete out of order because oversized groups split and go
    # round again. The partition proof requires the GLOBAL unit order, so
    # batches are re-sorted by where their first unit sits in `fitted` and
    # re-identified accordingly.
    position = {u["unit_sha256"]: i for i, (u, _c) in enumerate(fitted)}
    ordered = []
    for i, record in enumerate(sorted(
            batches,
            key=lambda b: position[b["unit_sha256_in_order"][0]])):
        record = dict(record)
        record["batch_id"] = f"batch-{i:04d}"
        record.pop("batch_sha256", None)
        record["batch_sha256"] = batching.batch_digest(record)
        ordered.append(record)
    return ordered


def _cost_plan(batches: list[dict], model_ids: list[str],
               pin_values: dict, *, mock: bool = True) -> dict:
    """Integer micro-USD cost PROJECTION. No float ever enters this ledger.

    When the counts behind it came from the mock transport, every number here
    is arithmetic over a local stand-in — not provider token evidence and not
    a spend estimate. The record says so in its own fields rather than
    leaving the reader to infer it from the evidence class elsewhere
    (C4-F19)."""
    per_model = {}
    total_input, total_output = 0, 0
    for model_id in model_ids:
        cap = capabilities.capability(model_id)
        input_micros = 0
        output_micros = 0
        surcharged: list[str] = []
        for batch in batches:
            tokens = batch["input_tokens_by_model"][model_id]
            rate = cap.input_micro_usd_per_million
            out_rate = cap.output_micro_usd_per_million
            if (cap.long_context_threshold_input_tokens is not None
                    and tokens > cap.long_context_threshold_input_tokens):
                rate = rate * cap.above_threshold_input_multiplier_bp // 10_000
                out_rate = (out_rate
                            * cap.above_threshold_output_multiplier_bp // 10_000)
                surcharged.append(batch["batch_id"])
            input_micros += -(-tokens * rate // MICRO_PER_MILLION)
            output_micros += -(-pin_values["VERIFIER_MAX_OUTPUT_TOKENS"]
                               * out_rate // MICRO_PER_MILLION)
        retries = pin_values["VERIFIER_GENERATION_MAX_RETRIES"]
        per_model[model_id] = {
            "input_projection_micro_usd": input_micros,
            "worst_case_output_micro_usd": output_micros,
            "worst_case_total_micro_usd":
                (input_micros + output_micros) * (1 + retries),
            # C4-F20: whether a batch CROSSED the threshold, not whether the
            # model has one. The old field was true for every batch of a
            # model with any threshold, which said nothing about this plan.
            "long_context_surcharge_batch_ids": surcharged,
            "long_context_surcharge_applied_to_any_batch": bool(surcharged),
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
        "input_projection_micro_usd": total_input,
        "worst_case_output_micro_usd": total_output,
        "worst_case_total_micro_usd": worst_case,
        "count_call_billing_state": counting.COUNT_BILLING_STATE,
        "money_unit": "integer micro-USD",
        "basis": ("MOCK_ARITHMETIC_NOT_PROVIDER_TOKEN_EVIDENCE" if mock
                  else "TRUSTED_PROVIDER_COUNT_EVIDENCE"),
        "not_provider_token_evidence": mock,
        "not_spend_estimate": mock,
        "honest_scope": (
            "arithmetic over a local stand-in's byte count and the pinned "
            "price table; no provider counted these tokens and no billing "
            "was observed" if mock else
            "arithmetic over trusted provider counts and the pinned price "
            "table; observed billing is a separate record"),
    }


def _assert_mock_transport(transport) -> None:
    """A2-F04: candidate finalization refuses any non-mock transport.

    Evidence authority and TRANSMISSION authority are different questions. A
    transport whose evidence would be untrusted can still open a socket and
    send content. Candidate finalization is local-only by construction, so it
    accepts a transport ONLY if it declares MOCK and carries no real network
    method — checked BEFORE any request is assembled, so a rejected transport
    never sees a byte."""
    source = getattr(transport, "source", None)
    if source != counting.SOURCE_MOCK:
        raise BlockingError(
            SECRET_PREFLIGHT_FAILED,
            f"category=candidate_finalization_requires_mock_transport "
            f"source={source!r} — candidate-side finalization is local-only; "
            "a real transport belongs to the trusted lane and is refused here "
            "before any request is built")
    for forbidden in ("connect", "sendall", "getaddrinfo", "urlopen",
                      "request", "session"):
        if callable(getattr(transport, forbidden, None)):
            raise BlockingError(
                SECRET_PREFLIGHT_FAILED,
                f"category=candidate_transport_has_network_method "
                f"method={forbidden} — a mock transport must not carry a "
                "network method; refused before any request is built")


def finalize(skeleton: dict, *, cwd, operator_pins: dict, transport,
             authorizations=None, challenge: str = "LOCAL-MOCK-CHALLENGE",
             required_approver: str = reviewpolicy.GOVERNED_REQUIRED_APPROVER,
             minimum_other_approvers: int = 1) -> dict:
    """Produce a strict, PRIVATE mock-finalization report. Zero generation.

    Named for what it does locally. Nothing here can produce provider
    evidence, so nothing here can produce an executable plan."""
    _assert_mock_transport(transport)          # before ANY request is built
    artifact.validate_strict(skeleton)
    _rebuild_and_compare(skeleton, cwd=cwd)
    structural_preconditions(skeleton)

    model_ids = list(skeleton["requested_model_ids"])
    capability_policy = capabilities.policy_record(model_ids)
    pin_record = pinsmod.test_pin_record(operator_pins, model_ids)
    pin_values = pin_record["pins"]
    review_policy = reviewpolicy.policy_record(
        model_ids,
        required_approver=required_approver,
        minimum_other_approvers=minimum_other_approvers,
        max_output_tokens=pin_values["VERIFIER_MAX_OUTPUT_TOKENS"])

    if len(skeleton["units"]) > pin_values["VERIFIER_MAX_REVIEW_UNITS"]:
        raise BlockingError(
            CHUNK_COUNT_EXHAUSTED,
            f"category=review_unit_cap_exceeded units={len(skeleton['units'])} "
            f"cap={pin_values['VERIFIER_MAX_REVIEW_UNITS']}")

    atom_map = atom_texts(skeleton, cwd=cwd)
    atom_records = unitpayload.index_atom_records(skeleton)
    ledger = counting2.CountLedger(transport, pin_values)
    manifest = PreflightGenerationManifest(
        authorizations, atom_records=atom_records, atom_map=atom_map)
    payloads_by_unit: dict[str, dict] = {}

    fitted = _fit_all(skeleton["units"], atom_records, atom_map, model_ids,
                      pin_values, ledger, manifest, payloads_by_unit,
                      review_policy, challenge)
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
        "mock_finalization_report_sha256": None,
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
        "preflight_manifest": manifest.record(),
        "cost_plan": cost,
        "logical_count_requests": ledger.logical_requests,
        # C4-F19: a MOCK report never contacted a provider. The field is
        # named for what it counts — attempts against the local stand-in.
        "mock_transport_attempts": ledger.provider_attempts,
        "count_cache_hits": ledger.cache_hits,
        "generation_calls_performed": 0,
        # C4-F13: the challenge the executor MUST use, bound into the report
        # and therefore into its digest. It was previously a caller argument
        # at execution time, so a caller could execute a request the plan had
        # never described.
        "execution_challenge": challenge,
        "execution_challenge_sha256": sha256_hex(challenge.encode()),
        "challenge_provenance": "LOCAL_MOCK_CONSTANT_NOT_UNPREDICTABLE",
        "pending_requirements": pending,
        "executable": evidence.is_executable_authority(count_evidence),
    }
    executable_plan["mock_finalization_report_sha256"] = mock_report_digest(
        executable_plan)
    return executable_plan


def mock_report_digest(record: dict) -> str:
    """The digest of a MOCK report, under a mock-specific label.

    `executable-review-plan-v1` is reserved for trusted evidence (C4-F19). A
    label is a claim about what a record is, and a locally-produced report
    built on a stand-in transport is not an executable review plan under any
    reading."""
    stripped = {k: v for k, v in record.items()
                if k != "mock_finalization_report_sha256"}
    return digest(b"mock-finalization-report-v1", canonical_json(stripped))


#: MC3 name, retained so an older caller fails loudly rather than silently
#: hashing a mock report under a trusted label.
def plan_digest(record: dict) -> str:
    raise BlockingError(
        EXECUTABLE_PLAN_INVALID,
        "category=reserved_trusted_digest_label — executable-review-plan-v1 "
        "names TRUSTED evidence; a mock report is hashed by "
        "mock_report_digest under mock-finalization-report-v1")


_PLAN_KEYS = (
    "schema_version", "artifact", "stage",
    "mock_finalization_report_sha256", "execution_challenge",
    "execution_challenge_sha256", "challenge_provenance",
    "publication_class", "review_skeleton_sha256", "repository_state",
    "identities", "disposition_root", "git_execution_policy_sha256",
    "capability_policy", "operator_pin_record",
    "final_units", "batches", "count_evidence",
    "cost_plan", "count_ledger", "preflight_manifest",
    "logical_count_requests", "mock_transport_attempts",
    "count_cache_hits", "review_request_policy",
    "requested_model_acceptance_policy", "generation_calls_performed",
    "pending_requirements", "executable",
)


def _plan_fail(reason: str):
    raise BlockingError(EXECUTABLE_PLAN_INVALID, reason)


def validate_mock_finalization_strict(record: dict, *, skeleton: dict,
                                      cwd, operator_pins: dict,
                                      transport=None,
                                      authorizations=None,
                                      expected_mock_algorithm: str = (
                                          counting.MOCK_COUNT_ALGORITHM)
                                      ) -> dict:
    """RECONSTRUCT the report from the commits and compare it, whole.

    `validate_report_shape` below recomputes what the record already
    contains. That is not verification of a private report whose CONTENT is
    deliberately absent: every load-bearing number — the counts, the request
    hashes, the origin maps, the child unit records, the headroom, the cost —
    is derived from source the record does not carry, so an edited claim plus
    a recomputed digest survived it (C4-F04).

    This runs the finalizer again against the same skeleton, commits, PINs
    and authorizations, and requires the canonical bytes to be identical. A
    validator with no repository context cannot make this claim, which is why
    `skeleton` and `cwd` are required rather than optional."""
    shape = validate_report_shape(record)
    if record["count_evidence"]["evidence_class"] != evidence.MOCK_TEST_EVIDENCE:
        _plan_fail("category=not_a_mock_report_evidence_class "
                   f"class={record['count_evidence']['evidence_class']}")
    if record["count_ledger"].get("mock_count_algorithm") != (
            expected_mock_algorithm):
        _plan_fail("category=mock_algorithm_mismatch")

    rebuilt = finalize(
        skeleton, cwd=cwd, operator_pins=operator_pins,
        transport=transport or counting.MockCountTransport(),
        authorizations=authorizations,
        challenge=record["execution_challenge"],
        required_approver=record["review_request_policy"][
            "required_approver"],
        minimum_other_approvers=record["review_request_policy"][
            "minimum_other_approvers"])
    if canonical_json(rebuilt) != canonical_json(record):
        differing = sorted(
            key for key in set(rebuilt) | set(record)
            if canonical_json(rebuilt.get(key)) != canonical_json(
                record.get(key)))
        _plan_fail("category=mock_report_not_reproducible "
                   f"differing_fields={differing} — the report does not match "
                   "a rebuild from its own skeleton, commits, PINs and "
                   "authorizations")
    return {**shape, "reconstructed": True,
            "reconstruction_scope": "full canonical-byte equality against a "
                                    "rebuild from the recorded commits"}


def validate_report_shape(record: dict) -> dict:
    """Recompute every claim the record itself carries.

    Honestly named: this proves internal consistency and re-derives what can
    be re-derived from the record alone. It CANNOT prove the record describes
    the commits it names — for that, `validate_mock_finalization_strict`
    rebuilds from source.

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

        if mock_report_digest(record) != record[
                "mock_finalization_report_sha256"]:
            _plan_fail("category=plan_digest_mismatch")
        if record["execution_challenge_sha256"] != sha256_hex(
                record["execution_challenge"].encode()):
            _plan_fail("category=challenge_digest_mismatch")

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

        recomputed_cost = _cost_plan(
            record["batches"], model_ids, pin_values,
            mock=record["count_evidence"]["evidence_class"]
            == evidence.MOCK_TEST_EVIDENCE)
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
        # PASS B: a MOCK report's counts are RECOMPUTABLE. The mock
        # algorithm is named, so a loader can check the numbers rather than
        # trusting them — a report whose counts were edited fails here even
        # though every digest was recomputed.
        if record["count_evidence"]["evidence_class"] == (
                evidence.MOCK_TEST_EVIDENCE):
            for entry in record["count_ledger"]["counts"]:
                if entry["input_tokens"] < 0:
                    _plan_fail("category=mock_count_negative")
            if record["count_ledger"].get("mock_count_algorithm") != (
                    counting.MOCK_COUNT_ALGORITHM):
                _plan_fail(
                    "category=mock_algorithm_not_named — a mock report must "
                    "state the algorithm its counts came from, so they can be "
                    "recomputed rather than believed")

        # every sealed preflight generation must be bound into the manifest
        manifest = record["preflight_manifest"]
        sealed = set(manifest["generations_sealed"])
        for entry in manifest["entries"]:
            if entry["generation"] not in sealed:
                _plan_fail(f"category=preflight_entry_unsealed_generation "
                           f"generation={entry['generation']}")
            for scan_key in ("count_payload_scan", "execution_payload_scan"):
                scan = entry[scan_key]
                if len(scan.get("payload_sha256", "")) != 64:
                    _plan_fail(f"category=preflight_payload_hash_missing "
                               f"label={entry['label']} kind={scan_key}")
            if len(entry.get("generation_sha256", "")) != 64:
                _plan_fail("category=preflight_generation_digest_missing")

        # attempt accounting must reconcile with the ledger
        ledger = record["count_ledger"]
        if ledger["provider_attempt_count"] != record[
                "mock_transport_attempts"]:
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


CLI_INPUT_INVALID = "CLI_INPUT_INVALID"


def _read_json_file(path: str, *, what: str) -> object:
    """Read and parse a JSON input, with a TYPED, sanitized failure (A2-F07).

    MC4's first CLI let a file-open or a parse error escape as a bare Python
    traceback. A CLI that transmits nothing should still fail like the rest of
    the package: one typed BlockingError, no stack, no path leakage beyond the
    argument the operator themselves supplied."""
    import json
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise BlockingError(
            CLI_INPUT_INVALID,
            f"category=cannot_read_{what} errno={exc.errno}") from None
    try:
        return json.loads(raw)
    except Exception as exc:
        raise BlockingError(
            CLI_INPUT_INVALID,
            f"category=unparseable_{what} "
            f"exception_class={type(exc).__name__}") from None


def _authorizations_from_json(skeleton: dict, path: str | None):
    """Load occurrence-scoped fixture authorization claims from JSON.

    There is deliberately no plaintext-literal option: MC4's `--allowlist`
    listed credential-shaped strings verbatim, which is the exact thing the
    authorization record exists to avoid. Claims carry the literal SHA-256 and
    the scope, never the value."""
    from . import authority
    if path is None:
        return None
    records = _read_json_file(path, what="authorizations")
    if not isinstance(records, list):
        raise BlockingError(CLI_INPUT_INVALID,
                            "category=authorizations_not_a_list")
    state = skeleton["repository_state"]
    return authority.LiteralAuthorizationSet(
        records, repository_identity=skeleton.get("repository_identity",
                                                  "unknown"),
        target_base_sha=state["target_base_sha"],
        diff_base_sha=state["diff_base_sha"], head_sha=state["head_sha"])


def main(argv: list[str]) -> int:
    """`--finalize-mock`: skeleton in, MOCK finalization report out.

    Local-only by construction: it runs on the labelled mock transport, makes
    zero provider and zero generation calls, and its report is necessarily
    non-executable. The name says so — MC4's `--finalize` overstated what a
    local run produces (A2-F07)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="independent_verify.py --finalize-mock")
    parser.add_argument("--finalize-mock", dest="finalize_mock",
                        action="store_true", required=True)
    parser.add_argument("--skeleton", required=True)
    parser.add_argument("--output", required=True,
                        help="private mock-finalization-report path")
    parser.add_argument("--authorizations",
                        help="JSON list of occurrence-scoped reviewed-literal "
                             "CLAIMS (hashes and scope, never plaintext)")
    parser.add_argument("--local-summary",
                        help="also write an UNTRUSTED_LOCAL_SUMMARY")
    args = parser.parse_args(argv)

    try:
        skeleton = artifact.parse_strict(
            _json_bytes(_read_json_file(args.skeleton, what="skeleton")))
        authorizations = _authorizations_from_json(skeleton,
                                                    args.authorizations)
        plan_record = finalize(skeleton, cwd=None,
                               operator_pins=_pins_from_environment(),
                               transport=counting.MockCountTransport(),
                               authorizations=authorizations)
        validate_report_shape(plan_record)
        artifact.write_atomic(plan_record, args.output)
        if args.local_summary:
            artifact.write_atomic(public_plan_summary(plan_record),
                                  args.local_summary)
    except BlockingError as exc:
        print(f"FINALIZE-MOCK BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(f"artifact={plan_record['artifact']} "
          f"units={len(plan_record['final_units'])} "
          f"batches={len(plan_record['batches'])} "
          f"logical_requests={plan_record['logical_count_requests']} "
          f"transport_attempts={plan_record['mock_transport_attempts']} "
          f"cache_hits={plan_record['count_cache_hits']} "
          f"generation_calls={plan_record['generation_calls_performed']} "
          f"evidence_class={plan_record['count_evidence']['evidence_class']} "
          f"executable={plan_record['executable']}")
    for item in plan_record["pending_requirements"]:
        print(f"PENDING {item['code']}: {item['reason']}")
    print(f"wrote {args.output}")
    return 0


def _json_bytes(obj) -> bytes:
    import json
    return json.dumps(obj).encode("utf-8")


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


def validate_plan_strict(record: dict) -> dict:
    """Retired (C4-F04). Say which check you mean.

    The old name promised strict loading and delivered internal consistency.
    Callers must now choose: `validate_report_shape` for what the record can
    prove about itself, or `validate_mock_finalization_strict` — which needs
    the skeleton and the checkout — for whether it describes the commits it
    names."""
    raise BlockingError(
        EXECUTABLE_PLAN_INVALID,
        "category=ambiguous_validator — validate_plan_strict promised more "
        "than it checked; use validate_report_shape for self-consistency or "
        "validate_mock_finalization_strict(record, skeleton=..., cwd=..., "
        "operator_pins=...) for reconstruction from the commits")


PUBLICATION_PREFLIGHT_FAILED = "PUBLICATION_PREFLIGHT_FAILED"

# Fields a local summary may carry. Anything else is refused rather than
# published — a summary is an allowlist of facts, not a filtered dump.
_SUMMARY_FIELDS = frozenset({
    "artifact", "publication_class", "trust_status", "publication_preflight",
    "head_sha", "diff_base_sha", "review_skeleton_sha256",
    "mock_finalization_report_sha256", "disposition_root", "final_unit_count",
    "batch_count", "logical_count_requests", "mock_transport_attempts",
    "generation_calls_performed", "cost_plan", "count_evidence_class",
    "executable", "pending_codes", "summary_sha256",
})


def publication_preflight(summary: dict) -> dict:
    """Gate a summary before it is written anywhere (MC3-F22).

    MC3 wrote the summary straight out and left a docstring saying
    publication still needed a preflight. Three checks, all fail-closed: only
    allowlisted fields, no raw path bytes or content, and a secret scan of the
    exact bytes that would be published."""
    unknown = sorted(set(summary) - _SUMMARY_FIELDS)
    if unknown:
        raise BlockingError(
            PUBLICATION_PREFLIGHT_FAILED,
            f"category=summary_field_not_allowlisted fields={unknown}")
    blob = canonical_json(summary).decode("utf-8", "surrogateescape")
    for forbidden in ("path_bytes_b64", "original_path_bytes_b64",
                      "atom_ids", "unit_sha256_in_order", "instructions",
                      "content"):
        if forbidden in blob:
            raise BlockingError(
                PUBLICATION_PREFLIGHT_FAILED,
                f"category=summary_contains_private_field field={forbidden}")
    findings = preflight.distinct_occurrences(preflight.scan_text(blob))
    if findings:
        raise BlockingError(
            PUBLICATION_PREFLIGHT_FAILED,
            f"category=summary_secret_preflight_failed "
            f"finding_count={len(findings)}")
    return {"passed": True, "scanned_chars": len(blob),
            "allowlisted_field_count": len(summary)}


def public_plan_summary(executable_plan: dict) -> dict:
    """UNTRUSTED LOCAL summary of a mock report: no unit paths, no request
    bodies. A2-F27/F37: this is neither public nor trusted — it is a local
    view of local mock arithmetic, and it says so in its own fields."""
    summary = {
        "artifact": "untrusted-local-summary",
        "publication_class": "local-only",
        "trust_status": "UNTRUSTED_LOCAL_SUMMARY",
        "head_sha": executable_plan["repository_state"]["head_sha"],
        "diff_base_sha": executable_plan["repository_state"]["diff_base_sha"],
        "review_skeleton_sha256": executable_plan["review_skeleton_sha256"],
        "mock_finalization_report_sha256":
            executable_plan["mock_finalization_report_sha256"],
        "disposition_root": executable_plan["disposition_root"],
        "final_unit_count": len(executable_plan["final_units"]),
        "batch_count": len(executable_plan["batches"]),
        "logical_count_requests": executable_plan["logical_count_requests"],
        "mock_transport_attempts":
            executable_plan["mock_transport_attempts"],
        "generation_calls_performed": 0,
        "cost_plan": executable_plan["cost_plan"],
        "count_evidence_class":
            executable_plan["count_evidence"]["evidence_class"],
        "executable": executable_plan["executable"],
        "pending_codes": [p["code"]
                          for p in executable_plan["pending_requirements"]],
    }
    # MC3-F22: the summary is gated on its EXACT bytes before it can be
    # written, and records that it was.
    summary["publication_preflight"] = publication_preflight(summary)
    summary["summary_sha256"] = digest(b"untrusted-local-summary-v1",
                                       canonical_json(summary))
    return summary
