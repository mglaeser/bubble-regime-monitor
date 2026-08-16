"""Static policy over the ONE privileged workflow that holds the panel secret.

## The hazard this file exists for

`livepolicy.py` enforces the rule that made V-TRUST a defect:

    a workflow that runs code a pull request can control must hold no
    provider credential.

It implements that by refusing any secret in a `pull_request`-triggered
workflow, and its refusal message prescribes the remedy verbatim: *"Move it to a
workflow_run job that checks out trusted code and takes the candidate as data."*

This package takes that advice. Which means `livepolicy` now passes the new
workflow trivially — it is not PR-controlled — and stops being the control. The
privileged workflow introduces a DIFFERENT hazard class that `livepolicy` has no
concept of, because until now the repository had no privileged workflow:

    a `workflow_run` workflow runs the DEFAULT BRANCH definition with full
    repository secret scope, while the event that triggered it was produced by
    a pull request. Everything reachable from that run — the PR head tree, the
    triggering run's artifacts, the cache it wrote — is candidate-controlled
    content arriving in a job that holds a provider key.

`workflow_run` is safe only because of what the workflow does NOT do. That is
precisely the kind of safety property that decays silently, so it is asserted
here rather than described in the workflow's own comments.

## The one property everything else serves

**The candidate's tree is never materialised in the workspace.**

Not "we do not run the candidate's tests" — that is a consequence. The property
is that the bytes never become files that anything could execute, import, source,
or install. Every check below is a way that property has been broken in real
workflows:

  * `actions/checkout` with a `ref:` — the direct form;
  * `git checkout`/`git switch`/`git merge` inside a `run:` — the same thing
    spelled in shell, which a checkout-shaped check never sees;
  * `actions/download-artifact` — the triggering run wrote those artifacts, and
    a zip is a tree;
  * `actions/cache` — the PR's CI populated it, and a restored cache is a tree
    someone else chose the contents of;
  * a local `uses: ./…` action — a directory in the workspace, which is only
    trustworthy while the workspace is;
  * job-level `uses:` — a whole reusable workflow that step-level pinning never
    walks.

Reading candidate commits as inert git OBJECTS is allowed and is the point:
`git cat-file`, `git diff` and `git ls-tree` read `.git`, where nothing runs.
The distinction between an object and a worktree is the entire safety argument,
so the checks are written to permit the first and refuse the second rather than
to pattern-match on the word "candidate".

Those objects arrive with `fetch-depth: 0` on the trusted checkout, while it
still holds its own token — NOT with a later fetch. The credential boundary
(`assert_no_post_checkout_network_git`) explains why that distinction is not
cosmetic.

## What is deliberately NOT refused

The candidate head SHA. It has to flow — into API calls, into `git cat-file`,
into the status the panel publishes on that exact commit, into the evidence. A
rule that banned the string would ban the architecture.

So the rule is POSITIONAL, not textual: a candidate SHA may appear anywhere that
treats it as data, and nowhere that treats it as a selector for code. That is why
`assert_no_candidate_checkout` refuses `ref:` outright instead of trying to
decide whether a particular expression resolves to something dangerous — the
laundering game (`env.`, `steps.`, `needs.`) was already lost once in
`workflowfile.CALLER_CONTROLLED_PREFIXES`, and the lesson recorded there is to
refuse the shape rather than to inspect the value.
"""

from __future__ import annotations

import os

import yaml

from . import (
    CI_WORKFLOW_NAME,
    SECRET_NAME,
    WORKFLOW_FILENAME,
    WORKFLOW_NAME,
)
from .errors import refuse

#: Triggers the privileged workflow may declare. Anything else is refused.
#:
#: An allowlist, not a denylist of the scary ones. `issue_comment`,
#: `pull_request_target` and `repository_dispatch` are all obviously wrong here,
#: and enumerating them would leave whatever GitHub ships next quietly permitted.
PERMITTED_TRIGGERS = ("workflow_run",)

#: Jobs permitted to reference the provider secret.
#:
#: `preflight` is excluded on purpose and it is the important exclusion: it is
#: the job that decides whether the run may proceed at all, and a decision made
#: in a process holding a credential is a decision whose failure modes include
#: leaking it. `finalize` is excluded because it runs on every path including
#: failure, which is the worst place for a key to be in scope.
SECRET_BEARING_JOBS = ("count", "panel")

#: Actions that hand this job content produced by some other run.
#:
#: `actions/download-artifact` is on this list AND has a single narrow exception
#: below. That is not a contradiction, it is the whole design: the default is
#: refusal, and the one permitted shape is enumerated so precisely that widening
#: it requires editing a constant a reviewer will see.
FORBIDDEN_ACTION_PREFIXES = (
    "actions/download-artifact",
    "actions/cache",
    "actions/upload-pages-artifact",
    "dawidd6/action-download-artifact",
)

#: The ONE artifact download this workflow may perform.
#:
#: The panel job must execute the plan the count job actually produced. Rebuilding
#: it independently and comparing digests cannot work: the plan contains the
#: provider's COUNTS, and a rebuild without calling the provider has nothing to
#: compare. So the artifact has to cross from count to panel, and the question
#: becomes which download is safe.
#:
#: Exactly one is: a same-run download. `actions/download-artifact` scopes itself
#: to the current workflow run unless it is given a `run-id` (and a
#: `github-token` to authorise reaching that run). Omitting those inputs is not a
#: stylistic choice — it is what makes the action unable to reach the candidate's
#: CI run, whose artifacts a pull request authored.
#:
#: So the permitted shape is defined by what it must NOT specify. Each forbidden
#: input is a different way of turning a same-run read into a cross-run one:
#:   run-id / github-token  name another run and authorise reaching it
#:   repository             another repository entirely
#:   pattern / merge-multiple  match more than the one artifact, including any
#:                          future artifact whose name happens to fit
ARTIFACT_DOWNLOAD_ACTION = "actions/download-artifact"
ARTIFACT_DOWNLOAD_JOB = "panel"
ARTIFACT_NAME_PREFIX = "midterm-count-"
ARTIFACT_DOWNLOAD_FORBIDDEN_INPUTS = (
    "github-token", "repository", "run-id", "pattern", "merge-multiple",
)

#: Shell that materialises a tree, in a job whose whole safety argument is that
#: no tree is materialised. Object READS (`git cat-file`, `git diff`, `git show`,
#: `git ls-tree`, `git rev-parse`) are absent deliberately — they are the
#: capability this lane needs. `git fetch` is absent from THIS list too, and
#: refused by `POST_CHECKOUT_NETWORK_COMMANDS` instead: it is not a tree
#: hazard, it is a credential one.
TREE_MATERIALISING_COMMANDS = (
    "git checkout",
    "git switch",
    "git merge",
    "git cherry-pick",
    "git rebase",
    "git apply",
    "git stash",
    "git worktree add",
    "gh run download",
    "gh pr checkout",
)

#: Commands and settings that would give a privileged step an authenticated
#: git capability after checkout — or make it need one.
#:
#: `persist-credentials: false` is the boundary: `actions/checkout` uses its
#: token, then removes it, so no later step can act as the repository. That is
#: what makes the workspace safe to hold a provider key in.
#:
#: A post-checkout `git fetch` that has to reach a PRIVATE origin cannot work
#: under it — which was the whole defect: the command was valid and the
#: authentication model was wrong. Re-adding one either fails, silently does
#: nothing (see `assert_no_post_checkout_network_git` for the measured
#: short-circuit), or worse, is "fixed" by persisting the credential and handing
#: every later step the repository token.
#:
#: `git cat-file`, `git diff`, `git show`, `git ls-tree` and `git rev-parse`
#: are deliberately absent: they read the object database and change no
#: worktree, which is exactly the capability this lane needs.
POST_CHECKOUT_NETWORK_COMMANDS = (
    "git fetch",
    "git pull",
    "git remote add",
    "git clone",
    "git ls-remote",
    "git submodule update",
    "git push",
)

#: Ways to smuggle a credential back into git after checkout dropped it.
CREDENTIAL_REINTRODUCTION_MARKERS = (
    "credential.helper",
    "http.extraheader",
    "extraheader",
    "GIT_ASKPASS",
    "askpass",
    "url.https://x-access-token",
    "x-access-token:",
)

#: Executing, importing or installing from a candidate path.
CANDIDATE_EXECUTION_MARKERS = (
    "pip install -e", "pip install .", "pip install -r",
    "setup.py", "python -m verifier", "scripts/verifier",
    "npm install", "npm ci", "yarn install", "make ",
    "bash candidate", "source candidate",
)


def _read(path: str) -> dict:
    """Parse the privileged workflow, refusing anything unparseable.

    Refused rather than skipped, for the reason `livepolicy._read` records: "could
    not read it, so it passed" is how a check reports success for the one file it
    understood least — and this is the file that holds the key."""
    try:
        with open(path, "rb") as handle:
            document = yaml.safe_load(handle.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        refuse(f"category=privileged_workflow_unreadable path={path} "
               f"exception_class={type(exc).__name__}")
    if not isinstance(document, dict):
        refuse(f"category=privileged_workflow_not_a_mapping path={path}")
    return document


def _on_block(document: dict) -> dict:
    """The trigger block, surviving YAML 1.1 folding `on:` to the boolean True.

    Same trap `livepolicy.trigger_names` documents: a validator that reads only
    `document["on"]` sees nothing on every real workflow and passes everything."""
    for key in ("on", True):
        if key in document:
            block = document[key]
            if not isinstance(block, dict):
                refuse("category=privileged_workflow_on_block_not_mapping")
            return block
    refuse("category=privileged_workflow_has_no_on_block")


def _steps(document: dict):
    """Every (job_id, index, step) in declaration order."""
    for job_id, job in (document.get("jobs") or {}).items():
        for index, step in enumerate((job or {}).get("steps") or []):
            yield str(job_id), index, (step or {})


def assert_triggers(document: dict) -> dict:
    """Only `workflow_run` on completed `ci`. Nothing else, including dispatch.

    The `workflows:` filter is checked, not just the trigger name. A
    `workflow_run` with no filter fires on the completion of EVERY workflow in
    the repository, including one a pull request just added — which would let a
    candidate choose when the privileged run starts and what event payload it
    reads."""
    block = _on_block(document)
    declared = sorted(str(k) for k in block)
    forbidden = [t for t in declared if t not in PERMITTED_TRIGGERS]
    if forbidden:
        refuse(f"category=privileged_workflow_trigger_not_permitted "
               f"triggers={forbidden} permitted={list(PERMITTED_TRIGGERS)} — a "
               "privileged workflow holding a repository secret may only be "
               "started by the completion of trusted CI. A dispatch runs "
               "against a SELECTED REF, and these checkouts name no ref")
    if "workflow_run" not in declared:
        refuse("category=privileged_workflow_has_no_workflow_run_trigger — "
               "without it the panel never runs automatically, which is the "
               "entire feature")

    run_block = block.get("workflow_run") or {}
    if not isinstance(run_block, dict):
        refuse("category=workflow_run_block_not_mapping")
    workflows = run_block.get("workflows")
    if not isinstance(workflows, list) or not workflows:
        refuse("category=workflow_run_does_not_filter_by_workflow — an "
               "unfiltered workflow_run fires on the completion of every "
               "workflow in the repository, including one a pull request just "
               "added; the candidate would choose when the privileged run "
               "starts")
    if [str(w) for w in workflows] != [CI_WORKFLOW_NAME]:
        refuse(f"category=workflow_run_filters_wrong_workflow "
               f"workflows={[str(w) for w in workflows]} "
               f"expected={[CI_WORKFLOW_NAME]}")
    types = run_block.get("types")
    if types is not None and [str(t) for t in types] != ["completed"]:
        refuse(f"category=workflow_run_types_not_completed types={types}")
    return {"triggers": declared, "workflow_run_filter": [str(w) for w in workflows]}


def assert_no_candidate_checkout(document: dict) -> dict:
    """Every checkout takes the default branch, with no persisted credential.

    `ref:` is refused OUTRIGHT — not inspected, not expression-matched. For a
    `workflow_run` event the default checkout is the default-branch commit the
    workflow definition itself came from, which is exactly what a privileged job
    wants; naming any ref can only move it away from that. Refusing the key
    rather than judging its value is the lesson `workflowfile` records after its
    prefix denylist was walked around with `env.` and `steps.`.

    A run-time check re-verifies the checked-out commit, because a static rule
    about a YAML key is a statement about the file, not about the runner."""
    checked = []
    for job_id, index, step in _steps(document):
        uses = str(step.get("uses") or "")
        if "actions/checkout" not in uses:
            continue
        where = f"{job_id}.steps[{index}]"
        with_block = step.get("with") or {}
        if with_block.get("persist-credentials") is not False:
            refuse(f"category=privileged_checkout_persists_credentials "
                   f"where={where} — a checkout that leaves a usable token in "
                   ".git/config hands write access to anything that later runs "
                   "in a workspace that also holds a provider key")
        if "repository" in with_block:
            refuse(f"category=privileged_checkout_selects_repository "
                   f"where={where} — the privileged job checks out its own "
                   "default branch only")
        if "ref" in with_block:
            refuse(f"category=privileged_checkout_names_a_ref where={where} "
                   f"ref={str(with_block.get('ref'))[:60]!r} — for a "
                   "workflow_run event the default checkout is already the "
                   "default-branch commit this workflow came from; naming a ref "
                   "can only move it toward candidate-controlled code, and "
                   "whether a particular expression is safe is not a question "
                   "this policy is willing to answer")
        checked.append(where)
    if not checked:
        refuse("category=privileged_workflow_has_no_checkout_step — a workflow "
               "that never checks out cannot have been checked for checking out "
               "the wrong thing; this guard exists so the check cannot silently "
               "cover nothing")
    # Reported here, and only here. The credential-boundary round nearly added
    # a second `persist-credentials` check in its own function — which would
    # have been this module's third duplicated rule, and duplicated rules are
    # how `aggregate` ended up disagreeing with the engine's role gate and
    # blocking every review. `assert_no_credential_reintroduction` owns the
    # other half of the boundary: putting a token back after checkout dropped
    # it.
    return {"checkout_steps": checked, "checkouts_persisting_credentials": 0}


def _is_permitted_same_run_download(job_id: str, step: dict) -> bool:
    """Exactly the one shape described at `ARTIFACT_DOWNLOAD_ACTION`.

    Every clause is load-bearing, and the function returns False rather than
    refusing so that the caller can report the step as a forbidden artifact
    consumer with the ordinary message — a near-miss on this shape IS a
    forbidden download, not a special case."""
    from trustedlane import actionpolicy

    uses = str(step.get("uses") or "")
    if not uses.startswith(ARTIFACT_DOWNLOAD_ACTION + "@"):
        return False
    if job_id != ARTIFACT_DOWNLOAD_JOB:
        return False
    try:
        actionpolicy.assert_pinned(uses, where=f"{job_id}:download-artifact")
    except Exception:
        return False
    with_block = step.get("with") or {}
    if any(key in with_block for key in ARTIFACT_DOWNLOAD_FORBIDDEN_INPUTS):
        return False
    name = str(with_block.get("name") or "")
    if not name.startswith(ARTIFACT_NAME_PREFIX):
        return False
    # The destination must be inside the runner's temp area. A download into the
    # workspace puts candidate-derived bytes next to the code that is about to
    # run, which is the adjacency this whole module exists to prevent.
    path = str(with_block.get("path") or "")
    if "runner.temp" not in path:
        return False
    return True


def assert_no_candidate_artifacts_or_cache(document: dict) -> dict:
    """No artifact or cache consumption, except one same-run download in `panel`.

    The classic `workflow_run` escalation: the privileged workflow downloads the
    artifact the PR's CI uploaded, unzips it, and runs or trusts what is inside.
    A zip is a tree, and a restored cache is a tree whose contents someone else
    chose.

    The single exception exists because the panel MUST execute the plan the count
    job produced, and that plan contains provider counts which cannot be
    rebuilt without calling the provider again. The exception is narrowed to a
    same-run download in one named job, with the cross-run selectors absent —
    see `ARTIFACT_DOWNLOAD_ACTION` for why each absent input is what makes it
    same-run.

    Caches remain refused outright. A cache has no same-run meaning here: it is
    populated by whichever run got there first, which for a pull request is the
    candidate's own CI."""
    offenders = []
    permitted = []
    for job_id, index, step in _steps(document):
        uses = str(step.get("uses") or "")
        if not any(uses.startswith(p) for p in FORBIDDEN_ACTION_PREFIXES):
            continue
        if _is_permitted_same_run_download(job_id, step):
            permitted.append(f"{job_id}.steps[{index}]")
            continue
        offenders.append(f"{job_id}.steps[{index}] uses={uses}")
    if offenders:
        refuse(f"category=privileged_workflow_consumes_candidate_artifacts "
               f"found={offenders} permitted_shape="
               f"'{ARTIFACT_DOWNLOAD_ACTION}@<approved pin> in job "
               f"{ARTIFACT_DOWNLOAD_JOB} with name {ARTIFACT_NAME_PREFIX}*, "
               f"path under runner.temp, and none of "
               f"{list(ARTIFACT_DOWNLOAD_FORBIDDEN_INPUTS)}' — the triggering "
               "run is the candidate's CI; its artifacts and its cache are "
               "content a pull request authored, and unpacking either into a job "
               "that holds a provider key is the canonical workflow_run "
               "escalation")
    if len(permitted) > 1:
        refuse(f"category=more_than_one_artifact_download found={permitted} — "
               "the count-to-panel handoff is one artifact; a second download "
               "is a second thing to validate that nothing validates")
    return {"artifact_or_cache_steps": 0,
            "permitted_same_run_downloads": permitted}


def assert_no_local_or_unpinned_actions(document: dict) -> dict:
    """No `./` action, no docker action, no job-level reusable workflow.

    A local action is a directory in the workspace, and it is only as trustworthy
    as the workspace — which is the thing the rest of this file is busy
    constraining. Job-level `uses:` is worse: it pulls in a whole file of
    someone else's steps that step-level pin checking never walks, the same
    bypass `workflowfile.FORBIDDEN_JOB_KEYS` refuses for the trusted lane."""
    offenders = []
    for job_id, job in (document.get("jobs") or {}).items():
        job = job or {}
        for key in ("uses", "secrets", "container", "services"):
            if key in job:
                offenders.append(f"jobs.{job_id}.{key}")
    for job_id, index, step in _steps(document):
        uses = str(step.get("uses") or "")
        if not uses:
            continue
        if uses.startswith("./") or uses.startswith(".\\") or uses.startswith("docker://"):
            offenders.append(f"{job_id}.steps[{index}] uses={uses}")
    if offenders:
        refuse(f"category=privileged_workflow_uses_untrusted_action "
               f"found={offenders} — a local `./` action is a directory in the "
               "workspace, a docker action is an unpinned image, and job-level "
               "`uses:`/`secrets:`/`container:`/`services:` each pull in code or "
               "credentials that step-level pinning never inspects")
    return {"untrusted_action_uses": 0}


def assert_no_tree_materialisation(document: dict) -> dict:
    """No `run:` may turn candidate objects into candidate files.

    This is the check that makes the object model meaningful. The candidate's
    commits sit in `.git`, where nothing executes them, and steps read them with
    `git cat-file`. The moment a step runs `git checkout`, `git merge`,
    `git apply` or `gh run download`, those bytes become a worktree, and every
    other guarantee in this module is about a workspace that no longer exists.

    Deliberately matches on the COMMAND, not on whether a candidate SHA appears
    near it. `git checkout $SHA` and `git checkout FETCH_HEAD` are the same act,
    and only one of them mentions anything candidate-shaped."""
    offenders = []
    for job_id, index, step in _steps(document):
        script = str(step.get("run") or "")
        hits = [c for c in TREE_MATERIALISING_COMMANDS if c in script]
        if hits:
            offenders.append(f"{job_id}.steps[{index}]:{hits}")
    if offenders:
        refuse(f"category=privileged_workflow_materialises_a_tree "
               f"found={offenders} — the candidate is fetched as inert git "
               "objects and must stay that way; a command that produces a "
               "worktree turns reviewed data back into runnable code inside a "
               "job that holds a provider key")
    return {"tree_materialising_steps": 0}


def assert_no_post_checkout_network_git(document: dict) -> dict:
    """No privileged step may run an authenticated git command after checkout.

    ## The defect this exists to keep out

    Both credential-bearing jobs used to run
    `git fetch --no-tags --no-recurse-submodules origin <sha>:<ref>` after a
    checkout configured with `persist-credentials: false`. The syntax was valid
    — an earlier review had already fixed an invalid `--no-checkout` — but the
    AUTHENTICATION model was wrong: the checkout deliberately removes its
    token, and this repository is private, so that command had no credential
    with which to reach origin.

    What it actually did is worse than plainly failing, and was measured rather
    than assumed (git 2.43.0, `tests/test_midterm_candidate_objects.py`):

      * `git fetch --no-tags origin <sha>` with the object ALREADY PRESENT
        exits 0 without contacting the remote at all — proved against a remote
        URL that does not exist. So under `fetch-depth: 0`, for a
        same-repository head, the old command silently did nothing and
        reported success.
      * The same command with the object ABSENT exits 128. So it failed in
        exactly the cases it was there to handle — a fork head, or a head that
        appeared after the checkout — and it failed INSIDE a job that had
        already read the provider key.
      * Dropping `--no-tags` makes it contact the remote unconditionally, so
        it fails always.

    A command that is a no-op when it is unnecessary and a mid-job failure when
    it is necessary is not an acquisition step. The local shell test could not
    have separated those cases either: it used a filesystem remote, which needs
    no authentication. It proved syntax, object/worktree separation and HEAD
    stability, and it proved them correctly. It said nothing about talking to a
    private GitHub origin.

    The fix removes the need rather than the boundary. `fetch-depth: 0` on the
    trusted checkout brings every branch's history in while the token is still
    held, so a same-repository candidate is already present; preflight refuses
    forks, for which that is not true; and the jobs ASSERT presence with
    `git cat-file -e`.

    So a re-added fetch is refused here — because the honest way to make one
    work is to persist the credential, and that hands every later step in a
    key-bearing job the repository token.

    This static check is the load-bearing one, not the shell test's empirical
    "the step needs no network". The short-circuit above means a reintroduced
    `--no-tags` fetch would keep that empirical test green while restoring the
    defect."""
    offenders = []
    for job_id, index, step in _steps(document):
        script = str(step.get("run") or "")
        hits = [c for c in POST_CHECKOUT_NETWORK_COMMANDS if c in script]
        if hits:
            offenders.append(f"{job_id}.steps[{index}]:{hits}")
    if offenders:
        refuse(f"category=privileged_workflow_fetches_after_checkout "
               f"found={offenders} — the checkout token is not persisted and "
               "this repository is private, so an authenticated git command "
               "after checkout cannot work. The candidate's objects arrive "
               "with `fetch-depth: 0`; assert them with `git cat-file -e`")
    return {"post_checkout_network_git_steps": 0}


def assert_no_credential_reintroduction(document: dict) -> dict:
    """Nothing may write a git credential back after checkout dropped it.

    The OTHER half of the credential boundary. `assert_no_candidate_checkout`
    already requires `persist-credentials: false` on every checkout, and this
    module deliberately does not check that twice: a second copy of a rule is a
    second chance for the two copies to disagree, and this lane has already
    shipped one of those.

    What is left is the way around it — not asking `actions/checkout` to keep a
    token, but installing one afterwards, through a credential helper, an
    `http.extraheader`, an askpass program or an `x-access-token` remote URL.
    Each of those restores exactly the capability the setting was chosen to
    remove, without changing the setting that documents the choice."""
    reintroduced = []
    for job_id, index, step in _steps(document):
        script = str(step.get("run") or "")
        hits = [m for m in CREDENTIAL_REINTRODUCTION_MARKERS if m in script]
        if hits:
            reintroduced.append(f"{job_id}.steps[{index}]:{hits}")
    if reintroduced:
        refuse(f"category=privileged_workflow_reintroduces_a_git_credential "
               f"found={reintroduced} — checkout dropped the token on purpose; "
               "writing one back into git config undoes the boundary without "
               "changing the setting that documents it")
    return {"credential_reintroduction_steps": 0}


def assert_no_candidate_execution(document: dict) -> dict:
    """No `run:` may execute, import or install from candidate content."""
    offenders = []
    for job_id, index, step in _steps(document):
        script = str(step.get("run") or "")
        hits = [m for m in CANDIDATE_EXECUTION_MARKERS if m in script]
        if hits:
            offenders.append(f"{job_id}.steps[{index}]:{hits}")
    if offenders:
        refuse(f"category=privileged_workflow_executes_candidate_content "
               f"found={offenders}")
    return {"candidate_execution_steps": 0}


def _expression_strings(node):
    """Every string in a nested structure (keys included)."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                yield key
            yield from _expression_strings(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _expression_strings(item)


def _names_secret(node) -> bool:
    """Does this subtree read the `secrets` context, by any syntax?

    Reuses the trusted lane's scanner rather than substring-matching
    `secrets.` — `secrets['NAME']` and `toJSON(secrets)` contain no dot and were
    the exact bug that scanner was rewritten to catch."""
    from trustedlane.workflowfile import _secret_references
    return bool(_secret_references(node))


def assert_secret_is_scoped(document: dict) -> dict:
    """The provider secret appears only in the jobs allowed to hold it.

    Workflow-level `env:` is checked first and separately: it is inherited by
    EVERY job, so a secret there is a secret in `preflight` and in `finalize`,
    and neither of those jobs is ever supposed to be able to spend money.

    The named secret is also checked: this workflow may reference exactly one,
    and a second credential arriving here would widen the blast radius of the
    same accepted residual without anyone re-accepting it."""
    if _names_secret(document.get("env") or {}):
        refuse("category=privileged_workflow_level_secret — workflow-level "
               "`env:` is inherited by every job including preflight and "
               "finalize; scope a secret to the job that spends it")

    holders = []
    for job_id, job in (document.get("jobs") or {}).items():
        if _names_secret(job or {}):
            holders.append(str(job_id))
    unexpected = sorted(set(holders) - set(SECRET_BEARING_JOBS))
    if unexpected:
        refuse(f"category=privileged_workflow_secret_in_unexpected_job "
               f"jobs={unexpected} permitted={list(SECRET_BEARING_JOBS)} — "
               "preflight decides whether the run may proceed and finalize runs "
               "on every failure path; neither should have a credential in "
               "scope when it does so")

    referenced = set()
    for text in _expression_strings(document):
        if SECRET_NAME in text:
            referenced.add(SECRET_NAME)
    others = []
    for text in _expression_strings(document):
        if "secrets." not in text and "secrets[" not in text:
            continue
        for token in text.replace("[", ".").replace("]", ".").replace("'", ".").split("."):
            token = token.strip(" }{$")
            if token.isupper() and len(token) > 3 and token != SECRET_NAME:
                others.append(token)
    others = sorted({o for o in others if o not in ("GITHUB_TOKEN",)})
    if others:
        refuse(f"category=privileged_workflow_names_another_secret "
               f"secrets={others} expected={SECRET_NAME!r} — the accepted "
               "residual covers one provider key; a second credential here "
               "widens it without anyone having accepted the wider version")
    return {"secret_bearing_jobs": sorted(holders),
            "named_secrets": sorted(referenced)}


#: The exact permission set the plan grants. Written as a mapping rather than a
#: maximum, because "no more than these" and "exactly these" differ: a workflow
#: that silently drops `statuses: write` would still pass a subset check and then
#: fail at run time, having already spent the provider budget.
#:
#: `checks: read` is here because preflight reads
#: `GET /commits/{sha}/check-runs` to confirm ordinary CI is green on the exact
#: head. An explicit `permissions:` block sets every UNLISTED scope to `none`,
#: so omitting it did not fall back to a default — it revoked the scope, and
#: the first real privileged run refused with
#: `github_api_error where=check-runs http_status=403`. It is a READ scope on a
#: surface the panel already reads by another route, so it widens nothing the
#: lane did not already depend on.
REQUIRED_PERMISSIONS = {
    "contents": "read",
    "actions": "read",
    "checks": "read",
    "pull-requests": "read",
    "statuses": "write",
}


#: The ONE job that may hold a write scope beyond `statuses`, and the ONE extra
#: scope it may hold.
#:
#: `pull-requests: write` exists so the panel can publish its findings as a
#: readable pull-request comment. Statuses carry a decision and 140 characters;
#: they cannot carry which file, which lines, which model, or why — so a
#: governed panel that publishes only statuses is a reviewer nobody can act on,
#: which is how a real refutation becomes "the bot is broken, merge it".
#:
#: Granted at the JOB and never at the workflow, and to one job. The top-level
#: block stays `pull-requests: read`, so `preflight` (which decides whether the
#: run may proceed) and `finalize` (which runs on every failure path) still
#: cannot comment on anything. Widening the top-level block would have been one
#: line shorter and would have handed the capability to all four jobs.
PUBLISHER_JOB = "panel"
PUBLISHER_EXTRA_WRITE = ("pull-requests", "write")


def _permitted_job_write(job_id: str, scope: str, value: str) -> bool:
    """`statuses: write` anywhere; the publisher scope in the publisher job."""
    if scope == "statuses" and value == "write":
        return True
    return (job_id == PUBLISHER_JOB
            and (scope, value) == PUBLISHER_EXTRA_WRITE)


def assert_permissions(document: dict) -> dict:
    """Exactly five scopes at the top, and every job write named and placed.

    An unset block inherits the repository default, which on many repositories
    is write-all — so absence is refused rather than defaulted.

    The job-level rule is an ALLOWLIST keyed by job id, not a set of permitted
    scope names. The difference is the whole control: `pull-requests: write` in
    `finalize` and `pull-requests: write` in `panel` are the same two words and
    a completely different capability surface, and only a rule that reads the
    job id can tell them apart."""
    permissions = document.get("permissions")
    if permissions is None:
        refuse("category=privileged_workflow_permissions_unset — an unset "
               "permissions block inherits the repository default, which may "
               "grant every write scope to a job that holds a provider key")
    if not isinstance(permissions, dict):
        refuse("category=privileged_workflow_permissions_not_mapping")
    got = {str(k): str(v) for k, v in permissions.items()}
    if got != REQUIRED_PERMISSIONS:
        refuse(f"category=privileged_workflow_permissions_mismatch got={got} "
               f"expected={REQUIRED_PERMISSIONS} — `statuses: write` is the only "
               "write scope this panel needs at the workflow level; anything "
               "more is a capability nobody asked for, and anything less fails "
               "after the money is already spent")
    publisher_scopes = []
    for job_id, job in (document.get("jobs") or {}).items():
        job_permissions = (job or {}).get("permissions")
        if job_permissions is None:
            continue
        # GitHub accepts a SCALAR shorthand — `permissions: write-all`,
        # `read-all`, `{}` — and the allowlist below assumes a mapping, so a
        # shorthand crashed this validator with an AttributeError instead of
        # refusing it. `write-all` is the single most dangerous thing that could
        # appear here, and it was the one shape that produced a stack trace
        # rather than a named refusal.
        if not isinstance(job_permissions, dict):
            refuse(f"category=privileged_job_permissions_not_a_mapping "
                   f"job={job_id} got={job_permissions!r} — the scalar "
                   "shorthand grants a whole class at once; this lane names "
                   "every scope it wants and refuses anything it cannot read "
                   "scope by scope")
        writes = sorted(
            f"{k}={v}" for k, v in job_permissions.items()
            if v not in ("read", "none")
            and not _permitted_job_write(str(job_id), str(k), str(v)))
        if writes:
            refuse(f"category=privileged_job_permissions_grant_write "
                   f"job={job_id} scopes={writes} — the only write scopes this "
                   f"lane grants are `statuses: write`, and "
                   f"`{PUBLISHER_EXTRA_WRITE[0]}: {PUBLISHER_EXTRA_WRITE[1]}` "
                   f"in the `{PUBLISHER_JOB}` job alone")
        if (str(job_permissions.get(PUBLISHER_EXTRA_WRITE[0]) or "")
                == PUBLISHER_EXTRA_WRITE[1]):
            publisher_scopes.append(str(job_id))
    if publisher_scopes not in ([], [PUBLISHER_JOB]):
        refuse(f"category=privileged_publisher_scope_in_the_wrong_jobs "
               f"jobs={sorted(publisher_scopes)} expected=[{PUBLISHER_JOB!r}]")
    return {"permissions": got,
            "publisher_write_jobs": sorted(publisher_scopes)}


def validate(*, root: str = ".") -> dict:
    """Every rule over the privileged workflow, as one record.

    Ordered most-serious-first for the same reason `livepolicy.validate_live_workflows`
    is: a refusal stops the walk, so whichever fires is the one the operator
    reads. Credential scope and candidate execution come before shape and
    pinning, because only the first two can hand a key or a shell to a pull
    request."""
    from trustedlane import statusnames
    from trustedlane.workflowfile import assert_actions_pinned

    path = os.path.join(root, ".github", "workflows", WORKFLOW_FILENAME)
    if not os.path.exists(path):
        refuse(f"category=privileged_workflow_missing path={path}")
    document = _read(path)

    declared_name = str(document.get("name") or "")
    if declared_name != WORKFLOW_NAME:
        refuse(f"category=privileged_workflow_name_mismatch "
               f"declared={declared_name!r} expected={WORKFLOW_NAME!r} — the "
               "declared name is what `workflow_run` filters and what the "
               "status policy registers; a mismatch means one of them is "
               "watching a workflow that does not exist")

    return {
        "path": path,
        "name": declared_name,
        # 1. can a pull request reach the key, or a shell?
        **assert_secret_is_scoped(document),
        **assert_no_candidate_checkout(document),
        **assert_no_tree_materialisation(document),
        **assert_no_post_checkout_network_git(document),
        **assert_no_credential_reintroduction(document),
        **assert_no_candidate_artifacts_or_cache(document),
        **assert_no_candidate_execution(document),
        **assert_no_local_or_unpinned_actions(document),
        # 2. is the run started by something trusted?
        **assert_triggers(document),
        # 3. capability surface
        **assert_permissions(document),
        # 4. supply chain
        **assert_actions_pinned(document, name=WORKFLOW_FILENAME),
        # 5. does every published check name mean what an operator would think?
        **statusnames.assert_workflow_statuses_are_registered(
            document, name=WORKFLOW_FILENAME),
        "honest_scope": (
            "this validates the privileged workflow's SHAPE. It says nothing "
            "about what the referenced scripts do once they run, and nothing "
            "about the accepted residual that a pull request's OWN workflows "
            "can reference the repository secret"),
    }
