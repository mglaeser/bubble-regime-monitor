"""Prove D0 containment in an ISOLATED import root, not in the checkout.

## Why this exists

The D0 workflow used to do this:

    sys.path.insert(0, "scripts")
    enginepolicy.assert_no_candidate_import()

which asks a process that has just made `verifier` importable whether
`verifier` is importable. While `scripts/verifier` did not exist on `main` the
answer was accidentally "no" and the step passed. The moment the candidate
package legitimately landed on `main`, `assert_no_candidate_import` did exactly
what it is for and refused with

    category=candidate_package_reachable paths=['scripts/verifier']

That refusal is correct and must stay correct. What was wrong is the shape of
the process the proof ran in: proving that a checkout contains no
`scripts/verifier` is not the property the trusted lane needs. The property is
that the CREDENTIAL-BEARING RUNTIME cannot reach the candidate package — and a
repository that contains the candidate is the normal case, not the failure.

So the proof now runs in a child process whose import root contains ONLY the
trusted lane, copied into a private temporary directory. The repository
`scripts/` parent is never on that process's `sys.path`, the interpreter is
started with `-P` so the working directory is not prepended, and its working
directory is a neutral empty temp dir so that even a leaked `cwd` could not
resolve `verifier`.

## What is deliberately NOT done here

The ratchet is not weakened. `CANDIDATE_PATH_MARKERS` is not shortened, the
`scripts/verifier` marker is not removed, and there is no "main is trusted"
exception. A credential-bearing process in which the candidate package is
reachable as `verifier` must still refuse, on `main` as anywhere else — and a
test asserts that putting `scripts/` back on the child's path turns this red
again.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

from .errors import refuse

#: The one package the isolated proof root may contain. Copied by name rather
#: than by "everything except the candidate", because an exclusion list silently
#: admits whatever is added next.
TRUSTED_PACKAGE = "trustedlane"

#: The namespace the approved engine is imported under. Loading the engine is
#: NOT a candidate import — the whole point of `enginebridge` is that the
#: artifact never enters `sys.modules` as `verifier`.
ENGINE_NAMESPACE_PREFIX = "trustedengine_"

#: The probe the child runs. Kept as one literal so the workflow, the helper
#: and the test all execute the SAME proof; a workflow that inlined its own
#: copy is how the old shape drifted from what the tests believed.
CHILD_PROBE = r"""
import json, sys

observed_path = list(sys.path)
from trustedlane import enginepolicy, errors, phases, prerequisites

# 1. Nothing named `verifier` was imported by importing the trusted lane.
candidate_modules = sorted(
    name for name in sys.modules
    if name == "verifier" or name.startswith("verifier."))

# 2. No search-path entry could resolve it either. This is the real check;
#    the module list above only proves nothing has happened YET.
reach = enginepolicy.assert_no_candidate_import()

# 3. The approved engine's namespace is separate, so loading it never
#    contributes a `verifier` module.
engine_namespaces = sorted(
    name for name in sys.modules if name.startswith("trustedengine_"))

# 4. Every credential-bearing gate still refuses in D0.
refused = []
for label, call in (
        ("D1", lambda: phases.assert_phase_permitted(phases.D1)),
        ("D2", lambda: phases.assert_phase_permitted(phases.D2)),
        ("real_calls", lambda: prerequisites.assert_real_calls_authorized(
            prerequisites.prerequisite_status())),
        ("generation", lambda: prerequisites.assert_generation_authorized(
            prerequisites.prerequisite_status())),
):
    try:
        call()
    except errors.LaneRefusal:
        refused.append(label)
    else:
        raise SystemExit("GATE DID NOT REFUSE: " + label)

print(json.dumps({
    "candidate_modules_loaded": candidate_modules,
    "candidate_reachability": reach,
    "engine_namespaces_loaded": engine_namespaces,
    "gates_refused": refused,
    "sys_path": observed_path,
}, sort_keys=True))
"""


def build_private_import_root(destination: str, *, repository_root: str = ".",
                              extra_packages=()) -> str:
    """Copy ONLY the trusted lane into a fresh import root.

    `extra_packages` exists for the tests, which need to be able to build a
    deliberately-contaminated root and see the proof go red. Production callers
    pass nothing."""
    source = os.path.join(repository_root, "scripts", TRUSTED_PACKAGE)
    if not os.path.isdir(source):
        refuse(f"category=trusted_package_not_found path={TRUSTED_PACKAGE} — "
               "the containment proof needs the trusted lane to copy")
    os.makedirs(destination, exist_ok=True)
    shutil.copytree(source, os.path.join(destination, TRUSTED_PACKAGE),
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in extra_packages:
        origin = os.path.join(repository_root, "scripts", name)
        if os.path.isdir(origin):
            shutil.copytree(origin, os.path.join(destination, name),
                            dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
    return destination


def run_containment_proof(*, repository_root: str = ".", extra_path=(),
                          extra_packages=()) -> dict:
    """Run the probe in a child whose import root is only the trusted lane.

    `extra_path` and `extra_packages` are the test seams: adding the
    repository `scripts/` directory back must turn this red, which is the
    assertion that the proof is testing anything at all."""
    with tempfile.TemporaryDirectory(prefix="d0-import-root-") as root, \
            tempfile.TemporaryDirectory(prefix="d0-cwd-") as neutral_cwd:
        build_private_import_root(root, repository_root=repository_root,
                                  extra_packages=extra_packages)
        search = [root, *[os.path.abspath(p) for p in extra_path]]
        environment = {
            # A minimal environment. Inheriting the parent's would carry
            # PYTHONPATH, and the parent legitimately has `scripts/` on it.
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.pathsep.join(search),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        completed = subprocess.run(  # noqa: S603
            # `-P` keeps the working directory off `sys.path`; the working
            # directory is an empty temp dir anyway, so a regression in one
            # does not silently defeat the other.
            [sys.executable, "-P", "-c", CHILD_PROBE],
            cwd=neutral_cwd, env=environment, capture_output=True, text=True,
            timeout=120, check=False)
    if completed.returncode != 0:
        refuse(f"category=d0_containment_proof_failed "
               f"returncode={completed.returncode} "
               f"stderr_tail={completed.stderr.strip()[-400:]!r}")
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        refuse("category=d0_containment_proof_output_not_json")

    # The parent re-checks the child's own report rather than trusting that a
    # zero exit meant the right things happened.
    if report["candidate_modules_loaded"]:
        refuse(f"category=candidate_module_loaded_in_isolated_process "
               f"modules={report['candidate_modules_loaded']}")
    if sorted(report["gates_refused"]) != ["D1", "D2", "generation",
                                           "real_calls"]:
        refuse(f"category=d0_gate_did_not_refuse "
               f"refused={report['gates_refused']}")
    for entry in report["sys_path"]:
        base = os.path.basename(os.path.normpath(entry or "."))
        if base == "scripts":
            refuse(f"category=repository_scripts_on_isolated_path entry={entry!r}"
                   " — the credential-bearing process must not be able to "
                   "reach the candidate package's parent directory")
    return {
        "isolated": True,
        "candidate_modules_loaded": report["candidate_modules_loaded"],
        "candidate_reachability": report["candidate_reachability"],
        "engine_namespaces_loaded": report["engine_namespaces_loaded"],
        "gates_refused": report["gates_refused"],
        "engine_namespace_prefix": ENGINE_NAMESPACE_PREFIX,
        "honest_scope": (
            "the proof ran in a child process whose import root contains only "
            "the trusted lane. It shows that THIS runtime shape cannot reach "
            "the candidate package; it does not show that the repository "
            "lacks one, and on main the repository legitimately has one"),
    }


def main() -> None:
    report = run_containment_proof(
        repository_root=os.environ.get("GITHUB_WORKSPACE", "."))
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
