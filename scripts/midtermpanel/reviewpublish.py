"""Publish the readable panel review as ONE sticky pull-request comment.

## Why this is a third publisher and not a widened second one

`midtermpanel.status` publishes commit statuses and can do nothing else: its
allowlist is a tuple of two context names, and that single sentence is what makes
"can the mid-term panel publish a trusted status?" answerable by reading one
file. Adding comment publication to it would cost exactly that sentence.

So this is a separate module with a separate capability, and the same discipline
applies to it: it can write ONE thing, to ONE endpoint family, on ONE repository,
and the URL is assembled here from a pinned numeric repository id and an integer
issue number. There is no code path in this file that takes a caller-supplied
URL, host, path or method.

## Why a sticky comment rather than a formal review

A formal pull-request review is immutable once submitted, so the panel would post
one per push and a pull request with six pushes would carry six of them, five
stale. Requirement 7 is that the prior comment is UPDATED, and the comments API
is the surface that supports it.

## The stale-head refusal

The panel runs asynchronously: it starts when ordinary CI finishes, and a person
can push again while it is running. If it then posted "approved" onto a pull
request whose head had moved, a reader would see an approval next to code no
model saw — the exact fail-open this whole programme is about, arrived at through
the friendliest possible route.

So before writing, the current head is READ BACK from the pull request and
compared to the head that was reviewed. A mismatch refuses the comment. It does
not refuse the run: the commit STATUS is published onto the exact reviewed SHA
and is correct there whatever the branch has since done, and the private evidence
is already on disk. Only the human-readable comment is suppressed, because it is
the only artifact whose meaning depends on where it is displayed.

## Publication can never change a verdict

Every refusal in this module returns a record; none of them alters the decision,
and `panelcli` treats the whole publication as reportable rather than blocking.
A publisher able to redden an approved review would be a way for a transient API
failure to become a review outcome, and a publisher able to green a blocked one
is the thing requirement 11 forbids outright.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import REPOSITORY_NUMERIC_ID
from .errors import PanelRefusal, refuse
from .preflight import parse_api_json
from .reviewrender import MARKER, assert_publishable, head_of

API_ROOT = "https://api.github.com"

#: The repository is addressed by NUMERIC ID, exactly as `githubapi` and
#: `status` already address it: a name can be transferred or reused, a numeric
#: id cannot. That route family — `/repositories/{id}/…` — is what every read in
#: this package and the status POST already use against this repository in
#: production, so it is a shape with evidence behind it rather than an
#: assumption. If GitHub ever declined it for the comments sub-resource, the
#: refusal would arrive here as `review_publish_api_error where=…
#: http_status=404`, the review would still be retained privately, and the
#: decision would be unaffected — which is why `where` names each call site
#: separately rather than sharing one label.

#: Comments to inspect when hunting for this panel's own.
#:
#: The first version sized this against `parse_api_json`'s 2 MiB cap and got the
#: arithmetic wrong — it treated GitHub's 65 536-CHARACTER comment limit as
#: bytes. A comment of 65 536 astral-plane characters is 256 KiB of UTF-8, so
#: eight legal comments already exceed the cap and NO page size fixes it.
#:
#: So the size is no longer load-bearing, and `existing_comment` degrades on a
#: failed look instead. 25 keeps an ordinary page small; six pages is 150
#: comments, scanned oldest-first because that is the order GitHub returns them
#: and the order `find_sticky_comment` resolves ties in. Past that, or on any
#: read fault, the panel posts a SECOND comment rather than none — see
#: `existing_comment` for why that direction is the safe one.
COMMENT_PAGE_SIZE = 25
MAX_COMMENT_PAGES = 6

#: The only two methods this module ever uses, and the only two shapes of URL.
#: Written down so the capability can be read rather than inferred.
PUBLISH_METHODS = ("POST", "PATCH")

#: Outcome codes. Each is a distinct thing that happened, so the private
#: artifact can record WHICH and a reader is never left with a bare "not
#: published".
CREATED = "created"
UPDATED = "updated"
REFUSED_STALE_HEAD = "refused_stale_head"
REFUSED_NO_PULL_REQUEST = "refused_no_pull_request"
REFUSED_API = "refused_api_error"
#: A dry run RENDERS the review and retains it and does not publish it. It gets
#: its own code rather than reusing a refusal, because "nothing was published
#: and that is correct" and "nothing was published and something went wrong"
#: are the two states a reader most needs told apart.
DRY_RUN_NOT_PUBLISHED = "dry_run_not_published"


def not_published_in_a_dry_run(*, body: str, candidate_head_sha: str) -> dict:
    """The dry run's publication record: rendered, retained, never sent.

    Deliberately NOT a sink that answers GitHub reads. `dryrun.StatusSink`
    records what would have been sent precisely because nothing in this lane
    depends on GitHub's behaviour beyond a status code — but the comment
    publisher DOES depend on it: it reads the pull request's current head and
    the existing comment list. A sink answering those would be a model of
    GitHub, and a model that got them wrong would prove the publisher worked
    against a fiction. So the dry run proves what it can honestly prove: the
    body was built, sanitized and passed the publication gate."""
    assert_publishable(body)
    return {"published": False, "outcome": DRY_RUN_NOT_PUBLISHED,
            "comment_id": None, "http_status": None,
            "body_chars": len(body), "candidate_head_sha": candidate_head_sha,
            "refusal": None,
            "honest_scope": ("the review was rendered, sanitized and passed "
                             "`assert_publishable`. No pull request was read "
                             "and no comment was created or edited")}


def comments_url(pr_number: int) -> str:
    """The list/create endpoint for ONE pull request's comments.

    Built from the pinned repository numeric id and an integer, so no candidate
    string reaches it. A repository NAME can be transferred or reused; the
    numeric id cannot, which is why every read in this package addresses the
    repository the same way."""
    return (f"{API_ROOT}/repositories/{REPOSITORY_NUMERIC_ID}"
            f"/issues/{_issue_number(pr_number)}/comments"
            f"?per_page={COMMENT_PAGE_SIZE}")


def comment_url(comment_id: int) -> str:
    """The edit endpoint for ONE existing comment, by integer id."""
    if isinstance(comment_id, bool) or not isinstance(comment_id, int) \
            or comment_id < 1:
        refuse("category=review_comment_id_not_a_positive_int")
    return (f"{API_ROOT}/repositories/{REPOSITORY_NUMERIC_ID}"
            f"/issues/comments/{comment_id}")


def pull_request_url(pr_number: int) -> str:
    return (f"{API_ROOT}/repositories/{REPOSITORY_NUMERIC_ID}"
            f"/pulls/{_issue_number(pr_number)}")


def _issue_number(value) -> int:
    """A positive integer, or a refusal.

    `bool` is excluded explicitly: `isinstance(True, int)` is true in Python and
    `/issues/True/comments` is a URL that would be built without complaint."""
    if isinstance(value, bool):
        refuse("category=review_pr_number_is_a_bool")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        refuse(f"category=review_pr_number_not_an_int value={type(value).__name__}")
    if number < 1:
        refuse(f"category=review_pr_number_not_positive value={number}")
    return number


# -------------------------------------------------------------- transport ---


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is a request to send the bearer token somewhere else.

    `urllib` follows 301/302/307 by default and re-sends the headers that were
    added to the Request — including `Authorization`. Every URL this module
    builds is pinned to `api.github.com` and a numeric repository id, and that
    pinning means nothing if the response can move the request. There is no
    legitimate redirect on these endpoints, so the honest handling is a refusal
    rather than a host allowlist that would have to be kept correct."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        refuse(f"category=review_publish_redirect_refused http_status={code} "
               "— the response asked this request to go somewhere else, and "
               "the request carries a bearer token. Every URL here is pinned "
               "to api.github.com and a numeric repository id; a redirect is "
               "the one thing that could move it")


def _no_redirects(opener):
    """The caller's opener, or a redirect-refusing one when none was given.

    `panelcli` wires one shared redirect-refusing opener for the status
    publisher and this one together, so in production `opener` is already safe.
    This is the default for a direct caller — a test, a future entry point —
    so the safe behaviour is what you get by not thinking about it."""
    if opener is not None:
        return opener
    return urllib.request.build_opener(_RefuseRedirects).open


def _request(url: str, *, method: str, token: str, opener=None,
             body: dict | None = None, where: str = "", timeout: int = 30):
    if method not in ("GET", *PUBLISH_METHODS):
        refuse(f"category=review_publish_method_not_permitted method={method!r} "
               f"permitted={['GET', *PUBLISH_METHODS]}")
    if not token or not isinstance(token, str):
        refuse("category=review_publish_token_absent — the publisher cannot "
               "write without a token, and must say so rather than reporting a "
               "comment it never sent")
    data = (json.dumps(body).encode("utf-8")) if body is not None else None
    # S310: the URL is not caller-supplied. Every one is an f-string over a
    # fixed https://api.github.com base, the pinned repository numeric id and an
    # integer this module validated. No scheme can be injected.
    http = urllib.request.Request(url, data=data, method=method)  # noqa: S310
    http.add_header("Authorization", f"Bearer {token}")
    http.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        http.add_header("Content-Type", "application/json")
    try:
        with _no_redirects(opener)(http, timeout=timeout) as response:
            return (int(getattr(response, "status", 0) or 0), response.read())
    except urllib.error.HTTPError as exc:
        # Status only, never the body. A GitHub error body can echo request
        # headers, and the one header here is a bearer token — in a job that is
        # also holding a provider key.
        refuse(f"category=review_publish_api_error where={where} "
               f"http_status={exc.code}")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        refuse(f"category=review_publish_transport_error where={where} "
               f"exception_class={type(exc).__name__}")


def observed_head(pr_number: int, *, token: str, opener) -> str | None:
    """The pull request's head SHA as GitHub reports it RIGHT NOW.

    Read immediately before the write, not carried from preflight. The gap this
    closes is the whole reason the read exists: preflight's value was true when
    the run started, and the question here is whether it is still true at the
    moment a comment would appear."""
    _, raw = _request(pull_request_url(pr_number), method="GET", token=token,
                      opener=opener, body=None, where="pull-request")
    document = parse_api_json(raw, where="pull-request")
    if not isinstance(document, dict):
        return None
    sha = ((document.get("head") or {}) if isinstance(document.get("head"), dict)
           else {}).get("sha")
    return sha if isinstance(sha, str) and len(sha) == 40 else None


def written_by_a_bot(entry: dict) -> bool:
    """Was this comment written by an Actions token rather than by a person?

    The marker alone is not enough to identify our own comment, and the gap is
    not theoretical: anyone who can comment on the pull request can paste the
    marker into a comment of their own, and a publisher that matched on the
    marker alone would then OVERWRITE that person's text with the panel review.
    Nothing is gained by an attacker doing it — the panel's own body replaces
    whatever was there — but destroying a reviewer's comment is a real harm and
    the check is two lines.

    Both forms of the same fact are accepted, so a change in one field does not
    silently stop the panel finding its own comment. A false NEGATIVE here costs
    a duplicate comment; a false positive costs somebody's writing."""
    user = entry.get("user")
    if not isinstance(user, dict):
        return False
    login = str(user.get("login") or "")
    return user.get("type") == "Bot" or login.endswith("[bot]")


def find_sticky_comment(comments) -> dict | None:
    """This panel's own comment: written by a bot, OPENING with the marker.

    Oldest wins, deliberately. If two ever exist — a run that created one and
    died before it could be found again — editing the oldest converges on a
    single comment, while editing the newest would leave the original stale
    forever at the top of the thread.

    `startswith` and not `in`: the marker is the first thing `render` emits, so
    our own comment always begins with it, and requiring the position removes
    the case where a person quotes the whole review back in a reply."""
    if not isinstance(comments, list):
        refuse("category=review_comments_response_not_a_list")
    found = []
    for entry in comments:
        if not isinstance(entry, dict):
            continue
        body = entry.get("body")
        if not isinstance(body, str) or not body.startswith(MARKER):
            continue
        if not written_by_a_bot(entry):
            continue
        identifier = entry.get("id")
        if isinstance(identifier, int) and not isinstance(identifier, bool):
            found.append({"id": identifier, "body": body,
                          "bound_head": head_of(body)})
    if not found:
        return None
    return min(found, key=lambda c: c["id"])


#: What `existing_comment` returns when it could not look properly. Not None —
#: "there is no prior comment" and "I could not tell" are different facts, and
#: only the second one should produce a duplicate.
LOOKUP_FAILED = "lookup_failed"


def existing_comment(pr_number: int, *, token: str, opener):
    """Walk the comment pages until the marker is found or the bound is hit.

    A failed LOOK degrades to `LOOKUP_FAILED` rather than propagating, and the
    reason is an attack adversarial review actually ran. `parse_api_json` caps a
    response at 2 MiB; a GitHub comment may be 65 536 CHARACTERS, which in
    astral-plane UTF-8 is 256 KiB — so eight legal comments exceed the cap and
    no page size fixes it. With the refusal propagating, anyone who could
    comment on the pull request could make every future panel review vanish:
    the blocked finding would never be published, and a stale `approved`
    headline from an earlier head would stay pinned at the top of the thread.

    Degrading here inverts the failure from SILENCE to NOISE. The worst an
    attacker now achieves is a second comment, and a duplicate review is
    readable — which is the property this whole publisher exists to provide.
    """
    for page in range(1, MAX_COMMENT_PAGES + 1):
        url = f"{comments_url(pr_number)}&page={page}"
        try:
            _, raw = _request(url, method="GET", token=token, opener=opener,
                              body=None, where="issue-comments")
            comments = parse_api_json(raw, where="issue-comments")
        except PanelRefusal:
            return LOOKUP_FAILED
        if not isinstance(comments, list):
            return LOOKUP_FAILED
        found = find_sticky_comment(comments)
        if found is not None:
            return found
        if len(comments) < COMMENT_PAGE_SIZE:
            return None
    # The bound, hit. Also a failed look rather than an absence: our comment may
    # be on page seven, and creating a second one is the safe half of the guess.
    return LOOKUP_FAILED


# ---------------------------------------------------------------- publish ---


def publish_review(*, body: str, pr_number, candidate_head_sha: str,
                   token: str, opener, challenge: str | None = None) -> dict:
    """Create or update the one sticky comment. Never raises for an API fault.

    Returns a record in every case. The caller writes it into the private
    artifact and prints it; it never becomes part of the decision, because a
    review that blocked must stay blocked when GitHub returns a 502 and a review
    that approved must not turn red for the same reason.
    """
    outcome = {"published": False, "outcome": None, "comment_id": None,
               "http_status": None, "body_chars": len(body or ""),
               "candidate_head_sha": candidate_head_sha, "refusal": None}
    try:
        assert_publishable(body, challenge=challenge)
        # No pull request is a NORMAL outcome, not a fault. `preflight` refuses
        # to proceed without one, so this is defence against a future caller
        # rather than a state the workflow reaches — and it is reported as its
        # own code so nobody reads it as an API failure.
        if str(pr_number or "").strip() in ("", "0"):
            outcome.update(
                outcome=REFUSED_NO_PULL_REQUEST,
                refusal=("no pull request to comment on; the review is "
                         "retained privately and the commit status carries the "
                         "decision"))
            return outcome
        number = _issue_number(pr_number)
        current = observed_head(number, token=token, opener=opener)
        if current != candidate_head_sha:
            outcome.update(
                outcome=REFUSED_STALE_HEAD, observed_head_sha=current,
                refusal=("the pull request's head moved while the panel was "
                         "running; a readable review published against a head "
                         "nobody reviewed would be read as a review of it. The "
                         "commit status on the reviewed SHA is unaffected"))
            return outcome
        prior = existing_comment(number, token=token, opener=opener)
        if prior is None or prior == LOOKUP_FAILED:
            status, _ = _request(comments_url(number), method="POST",
                                 token=token, opener=opener,
                                 body={"body": body}, where="create-comment")
            outcome.update(published=status in (200, 201), outcome=CREATED,
                           http_status=status,
                           # Recorded, so a run of duplicates is diagnosable
                           # rather than mysterious.
                           lookup_degraded=prior == LOOKUP_FAILED)
            return outcome
        status, _ = _request(comment_url(prior["id"]), method="PATCH",
                             token=token, opener=opener, body={"body": body},
                             where="update-comment")
        outcome.update(published=status in (200, 201), outcome=UPDATED,
                       http_status=status, comment_id=prior["id"],
                       replaced_head_sha=prior.get("bound_head"))
        return outcome
    except Exception as exc:                       # never blocks the decision
        # The message of a `PanelRefusal` is already sanitized by construction —
        # this package's refusals carry a category and no untrusted text. Any
        # other exception contributes its CLASS only, for the same reason
        # `githubapi` reports a status code and not a body.
        reason = getattr(exc, "reason", None)
        outcome.update(outcome=REFUSED_API,
                       refusal=(reason if isinstance(reason, str)
                                else f"exception_class={type(exc).__name__}"))
        return outcome


def assert_no_candidate_influence(*, body: str, pr_number) -> dict:
    """The two things a candidate could try to steer, checked before the write.

    A pull request controls its own number and its own file names. The number is
    forced through `_issue_number`, so a crafted string cannot become a path
    segment; the file names reach the body only through `reviewrender.sanitize`.
    This states both as one assertion so a test can name the property rather
    than approximating it."""
    number = _issue_number(pr_number)
    assert_publishable(body)
    return {"pr_number": number, "repository_numeric_id": REPOSITORY_NUMERIC_ID,
            "url": comments_url(number),
            "honest_scope": ("the URL is built from a pinned numeric "
                             "repository id and a validated integer; the body "
                             "passed the publication gate")}


__all__ = [
    "CREATED", "UPDATED", "REFUSED_STALE_HEAD", "REFUSED_NO_PULL_REQUEST",
    "REFUSED_API", "DRY_RUN_NOT_PUBLISHED", "comments_url", "comment_url",
    "pull_request_url", "find_sticky_comment", "written_by_a_bot",
    "LOOKUP_FAILED",
    "existing_comment", "observed_head", "publish_review",
    "not_published_in_a_dry_run", "assert_no_candidate_influence",
]
