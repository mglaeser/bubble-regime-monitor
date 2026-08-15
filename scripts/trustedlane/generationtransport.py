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

from . import adapterv2
from . import transport as lanetransport
from .errors import refuse

SOURCE_PROVIDER = "PROVIDER"
API_HOST = "https://api.openai.com"


class TrustedGenerationTransport:
    """One review's worth of generation, through one credential."""

    source = SOURCE_PROVIDER

    def __init__(self, *, opener, credential: str, phase: str,
                 engine_generation_path: str, generation_attempt_cap: int,
                 adapter_identity: dict):
        self._engine_path = assert_endpoints_agree(engine_generation_path)
        # Settled in the constructor, before a socket can exist. An adapter
        # checked at first use is an adapter that is checked after the
        # credential has already been spent once.
        adapterv2.assert_adapter_is_trusted(adapter_identity)
        self._adapter_identity = dict(adapter_identity)
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
        self.normalization_records: list[dict] = []

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
        # NORMALIZED, never raw. `verifier.executor.validate_response_envelope`
        # takes the internal four-key envelope, and this used to hand it the
        # provider's own document — so every successful generation was rejected
        # as PROVIDER_RESPONSE_INVALID. The model id comes from the OUTGOING
        # payload rather than from the reply, because a normalizer that learns
        # which model to expect from the answer cannot detect a mismatch.
        normalized = adapterv2.normalize_generation_response(
            reply["body"], http_status=reply["status"],
            requested_model=requested_model_of(bytes(body)),
            payload_sha256=request["payload_sha256"], attempt=self.calls,
            adapter_identity=self._adapter_identity)
        self.normalization_records.append(normalized["record"])
        return 200, normalized["envelope_bytes"]

    def record(self) -> dict:
        return {"transport_class": "TRUSTED_LANE_GENERATION_TRANSPORT",
                "source": self.source,
                "endpoint": f"{lanetransport.BASE_URL}/responses",
                "generation_attempts": self.calls,
                "generation_attempt_cap": self.generation_attempt_cap,
                "count_calls": self.count_calls,
                "normalization_version": adapterv2.NORMALIZATION_VERSION,
                "normalization_records": [dict(r) for r in
                                          self.normalization_records],
                "normalization_records_sha256": (
                    adapterv2.records_digest(self.normalization_records)
                    if self.normalization_records else None),
                "honest_scope": ("what this lane sent, how often, and the "
                                 "structural binding from each raw reply to "
                                 "the envelope the engine was given. Whether "
                                 "the verdicts inside are sound is the "
                                 "engine's question, and it answers it")}


def requested_model_of(payload: bytes) -> str:
    """The model this request asked for, read out of the request.

    Deliberately not read from the response. `normalize_generation_response`
    refuses when the two disagree, and a check whose expected value comes from
    the thing being checked cannot fail."""
    import json
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        refuse(f"category=generation_payload_not_json "
               f"exception_class={type(exc).__name__} — this lane assembled "
               "the bytes it is now unable to read, which is a defect here and "
               "not a provider fault")
    if not isinstance(document, dict):
        refuse("category=generation_payload_not_an_object")
    model = document.get("model")
    if not isinstance(model, str) or not model:
        refuse("category=generation_payload_names_no_model — a request that "
               "does not name a model cannot have its answer checked against "
               "one")
    return model


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


def assert_envelope_contract_agrees(executor) -> dict:
    """The adapter's output shape and the engine's expected shape are one thing.

    Stated as a check rather than a comment because they live in two artifacts
    that version separately: this module ships with the trusted runtime and the
    executor comes out of the approved engine. If a future engine adds a fifth
    envelope key, a normalizer that still emits four should fail here — at bind
    time, before a credential is spent — and not once per model inside the
    panel."""
    for name, expected in (("ENVELOPE_KEYS", adapterv2.ENVELOPE_KEYS),
                           ("USAGE_KEYS", adapterv2.USAGE_KEYS)):
        observed = getattr(executor, name, None)
        if observed is None:
            refuse(f"category=engine_declares_no_{name.lower()} — the adapter "
                   "normalizes into a shape the engine defines; an engine that "
                   "does not declare it cannot be normalized for")
        if set(observed) != set(expected):
            refuse(f"category=envelope_contract_disagrees_with_engine "
                   f"field={name} missing={sorted(set(observed) - set(expected))} "
                   f"unexpected={sorted(set(expected) - set(observed))}")
    observed_object = getattr(executor, "EXPECTED_OBJECT", None)
    if observed_object != adapterv2.ENVELOPE_OBJECT:
        refuse("category=envelope_object_tag_disagrees_with_engine")
    return {"envelope_contract_agrees": True,
            "normalization_version": adapterv2.NORMALIZATION_VERSION}


def bind(engine, *, opener, credential: str, phase: str,
         generation_attempt_cap: int,
         engine_source_sha256: str) -> TrustedGenerationTransport:
    """Construct against the loaded engine, settling every agreement first.

    `engine_source_sha256` is REQUIRED and comes from the verified identity the
    operator approved, so the adapter is identified by that artifact rather
    than by hashing a file on disk."""
    executor = engine["modules"]["verifier.executor"]
    assert_envelope_contract_agrees(executor)
    transport = TrustedGenerationTransport(
        opener=opener, credential=credential, phase=phase,
        engine_generation_path=executor.GENERATION_PATH,
        generation_attempt_cap=generation_attempt_cap,
        adapter_identity=adapterv2.builtin_adapter_identity(
            engine_source_sha256=engine_source_sha256))
    expected = engine["modules"]["verifier.counting"].SOURCE_PROVIDER
    if transport.source != expected:
        refuse(f"category=generation_transport_source_disagrees_with_engine "
               f"lane={transport.source!r} engine={expected!r}")
    return transport
