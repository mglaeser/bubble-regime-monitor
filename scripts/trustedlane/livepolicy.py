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
    # Bookkeeping, not a gate. This step reads the provider-attempt journal and
    # reports it as a job output; it decides nothing and blocks nothing. It is
    # advisory in the exact sense this allowlist is for: it runs `always()`,
    # AFTER the step that may have refused, and a failure to read the ledger
    # must not replace the real refusal with a louder one about the ledger.
    #
    # What it cannot hide: the attempt counts themselves. A failure here yields
    # empty job outputs, which `finalize` reports as zero attempts with
    # `journal_present` false — visibly missing rather than silently clean.
    "midterm-panel-review.yml": frozenset({
        "Account for every provider attempt",
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
    # `secrets: inherit` contains no `${{ }}` expression, so the expression
    # scanner never sees it — and it is the single most powerful secret-reaching
    # syntax GitHub has, handing the called workflow EVERY secret in the
    # repository in one word. Found by probing this function rather than by
    # reasoning about it, which is the only reason it is here: the expression
    # scanner looked complete and was not.
    #
    # Any job-level `secrets:` key counts, whatever its value. A mapping is
    # caught by the scanner anyway; the bare string is the one that got through,
    # and enumerating which values are dangerous is the mistake this file exists
    # to stop making.
    for job_name, job in (document.get("jobs") or {}).items():
        if "secrets" in (job or {}):
            found.append(f"jobs.{job_name}.secrets={(job or {})['secrets']!r}")
    if found:
        classes = sorted({_classify(r) for r in found})
        refuse(f"category=pr_controlled_workflow_reaches_a_secret name={name} "
               f"count={len(found)} classes={classes} triggers="
               f"{trigger_names(document)} — this workflow runs code the pull "
               "request author controls; a credential here is a credential any "
               "pull request can read. Move it to a workflow_run job that "
               "checks out trusted code and takes the candidate as data")
    return {"pr_controlled": True, "secrets_reachable": 0}


def assert_pull_request_target_checks_out_nothing(document: dict, *,
                                                  name: str = "") -> dict:
    """`pull_request_target` + a checkout of the PR head is the canonical hole.

    The trigger exists so a workflow can run with the BASE repository's
    permissions — including a write-scoped `GITHUB_TOKEN` — on a pull request
    from anywhere. Its default checkout is the base, which is the whole safety
    property. Naming a `ref:` opts out of it and runs the pull request author's
    code with those permissions.

    Refused on the presence of `ref:` at all, not on whether the ref looks
    dynamic. `${{ github.event.pull_request.head.sha }}` and a literal
    `refs/pull/N/merge` are the same code from the same author; a check that
    only caught the expression would be asking whether the hazard was spelled in
    the obvious way.

    A `pull_request_target` workflow that never checks out — a labeller, a
    commenter — is untouched, which is the legitimate use."""
    if "pull_request_target" not in trigger_names(document):
        return {"pull_request_target": False}
    offenders = []
    for job_name, job in (document.get("jobs") or {}).items():
        for index, step in enumerate((job or {}).get("steps") or []):
            step = step or {}
            uses = str(step.get("uses") or "")
            if not uses.startswith("actions/checkout"):
                continue
            ref = (step.get("with") or {}).get("ref")
            if ref is not None:
                offenders.append(f"{job_name}.steps[{index}] ref={ref!r}")
    if offenders:
        refuse(f"category=pull_request_target_checks_out_candidate_code "
               f"name={name} found={offenders} — this trigger runs with the "
               "base repository's permissions including a write-scoped token; "
               "its default checkout is the base, and naming a ref opts out of "
               "the only thing that makes it safe")
    return {"pull_request_target": True, "candidate_checkouts": 0}


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
    """Run every live-directory rule over every file GitHub will schedule.

    Checks run MOST SERIOUS FIRST, and the ordering is deliberate. A refusal
    stops the walk, so whichever check fires is the one the operator reads —
    and only that one. The first draft computed `pinning` above the dict
    literal, so a workflow that both reached a provider secret and used a moving
    tag was reported as "action not pinned". Found by running PR #23's workflow
    blob through it: the reintroduced SECOND_VENDOR_API_KEY and OPENAI_API_KEY
    were the reason to reject that file, and the message named the tag.

    Both refusals are correct. Only one of them tells the operator they are
    about to hand a pull request two provider credentials.
    """
    results = {}
    for path in live_workflow_paths(root=root):
        name = os.path.basename(path)
        document = _read(path)
        record = {
            "triggers": trigger_names(document),
            # 1. credential reach — the only one that leaks a key
            **assert_no_secret_in_pr_controlled_workflow(document, name=name),
            # 2. candidate code running with the base repo's permissions
            **assert_pull_request_target_checks_out_nothing(document,
                                                            name=name),
            # 3. a gate whose failure is survivable
            **assert_only_allowlisted_steps_survive_failure(document,
                                                            name=name),
        }
        # 4. supply chain, last: bad, but it does not by itself hand anything out
        uses = declared_uses(document)
        record.update(assert_actions_pinned(document, name=name) if uses
                      else {"pinned_actions": [], "declares_no_actions": True})
        results[name] = record
    return {
        "workflows": results,
        "count": len(results),
        "honest_scope": "every live workflow was parsed and checked; this says "
                        "nothing about what the referenced scripts do once they "
                        "run, only about what credentials can reach them",
    }
