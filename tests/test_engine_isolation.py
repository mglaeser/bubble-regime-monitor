"""EX6-R02 — the engine artifact and the in-tree package, in one interpreter.

This module exists because a green suite is not evidence of isolation. The
hosted failure that produced this file looked like one wrong `pytest.raises`
target:

    tests/test_verifier_mc4_passc.py::TestPassEAuthorizationScopeIsBound::
      test_a_swapped_scope_cannot_be_assembled_at_all

The engine blocked correctly — `SECRET_PREFLIGHT_FAILED` /
`category=span_path_scope_mismatch` — and the test did not see it, because the
exception's class and the test's class were two different objects that print
the same name. Fixing the assertion would have made the symptom go away and
left the cause, which is that ONE PYTHON PROCESS HELD TWO PACKAGES CALLED
`verifier`.

**What produced the collision.** `enginebridge.load_engine` inserted the
artifact root at `sys.path[0]` and imported `verifier` from it. `scripts/` is
also on `sys.path` on this branch — it must be, the candidate package is what
is under review — so `verifier` resolved to whichever entry came first, and
which one that was depended on import order.

**What removed it.** The engine's modules import each other relatively and
never absolutely (`test_the_engine_uses_only_relative_imports` proves it over
the artifact's own AST), so the package is relocatable. `load_engine` now uses
`importlib.util.spec_from_file_location` to load it as
`trustedengine_<16 hex of the root path>`, and touches `sys.path` not at all.

So the invariants below are the ones worth holding, and each is written as the
attack rather than as a restatement of the fix:

* nothing named `verifier` is ever contributed by the bridge;
* `sys.path` is byte-identical before and after;
* every module object that existed before a load is the SAME object after;
* two artifacts can be loaded in one process without either becoming the other;
* the in-tree package's class identities survive an engine load, which is the
  exact property whose absence produced the hosted failure;
* and a real runner — a genuinely clean subprocess — has no candidate package
  at all, which is the only claim here that does not depend on a model.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from trustedlane import enginebridge, enginepolicy  # noqa: E402
from trustedlane.errors import LaneRefusal  # noqa: E402

# The lane suite already builds one real artifact per session and caching it
# again here would double a slow build. Importing the helper is deliberate: it
# is the same artifact the rest of the evidence is about.
sys.path.insert(0, str(ROOT / "tests"))
from test_trusted_lane_bootstrap import (  # noqa: E402
    _ENGINE_CACHE,
    _engine,
)


def _artifact_root() -> str:
    _engine()
    return _ENGINE_CACHE["artifact"]["root"]


def _verifier_modules(modules=None) -> list:
    modules = sys.modules if modules is None else modules
    return sorted(n for n in modules
                  if n == "verifier" or n.startswith("verifier."))


def _module_file(name: str):
    module = sys.modules.get(name)
    if module is None:
        return None
    return getattr(module, "__file__", None)


# ------------------------------------------------------- the precondition ---


def test_the_engine_uses_only_relative_imports():
    """The property that makes aliasing possible, checked over the ARTIFACT.

    An absolute `from verifier.canon import ...` inside the engine would
    resolve to whatever `verifier` is on `sys.path` — the checkout — the moment
    the artifact were loaded under a different name. Then the artifact's
    modules would silently call the candidate's, which is worse than the
    collision this replaced.

    Read over the AST of the artifact's own files, not the checkout's: the
    checkout is not what gets loaded."""
    package = Path(_artifact_root()) / "verifier"
    sources = sorted(package.glob("*.py"))
    assert len(sources) >= 15, "the probe found almost no engine source"
    offenders = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level == 0 and (node.module or "").split(".")[0] == "verifier":
                    offenders.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "verifier":
                        offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], (
        f"the engine imports itself absolutely at {offenders}; under aliasing "
        "that would resolve to the CANDIDATE package on sys.path, so the "
        "artifact would be calling the code it is supposed to be reviewing")


# --------------------------------------------------- the bridge's promises ---


def test_the_bridge_contributes_no_module_named_verifier():
    """The invariant, stated as the thing a reader can check in one line."""
    root = _artifact_root()
    alias = enginebridge.engine_namespace(root)
    engine = enginebridge.load_engine(root)
    aliased = sorted(n for n in sys.modules
                     if n == alias or n.startswith(alias + "."))
    assert len(aliased) >= 10, "the engine did not load under its namespace"
    for name, module in engine["modules"].items():
        assert name.startswith("verifier."), "the logical key is the engine's own name"
        assert module.__name__.startswith(alias), (
            f"{name} is registered as {module.__name__}, not under the alias")
        assert os.path.realpath(module.__file__).startswith(
            os.path.realpath(root)), f"{name} did not come from the artifact"


def test_loading_the_engine_does_not_touch_sys_path():
    """It used to insert the artifact root at position 0 and never remove it.

    That single line is what let a later `import verifier` resolve to the
    artifact — and, once a purge had run, what gave a previously-imported test
    module a second, different `BlockingError`."""
    before = list(sys.path)
    enginebridge.load_engine(_artifact_root())
    assert sys.path == before, (
        "load_engine changed sys.path; every import in the process after this "
        "point may resolve somewhere else")


def test_loading_the_engine_replaces_no_existing_module():
    """The check that actually matters.

    Removing the artifact's modules afterwards is easy. Proving nothing ELSE
    moved is what a reader needs, because the failure this prevents shows up as
    a changed object under an unchanged name."""
    before = dict(sys.modules)
    enginebridge.load_engine(_artifact_root())
    changed = sorted(n for n, m in before.items() if sys.modules.get(n) is not m)
    assert changed == [], f"these modules are different objects now: {changed}"


def test_the_in_tree_verifier_survives_an_engine_load_with_its_classes():
    """THE HOSTED FAILURE, as a property rather than as one assertion.

    A class captured before the engine is loaded must still be the class the
    checkout's code raises afterwards. This is exactly what stopped being true
    and exactly what `pytest.raises` depends on."""
    import verifier.errors
    import verifier.origin

    before_error = verifier.errors.BlockingError
    before_origin_file = verifier.origin.__file__

    enginebridge.load_engine(_artifact_root())

    assert verifier.errors.BlockingError is before_error, (
        "the in-tree BlockingError is a different class after an engine load; "
        "every pytest.raises holding the old one now silently misses")
    assert verifier.origin.__file__ == before_origin_file
    assert os.path.realpath(before_origin_file).startswith(
        os.path.realpath(str(ROOT / "scripts" / "verifier")))
    # And the two classes are genuinely distinct — if they were the same object
    # the assertion above would be vacuous.
    engine = enginebridge.load_engine(_artifact_root())
    assert engine["modules"]["verifier.errors"].BlockingError is not before_error


def test_the_artifact_error_class_is_the_one_the_engine_raises():
    """When a test drives artifact code directly it must catch the ARTIFACT's
    type. The mandate's required immediate fix, as a standing property."""
    engine = enginebridge.load_engine(_artifact_root())
    artifact_error = engine["modules"]["verifier.errors"].BlockingError
    counting = engine["modules"]["verifier.counting"]
    assert hasattr(counting, "COUNT_PATH")
    import verifier.errors as in_tree
    assert artifact_error is not in_tree.BlockingError
    assert artifact_error.__module__.startswith(
        enginebridge.ENGINE_NAMESPACE_PREFIX), (
        "the artifact's error class does not name its own namespace, so a "
        "reader looking at a traceback cannot tell which package raised")


# ------------------------------------------------------------- unloading ----


def test_unload_removes_exactly_the_artifacts_modules():
    root = _artifact_root()
    alias = enginebridge.engine_namespace(root)
    enginebridge.load_engine(root)
    unrelated = dict(sys.modules)
    record = enginebridge.unload_engine(root)
    assert record["removed_count"] >= 10
    assert all(n.startswith(alias) for n in record["removed"])
    survivors = {n: m for n, m in unrelated.items() if not n.startswith(alias)}
    changed = sorted(n for n, m in survivors.items() if sys.modules.get(n) is not m)
    assert changed == [], f"unload removed or replaced {changed}"
    enginebridge.load_engine(root)          # leave the session as we found it


def test_an_engine_session_restores_the_process_exactly():
    root = _artifact_root()
    before_modules, before_path = dict(sys.modules), list(sys.path)
    with enginebridge.EngineSession(root) as engine:
        assert engine["modules"]["verifier.plan"].build_skeleton
    assert sys.path == before_path
    changed = sorted(n for n, m in before_modules.items()
                     if sys.modules.get(n) is not m)
    assert changed == [], f"the session did not restore {changed}"
    # And a session entered on a CLEAN process leaves no artifact module behind
    # — the other half of "exactly", which the first assertion cannot see
    # because this process already had the engine loaded.
    alias = enginebridge.engine_namespace(root)
    enginebridge.unload_engine(root)
    with enginebridge.EngineSession(root):
        assert any(n.startswith(alias) for n in sys.modules)
    assert [n for n in sys.modules if n.startswith(alias)] == []
    enginebridge.load_engine(root)          # the cached engine is reloadable


def test_two_artifact_roots_do_not_become_one_engine(tmp_path):
    """Loading artifact A then B must give two engines, not one shadowing the
    other. Under the old scheme the second `import verifier` was a no-op — the
    first artifact was already in `sys.modules` under that name — so B's
    modules were silently A's."""
    from trustedlane import artifactload

    second = tmp_path / "engine"
    second.mkdir()
    artifactload.extract(_ENGINE_CACHE["artifact"]["path"],
                         destination=str(second),
                         expected_sha256=_ENGINE_CACHE["artifact"][
                             "expected_sha256"])
    first = enginebridge.load_engine(_artifact_root())
    other = enginebridge.load_engine(str(second))
    assert first["namespace"] != other["namespace"]
    a = first["modules"]["verifier.errors"].BlockingError
    b = other["modules"]["verifier.errors"].BlockingError
    assert a is not b, (
        "the second artifact resolved to the first one's modules; identical "
        "bytes are still two extractions and a test that loads both must be "
        "able to tell them apart")
    assert os.path.realpath(
        other["modules"]["verifier.plan"].__file__).startswith(
        os.path.realpath(str(second)))
    enginebridge.unload_engine(str(second))


# ------------------------------------------------- the unmodelled process ---


def test_the_real_process_view_refuses_here():
    """The fact the lane's test fixtures work around, asserted directly.

    `_clean_runner_modules` hands the runtime a modelled view in which the
    checkout's own package is excluded. That is legitimate only if the
    UNMODELLED view is honestly reported as failing — otherwise the model is a
    way of passing by pretending."""
    assert _verifier_modules(), (
        "the candidate package is not loaded in this process, so this test is "
        "not checking what it claims to")
    with pytest.raises(LaneRefusal) as caught:
        enginepolicy.assert_no_candidate_import()
    assert "candidate_module_imported" in caught.value.reason


def test_the_runtime_checks_the_real_process_by_default():
    """A real runner supplies no view, so the runtime must read the real
    process. Driven over the AST rather than by running D1, because the point
    is what the call site passes when the artifact record is silent."""
    import inspect as _inspect

    from trustedlane import d1runtime, d2runtime

    for module in (d1runtime, d2runtime):
        tree = ast.parse(_inspect.getsource(module))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", None) == "assert_no_candidate_import"]
        assert len(calls) == 1, f"{module.__name__} calls it {len(calls)} times"
        kwargs = {k.arg for k in calls[0].keywords}
        assert kwargs == {"modules", "search_path"}, kwargs
        # Both come off `.get(...)`, which is None when the record is silent.
        for keyword in calls[0].keywords:
            assert getattr(keyword.value.func, "attr", None) == "get", (
                f"{module.__name__} passes {keyword.arg} as something other "
                "than an optional artifact field; a real runner supplies "
                "neither and must get the real process")


def test_no_lane_test_purges_sys_modules():
    """The Exchange-6 fixture must not come back.

    It was added to make the lane tests pass and it caused EX6-R02: purging
    `verifier*` while the artifact root was on `sys.path` gave every later
    import a fresh class. Aliasing removes the need; this removes the
    temptation."""
    source = (ROOT / "tests" / "test_trusted_lane_bootstrap.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Delete):
            continue
        for target in node.targets:
            if isinstance(target, ast.Subscript) and getattr(
                    target.value, "attr", None) == "modules":
                raise AssertionError(
                    "a lane test deletes from sys.modules again; that is what "
                    "gave the verifier tests a second BlockingError")


# --------------------------------------------- the only unmodelled evidence --

_SUBPROCESS_PROBE = textwrap.dedent("""
    import json, os, sys
    sys.path.insert(0, {scripts!r})
    from trustedlane import enginebridge, enginepolicy
    before = list(sys.path)
    engine = enginebridge.load_engine({root!r})
    containment = enginepolicy.assert_no_candidate_import()
    print(json.dumps({{
        "verifier_modules": sorted(n for n in sys.modules
                                   if n == "verifier" or n.startswith("verifier.")),
        "namespace": engine["namespace"],
        "sys_path_unchanged": list(sys.path) == before,
        "containment": containment["candidate_isolated"],
        "planner": engine["modules"]["verifier.plan"].__file__,
        "error_class_module":
            engine["modules"]["verifier.errors"].BlockingError.__module__,
    }}))
""")


def test_a_subprocess_runner_has_no_candidate_package(tmp_path):
    """The mandate's preferred shape, and the only evidence here that does not
    depend on a model.

    A trusted runner is a fresh process that checks out the protected ref and
    unpacks one artifact. This builds that process: a clean interpreter whose
    `sys.path` carries the lane but NOT `scripts/verifier`, loading the real
    artifact. `assert_no_candidate_import` runs unmodelled and passes, which is
    the claim the in-process fixtures can only approximate.

    `scripts/` cannot be on the path — that is what makes the candidate
    reachable — so the lane package is staged on its own.
    """
    stage = tmp_path / "runner"
    stage.mkdir()
    lane_src = ROOT / "scripts" / "trustedlane"
    lane_dst = stage / "trustedlane"
    lane_dst.mkdir()
    for path in lane_src.rglob("*"):
        target = lane_dst / path.relative_to(lane_src)
        if path.is_dir():
            target.mkdir(exist_ok=True)
        else:
            target.write_bytes(path.read_bytes())
    assert not (stage / "verifier").exists()

    probe = _SUBPROCESS_PROBE.format(scripts=str(stage), root=_artifact_root())
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no input
        [sys.executable, "-c", probe], capture_output=True, text=True,
        cwd=str(tmp_path), timeout=180,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
             "PYTHONPATH": ""})
    assert result.returncode == 0, result.stderr[-2000:]
    record = json.loads(result.stdout.strip().splitlines()[-1])

    assert record["verifier_modules"] == [], (
        "a clean runner loaded modules named `verifier`; the artifact is "
        "supposed to be unreachable under that name")
    assert record["namespace"].startswith(enginebridge.ENGINE_NAMESPACE_PREFIX)
    assert record["sys_path_unchanged"] is True
    assert record["containment"] is True
    assert record["planner"].startswith(os.path.realpath(_artifact_root()))
    assert record["error_class_module"].startswith(
        enginebridge.ENGINE_NAMESPACE_PREFIX)
