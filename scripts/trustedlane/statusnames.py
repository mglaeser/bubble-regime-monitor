"""Check-status naming policy — which green means what.

A branch-protection rule requires a status by NAME. It does not know what the
job did, whether it held a credential, or whether it cast a vote. So the name is
the entire interface between "this ran" and "this is authoritative", and a name
that sounds authoritative while the job is inert is a fail-open waiting for
someone to tick a box in a settings page.

That is not hypothetical here. The repository shipped a job literally called
`cross-vendor` whose step is named "Cross-vendor review panel (inactive — no
provider credential here)". It reports `success` in seconds having cast zero
votes, because the provider secrets were removed from it (V-TRUST). If an
operator later marks `cross-vendor` a required status — a completely reasonable
thing to do given the name, and something the programme's own older documents
recommend — the required cross-vendor review would be satisfied, permanently, by
a job that reviews nothing.

So statuses are classified, the classes are disjoint by assertion, and the
inactive class can never be the required one:

    ORDINARY      the deterministic gate; merge authority today
    CONTAINMENT   D0; hosted proof that the gates refuse. Holds no credential.
    INACTIVE      runs, reports, decides nothing. NEVER required.
    TRUSTED       D1/D2. Reserved now, deployable only after operator approval.

The TRUSTED names are declared here before the workflows that use them are
deployable, on purpose. Reserving a name is what stops the inactive job from
drifting into it later, and it gives the operator an exact string to configure
rather than a description to interpret.
"""

from __future__ import annotations

import os

from .errors import refuse

#: The deterministic gate. Authoritative for ordinary correctness, and the sole
#: merge authority while the trusted lane is inactive.
#: `midterm-panel-selftest` is ORDINARY and not MIDTERM_JOB, and the distinction
#: is the trigger rather than the subject. The MIDTERM_JOB checks come from the
#: privileged `workflow_run` panel, so they land on main's commit and can never
#: satisfy a pull request. This one runs on `pull_request` against the candidate
#: head, like any other test job, and requiring it is meaningful.
#:
#: What requiring it means is narrow, and the NAME has to carry that: the panel's
#: own suites — including the whole no-key vertical — pass on this head. It is
#: not a review of the change and no model saw the change. The job was first
#: called `fake-provider vertical (no key)`, which reads on a checks tab like a
#: verdict about the candidate; `assert_workflow_statuses_are_registered` caught
#: it as unregistered, which is this registry doing exactly what it is for.
ORDINARY_STATUSES = ("test (3.12)", "image", "midterm-panel-selftest")

#: D0. Proves containment on the allowed default ref; holds no credential and
#: reviews nothing.
#:
#: NOT candidate-requirable, and the reason is mechanical rather than a matter of
#: trust: D0 triggers on `workflow_dispatch` and `push` to main under path
#: filters. It never runs on `pull_request`, so it never reports a status on a
#: candidate head. Requiring it would leave every PR waiting forever for a check
#: that cannot arrive — the same end state as requiring the dispatch-only
#: `probe`, reached from a different direction.
#:
#: The first version of this file classified it CONTAINMENT and put it in
#: `require_now` with the comment "safe to require". Safe, and useless: a status
#: that never reports is not a gate, it is a deadlock. D0 remains valuable as
#: protected-ref evidence; it is simply not a PR check.
CONTAINMENT_STATUSES = ("d0-containment",)

#: Runs and reports a documented residual. Requiring one of these would make a
#: no-vote success satisfy a review requirement.
INACTIVE_STATUSES = ("independent-verify-inactive",)

#: Reserved for the credential-bearing lane. Neither is deployable yet — D1 and
#: D2 live as `.yml.template` — and the names exist here so that the inactive
#: job cannot later be renamed into one of them without this policy refusing.
TRUSTED_STATUSES = ("trusted-verifier-count", "trusted-cross-vendor-review")

#: D1/D2 each embed their own containment job. It must NOT be called
#: `d0-containment`: two live workflows declaring the same check name make the
#: required-status setting ambiguous — the operator picks one string and gets
#: whichever workflow GitHub matched. Same defect class as the inactive name,
#: reached from the other direction.
TRUSTED_CONTAINMENT_STATUSES = ("d1-containment-gate", "d2-containment-gate")

#: The mid-term single-repository panel (`scripts/midtermpanel/`).
#:
#: Requirable, and for exactly the same mechanical reason TRUSTED is: the panel
#: publishes these two contexts EXPLICITLY onto the candidate head SHA, so they
#: can satisfy a check on a pull request. A `workflow_run` job's own check lands
#: on the default branch and never would.
#:
#: Requirable is not the same as authoritative, and the difference is the whole
#: reason these are a separate class instead of extra TRUSTED entries. A
#: mid-term panel runs from a repository-scoped secret in the repository it is
#: reviewing. It is not write-separated, no operator prerequisite is recorded,
#: and a pull request that edits a `pull_request` workflow may be able to read
#: the same key. Requiring `midterm-panel-review` means "a panel ran and did not
#: refuse"; it does not mean what `trusted-cross-vendor-review` is reserved to
#: mean, and `assert_midterm_and_trusted_never_collide` keeps the two from ever
#: becoming the same string.
MIDTERM_STATUSES = ("midterm-panel-count", "midterm-panel-review")

#: The privileged panel workflow's own job checks.
#:
#: Never requirable, for the `d1-containment-gate` reason: the panel runs via
#: `workflow_run` from the default branch, so these checks are associated with
#: main's commit rather than with the candidate head. Requiring one would leave
#: every pull request waiting on a check that never arrives there.
MIDTERM_JOB_STATUSES = ("midterm-preflight", "midterm-count",
                        "midterm-panel", "midterm-finalize")

#: Operator-dispatched diagnostics. `workflow_dispatch` only, so they never run
#: on a pull request — requiring one would leave every PR permanently pending on
#: a check that cannot start. Not requirable for a different reason than
#: INACTIVE: an inactive check passes when it should not, a diagnostic never
#: reports at all.
#: `midterm-panel-rerun` is DIAGNOSTIC for the same mechanical reason as
#: `probe`, not because of anything about trust: it is `workflow_dispatch` only,
#: so it never runs on a pull request and never reports a status on a candidate
#: head. Requiring it would leave every PR permanently pending on a check that
#: cannot start.
#:
#: It exists because the PRIVILEGED workflow may not have a dispatch trigger at
#: all — a dispatched run executes against a selected ref, and that workflow's
#: checkouts name no ref. This one holds no secret and only asks CI to run
#: again, which is what starts a panel through the one safe trigger.
DIAGNOSTIC_STATUSES = ("probe", "midterm-panel-rerun")

#: R10. The protected engine build, which produces what the trusted lane
#: trusts. Its own class rather than DIAGNOSTIC: a diagnostic reports on
#: something, and this one MAKES the artifact and the identity record D1 and D2
#: read. Never requirable for the same mechanical reason as the others — it is
#: `workflow_dispatch` only, so it never reports on a candidate head, and
#: requiring it would leave every pull request waiting on a check that cannot
#: start.
#:
#: There is a second reason worth stating, because it is the one a reader
#: would otherwise supply for themselves and get wrong: a green build says the
#: artifact was PRODUCED, not that anyone approved it. Approval of the five
#: digests is operator prerequisite 14, out of band, and `runtimebinding`
#: compares it to the artifact the run opened.
BUILD_STATUSES = ("build-engine",)

STATUS_CLASSES = {
    "ORDINARY": ORDINARY_STATUSES,
    "CONTAINMENT": CONTAINMENT_STATUSES,
    "INACTIVE": INACTIVE_STATUSES,
    "TRUSTED": TRUSTED_STATUSES,
    "TRUSTED_CONTAINMENT": TRUSTED_CONTAINMENT_STATUSES,
    "MIDTERM": MIDTERM_STATUSES,
    "MIDTERM_JOB": MIDTERM_JOB_STATUSES,
    "DIAGNOSTIC": DIAGNOSTIC_STATUSES,
    "BUILD": BUILD_STATUSES,
}

#: Classes an operator may configure as a required status ON A CANDIDATE PULL
#: REQUEST once the corresponding phase is live.
#:
#: Only two, and every exclusion is mechanical:
#:   INACTIVE            reports success having reviewed nothing
#:   DIAGNOSTIC          dispatch-only, never reports on a PR head
#:   CONTAINMENT         D0: push/dispatch only, never reports on a PR head
#:   BUILD               dispatch-only, and a green build is production, not
#:                       approval
#:   TRUSTED_CONTAINMENT internal jobs of a main-dispatched trusted run; their
#:                       checks land on main's commit, not the candidate head
#:
#: TRUSTED is requirable only because D1/D2 publish those contexts EXPLICITLY on
#: the candidate SHA (see statuspublish.py). The workflow job's own check lands
#: on main and would never satisfy a PR — reserving a job name is not enough.
REQUIRABLE_CLASSES = ("ORDINARY", "TRUSTED", "MIDTERM")

#: Everything that must never appear in a required-status list, for either
#: reason. Derived rather than written out, so adding a class cannot forget it.
NEVER_REQUIRABLE_CLASSES = tuple(
    label for label in STATUS_CLASSES if label not in REQUIRABLE_CLASSES)


def all_statuses() -> tuple:
    return tuple(name for names in STATUS_CLASSES.values() for name in names)


def class_of(status: str):
    for label, names in STATUS_CLASSES.items():
        if status in names:
            return label
    return None


def assert_classes_are_disjoint() -> dict:
    """No status may belong to two classes.

    The failure this prevents is a rename that quietly promotes: move
    `independent-verify-inactive` into TRUSTED_STATUSES and, without this check,
    the policy would happily report it as both inactive and trusted, and every
    downstream question ("is this requirable?") would depend on dict order."""
    seen = {}
    for label, names in STATUS_CLASSES.items():
        for name in names:
            if name in seen:
                refuse(f"category=status_name_in_two_classes status={name!r} "
                       f"classes={[seen[name], label]} — a status that is both "
                       "inactive and trusted is whichever one the caller "
                       "happens to ask about")
            seen[name] = label
    return {"statuses": len(seen), "classes": len(STATUS_CLASSES)}


def assert_inactive_and_trusted_never_collide() -> dict:
    """The regression test the contract asks for, as an assertion.

    Stated separately from disjointness because this is the specific pair that
    matters: an inactive status satisfying a trusted requirement is the
    fail-open. Disjointness would also be violated by a harmless ORDINARY /
    CONTAINMENT overlap; this one is never harmless."""
    overlap = sorted(set(INACTIVE_STATUSES) & set(TRUSTED_STATUSES))
    if overlap:
        refuse(f"category=inactive_status_is_also_trusted overlap={overlap} — "
               "an inactive no-vote job must never be able to satisfy a trusted "
               "review requirement")
    for inactive in INACTIVE_STATUSES:
        for trusted in TRUSTED_STATUSES:
            if inactive.strip().lower() == trusted.strip().lower():
                refuse(f"category=inactive_status_matches_trusted_case_"
                       f"insensitively inactive={inactive!r} trusted={trusted!r}")
    return {"inactive": list(INACTIVE_STATUSES),
            "trusted": list(TRUSTED_STATUSES), "collisions": 0}


def assert_midterm_and_trusted_never_collide() -> dict:
    """A mid-term panel status must never become a trusted one.

    Same shape as the inactive/trusted check and a different failure. The
    inactive job's danger is that it reviews nothing; the mid-term panel's danger
    is the opposite — it reviews something real, with real models, and produces a
    result an operator will reasonably believe. What it does NOT have is write
    separation: it runs from a repository-scoped secret in the repository under
    review, with no operator prerequisite recorded and no protected environment.

    So the risk is not that someone requires `midterm-panel-review` — that is a
    legitimate thing to require. The risk is that the two name sets converge,
    by a rename or by someone deciding the mid-term panel has "become" trusted,
    and a requirement written for a write-separated reviewer is then satisfied by
    one that is not. Reserving the strings is what stops that being a one-line
    diff nobody reads twice."""
    overlap = sorted(set(MIDTERM_STATUSES) & set(TRUSTED_STATUSES))
    if overlap:
        refuse(f"category=midterm_status_is_also_trusted overlap={overlap} — a "
               "single-repository panel using a repository-scoped secret must "
               "never satisfy a requirement reserved for a write-separated "
               "reviewer")
    for midterm in MIDTERM_STATUSES:
        for trusted in TRUSTED_STATUSES:
            if midterm.strip().lower() == trusted.strip().lower():
                refuse("category=midterm_status_matches_trusted_case_"
                       f"insensitively midterm={midterm!r} trusted={trusted!r}")
    for midterm in MIDTERM_STATUSES:
        if midterm.strip().lower().startswith("trusted"):
            refuse(f"category=midterm_status_claims_trusted_prefix "
                   f"status={midterm!r} — the name is the entire interface "
                   "between 'this ran' and 'this is authoritative'")
    return {"midterm": list(MIDTERM_STATUSES),
            "trusted": list(TRUSTED_STATUSES), "collisions": 0}


def assert_only_requirable_statuses(required) -> dict:
    """Refuse any status whose class is not candidate-requirable.

    Derived from REQUIRABLE_CLASSES rather than enumerating the bad classes.
    The first version checked INACTIVE and DIAGNOSTIC by name, so when D0 moved
    out of the requirable set it kept accepting `d0-containment` — the
    enumerate-the-hazards mistake, committed inside the module written to stop
    committing it. Every non-requirable class is now refused because it is not
    on the allowlist, and the message says which class and why.

    Takes the list an operator configured or proposes to configure. The lane
    holds no credential and cannot read branch protection, so this is a check
    over an observed value, not an enforcement — and the operator packet says so
    rather than implying the code guarantees it."""
    if required is None:
        refuse("category=required_status_list_not_observed — nobody supplied "
               "the configured list; the lane must not assume it is clean")
    if isinstance(required, str) or not hasattr(required, "__iter__"):
        refuse("category=required_status_list_not_a_sequence")
    names = [str(n) for n in required]

    unknown = sorted(n for n in names if class_of(n) is None)
    if unknown:
        refuse(f"category=required_status_not_in_registry statuses={unknown} — "
               "a required status this policy does not know about is a status "
               "nobody classified; add it to STATUS_CLASSES first")

    why = branch_protection_instructions()["why_never"]
    offenders = [(n, class_of(n)) for n in names
                 if class_of(n) not in REQUIRABLE_CLASSES]
    if offenders:
        detail = "; ".join(f"{n} ({cls}): {why.get(n, 'not candidate-requirable')}"
                           for n, cls in sorted(offenders))
        refuse(f"category=status_is_not_candidate_requirable "
               f"statuses={sorted(n for n, _ in offenders)} — {detail}")
    return {"required": sorted(names),
            "classes": sorted({class_of(n) for n in names})}


#: Kept as the historical name; the check is now class-derived.
assert_no_inactive_status_is_required = assert_only_requirable_statuses


def _matrix_suffixes(job: dict, *, where: str) -> list:
    """The ` (v1, v2)` suffixes a matrix job appends to its check name.

    A matrix job does NOT publish its job id. `jobs.test` with
    `matrix.python-version: ["3.12"]` publishes `test (3.12)` — which is exactly
    the string an operator types into branch protection, and exactly the string
    reading the job id would miss. Caught by this policy refusing `test` as
    unregistered on its first run: the job id was a proxy for the published
    name, and for matrix jobs the two differ.

    Only the plain cartesian form is resolved. `include`/`exclude` change the
    published names in ways that depend on GitHub's expansion order, so they are
    refused rather than guessed — a wrong name here is a required status that
    silently matches nothing."""
    strategy = job.get("strategy") or {}
    matrix = strategy.get("matrix")
    if not matrix:
        return [""]
    if not isinstance(matrix, dict):
        refuse(f"category=matrix_not_a_mapping where={where}")
    unsupported = sorted(k for k in matrix if k in ("include", "exclude"))
    if unsupported:
        refuse(f"category=matrix_include_exclude_unresolved where={where} "
               f"keys={unsupported} — the published check name depends on how "
               "these expand; resolve it explicitly rather than guessing a "
               "required-status string that may match nothing")
    axes = []
    for key in matrix:
        values = matrix[key]
        if not isinstance(values, list) or not values:
            refuse(f"category=matrix_axis_not_a_nonempty_list where={where} "
                   f"axis={key}")
        axes.append([str(v) for v in values])
    combos = [[]]
    for axis in axes:
        combos = [combo + [value] for combo in combos for value in axis]
    return [f" ({', '.join(combo)})" for combo in combos]


def check_names(document: dict) -> list:
    """The check names a workflow will actually publish.

    GitHub names a check after the job's `name:` when present and the job id
    otherwise, then appends the matrix values. Both halves matter: reading only
    the id misses a job that renames itself into a reserved string, and ignoring
    the matrix misses that `test` publishes as `test (3.12)`."""
    names = []
    for job_id, job in (document.get("jobs") or {}).items():
        job = job or {}
        declared = job.get("name")
        base = str(declared) if declared else str(job_id)
        for suffix in _matrix_suffixes(job, where=str(job_id)):
            names.append(f"{base}{suffix}")
    return names


def assert_workflow_statuses_are_registered(document: dict, *,
                                            name: str = "") -> dict:
    """Every check a workflow publishes must be a classified name.

    Refuse-by-default: an unregistered check name is refused rather than
    ignored, because "we did not think about this one" is exactly the state in
    which `cross-vendor` sat for the whole life of the programme."""
    published = check_names(document)
    unknown = sorted(n for n in published if class_of(n) is None)
    if unknown:
        refuse(f"category=unregistered_check_name workflow={name} "
               f"statuses={unknown} — every published check name must be "
               "classified in statusnames.STATUS_CLASSES so an operator can "
               "tell what requiring it would mean")
    return {"published": published,
            "classes": sorted({class_of(n) for n in published})}


def assert_no_duplicate_check_names(documents: dict) -> dict:
    """Two workflows must not publish the same check name.

    A required-status setting is one string. If D0 and D1 both publish
    `d0-containment`, the operator configures one name and gets whichever
    workflow GitHub matched — and the one that matched may be the one that did
    not run. `documents` maps workflow filename -> parsed document."""
    owners = {}
    clashes = []
    for filename, document in sorted(documents.items()):
        for published in check_names(document):
            if published in owners:
                clashes.append(f"{published!r}: {owners[published]} and {filename}")
            else:
                owners[published] = filename
    if clashes:
        refuse(f"category=duplicate_check_name_across_workflows "
               f"clashes={clashes} — a required status is one string; two "
               "workflows publishing it makes the requirement ambiguous")
    return {"check_names": len(owners),
            "owners": {k: owners[k] for k in sorted(owners)}}


def branch_protection_instructions() -> dict:
    """The exact strings an operator should and should not configure.

    A description ("require the cross-vendor review") is what produced this
    defect. These are literals, generated from the same constants the code
    asserts on, so the instruction cannot drift from the policy."""
    never = sorted({n for c in NEVER_REQUIRABLE_CLASSES
                    for n in STATUS_CLASSES[c]})
    return {
        "require_now": list(ORDINARY_STATUSES),
        "require_after_d1": [TRUSTED_STATUSES[0]],
        "require_after_d2": [TRUSTED_STATUSES[1]],
        "never_require": never,
        "why_never": {
            "independent-verify-inactive":
                "reports a documented residual having cast zero votes; "
                "requiring it makes a no-review success satisfy a review "
                "requirement",
            "d0-containment":
                "triggers on push and workflow_dispatch only, never on "
                "pull_request, so it never reports on a candidate head; "
                "requiring it deadlocks every PR",
            "probe":
                "workflow_dispatch only, so it never reports on a candidate "
                "head; requiring it deadlocks every PR",
            "midterm-panel-rerun":
                "workflow_dispatch only, so it never reports on a candidate "
                "head; requiring it deadlocks every PR. It exists because the "
                "PRIVILEGED panel workflow may not have a dispatch trigger at "
                "all — a dispatched run executes against a ref the dispatcher "
                "selects, and that workflow's checkouts name no ref — so the "
                "rerun lives in a workflow that holds no secret and only asks "
                "ordinary CI to run again",
            "d1-containment-gate":
                "internal job of a main-dispatched trusted run; its check "
                "lands on main's commit, not on a candidate head",
            "d2-containment-gate":
                "internal job of a main-dispatched trusted run; its check "
                "lands on main's commit, not on a candidate head",
            "build-engine":
                "workflow_dispatch only, so it never reports on a candidate "
                "head; and a green build says the engine artifact was "
                "PRODUCED, never that anyone approved it — approval of its "
                "five digests is operator prerequisite 14, out of band",
            **{
                job: ("internal job of the mid-term panel, which runs via "
                      "workflow_run from the default branch; its check lands on "
                      "main's commit, not on the candidate head. The panel "
                      "publishes `midterm-panel-count` and "
                      "`midterm-panel-review` onto the candidate SHA — require "
                      "those instead")
                for job in MIDTERM_JOB_STATUSES
            },
        },
        "require_midterm_panel": list(MIDTERM_STATUSES),
        "midterm_is_not_trusted": (
            "`midterm-panel-*` may be required and means 'a three-model panel "
            "ran on this exact head and did not refuse'. It does NOT mean what "
            "`trusted-*` is reserved to mean: the panel is not write-separated, "
            "holds a repository-scoped secret in the repository it reviews, and "
            "rests on no recorded operator prerequisite"),
        "trusted_contexts_must_be_published_on_the_candidate_sha": True,
        "honest_scope": ("this is the instruction, not the enforcement — the "
                         "lane holds no credential and cannot read or set "
                         "branch protection; pass the configured list to "
                         "assert_no_inactive_status_is_required to check it"),
    }


def validate_status_policy(*, root: str = ".") -> dict:
    """Whole-policy check over every workflow the repository carries.

    Covers the live directory AND the D1/D2 templates: a template is not live,
    but it is what gets deployed, and a name collision introduced there would
    only be discovered at deployment time, on the protected ref, with a
    credential attached."""
    from . import workflowfile
    from .livepolicy import _read, live_workflow_paths

    assert_classes_are_disjoint()
    assert_inactive_and_trusted_never_collide()
    assert_midterm_and_trusted_never_collide()

    documents = {}
    for path in live_workflow_paths(root=root):
        documents[os.path.basename(path)] = _read(path)
    for template in (workflowfile.D1_FILE, workflowfile.D2_FILE):
        path = os.path.join(root, workflowfile.WORKFLOW_DIR, template)
        if os.path.exists(path):
            documents[template] = _read(path)

    per_workflow = {}
    for filename, document in sorted(documents.items()):
        per_workflow[filename] = assert_workflow_statuses_are_registered(
            document, name=filename)
    duplicates = assert_no_duplicate_check_names(documents)
    return {
        "workflows": per_workflow,
        **duplicates,
        "instructions": branch_protection_instructions(),
        "honest_scope": ("names are classified and unique across workflows; "
                         "whether branch protection actually requires the right "
                         "ones is an operator setting this cannot observe"),
    }
