"""R07/R20 — the publisher that actually puts a status on the candidate head.

`statuspublish.publish` refused, and the refusal was honest: publishing needs an
installation token with Statuses write, which a reviewed branch must never hold.
But "the transport needs a capability" was doing two jobs — it named a genuine
operator dependency AND it stood in for code that did not exist. Only the first
is legitimate. Everything except obtaining the token is implementable, testable
against a fake server in D0, and is implemented here.

**Why this module exists separately from `statuspublish`.** That module builds
and validates the REQUEST: what a status must say, which contexts may be
published, that a green trusted context carries an evidence digest, that pending
precedes terminal. Those are decisions, and they must be checkable without a
token. This module is the SEND, and it holds the one capability. Keeping them
apart is what lets every decision be exercised in a phase that cannot publish.

**R20 — ordinary green is not trusted green.** The whole point of publishing on
the candidate head is that a reader looking at a pull request can tell "CI
passed" from "a cross-vendor panel reviewed this and Sol approved". That
distinction survives only if the trusted contexts are published by something
that could not have been the ordinary lane: a different token, on the exact
commit, naming the evidence. A publisher that could be reached from candidate
code would collapse the two, which is why the token is injected and the phase
gate sits on obtaining it.

**What this module does NOT do.** Decide whether the review passed, choose the
context, or reach the network by itself. `opener` is a parameter for the same
reason it is everywhere else in this lane, and the phase gate is on
`read_installation_token`, not on the code below it.
"""

from __future__ import annotations

import hashlib
import json

from .errors import refuse

#: The API this posts to. The commit-status endpoint rather than check-runs: a
#: status is what branch protection's "required status checks" reads by NAME,
#: and a check run with the same name is a different object an operator would
#: have to configure differently. One shape, matching what
#: `statusnames.branch_protection_instructions` tells them to type.
API_HOST = "https://api.github.com"
STATUS_PATH = "/repos/{repository}/statuses/{sha}"

#: The environment variable the protected runtime binds the installation token
#: to. A NAME, never a value; `ruff` flags it for the usual reason.
TOKEN_ENV = "TRUSTED_STATUS_TOKEN"  # noqa: S105 - a NAME; pragma: allowlist secret

#: Statuses are small. A bound at all is the point: a response with no limit is
#: a runner an API can fill, and this one runs after a credential-bearing step.
MAX_RESPONSE_BYTES = 64 * 1024

#: What GitHub returns for a created status. Anything else is a refusal naming
#: the CODE — never the body, which echoes the description and the target URL
#: and, on an auth failure, sometimes the request headers.
CREATED = 201


def read_installation_token(*, phase: str, environ=None) -> str:
    """The one capability, behind the phase gate. Refuses in D0.

    Same shape as `transport.read_credential` and deliberately a separate
    function rather than a parameter on it: a token that can set a status and a
    key that can spend money are different capabilities, and a lane that
    obtained them through one call would grant both wherever it granted
    either."""
    import os

    from .phases import D1, D2, assert_phase_permitted

    if phase not in (D1, D2):
        refuse(f"category=status_token_phase_not_permitted phase={phase!r}")
    assert_phase_permitted(phase)
    value = (environ if environ is not None else os.environ).get(TOKEN_ENV)
    if not value:
        refuse(f"category=status_token_absent variable={TOKEN_ENV} — the "
               "protected environment did not bind it, which is what happens "
               "when this runs from an unapproved ref")
    return assert_token_shape(value)


def assert_token_shape(value) -> str:
    """Shape only, with no phase gate, so it is drivable in D0.

    Inside `read_installation_token` these checks sit behind a gate that always
    refuses here — so a test could only reach them by grepping the source, and
    a test that greps source asks whether a string is present rather than
    whether a value is refused."""
    if not isinstance(value, str):
        refuse("category=status_token_not_a_string")
    if value != value.strip():
        refuse("category=status_token_has_surrounding_whitespace — a token "
               "copied with a trailing newline authenticates nothing and the "
               "failure looks like a permissions problem")
    if len(value) < 20:
        refuse(f"category=status_token_too_short len={len(value)} — the length "
               "is reported and the value never is")
    if any(c in value for c in " \t\n\r"):
        refuse("category=status_token_contains_whitespace")
    return value


def build_post(request: dict, *, repository: str) -> dict:
    """The HTTP request, with NO token in it.

    The same promise the provider transport makes, for the same reason: the
    record a caller holds and digests must carry no capability, so the token is
    joined to a COPY of the headers inside `send`.

    `request` is a validated `statuspublish.status_request`. Rebuilt from it
    rather than taken as loose arguments, because the validation lives there and
    a second entry point is a second set of rules."""
    from . import CANONICAL_REPOSITORY
    from .statuspublish import STATES, assert_candidate_sha

    if not isinstance(request, dict) or "external_id" not in request:
        refuse("category=status_request_not_validated — build it with "
               "status_request() so the refusals run before anything is sent")
    if repository != CANONICAL_REPOSITORY:
        refuse(f"category=status_repository_not_canonical "
               f"repository={repository!r} — the repository is fixed in "
               "trusted policy; a caller-supplied one is a caller choosing "
               "which pull request gets a green trusted status")
    sha = assert_candidate_sha(request["candidate_head_sha"])
    if request.get("state") not in STATES:
        refuse("category=status_state_not_permitted")
    body = json.dumps({
        "state": request["state"],
        "target_url": request["target_url"],
        "description": request["description"],
        "context": request["context"],
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "method": "POST",
        "url": f"{API_HOST}{STATUS_PATH.format(repository=repository, sha=sha)}",
        "headers": {"accept": "application/vnd.github+json",
                    "content-type": "application/json",
                    "x-github-api-version": "2022-11-28"},
        "body": body,
        "payload_sha256": hashlib.sha256(body).hexdigest(),
        "carries_credential": False,
        "external_id": request["external_id"],
    }


def assert_request_is_a_status_post(post: dict, *, repository: str) -> dict:
    """The endpoint gate, mirroring the count and generation ones.

    A status token cannot spend money, but it can mark a pull request green —
    so the same question applies: is this request going where this capability
    is allowed to go. The SHA is re-derived from the URL and compared, because
    the URL is what is actually sent and everything else is a description of
    it."""
    from .statuspublish import assert_candidate_sha

    if not isinstance(post, dict):
        refuse("category=status_post_not_an_object")
    if post.get("method") != "POST":
        refuse(f"category=status_method_not_permitted "
               f"method={post.get('method')!r}")
    url = post.get("url")
    prefix = f"{API_HOST}/repos/{repository}/statuses/"
    if not isinstance(url, str) or not url.startswith(prefix):
        refuse(f"category=status_endpoint_not_permitted url={url!r} — this "
               "token may set a commit status and nothing else; a URL it did "
               "not build is a URL nobody checked")
    sha = url[len(prefix):]
    assert_candidate_sha(sha, field="status_url_sha")
    if "/" in sha:
        refuse("category=status_url_has_extra_path_segments")
    return {"endpoint": url, "candidate_head_sha": sha}


class TrustedStatusPublisher:
    """One run's worth of status publication, through one token.

    Callable, so it is a drop-in for the `publisher` parameter D1 and D2
    already take. Every decision about WHAT to publish was made by
    `statuspublish.status_request` before this sees it; this decides only
    whether the request may be sent and reports what came back."""

    def __init__(self, *, opener, token: str, repository: str, phase: str,
                 max_publications: int = 8):
        if not callable(opener):
            refuse("category=status_opener_not_callable")
        self._opener = opener
        self._token = assert_token_shape(token)
        self._repository = repository
        self._phase = phase
        # A run publishes pending plus one terminal. The bound is generous and
        # finite: a loop that republished on every retry would spam the pull
        # request timeline, and the ordering rules alone would not stop it.
        self.max_publications = max_publications
        self.published: list[dict] = []

    def __call__(self, request: dict) -> dict:
        return self.send(request)

    def send(self, request: dict) -> dict:
        if len(self.published) >= self.max_publications:
            refuse(f"category=status_publication_cap_reached "
                   f"published={len(self.published)} "
                   f"cap={self.max_publications}")
        post = build_post(request, repository=self._repository)
        assert_request_is_a_status_post(post, repository=self._repository)
        headers = dict(post["headers"])
        # Joined HERE, to a copy. `post` is what gets recorded and digested.
        headers["authorization"] = f"Bearer {self._token}"
        try:
            status, body = self._opener(post["method"], post["url"], headers,
                                        post["body"])
        except Exception as exc:  # noqa: BLE001 — the class, never the text
            refuse(f"category=status_publication_transport_failed "
                   f"exception_class={type(exc).__name__} — the text is not "
                   "reported; a transport error can carry the whole "
                   "Authorization header")
        if not isinstance(body, (bytes, bytearray)):
            refuse("category=status_response_not_bytes")
        if len(body) > MAX_RESPONSE_BYTES:
            refuse(f"category=status_response_too_large bytes={len(body)}")
        if status != CREATED:
            # The CODE and nothing else. A GitHub error body echoes the
            # description and the target URL, and on an auth failure it can
            # echo request headers.
            refuse(f"category=status_publication_rejected http_status={status} "
                   "— the response body is deliberately not reported; it "
                   "echoes the request and, on an auth failure, its headers")
        record = {
            "context": request["context"],
            "state": request["state"],
            "candidate_head_sha": request["candidate_head_sha"],
            "external_id": request["external_id"],
            "payload_sha256": post["payload_sha256"],
            "http_status": status,
        }
        self.published.append(record)
        return record

    def record(self) -> dict:
        """What this run published, and nothing about whether it was right."""
        return {
            "publisher_class": "TRUSTED_LANE_STATUS_PUBLISHER",
            "phase": self._phase,
            "repository": self._repository,
            "publications": list(self.published),
            "publication_count": len(self.published),
            "honest_scope": (
                "each entry is a status GitHub accepted on the named commit. "
                "Whether the review behind it was sound is the evidence "
                "signature's question; this only says the status was set, and "
                "on which commit"),
        }


def bind(*, opener, token: str, phase: str, repository=None):
    """Construct against trusted policy. The repository is not a parameter.

    It defaults to the canonical identity and refuses anything else, because a
    caller-supplied repository is a caller choosing which pull request gets a
    green trusted status."""
    from . import CANONICAL_REPOSITORY

    return TrustedStatusPublisher(
        opener=opener, token=token, phase=phase,
        repository=repository or CANONICAL_REPOSITORY)
