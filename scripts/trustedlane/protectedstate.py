"""Strict protected-state checks, run BEFORE a credential becomes reachable.

`identity.assert_allowed_default_ref` compares a ref name. `identity.
assert_branch_protection_observed` takes the branch record the API returned.
Neither is enough on its own for D1/D2, because the thing that must be true
before a provider key is reachable is a conjunction:

    the ref is the allowed default ref
    AND the API says that branch is protected
    AND the environment exists and admits only that ref
    AND no repository-level or organization-level secret shadows the
        environment secret

The last one is the subtle one and it is operator prerequisite 6. A
repository-level secret is readable from ANY ref. If one exists under the same
name as the environment secret, the environment's deployment-branch policy is
decoration: the workflow resolves the repository-level value from whatever
branch it runs on, and every other check in this file passes while the
credential is reachable from a ref the policy was written to exclude.

Ordering is part of the contract, not an implementation detail. Every check here
takes an OBSERVATION — what the API actually returned — and the whole point is
that it runs before the job that can spend money. A check that runs after the
key is in the environment has already lost.

Three-state discipline throughout, because these are different failures with
different operator fixes:

    not observed    nobody asked the API           -> refuse, distinctly
    observed false  the API said no                -> refuse, distinctly
    observed true   the API said yes               -> proceed
"""

from __future__ import annotations

from .errors import refuse
from .identity import ALLOWED_DEFAULT_REFS, assert_branch_protection_observed

#: Protection rules that must be in force on the default branch before a
#: credential-bearing workflow may be deployed to it. Names match the GitHub
#: branch-protection / ruleset vocabulary.
REQUIRED_BRANCH_RULES = (
    "required_pull_request_reviews",
    "block_force_pushes",
    "block_deletions",
)


def assert_branch_rules_observed(observed) -> dict:
    """The named rules must each be observed true.

    `"protected": true` alone is not enough: a branch can be "protected" with a
    ruleset that requires nothing. Each rule is checked by name so that a
    missing one is reported as missing rather than folded into a single
    boolean."""
    if observed is None:
        refuse("category=branch_rules_not_observed — nobody asked the API "
               "which rules are in force; a ref name and a `protected` flag do "
               "not say whether force-push is blocked")
    if not isinstance(observed, dict):
        refuse("category=branch_rules_not_object")
    missing = [rule for rule in REQUIRED_BRANCH_RULES if rule not in observed]
    if missing:
        refuse(f"category=branch_rule_not_observed rules={missing} — absent "
               "from the observation is not the same as observed false, and "
               "the operator fixes them differently")
    disabled = [rule for rule in REQUIRED_BRANCH_RULES
                if observed[rule] is not True]
    if disabled:
        refuse(f"category=branch_rule_not_enforced rules={disabled}")
    bypass = observed.get("bypass_actors")
    if bypass is None:
        refuse("category=branch_bypass_actors_not_observed — a ruleset with an "
               "unlisted bypass list is a ruleset of unknown strength")
    if bypass:
        refuse(f"category=branch_rules_have_bypass_actors count={len(bypass)} "
               "— an actor who can bypass the rules can push the credential-"
               "bearing workflow the rules exist to protect; remove them or "
               "record an explicit approved emergency policy")
    return {"rules_enforced": list(REQUIRED_BRANCH_RULES), "bypass_actors": 0}


def assert_environment_admits_only_protected_ref(observed, *,
                                                 expected_name: str) -> dict:
    """The deployment-branch policy is what makes an environment secret safe."""
    if observed is None:
        refuse(f"category=environment_not_observed environment={expected_name}")
    if not isinstance(observed, dict):
        refuse("category=environment_record_not_object")
    name = observed.get("name")
    if name != expected_name:
        refuse(f"category=environment_name_mismatch expected={expected_name!r} "
               f"observed={name!r} — checking a different environment than the "
               "one the workflow names proves nothing about the workflow")
    policy = observed.get("deployment_branch_policy")
    if policy is None:
        refuse(f"category=environment_has_no_deployment_branch_policy "
               f"environment={name} — without one the environment secret is "
               "reachable from any ref that can start the workflow")
    if not isinstance(policy, dict):
        refuse("category=environment_deployment_policy_not_object")
    if policy.get("custom_branch_policies") is not True:
        refuse(f"category=environment_policy_not_custom environment={name} — "
               "`protected_branches: true` admits every protected branch, not "
               "only the default one")
    allowed = observed.get("allowed_branches")
    if allowed is None:
        refuse(f"category=environment_allowed_branches_not_observed "
               f"environment={name}")
    if not isinstance(allowed, (list, tuple)) or not allowed:
        refuse(f"category=environment_admits_no_branch environment={name}")
    outside = [b for b in allowed if f"refs/heads/{b}" not in ALLOWED_DEFAULT_REFS]
    if outside:
        refuse(f"category=environment_admits_unprotected_branch "
               f"environment={name} branches={sorted(outside)}")
    return {"environment": name, "allowed_branches": sorted(allowed),
            "policy": "custom_branch_policies"}


def assert_no_shadowing_secret(*, secret_name: str,
                               repository_secret_names,
                               organization_secret_names) -> dict:
    """Operator prerequisite 6, as a check rather than an attestation.

    A repository-level secret is readable from ANY ref. One under the same name
    as the environment secret makes the deployment-branch policy decoration: the
    workflow resolves the repository-level value from whatever branch it runs
    on, and every other check in this module passes while the credential is
    reachable from a ref the policy was written to exclude.

    Both inventories must be OBSERVED. `None` means nobody listed them, which is
    refused separately from "listed, and the name is absent" — the first is an
    unasked question, the second is an answer."""
    if not isinstance(secret_name, str) or not secret_name:
        refuse("category=shadow_check_secret_name_missing")
    for label, names in (("repository", repository_secret_names),
                         ("organization", organization_secret_names)):
        if names is None:
            refuse(f"category={label}_secret_inventory_not_observed — nobody "
                   f"listed the {label}-level secrets, and a fallback under "
                   "this name would defeat the environment's branch policy "
                   "while every other check still passed")
        if isinstance(names, str) or not hasattr(names, "__iter__"):
            refuse(f"category={label}_secret_inventory_not_a_sequence")
    repo = [str(n) for n in repository_secret_names]
    org = [str(n) for n in organization_secret_names]
    shadows = sorted({n for n in repo + org if n == secret_name})
    if shadows:
        where = []
        if secret_name in repo:
            where.append("repository")
        if secret_name in org:
            where.append("organization")
        refuse(f"category=environment_secret_is_shadowed name={secret_name} "
               f"level={where} — a {'/'.join(where)}-level secret of this name "
               "is readable from any ref, so the environment's deployment-"
               "branch policy no longer constrains who can read the credential")
    return {"secret_name": secret_name, "shadowed": False,
            "repository_secret_count": len(repo),
            "organization_secret_count": len(org)}


def assert_ready_for_credential(*, observed_ref: str, branch_record,
                                branch_rules, environment_record,
                                environment_name: str, secret_name: str,
                                repository_secret_names,
                                organization_secret_names) -> dict:
    """The full conjunction, in order, before any credential is reachable.

    One entry point rather than five call sites, because the failure this
    prevents is a caller that runs four of the five checks. Every argument is an
    observation the caller made against the API; none of it is read from this
    branch, and none of it is inferred from a name."""
    from .identity import assert_allowed_default_ref

    assert_allowed_default_ref(observed_ref)
    protection = assert_branch_protection_observed(branch_record)
    rules = assert_branch_rules_observed(branch_rules)
    environment = assert_environment_admits_only_protected_ref(
        environment_record, expected_name=environment_name)
    shadow = assert_no_shadowing_secret(
        secret_name=secret_name,
        repository_secret_names=repository_secret_names,
        organization_secret_names=organization_secret_names)
    return {
        "ref": observed_ref,
        **protection,
        **rules,
        **environment,
        **shadow,
        "credential_may_be_reachable": True,
        "honest_scope": ("every precondition was checked against an observation "
                         "the caller supplied. This says the configuration is "
                         "right; it does not verify that the observations came "
                         "from the real API, which is why this runs inside the "
                         "trusted lane and not on a candidate branch"),
    }
