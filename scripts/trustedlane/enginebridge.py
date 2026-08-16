"""The ONLY trusted-lane entry into `scripts/verifier`.

The integration addendum settles an architectural question this lane had been
answering the wrong way. The candidate verifier already contains the stronger
planner, finalizer, exact occurrence-scoped preflight, OriginMap, request
assembly, twelve-PIN schema, capability table, batcher, executor, response
validation and verdict aggregation. The previous trusted runtime carried a
parallel, weaker version of several of those — which is precisely why
single-model review, unbound plans and a budget-only "preflight" got through.

So the division is now:

    scripts/verifier      owns review semantics
    scripts/trustedlane   owns the protected capability and evidence boundary

and this module is the seam. Everything the lane needs from the engine comes
through here, so there is one place to audit for "did the trusted side reach
into candidate internals", rather than a scatter of imports across the runtime.

**The artifact is NEVER imported as `verifier` (EX6-R02).** It used to be, and
that was the defect. `load_engine` inserted the artifact root on `sys.path` and
imported `verifier`, so one interpreter could hold two packages called
`verifier` — the artifact's and the candidate checkout's — whose classes are
different objects. Everything downstream then depended on which one won:
`pytest.raises(BlockingError)` caught the wrong class, and
`assert_no_candidate_import` needed an `engine_root` exception carved into it
because it could no longer tell the two apart by name.

The engine's modules use ONLY relative imports — `from .errors import`, never
`from verifier.errors import`, verified by `test_the_engine_uses_only_relative_
imports` — so the package is relocatable. `EngineSession` loads it under a
top-level name derived from the artifact root:

    trustedengine_<16 hex of the root path>

Nothing named `verifier` enters `sys.modules`, `sys.path` is not touched at all,
and the origin question is answered by the module NAME as well as by its file.
That makes `assert_no_candidate_import` absolute again: in a credential-bearing
runner, ANY `verifier` module is the candidate, full stop.

The logical keys stay `verifier.executor` and so on, because that is what the
engine's own documentation calls them and what a reader is looking for. The
actual `sys.modules` name is the alias, and `record()` reports both.

**What this module must never grow.** A second scanner, origin map, PIN schema,
capability table, review policy, request builder, response schema, batcher,
splitter, executor, verdict aggregator or strict plan loader. When something
here looks like it needs one, it needs to call the engine instead. The only
code that belongs in this file is the call, the argument marshalling, and the
refusals that protect the boundary.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import os
import re
import sys

from .errors import refuse

#: The two source roles an engine artifact must carry, and the package name
#: each is imported under once extracted. `enginesource.SOURCE_ROLES` maps the
#: same roles to their repository prefixes.
ENGINE_PACKAGES = ("verifier", "trustedlane")

#: The exact planner entry point. `build_review_skeleton` does NOT exist — the
#: previous `d1cli` imported that name, so its path could never have run. The
#: real signature is
#:
#:     build_skeleton(target_base_ref, head_ref, *, cwd, budget=...)
#:
#: recorded here so a rename upstream is a named refusal rather than an
#: ImportError somewhere inside a credential-bearing job.
PLANNER_MODULE = "verifier.plan"
PLANNER_FUNCTION = "build_skeleton"

#: Symbols the bridge requires the engine to expose. Checked at load time so a
#: mismatched artifact fails before a credential is read, rather than at the
#: first call after one has been.
REQUIRED_ENGINE_SYMBOLS = {
    "verifier.plan": ("build_skeleton", "REQUESTED_MODEL_IDS",
                      "POLICY_PIN_NAMES"),
    "verifier.policy": ("REQUESTED_MODEL_IDS", "POLICY_PIN_NAMES"),
    "verifier.authority": ("TrustedVerifier", "VERIFIED_CLASSES",
                           "promote_literal_authorizations",
                           "literal_authorization_digest",
                           "validate_literal_authorization",
                           "assert_usable_for_real_call"),
    "verifier.pins": ("validate_pins", "promote_pin_authorization",
                      "validate_pin_authority", "pin_digest"),
    # The engine's own typed failure. The lane catches it at the seam and
    # re-raises a lane refusal: a `BlockingError` escaping into D1 would be an
    # unhandled crash in a credential-bearing job, and catching `Exception`
    # would swallow real bugs in this bridge.
    "verifier.errors": ("BlockingError",),
    "verifier.reviewpolicy": ("GOVERNED_REQUIRED_APPROVER", "policy_record",
                              "LENS_IDS"),
    "verifier.providerreq": ("assemble_request", "ProviderRequest"),
    "verifier.preflight": ("scan_text", "preflight_request"),
    "verifier.origin": ("OriginMap", "resolve_finding"),
    "verifier.executor": ("execute_batch", "validate_response_envelope",
                          "rebuild_payloads", "reconstruct_batch_requests",
                          "assert_request_matches_plan", "GenerationLedger",
                          "ExecutionPreflightManifest", "synthesize",
                          "assert_output_carries_no_secret"),
    "verifier.verdicts": ("validate_verdicts", "assert_distinct_reasoning"),
    # Slice 2. `prepare_review_plan_core` is the evidence-NEUTRAL half of
    # `finalize`: everything both lanes do, and nothing either does alone. The
    # lane calls it instead of carrying a second copy of unit derivation,
    # preflight, counting, packing and the cost gates — which is where the
    # single-model review and the budget-only "preflight" came from.
    "verifier.finalize": ("prepare_review_plan_core", "finalize_mock",
                          "PREPARE_CORE_VERSION"),
    "verifier.counting": ("SOURCE_PROVIDER", "SOURCE_MOCK", "COUNT_PATH"),
    "verifier.capabilities": ("policy_record", "policy_digest", "capability"),
    # The two the lane had been reaching for by direct `import verifier.…`,
    # which is exactly what this seam exists to prevent. `canon.b64` encodes
    # every path identity the lane passes to the executor, and
    # `unitpayload.index_atom_records` is how a literal occurrence is located
    # in the transmitted bytes. Both were being imported from the CHECKOUT in
    # the test process — a second engine instance, silently.
    "verifier.canon": ("b64", "canonical_json", "digest", "sha256_hex"),
    "verifier.unitpayload": ("structured_unit", "index_atom_records"),
}

#: What the lane refuses to find in a core result. `prepare_review_plan_core`
#: must decide nothing about evidence; if a future edit made it emit any of
#: these, the lane would be labelling evidence produced by candidate code.
FORBIDDEN_CORE_FIELDS = ("count_evidence", "evidence_class", "executable",
                         "publication_class", "signature", "signed_by",
                         "authority_class")

#: The key `executor.execute_batch` returns its per-model verdict evidence
#: under. Named as a constant so the test that asserts the engine still emits
#: it has something to compare against, rather than repeating the string and
#: agreeing with itself.
EVIDENCE_RECORDS_KEY = "per_model_verdict_evidence"


def _resolved(path: str) -> str:
    return os.path.realpath(path)


#: A STATIC sentence per engine code, saying what the operator can do next.
#:
#: Keyed on the code alone. Nothing the engine returned reaches this text —
#: not the message, not a path, not an atom id, not a byte of the payload —
#: so it can be published wherever the code can. That distinction is the
#: whole design: the message is withheld because it carries the payload, and
#: a fixed remediation string carries nothing.
#:
#: The gap this closes is measured, not hypothetical. The first privileged run
#: on the pull request that added the readable reviewer refused with
#: `code=SECRET_PREFLIGHT_FAILED` and nothing else, and finding out why took
#: rebuilding the trusted runtime in a scratch directory, unmasking the
#: engine's message in that throwaway copy, and re-running the count against
#: the real diff. The cause was two credential-SHAPED test fixtures written as
#: literals — both carrying a `detect-secrets` pragma, which is exactly the
#: wrong instinct here and produced no signal that it was wrong.
#: The engine's own category token, and nothing else.
#:
#: ANCHORED, and the charset admits an identifier only: no `/` for a path, no
#: `.` for a filename, no space, no `=`, and a length bound. Whatever else the
#: engine's message carries — a path, an atom id, a fragment of the payload it
#: just refused to transmit — begins after this match and is discarded.
#:
#: Written because the blanket suppression was right about SOME codes and
#: blinding about others. Every message
#: `executor.validate_response_envelope` can produce for
#: PROVIDER_RESPONSE_INVALID is structural — a category name, an HTTP status,
#: a byte count, a key name — and suppressing all of it turned the panel's
#: first real generation failure into a run nobody could diagnose.
#:
#: The identifier must also be TERMINATED by whitespace or the end of the
#: message. Without that, `category=has/a/path` matched its leading `has` and
#: published a prefix of something that was never an engine category — no leak,
#: but a weaker rule than the one this claims to be. A real category is always
#: a whole word followed by a space or nothing.
_ENGINE_CATEGORY = re.compile(r"\Acategory=([a-z_][a-z0-9_]{0,63})(?=\s|\Z)")


#: The attribute an engine `BlockingError` actually carries its text in.
#: `verifier.errors.BlockingError.__init__` binds `code` and `message`, and
#: NOTHING in the engine binds `reason` — so reading `reason` here returned
#: None for every refusal from every one of the 154 sites, and the token this
#: function exists to publish was never emitted once. The unit tests did not
#: catch it because they raised a locally-defined stub that had a `reason`;
#: they proved the stub. `test_engine_category_reads_the_real_exception`
#: now binds the real class so the two cannot drift apart again.
_ENGINE_MESSAGE_ATTRIBUTE = "message"


def engine_category(exc) -> str | None:
    """The category identifier from an engine refusal, or None.

    Returns None rather than guessing whenever the message does not BEGIN with
    a literal `category=<identifier>`: 35 of the engine's 154 refusal sites
    start with prose instead, and prose is exactly the thing that can carry a
    path. A partial match is not attempted anywhere in the string, because a
    `category=` appearing mid-message could have been interpolated from
    content."""
    message = getattr(exc, _ENGINE_MESSAGE_ATTRIBUTE, None)
    if not isinstance(message, str):
        return None
    match = _ENGINE_CATEGORY.match(message.strip())
    return match.group(1) if match else None


ENGINE_CODE_REMEDIES = {
    "PROVIDER_RESPONSE_INVALID": (
        "the provider replied and the engine refused the reply. This code "
        "spans TWO very different failures and the `engine_category=` above "
        "is what tells them apart. `generation_*` categories are envelope "
        "structure, raised by `executor.validate_response_envelope`. "
        "Everything else comes from `verdicts.py` and is verdict CONTENT "
        "policy — the unit set answered, the challenge echo in "
        "`proof_of_check`, reason length and charset, the lens vocabulary, "
        "identical canned approvals. Content policy is the larger set by far, "
        "so do not read this code as 'a malformed reply'"),
    "SECRET_PREFLIGHT_FAILED": (
        "a secret-shaped literal in the REVIEWED DIFF could not be mapped to "
        "an authorized source occurrence. This lane passes "
        "`authorizations=None` because it has no operator envelope, so no "
        "clearance exists that could ever authorize one and a "
        "`detect-secrets` pragma does not help. Do not write "
        "credential-shaped literals in source; assemble test fixtures at run "
        "time from a fixed seed. To find the literal, scan the changed files "
        "with the engine's own `verifier.preflight.scan_text`, which RETURNS "
        "findings rather than raising"),
}


@contextlib.contextmanager
def engine_refusals(engine: dict, *, where: str):
    """Translate the engine's typed failure into a lane refusal.

    `BlockingError` is the engine's fail-closed stop and it is CORRECT for it
    to raise — a preflight that found an unauthorized literal must not return
    normally. But it is not a `LaneRefusal`, so it escaped D1's
    `except LaneRefusal` handler: the pending status stayed on the pull request
    forever, no failure was published, and `laneentry` reported a crash rather
    than a refusal. The lane's whole error contract is that a refusal is
    distinguishable from a crash without reading a traceback.

    The engine's CODE is reported and its message is not. The message can carry
    a path, an atom id or a fragment of the scanned payload, and the payload is
    the thing the preflight just refused to transmit.

    What IS added is a static remediation sentence looked up by that code —
    see `ENGINE_CODE_REMEDIES`. Withholding the message was right and stays;
    withholding every way to act on it was not the same decision, and for
    `SECRET_PREFLIGHT_FAILED` it left an operator with a code and no next
    step."""
    blocking = engine["modules"]["verifier.errors"].BlockingError
    try:
        yield
    except blocking as exc:
        code = str(getattr(exc, "code", "UNKNOWN"))
        # Looked up by code and nothing else. `.get` rather than a membership
        # test so an unknown code degrades to the old message rather than to a
        # KeyError inside an error path.
        remedy = ENGINE_CODE_REMEDIES.get(code)
        category = engine_category(exc)
        refuse(f"category=engine_refused where={where} "
               f"code={code}"
               + (f" engine_category={category}" if category else "")
               + " — the engine's own fail-closed stop; its message is not "
               "reported beyond the category identifier above, because the "
               "rest can carry a path, an atom id or a fragment of the "
               "payload the engine just refused to transmit"
               + (f". What to do: {remedy}" if remedy else ""))


def assert_layout(engine_root: str) -> dict:
    """Both packages present under ONE import root, before anything is loaded.

    The addendum requires an explicit layout with a single tested import root.
    An artifact carrying only `verifier` would import, run, and produce a plan
    with no trusted lane in it; an artifact carrying only `trustedlane` would
    fail at the first engine call with the credential already read."""
    root = _resolved(engine_root)
    if not os.path.isdir(root):
        refuse("category=engine_root_not_a_directory — the path is not "
               "reported; it carries the runner layout")
    missing = [p for p in ENGINE_PACKAGES
               if not os.path.isdir(os.path.join(root, p))]
    if missing:
        refuse(f"category=engine_artifact_missing_packages packages={missing} "
               f"expected={list(ENGINE_PACKAGES)} — the engine is the candidate "
               "verifier that plans and decides PLUS the trusted lane that "
               "transports and attests; an artifact with one of them is not an "
               "engine")
    return {"engine_root": root, "packages": list(ENGINE_PACKAGES)}


#: The top-level package name the artifact's `verifier` is loaded under. The
#: suffix is derived from the artifact ROOT rather than from the artifact
#: digest, because two roots holding identical bytes are still two extractions
#: and a test that loads both must be able to tell them apart.
ENGINE_NAMESPACE_PREFIX = "trustedengine"


def engine_namespace(engine_root: str) -> str:
    """The alias this artifact's package is imported under.

    Deterministic in the root, so a second `load_engine` of the same root
    reuses the same modules instead of building a third copy of the engine in
    one process."""
    digest = hashlib.sha256(
        os.path.realpath(engine_root).encode("utf-8")).hexdigest()[:16]
    return f"{ENGINE_NAMESPACE_PREFIX}_{digest}"


def _import_aliased(root: str, alias: str, logical: str):
    """Import one engine module under the alias, or refuse naming it.

    `verifier.executor` becomes `<alias>.executor`. The engine's own imports
    are relative, so they resolve inside the alias and never reach a `verifier`
    on `sys.path` — which is the whole point: there is no `sys.path` entry to
    reach."""
    package, _, submodule = logical.partition(".")
    if package not in ENGINE_PACKAGES:
        refuse(f"category=engine_module_not_in_an_engine_package "
               f"module={logical} packages={list(ENGINE_PACKAGES)}")
    if package in sys.modules and not sys.modules[package].__name__.startswith(
            ENGINE_NAMESPACE_PREFIX):
        # Not fatal here — `assert_no_candidate_import` is the check that
        # decides whether a loaded candidate package is permitted. This only
        # guarantees the LOADER never resolves to it.
        pass
    top = f"{alias}" if package == "verifier" else f"{alias}__{package}"
    if top not in sys.modules:
        package_dir = os.path.join(root, package)
        init = os.path.join(package_dir, "__init__.py")
        if not os.path.isfile(init):
            refuse(f"category=engine_package_absent package={package} — the "
                   "approved artifact does not carry it")
        spec = importlib.util.spec_from_file_location(
            top, init, submodule_search_locations=[package_dir])
        if spec is None or spec.loader is None:
            refuse(f"category=engine_package_unloadable package={package}")
        module = importlib.util.module_from_spec(spec)
        # Registered BEFORE exec: a package whose submodules import each other
        # relatively needs its own name resolvable while its `__init__` runs.
        sys.modules[top] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 — the class, never the text
            del sys.modules[top]
            refuse(f"category=engine_package_import_failed package={package} "
                   f"exception_class={type(exc).__name__} — the message is not "
                   "reported; an import error quotes source lines")
    if not submodule:
        return sys.modules[top]
    try:
        return importlib.import_module(f"{top}.{submodule}")
    except ImportError as exc:
        refuse(f"category=engine_module_absent module={logical} "
               f"exception_class={type(exc).__name__} — the approved "
               "artifact does not carry the engine this lane was written "
               "against, and falling back to anything else would let the "
               "reviewed branch choose its own reviewer")


def load_engine(engine_root: str) -> dict:
    """Import the engine from the artifact and prove it came from there.

    Returns the loaded modules by their LOGICAL names (`verifier.executor`),
    which is what the engine's documentation calls them and what a reader is
    looking for. The actual `sys.modules` name is the alias, and both appear in
    the returned record.

    Nothing else in the trusted lane may import the engine — this is the single
    seam, so this is the single place the origin check has to hold."""
    from .enginepolicy import assert_not_candidate_checkout

    layout = assert_layout(engine_root)
    root = layout["engine_root"]
    assert_not_candidate_checkout(root)
    alias = engine_namespace(root)

    modules = {}
    for name, symbols in sorted(REQUIRED_ENGINE_SYMBOLS.items()):
        module = _import_aliased(root, alias, name)
        assert_module_loaded_from(module, root=root, name=name)
        missing = [s for s in symbols if not hasattr(module, s)]
        if missing:
            refuse(f"category=engine_module_missing_symbols module={name} "
                   f"symbols={missing} — the artifact carries a different "
                   "version of the engine than this bridge calls")
        modules[name] = module
    return {**layout, "modules": modules, "namespace": alias,
            "honest_scope": (
                "every module above was loaded from a file under the approved "
                "root AND is named under this artifact's own namespace. The "
                "second half is what makes the first non-negotiable: there is "
                "no `verifier` on sys.path for an import to fall back to")}


def unload_engine(engine_root: str) -> dict:
    """Remove every module this artifact contributed, and nothing else.

    The counterpart `load_engine` never had. Used by `EngineSession` and by the
    isolation regressions; production does not call it, because a trusted run
    is one artifact in one fresh process and unloading at the end would only be
    tidying up before exit.

    Scoped by the alias prefix, so it cannot remove a module some other loader
    put there — which is exactly what the old purge did."""
    alias = engine_namespace(engine_root)
    removed = sorted(name for name in list(sys.modules)
                     if name == alias or name.startswith(alias + ".")
                     or name.startswith(alias + "__"))
    for name in removed:
        del sys.modules[name]
    return {"namespace": alias, "removed": removed,
            "removed_count": len(removed)}


class EngineSession:
    """Load one artifact, and prove the process is unchanged afterwards.

    The mandate's alternative to a subprocess, and it is only worth anything if
    the restoration is EXACT rather than approximate — so this asserts it
    rather than performing it and hoping. On exit:

    * every module the artifact added is removed, by alias prefix;
    * `sys.path` is compared to the entry snapshot and refuses on any drift;
    * every module object that existed on entry is still the SAME object.

    That third check is the one that matters. Removing the artifact's modules
    is easy; proving nothing else moved is what a reader needs, because the
    failure this exists to prevent — the checkout's `verifier` being silently
    swapped for the artifact's — shows up as a changed object under an
    unchanged name.
    """

    def __init__(self, engine_root: str):
        self.engine_root = engine_root
        self.namespace = engine_namespace(engine_root)
        self._modules_before: dict = {}
        self._path_before: list = []
        self.engine = None

    def __enter__(self):
        self._modules_before = dict(sys.modules)
        self._path_before = list(sys.path)
        self.engine = load_engine(self.engine_root)
        return self.engine

    def __exit__(self, exc_type, exc, tb):
        # Remove what this session ADDED, then put back exactly what was there.
        # Removing the artifact's modules unconditionally would be wrong when
        # the engine was already loaded on entry: the session did not load it,
        # and unloading it is a side effect the caller did not ask for. Found
        # by `test_an_engine_session_restores_the_process_exactly`, which ran
        # after another test had already loaded the same root.
        unload_engine(self.engine_root)
        for name, module in self._modules_before.items():
            if name.startswith(self.namespace):
                sys.modules[name] = module
        if list(sys.path) != self._path_before:
            added = [p for p in sys.path if p not in self._path_before]
            removed = [p for p in self._path_before if p not in sys.path]
            refuse(f"category=engine_session_leaked_sys_path "
                   f"added={len(added)} removed={len(removed)} — the paths are "
                   "not reported; they carry the runner layout. A session that "
                   "leaves an import root behind changes what every later "
                   "import resolves to")
        changed = sorted(name for name, module in self._modules_before.items()
                         if sys.modules.get(name) is not module)
        if changed:
            refuse(f"category=engine_session_replaced_a_module "
                   f"modules={changed[:8]} count={len(changed)} — a module "
                   "that existed before the session is a different object "
                   "after it, so every class identity taken from it before is "
                   "now wrong")
        return False


def assert_module_loaded_from(module, *, root: str, name: str) -> dict:
    """A loaded module must live under the approved artifact root.

    A name check cannot answer this: `import verifier` succeeds identically
    whether the package came from the operator-approved tarball or from the
    candidate clone sitting on the same disk, and in D1 both are present. The
    path comparison is the only thing that tells them apart."""
    origin = getattr(module, "__file__", None)
    if origin is None:
        locations = list(getattr(module, "__path__", []) or [])
        if not locations:
            refuse(f"category=engine_module_origin_unknown module={name} — a "
                   "module with no file and no search path cannot be traced to "
                   "an artifact")
        outside = [p for p in locations
                   if not _resolved(p).startswith(root + os.sep)]
        if outside:
            refuse(f"category=engine_module_loaded_outside_the_artifact "
                   f"module={name} count={len(outside)} — it resolved to a "
                   "namespace root that is not inside the approved engine")
        return {"module": name, "inside_artifact": True}
    if not _resolved(origin).startswith(root + os.sep):
        refuse(f"category=engine_module_loaded_outside_the_artifact "
               f"module={name} — imported from somewhere other than the "
               "approved engine root; the path is not reported because it "
               "carries the runner layout")
    return {"module": name, "inside_artifact": True}


def build_skeleton(engine: dict, *, target_base_sha: str,
                   candidate_head_sha: str, repository_path: str,
                   budget=None) -> dict:
    """Call the REAL planner, with its real signature.

    `verifier.plan.build_skeleton(target_base_ref, head_ref, *, cwd, budget=)`.
    The previous lane imported `build_review_skeleton`, which does not exist,
    and called it with `(candidate=..., plan=..., checkout=...)`, which is not
    its signature either — so the D1 path could never have run even once.

    The skeleton is returned as the engine produced it. It is already
    self-hashed and strict-validated by `artifact.validate_strict` before
    `build_skeleton` returns, so re-validating here would be a second copy of
    the engine's own rule."""
    plan_module = engine["modules"]["verifier.plan"]
    planner = getattr(plan_module, PLANNER_FUNCTION)
    kwargs = {"cwd": repository_path}
    if budget is not None:
        kwargs["budget"] = budget
    with engine_refusals(engine, where="build_skeleton"):
        skeleton = planner(target_base_sha, candidate_head_sha, **kwargs)
    return assert_skeleton_is_usable(skeleton, engine=engine)


def assert_skeleton_is_usable(skeleton: dict, *, engine: dict) -> dict:
    """The engine reports structural blocks in DATA, not by raising.

    `build_skeleton` puts ordinary policy blocks into
    `skeleton['blocking_reasons']` and sets `structurally_clean=False` — so a
    caller that only catches exceptions treats a blocked plan as a good one.
    The engine's own docstring says the caller must check; this is the check.

    `executable` is always False and `requires_online_finalization` always True
    at this stage, so neither is asserted as a property of a good skeleton —
    they are the engine telling us Stage 2 has not happened yet."""
    if not isinstance(skeleton, dict):
        refuse("category=skeleton_not_an_object")
    blocking = skeleton.get("blocking_reasons")
    if blocking:
        codes = sorted({str(b.get("code")) for b in blocking
                        if isinstance(b, dict)})
        refuse(f"category=candidate_is_structurally_blocked codes={codes} "
               f"count={len(blocking)} — the engine reports these in data "
               "rather than by raising, so a caller that only catches "
               "exceptions would review a plan the planner refused")
    if skeleton.get("structurally_clean") is not True:
        refuse("category=skeleton_not_structurally_clean")
    models = list(skeleton.get("requested_model_ids") or [])
    expected = list(engine["modules"]["verifier.policy"].REQUESTED_MODEL_IDS)
    if models != expected:
        refuse(f"category=skeleton_model_panel_mismatch observed={models} "
               f"expected={expected} — the panel is governed policy, and a "
               "skeleton naming a different one is not the review that was "
               "approved")
    return skeleton


def prepare_review_plan_core(engine: dict, *, skeleton: dict,
                             repository_path: str, pin_record: dict,
                             transport, authorizations, challenge: str,
                             minimum_other_approvers: int = 1) -> dict:
    """Call the ENGINE's shared finalization core. The whole of Slice 2.

    Everything this returns — final units, the global preflight manifest, all
    three model counts, the batches, the exact request hashes, the coverage
    proof and the cost gates — was computed by `scripts/verifier`, which is the
    package that actually implements them. The lane supplies four things the
    engine cannot: an authenticated PIN record, a verified literal
    authorization set, a real transport, and an unpredictable challenge.

    `required_approver` is deliberately NOT a parameter. It is governed policy
    and comes from the engine's own `reviewpolicy`; letting a caller pass it
    would make "which model must approve" a call-site decision."""
    if not isinstance(challenge, str) or len(challenge) < 16:
        refuse("category=trusted_challenge_too_short — the challenge is what "
               "proves a verdict was written for this run; a short or absent "
               "one is guessable, and a guessable challenge proves nothing")
    finalize = engine["modules"]["verifier.finalize"]
    with engine_refusals(engine, where="prepare_review_plan_core"):
        core = finalize.prepare_review_plan_core(
            skeleton, cwd=repository_path, pin_record=pin_record,
            transport=transport, authorizations=authorizations,
            challenge=challenge,
            required_approver=required_approver(engine),
            minimum_other_approvers=minimum_other_approvers)
    return assert_core_is_evidence_neutral(core, engine=engine)


def assert_core_is_evidence_neutral(core, *, engine: dict) -> dict:
    """The core computed review semantics; it must not have decided evidence.

    Checked on the RESULT rather than trusted from the docstring. If a future
    edit to the candidate package made the core emit `executable` or an
    evidence class, this lane would be signing a trust conclusion drawn inside
    the artifact under review — which is the whole defect class."""
    if not isinstance(core, dict):
        refuse("category=engine_core_result_not_an_object")
    expected = engine["modules"]["verifier.finalize"].PREPARE_CORE_VERSION
    if core.get("core_version") != expected:
        refuse(f"category=engine_core_version_mismatch "
               f"observed={core.get('core_version')!r} expected={expected!r} — "
               "the artifact carries a different core than this bridge was "
               "written against, and reading its fields by name would be "
               "guessing")
    present = [f for f in FORBIDDEN_CORE_FIELDS if f in core]
    if present:
        refuse(f"category=engine_core_decided_evidence fields={present} — the "
               "shared core must compute review semantics and decide nothing "
               "about trust; a core that labels evidence would be candidate "
               "code drawing the conclusion this lane exists to draw")
    if core.get("generation_calls_performed") != 0:
        refuse(f"category=engine_core_made_a_generation_call "
               f"calls={core.get('generation_calls_performed')} — counting "
               "and generating are separately approved, and D1 holds only the "
               "count approval")
    return core


def governed_policy_digests(engine: dict, *, pin_values: dict,
                            minimum_other_approvers: int = 1) -> dict:
    """The two governed policy records, and their digests, BEFORE any call.

    EX5-R21 compares the operator's approved capability-policy and
    ReviewRequestPolicy digests against the ones the engine actually loads. That
    comparison has to happen before a credential is spent, and the shared core
    computes both internally — so they are computed here first, from the same
    engine, and `assert_core_used_the_governed_policies` proves afterwards that
    the core used these and not something else.

    Both are pure functions of the panel and the PIN values. Recomputing them is
    not a second implementation: it is the same engine function called
    twice."""
    model_ids = list(model_panel(engine))
    capabilities = engine["modules"]["verifier.capabilities"]
    reviewpolicy = engine["modules"]["verifier.reviewpolicy"]
    # Wrapped, like every other engine call. Both refuse a model outside the
    # capability table and a max-output above what the model supports — a PIN
    # the operator set too high reaches here, and unwrapped it would escape as
    # a crash rather than as the lane's own refusal. Found by the test that
    # asks which engine calls are NOT inside `engine_refusals`, after that test
    # stopped comparing against a hand-written list.
    with engine_refusals(engine, where="governed_policy_digests"):
        capability_policy = capabilities.policy_record(model_ids)
        review_request_policy = reviewpolicy.policy_record(
            model_ids, required_approver=required_approver(engine),
            minimum_other_approvers=minimum_other_approvers,
            max_output_tokens=pin_values["VERIFIER_MAX_OUTPUT_TOKENS"])
    return {
        "capability_policy": capability_policy,
        "review_request_policy": review_request_policy,
        "capability_policy_sha256":
            capability_policy["capability_policy_sha256"],
        "review_request_policy_sha256":
            review_request_policy["review_request_policy_sha256"],
    }


def assert_core_used_the_governed_policies(core: dict, *, governed: dict
                                           ) -> dict:
    """The core must have used the policies the operator's approval was checked
    against.

    Without this the gate is decorative: it would compare an operator approval
    to a policy record computed for the comparison and then discarded, while
    the core built requests under whatever it computed for itself."""
    for field in ("capability_policy_sha256", "review_request_policy_sha256"):
        source = ("capability_policy" if field.startswith("capability")
                  else "review_request_policy")
        observed = (core.get(source) or {}).get(field)
        if observed != governed[field]:
            refuse(f"category=engine_core_used_a_different_governed_policy "
                   f"field={field} — the operator's approval was checked "
                   "against one policy record and the requests were built "
                   "under another")
    return {"governed_policies_match": True,
            "capability_policy_sha256": governed["capability_policy_sha256"],
            "review_request_policy_sha256":
                governed["review_request_policy_sha256"]}


def execute_review_plan(engine: dict, *, skeleton: dict, plan: dict,
                        repository_path: str, transport, authorizations,
                        challenge: str) -> dict:
    """Stage 3, in the ENGINE. The protected counterpart of the Slice-2 bridge.

    Everything the mandate lists for D2 is already implemented in
    `verifier.executor`, correctly and with the properties the lane needs:

    * `rebuild_payloads` earns the prompt bytes again from the commits, which
      also re-proves that every unit's atoms still exist in the claimed range —
      the plan stores request HASHES, not bodies, deliberately, so that it
      carries no source content;
    * `assert_request_matches_plan` requires HASH equality, not token equality.
      Usage drift is arithmetic and two different review questions can cost the
      same; only the hash says the reviewer is being asked what was priced;
    * `ExecutionPreflightManifest.scan_all` scans EVERY execution payload
      before any generation call, so a secret in the last batch blocks the
      first batch's call rather than being discovered after it;
    * `execute_batch` runs the whole panel, validates each response envelope,
      validates the verdicts, applies the anti-copy tripwire and decides each
      unit;
    * `synthesize` is additive-only: a unit any panelist refuted stays refuted.

    The lane supplies the four things the engine cannot have: the verified
    plan, a real transport, a verified literal set, and the challenge D1
    counted. It reimplements none of the above, which is the rule this bridge
    exists to keep.
    """
    executor = engine["modules"]["verifier.executor"]
    if plan["execution_challenge"] != challenge:
        refuse("category=execution_challenge_is_not_the_counted_one — the "
               "challenge is in the request bytes and the request bytes are "
               "what was counted, so sending a different one executes requests "
               "whose cost nobody approved")
    if skeleton["review_skeleton_sha256"] != plan["review_skeleton_sha256"]:
        refuse("category=rebuilt_skeleton_is_not_the_counted_one — D2 rebuilt "
               "the range and got a different plan than D1 counted; the "
               "candidate moved, or the plan is for another range")

    review_policy = plan["review_request_policy"]
    pin_values = plan["operator_pin_record"]["pins"]
    with engine_refusals(engine, where="rebuild_payloads"):
        payloads = executor.rebuild_payloads(skeleton, plan,
                                             cwd=repository_path)
        paths = executor._path_identities(skeleton, plan["final_units"])

    # 1. Reconstruct EVERY request first, and hash-check each against the plan.
    assemblies_by_batch = {}
    with engine_refusals(engine, where="reconstruct_batch_requests"):
        for batch in plan["batches"]:
            assemblies = executor.reconstruct_batch_requests(
                plan, batch, payloads_by_unit=payloads,
                path_bytes_b64_by_unit=paths)
            for model_id, assembly in assemblies.items():
                executor.assert_request_matches_plan(assembly, batch, model_id)
            assemblies_by_batch[batch["batch_id"]] = assemblies

    # 2. Scan them ALL before any is sent.
    with engine_refusals(engine, where="execution_preflight"):
        # Inside the guard: the ledger validates the PIN values it is built
        # from, so a retry or timeout PIN the operator set out of range refuses
        # HERE, before any request is scanned and long before one is sent.
        ledger = executor.GenerationLedger(pin_values)
        manifest = executor.ExecutionPreflightManifest(
            skeleton, plan, cwd=repository_path,
            authorizations=authorizations)
        manifest.scan_all(assemblies_by_batch, ledger)

    # 3. Only now does anything reach a socket.
    results = []
    with engine_refusals(engine, where="execute_batch"):
        for batch in plan["batches"]:
            counted = {model_id: (batch.get("input_tokens_by_model") or {}).get(
                           model_id)
                       for model_id in review_policy["model_ids"]}
            results.append(executor.execute_batch(
                batch, review_policy, transport=transport,
                pin_values=pin_values, challenge=challenge,
                counted_by_model=counted,
                assemblies=assemblies_by_batch[batch["batch_id"]],
                ledger=ledger))

    # 4. Refutations survive synthesis, and provider text is scanned before it
    #    is persisted — a secret that leaves in a request and comes back in a
    #    verdict has still left twice.
    with engine_refusals(engine, where="synthesize"):
        synthesis = executor.synthesize(results)
    # `per_model_verdict_evidence`, by its exact name and with no default. The
    # first version of this line read `result.get("evidence_records", [])` — a
    # key the engine has never emitted — so the scan received an empty list,
    # scanned nothing, and returned a record saying so. Every test asserting
    # "the output was scanned" passed. A `.get` with a default turns "the
    # engine renamed this" into "there was nothing to scan", which is the
    # answer a caller wants and not the one that is true.
    evidence_records = []
    for result in results:
        if EVIDENCE_RECORDS_KEY not in result:
            refuse(f"category=engine_batch_result_carries_no_verdict_evidence "
                   f"key={EVIDENCE_RECORDS_KEY} present={sorted(result)} — the "
                   "privacy scan reads the provider's own text out of this "
                   "key; absent, it would scan nothing and report success")
        evidence_records.extend(result[EVIDENCE_RECORDS_KEY])
    with engine_refusals(engine, where="output_privacy"):
        privacy = executor.assert_output_carries_no_secret(
            evidence_records, path_identities=frozenset(paths.values()))
    # A scan that scanned nothing is not a scan. Every verdict carries at least
    # `reason` and `proof_of_check`, so zero here means the records were empty
    # or the field set moved — either way the run must not report that the
    # provider's output was checked.
    if privacy["scanned_field_count"] < 1:
        refuse(f"category=output_privacy_scanned_nothing "
               f"evidence_records={len(evidence_records)} — the scan reported "
               "success over an empty field set; a check nothing passed "
               "through is not a check")

    return {
        "batch_results": results,
        "synthesis": synthesis,
        "execution_preflight": manifest.record(),
        "generation_ledger": ledger.record(),
        "output_privacy": privacy,
        "requested_model_ids": list(review_policy["model_ids"]),
        "required_approver": review_policy["required_approver"],
        # From the policy the requests were COUNTED under, not from a lane
        # constant. `d2runtime.MINIMUM_OTHER_APPROVERS = 1` used to sit beside
        # the engine's own value; two numbers for one rule is how the two
        # copies end up disagreeing, and the lane's copy is the one no
        # governance change would reach.
        "minimum_other_approvers": review_policy["minimum_other_approvers"],
    }


#: The five digests an operator approves under `approve_engine_identity`, and
#: therefore the five a run must be able to state about the engine it loaded.
ENGINE_IDENTITY_FIELDS = ("engine_artifact_sha256", "engine_source_sha256",
                          "runtime_lock_sha256", "sbom_sha256",
                          "provenance_sha256")


def assert_identity_is_trusted_grade(record) -> dict:
    """The trusted lane accepts protected identities only.

    `MIDTERM_OPERATOR_APPROVED_ENGINE_IDENTITY` is a real, deterministic,
    operator-approved record — and it is backed by a human agreeing to look at
    an exact SHA, not by the platform refusing unreviewed writes. That is
    enough for `MIDTERM_SINGLE_REPO_*` evidence and it is not enough for
    `TRUSTED_*`. Refusing it here, by state, is what keeps "we accepted a
    weaker control once, for the panel" from becoming the trusted lane's
    standard by drift.

    Checked separately from the five digests: those say WHICH engine, this
    says whether this lane may use that kind of engine at all."""
    from .enginebuild import (
        CONTROL_NATIVE_PROTECTED_REF,
        MIDTERM_STATE,
        PROTECTED_STATE,
        assert_ref_protected,
    )

    if not isinstance(record, dict):
        refuse("category=engine_identity_not_supplied")
    state = record.get("state")
    if state == MIDTERM_STATE:
        refuse(f"category=trusted_lane_refuses_midterm_engine_identity "
               f"state={state!r} — this artifact was built from an "
               "unprotected ref under a human exact-head compensating "
               "control. It may back MIDTERM_SINGLE_REPO_* evidence; the "
               "trusted lane requires native branch protection and will not "
               "accept a weaker control by inheritance")
    if state != PROTECTED_STATE:
        refuse(f"category=trusted_lane_engine_identity_unknown_state "
               f"state={state!r} expected={PROTECTED_STATE}")
    if not assert_ref_protected(record.get("build_ref_protected")):
        refuse("category=trusted_lane_engine_identity_not_protected — the "
               "record claims the protected state while the platform said "
               "the ref was unprotected")
    if record.get("control_class") != CONTROL_NATIVE_PROTECTED_REF:
        refuse(f"category=trusted_lane_engine_identity_wrong_control "
               f"control_class={record.get('control_class')!r} "
               f"expected={CONTROL_NATIVE_PROTECTED_REF}")
    return {"state": PROTECTED_STATE, "native_branch_protection": True,
            "control_class": CONTROL_NATIVE_PROTECTED_REF,
            "honest_scope": ("the build reported the platform's "
                             "ref-protection fact and it was true; this does "
                             "not by itself satisfy the lane's other "
                             "operator prerequisites")}


def assert_identity_is_this_engine(engine_identity, *,
                                   engine_artifact: dict) -> dict:
    """The identity record must describe the artifact this run VERIFIED.

    The missing link in EX5-R21's chain, and it was a real gap.
    `runtimebinding` compares the operator's five approved digests against the
    identity record; `artifactload.inspect_archive` verifies the artifact
    against `engine_artifact["expected_sha256"]`. Both passed while nothing
    required the two artifact digests to be the same number — so an identity
    record naming the approved engine could sit beside a run that loaded a
    different one, and each half looked right to whichever check read it.

    Deliberately here rather than inside `runtimebinding`: that module compares
    authorizations to runtime facts and must never go looking for facts itself.
    This is a fact about the runtime, established where both values are in
    scope, and passed in."""
    if not isinstance(engine_identity, dict):
        refuse("category=engine_identity_not_supplied")
    missing = [f for f in ENGINE_IDENTITY_FIELDS if not engine_identity.get(f)]
    if missing:
        refuse(f"category=engine_identity_incomplete fields={missing} — five "
               "digests are approved; a run that can only state four is asking "
               "the operator to approve something it cannot check")
    verified = engine_artifact.get("expected_sha256")
    if engine_identity["engine_artifact_sha256"] != verified:
        refuse("category=engine_identity_is_for_a_different_artifact — the "
               "identity record names one artifact and this run verified "
               "another; the operator's approval would then be compared "
               "against a document, not against what loaded")
    return {"engine_artifact_sha256": verified,
            "identity_fields": list(ENGINE_IDENTITY_FIELDS),
            "honest_scope": (
                "the artifact digest was verified by this run against the "
                "archive on disk. The other four are claims made by the "
                "protected build that produced them; this only proves the "
                "record is about the artifact that loaded")}


def model_panel(engine: dict) -> tuple:
    """The governed three-model panel, from the engine — never a local copy."""
    return tuple(engine["modules"]["verifier.policy"].REQUESTED_MODEL_IDS)


def required_approver(engine: dict) -> str:
    """`gpt-5.6-sol`, from the engine's own review policy."""
    return engine["modules"]["verifier.reviewpolicy"].GOVERNED_REQUIRED_APPROVER


def pin_names(engine: dict) -> tuple:
    """The twelve PIN names, in order, from the engine."""
    return tuple(engine["modules"]["verifier.policy"].POLICY_PIN_NAMES)


def secret_scanner(engine: dict):
    """The engine's OWN `scan_text`, as a one-argument callable.

    Exposed for the readable-review renderer, which has to scan every field it
    is about to publish. This is emphatically NOT the second scanner this
    module's docstring forbids — it is the first one, handed out. A renderer
    that imported a scanner for itself would be a second implementation of the
    one check whose disagreement nobody would notice until a credential was in
    a comment, and it would not be the operator-approved artifact's copy.

    The narrowed signature is deliberate. `scan_text` also takes `allowlist` and
    `cleared_hashes` — the scoped clearances that let a REVIEWED source literal
    through on its way to a provider. Nothing published to a pull request has
    ever been reviewed for that, so the publisher gets the version with no way
    to clear anything."""
    scan_text = engine["modules"]["verifier.preflight"].scan_text
    return lambda text: scan_text(text)
