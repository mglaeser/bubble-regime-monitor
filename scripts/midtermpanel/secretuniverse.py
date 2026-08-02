"""Where the provider key may be named, across every live workflow.

## Why validating one file is not enough

`privilegedworkflow.validate` proves the panel workflow is well-shaped. It says
nothing about the other five live workflows, and the question an operator
actually has before installing a persistent repository secret is not "is the
panel safe" but:

    what, in this repository, can read this key?

A repository-scoped secret is readable by every workflow in the repository. So
the answer has to be established over the whole live directory, and it has to be
re-established on every change, because the failure mode is additive: someone
adds a second workflow that names the key, and nothing that validates the first
one notices.

## What this cannot do, said plainly

This does not eliminate the accepted residual. A pull request can add a
`pull_request`-triggered workflow that references the secret, and that workflow
exists on the PR's branch where this scanner — which reads the merge target —
does not see it. `livepolicy` refuses secrets in PR-triggered workflows and would
turn CI red, which is a deterrent and a detection, not a prevention.

What this DOES do is keep main's live workflow universe small, enumerated and
reviewable, so that "what can read the key" has a short answer somebody can
check rather than a long one somebody has to trust.
"""

from __future__ import annotations

import os

import yaml

from . import SECRET_NAME, WORKFLOW_FILENAME
from .errors import refuse
from .privilegedworkflow import SECRET_BEARING_JOBS

#: Secret names the runner provides that are not provider credentials.
#: `GITHUB_TOKEN` is scoped by the `permissions:` block and is not a spend.
RUNNER_PROVIDED_SECRETS = ("GITHUB_TOKEN", "github.token")

#: Provider credentials that already existed before the mid-term panel, with
#: where they live and why they are not this panel's problem.
#:
#: A register rather than a blanket refusal, and the distinction is the whole
#: reason this constant exists. The first version refused ANY second provider
#: secret anywhere in the live directory. That fired immediately on the
#: capability probe — a real, pre-existing, `workflow_dispatch`-only workflow
#: with its own separately-named key — and the only way to make the gate pass
#: would have been to delete the probe or to delete the check. A gate whose
#: only escape is to disable it will be disabled; this repository has written
#: that lesson down twice already.
#:
#: So known credentials are enumerated with their reason, and anything NOT
#: enumerated still refuses. The property being protected is "no new provider
#: credential appears in the live directory unreviewed", which this preserves
#: and a blanket ban did not.
#:
#: Adding an entry here is a deliberate edit in a diff a reviewer sees. That is
#: the point.
KNOWN_OTHER_PROVIDER_SECRETS = {
    "openai-verifier-capability-probe.yml": {
        "secrets": ("OPENAI_VERIFIER_PROBE_API_KEY",),
        "why": ("the pre-existing capability probe. `workflow_dispatch` only, "
                "so it never runs on a pull request and never reports on a "
                "candidate head. It holds its OWN key under its own name, so it "
                "is not a second reader of the panel's credential. Trusted-lane "
                "operator prerequisite 3 covers rotating or deleting it"),
    },
}


def _live_workflow_paths(root: str) -> list:
    """Every file GitHub will schedule, by extension — not by name.

    Mirrors `livepolicy.live_workflow_paths` exactly rather than importing a
    private helper: GitHub reads `.yml`/`.yaml` under `.github/workflows/` and
    ignores everything else, which is why the lane's templates are safe to commit
    as `.yml.template`."""
    directory = os.path.join(root, ".github", "workflows")
    if not os.path.isdir(directory):
        return []
    return [os.path.join(directory, entry)
            for entry in sorted(os.listdir(directory))
            if entry.endswith((".yml", ".yaml"))]


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                yield key
            yield from _strings(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _strings(item)


def _named_secrets(node) -> set:
    """Every secret NAME reachable from this subtree, by any syntax.

    Reuses the trusted lane's expression scanner for the hard part —
    `secrets['NAME']` and `toJSON(secrets)` contain no `secrets.` and were the
    exact bug that scanner was rewritten to catch — and then extracts the
    identifiers from the expressions it flags.

    `toJSON(secrets)` yields the sentinel `*` because it reaches EVERY secret and
    naming one would understate it."""
    from trustedlane.workflowfile import _EXPRESSION, _secret_references

    found = set()
    for text in _secret_references(node):
        for expression in _EXPRESSION.findall(text):
            if "toJSON" in expression or "tojson" in expression:
                found.add("*")
                continue
            cleaned = (expression.replace("[", ".").replace("]", ".")
                       .replace("'", ".").replace('"', "."))
            for token in cleaned.split("."):
                token = token.strip()
                if token and token.upper() == token and len(token) > 3:
                    found.add(token)
    return found


def scan(*, root: str = ".") -> dict:
    """Every live workflow, every job, every secret name it can reach."""
    universe = {}
    for path in _live_workflow_paths(root):
        name = os.path.basename(path)
        try:
            with open(path, "rb") as handle:
                document = yaml.safe_load(handle.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            refuse(f"category=live_workflow_unreadable name={name} "
                   f"exception_class={type(exc).__name__}")
        if not isinstance(document, dict):
            refuse(f"category=live_workflow_not_a_mapping name={name}")

        record = {"workflow_level": sorted(_named_secrets(
            document.get("env") or {})), "jobs": {}}
        for job_id, job in (document.get("jobs") or {}).items():
            names = _named_secrets(job or {})
            if "secrets" in (job or {}):
                # `secrets: inherit` hands over every secret in one word and
                # contains no expression for the scanner to find.
                names.add("*")
            if names:
                record["jobs"][str(job_id)] = sorted(names)
        universe[name] = record
    return universe


def assert_provider_key_universe(*, root: str = ".") -> dict:
    """`TRUSTED_VERIFIER_OPENAI_KEY` may be named only by count and panel.

    Four separate refusals rather than one, because they are four different
    mistakes and the operator's next action differs for each: a second workflow
    naming the key is a new attack surface; the same key in `finalize` is a
    scoping slip; a workflow-level `env:` is a slip with a wider blast radius;
    and a SECOND provider secret is a widening of the accepted residual that
    nobody accepted."""
    universe = scan(root=root)
    offenders = {"other_workflows": [], "wrong_jobs": [],
                 "workflow_level": [], "second_secrets": []}

    for workflow, record in sorted(universe.items()):
        if record["workflow_level"]:
            offenders["workflow_level"].append(
                f"{workflow}: {record['workflow_level']}")
        for job_id, names in sorted(record["jobs"].items()):
            for named in names:
                if named in RUNNER_PROVIDED_SECRETS:
                    continue
                if named == "*":
                    offenders["second_secrets"].append(
                        f"{workflow}.{job_id}: reaches EVERY secret "
                        "(`secrets: inherit` or toJSON(secrets))")
                    continue
                if named != SECRET_NAME:
                    known = KNOWN_OTHER_PROVIDER_SECRETS.get(workflow, {})
                    if named in known.get("secrets", ()):
                        # Registered above with a stated reason. Not this
                        # panel's credential and not a new appearance.
                        continue
                    offenders["second_secrets"].append(
                        f"{workflow}.{job_id}: {named}")
                    continue
                if workflow != WORKFLOW_FILENAME:
                    offenders["other_workflows"].append(f"{workflow}.{job_id}")
                elif job_id not in SECRET_BEARING_JOBS:
                    offenders["wrong_jobs"].append(f"{workflow}.{job_id}")

    if offenders["workflow_level"]:
        refuse(f"category=provider_key_in_workflow_level_env "
               f"found={offenders['workflow_level']} — workflow-level `env:` is "
               "inherited by every job, including the ones whose whole purpose "
               "is to decide and to clean up without a credential in scope")
    if offenders["other_workflows"]:
        refuse(f"category=provider_key_named_by_another_workflow "
               f"found={offenders['other_workflows']} — the accepted residual "
               f"covers ONE privileged workflow ({WORKFLOW_FILENAME}); a second "
               "reader of the same key is a second thing to review whenever the "
               "key is rotated, and nobody agreed to it")
    if offenders["wrong_jobs"]:
        refuse(f"category=provider_key_in_unexpected_job "
               f"found={offenders['wrong_jobs']} "
               f"permitted={list(SECRET_BEARING_JOBS)}")
    if offenders["second_secrets"]:
        refuse(f"category=second_provider_secret_named "
               f"found={offenders['second_secrets']} — the residual the "
               "operator accepted is one provider key in one workflow; a second "
               "credential widens it without anyone having accepted the wider "
               "version")

    reachable = sorted(
        f"{WORKFLOW_FILENAME}.{job}" for job in SECRET_BEARING_JOBS
        if SECRET_NAME in universe.get(WORKFLOW_FILENAME, {})
        .get("jobs", {}).get(job, []))
    return {
        "provider_key": SECRET_NAME,
        "reachable_from": reachable,
        "live_workflows_scanned": len(universe),
        "honest_scope": (
            "this constrains main's live workflow universe. It cannot see a "
            "workflow that exists only on a pull request branch — that is the "
            "accepted same-repository residual, detected by livepolicy turning "
            "CI red rather than prevented"),
    }
