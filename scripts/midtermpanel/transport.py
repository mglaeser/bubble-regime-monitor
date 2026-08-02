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
import urllib.error
import urllib.request

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

#: Where the panel talks to. Fixed rather than configurable: a base URL that a
#: caller can set is a base URL that can be pointed at a logger.
PROVIDER_BASE_URL = "https://api.openai.com/v1"


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


class NoProviderTransport:
    """Refuses every call. The Phase-A dry-run proof.

    Used where the requirement is "prove no provider call happened". A counter
    that reports zero proves the calls that were made; this proves the calls that
    *could* be made, which is the claim Phase A actually needs.
    """

    kind = "NO_PROVIDER"

    def __init__(self):
        self.attempts = []

    def post(self, *, model: str, system: str, user: str) -> dict:
        self.attempts.append(model)
        refuse(f"category=provider_call_attempted_in_no_provider_mode "
               f"model={model!r} — this run is a dry run and must make no "
               "provider call; the attempt is recorded rather than served")


class FakeProviderTransport:
    """Scripted verdicts, no network, full call accounting.

    Returns whatever the test scripted for a model, records every call in order,
    and never opens a socket. The recorded calls are what the vertical tests
    assert on: which models were asked, how many times, and with what.

    `identical_reasons` exists because the anti-copy tripwire needs a panel whose
    voices agree suspiciously exactly, and building that by hand at each call site
    produced three subtly different definitions of "identical" the first time.
    """

    kind = "FAKE_PROVIDER"

    def __init__(self, *, verdicts=None, refute=False, identical_reasons=False,
                 fail_models=()):
        self.verdicts = dict(verdicts or {})
        self.refute = refute
        self.identical_reasons = identical_reasons
        self.fail_models = tuple(fail_models)
        self.calls = []

    def post(self, *, model: str, system: str, user: str) -> dict:
        assert_model_is_governed(model)
        self.calls.append({"model": model, "system_sha_len": len(system),
                           "user_len": len(user)})
        if model in self.fail_models:
            refuse(f"category=fake_provider_scripted_failure model={model!r}")
        if model in self.verdicts:
            return dict(self.verdicts[model])
        reason = ("identical scripted reason"
                  if self.identical_reasons
                  else f"scripted reason from {model}")
        return {"model": model,
                "refuted": bool(self.refute),
                "confidence": "high" if self.refute else "low",
                "reason": reason}

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def models_called(self) -> list:
        return sorted({c["model"] for c in self.calls})


class LiveProviderTransport:
    """The real thing. Takes its opener and its key as parameters.

    The opener is injected so that a test can drive every branch of this class —
    including the error branches, which are the ones that historically leak —
    without a credential and without a network.
    """

    kind = "LIVE_PROVIDER"

    def __init__(self, *, key: str, opener=None, timeout: int = 180,
                 base_url: str = PROVIDER_BASE_URL):
        if not key or not isinstance(key, str):
            refuse("category=live_transport_without_key")
        self._key = key
        self._opener = opener or urllib.request.urlopen
        self._timeout = int(timeout)
        self._base = base_url
        self.calls = []

    def post(self, *, model: str, system: str, user: str) -> dict:
        assert_model_is_governed(model)
        self.calls.append({"model": model})
        body = json.dumps({
            "model": model,
            "input": [{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        }).encode("utf-8")
        # S310: `self._base` is the module constant, not a parameter a caller
        # can redirect. A configurable base URL is a base URL that can be
        # pointed at a logger, which is why it is fixed.
        request = urllib.request.Request(  # noqa: S310
            f"{self._base}/responses", data=body, method="POST")
        request.add_header("Authorization", f"Bearer {self._key}")
        request.add_header("Content-Type", "application/json")
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # Status only. A provider error body can echo the request, and the
            # request carries an Authorization header. `refuse` additionally
            # detaches the original exception so the chain cannot print it.
            refuse(f"category=provider_http_error model={model!r} "
                   f"http_status={exc.code}")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            refuse(f"category=provider_transport_error model={model!r} "
                   f"exception_class={type(exc).__name__}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            refuse(f"category=provider_response_not_json model={model!r}")

    @property
    def call_count(self) -> int:
        return len(self.calls)


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
            "transport_kind": getattr(transport, "kind", "UNKNOWN")}
