"""Obtaining and using the provider capability — the mid-term lane's own seam.

## Why this does not call the trusted lane

`trustedlane.transport.read_credential` gates on `phases.IMPLEMENTED_PHASE`,
which is `D0`, and refuses. The tempting fix is to raise that constant. It is the
wrong fix and it is worth being explicit about why: `IMPLEMENTED_PHASE` is not a
feature flag, it is the statement "this deployment has a protected environment,
an operator-approved digest-pinned engine, and an environment-only credential."
None of those is true here. Raising it to make one function return would activate
D1 and D2 across the whole trusted lane on the strength of a mid-term
architecture that has none of their preconditions.

So the mid-term lane obtains its own credential, from its own environment
variable, with its own refusal — and the trusted lane keeps refusing, which is
the correct answer for the trusted lane.

## The transports speak the ENGINE's protocol, not one of our own

The first version of this module invented a signature —
`post(*, model, system, user) -> dict` — and nothing in the engine ever calls
that. `verifier.counting.count_input_tokens` and `verifier.executor.execute_batch`
both call `transport.post(path, body, timeout=...)` and expect
`(status: int, body: bytes)` back, and both read `transport.source` to decide
whether the result may be represented as provider evidence at all.

A transport with the wrong signature does not fail loudly at the seam; it fails
at the first real call, in the job holding the credential, after the count has
been paid for. So the protocol here is the engine's:

    post(path: str, body: bytes, *, timeout: int | None) -> (int, bytes)
    source: "PROVIDER" | "MOCK_NOT_PROVIDER"

## Counting and generating are different capabilities

They are separately approved and separately priced, so they are separately
transported. `LiveCountTransport` can reach `/v1/responses/input_tokens` and
nothing else; `LiveGenerationTransport` can reach `/v1/responses` and nothing
else. A single object that could do both would make "this run only counted" a
property of the call sites rather than of the capability the run was handed —
and call sites are what change.

The path is checked against the transport's own allowlist on every call, so a
count run that somehow reached the generation code path refuses at the socket
rather than at the accounting afterwards.

## The stand-ins are the engine's, not ours

`verifier.counting.MockCountTransport` and
`verifier.executor.MockGenerationTransport` already produce exactly the
envelopes the engine's own validators accept — including the challenge echo, the
per-lens phrasing the anti-canned gate needs, and the usage keys. Writing a
second fake here would be writing a first implementation of a slightly different
protocol, and every test built on it would pass while proving nothing about the
real one. `EngineStandInTransport` wraps the engine's object and adds only what
the lane needs: a path allowlist and call accounting.

## The seam, and why every dangerous thing is a parameter

`publish`, `count` and `execute` all take their transport as an argument.
Nothing in this package reaches out and gets a network connection for itself.
That shape is inherited deliberately from the trusted lane, where it exists so
the gate can sit on OBTAINING a capability rather than on using it: a function
that fetches its own credential cannot be tested without one and cannot be
stopped from using one.

The practical payoff is `NoProviderTransport`. Phase A has to prove that a full
dry run makes ZERO provider calls, and the only proof that means anything is a
transport that would raise if anyone tried. Counting calls after the fact tells
you what happened; a transport that cannot make one tells you what is possible.
"""

from __future__ import annotations

import json
import os

from . import PANEL_MODELS
from .errors import refuse

#: The environment variable the workflow populates from the repository secret.
#:
#: NOT the same name as the secret itself. The workflow maps
#: `secrets.TRUSTED_VERIFIER_OPENAI_KEY` onto this variable, and the indirection
#: is deliberate: `trustedlane.runtimebinding` checks the ENVIRONMENT of a
#: trusted run for the presence of a variable named after the trusted secret, and
#: a mid-term process exporting that exact name into its own environment would
#: look, to any such check, like a trusted runner that had obtained a trusted
#: credential.
PROVIDER_KEY_ENV = (
    "MIDTERM_PANEL_PROVIDER_KEY"  # noqa: S105 - a NAME; pragma: allowlist secret
)

#: The two paths, by their engine names. Duplicated here as constants ONLY so
#: that the allowlists can be written before an engine is loaded;
#: `assert_paths_match_engine` proves at runtime that they still agree with the
#: engine actually in use, which is the check that survives an upstream rename.
COUNT_PATH = "/v1/responses/input_tokens"
GENERATION_PATH = "/v1/responses"

#: The engine's two declared source labels, by exact string.
#: `verifier.counting.transport_source` refuses anything else, and a transport
#: that failed to declare would otherwise be treated as the provider by default
#: — the inversion that module exists to prevent.
SOURCE_PROVIDER = "PROVIDER"
SOURCE_MOCK = "MOCK_NOT_PROVIDER"


def read_provider_key(environ=None) -> str:
    """The ONLY place the mid-term panel obtains a provider credential.

    Absence is refused rather than defaulted to a disabled mode. A panel that
    silently degrades to "no key, so no findings" publishes a green review having
    reviewed nothing — the exact shape of the inactive-check defect that
    `statusnames.py` exists for, reached from a different direction.

    The refusal names the variable and never its value, and it does not report
    the length either: a length is a small oracle and there is no reason to give
    one away in a log."""
    environ = os.environ if environ is None else environ
    key = environ.get(PROVIDER_KEY_ENV)
    if not key or not isinstance(key, str) or not key.strip():
        refuse(f"category=midterm_provider_key_absent variable={PROVIDER_KEY_ENV} "
               "— the panel refuses rather than degrading to a review that "
               "reports success having called nothing")
    return key


def assert_model_is_governed(model: str) -> str:
    """Only the three governed models, by exact name.

    Not a prefix check. `gpt-5.6-solaris` shares a prefix with the required
    approver and is a different model; `independent_verify.model_matches`
    documents that exact edge and this refuses the whole class of it by
    demanding an exact match against the governed tuple."""
    if model not in PANEL_MODELS:
        refuse(f"category=model_not_governed model={model!r} "
               f"governed={list(PANEL_MODELS)} — the panel's composition is "
               "policy, and a model nobody approved is a voice nobody approved")
    return model


def assert_paths_match_engine(engine: dict) -> dict:
    """The two path constants must be the engine's own, checked at runtime.

    A lane constant that has drifted from the engine's does not fail: the
    allowlist would simply permit a path the engine never sends and refuse the
    one it does, which reads as a transport error rather than as the
    configuration mistake it is."""
    counting = engine["modules"]["verifier.counting"]
    executor = engine["modules"]["verifier.executor"]
    observed = {"count": counting.COUNT_PATH,
                "generation": executor.GENERATION_PATH}
    expected = {"count": COUNT_PATH, "generation": GENERATION_PATH}
    if observed != expected:
        refuse(f"category=transport_paths_disagree_with_engine "
               f"observed={observed} lane={expected} — the path allowlist is "
               "what keeps a count transport off the generation endpoint, and "
               "an allowlist naming paths the engine does not use permits "
               "nothing and protects nothing")
    if {counting.SOURCE_PROVIDER, counting.SOURCE_MOCK} != {SOURCE_PROVIDER,
                                                            SOURCE_MOCK}:
        refuse("category=transport_source_labels_disagree_with_engine — the "
               "engine decides what may be represented as provider evidence "
               "by comparing these exact strings")
    return {"paths_match_engine": True, **observed}


def _assert_path_permitted(path, *, permitted, transport_class: str) -> str:
    """The allowlist, applied before anything is sent.

    Exact match against a tuple, never a prefix: `/v1/responses/input_tokens`
    starts with `/v1/responses`, so a prefix test on a count transport would
    permit the generation endpoint — which is the one that costs money."""
    if path not in permitted:
        refuse(f"category=transport_path_not_permitted path={path!r} "
               f"permitted={list(permitted)} transport={transport_class} — "
               "counting and generating are separately approved capabilities, "
               "and this transport holds only one of them")
    return path


def _assert_body_is_bytes(body, *, transport_class: str) -> bytes:
    if not isinstance(body, (bytes, bytearray)):
        refuse(f"category=transport_body_not_bytes "
               f"observed={type(body).__name__} transport={transport_class} — "
               "the engine hashes the exact bytes it counted and re-sends "
               "those bytes; encoding here would make the two differ")
    return bytes(body)


class NoProviderTransport:
    """Refuses every call. The Phase-A dry-run proof.

    Used where the requirement is "prove no provider call happened". A counter
    that reports zero proves the calls that were made; this proves the calls that
    *could* be made, which is the claim Phase A actually needs.

    Declares `MOCK_NOT_PROVIDER` so that if a run somehow got past the refusal,
    nothing downstream could label the result provider evidence either.
    """

    kind = "NO_PROVIDER"
    source = SOURCE_MOCK

    def __init__(self):
        self.attempts = []
        self.calls = 0

    def post(self, path, body, *, timeout=None):
        self.attempts.append({"path": path,
                              "bytes": len(body) if body else 0})
        refuse(f"category=provider_call_attempted_in_no_provider_mode "
               f"path={path!r} — this run is a dry run and must make no "
               "provider call; the attempt is recorded rather than served")

    @property
    def call_count(self) -> int:
        return self.calls


class EngineStandInTransport:
    """The ENGINE's own stand-in, wrapped with a path allowlist and accounting.

    Deliberately not a fake of its own. The engine's mocks emit the envelopes
    the engine's own validators accept; a second fake written here would be a
    first implementation of a subtly different protocol, and the vertical tests
    built on it would prove that the lane works against our fake rather than
    against the engine.

    `configuration()` is forwarded when the wrapped object has one, because the
    execution report records exactly how the stand-in was configured — without
    it, two runs that behaved differently would reconcile.
    """

    kind = "FAKE_PROVIDER"

    def __init__(self, inner, *, permitted_paths):
        declared = getattr(inner, "source", None)
        if declared != SOURCE_MOCK:
            refuse(f"category=stand_in_transport_not_declared_mock "
                   f"source={declared!r} transport={type(inner).__name__} — a "
                   "wrapper cannot make an undeclared transport safe, and a "
                   "stand-in that declares PROVIDER would become provider "
                   "evidence by omission")
        self._inner = inner
        self.permitted_paths = tuple(permitted_paths)
        self.calls = []

    source = SOURCE_MOCK

    def post(self, path, body, *, timeout=None):
        _assert_path_permitted(path, permitted=self.permitted_paths,
                               transport_class=type(self).__name__)
        _assert_body_is_bytes(body, transport_class=type(self).__name__)
        self.calls.append({"path": path, "bytes": len(body),
                           "timeout": timeout})
        return self._inner.post(path, body, timeout=timeout)

    def configuration(self) -> dict:
        if hasattr(self._inner, "configuration"):
            return {**self._inner.configuration(),
                    "wrapped_by": type(self).__name__,
                    "permitted_paths": list(self.permitted_paths)}
        return {"transport_class": type(self._inner).__name__,
                "source": SOURCE_MOCK,
                "configuration_declared": False,
                "wrapped_by": type(self).__name__,
                "permitted_paths": list(self.permitted_paths),
                "honest_scope": "this stand-in does not describe its own "
                                "behaviour, so the report cannot state what "
                                "it was configured to do"}

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def paths_called(self) -> list:
        return sorted({call["path"] for call in self.calls})


def engine_stand_in_count_transport(engine: dict, **kwargs
                                    ) -> EngineStandInTransport:
    """The engine's `MockCountTransport`, restricted to the count path."""
    assert_paths_match_engine(engine)
    counting = engine["modules"]["verifier.counting"]
    return EngineStandInTransport(counting.MockCountTransport(**kwargs),
                                  permitted_paths=(COUNT_PATH,))


def engine_stand_in_generation_transport(engine: dict, **kwargs
                                         ) -> EngineStandInTransport:
    """The engine's `MockGenerationTransport`, restricted to the generation
    path."""
    assert_paths_match_engine(engine)
    executor = engine["modules"]["verifier.executor"]
    return EngineStandInTransport(executor.MockGenerationTransport(**kwargs),
                                  permitted_paths=(GENERATION_PATH,))


# ------------------------------------------------- the live capabilities ----
#
# NOT implemented here. `trustedlane.counttransport.TrustedCountTransport` and
# `trustedlane.generationtransport.TrustedGenerationTransport` already are
# exactly what §7 describes: the engine's `post(path, body, *, timeout)`
# signature, one endpoint each with the other refused by name, a declared
# `source` checked against the engine's own spelling, an endpoint-agreement
# check that fires at construction rather than as three retries and a "retry
# exhausted", and an operator ceiling the transport itself enforces.
#
# Writing a second pair here would be the mistake this package has already made
# once — a first implementation of subtly different rules, tested against
# itself. What the mid-term lane genuinely needs to add is one thing: the
# trusted classes label their own evidence `TRUSTED_LANE_*`, and no mid-term run
# may emit a record that says trusted. So the lane's contribution is a wrapper
# that relabels and accounts, and delegates everything that touches a socket.

#: What the mid-term lane calls itself where the trusted classes want a phase.
#: Deliberately not `D1` or `D2`: those name trusted-lane phases with operator
#: preconditions this architecture does not have, and a record carrying one
#: would be this lane describing itself as the lane it is not.
PHASE_LABEL = "MIDTERM_SINGLE_REPO"


class MidtermProviderTransport:
    """The trusted lane's transport, wrapped so the EVIDENCE says mid-term.

    Delegates every byte. The wrapper exists for two reasons and no others:

    * `TrustedCountTransport.record()` returns
      `transport_class="TRUSTED_LANE_COUNT_TRANSPORT"`, and a mid-term run
      writing that into evidence would be claiming a lane whose operator
      prerequisites are unrecorded — the one claim this whole package is
      careful never to make;
    * the mid-term evidence needs per-call path accounting, which the trusted
      classes keep as attempt records shaped for their own report.

    Everything else — the endpoint allowlist, the ceiling, the source agreement
    — is the wrapped object's, because that is where it is already correct.
    """

    kind = "LIVE_PROVIDER"
    source = SOURCE_PROVIDER

    def __init__(self, inner, *, capability: str, permitted_paths):
        declared = getattr(inner, "source", None)
        if declared != SOURCE_PROVIDER:
            refuse(f"category=live_transport_does_not_declare_provider "
                   f"source={declared!r} transport={type(inner).__name__}")
        if not callable(getattr(inner, "post", None)):
            refuse(f"category=live_transport_has_no_post "
                   f"transport={type(inner).__name__}")
        self._inner = inner
        self.capability = capability
        self.permitted_paths = tuple(permitted_paths)
        self.calls = []

    def post(self, path, body, *, timeout=None):
        # Checked here as well as inside. Not redundant: this one is the
        # mid-term lane's own statement about which capability it handed out,
        # and it holds even if a future trusted-lane edit widened theirs.
        _assert_path_permitted(path, permitted=self.permitted_paths,
                               transport_class=type(self).__name__)
        _assert_body_is_bytes(body, transport_class=type(self).__name__)
        self.calls.append({"path": path, "bytes": len(body)})
        return self._inner.post(path, body, timeout=timeout)

    def __getattr__(self, name):
        """Forward the trusted transport's own gates — `assert_zero_generation`,
        `assert_within_authorized_tokens` — rather than reimplementing them.

        Only reached for names this class does not define, so it cannot shadow
        `post`, `record` or the accounting."""
        return getattr(self._inner, name)

    def record(self) -> dict:
        inner = self._inner.record() if hasattr(self._inner, "record") else {}
        return {**inner,
                "transport_class": f"MIDTERM_SINGLE_REPO_{self.capability}",
                "wrapped_transport_class": type(self._inner).__name__,
                "source": self.source,
                "capability": self.capability,
                "permitted_paths": list(self.permitted_paths),
                "provider_calls": len(self.calls),
                "honest_scope": ("what this lane sent and how often, through "
                                 "the trusted lane's transport. This is a "
                                 "mid-term single-repository run: it is not a "
                                 "trusted-lane phase and carries none of that "
                                 "lane's operator prerequisites")}

    def configuration(self) -> dict:
        return self.record()

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def paths_called(self) -> list:
        return sorted({call["path"] for call in self.calls})


def live_count_transport(engine: dict, *, opener, key: str,
                         authorized_input_tokens: int
                         ) -> MidtermProviderTransport:
    """Counting, and only counting. One instance per run — see §8.

    The ceiling is required by the wrapped class, which refuses a transport
    built without one: a count transport with no budget would spend against a
    number nobody approved."""
    from trustedlane import counttransport
    assert_paths_match_engine(engine)
    inner = counttransport.bind(
        engine, opener=opener, credential=key, phase=PHASE_LABEL,
        authorized_input_tokens=authorized_input_tokens)
    return MidtermProviderTransport(inner, capability="COUNT_TRANSPORT",
                                    permitted_paths=(COUNT_PATH,))


def live_generation_transport(engine: dict, *, opener, key: str,
                              generation_attempt_cap: int
                              ) -> MidtermProviderTransport:
    """The panel itself. Reaches `/v1/responses` and nothing else."""
    from trustedlane import generationtransport
    assert_paths_match_engine(engine)
    inner = generationtransport.bind(
        engine, opener=opener, credential=key, phase=PHASE_LABEL,
        generation_attempt_cap=generation_attempt_cap)
    return MidtermProviderTransport(inner, capability="GENERATION_TRANSPORT",
                                    permitted_paths=(GENERATION_PATH,))


#: The public name for "this object can reach the provider", for the gates that
#: ask that question. One class rather than a tuple of concrete types: a tuple
#: is a list somebody has to remember to extend, and the failure mode of a
#: missed entry is a live transport that passes the dry-run gate.
LiveTransportBase = MidtermProviderTransport


def assert_no_provider_calls(transport) -> dict:
    """The Phase-A assertion: this run spent nothing.

    Accepts any transport that counts, and refuses one that cannot be asked —
    "the object had no counter, so we could not prove a call was made" is not a
    proof of zero."""
    count = getattr(transport, "call_count", None)
    attempts = getattr(transport, "attempts", None)
    if count is None and attempts is None:
        refuse("category=transport_cannot_account_for_calls — a transport that "
               "cannot report its call count cannot be used to prove that no "
               "call happened")
    made = int(count or 0)
    tried = len(attempts or ())
    if made or tried:
        refuse(f"category=provider_calls_were_made calls={made} attempts={tried} "
               "— this run was required to make none")
    return {"provider_calls": 0, "provider_attempts": 0,
            "transport_kind": getattr(transport, "kind", "UNKNOWN"),
            "declared_source": getattr(transport, "source", None)}


def assert_provider_calls_all_went_through(transport, *, expected_paths) -> dict:
    """Accounting for a run that DID spend: every call, on the right path.

    §8's requirement in one place. The count path is walked by more than one
    function and an earlier version constructed a fresh transport per batch, so
    the totals were per-batch totals summed by the caller — which agreed with
    itself and with nothing else."""
    calls = getattr(transport, "calls", None)
    if calls is None:
        refuse("category=transport_cannot_account_for_calls — a run that spent "
               "must be able to say what it spent it on")
    if isinstance(calls, int):
        refuse(f"category=transport_accounting_is_a_bare_count calls={calls} — "
               "a total cannot say which endpoint was reached, and the whole "
               "point of the split is that one of them costs more")
    observed = sorted({call.get("path") for call in calls})
    unexpected = [path for path in observed if path not in tuple(expected_paths)]
    if unexpected:
        refuse(f"category=provider_calls_on_unexpected_paths "
               f"paths={unexpected} expected={list(expected_paths)}")
    return {"provider_calls": len(calls), "paths": observed,
            "declared_source": getattr(transport, "source", None)}


def scripted_count_body(tokens: int) -> bytes:
    """A count response body, for tests that need one without an engine.

    Uses `json` on a literal dict rather than a hand-built string so that a
    change to the envelope is a change to one line here and a failing test
    everywhere it matters."""
    return json.dumps({"object": "response.input_tokens",
                       "input_tokens": int(tokens)}).encode("utf-8")
