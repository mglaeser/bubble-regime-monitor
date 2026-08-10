"""Validate the exact approved engine release, with no route to the provider.

## What this answers

The engine release exists and `governance/midterm-panel-engine-release.json`
now names it. Two questions follow, and only one of them had a check:

    does the binding reproduce?          `release_binding` recomputes it
    is the released document the one     nothing read the asset outside a
    the operator approved?               credential-bearing job

`releaseasset` can answer the second, but until now it only ever ran inside the
privileged panel workflow, one step before the job that receives the provider
key. So the first time anyone would learn that the release asset is unreadable,
mis-digested or relabelled was on the run that spends money. That is the wrong
place to find out, and it is also the wrong place to find out on purpose: the
provider key is installed now, so the cheapest possible check has to be the one
that runs first.

This module is that check, and it runs where no provider call is possible.

## "No provider call" is proved, not asserted

Three independent controls, none of which is a promise:

  1. No provider credential is in scope. Both name families are read out of the
     environment — this lane's `MIDTERM_PANEL_PROVIDER_KEY` and the trusted
     lane's `TRUSTED_VERIFIER_OPENAI_KEY` — because a guard that checked only
     one would read "no provider secret" with the key sitting right there under
     the other. Without a credential a provider call cannot be authenticated.

  2. An egress guard is installed for the whole validation. Every outbound
     connection goes through `socket.create_connection` or `socket.socket
     .connect`; both are wrapped, both record what was asked for, and both
     refuse any host that is not one of the release-asset hosts. Without a
     socket a provider call cannot leave the process.

  3. The guard is DEMONSTRATED, in the same process, at the same moment. A
     connection to the provider host is attempted and must be refused. A guard
     that was installed wrongly — patched onto the wrong module, shadowed by an
     already-imported alias — would pass an inspection and fail this.

The connection ledger is returned. `provider_calls: 0` is then an observation
about a list of hosts that were actually contacted, not a claim about intent.

One consequence worth stating rather than discovering: behind an HTTP proxy,
`urllib` connects to the PROXY, and a proxy host is not a release-asset host, so
the guard refuses it and the validation fails closed. That is the correct
direction — a proxy is a third party in the path of the bytes an operator
approved — and it is why this job runs on a hosted runner with direct egress.

## What a green run here does NOT prove

It does not prove the engine reviews well, that the operator approved the right
digests, or that the panel works. It proves that the exact bytes the governance
document names are the bytes the release serves, that they are internally
sealed, that they describe the mid-term control class rather than a protected
one, and that reading them costs nothing and can reach nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile

from .errors import refuse

#: The hosts a release-asset read may legitimately touch. Taken from
#: `releaseasset` rather than restated: two lists of permitted hosts is two
#: things that can disagree, and the one that would be wrong is the one nobody
#: looks at.
from .releaseasset import PERMITTED_HOSTS

#: The host the guard is proved against. Attempted and required to fail, so
#: "the guard is installed" is a result rather than an inspection.
PROVIDER_PROOF_HOST = "api.openai.com"
PROVIDER_PROOF_PORT = 443

RELEASE_DOCUMENT = os.path.join("governance",
                                "midterm-panel-engine-release.json")


class EgressRefused(OSError):
    """Raised in place of a connection the guard will not permit.

    An `OSError` subclass on purpose: every HTTP client in the standard
    library already handles a connection failure, so a refused host surfaces as
    an ordinary unreachable-host error rather than as a crash inside somebody
    else's retry loop."""


class EgressGuard:
    """Permit connections to the release hosts; refuse and record everything else.

    Both entry points are wrapped. `socket.create_connection` is what
    `http.client` uses and it still knows the HOSTNAME, which is the thing
    worth deciding on; `socket.socket.connect` is the lower-level path, and it
    is wrapped as well so that a client which resolves the name itself and
    connects to an address cannot walk around the check.

    The ledger records every attempt with its verdict. A guard that only
    refused would leave "how many connections did this make, and to what"
    unanswerable, and that question is the whole reason the guard exists."""

    def __init__(self, permitted=None):
        # Read at CALL time, not bound as a default. A default argument is
        # evaluated once when the class is defined, which freezes the host list
        # to whatever it was at import — so a test that narrows
        # `PERMITTED_HOSTS` would still get the full set and would pass for a
        # reason it did not write down. That happened: the refusal it observed
        # came from this container's local HTTP proxy, not from the narrowing.
        self.permitted = tuple(PERMITTED_HOSTS if permitted is None
                               else permitted)
        self.ledger = []
        self._create_connection = None
        self._socket_connect = None
        #: How many permitted `create_connection` calls are on the stack.
        #:
        #: `socket.create_connection` resolves the name and then calls
        #: `sock.connect(sa)` with an ADDRESS — `('140.82.121.6', 443)` — so
        #: the low-level wrapper sees an IP for a connection the hostname
        #: check has already authorised, and refusing it broke the one thing
        #: this guard is supposed to permit. The nesting is what makes the two
        #: wrappers agree: while an authorised named-host connection is in
        #: progress, its own socket connect belongs to it. The low-level
        #: wrapper still refuses everything outside that window, which is the
        #: case it exists for — a client that resolves the name itself and
        #: connects to an address, bypassing `create_connection` entirely.
        #:
        #: Not thread-local, and this is single-threaded on purpose: the
        #: validation makes two sequential requests and nothing else. A
        #: concurrent caller would need a `contextvars` counter, and a guard
        #: that pretended to be concurrency-safe without being it would be
        #: worse than one that says which shape it covers.
        self._authorised_depth = 0
        self._authorising_host = None
        #: `socket.socket` inherits `connect` from `_socket.socket`, so
        #: assigning to it creates a NEW attribute on the subclass rather than
        #: replacing one. Restoring by assignment would leave that shadow in
        #: place forever — harmless today, and exactly the kind of residue that
        #: makes a later "is the guard installed?" question unanswerable. So
        #: whether the attribute was the class's own is recorded, and an
        #: attribute this guard created is deleted rather than overwritten.
        self._connect_was_own = False

    # -- the decision ------------------------------------------------------

    def _verdict(self, host, port, *, via: str) -> bool:
        permitted = host in self.permitted
        self.ledger.append({"host": host, "port": port, "via": via,
                            "permitted": permitted})
        return permitted

    @staticmethod
    def _host_of(address):
        """The host part of whatever a caller passed as an address.

        AF_UNIX addresses are strings and AF_INET6 addresses are 4-tuples;
        neither is a `(host, port)` pair, and treating them as one would either
        crash or — worse — read a permitted-looking value out of the wrong
        slot. An address this cannot decompose is reported as its repr and
        refused, because an address the guard does not understand is not an
        address the guard can vouch for."""
        if isinstance(address, tuple) and len(address) >= 2:
            return str(address[0]), address[1]
        return repr(address), None

    # -- the wrappers ------------------------------------------------------

    def __enter__(self):
        self._create_connection = socket.create_connection
        self._socket_connect = socket.socket.connect
        self._connect_was_own = "connect" in socket.socket.__dict__
        guard = self

        def create_connection(address, *args, **kwargs):
            host, port = guard._host_of(address)
            if not guard._verdict(host, port, via="create_connection"):
                raise EgressRefused(
                    f"category=egress_refused host={host!r} port={port!r} "
                    f"permitted={list(guard.permitted)} — this validation reads "
                    "a release asset and nothing else. A connection anywhere "
                    "else is the thing it exists to make impossible")
            guard._authorised_depth += 1
            guard._authorising_host = host
            try:
                return guard._create_connection(address, *args, **kwargs)
            finally:
                guard._authorised_depth -= 1
                if not guard._authorised_depth:
                    guard._authorising_host = None

        def connect(sock, address):
            host, port = guard._host_of(address)
            if guard._authorised_depth:
                # The resolved address of a name this guard already permitted.
                # Recorded with the host that authorised it, so the ledger says
                # which decision covered it rather than quietly growing an IP
                # nobody approved.
                guard.ledger.append({
                    "host": host, "port": port,
                    "via": f"socket.connect under {guard._authorising_host}",
                    "permitted": True,
                    "authorised_by": guard._authorising_host})
                return guard._socket_connect(sock, address)
            if not guard._verdict(host, port, via="socket.connect"):
                raise EgressRefused(
                    f"category=egress_refused host={host!r} port={port!r} "
                    f"permitted={list(guard.permitted)} — a connection made "
                    "outside `create_connection`, so no permitted hostname "
                    "authorised it")
            return guard._socket_connect(sock, address)

        socket.create_connection = create_connection
        socket.socket.connect = connect
        return self

    def __exit__(self, *exc_info):
        socket.create_connection = self._create_connection
        if self._connect_was_own:
            socket.socket.connect = self._socket_connect
        else:
            del socket.socket.connect
        return False

    # -- the report --------------------------------------------------------

    @property
    def refused(self) -> list:
        return [entry for entry in self.ledger if not entry["permitted"]]

    @property
    def hosts_contacted(self) -> list:
        """The NAMES this guard decided on, not the addresses they resolved to.

        A connection made under an authorisation is listed separately: it was
        permitted by the hostname decision above it, and folding its resolved
        IP in here would read as an extra host somebody approved."""
        return sorted({entry["host"] for entry in self.ledger
                       if entry["permitted"] and not entry.get("authorised_by")})

    @property
    def addresses_reached_under_authorisation(self) -> list:
        return sorted({f"{entry['host']} (via {entry['authorised_by']})"
                       for entry in self.ledger if entry.get("authorised_by")})


def prove_the_guard_refuses_the_provider(guard: EgressGuard) -> dict:
    """Attempt the provider host and require the attempt to fail.

    Inside the guard, so what is proved is the guard that is actually in force
    rather than a second one built for the test. The connection is attempted
    with a short timeout and is expected never to be made — but if the guard
    were absent, a real socket to the provider would be the failure this
    reports, and it would report it having connected to nothing else."""
    before = len(guard.ledger)
    try:
        socket.create_connection((PROVIDER_PROOF_HOST, PROVIDER_PROOF_PORT),
                                 timeout=1)
    except EgressRefused as exc:
        entries = guard.ledger[before:]
        return {"provider_host_probed": PROVIDER_PROOF_HOST,
                "refused": True,
                "refusal": str(exc).split(" — ")[0],
                "ledger_entries": entries}
    except OSError as exc:
        refuse(f"category=egress_guard_not_in_force "
               f"host={PROVIDER_PROOF_HOST} "
               f"exception_class={type(exc).__name__} — the connection failed "
               "for a NETWORK reason, which means the guard did not decide it. "
               "A validation whose egress control is only accidentally "
               "effective proves nothing about the run where the network is up")
    refuse(f"category=egress_guard_did_not_refuse host={PROVIDER_PROOF_HOST} — "
           "a socket to the provider host was opened from the job that is "
           "supposed to be unable to reach it")


def assert_no_provider_credential_is_in_scope(environ: dict) -> dict:
    """Both name families, out of the environment this process actually has."""
    from .dryrun import assert_no_credential_is_present
    return assert_no_credential_is_present(dict(environ))


def load_release_document(*, root: str = ".") -> dict:
    """Read the governance binding, refusing a document with duplicate keys.

    Same discipline as `policystate.load_policy` and for the same reason: a
    document declaring `approved_engine_artifact_sha256` twice parses fine
    under the default loader and silently keeps the last one, so the effective
    approval would depend on file order rather than on review."""
    path = os.path.join(root, RELEASE_DOCUMENT)

    def _no_duplicates(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                refuse(f"category=engine_release_document_duplicate_key "
                       f"key={key!r}")
            seen[key] = value
        return seen

    try:
        with open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"),
                              object_pairs_hook=_no_duplicates)
    except (OSError, UnicodeDecodeError) as exc:
        refuse(f"category=engine_release_document_unreadable path={path} "
               f"exception_class={type(exc).__name__}")
    except json.JSONDecodeError as exc:
        refuse(f"category=engine_release_document_not_json line={exc.lineno}")


def release_config_from_document(document: dict) -> dict:
    """The seven binding fields, in the shape `engine` expects.

    Nothing else is copied across. A config assembled by `dict(document)` would
    carry every restatement in the file into the code that decides, and a
    restatement that decides is no longer a restatement."""
    from .engine import RELEASE_BINDING_FIELDS
    if not isinstance(document, dict):
        refuse("category=engine_release_document_not_an_object")
    return {field: document.get(field) for field in RELEASE_BINDING_FIELDS}


def assert_binding_reproduces(document: dict) -> dict:
    """The recorded binding digest must be RECOMPUTED, never merely read.

    The document carries `engine_release_binding_sha256` so a human has one
    number to check. If the code read that number, the file would be asserting
    its own correctness — the same shape as comparing two stored copies of a
    provenance digest, which is the check `recomputed_provenance_digest` exists
    because it does not work."""
    from .engine import (
        APPROVED_RELEASE,
        assert_release_protection_claim,
        provenance_of,
        release_binding,
    )

    config = release_config_from_document(document)
    claim = assert_release_protection_claim(config)
    provenance = provenance_of(config)
    if provenance != APPROVED_RELEASE:
        missing = sorted(f for f, v in config.items()
                         if v is None or v == "")
        refuse(f"category=engine_release_binding_is_not_approved "
               f"provenance={provenance} missing={missing} — this validation "
               "exists to check an approved release; a partial binding has "
               "nothing to check")
    computed = release_binding(config)["engine_release_binding_sha256"]
    recorded = document.get("engine_release_binding_sha256")
    if not recorded:
        refuse("category=engine_release_binding_digest_not_recorded — the "
               f"document must state `engine_release_binding_sha256`; it is "
               f"{computed} for the seven fields as written")
    if recorded != computed:
        refuse(f"category=engine_release_binding_digest_mismatch "
               f"recorded={str(recorded)[:16]} recomputed={computed[:16]} — the "
               "seven binding fields do not produce the digest the document "
               "claims for them, so one of them was edited without the other")
    return {**config, "engine_release_binding_sha256": computed,
            "provenance": provenance,
            "native_branch_protection": claim["native_branch_protection"],
            "control_class": claim["control_class"]}


def assert_restated_digests_match(document: dict, record: dict) -> dict:
    """The five digests restated in governance must be the released ones.

    The restatement exists so an operator can read the approved numbers without
    downloading anything. A restatement nobody compares is a second source of
    truth waiting to drift, so it is compared against the materialised
    document, in full, and a difference is a refusal rather than a note."""
    #: Imported by name from the PRODUCER so a build that emitted a different
    #: set of digests could not leave this comparison silently checking four of
    #: them. Imported inside the function because every other `midtermpanel`
    #: module reaches into `trustedlane` lazily, and a module-scope import here
    #: would make this the one file that binds the two packages at import time.
    from trustedlane.enginebuild import BUILT_IDENTITY_FIELDS

    restated = document.get("approved_engine_identity_digests")
    if not isinstance(restated, dict):
        refuse("category=engine_release_identity_digests_not_restated — the "
               "governance document must restate the five approved digests")
    differences = sorted(
        field for field in BUILT_IDENTITY_FIELDS
        if restated.get(field) != record.get(field))
    if differences:
        refuse(f"category=engine_release_restated_digests_differ "
               f"fields={differences} — the governance document restates one "
               "set of approved digests and the released identity document "
               "carries another")
    return {field: record[field] for field in BUILT_IDENTITY_FIELDS}


def assert_provenance_reseals(record: dict) -> dict:
    """Re-seal the identity's provenance from its own contents.

    `strict_load_built_identity` already does this, and it is asserted again
    here rather than assumed, because the property this validation reports —
    "the released document is internally sealed" — should be true of the bytes
    THIS run read, not true of whatever the loader happened to check."""
    from trustedlane.enginebuild import recomputed_provenance_digest

    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        refuse("category=engine_identity_provenance_missing")
    recomputed = recomputed_provenance_digest(provenance)
    for where, claimed in (("provenance", provenance.get("provenance_sha256")),
                           ("record", record.get("provenance_sha256"))):
        if claimed != recomputed:
            refuse(f"category=engine_identity_provenance_does_not_reseal "
                   f"where={where} claimed={str(claimed)[:16]} "
                   f"recomputed={recomputed[:16]} — the provenance digest "
                   "describes different contents than the ones it travels with")
    return {"provenance_sha256": recomputed,
            "build_workflow_run_id": provenance.get("build_workflow_run_id"),
            "build_workflow_run_attempt":
                provenance.get("build_workflow_run_attempt"),
            "build_ref": provenance.get("build_ref"),
            "build_head_sha": provenance.get("build_head_sha"),
            "build_ref_protected": provenance.get("build_ref_protected")}


def assert_build_facts_match(document: dict, provenance: dict) -> dict:
    """What governance says the build was, against what the build recorded."""
    stated = document.get("approved_engine_build")
    if not isinstance(stated, dict):
        refuse("category=engine_release_build_facts_not_recorded")
    differences = sorted(
        field for field in ("build_workflow_run_id", "build_workflow_run_attempt",
                            "build_ref", "build_head_sha", "build_ref_protected")
        if stated.get(field) != provenance.get(field))
    if differences:
        refuse(f"category=engine_release_build_facts_differ fields={differences} "
               "— the governance document describes one build and the sealed "
               "provenance describes another")
    return {field: provenance[field] for field in
            ("build_workflow_run_id", "build_workflow_run_attempt",
             "build_ref", "build_head_sha", "build_ref_protected")}


def validate(*, environ: dict, root: str = ".", destination: str,
             token: str) -> dict:
    """The whole no-provider validation, in order, inside the egress guard.

    Order matters and is the argument. The credential check runs first, before
    anything opens a socket; the binding is recomputed before anything is
    downloaded, so a document that does not describe an approved release costs
    no network at all; and the guard proof runs inside the same `with` as the
    download, so what it proves is the guard that was in force for it."""
    from . import REPOSITORY_NUMERIC_ID
    from .engine import assert_identity_is_midterm_grade, source_roles
    from .releaseasset import materialise_release_identity

    credentials = assert_no_provider_credential_is_in_scope(environ)
    document = load_release_document(root=root)
    release = assert_binding_reproduces(document)

    with EgressGuard() as guard:
        proof = prove_the_guard_refuses_the_provider(guard)
        expected_refusals = len(guard.refused)
        try:
            materialised = materialise_release_identity(
                owner="mglaeser", repo="bubble-regime-monitor",
                tag=release["approved_engine_release_tag"], token=token,
                repository_numeric_id=REPOSITORY_NUMERIC_ID,
                expected_sha256=release["approved_engine_identity_sha256"],
                destination=destination)
        except Exception:
            # `EgressRefused` is an `OSError`, and `releaseasset._request`
            # catches `OSError` and reports `release_api_unreachable` — which
            # reads as "GitHub was down" and was, the first time this ran, the
            # guard eating its own permitted traffic. The masking is worth
            # keeping (that refusal deliberately withholds the URL, which can
            # carry a token), so the guard's own ledger is re-read here and the
            # more specific cause is stated on top of the more general one.
            surprises = guard.refused[expected_refusals:]
            if surprises:
                refuse(f"category=release_read_blocked_by_the_egress_guard "
                       f"refused={[(e['host'], e['via']) for e in surprises]} — "
                       "the download was stopped by this validation's own "
                       "guard, not by the network. Any 'unreachable' refusal "
                       "above is a consequence of this one")
            raise

    from trustedlane.enginebuild import strict_load_built_identity
    record = strict_load_built_identity(destination)
    grade = assert_identity_is_midterm_grade(record, release=release)
    provenance = assert_provenance_reseals(record)
    digests = assert_restated_digests_match(document, record)
    build = assert_build_facts_match(document, provenance)

    expected_roles = source_roles(release)
    observed_roles = {role: (record.get("source_roles") or {}).get(role)
                      for role in expected_roles}
    observed_roles = {
        role: value.get("source_commit") if isinstance(value, dict) else value
        for role, value in observed_roles.items()}
    if observed_roles != expected_roles:
        refuse(f"category=engine_release_source_roles_differ "
               f"expected={ {k: v[:12] for k, v in expected_roles.items()} } "
               f"observed={ {k: str(v)[:12] for k, v in observed_roles.items()} }"
               " — the released identity describes a build of different "
               "commits than the ones the operator pinned")

    with open(destination, "rb") as handle:
        observed_document_sha256 = hashlib.sha256(handle.read()).hexdigest()
    if observed_document_sha256 != release["approved_engine_identity_sha256"]:
        refuse("category=engine_release_identity_document_digest_mismatch")

    refused_hosts = sorted({e["host"] for e in guard.refused})
    return {
        "validation_class": "MIDTERM_SINGLE_REPO_NO_PROVIDER_RELEASE_VALIDATION",
        "approved_engine_release_tag": release["approved_engine_release_tag"],
        "engine_release_binding_sha256":
            release["engine_release_binding_sha256"],
        "engine_identity_sha256": observed_document_sha256,
        "engine_identity_state": grade["state"],
        "native_branch_protection": grade["native_branch_protection"],
        "control_class": grade["control_class"],
        "engine_identity_digests": digests,
        "engine_build": build,
        "engine_source_roles": expected_roles,
        "release_asset_id": materialised["asset_id"],
        "provider_calls": 0,
        "generation_calls": 0,
        "provider_credential_variables_checked":
            credentials["credential_variables_checked"],
        "egress_hosts_contacted": guard.hosts_contacted,
        "egress_hosts_refused": refused_hosts,
        "egress_addresses_under_authorisation":
            guard.addresses_reached_under_authorisation,
        "egress_guard_proof": proof,
        "honest_scope": (
            "the exact release asset the governance binding names was read "
            "with GITHUB_TOKEN only, hashed to the approved digest, "
            "strict-loaded, re-sealed from its own provenance, and found to "
            "describe the mid-term control class with no native branch "
            "protection. `provider_calls: 0` and `generation_calls: 0` are "
            "observations: no provider credential was in scope, and the "
            "connection ledger above lists every host this process actually "
            "reached. This is not write-separated evidence, not an independent "
            "attestation, and says nothing about the quality of the engine"),
    }


def main() -> None:
    """Run the validation and print it. `GITHUB_TOKEN` and nothing else."""
    import sys

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        refuse("category=github_token_absent — reading the approved release "
               "asset needs GITHUB_TOKEN; it must never be the provider key")
    root = os.environ.get("GITHUB_WORKSPACE") or "."
    with tempfile.TemporaryDirectory(prefix="midterm-release-") as scratch:
        report = validate(
            environ=dict(os.environ), root=root, token=token,
            destination=os.path.join(scratch, "engine-identity.json"))
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(
                "### Approved engine release — no-provider validation\n\n"
                f"- release tag `{report['approved_engine_release_tag']}`\n"
                f"- binding `{report['engine_release_binding_sha256']}`\n"
                f"- identity `{report['engine_identity_sha256']}`\n"
                f"- state `{report['engine_identity_state']}`, control "
                f"`{report['control_class']}`, native protection "
                f"`{report['native_branch_protection']}`\n"
                f"- provider calls {report['provider_calls']}, generation "
                f"calls {report['generation_calls']}\n"
                f"- hosts contacted: {report['egress_hosts_contacted']}\n"
                f"- hosts refused: {report['egress_hosts_refused']}\n")


if __name__ == "__main__":
    main()
