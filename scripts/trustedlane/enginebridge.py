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

**Why the import is deferred and path-checked.** `verifier` is candidate code
in D0 — any import of it there means the artifact under review reached the
process — and it is TRUSTED code in D1/D2, where it arrives inside the
operator-approved engine artifact. The same module name means opposite things
depending on where it was loaded from, so the check cannot be a name check once
the artifact is unpacked. `load_engine` resolves the import root, imports, and
then proves every loaded module's file lives under that root.

**What this module must never grow.** A second scanner, origin map, PIN schema,
capability table, review policy, request builder, response schema, batcher,
splitter, executor, verdict aggregator or strict plan loader. When something
here looks like it needs one, it needs to call the engine instead. The only
code that belongs in this file is the call, the argument marshalling, and the
refusals that protect the boundary.
"""

from __future__ import annotations

import os

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
                           "promote_literal_authorizations"),
    "verifier.pins": ("validate_pins", "promote_pin_authorization"),
    "verifier.reviewpolicy": ("GOVERNED_REQUIRED_APPROVER", "policy_record",
                              "LENS_IDS"),
    "verifier.providerreq": ("assemble_request", "ProviderRequest"),
    "verifier.preflight": ("scan_text", "preflight_request"),
    "verifier.origin": ("OriginMap", "resolve_finding"),
    "verifier.executor": ("execute_batch", "validate_response_envelope"),
    "verifier.verdicts": ("validate_verdicts", "assert_distinct_reasoning"),
}


def _resolved(path: str) -> str:
    return os.path.realpath(path)


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


def load_engine(engine_root: str) -> dict:
    """Import the engine from the artifact and prove it came from there.

    Returns the loaded modules by name. Nothing else in the trusted lane may
    import `verifier` — this is the single seam, so this is the single place
    the origin check has to hold."""
    import importlib
    import sys

    from .enginepolicy import assert_not_candidate_checkout

    layout = assert_layout(engine_root)
    root = layout["engine_root"]
    assert_not_candidate_checkout(root)

    if root not in sys.path:
        sys.path.insert(0, root)

    modules = {}
    for name, symbols in sorted(REQUIRED_ENGINE_SYMBOLS.items()):
        try:
            module = importlib.import_module(name)
        except ImportError as exc:
            refuse(f"category=engine_module_absent module={name} "
                   f"exception_class={type(exc).__name__} — the approved "
                   "artifact does not carry the engine this lane was written "
                   "against, and falling back to anything else would let the "
                   "reviewed branch choose its own reviewer")
        assert_module_loaded_from(module, root=root, name=name)
        missing = [s for s in symbols if not hasattr(module, s)]
        if missing:
            refuse(f"category=engine_module_missing_symbols module={name} "
                   f"symbols={missing} — the artifact carries a different "
                   "version of the engine than this bridge calls")
        modules[name] = module
    return {**layout, "modules": modules}


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


def model_panel(engine: dict) -> tuple:
    """The governed three-model panel, from the engine — never a local copy."""
    return tuple(engine["modules"]["verifier.policy"].REQUESTED_MODEL_IDS)


def required_approver(engine: dict) -> str:
    """`gpt-5.6-sol`, from the engine's own review policy."""
    return engine["modules"]["verifier.reviewpolicy"].GOVERNED_REQUIRED_APPROVER


def pin_names(engine: dict) -> tuple:
    """The twelve PIN names, in order, from the engine."""
    return tuple(engine["modules"]["verifier.policy"].POLICY_PIN_NAMES)
