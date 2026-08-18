"""Send one iMessage via an imessage-proxy instance.

Endpoint: POST {IMESSAGE_API_BASE_URL}/api/messages
Auth: `Authorization: Bearer <key>` — exactly one scoped key, `messages:send`.
      The service accepts a credential in no other place: not a query
      parameter, not a cookie, not the body.
Body: {"recipient": "<+E.164 or email>", "text": "<1..4000>",
       "service": "imessage"}   -- additionalProperties is false upstream, so
      sending any other field is a 400. In particular `sender_identifier` is
      admin-scoped and a `messages:send` key that sends it AT ALL, with either
      value, is refused 403 rather than having it ignored.
Success: HTTP 202 with {"operation_id", "state": "accepted", ...}.

TWO THINGS THIS MODULE DOES NOT PROMISE.

First, 202 is not delivery. The proxy's own contract says "Messages.app
accepted the command; this is not delivery confirmation". A digest that
returns ok=True reached Messages.app, and nothing here can tell you it
reached a phone.

Second, exactly-once. `Idempotency-Key` is REQUIRED by the contract and makes
an accepted or ambiguous attempt safe from automatic duplicate execution, but
the daily digest generates a fresh key per call by design: two digests on the
same day are two logical sends, and collapsing them would silently drop the
second. The key protects against a retry of ONE attempt, not against the
scheduler firing twice.

Reference: imessage-proxy/openapi.yaml (operationId sendMessage) and
imessage-proxy/docs/api.md.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.config import get_settings
from app.logging_conf import get_logger
from app.redaction import sanitize

log = get_logger(__name__)

SEND_PATH = "/api/messages"

#: text: 1..4000 code points, and a leading hyphen is rejected upstream. That
#: last one is the trap worth naming — the digest body is LLM-written, and a
#: model that opens with "- bubblegauge 41/100" would earn a 400 whose message
#: says nothing about the hyphen.
MAX_TEXT_LEN = 4000

#: The only control characters the contract permits inside `text`.
_ALLOWED_CTRL = frozenset("\t\n\r")

_E164_RE = re.compile(r"\+[1-9][0-9]{6,14}")

#: Characters ECMA-262 `\s` treats as whitespace but `str.isspace()` does not.
#: The proxy validates with an ECMA regex, so relying on Python's narrower set
#: alone would pass a handle the proxy then rejects. Written as an ordinal so
#: it cannot be mistaken for an empty string when this file is read or edited.
_EXTRA_SPACE = frozenset({chr(0xFEFF)})   # ZERO WIDTH NO-BREAK SPACE / BOM


def _is_control(ch: str) -> bool:
    """C0 and C1 control characters, which the proxy rejects in both the
    recipient and the text. Written as ordinal comparisons rather than a
    character-class regex: the class would have to embed literal control
    bytes or \\u escapes, and both are easy to corrupt silently in transit."""
    code = ord(ch)
    return code < 0x20 or 0x7F <= code <= 0x9F


def strip_control(text: str) -> str:
    """Drop every control character except tab, LF and CR."""
    return "".join(ch for ch in text if ch in _ALLOWED_CTRL or not _is_control(ch))


def is_valid_recipient(handle: str) -> bool:
    """The contract's recipient grammar: `+` then 7-15 digits with a nonzero
    first digit, or one email-like `@` handle carrying no whitespace, no
    control character, no second `@`, and no leading hyphen.

    Checked locally so a misconfigured recipient names the setting that is
    wrong, instead of surfacing as an opaque upstream 400."""
    if not handle or len(handle) > 256:
        return False
    # BEFORE the E.164 branch, not after: Python's `$` also matches just before
    # a trailing newline, so `_E164_RE` alone would accept "+4915...\n" — and a
    # newline is exactly what a quoted .env value expands to. The spec's `$` is
    # a hard end-of-input, and its character class bans control characters in
    # both recipient forms.
    if any(ch.isspace() or _is_control(ch) or ch in _EXTRA_SPACE for ch in handle):
        return False
    if _E164_RE.fullmatch(handle):
        return True
    if handle.startswith("-"):
        return False
    local, sep, domain = handle.partition("@")
    return bool(sep and local and domain and "@" not in domain)


@dataclass
class ImessageResult:
    ok: bool
    status_code: int | None
    error: str | None = None
    operation_id: str | None = None


#: The ONLY name permitted for a plain-HTTP destination.
#:
#: Everything else must be an IP literal in a loopback range. The distinction
#: is whether the guarantee rests on DNS: `localhost` is fixed in /etc/hosts,
#: and anything able to rewrite that file inside this container already has
#: code execution here, so trusting it adds no exposure. Runtime-injected names
#: are a different matter and are deliberately NOT here — see check_destination.
_LOOPBACK_NAMES = frozenset({"localhost"})


def _base_url() -> str:
    """Origin with any trailing slash removed, so path joining cannot produce
    a double slash the proxy would treat as a different route."""
    return get_settings().imessage_api_base_url.rstrip("/")


def check_destination(base_url: str) -> str | None:
    """None when the destination is safe to POST a bearer key to, else the
    reason it is not.

    THIS IS THE APP'S FIRST CONFIG-DRIVEN OUTBOUND DESTINATION. Every other
    outbound host in this service is a literal in code (`app/notify/sipgate.py`
    and the price sources), which is the app-level control that
    `audit/00-system-map.md` records as standing in for the missing container
    egress allowlist. A host that arrives from configuration has no such
    control, so the scheme check is the only thing between a typo and sending
    a `messages:send` bearer key plus the digest text in cleartext to whatever
    answers.

    Plain HTTP is permitted only where it cannot leave the machine. Note that
    the proxy's own `servers:` block declares `https://{host}` and plain-HTTP
    loopback appears only in its docs, so this is stricter than the docs and
    exactly as strict as the contract."""
    if not base_url:
        return "IMESSAGE_API_BASE_URL is empty"
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https"):
        return (f"IMESSAGE_API_BASE_URL scheme must be https (or http on loopback); "
                f"got {parsed.scheme or 'no scheme'!r}")
    if not parsed.hostname:
        return "IMESSAGE_API_BASE_URL has no host"
    if parsed.username or parsed.password:
        # Credentials in the URL would be sent on every request and would ride
        # along in any exception string. `sanitize` masks that shape, but not
        # accepting it in the first place is the stronger control.
        return "IMESSAGE_API_BASE_URL must not carry credentials in the URL"
    if parsed.path.rstrip("/"):
        return ("IMESSAGE_API_BASE_URL must be an origin with no path; "
                f"got a path component {parsed.path!r}")
    # ORIGIN-ONLY MEANS NO QUERY AND NO FRAGMENT EITHER. This is not pedantry:
    # SEND_PATH is appended by string concatenation, so a base of
    # "https://host?x=1" produces "https://host?x=1/api/messages" — the path
    # is swallowed into the query, the request lands on "/" instead of the
    # send route, and the bearer key plus the digest text go with it. A
    # fragment does the same. Both pass a path-only check.
    if parsed.query:
        return ("IMESSAGE_API_BASE_URL must be an origin with no query string; "
                f"got {parsed.query!r} — appending {SEND_PATH} to it would misroute "
                f"the request and send the API key to the wrong path")
    if parsed.fragment:
        return ("IMESSAGE_API_BASE_URL must be an origin with no fragment; "
                f"got {parsed.fragment!r} — appending {SEND_PATH} to it would misroute "
                f"the request and send the API key to the wrong path")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        return (f"IMESSAGE_API_BASE_URL uses http:// to a non-loopback host "
                f"({parsed.hostname}); that sends the API key and the digest in "
                f"cleartext. Use https://, or a tunnel that presents as loopback.")
    return None


def _is_loopback(hostname: str) -> bool:
    """True only where plain HTTP provably cannot leave this machine.

    An IP LITERAL in a loopback range, or `localhost`. The whole of
    127.0.0.0/8 counts, not just the canonical spelling — http://127.0.0.2
    leaves the machine no more than http://127.0.0.1 does, and a check with
    false negatives is one an operator learns to route around.

    `host.docker.internal` and `host.containers.internal` are deliberately
    ABSENT, though they are the obvious way to reach a proxy on the container
    host. Two reasons, either sufficient. They are resolved by DNS the
    container runtime injects, so treating them as loopback makes the
    cleartext guarantee depend on name resolution staying honest — and a
    resolve-then-connect check cannot close that, because the two steps are not
    atomic. And even when they resolve correctly they address the host gateway
    across a bridge interface, which is not loopback in the first place. A
    container that must reach a host-side proxy over plain HTTP should name the
    gateway by address; anything else should use https."""
    host = hostname.lower().strip("[]")
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


#: A hyphen run FOLLOWED BY WHITESPACE is a bullet marker. A hyphen followed by
#: anything else is a minus sign or a dash-word, and the difference matters —
#: see normalise_text.
_BULLET_RE = re.compile(r"^\s*-+\s+")


def normalise_text(body: str) -> str:
    """Coerce a digest body into something the contract accepts.

    Strips control characters, removes bullet markers, guarantees the result
    does not begin with a hyphen, and hard-caps to MAX_TEXT_LEN. Returns ""
    when nothing usable survives, which the caller reports rather than posting
    a guaranteed 400.

    THE LEADING HYPHEN IS NOT SAFE TO SIMPLY DELETE. The contract's `^[^-]`
    rejects it, but the body is LLM-written and `_asciify` in
    app/engine/sms_report.py folds en- and em-dashes to ASCII "-", so a leading
    hyphen is as likely to be a minus sign as decoration. Deleting it turns
    "-3.1% breadth" into "3.1% breadth" and inverts the reading of a financial
    digest — a silently wrong message is worse than an undelivered one. So a
    bullet is dropped and a sign is spelled out.

    Stripping runs to a fixed point: one lstrip("-") followed by .strip() is
    not idempotent, because the trailing strip can re-expose a hyphen that
    whitespace had shielded ("- -3.1%")."""
    cleaned = strip_control(body).strip()
    while True:
        without_bullet = _BULLET_RE.sub("", cleaned, count=1).strip()
        if without_bullet == cleaned:
            break
        cleaned = without_bullet
    if cleaned.startswith("-"):
        rest = cleaned.lstrip("-").lstrip()
        cleaned = f"minus {rest}" if rest[:1].isdigit() else rest
    return cleaned[:MAX_TEXT_LEN]


def send_imessage(message: str, *, recipient: str | None = None) -> ImessageResult:
    """Send one iMessage. Never raises — mirrors `app.notify.sipgate.send_sms`,
    because a failed digest must never take down the scheduler."""
    settings = get_settings()
    base = _base_url()
    if not (base and settings.imessage_api_key):
        return ImessageResult(ok=False, status_code=None,
                              error="imessage proxy URL/key not configured")
    destination_problem = check_destination(base)
    if destination_problem:
        # Refused before a socket opens. A cleartext send cannot be un-sent,
        # so this is one of the few places where failing the digest outright
        # is plainly better than delivering it.
        log.error("imessage_destination_refused", reason=destination_problem)
        return ImessageResult(ok=False, status_code=None, error=destination_problem)
    to = recipient or settings.imessage_recipient
    if not to:
        return ImessageResult(ok=False, status_code=None, error="no recipient configured")
    if not is_valid_recipient(to):
        # Never echo the value: it is a phone number or an Apple ID.
        return ImessageResult(ok=False, status_code=None,
                              error="IMESSAGE_RECIPIENT is not a +E.164 number or an email handle")

    text = normalise_text(message)
    if not text:
        return ImessageResult(ok=False, status_code=None, error="message body empty after cleaning")
    if text.startswith("-"):
        # normalise_text guarantees this cannot happen. Asserting it here turns
        # any future regression into a local error naming the cause, instead of
        # an opaque upstream 400 whose body never mentions the hyphen.
        return ImessageResult(ok=False, status_code=None,
                              error="message body still begins with '-' after normalisation")

    body = {"recipient": to, "text": text, "service": "imessage"}
    headers = {
        "Authorization": f"Bearer {settings.imessage_api_key}",
        "Content-Type": "application/json",
        # Required by the contract: 8..128 chars from [A-Za-z0-9._~-].
        # uuid4 hex is 32 characters and lies inside that class.
        "Idempotency-Key": uuid.uuid4().hex,
    }
    timeout = httpx.Timeout(float(settings.imessage_timeout_s), connect=10.0)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(base + SEND_PATH, json=body, headers=headers)
    except Exception as exc:
        # sanitize(): an httpx exception message embeds the request URL, and a
        # misconfigured base URL can carry credentials in its userinfo.
        detail = sanitize(str(exc), limit=200)
        log.warning("imessage_send_failed", error_class=type(exc).__name__, error=detail)
        return ImessageResult(ok=False, status_code=None, error=detail)

    # 202 EXACTLY, not any 2xx. The contract documents one success status for
    # this route, so another 2xx means something other than the proxy answered
    # — a base URL pointing at a different service, a captive portal, a
    # load balancer's health page — and every one of those would otherwise be
    # reported as a delivered digest. sipgate's sender accepts any 2xx because
    # its contract is looser; this one is not.
    if resp.status_code == 202:
        operation_id = _operation_id(resp)
        log.info("imessage_sent", status=resp.status_code, chars=len(text),
                 recipient=_mask_recipient(to), operation_id=operation_id)
        return ImessageResult(ok=True, status_code=resp.status_code, operation_id=operation_id)
    if 200 <= resp.status_code < 300:
        log.warning("imessage_unexpected_success_status", status=resp.status_code,
                    hint="contract specifies 202 for this route; is the base URL correct?")
        return ImessageResult(
            ok=False, status_code=resp.status_code,
            error=(f"unexpected {resp.status_code}; the contract specifies 202 for "
                   f"/api/messages, so this reply did not come from the proxy's send route"))

    # problem+json bodies quote the offending value, which for a 401 can be the
    # key itself. sanitize() before this reaches a log or an admin response.
    detail = sanitize(resp.text, limit=200)
    log.warning("imessage_rejected", status=resp.status_code, body=detail,
                hint=_hint(resp.status_code))
    return ImessageResult(ok=False, status_code=resp.status_code, error=detail)


def _operation_id(resp: httpx.Response) -> str | None:
    """The 202 body's operation_id, or None. Never lets a malformed success
    body turn a delivered message into a reported failure."""
    try:
        payload = resp.json()
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("operation_id")
    return value if isinstance(value, str) else None


def _hint(status: int) -> str:
    """Operator-facing cause for the statuses this integration actually hits.

    403 is the one worth spelling out: the proxy's recipient allowlist is
    admin-only, so a `messages:send` key cannot add its own destination and
    the fix is on the proxy, not here."""
    return {
        401: "key missing, malformed, unknown, revoked or expired (proxy keys expire, default 90d)",
        403: "key lacks messages:send, or the recipient is not on the proxy's admin-only allowlist",
        404: "base URL does not serve /api/messages — check IMESSAGE_API_BASE_URL",
        409: "chat/service mismatch upstream",
        413: "message too large",
        429: "proxy rate limit",
        503: "proxy or Messages.app not ready",
    }.get(status, "")


def _mask_recipient(handle: str) -> str:
    """C-23: keep a personal handle out of cleartext logs. Retains only the
    last 3 characters, matching `app.notify.sipgate._mask_recipient`."""
    if not handle:
        return "(none)"
    return f"…{handle[-3:]}"
