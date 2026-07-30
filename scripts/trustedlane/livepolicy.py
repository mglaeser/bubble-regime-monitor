"""Policy over the LIVE workflow directory — the files GitHub actually runs.

`workflowfile` validates the trusted lane's own three phase files. This module
validates everything in `.github/workflows/`, including workflows that have
nothing to do with the lane, because the rule it enforces is not about the lane:

    a workflow that runs code a pull request can control must hold no
    provider credential.

That rule was violated by `independent-verify.yml` for the whole life of the
programme. It ran `on: pull_request`, checked out the PR merge ref, injected
`SECOND_VENDOR_API_KEY` and `OPENAI_API_KEY`, and executed
`python scripts/independent_verify.py` from that checkout. A pull request that
edited that one script ran its own code with both keys in the environment, and
opening the PR was the entire attack. Every document in the programme described
a trust boundary that this file crossed on every push.

Removing the two `env:` lines fixes today. It does not fix tomorrow: the same
two lines are one plausible-looking commit away from returning, and the review
that would catch them is the review that missed them for months. So the fix is
here, in a check that runs in CI over whatever is on disk.

Deliberately NOT a check on one named file. Keying on `independent-verify.yml`
would have exactly the flaw the third review round found in the D0 deployment
check: it validates a filename rather than the property, and the next
credential-bearing PR workflow arrives under a different name.
"""

from __future__ import annotations

import os

import yaml

from .errors import refuse
from .workflowfile import (
    LIVE_WORKFLOW_DIR,
    _secret_references,
    assert_actions_pinned,
)


def _read(path: str) -> dict:
    """Parse a live workflow, refusing anything that is not a mapping.

    A file that fails to parse is refused rather than skipped. "Could not read
    it, so it passed" is how a check reports success for the one file it
    understood least."""
    try:
        with open(path, "rb") as handle:
            document = yaml.safe_load(handle.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        refuse(f"category=live_workflow_unreadable path={path} "
               f"exception_class={type(exc).__name__}")
    if not isinstance(document, dict):
        refuse(f"category=live_workflow_not_a_mapping path={path}")
    return document

#: Triggers that hand a job code the pull request author wrote. `pull_request`
#: checks out the merge ref; `pull_request_target` is worse still, running with
#: the base repository's full secret scope while the checkout can be redirected
#: at the PR head. Both are refused the same way, because the distinction only
#: matters for how bad it is.
PR_CONTROLLED_TRIGGERS = ("pull_request", "pull_request_target")

#: Secrets whose disclosure spends money or grants model access. The check does
#: not use this list to decide what to refuse — it refuses ANY secret in a
#: PR-controlled workflow. It exists so the refusal can say which credential
#: class was reachable, which is what an operator needs to know first when
#: deciding whether to rotate.
PROVIDER_SECRET_HINTS = ("API_KEY", "TOKEN", "SECRET", "VENDOR", "OPENAI",
                         "ANTHROPIC")


#: The ONLY steps in the live directory permitted to survive their own failure,
#: as `workflow filename -> step names`. Everything else that declares
#: `continue-on-error` is refused.
#:
#: An allowlist rather than a denylist, and recorded in code rather than
#: inferred from a step name containing the word "advisory". Naming a step
#: "Type-check (ADVISORY)" is a comment; this is the check. Adding a genuinely
#: advisory step means adding it here, in a diff a reviewer sees, which is the
#: point — the failure mode being prevented is `continue-on-error` arriving on
#: the pytest step, where it turns a red suite into a green check and nothing
#: else in the repository would notice.
ADVISORY_STEPS = {
    "ci.yml": frozenset({
        "Type-check (ADVISORY — 43 tracked errors, A-13; not a gate yet)",
    }),
}


def assert_only_allowlisted_steps_survive_failure(document: dict, *,
                                                  name: str = "") -> dict:
    """`continue-on-error` turns a failing gate into a passing check.

    Job-level too: `continue-on-error` on the job makes every step in it
    advisory at once, which is the same defect with a wider blast radius and is
    easier to miss because it sits nowhere near the step it disarms."""
    permitted = ADVISORY_STEPS.get(name, frozenset())
    offenders = []
    for job_name, job in (document.get("jobs") or {}).items():
        job = job or {}
        if job.get("continue-on-error"):
            offenders.append(f"{job_name}: job-level")
        for index, step in enumerate(job.get("steps") or []):
            step = step or {}
            if not step.get("continue-on-error"):
                continue
            step_name = str(step.get("name") or f"steps[{index}]")
            if step_name not in permitted:
                offenders.append(f"{job_name}.{step_name}")
    if offenders:
        refuse(f"category=unallowlisted_continue_on_error name={name} "
               f"found={offenders} — a step that survives its own failure is "
               "not a gate; if it is genuinely advisory, add it to "
               "livepolicy.ADVISORY_STEPS so the exemption is reviewable")
    return {"advisory_steps": sorted(permitted)}


def live_workflow_paths(*, root: str = ".") -> list:
    """Every file GitHub will schedule, by extension — not by name.

    GitHub reads `.yml` and `.yaml` under `.github/workflows/` and ignores
    everything else, which is why the lane's D1/D2 templates are safe to commit
    as `.yml.template`. This mirrors that rule exactly rather than approximating
    it with a name list."""
    directory = os.path.join(root, LIVE_WORKFLOW_DIR)
    if not os.path.isdir(directory):
        return []
    return [os.path.join(directory, entry)
            for entry in sorted(os.listdir(directory))
            if entry.endswith((".yml", ".yaml"))]


def trigger_names(document: dict) -> list:
    """The declared triggers, surviving YAML's `on:` -> `True` coercion.

    Unquoted `on` is a YAML 1.1 boolean, so a plain loader returns the key
    `True`. A check that reads `document["on"]` sees nothing on every real
    workflow file and passes everything."""
    block = document.get("on")
    if block is None:
        block = document.get(True)
    if block is None:
        return []
    if isinstance(block, str):
        return [block]
    if isinstance(block, dict):
        return sorted(str(k) for k in block)
    if isinstance(block, list):
        return sorted(str(k) for k in block)
    refuse(f"category=workflow_trigger_block_unreadable type={type(block).__name__}")


def is_pr_controlled(document: dict) -> bool:
    return any(t in PR_CONTROLLED_TRIGGERS for t in trigger_names(document))


def _classify(reference: str) -> str:
    upper = reference.upper()
    hit = [h for h in PROVIDER_SECRET_HINTS if h in upper]
    return "PROVIDER_CLASS" if hit else "UNCLASSIFIED"


def assert_no_secret_in_pr_controlled_workflow(document: dict, *,
                                               name: str = "") -> dict:
    """Any secret, not just a provider secret, in a PR-triggered workflow.

    Refuse-by-default rather than a denylist of credential names. A denylist
    invites the argument "this one is harmless", and every enumerate-the-hazards
    check in this repository has had an edge someone walked around. A
    `pull_request`-triggered job has no business holding any secret; if one
    genuinely needs a credential, it belongs in a `workflow_run` job that
    executes trusted code, not here."""
    if not is_pr_controlled(document):
        return {"pr_controlled": False, "secrets_reachable": 0}
    found = _secret_references(document)
    if found:
        classes = sorted({_classify(r) for r in found})
        refuse(f"category=pr_controlled_workflow_reaches_a_secret name={name} "
               f"count={len(found)} classes={classes} triggers="
               f"{trigger_names(document)} — this workflow runs code the pull "
               "request author controls; a credential here is a credential any "
               "pull request can read. Move it to a workflow_run job that "
               "checks out trusted code and takes the candidate as data")
    return {"pr_controlled": True, "secrets_reachable": 0}


def declared_uses(document: dict) -> list:
    """Every `uses:` a live workflow declares, counted before it is judged.

    Separate from `assert_actions_pinned` because that function refuses a
    workflow with no `uses:` at all — a deliberate zero-coverage guard, correct
    for the lane's own three files, which all use actions. Applied to an
    arbitrary live workflow it is wrong: `openai-verifier-capability-probe.yml`
    runs an inline script and legitimately uses none.

    Counting first turns "no actions" into an observed, recorded fact instead of
    either a false refusal or a silent skip. A skip is the worse of the two: it
    reads as coverage."""
    found = []
    for job in (document.get("jobs") or {}).values():
        for step in ((job or {}).get("steps") or []):
            uses = (step or {}).get("uses")
            if uses is not None:
                found.append(str(uses))
    return found


def validate_live_workflows(*, root: str = ".") -> dict:
    """Run every live-directory rule over every file GitHub will schedule."""
    results = {}
    for path in live_workflow_paths(root=root):
        name = os.path.basename(path)
        document = _read(path)
        uses = declared_uses(document)
        pinning = (assert_actions_pinned(document, name=name) if uses
                   else {"pinned_actions": [], "declares_no_actions": True})
        results[name] = {
            "triggers": trigger_names(document),
            **assert_no_secret_in_pr_controlled_workflow(document, name=name),
            **assert_only_allowlisted_steps_survive_failure(document,
                                                            name=name),
            **pinning,
        }
    return {
        "workflows": results,
        "count": len(results),
        "honest_scope": "every live workflow was parsed and checked; this says "
                        "nothing about what the referenced scripts do once they "
                        "run, only about what credentials can reach them",
    }
