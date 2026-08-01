"""The engine's GENERATION transport, backed by the lane's credential path.

The counterpart of `counttransport`, and deliberately a separate module rather
than a flag on it. `transport.exchange` refuses any endpoint but the count one
and `transport.exchange_generation` refuses the count endpoint symmetrically;
putting both behind one adapter with a `generate=True` parameter would move the
difference between "counts" and "generates" into an argument, where getting it
wrong looks like configuration rather than like generating from the count lane.

`verifier.executor` drives generation through `post(path, body, *, timeout=)`
returning `(status, body)` — the same shape `counting2` uses, and the engine
checks that `timeout` is keyword-only before it calls anything, because falling
back to "no timeout" is the one behaviour a timeout PIN exists to prevent.

**The response bound is different, and that difference was a live defect.** A
generation body is orders of magnitude larger than a token count. Reading one
through the count lane's 64 KiB cap truncated it into a parse failure that
looked like a malformed provider response, so the run reported the provider as
broken and spent the retry budget proving it.
"""

from __future__ import annotations

import hashlib

from . import transport as lanetransport
from .errors import refuse

SOURCE_PROVIDER = "PROVIDER"
API_HOST = "https://api.openai.com"


class TrustedGenerationTransport:
    """One review's worth of generation, through one credential."""

    source = SOURCE_PROVIDER

    def __init__(self, *, opener, credential: str, phase: str,
                 engine_generation_path: str, generation_attempt_cap: int):
        self._engine_path = assert_endpoints_agree(engine_generation_path)
        if not callable(opener):
            refuse("category=generation_transport_opener_not_callable")
        if not isinstance(credential, str) or not credential:
            refuse("category=generation_transport_credential_missing")
        if isinstance(generation_attempt_cap, bool) or not isinstance(
                generation_attempt_cap, int) or generation_attempt_cap < 1:
            refuse("category=generation_attempt_cap_not_a_positive_integer — "
                   "the cap comes from an authenticated operator envelope; a "
                   "transport built without one would spend against a budget "
                   "nobody approved")
        self._opener = opener
        self._credential = credential
        self._phase = phase
        self.generation_attempt_cap = generation_attempt_cap
        self.calls = 0
        self.count_calls = 0
        self.attempts: list[dict] = []

    def post(self, path: str, body: bytes, *, timeout: int | None = None):
        """The engine's transport contract, and nothing wider."""
        if path != self._engine_path:
            refuse(f"category=generation_transport_path_not_permitted "
                   f"path={path!r} permitted={self._engine_path}")
        if not isinstance(body, (bytes, bytearray)) or not body:
            refuse("category=generation_transport_body_not_bytes")
        # The lane's OWN cap, on top of the engine's PIN. The engine reserves a
        # retry budget per request; this is the total across the run, and it is
        # the number the operator approved.
        if self.calls >= self.generation_attempt_cap:
            refuse(f"category=generation_attempt_cap_reached "
                   f"attempts={self.calls} cap={self.generation_attempt_cap} — "
                   "the operator approved a number of generation calls, and "
                   "this run has made them")
        request = {
            "method": "POST",
            "url": f"{lanetransport.BASE_URL}/responses",
            "headers": {"content-type": "application/json",
                        "accept": "application/json"},
            "body": bytes(body),
            "payload_sha256": hashlib.sha256(bytes(body)).hexdigest(),
            "carries_credential": False,
        }
        self.calls += 1
        reply = lanetransport.exchange_generation(
            request, opener=self._opener, credential=self._credential)
        self.attempts.append({"attempt": self.calls,
                              "payload_sha256": request["payload_sha256"],
                              "status": reply["status"]})
        return reply["status"], reply["body"]

    def record(self) -> dict:
        return {"transport_class": "TRUSTED_LANE_GENERATION_TRANSPORT",
                "source": self.source,
                "endpoint": f"{lanetransport.BASE_URL}/responses",
                "generation_attempts": self.calls,
                "generation_attempt_cap": self.generation_attempt_cap,
                "count_calls": self.count_calls,
                "honest_scope": ("what this lane sent and how often. Whether "
                                 "the replies were valid is the engine's "
                                 "question, and it answers it")}


def assert_endpoints_agree(engine_generation_path: str) -> str:
    """The engine and the lane must name the same endpoint.

    The same identity check the count transport does, and for the same reason
    it was needed there: the engine's path carries `/v1` and the lane's base
    already ends in it, so concatenating produced a doubled prefix that the
    engine retried three times before reporting "retry exhausted", naming
    neither path."""
    if not isinstance(engine_generation_path, str) or not engine_generation_path:
        refuse("category=engine_generation_path_missing")
    lane_url = f"{lanetransport.BASE_URL}/responses"
    engine_url = f"{API_HOST}{engine_generation_path}"
    if lane_url != engine_url:
        refuse(f"category=generation_endpoint_disagrees_with_engine "
               f"lane={lane_url} engine={engine_url} — whichever one is wrong "
               "would send a credential to a URL nobody chose")
    return engine_generation_path


def bind(engine, *, opener, credential: str, phase: str,
         generation_attempt_cap: int) -> TrustedGenerationTransport:
    """Construct against the loaded engine, settling both agreements first."""
    executor = engine["modules"]["verifier.executor"]
    transport = TrustedGenerationTransport(
        opener=opener, credential=credential, phase=phase,
        engine_generation_path=executor.GENERATION_PATH,
        generation_attempt_cap=generation_attempt_cap)
    expected = engine["modules"]["verifier.counting"].SOURCE_PROVIDER
    if transport.source != expected:
        refuse(f"category=generation_transport_source_disagrees_with_engine "
               f"lane={transport.source!r} engine={expected!r}")
    return transport
