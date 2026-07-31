"""The one lane module that can reach a provider — and only from D1 or D2.

EX3-R01. Everything else in `scripts/trustedlane/` is analysis: it reads,
compares and refuses. This file is the exception, and it is the exception on
purpose rather than by accident, because the count lane cannot count without
sending a request.

The suite's rule was "no module in `scripts/trustedlane/` imports a network
client". That rule proved something real — D0 cannot make a request because
there is nothing in it that could — and deleting it to add this file would have
traded a checked property for a comment. So the rule is narrowed rather than
dropped, and the narrowing is itself checked:

* exactly ONE file may import an HTTP client, and the test names it;
* the import is inside a function body, so importing this module does not even
  load one;
* that function refuses before it imports, and at D0 the refusal always fires.

The seam that makes this testable is `opener`. The dangerous operations are
reading the real credential and opening a real socket, and those are the only
two things behind the phase gate. Building the request, sending it through a
caller-supplied opener, and parsing the reply are ordinary functions with no
gate at all — so the entire protocol, including every refusal, runs against a
fake server in the D0 suite with no credential anywhere.

The endpoint and the response shape are not guesses. They come from the hosted
capability probe (`docs/VERIFIER_CAPABILITY_PROBE.md`): `POST
/responses/input_tokens` returns an object whose `object` field is
`response.input_tokens` and whose `input_tokens` field is a non-negative
integer. Nothing else in that body is used, and nothing else is recorded.
"""

from __future__ import annotations

import hashlib
import json

from .errors import refuse

#: Fixed in trusted policy, never taken from an input. A base URL from an
#: argument lets whoever computes that argument choose which server sees the
#: credential, which is the same defect as a caller-supplied git remote.
BASE_URL = "https://api.openai.com/v1"
COUNT_PATH = "/responses/input_tokens"

#: The provider's own name for the object it returns. Used ONLY as a comparison
#: target — the observed value is never recorded, because a provider-controlled
#: string written into evidence is a channel out of the trusted lane.
COUNT_OBJECT = "response.input_tokens"

#: The environment variable the protected environment binds the key to. This is
#: a NAME. It resolves to nothing outside the `trusted-verifier` environment,
#: and to nothing at all in D0.
CREDENTIAL_ENV = "TRUSTED_VERIFIER_OPENAI_KEY"  # pragma: allowlist secret

#: A reply larger than this is refused before it is parsed. A count response is
#: a few hundred bytes; anything near this bound is not one, and decoding it
#: first in order to find that out is the wrong order.
MAX_RESPONSE_BYTES = 64 * 1024

#: A count above this is refused rather than believed. The ledger multiplies
#: counts by prices, and an absurd count is either a different endpoint's
#: response or a number nobody should silently budget for.
MAX_PLAUSIBLE_INPUT_TOKENS = 50_000_000

REQUEST_TIMEOUT_SECONDS = 60

_HOP_BY_HOP = ("authorization", "cookie", "set-cookie", "proxy-authorization")


def build_count_request(*, model: str, instructions: str, input_text: str,
                        truncation: str = "disabled") -> dict:
    """The exact count request, with NO credential in it.

    Returned without an `Authorization` header on purpose. The credential is
    joined to the request inside `exchange`, at the last possible moment, so a
    request record can be logged, digested, compared and put in evidence
    without anyone having to remember to strip it first — there is nothing to
    strip."""
    for label, value in (("model", model), ("instructions", instructions),
                         ("input_text", input_text)):
        if not isinstance(value, str) or not value:
            refuse(f"category=count_request_field_missing field={label}")
    if truncation != "disabled":
        refuse(f"category=count_request_truncation_not_disabled "
               f"truncation={truncation!r} — truncation lets the provider "
               "decide what was counted, so the number would describe an input "
               "nobody chose")
    payload = {"model": model, "instructions": instructions,
               "input": input_text, "truncation": truncation}
    body = json.dumps(payload, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return {
        "method": "POST",
        "url": f"{BASE_URL}{COUNT_PATH}",
        "path": f"/v1{COUNT_PATH}",
        "headers": {"content-type": "application/json",
                    "accept": "application/json"},
        "body": body,
        "payload_sha256": hashlib.sha256(body).hexdigest(),
        "carries_credential": False,
    }


def assert_request_is_count_only(request: dict) -> dict:
    """D1 counts. A request to any other path is a generation call wearing the
    count lane's clothes, and the phase forbids it in words that are worth
    enforcing in code."""
    if not isinstance(request, dict):
        refuse("category=request_not_object")
    if request.get("method") != "POST":
        refuse(f"category=request_method_not_permitted "
               f"method={request.get('method')!r}")
    url = request.get("url")
    if url != f"{BASE_URL}{COUNT_PATH}":
        refuse(f"category=request_endpoint_not_the_count_endpoint url={url!r} "
               f"permitted={BASE_URL}{COUNT_PATH} — D1 makes zero generation "
               "calls; generation is D2 and a separate operator approval")
    return {"endpoint": url, "generation_call": False}


def read_credential(*, phase: str, environ=None) -> str:
    """Refuses at D0, and at D0 that is the only outcome.

    `phases.assert_phase_permitted` compares against `IMPLEMENTED_PHASE`, which
    is a deployment fact rather than a source-code flag, so this cannot be
    unlocked by editing this file."""
    import os

    from .phases import D1, D2, assert_phase_permitted

    if phase not in (D1, D2):
        refuse(f"category=credential_phase_not_permitted phase={phase!r}")
    capabilities = assert_phase_permitted(phase)
    if not capabilities.get("reads_credential"):
        refuse(f"category=phase_does_not_read_credential phase={phase}")
    value = (environ if environ is not None else os.environ).get(CREDENTIAL_ENV)
    if not value:
        refuse(f"category=credential_absent variable={CREDENTIAL_ENV} — the "
               "protected environment did not bind it, which is what happens "
               "when this runs from an unapproved ref")
    if not isinstance(value, str) or value.strip() != value or len(value) < 20:
        refuse("category=credential_malformed — length and surrounding "
               "whitespace only; the value is never echoed")
    return value


def open_https(*, phase: str):
    """Build the real opener. The HTTP client is imported HERE, after the
    refusal, so that importing this module loads no network code at all.

    No proxy environment is consulted. On a hosted runner there is none, and
    honouring one would let an environment variable redirect a credential-
    bearing request to a host nobody approved."""
    from .phases import D1, D2, assert_phase_permitted

    if phase not in (D1, D2):
        refuse(f"category=transport_phase_not_permitted phase={phase!r}")
    capabilities = assert_phase_permitted(phase)
    if not capabilities.get("calls_provider"):
        refuse(f"category=phase_does_not_call_provider phase={phase}")

    import http.client
    import ssl

    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    def opener(method, url, headers, body):
        host = url.split("/", 3)[2]
        path = "/" + url.split("/", 3)[3]
        connection = http.client.HTTPSConnection(
            host, timeout=REQUEST_TIMEOUT_SECONDS, context=context)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            connection.close()

    return opener


def exchange(request: dict, *, opener, credential: str) -> dict:
    """Send the request and return a record that contains no provider string.

    The credential is added to a COPY of the headers. The record this returns,
    and every record derived from it, is safe to write to evidence because the
    only values in it are ones this repository chose or a plain integer."""
    assert_request_is_count_only(request)
    if not callable(opener):
        refuse("category=transport_opener_not_callable")
    if not isinstance(credential, str) or not credential:
        refuse("category=transport_credential_missing")

    headers = dict(request["headers"])
    for name in headers:
        if name.lower() in _HOP_BY_HOP:
            refuse(f"category=request_header_not_permitted header={name!r}")
    headers["authorization"] = f"Bearer {credential}"

    try:
        status, body = opener(request["method"], request["url"], headers,
                              request["body"])
    except Exception as exc:  # noqa: BLE001 — the type is reported, never the text
        refuse(f"category=transport_call_failed "
               f"exception_class={type(exc).__name__} — the exception text is "
               "not reported: it can carry a URL, a header or a response "
               "fragment, and this is the one place a credential is in scope")
    return {
        "status": status,
        "body": body,
        "payload_sha256": request["payload_sha256"],
        "endpoint": request["url"],
        "honest_scope": "a reply was received; nothing here says it is valid",
    }


def parse_count_response(status, body) -> dict:
    """Strict, and deliberately unforgiving about everything except the number.

    Three things get out of this function: a validated integer, a boolean
    saying the object name matched, and a digest of the request that produced
    it. No provider-controlled string is carried forward — not the object name,
    not an id, not an error message. A body carrying
    `{"input_tokens": "<secret>"}` must not be written into evidence on the
    failure path, which is precisely the bug the capability probe had."""
    if isinstance(status, bool) or not isinstance(status, int):
        refuse("category=response_status_not_an_integer")
    if not isinstance(body, (bytes, bytearray)):
        refuse("category=response_body_not_bytes")
    if len(body) > MAX_RESPONSE_BYTES:
        refuse(f"category=response_body_oversized bytes={len(body)} "
               f"max={MAX_RESPONSE_BYTES} — refused before parsing, because "
               "decoding it in order to find out how big it is is the wrong "
               "order")
    if status != 200:
        refuse(f"category=count_response_status_not_ok status={status} — the "
               "body is not reported; a provider error string is a channel and "
               "this one arrives on the path where nobody is looking")
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        refuse(f"category=count_response_not_json "
               f"exception_class={type(exc).__name__}")
    if not isinstance(document, dict):
        refuse("category=count_response_not_an_object")
    if document.get("object") != COUNT_OBJECT:
        refuse("category=count_response_object_mismatch — the observed value "
               "is not reported; only that it differs from the expected one")
    tokens = document.get("input_tokens")
    # `bool` subclasses `int`, so `true` would otherwise pass as a count of 1.
    if isinstance(tokens, bool) or not isinstance(tokens, int):
        refuse("category=count_response_input_tokens_not_an_integer")
    if tokens < 0:
        refuse(f"category=count_response_input_tokens_negative tokens={tokens}")
    if tokens > MAX_PLAUSIBLE_INPUT_TOKENS:
        refuse(f"category=count_response_input_tokens_implausible "
               f"tokens={tokens} max={MAX_PLAUSIBLE_INPUT_TOKENS} — the ledger "
               "multiplies this by a price; an absurd count is either another "
               "endpoint's reply or a number nobody should silently budget for")
    return {
        "input_tokens": tokens,
        "object_matches": True,
        "status": status,
        "honest_scope": ("a validated non-negative integer and a comparison "
                         "result; no provider-controlled string survives this "
                         "function"),
    }


def count_once(*, request: dict, opener, credential: str) -> dict:
    """One request, one validated count. The whole path, in one place, so a
    caller cannot do half of it."""
    reply = exchange(request, opener=opener, credential=credential)
    parsed = parse_count_response(reply["status"], reply["body"])
    return {
        "input_tokens": parsed["input_tokens"],
        "payload_sha256": request["payload_sha256"],
        "endpoint": request["url"],
        "generation_call": False,
    }
