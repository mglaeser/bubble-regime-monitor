"""The engine's count transport, backed by the lane's credential path.

`verifier.counting2.CountLedger` drives counting through an object with two
members: a declared `source`, and `post(path, body, *, timeout=None)` returning
`(status, body)`. `counting.MockCountTransport` is the candidate-side
implementation and declares MOCK, which is why every candidate plan is
non-executable by construction.

This is the trusted-side implementation. It declares PROVIDER, and it is the
only place in the lane where the engine's counting reaches a socket.

**Why an adapter rather than the lane counting for itself.** The lane used to
build its own count requests, send them, and keep its own ledger — a second,
weaker implementation of what `verifier.counting2` already does properly
(per-request retry reservation, attempt accounting before the attempt, exact
request hashing, response envelope validation). That parallel implementation is
how a "preflight" that only checked a budget got as far as it did. So counting
semantics stay in the engine, and this object supplies only what the engine
cannot have: the credential, the opener, and the endpoint policy.

**What it refuses.** The engine passes a path; this refuses any path but the
count endpoint. That assertion is not a formality — `exchange` already refuses
a non-count URL, and this refuses before the URL is even built, so a caller
that got the path wrong never reaches the code where a credential is in scope.
"""

from __future__ import annotations

import hashlib

from . import transport as lanetransport
from .errors import refuse

#: The engine's vocabulary, restated here so this module does not have to
#: import the candidate package to name its own source. `bind` checks the two
#: agree once the engine is loaded — a lane that declared PROVIDER while the
#: engine spelled it differently would be treated as an undeclared transport
#: and refused, which is fail-closed but undiagnosable.
SOURCE_PROVIDER = "PROVIDER"

#: The host both sides must be naming. The two halves spell the endpoint
#: differently — the engine's `COUNT_PATH` is `/v1/responses/input_tokens`
#: while the lane's `BASE_URL` already ends in `/v1` and its `COUNT_PATH` does
#: not — so the two are reconciled by asserting they compose to the SAME URL
#: rather than by concatenating one onto the other. Concatenation produced
#: `https://api.openai.com/v1/v1/responses/input_tokens`, which this transport
#: refused three times per request while the engine recorded three attempts and
#: reported "retry exhausted".
API_HOST = "https://api.openai.com"


class TrustedCountTransport:
    """One review's worth of counting, through one credential.

    Every attempt is recorded here as well as in the engine's ledger. The two
    are not redundant: the engine counts LOGICAL requests and attempts against
    its own PIN caps, and this counts sockets actually opened, which is what
    the operator's spending approval is denominated in. When they disagree the
    run refuses rather than picking one."""

    source = SOURCE_PROVIDER

    def __init__(self, *, opener, credential: str, phase: str,
                 authorized_input_tokens: int, engine_count_path: str):
        self._engine_count_path = assert_endpoints_agree(engine_count_path)
        if not callable(opener):
            refuse("category=count_transport_opener_not_callable")
        if not isinstance(credential, str) or not credential:
            refuse("category=count_transport_credential_missing")
        if isinstance(authorized_input_tokens, bool) or not isinstance(
                authorized_input_tokens, int) or authorized_input_tokens < 1:
            refuse("category=count_transport_ceiling_not_a_positive_integer — "
                   "the ceiling comes from an authenticated operator envelope; "
                   "a transport built without one would spend against a budget "
                   "nobody approved")
        self._opener = opener
        self._credential = credential
        self._phase = phase
        self.authorized_input_tokens = authorized_input_tokens
        self.calls = 0
        self.generation_calls = 0
        self.counted_input_tokens = 0
        self.attempts: list[dict] = []

    def post(self, path: str, body: bytes, *, timeout: int | None = None):
        """The engine's transport contract, and nothing wider.

        No model, no instructions, no URL: the engine hands over exact bytes it
        has already assembled, scanned and hashed, and this sends them. A
        signature that accepted a URL would let whoever computes it choose
        which server sees the credential."""
        if path != self._engine_count_path:
            # Should be unreachable: the path is fixed in the engine and
            # checked at construction. Kept because "unreachable" is a claim
            # about today's engine, and the cost of being wrong here is a
            # credential sent to an endpoint nobody chose.
            refuse(f"category=count_transport_path_not_permitted path={path!r} "
                   f"permitted={self._engine_count_path} — this transport "
                   "exists to count; a generation endpoint reached through it "
                   "would spend a count approval on a verdict")
        if not isinstance(body, (bytes, bytearray)) or not body:
            refuse("category=count_transport_body_not_bytes")

        request = {
            "method": "POST",
            # Built from the LANE's constants. The engine's path was compared
            # against them at construction; it is not concatenated onto them.
            "url": f"{lanetransport.BASE_URL}{lanetransport.COUNT_PATH}",
            "headers": {"content-type": "application/json",
                        "accept": "application/json"},
            "body": bytes(body),
            "payload_sha256": hashlib.sha256(bytes(body)).hexdigest(),
            "carries_credential": False,
        }
        self.calls += 1
        reply = lanetransport.exchange(request, opener=self._opener,
                                       credential=self._credential)
        self.attempts.append({"attempt": self.calls,
                              "payload_sha256": request["payload_sha256"],
                              "status": reply["status"]})
        return reply["status"], reply["body"]

    def assert_zero_generation(self) -> dict:
        """D1 makes no generation call. Asserted on the transport rather than
        inferred from the plan: the plan says what was intended, and this says
        what was sent."""
        if self.generation_calls:
            refuse(f"category=d1_made_a_generation_call "
                   f"calls={self.generation_calls}")
        return {"generation_calls": 0, "count_attempts": self.calls}

    def assert_within_authorized_tokens(self, counted: int) -> dict:
        """The operator's ceiling, applied to what the engine actually counted.

        The engine's own caps are on ATTEMPTS and cost; this is the input-token
        total the operator wrote in `approve_count_spending`, and nothing else
        in the run compares against it."""
        if isinstance(counted, bool) or not isinstance(counted, int) or (
                counted < 0):
            refuse("category=counted_input_tokens_not_an_integer")
        if counted > self.authorized_input_tokens:
            refuse(f"category=authorized_input_tokens_would_be_exceeded "
                   f"counted={counted} "
                   f"authorized={self.authorized_input_tokens} — the ceiling "
                   "is the number an operator wrote for this occurrence")
        self.counted_input_tokens = counted
        return {"counted_input_tokens": counted,
                "authorized_input_tokens": self.authorized_input_tokens,
                "headroom": self.authorized_input_tokens - counted}

    def record(self) -> dict:
        """Inert evidence about the sockets, not about the counts."""
        return {"transport_class": "TRUSTED_LANE_COUNT_TRANSPORT",
                "source": self.source,
                "endpoint": f"{lanetransport.BASE_URL}{lanetransport.COUNT_PATH}",
                "count_attempts": self.calls,
                "generation_calls": self.generation_calls,
                "counted_input_tokens": self.counted_input_tokens,
                "authorized_input_tokens": self.authorized_input_tokens,
                "honest_scope": ("what this lane sent and how often. It says "
                                 "nothing about whether the numbers that came "
                                 "back are correct — the engine validates "
                                 "those")}


def assert_endpoints_agree(engine_count_path: str) -> str:
    """The engine and the lane must be naming the same endpoint.

    Both halves are right about their own spelling and neither can be derived
    from the other by string surgery, so the check is an identity: the URL the
    lane would build must equal the URL the engine's path names against the
    same host. A rename on either side is a named refusal here rather than a
    credential sent somewhere else, or — as it first appeared — three retries
    and a "retry exhausted" that named neither path."""
    if not isinstance(engine_count_path, str) or not engine_count_path:
        refuse("category=engine_count_path_missing")
    lane_url = f"{lanetransport.BASE_URL}{lanetransport.COUNT_PATH}"
    engine_url = f"{API_HOST}{engine_count_path}"
    if lane_url != engine_url:
        refuse(f"category=count_endpoint_disagrees_with_engine "
               f"lane={lane_url} engine={engine_url} — the two halves spell "
               "the endpoint differently, and whichever one is wrong would "
               "send a credential to a URL nobody chose")
    return engine_count_path


def bind(engine, *, opener, credential: str, phase: str,
         authorized_input_tokens: int) -> TrustedCountTransport:
    """Construct against the loaded engine, checking both agreements first.

    The source spelling and the endpoint are settled here rather than at the
    first call. A mismatch at call time is caught by the engine as a generic
    transport exception and RETRIED — so a deterministic disagreement burns the
    operator's retry budget and is reported as "retry exhausted", naming
    neither side."""
    transport = TrustedCountTransport(
        opener=opener, credential=credential, phase=phase,
        authorized_input_tokens=authorized_input_tokens,
        engine_count_path=engine["modules"]["verifier.counting"].COUNT_PATH)
    assert_source_agrees_with_engine(transport, engine=engine)
    return transport


def assert_source_agrees_with_engine(transport, *, engine) -> dict:
    """The engine refuses an UNDECLARED transport rather than defaulting it.

    That is the right behaviour and it makes a spelling mismatch fail closed —
    but it fails closed as "undeclared", which tells an operator nothing. This
    compares the two spellings and says which disagreed."""
    expected = engine["modules"]["verifier.counting"].SOURCE_PROVIDER
    if getattr(transport, "source", None) != expected:
        refuse(f"category=count_transport_source_disagrees_with_engine "
               f"lane={getattr(transport, 'source', None)!r} "
               f"engine={expected!r} — the engine treats an undeclared "
               "transport as not-the-provider, so a mismatch here would be "
               "reported as a missing declaration rather than as a rename")
    return {"source": expected, "agrees": True}
