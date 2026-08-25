#!/usr/bin/env python3
"""Independent cross-vendor PR review panel (ported from RoseLohr/roses-food-blog,
scripts/regime/independent-verify.mjs — same mechanism, same roles).

Every pull request is attacked by a PANEL of verifier voices from a DIFFERENT
AI vendor than the one that writes this repo's code (this repo is written with
Anthropic models, so the panel runs on an OpenAI-compatible endpoint). Each
voice is a DIFFERENT model — model diversity beats mere lens diversity —
and each voice gets a distinct review lens.

ROLES (identical to the reference):
  * REQUIRED APPROVER — a named panelist (default ``gpt-5.6-sol``, "Sol") that
    MUST be resolved in the panel and MUST explicitly approve. A refutation of
    ANY confidence (even "low") is a Sol veto; a missing/fallback-replaced Sol
    or an invalid Sol vote blocks (fail-closed). Sol's approval must be PROVEN:
    a substantive own reason and (when a challenge is active) a valid own
    proof-of-check — it may not free-ride on the other voices' attestation.
  * INDEPENDENT CORROBORATORS — at least VERIFIER_MIN_OTHER_APPROVERS (default
    1) DISTINCT non-Sol models must also approve. Repeat votes of the same
    model never count twice.

INTEGRITY GATES (all fail-closed, in order):
  1. require_approvals() — the role gate above.
  1a. attest_consistency() — a GREEN vote must not itself report a defect. Two
                           channels, in this order: (a) the DECLARED DEFECT
                           LEDGER, a required typed ``defects: string[]`` in the
                           verdict schema — refuted=false with a non-empty
                           ledger is a self-contradicting vote and blocks; this
                           channel reads len(), never prose, so no phrasing
                           defeats it. (b) a demoted PROSE TRIPWIRE over the
                           vote's own reason — a heuristic, known-evadable,
                           retained only because the ledger is only as good as
                           the model that fills it.
  2. attest_reasons()    — a majority of the GREEN votes must carry substantive,
                           mutually distinct reasons (anti canned-green).
  3. attest_proof()      — a majority of the GREEN votes must echo the per-run
                           random challenge as "<challenge>-<tier 1-9999>"
                           (anti hardcoded-green: a future "return green"
                           shortcut without a real endpoint round-trip cannot
                           carry a run-valid echo). Honest scope note: this does
                           NOT cryptographically prove an LLM round-trip — a
                           malicious endpoint could mirror the challenge. That
                           is the documented cross-vendor trust assumption,
                           compensated by the deterministic CI gate remaining
                           the sole merge authority.

NO-KEY MODE: without SECOND_VENDOR_API_KEY (or OPENAI_API_KEY) the panel is
inactive; the run prints the documented residual and exits 0 — visible, never
fake-green and never fake-blocking.

PRIVACY: only CODE leaves the origin. Images (raster AND vector), fonts,
binaries and data files are excluded from both the --stat overview and the
diff body via per-extension pathspecs (git pathspec globs do NOT brace-expand,
hence one exclude per extension).

ENV (same contract as the reference):
  SECOND_VENDOR_API_KEY / OPENAI_API_KEY   activates the panel
  VERIFIER_BASE_URL       required explicit HTTPS /v1 endpoint when panel is active
  VERIFIER_MODEL          single pin: this one model for ALL voices
  VERIFIER_PANEL_MODELS   comma list — one DIFFERENT model per voice
  VERIFIER_PANEL          voice count in single-pin mode (default 3, cap 64)
  VERIFIER_REQUIRED_APPROVER      required-approver model prefix (default gpt-5.6-sol)
  VERIFIER_MIN_OTHER_APPROVERS    independent corroborations required (default 1)
  VERIFIER_REQUIRE_DEFECT_LIST    a GREEN vote MUST carry a "defects" ledger (default
                                  off; turn on only once the run logs show every
                                  configured voice emitting the field, then it is on
                                  for good — see docs/INDEPENDENT_REVIEW_PANEL.md)

Usage:
  python scripts/independent_verify.py             verify the PR diff (exit != 0 on block)
  python scripts/independent_verify.py --selftest  exercise every pure decision function
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from typing import Any

KEY = os.environ.get("SECOND_VENDOR_API_KEY") or os.environ.get("OPENAI_API_KEY")
def normalize_base(raw: str | None) -> str:
    """Trailing slashes off. Every call site builds f"{BASE}/models" and
    friends, so a base ending in "/" produces "//models", which this gateway
    answers 404 — a configuration slip that would otherwise surface as an
    unexplained panel outage. Verified live: ".../v1/models" 200,
    ".../v1//models" 404."""
    return (raw or "").strip().rstrip("/")


# This key belongs to one operator-pinned endpoint.  Keep an absent Actions
# secret absent: choosing a public provider default here would send a private
# gateway credential to the wrong host outside the workflow's own preflight.
BASE = normalize_base(os.environ.get("VERIFIER_BASE_URL"))

_API_PATHS = frozenset({"/models", "/responses", "/chat/completions"})
_BASE_ERROR = (
    "VERIFIER_BASE_URL must be an explicit HTTPS endpoint on an ordinary "
    "ASCII DNS hostname and ending in /v1; "
    "refusing the credentialed verifier request"
)
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_HEADER_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_CUSTOM_KEY_HEADER = re.compile(
    r"X-(?:[A-Za-z0-9]+-)*(?:API-)?Key\Z",
    re.IGNORECASE,
)
_RESERVED_HEADERS = frozenset({
    "connection",
    "content-length",
    "content-type",
    "cookie",
    "host",
    "proxy-authorization",
    "transfer-encoding",
})
_AUTH_HEADER_ERROR = (
    "VERIFIER_AUTH_HEADER must be Authorization or an X-*-Key header; "
    "refusing the credentialed verifier request"
)
_KEY_ERROR = (
    "verifier API key must be a nonempty visible-ASCII value; "
    "refusing the credentialed verifier request"
)


def _is_plain_dns_hostname(hostname: str) -> bool:
    """Accept one stable spelling, not resolver-normalized IP/IDN aliases."""
    if not hostname.isascii() or len(hostname) > 253:
        return False
    labels = hostname.split(".")
    return (
        len(labels) >= 2
        and labels[-1].isalpha()
        and all(_DNS_LABEL.fullmatch(label) for label in labels)
        and not any(label.casefold().startswith("xn--") for label in labels)
    )


def _api_url(path: str) -> str:
    """Build one fixed API URL or refuse before an auth header reaches I/O."""
    if path not in _API_PATHS:
        raise ValueError("verifier API path is not an allowed internal route")
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(BASE)
        hostname = parsed.hostname
        _ = parsed.port
    except (TypeError, ValueError):
        raise ProviderConfigError(_BASE_ERROR) from None
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or not _is_plain_dns_hostname(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or "?" in BASE
        or "#" in BASE
        or parsed.query
        or parsed.fragment
        or not parsed.path.rstrip("/").endswith("/v1")
        or "//" in parsed.path
        or any(ord(char) < 32 or char.isspace() for char in BASE)
    ):
        raise ProviderConfigError(_BASE_ERROR)
    return f"{BASE}{path}"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward a verifier credential across an HTTP redirect."""

    def redirect_request(
        self,
        _req: urllib.request.Request,
        _fp: Any,
        _code: int,
        _msg: str,
        _headers: Any,
        _newurl: str,
    ) -> None:
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirectHandler(),
)


def _urlopen(req: urllib.request.Request, timeout: int = 180) -> Any:
    """Open only through the no-proxy, no-redirect verifier transport."""
    return _NO_REDIRECT_OPENER.open(req, timeout=timeout)  # noqa: S310 -- URL validated above


def _endpoint_literals() -> tuple[str, ...]:
    values = {BASE} if isinstance(BASE, str) and BASE else set()
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(BASE)
        values.update(value for value in (parsed.netloc, parsed.hostname) if value)
    except (TypeError, ValueError):
        pass
    return tuple(sorted(values, key=len, reverse=True))


def _protected_literals() -> tuple[str, ...]:
    values = set(_endpoint_literals())
    if isinstance(KEY, str) and KEY:
        values.add(KEY)
    return tuple(sorted(values, key=len, reverse=True))


def _safe_diag(value: object, limit: int | None = 300) -> str:
    """Bound an untrusted diagnostic and remove credential/endpoint echoes."""
    text = str(value)
    # Delete rather than substitute a fixed marker: an operator credential can
    # itself equal (or be contained in) a marker such as ``<redacted>``.  One
    # longest-first literal set also handles a key that overlaps the endpoint.
    for literal in _protected_literals():
        text = re.sub(re.escape(literal), "", text, flags=re.IGNORECASE)
    return text if limit is None else text[:limit]


def _contains_protected_text(value: str) -> bool:
    folded = value.casefold()
    return any(literal.casefold() in folded for literal in _protected_literals())


def _contains_protected_value(value: object) -> bool:
    """Scan parsed peer output after JSON escapes have been materialized."""
    if isinstance(value, str):
        return _contains_protected_text(value)
    if isinstance(value, dict):
        return any(
            _contains_protected_value(key) or _contains_protected_value(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_protected_value(item) for item in value)
    return False


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """Classify one allowlisted condition; never publish a peer-controlled body."""
    try:
        body = exc.read(4096).decode(errors="replace")
    except Exception:
        body = ""
    if re.search(r"no usable account credential", body, re.IGNORECASE):
        return "no usable account credential (response body withheld)"
    return "response body withheld"


def auth_header() -> dict[str, str]:
    """The configured gateway's auth header. Defaults to the OpenAI convention
    ``Authorization: Bearer``. VERIFIER_AUTH_HEADER names a DIFFERENT header for
    gateways that reserve Authorization for upstream forwarding — verified live
    against the configured private gateway, whose provider adapter runs authMode="forward"
    and answers Bearer with 401 "opencodex API key required" while accepting
    X-OpenCodex-API-Key. Actions injects an EMPTY STRING for an unset repo
    variable, so the empty form must retain the Authorization default."""
    if (not isinstance(KEY, str) or not 1 <= len(KEY) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in KEY)):
        raise ProviderConfigError(_KEY_ERROR)
    name = (os.environ.get("VERIFIER_AUTH_HEADER") or "").strip()
    lower_name = name.casefold()
    if not name or lower_name == "authorization":
        return {"Authorization": f"Bearer {KEY or ''}"}
    if (not _HEADER_TOKEN.fullmatch(name)
            or lower_name in _RESERVED_HEADERS
            or not _CUSTOM_KEY_HEADER.fullmatch(name)):
        raise ProviderConfigError(_AUTH_HEADER_ERROR)
    return {name: KEY or ""}

# MODEL_PREFERENCE and FALLBACK_MODEL used to live here. Both existed to choose
# a SUBSTITUTE when a configured voice was not served by the account, and that
# substitution was removed as fail-open: the panel reviewed with voices the
# operator never chose and the run went green. resolve_panel_models() now
# refuses instead, so nothing reads either constant. They are deleted rather
# than left in place, because a merge-gate file whose constants describe a
# deleted mechanism is exactly the reading-versus-behaviour gap this file keeps
# being audited for.

# One DIFFERENT model per voice (override via VERIFIER_PANEL_MODELS).
DEFAULT_PANEL_MODELS = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]

# A real reason needs substance; "ok"/"1"/"n/a" cannot attest green.
MIN_REASON_LEN = 8

# ---------------------------------------------------------------- pure gates --


def decide(v: Any) -> dict[str, Any]:
    """Per-vote decision, fail-CLOSED: a reply without a boolean ``refuted``
    blocks; a refutation with confidence high OR medium blocks; only a
    low-confidence refutation passes (the required approver is stricter)."""
    if not isinstance(v, dict) or not isinstance(v.get("refuted"), bool):
        return {"block": True, "reason": "unparsable/no refuted field -> fail-closed"}
    if v["refuted"] and v.get("confidence") != "low":
        return {"block": True, "reason": v.get("reason") or "refutation (confidence >= medium)"}
    return {"block": False}


def model_matches(model_id: Any, want: Any) -> bool:
    """Does a resolved model ID count as the required approver? Exact ID or a
    DATED snapshot ``<want>-YYYY-MM-DD`` only — a variant suffix (-mini/-codex/
    -preview) is a DIFFERENT, possibly weaker model and must never impersonate
    the approver; a bare name prefix without separator does not count either."""
    if not model_id or not want:
        return False
    if model_id == want:
        return True
    return re.match(rf"^{re.escape(want)}-\d{{4}}-\d{{2}}-\d{{2}}$", model_id) is not None


def norm_reason(r: Any) -> str:
    """Whitespace-collapsed, trimmed, lowercased reason ('' for non-strings)."""
    return re.sub(r"\s+", " ", r).strip().lower() if isinstance(r, str) else ""


def has_substantive_reason(v: Any) -> bool:
    return len(norm_reason((v or {}).get("reason"))) >= MIN_REASON_LEN


def has_valid_proof(v: Any, challenge: str | None) -> bool:
    """Valid proof-of-check: '<challenge>-<tier 1-9999>' (no 0 / leading zero /
    >= 10000)."""
    proof = (v or {}).get("proof")
    if not isinstance(proof, str) or not challenge:
        return False
    pre = challenge + "-"
    return proof.startswith(pre) and re.match(r"^[1-9]\d{0,3}$", proof[len(pre):]) is not None


def _is_valid(x: Any) -> bool:
    return bool(x and x.get("ok") and isinstance(x.get("v"), dict)
                and isinstance(x["v"].get("refuted"), bool))


def approving_models(votes: list[dict], models: list[str]) -> list[str]:
    """Models with a VALID, APPROVING voice. An errored voice never approves, so
    an outage shrinks this list -- which is the point."""
    return [models[i] for i, x in enumerate(votes)
            if _is_valid(x) and x["v"]["refuted"] is False and models[i]]


def require_distinct_voices(models: list[str]) -> dict[str, Any]:
    """Every voice must be a DIFFERENT configured id.

    Independence here is a property of the panel's composition, not of anything
    this script can inspect: each voice is an inference-server GROUP that
    rotates over its own members, and the server reports the group id back as
    the model, never the member that served the call. Probed live -- a request
    to `combo/SOTA-A` answers with `"model": "combo/SOTA-A"`.

    So the operator attests that the groups are independent, and this gate
    enforces the one part that IS checkable: that two voices are not the same
    group. Two identical ids are one opinion counted twice, which is precisely
    the shape the required-approver gate exists to prevent.

    RESIDUAL, and it cannot be closed from here: if two groups share a member,
    rotation can land both voices on one model. Nothing in the API distinguishes
    that from genuine agreement. It is bounded by how the groups are built."""
    seen = [m for m in models if m]
    dupes = sorted({m for m in seen if seen.count(m) > 1})
    if dupes:
        return {"block": True,
                "reason": f"the panel lists the same voice more than once ({', '.join(dupes)}) "
                          "-- one opinion counted twice is not corroboration, fail-closed"}
    return {"block": False,
            "reason": f"{len(seen)} distinct voices ({', '.join(seen)})"}


def require_approvals(votes: list[dict], models: list[str], required_approver: str,
                      min_others: Any = 1, challenge: str | None = None) -> dict[str, Any]:
    """The ROLE gate (pure, testable). Green ONLY if ALL hold:
      1) the required approver is RESOLVED in the panel (>= 1 voice actually
         runs on that model; a fallback replacement does not count), AND
      2) EVERY required-approver voice is valid and EXPLICITLY approves —
         a refutation of ANY confidence (even low) is a veto — and carries its
         own substantive reason plus (when a challenge is set) a valid own
         proof-of-check, AND
      3) at least ``min_others`` DISTINCT non-approver models approve; repeat
         votes of the same model never count as independent corroboration.
    NaN/garbage ``min_others`` degrades to 1 (never fail-open)."""
    try:
        mo = int(min_others)
        need = mo if mo >= 1 else 1
    except (TypeError, ValueError):
        need = 1
    req_idx = {i for i in range(len(votes)) if model_matches(models[i], required_approver)}
    if not req_idx:
        return {"block": True,
                "reason": f'required approver "{required_approver}" not resolved in the panel '
                          "(not enabled or replaced by fallback) -> fail-closed"}
    for i in req_idx:
        x = votes[i]
        if not _is_valid(x):
            return {"block": True,
                    "reason": f'required approver "{required_approver}" without a valid vote '
                              "(error/unparsable) -> fail-closed"}
        if x["v"]["refuted"] is not False:
            return {"block": True,
                    "reason": f'required approver "{required_approver}" does NOT approve '
                              f'(refuted, confidence={x["v"].get("confidence", "?")}) -> veto, fail-closed'}
        if not has_substantive_reason(x["v"]):
            return {"block": True,
                    "reason": f'required approver "{required_approver}" without a substantive own '
                              "reason -> suspected sham green, fail-closed"}
        if challenge and not has_valid_proof(x["v"], challenge):
            return {"block": True,
                    "reason": f'required approver "{required_approver}" without a valid own '
                              "proof-of-check (challenge echo) -> fail-closed"}
    independent = {models[i] for i, x in enumerate(votes)
                   if i not in req_idx and _is_valid(x) and x["v"]["refuted"] is False and models[i]}
    if len(independent) < need:
        return {"block": True,
                "reason": f'only {len(independent)} independent approving model(s) besides '
                          f'"{required_approver}" (< {need}) -> no independent corroboration, fail-closed'}
    return {"block": False,
            "reason": f'"{required_approver}" approves + {len(independent)} independent model '
                      f"approval(s) (>= {need})"}


def _green(votes: list[dict]) -> list[dict]:
    """Votes that CARRY a green: explicit approvals only (refuted is False).
    Round-8 panel finding: the previous filter ("everything decide() does not
    block") also admitted LOW-CONFIDENCE REFUTATIONS, so a dissenter's
    reason/proof could satisfy the attestation majorities while an actual
    approving corroborator rode through canned and unattested. A low-confidence
    refutation neither blocks nor attests."""
    return [x for x in votes if _is_valid(x) and x["v"]["refuted"] is False]


def strict_any_refutation(votes: list[dict], models: list[str]) -> dict[str, Any]:
    """Strict mode (VERIFIER_STRICT_ANY_REFUTATION): block when ANY valid voice
    refutes with confidence high/medium — not only the required approver.

    ON unless explicitly disabled, so an absent or misspelled variable cannot
    retire the gate. It exists because the reference mechanism
    whose documented semantics green on Sol + one corroborator even if a third
    voice refutes (that property was flagged by the panel itself — Sol veto on
    PR #21 — and is hereby operator-selectable)."""
    for i, x in enumerate(votes):
        if _is_valid(x) and decide(x["v"])["block"]:
            return {"block": True,
                    "reason": f"strict mode: {models[i]} refutes "
                              f'(confidence={x["v"].get("confidence", "?")}) -> fail-closed'}
    return {"block": False, "reason": "strict mode: no high/medium refutation"}


# --------------------------------------------------- declared defect ledger --
#
# WHY THIS FIELD EXISTS — read before touching anything below it.
#
# THREE predecessors of this gate tried to INFER A FACT FROM A STRING, and the
# panel refuted every one of them with an input its author had not imagined:
#   * vendor_key()    — a vendor from a model id's leading token. "gpt-5.6-sol"
#                       -> "gpt" but "openai/gpt-4.1-mini" -> "openai", so ONE
#                       vendor in two spellings satisfied a CROSS-vendor gate.
#   * vendor_tokens() — vendor identity from token-set disjointness. "nvidia/"
#                       is a HOST prefix, so Meta and DeepSeek read as one
#                       vendor. Wrong in both directions.
#   * rung 4 — a LITERAL defect-word match in an approving reason. Also
#                       wrong in both directions, because the negator negates a
#                       different word than the defect one:
#                         "without fixing auth bypass"               -> no claim
#                         "not fixed: privilege escalation in admin" -> no claim
#                         "none of the auth paths bypass the check"  -> claim
#
# The common shape is not a bad word list. It is that a STRING WRITTEN BY THE
# REVIEWER was asked a question only the REVIEWER can answer. So the verdict
# schema now MAKES THE REVIEWER ANSWER IT in a typed field, and the gate reads
# the field instead of parsing the prose:
#
#     "defects": string[]   one entry per concrete defect found, [] iff none
#     refuted == False  AND  len(defects) > 0   ->  self-contradicting, BLOCK
#
# That decision is an integer comparison over a value the model itself declared.
# There is no vocabulary to miss, no negation to scope, no phrasing to shape:
# its input domain is {list of strings} + {everything else} and both halves are
# handled explicitly, so it has no "unimagined input" class at all.
#
# WHAT IT DOES NOT DO, stated here so nobody reads it as more than it is: a
# model that finds a defect, writes clean prose and declares [] is invisible to
# this channel. It converts a ONE-channel failure into a TWO-channel one (the
# model must now get the same fact wrong twice, in two representations, having
# been told they must agree) — it does not eliminate it. That residual is why
# the prose tripwire below is retained rather than deleted.

# Ledger entries a model writes when it means "empty". CLOSED set, and safe in
# the P1 direction because none of these strings can name a defect.
_LEDGER_NULLS = frozenset({
    "", "-", "--", "n/a", "na", "nil", "null", "none", "nothing", "empty", "[]",
    "no defect", "no defects", "no defects found", "none found", "nothing found",
    "no issue", "no issues", "no issues found", "no findings", "no concerns",
})

# Accepted spellings of the ledger key, in priority order. A model that answers
# the schema with "Defects" or "findings" HAS answered it; treating that as an
# absent field would silently disarm the gate on a capitalisation slip.
_LEDGER_KEYS = ("defects", "defect", "findings", "defects_found")


def _ledger_key_state(k: Any) -> str:
    """"accepted" | "lookalike" | "other" for one key of a verdict object.

    LOOKALIKE is the one worth explaining. A key that NFKC-normalises into an
    accepted spelling but is not byte-identical to it is not a typo, it is a
    key wearing the ledger's name:

        {"defects": [], "defe\u0441ts": ["app/a.py:1 - auth bypass"]}

    (Cyrillic \u0441). Measured before this function existed: rung 3 was
    satisfied by the real, empty ``defects``, the lookalike was not recognised
    as a ledger key at all, and the vote PASSED with a declared defect in it.
    Such a key is treated as MALFORMED rather than ignored -- an object that
    carries two keys normalising to one name is ambiguous, and ambiguity in the
    verdict fails closed, the same way a duplicate key does.

    Everything else is "other", and an "other" key is genuinely not read. That
    is a stated limit, not an oversight: a defect written under a name nobody
    agreed on cannot be found by matching names. Rung 4's prose scan is what
    covers the vote that describes a defect somewhere this does not look."""
    if not isinstance(k, str):
        return "other"
    flat = k.strip().lower()
    if flat in _LEDGER_KEYS:
        return "accepted"
    # A NON-ASCII KEY IS A LOOKALIKE, whatever it spells. NFKC was the first
    # attempt and it is the wrong tool: it folds compatibility forms (fullwidth,
    # ligatures) but NOT cross-script confusables, so Cyrillic \u0441 in
    # "defe\u0441ts" survived it untouched and the vote still passed. A
    # confusables table would rot; this contract's keys are ASCII by definition,
    # so a key outside ASCII cannot be matched reliably and the object carrying
    # it is ambiguous. Ambiguity fails closed here, as it does for a duplicate.
    if not flat.isascii():
        return "lookalike"
    if unicodedata.normalize("NFKC", flat) in _LEDGER_KEYS:
        return "lookalike"
    return "other"


def _ledger_fields(v: Any) -> tuple[list[Any], bool]:
    """(values under EVERY accepted spelling, saw_a_lookalike).

    All of them, not the first one: a vote carrying both ``"defects": []`` and
    ``"findings": ["auth bypass"]`` has declared a defect, and reading only the
    higher-priority key would let the other one hide it."""
    if not isinstance(v, dict):
        return [], False
    states = {k: _ledger_key_state(k) for k in v}
    return ([v[k] for k, st in states.items() if st == "accepted"],
            any(st == "lookalike" for st in states.values()))


def has_conformant_ledger(v: Any) -> bool:
    """True only when the CANONICAL ``defects`` key is present AND is a list.

    declared_defects() reads every accepted spelling and tolerates the enumerated
    empty-sentinels on purpose: none of those strings can name a defect, so
    reading them is safe in the P1 direction, and reading every spelling stops an
    alias from hiding an entry. But TOLERATING a shape is not the same as the
    schema having been ANSWERED, and require_ledger claims the latter.

    Group A refuted exactly that gap: with VERIFIER_REQUIRE_DEFECT_LIST=true,
    ``"defects": "none"`` and an alias-only ``"findings": []`` both passed while
    carrying no ``defects: string[]`` at all -- "required" did not mean required.
    Measured before the fix:

        "defects": []       -> passes   (conformant)
        "defects": "none"   -> passes   (NOT conformant, should block)
        "findings": []      -> passes   (NOT conformant, should block)
        no ledger           -> blocks
    """
    if not isinstance(v, dict):
        return False
    return any(isinstance(k, str) and k.strip().lower() == "defects" and isinstance(val, list)
               for k, val in v.items())


def declared_defects(v: Any) -> tuple[str, list[str]]:
    """Read a vote's MACHINE-DECLARED defect ledger.

    Returns ("ok", items) | ("missing", []) | ("malformed", []). Total on every
    input: no parsing, no inference, no natural language. ``items`` is the
    union of the declared lists with the enumerated empty-sentinels removed."""
    raws, lookalike = _ledger_fields(v)
    if lookalike:
        return ("malformed", [])
    if not raws:
        return ("missing", [])
    items: list[str] = []
    for raw in raws:
        # A bare string is a schema error, but a bare string that IS one of the
        # enumerated empty-sentinels ('"defects": "none"') cannot name a defect,
        # so reading it as [] is safe in the P1 direction and removes the most
        # likely rollout wedge. Any OTHER non-list fails closed.
        if isinstance(raw, str):
            if re.sub(r"\s+", " ", raw).strip().lower() in _LEDGER_NULLS:
                continue
            return ("malformed", [])
        if not isinstance(raw, list):
            return ("malformed", [])
        for entry in raw:
            if not isinstance(entry, str):
                return ("malformed", [])
            s = re.sub(r"\s+", " ", entry).strip()
            if s.lower() not in _LEDGER_NULLS:
                items.append(s)
    return ("ok", items)


# ------------------------------------------------------- prose tripwire ------
#
# SECONDARY, DEMOTED, AND HONESTLY LABELLED: everything from here to
# defect_claims() IS a heuristic and a determined writer evades it. It is kept
# unchanged — deliberately NOT rewritten — because three rewrites of exactly
# this rule have now been refuted, each shipping a fresh BYPASS in exchange for
# the false positives it fixed. The known holes are documented on
# defect_claims() itself. It is the belt to the ledger's braces, not the
# mechanism.

# Words that name a DEFECT rather than describe a review. Deliberately narrow:
# each denotes a concrete failure, not a hedge ("could", "consider", "prefer"),
# so ordinary approving prose does not trip the gate.
#
# INFLECTIONS: the list is matched in the ordinary inflections of the SAME
# terms ("bypassed", "injections", "deadlocks"), which the earlier bare-\b form
# missed. Widening a term is MONOTONE — it can only turn a PASS into a BLOCK,
# never the reverse — so it trades P2 for P1 in the direction this file
# resolves conflicts. Adding a NEW concept here is not monotone in the same
# way and is a separate, reviewed decision.
# ONE definition, TWO consumers: the regex that BLOCKS is built from this list,
# and the sentence in build_system_prompt() that tells a voice what to avoid is
# rendered from the same list. The two cannot drift.
#
# They did drift, and it was measured: the prompt named six terms and trailed
# off with "...", while the regex also blocked `fails open`, `vulnerable`,
# `exploitable`, `regressed`, `deadlocked` and `privilege escalations`. Three
# natural approvals -- "verified the header path does not fail open",
# "confirmed the gate cannot fail open on a missing key", "the substitution path
# no longer fails open" -- were blocked by a rule the voice was never shown. A
# gate that publishes a rule narrower than the one it enforces is unanswerable
# by the reviewer it judges.
#
# (published term, regex alternative)
_DEFECT_VOCAB: tuple[tuple[str, str], ...] = (
    # EVERY alternative is a stem plus \w*, without exception. Enumerating
    # inflections was the previous form and it leaked exactly the way an
    # enumeration does: `fails? open` missed "failed open" and "failing open",
    # `inject(?:ions|ion|ed|able|s)` missed "injecting". combo/SOTA-A found all
    # three. Those are not regressions -- main misses them too -- but a list of
    # endings is a list somebody has to keep complete, and the next inflection
    # is always the one nobody wrote down. A stem cannot be incomplete that way.
    ("bypass",              r"bypass\w*"),
    ("fail-open",           r"fail-open\w*"),
    ("fails open",          r"fail\w* open"),
    ("unauthenticated",     r"unauthenticated\w*"),
    ("injection",           r"inject\w*"),
    ("vulnerability",       r"vulnerab\w*"),
    ("exploitable",         r"exploitab\w*"),
    ("race condition",      r"race condition\w*"),
    ("deadlock",            r"deadlock\w*"),
    ("regression",          r"regress\w*"),
    ("data loss",           r"data loss\w*"),
    ("privilege escalation", r"privilege escalation\w*"),
)

_DEFECT_WORDS = re.compile(r"\b(" + "|".join(a for _, a in _DEFECT_VOCAB) + r")\b", re.I)

#: Every published term, for the prompt. No ellipsis: the voice is told the
#: whole rule it is judged by.
_DEFECT_VOCAB_PUBLISHED = ", ".join(t for t, _ in _DEFECT_VOCAB)



#: Clause delimiters, for quoting the offending fragment back to the operator.
_CLAUSE_BOUNDARY = re.compile(r"[;,.:]")


def _clause_at(text: str, start: int) -> str:
    """The clause containing offset ``start``.

    Reported with the block so an operator can tell a real inconsistency from a
    phrasing artefact WITHOUT opening the raw job log. A bare word could not:
    deciding whether `("fail-open")` was a confession or the tail of "no
    fail-open" took reading the log by hand (PR #64)."""
    lo = max((m.end() for m in _CLAUSE_BOUNDARY.finditer(text[:start])), default=0)
    tail = _CLAUSE_BOUNDARY.search(text, start)
    return text[lo:tail.start() if tail else len(text)].strip()


def attest_consistency(votes: list[dict], models: list[str],
                       require_ledger: bool = False) -> dict[str, Any]:
    """A GREEN vote that itself reports a defect is not an approval — it is a
    model that analysed correctly and then set the boolean wrong. decide() reads
    only ``refuted``, so without this gate that vote counts toward the quorum
    AND supplies a substantive, distinct reason that helps attest_reasons pass.

    Found adversarially: a panelist returned refuted=false with a reason that
    named two concrete defects. Fail-CLOSED — an inconsistent vote is discarded
    as a parse failure, not resolved in favour of green.

    THE LADDER, per green vote, first hit wins, every rung fail-CLOSED:
      1. a ``defects`` ledger present but not a list of strings -> the schema
         was not answered, BLOCK.
      2. NO ledger at all -> BLOCK only under VERIFIER_REQUIRE_DEFECT_LIST
         (``require_ledger``). Tolerated by default ON PURPOSE: a voice that has
         not seen the new prompt would otherwise block every unanimous green,
         which is the failure mode that gets a gate deleted. Rung 4 still
         applies to it, so tolerating absence costs nothing this file did not
         already cost before the ledger existed.
      3. ledger non-empty -> the model declared a defect and greened anyway,
         BLOCK. THIS RUNG IS THE MECHANISM: two declarations about one fact,
         compared as values, no prose read, nothing inferred.
      4. the vote's own prose names a defect word -> BLOCK.

    RUNG 4'S MATCH IS DELIBERATELY LITERAL, and stays that way. A green reason
    saying "no fail-open" is discarded even though it asserts the OPPOSITE of a
    defect, which cost a unanimous 3/3 approval on PR #64. The fix for that is
    upstream, in `build_system_prompt`: a green vote is asked to name code paths
    and no defect term at all, so the ambiguous phrasing is never produced.

    Teaching this function to read negation was tried, in two repositories and
    two independent attempts, and withdrawn both times. Seven laundering paths
    in five review rounds on one side ("no auth: privilege escalation via
    /admin", "not regression-free", "not without injection", ...); on the other,
    a three-word negator window that measured WEAKER THAN MAIN on 71 of 165
    defect-affirming reasons, shipped with tests that asserted the fail-open and
    a comment telling the next reviewer not to repair it. Negation scope in free
    prose is not something a regex can be trusted to decide, and every attempt
    traded a safe over-block for an unsafe under-block. If this needs to change
    again, change what the models are ASKED to write, not what this reads.

    WHERE P1 AND P2 CONFLICT, P1 WINS, at every rung. A false block costs one
    re-vote; a false pass costs a merged defect that three voices signed."""
    for i, x in enumerate(votes):
        if not _is_valid(x) or x["v"]["refuted"] is not False:
            continue
        state, declared = declared_defects(x["v"])
        if state == "malformed":
            return {"block": True,
                    "reason": f'{models[i]}: refuted=false and its "defects" ledger is not a '
                              "JSON array of strings -> schema not answered, fail-closed"}
        if state == "missing" and require_ledger:
            return {"block": True,
                    "reason": f'{models[i]}: refuted=false without the required "defects" '
                              "ledger while VERIFIER_REQUIRE_DEFECT_LIST=true "
                              "-> unverifiable green, fail-closed"}
        if require_ledger and not has_conformant_ledger(x["v"]):
            return {"block": True,
                    "reason": f'{models[i]}: refuted=false without a conformant "defects" '
                              "array -- VERIFIER_REQUIRE_DEFECT_LIST=true requires "
                              "defects: string[], not a sentinel string and not an alias key "
                              "-> schema not answered, fail-closed"}
        if declared:
            return {"block": True,
                    "reason": f'{models[i]}: refuted=false but its own defects ledger declares '
                              f'{len(declared)} defect(s) ("{declared[0][:120]}") '
                              "-> the boolean contradicts the vote's own findings, fail-closed"}
        reason = norm_reason(x["v"].get("reason"))
        hit = _DEFECT_WORDS.search(reason)
        if hit:
            return {"block": True,
                    "reason": f'{models[i]}: refuted=false with an empty defects ledger, but '
                              f'its own reason names a defect ("{hit.group(0)}" in '
                              f'"{_clause_at(reason, hit.start())}") '
                              "-> inconsistent vote, fail-closed"}
    return {"block": False,
            "reason": f"{len(_green(votes))} green vote(s): empty defect ledger, "
                      "no defect claim in their prose"}


def attest_reasons(votes: list[dict], panel_size: int) -> dict[str, Any]:
    """Anti sham-green gate: a MAJORITY of the green-carrying votes must have a
    substantive reason, and a MAJORITY of those must be mutually DISTINCT after
    normalization. Only green votes count (a refutation's reason must not
    inflate the release's distinctness)."""
    need = panel_size // 2 + 1
    normed = [s for s in (norm_reason(x["v"].get("reason")) for x in _green(votes))
              if len(s) >= MIN_REASON_LEN]
    if len(normed) < need:
        return {"block": True,
                "reason": f"only {len(normed)}/{panel_size} green votes with a real reason "
                          f"(< majority {need}) -> suspected sham green, fail-closed"}
    if len(set(normed)) < need:
        return {"block": True,
                "reason": f"only {len(set(normed))} distinct green reason(s) among {len(normed)} "
                          f"(< majority {need}) -> suspected sham green, fail-closed"}
    return {"block": False, "reason": f"{len(set(normed))} independent green reasons attested"}


def attest_proof(votes: list[dict], challenge: str, panel_size: int) -> dict[str, Any]:
    """Anti hardcoded-green gate: a MAJORITY of the green-carrying votes must
    echo the per-run random challenge as a valid proof-of-check."""
    need = panel_size // 2 + 1
    proven = [x for x in _green(votes) if has_valid_proof(x["v"], challenge)]
    if len(proven) < need:
        return {"block": True,
                "reason": f"only {len(proven)}/{panel_size} green votes with a valid proof-of-check "
                          f"(challenge echo) (< majority {need}) -> suspected hardcoded green, fail-closed"}
    return {"block": False, "reason": f"{len(proven)} proof-of-check (challenge echo) confirmed"}


# ------------------------------------------------------------- diff assembly --

# PRIVACY: only code goes to the foreign vendor. One exclude pathspec PER
# extension (git ':(glob)' does not brace-expand). data/ (runtime DB volume)
# excluded as a category.
# Data-file classes added after the panel's own round-3 finding (root-level
# customers.csv/backup.sql would have gone to the vendor). NOTE: .json stays
# reviewable ON PURPOSE — frozen_methodology.json IS the methodology and must
# be visible to the panel. A denylist is inherently incomplete; that residual
# is documented in docs/INDEPENDENT_REVIEW_PANEL.md.
EXCLUDE_EXTS = ["webp", "png", "jpg", "jpeg", "gif", "ico", "svg", "avif", "bmp", "tiff",
                "woff", "woff2", "ttf", "otf", "eot", "pdf", "geojson", "db", "rds", "xlsx",
                "csv", "tsv", "sql", "jsonl", "ndjson", "parquet", "feather", "sqlite",
                "sqlite3", "dump", "bak", "pickle", "pkl", "npz", "npy"]
# icase: pathspecs are case-sensitive by default — an uppercase .PNG/.SVG would
# otherwise reach the vendor (found by the panel itself: Sol veto on PR #21).
_EXCLUDES = [":(exclude,icase,glob)data/**"] + [f":(exclude,icase,glob)**/*.{e}" for e in EXCLUDE_EXTS]


class DiffError(RuntimeError):
    """A REQUIRED diff command failed — the panel must BLOCK, never green on
    the resulting emptiness (round-6 panel finding: text=True decode errors and
    git failures were silently converted to empty output)."""


class ProviderConfigError(RuntimeError):
    """The endpoint is misconfigured, so no panel can be assembled. Distinct
    from a vote failure: this must name the CAUSE, because the previous
    behaviour (fall back to one pinned model for every voice) turned a wrong
    BASE URL into three identical, unexplained vote errors."""


def _sh(args: list[str], *, required: bool = False) -> str:
    """Run a git command; decode with errors="replace" so invalid UTF-8 becomes
    VISIBLE replacement characters instead of an empty (falsely reviewable)
    diff. required=True turns any failure into a blocking DiffError."""
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed git argv built from constants, no shell
            args, capture_output=True, timeout=120)
    except Exception as exc:
        if required:
            raise DiffError(f"{' '.join(args[:3])}...: {exc}") from exc
        return ""
    out = proc.stdout.decode("utf-8", errors="replace")
    if required and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[:200]
        raise DiffError(f"{' '.join(args[:3])}... exited {proc.returncode}: {err}")
    return out


def base_branch() -> str:
    """The PR's target branch (GITHUB_BASE_REF; empty on non-PR events -> main).
    Round-4 panel finding: a hard-coded origin/main mis-based the diff for PRs
    targeting any other branch, letting them green unreviewed."""
    return os.environ.get("GITHUB_BASE_REF") or "main"


def review_range() -> tuple[str, str]:
    """(merge_base, head) for the review.

    The head comes EXPLICITLY from VERIFIER_HEAD_SHA, because the job runs from
    the DEFAULT BRANCH (pull_request_target) where HEAD *is* main: without this
    merge-base(main, HEAD) diffs main against itself, yields an empty diff and
    goes permanently FAKE-GREEN. Reproduced before this guard existed, so the
    empty-diff path is fail-CLOSED whenever a candidate sha was supplied.

    The sha is validated as 40-hex and must not be an ancestor of the base --
    an ancestor means there is nothing to review, which in a PR context is a
    fault, not an approval."""
    head = (os.environ.get("VERIFIER_HEAD_SHA") or "").strip() or "HEAD"
    base = (os.environ.get("VERIFIER_BASE_BRANCH") or "").strip() or base_branch()
    if head != "HEAD" and not re.fullmatch(r"[0-9a-f]{40}", head):
        raise DiffError(f"VERIFIER_HEAD_SHA is not a 40-hex sha: {head!r}")
    mb = _sh(["git", "merge-base", f"origin/{base}", head], required=True).strip()
    if not mb:
        raise DiffError(f"empty merge-base for origin/{base}...{head}")
    if mb == _sh(["git", "rev-parse", head], required=True).strip():
        raise DiffError(
            f"candidate {head[:12]} is an ancestor of origin/{base} — nothing to "
            f"review; in a PR context that is a fault, fail-closed")
    return mb, head


def diff_commands(merge_base: str, head: str = "HEAD") -> dict[str, list[str]]:
    """The three diff invocations. The NAME-STATUS list carries ALL changed
    paths WITHOUT excludes (round-4 panel finding: filtering the authoritative
    list let an excluded-only PR return an empty diff and auto-green with zero
    votes) — paths are not content; the privacy excludes protect CONTENTS and
    stay on stat/body."""
    return {
        "names": ["git", "diff", "--name-status", f"{merge_base}...{head}"],
        "stat": ["git", "diff", "--stat", f"{merge_base}...{head}", "--", "."] + _EXCLUDES,
        "body": ["git", "diff", f"{merge_base}...{head}", "--", "."] + _EXCLUDES,
    }


def truncate_marked(text: str, cap: int, label: str) -> str:
    """Cap text with an EXPLICIT marker — silent truncation let unreviewed
    changes green (panel round-3 finding); a cut must be visible to reviewers."""
    if len(text) <= cap:
        return text
    return (text[:cap] +
            f"\n[{label} TRUNCATED — {len(text) - cap} of {len(text)} bytes omitted]")


def build_diff() -> str:
    """COMPLETE changed-file list (--name-status) + capped stat + capped body,
    every cap explicitly marked; base = merge-base with main."""
    # No HEAD~1 fallback (round-7 panel finding): a failed merge-base on a
    # multi-commit PR would silently shrink the review to the TIP commit only.
    # checkout runs with fetch-depth: 0, so origin/<base> is always present in
    # CI; a merge-base failure is a real fault and must BLOCK.
    mb, head = review_range()
    cmds = diff_commands(mb, head)
    names = _sh(cmds["names"], required=True)
    stat = _sh(cmds["stat"], required=True)
    body = _sh(cmds["body"], required=True)
    if not names.strip() and not body.strip():
        return ""
    return (f"# COMPLETE changed-file list (authoritative — ALL changed paths, including "
            f"files whose CONTENT is privacy-excluded; contents of excluded classes are "
            f"never sent):\n"
            f"{truncate_marked(names, 100_000, 'FILE LIST')}\n\n"
            f"# Diffstat:\n{truncate_marked(stat, 8_000, 'DIFFSTAT')}\n\n"
            f"# Code changes (binaries/assets/data excluded):\n"
            f"{truncate_marked(body, 200_000, 'DIFF BODY')}")


# ------------------------------------------------------------------ prompts --

LENSES = [
    " Focus of this review: security holes and data protection.",
    " Focus of this review: correctness, broken invariants, fail-closed becoming fail-open.",
    " Focus of this review: a gate/control that stops firing; widened blast radius; "
    "and for this repository specifically: any change that alters a scored value, the frozen "
    "methodology artifact, its SHA-256 pin, or the golden fixture without declaring it.",
]


def build_system_prompt(challenge: str) -> str:
    return (
        "You are a hostile code reviewer from a DIFFERENT AI vendor. Goal: REFUTE that this "
        "diff is correct and safe. Look for REAL defects visible in the code: security hole, "
        "broken invariant, a gate/control that stops firing, fail-closed turned fail-open, "
        "widened blast radius, an undeclared change to a frozen methodology constant or golden "
        "fixture. The diff may be truncated; the --stat overview lists ALL files. NOT a defect: "
        "style/readability; 'could be more secure'; speculative attacks without a concrete "
        "path; hypothetical hardening; anything without nameable misbehavior. BOUNDARY: refute "
        "ONLY on a CONCRETE, reproducible defect with nameable misbehavior (wrong output, "
        "crash, a control demonstrably no longer firing, a really exploitable hole WITH an "
        "attack path). The changed-FILE LIST is COMPLETE; diffstat/body may be truncated, "
        "and any truncation is explicitly marked — if a marked truncation hides files you "
        "would need to see to approve, say so instead of approving blind. "
        "No concrete failure path -> refuted=false. Do NOT refute over the "
        "inherent cross-vendor trust assumption itself (that a malicious endpoint could fool "
        "the verifier including the challenge echo) — that is the DOCUMENTED residual, "
        "compensated by the deterministic CI gate; it is not a defect of THIS diff. "
        "Answer ONLY as JSON, no prose/markdown: "
        '{"refuted": boolean, "confidence": "high"|"medium"|"low", "reason": string, '
        '"defects": string[], "proof": string}. '
        'DEFECT LEDGER: "defects" is REQUIRED on every answer and is a JSON array of '
        "strings — ONE ENTRY PER CONCRETE DEFECT you found, each in the schema "
        "'path/file:line — defect — misbehavior', and the EMPTY array [] if you found "
        'none. Every defect you can name anywhere in your answer MUST also appear in '
        '"defects", and refuted MUST be true whenever "defects" is non-empty. Never '
        "leave a finding out because you judged it minor: if you can name it, list it "
        "and refute on it. ALWAYS fill reason (also for refuted=false), maximally terse/"
        "machine-like, abbreviations fine, no full sentences, but technically informative. "
        "Name ALL defects found — each EXTREMELY compact, separated by ' ; '; rather abbreviate "
        "each point harder than drop one. Upper bound ~800 chars. For refuted=true use the "
        "schema 'path/file:line — defect — misbehavior' per point. For refuted=false say in "
        "3-8 words WHAT WAS CHECKED — name the code paths, not defects. A green reason "
        "must contain NO defect term (" + _DEFECT_VOCAB_PUBLISHED + ") OR ANY "
        "INFLECTION OF ONE, EVEN NEGATED: a green vote naming one is discarded "
        "as self-contradictory, so write 'throttle + lock ordering verified', never "
        "'no race condition'. "
        "PROOF-OF-CHECK: for refuted=false, proof MUST be EXACTLY '" + challenge + "-<tier>' "
        "where <tier> is an integer 1-9999 you choose at random (e.g. '" + challenge + "-4213'). "
        "That proves you really executed this check; a missing/wrong proof invalidates a green. "
        "For refuted=true, proof is optional. refuted=true ONLY on a concrete, nameable defect."
    )


# ----------------------------------------------------------------- selftest --


def selftest() -> None:
    global BASE, KEY

    def expect(cond: bool, msg: str) -> None:
        if not cond:
            print(f"BLOCK selftest: {msg}", file=sys.stderr)
            sys.exit(1)

    # decide()
    expect(decide({"refuted": True, "confidence": "high"})["block"] is True, "high refutation must block")
    expect(decide({"refuted": True, "confidence": "medium"})["block"] is True, "medium refutation must block")
    expect(decide({})["block"] is True, "missing refuted field must fail-closed block")
    expect(decide(None)["block"] is True, "null/unparsable must fail-closed block")
    expect(decide({"refuted": False})["block"] is False, "no refutation must pass")
    expect(decide({"refuted": True, "confidence": "low"})["block"] is False, "low-confidence refutation passes")
    # model_matches()
    expect(model_matches("gpt-5.6-sol", "gpt-5.6-sol") is True, "exact ID matches")
    expect(model_matches("gpt-5.6-sol-2026-07-01", "gpt-5.6-sol") is True, "dated snapshot matches")
    expect(model_matches("gpt-5.6", "gpt-5.6-sol") is False, "shorter model must NOT match")
    expect(model_matches("gpt-5.6-solaris", "gpt-5.6-sol") is False, "prefix without separator must NOT match")
    expect(model_matches(None, "gpt-5.6-sol") is False, "missing model must NOT match")
    expect(model_matches("gpt-5.6-sol-mini", "gpt-5.6-sol") is False, "-mini variant must NOT match")
    expect(model_matches("gpt-5.6-sol-codex", "gpt-5.6-sol") is False, "-codex variant must NOT match")
    expect(model_matches("gpt-5.6-sol-preview", "gpt-5.6-sol") is False, "-preview variant must NOT match")
    # require_approvals() — votes[i] belongs to MDL[i]; Sol at index 1.
    MDL = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
    A = {"ok": True, "v": {"refuted": False, "reason": "reason long enough a"}}
    A2 = {"ok": True, "v": {"refuted": False, "reason": "reason long enough b"}}
    RF = {"ok": True, "v": {"refuted": True, "confidence": "high", "reason": "real bug"}}
    E = {"ok": False, "reason": "API 500"}
    expect(require_approvals([A, A2, A], MDL, "gpt-5.6-sol", 1)["block"] is False, "Sol + 2 others approve -> green")
    expect(require_approvals([RF, A2, A], MDL, "gpt-5.6-sol", 1)["block"] is False, "Sol approves, 1 refutes, 1 approves -> green")
    expect(require_approvals([A, RF, A], MDL, "gpt-5.6-sol", 1)["block"] is True, "Sol REFUTES -> veto, block")
    expect(require_approvals([A, E, A], MDL, "gpt-5.6-sol", 1)["block"] is True, "Sol vote errored -> fail-closed block")
    expect(require_approvals([A, {"ok": True, "v": None}, A], MDL, "gpt-5.6-sol", 1)["block"] is True, "Sol unparsable -> block")
    expect(require_approvals([RF, A, RF], MDL, "gpt-5.6-sol", 1)["block"] is True, "no independent approval -> block")
    expect(require_approvals([A, A2, A], ["gpt-5.3-codex", "gpt-5.6", "gpt-4.1-mini"], "gpt-5.6-sol", 1)["block"] is True,
           "Sol not in panel (fallback) -> fail-closed block")
    expect(require_approvals([A, A2, A], ["gpt-5.3-codex", "gpt-5.6-sol-2026-07-01", "gpt-4.1-mini"], "gpt-5.6-sol", 1)["block"] is False,
           "Sol as dated snapshot counts -> green")
    expect(require_approvals([A, A2, RF], MDL, "gpt-5.6-sol", 2)["block"] is True, "MIN_OTHERS=2 with 1 approver -> block")
    expect(require_approvals([A, A2, A], MDL, "gpt-5.6-sol", 2)["block"] is False, "MIN_OTHERS=2 with 2 approvers -> green")
    RFLOW = {"ok": True, "v": {"refuted": True, "confidence": "low", "reason": "small doubt"}}
    expect(require_approvals([A, RFLOW, A], MDL, "gpt-5.6-sol", 1)["block"] is True, "Sol low-confidence refutation -> veto")
    SOLO = ["gpt-5.6-sol", "gpt-5.6-sol", "gpt-5.6-sol"]
    expect(require_approvals([A, A2, A], SOLO, "gpt-5.6-sol", 1)["block"] is True, "single-pin all-Sol: no independent corroboration -> block")
    expect(require_approvals([A, RF, A], SOLO, "gpt-5.6-sol", 1)["block"] is True, "single-pin: one Sol voice refutes -> veto")
    DUP = ["gpt-4.1-mini", "gpt-5.6-sol", "gpt-4.1-mini"]
    expect(require_approvals([A, A2, A], DUP, "gpt-5.6-sol", 1)["block"] is False, "1 distinct independent model suffices at MIN_OTHERS=1")
    expect(require_approvals([A, A2, A], DUP, "gpt-5.6-sol", 2)["block"] is True, "same model twice counts once -> < 2 -> block")
    expect(require_approvals([RF, A2, RF], MDL, "gpt-5.6-sol", float("nan"))["block"] is True, "NaN min_others -> need 1; none approve -> block")
    expect(require_approvals([A, A2, RF], MDL, "gpt-5.6-sol", float("nan"))["block"] is False, "NaN min_others -> need 1; codex approves -> green")
    CH2 = "selftest-challenge"
    sol_empty = {"ok": True, "v": {"refuted": False, "reason": "", "proof": f"{CH2}-7"}}
    sol_no_proof = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol"}}
    sol_good = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol", "proof": f"{CH2}-7"}}
    expect(require_approvals([A, sol_empty, A], MDL, "gpt-5.6-sol", 1, CH2)["block"] is True, "Sol without own substantive reason -> block")
    expect(require_approvals([A, sol_no_proof, A], MDL, "gpt-5.6-sol", 1, CH2)["block"] is True, "Sol without own valid proof -> block")
    expect(require_approvals([A, sol_good, A], MDL, "gpt-5.6-sol", 1, CH2)["block"] is False, "Sol with reason + proof + corroboration -> green")
    expect(require_approvals([A, sol_no_proof, A], MDL, "gpt-5.6-sol", 1)["block"] is False, "no challenge -> no proof required")
    # attest_reasons()
    def R(reason: Any, refuted: bool = False) -> dict:
        return {"ok": True, "v": {"refuted": refuted, "reason": reason}}
    g1, g2, g3 = "reason one aaaa", "reason two bbbb", "reason three cccc"
    expect(attest_reasons([R(g1), R(g2), R(g3)], 3)["block"] is False, "3 distinct green reasons must pass")
    expect(attest_reasons([R(g1), R(g1), R(g2)], 3)["block"] is False, "2/3 majority distinct suffices (one duplicate allowed)")
    expect(attest_reasons([R(g1), R(g1), R(g1)], 3)["block"] is True, "all-identical (canned) green reasons must block")
    expect(attest_reasons([R("Reason AAA XX"), R(" reason   aaa xx "), R("reason aaa xx")], 3)["block"] is True,
           "whitespace/case variants collapse -> distinct=1 -> block")
    expect(attest_reasons([R("a      b"), R("c      d"), R("e      f")], 3)["block"] is True,
           "whitespace padding (< MIN normalized) is not a real reason -> block")
    expect(attest_reasons([R(""), R(""), R("")], 3)["block"] is True, "empty reasons must fail-closed block")
    expect(attest_reasons([R(g1), R(g1), R("real bug here", True)], 3)["block"] is True,
           "canned green majority; the refutation's reason must NOT count -> block")
    expect(attest_reasons([R("ok"), R("ok"), E], 3)["block"] is True, "shortest token 'ok' is not a real reason -> block")
    expect(attest_reasons([R(1), R(2), E], 3)["block"] is True, "numeric reason (coercion) must NOT count -> block")
    expect(attest_reasons([R(g1), R(g2), E], 3)["block"] is False, "2/3 green majority with distinct reasons suffices")
    expect(attest_reasons([R(g1), R(g1), E], 3)["block"] is True, "2 green but only 1 distinct (< majority) -> block")
    expect(attest_reasons([R(g1), E, E], 3)["block"] is True, "only 1/3 green reasoned (< majority) -> fail-closed block")
    expect(attest_reasons([{"ok": True, "v": None}, R(g1), R(g2)], 3)["block"] is False, "unparsable vote ignored, rest distinct -> pass")
    expect(attest_reasons([R("reason solo aaaa")], 1)["block"] is False, "panel=1 with a real reason passes")
    # attest_proof()
    CH = "selftest-challenge"
    def PR(proof: str, refuted: bool = False) -> dict:
        return {"ok": True, "v": {"refuted": refuted, "reason": "reason long enough", "proof": proof}}
    expect(attest_proof([PR(f"{CH}-7"), PR(f"{CH}-42"), PR(f"{CH}-9")], CH, 3)["block"] is False, "3 valid echoes must pass")
    expect(attest_proof([PR(f"{CH}-7"), PR(f"{CH}-42"), E], CH, 3)["block"] is False, "2/3 majority of valid proofs suffices")
    expect(attest_proof([PR("no-echo-1"), PR("no-echo-2"), PR("no-echo-3")], CH, 3)["block"] is True, "wrong challenge must block")
    expect(attest_proof([PR(f"{CH}-7"), PR(""), PR("")], CH, 3)["block"] is True, "only 1/3 with valid proof -> block")
    expect(attest_proof([PR(f"{CH}-x"), PR(f"{CH}-y"), PR(f"{CH}-z")], CH, 3)["block"] is True, "non-numeric tier -> invalid proof -> block")
    expect(attest_proof([PR(f"{CH}-0"), PR(f"{CH}-0"), PR(f"{CH}-0")], CH, 3)["block"] is True, "tier 0 is invalid -> block")
    expect(attest_proof([PR(f"{CH}-10000"), PR(f"{CH}-10000"), PR(f"{CH}-10000")], CH, 3)["block"] is True, "tier >= 10000 invalid -> block")
    expect(attest_proof([PR(f"{CH}-9999"), PR(f"{CH}-1"), PR(f"{CH}-500")], CH, 3)["block"] is False, "boundary tiers 1 and 9999 valid -> pass")
    expect(attest_proof([PR(f"{CH}-7"), PR(f"{CH}-8"), PR("nope-1", True)], CH, 3)["block"] is False,
           "refuting vote needs no proof while green majority has valid proofs")
    expect(attest_proof([PR(f"{CH}-7")], CH, 1)["block"] is False, "panel=1 with valid proof passes")
    expect(attest_proof([PR(f"{CH}-7"), PR(f"{CH}-8"), PR(f"{CH}-9")], "", 3)["block"] is True, "empty challenge -> no proof valid -> fail-closed")
    # attest_consistency() -- the DECLARED LEDGER is the mechanism; the prose
    # tripwire is the demoted second channel. GV() omits the ledger (the
    # tolerated pre-rollout shape); GL() declares one.
    def GV(reason: Any, refuted: bool = False) -> dict:
        return {"ok": True, "v": {"refuted": refuted, "reason": reason, "confidence": "high"}}

    def GL(reason: Any, refuted: bool = False, defects: Any = ()) -> dict:
        return {"ok": True, "v": {"refuted": refuted, "reason": reason,
                                  "confidence": "high", "defects": list(defects)}}
    M3 = ["m-a", "m-b", "m-c"]
    # -- declared_defects(): total on every input, nothing inferred
    expect(declared_defects({"defects": []}) == ("ok", []), "an empty ledger reads as ok/[]")
    expect(declared_defects({"defects": ["a.py:1 — x — y"]})[1] == ["a.py:1 — x — y"],
           "a declared defect is returned verbatim")
    expect(declared_defects({})[0] == "missing", "an absent ledger is missing")
    expect(declared_defects(None)[0] == "missing", "a non-dict vote has no ledger")
    expect(declared_defects({"defects": "none"}) == ("ok", []),
           "a bare-string sentinel ledger reads as empty -- it cannot name a defect")
    expect(declared_defects({"defects": "auth bypass"})[0] == "malformed",
           "any OTHER bare string is an unanswered schema, fail-closed")
    expect(declared_defects({"defects": [], "findings": ["auth bypass"]})[1] == ["auth bypass"],
           "every accepted key is read -- one must not hide behind another")
    expect(declared_defects({"defects": None})[0] == "malformed", "a null ledger is malformed")
    expect(declared_defects({"defects": [{"f": 1}]})[0] == "malformed",
           "non-string entries are malformed")
    expect(declared_defects({"defects": ["none", "", " N/A "]}) == ("ok", []),
           "enumerated empty-sentinels are an empty ledger, not defects")
    expect(declared_defects({"Defects": ["x"]}) == ("ok", ["x"]),
           "a capitalisation slip must not silently disarm the ledger")
    expect(declared_defects({"findings": ["x"]}) == ("ok", ["x"]),
           "an accepted alias key is still an answered schema")
    # -- rung 3: the ledger itself. No prose is read on this rung.
    _d = attest_consistency([GL("looks fine to me",
                                defects=["api.py:7 — authz skipped — any user reads /admin"]),
                             GL("docs only"), GL("fine")], M3)
    expect(_d["block"] is True and "ledger declares" in _d["reason"],
           "refuted=false with a NON-EMPTY declared ledger is the inconsistency -> block")
    expect(attest_consistency([GL("all good, nothing of note", defects=["x.py:1 — y — z"]),
                               GL("docs only"), GL("fine")], M3)["block"] is True,
           "the ledger blocks even when the prose is spotless -- no prose parsing involved")
    expect(attest_consistency([GL("fail-open on missing header", True, defects=["h.py:3 — fo"]),
                               GL("docs only"), GL("no issue found")], M3)["block"] is False,
           "a REFUTING vote may declare defects -- that is its job")
    expect(attest_consistency([GL("docs only change", defects=["none"]), GL("docs only"),
                               GL("fine")], M3)["block"] is False,
           "a sentinel-only ledger is an empty ledger, not a declared defect")
    # -- rungs 1 and 2: schema posture
    expect(attest_consistency([{"ok": True, "v": {"refuted": False, "reason": "docs only",
                                                  "defects": "auth bypass on /admin"}},
                               GL("docs only"), GL("fine")], M3)["block"] is True,
           "a malformed (non-array) ledger fails closed, with or without the env switch")
    expect(attest_consistency([GV("docs only change"), GV("docs only"), GV("fine")],
                              M3)["block"] is False,
           "an ABSENT ledger is tolerated by default -- a pre-rollout voice must not block")
    expect(attest_consistency([GV("docs only change"), GV("docs only"), GV("fine")],
                              M3, require_ledger=True)["block"] is True,
           "VERIFIER_REQUIRE_DEFECT_LIST=true makes an absent ledger an unverifiable green")
    expect(attest_consistency([E, GL("docs only"), GL("no issue")], M3,
                              require_ledger=True)["block"] is False,
           "an errored vote has no ledger to read")
    expect(attest_consistency([GV("checked auth paths, no issue"), GV("docs only change"),
                               GV("reviewed diff, behaviour unchanged")], M3)["block"] is False,
           "ordinary approving reasons must pass")
    expect(attest_consistency([GV("checked auth paths"), GV("auth bypass when key is None"),
                               GV("docs only")], M3)["block"] is True,
           "green vote naming a bypass must fail-closed")
    expect(attest_consistency([GV("fail-open on missing header", True), GV("docs only"),
                               GV("no issue found")], M3)["block"] is False,
           "a REFUTING vote may name a defect -- that is its job")
    expect(attest_consistency([GV("could be more defensive; consider hardening"), GV("ok looks fine"),
                               GV("no problems seen")], M3)["block"] is False,
           "hedging words are not defect claims")
    expect(attest_consistency([E, GV("docs only"), GV("no issue")], M3)["block"] is False,
           "an errored vote is not inspected for consistency")
    expect(attest_consistency([GV("security gates reviewed; no concrete regression"),
                               GV("docs only"), GV("looks fine")], M3)["block"] is True,
           "the match is LITERAL: a negated defect word still blocks. The relief for "
           "this is in build_system_prompt, not here -- reading negation was tried "
           "twice and withdrawn twice, the second time measured WEAKER THAN MAIN on "
           "71 of 165 defect-affirming reasons")
    expect(attest_consistency([GV("no bypass here, but there is an injection in the parser"),
                               GV("docs only"), GV("fine")], M3)["block"] is True,
           "one negation must not launder a real claim later in the same reason")
    # Group A's refutation of the ledger: "required" must mean required.
    _GM = ["combo/SOTA-A", "combo/SOTA-B", "combo/SOTA-C"]

    def _gv(**kw):
        return {"ok": True, "v": {"refuted": False, "reason": "docs only change", **kw}}
    expect(has_conformant_ledger({"defects": []}) and has_conformant_ledger({"defects": ["x"]}),
           "a defects ARRAY is conformant")
    expect(not has_conformant_ledger({"defects": "none"}),
           "a bare sentinel string is not defects: string[]")
    expect(not has_conformant_ledger({"findings": []}),
           "an alias key alone does not answer the schema")
    expect(not attest_consistency([_gv(defects=[])] * 3, _GM, True)["block"],
           "conformant empty ledgers pass in required mode")
    for _bad in ({"defects": "none"}, {"findings": []}, {"defects": "n/a"}):
        expect(attest_consistency([_gv(**_bad), _gv(defects=[]), _gv(defects=[])], _GM, True)["block"],
               f"required mode must refuse a non-conformant ledger: {_bad}")
    expect(not attest_consistency([_gv(defects="none"), _gv(), _gv()], _GM, False)["block"],
           "with the switch OFF the tolerant reading is unchanged -- no forced rollout")
    # Ordinary inflections of the SAME published terms are matched too. Widening
    # a term is monotone (PASS -> BLOCK only), which is the direction this file
    # resolves P1/P2 conflicts in.
    expect(attest_consistency([GV("deadlocks on shutdown remain"), GV("docs only"),
                               GV("fine")], M3)["block"] is True,
           "a plural defect word is still a claim")
    expect(attest_consistency([GV("no bypasses, no regressions"), GV("docs only"),
                               GV("fine")], M3)["block"] is True,
           "and so is a negated one -- literal, by design")
    # THE SHARED ATTACK CORPUS, scored through the REAL gate in the two modes
    # that matter. Mode B: the voice fills the ledger honestly -> 7/7 blocked,
    # and the boolean never has to be read out of prose at all.
    _CORPUS_BLOCK = [
        "auth bypass when key is None and a race condition on refresh",
        "without fixing auth bypass",
        "merged without addressing the injection",
        "not fixed: privilege escalation in admin",
        "no bypass here, but there is an injection in the parser",
        "looks good; minor: unauthenticated /readyz leaks the key",
        "approved, though the deadlock on shutdown remains",
    ]
    # P2 rows carry NO defect term, not even negated -- which is exactly what
    # build_system_prompt now asks a green vote for. The three phrasings that
    # used to sit here ("no concrete regression", "no regression, no bypass, no
    # injection", "checked auth paths, no issue") block under the literal match
    # and are listed below as the accepted, measured cost.
    _CORPUS_PASS = [
        "docs only change",
        "reviewed diff, behaviour unchanged",
        "could be more defensive; consider hardening",
        "gates re-checked, all five fire",
        "verified the retry path is unchanged",
        "checked auth paths, no issue",
    ]
    _CORPUS_P2_COST = [
        "security gates reviewed; no concrete regression",
        "no regression, no bypass, no injection",
    ]
    for _r in _CORPUS_BLOCK:
        expect(attest_consistency([GL(_r, defects=[_r]), GL("docs only"), GL("fine")],
                                  M3)["block"] is True,
               f"P1, declared ledger: a green that declares its finding must block: {_r!r}")
    for _r in _CORPUS_PASS:
        expect(attest_consistency([GL(_r), GL("docs only"), GL("fine")], M3)["block"] is False,
               f"P2: an ordinary approval must not block: {_r!r}")
    for _r in _CORPUS_P2_COST:
        expect(attest_consistency([GL(_r), GL("docs only"), GL("fine")], M3)["block"] is True,
               f"P2 COST, accepted and measured: a negated defect term still blocks: {_r!r}")
    # Mode C: the voice HIDES the finding (declares [] and writes the defect in
    # prose). Rung 4 is main's literal scan, so ALL SEVEN corpus attacks block --
    # including the three that a negation window used to let through. Those three
    # were never pre-existing holes: main blocked them, this branch briefly did
    # not, and the tests here asserted the bypass. That is the regression
    # combo/SOTA-A refuted.
    for _r in _CORPUS_BLOCK:
        expect(attest_consistency([GL(_r), GL("docs only"), GL("fine")], M3)["block"] is True,
               f"P1, hidden finding: the literal scan still catches: {_r!r}")

    # auth_header() -- both branches
    _saved = os.environ.pop("VERIFIER_AUTH_HEADER", None)
    _saved_key = KEY
    KEY = "selftest-verifier-key"
    try:
        expect("Authorization" in auth_header(), "default must be Authorization: Bearer")
        os.environ["VERIFIER_AUTH_HEADER"] = "X-OpenCodex-API-Key"
        expect(list(auth_header()) == ["X-OpenCodex-API-Key"], "custom header must replace Authorization")
        os.environ["VERIFIER_AUTH_HEADER"] = ""
        expect("Authorization" in auth_header(), "empty variable (unset Actions var) -> default")
        os.environ["VERIFIER_AUTH_HEADER"] = "authorization"
        expect("Authorization" in auth_header(), "case-insensitive 'authorization' -> default Bearer form")
        os.environ["VERIFIER_AUTH_HEADER"] = "Host"
        try:
            auth_header()
            expect(False, "a routing header must not carry the verifier credential")
        except ProviderConfigError:
            pass
    finally:
        KEY = _saved_key
        os.environ.pop("VERIFIER_AUTH_HEADER", None)
        if _saved is not None:
            os.environ["VERIFIER_AUTH_HEADER"] = _saved

    # review_range() -- a non-hex candidate sha must never reach git
    _sv = os.environ.get("VERIFIER_HEAD_SHA")
    try:
        os.environ["VERIFIER_HEAD_SHA"] = "not-a-sha; rm -rf /"
        try:
            review_range()
            expect(False, "non-hex VERIFIER_HEAD_SHA must raise DiffError")
        except DiffError:
            pass
    finally:
        os.environ.pop("VERIFIER_HEAD_SHA", None)
        if _sv is not None:
            os.environ["VERIFIER_HEAD_SHA"] = _sv

    # normalize_base() -- a trailing slash builds "//models", answered 404
    expect(normalize_base("https://h/v1/") == "https://h/v1", "trailing slash must be stripped")
    expect(normalize_base("https://h/v1///") == "https://h/v1", "repeated slashes must be stripped")
    expect(normalize_base("  https://h/v1  ") == "https://h/v1", "surrounding whitespace must be stripped")
    expect(normalize_base("") == "", "empty stays empty so credentialed use refuses")
    expect(normalize_base(None) == "", "None stays empty so credentialed use refuses")
    _saved_base = BASE
    try:
        BASE = "https://verifier.example.test/v1"
        expect(_api_url("/models") == f"{BASE}/models",
               "an explicit HTTPS /v1 endpoint must build a fixed API URL")
        BASE = ""
        try:
            _api_url("/models")
            expect(False, "a blank verifier endpoint must fail closed")
        except ProviderConfigError:
            pass
        BASE = "https://127.0.0.0x1/v1"
        try:
            _api_url("/models")
            expect(False, "a resolver-normalized numeric host must fail closed")
        except ProviderConfigError:
            pass
    finally:
        BASE = _saved_base

    # Voice distinctness. Each configured voice is an inference-server GROUP that
    # rotates over its own members; the server answers with the group id, never
    # the member, so this script cannot see a vendor at all. What it CAN check is
    # that two voices are not the same group.
    expect(require_distinct_voices(["combo/SOTA-A", "combo/SOTA-B", "combo/SOTA-C"])["block"] is False,
           "three distinct groups are three voices")
    _dup = require_distinct_voices(["combo/SOTA-A", "combo/SOTA-A", "combo/SOTA-C"])
    expect(_dup["block"] and "more than once" in _dup["reason"],
           "one opinion counted twice is not corroboration")
    expect(require_distinct_voices(["combo/SOTA-A", "", "combo/SOTA-C"])["block"] is False,
           "an empty slot is not a duplicate; resolution failure is caught before this gate")
    _G = ["combo/SOTA-A", "combo/SOTA-B", "combo/SOTA-C"]
    expect(approving_models([A, {"ok": False, "reason": "504"}, RF], _G) == ["combo/SOTA-A"],
           "an errored or refuting voice never counts as approving")
    # The composition rule: A must agree, plus either B or C.
    expect(not require_approvals([A, A2, {"ok": False, "reason": "504"}], _G, "combo/SOTA-A", 1)["block"],
           "group A plus one other is the quorum")
    expect(require_approvals([A, {"ok": False, "reason": "504"}, {"ok": False, "reason": "504"}],
                             _G, "combo/SOTA-A", 1)["block"],
           "group A alone is not corroboration")
    expect(require_approvals([{"ok": False, "reason": "504"}, A, A2], _G, "combo/SOTA-A", 1)["block"],
           "B and C without A cannot carry the panel")
    # NOTE: resolve_panel_models fails closed on an unresolvable voice, but it
    # needs the model catalogue, so its coverage is in tests/test_independent_verify.py
    # (TestResolutionFailsClosed) rather than here.

    print("   OK selftest: decide() + model_matches() + require_approvals() (required approver Sol "
          "+ corroboration) + attest_reasons() + attest_proof() + declared_defects() + "
          "attest_consistency() (ledger 7/7; literal prose scan 7/7 on the shared corpus) + "
          "auth_header() + review_range() + normalize_base() + _api_url() correct.")


# --------------------------------------------------------------- API plumbing --


def _http_json(path: str, payload: dict | None = None, timeout: int = 180) -> tuple[int, Any]:
    url = _api_url(path)
    req = urllib.request.Request(  # noqa: S310 -- operator-configured https endpoint (VERIFIER_BASE_URL)
        url, headers={"Content-Type": "application/json", **auth_header()},
        data=json.dumps(payload).encode() if payload is not None else None,
        method="POST" if payload is not None else "GET")
    try:
        with _urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, _http_error_detail(exc)
    except Exception as exc:
        return 0, _safe_diag(exc)


def pick_for_pref(ids: list[str], p: str) -> str | None:
    """Exact ID first, else ONLY a dated snapshot p-YYYY-MM-DD (never a -mini/
    -nano variant); among snapshots the NEWEST."""
    if p in ids:
        return p
    dated = sorted(i for i in ids if re.match(rf"^{re.escape(p)}-\d{{4}}-\d{{2}}-\d{{2}}$", i))
    return dated[-1] if dated else None


def fetch_model_ids() -> tuple[list[str] | None, str]:
    """Return model ids or a status-bearing, peer-body-free diagnostic."""
    status, data = _http_json("/models")
    if status == 200 and isinstance(data, dict):
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        if any(isinstance(model_id, str) and _contains_protected_text(model_id)
               for model_id in ids):
            return None, "model catalogue echoed protected verifier configuration"
        return ids, ""
    return None, (f"GET configured /models -> {status or 'no response'}: "
                  f"{_safe_diag(data, limit=200)}")


def resolve_panel_models(panel_size: int, wanted: list[str]) -> list[str]:
    if os.environ.get("VERIFIER_MODEL"):
        return [os.environ["VERIFIER_MODEL"]] * panel_size
    ids, why = fetch_model_ids()
    if not ids:
        # An unreachable catalogue used to degrade silently to a pinned fallback
        # for every voice; each vote then errored on a model the gateway does
        # not serve, and the operator saw three identical failures with no hint
        # that endpoint configuration was the cause. Common causes include a
        # missing version segment or the wrong auth-header setting.
        # Verified live: ".../v1/models" 200, ".../models" 401, ".../v1//models" 404.
        raise ProviderConfigError(
            f"model catalogue unreachable, so the panel cannot be resolved -- {why}\n"
            f"  The configured VERIFIER_BASE_URL must include the API version "
            f"segment (for example an HTTPS /v1 endpoint), and the auth header must be the "
            f"one this gateway expects (VERIFIER_AUTH_HEADER).")
    # SUBSTITUTION IS FAIL-OPEN, so it is gone. A configured voice that the
    # account does not serve used to print a WARNING and be replaced by the
    # newest available model. In a merge gate that is the worst possible
    # degradation: the panel reviews with voices the operator did not choose,
    # every gate downstream passes, and the run goes green. Two ways it fires
    # in practice, both cheap: a group not yet published to /v1/models, and a
    # one-character slip in an id -- `combo/SOTA-B` and `combo/SOAT-B` differ by
    # a transposition, and only combo/SOTA-B exists today. The registry itself
    # shipped the SOAT-B typo first and later renamed it, so the live spelling
    # has already flipped once -- a substitute picked by this script would have
    # guessed wrong on both sides of that rename.
    #
    # A voice that cannot be resolved is a panel that cannot be assembled.
    missing = [w for w in wanted if not pick_for_pref(ids, w)]
    if missing:
        raise ProviderConfigError(
            "panel model(s) not enabled on this account: " + ", ".join(repr(m) for m in missing)
            + "\n  Refusing to substitute: reviewing with a voice the operator did not choose "
            "is a green run that reviewed the wrong thing.\n  Enabled ids are: "
            + ", ".join(sorted(ids)[:40])
            + "\n  Set VERIFIER_PANEL_MODELS to ids from that list.")
    return [pick_for_pref(ids, w) for w in wanted]


class DuplicateKey(ValueError):
    """A verdict object carrying the same key twice."""


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    """json.loads keeps the LAST of duplicate keys, silently.

    That is a bypass of the whole ledger, measured before this hook existed:

        {"refuted": false, ..., "defects": ["admin.py:83 - auth bypass"], "defects": []}

    parses to defects == [] and the gate PASSES a vote that declared a defect in
    the same object it erased it from. The two declarations this gate compares
    are only comparable if the object says one thing per key, so a duplicate is
    not resolved -- it is refused."""
    seen: set[str] = set()
    for k, _ in pairs:
        if k in seen:
            raise DuplicateKey(f"duplicate key {k!r} in the verdict object")
        seen.add(k)
    return dict(pairs)


def parse_verdict(content: str) -> Any:
    if not content:
        return None
    decoder = json.JSONDecoder(object_pairs_hook=_no_duplicate_keys)
    try:
        return decoder.decode(content)
    except DuplicateKey:
        # Ambiguous by construction. Returning None makes _is_valid() false, and
        # a vote that cannot be parsed is discarded rather than counted -- the
        # same fail-closed route an unparsable reply already takes.
        return None
    except ValueError:
        m = re.search(r"\{[\s\S]*\}", content)
        if m:
            try:
                return decoder.decode(m.group(0))
            except (ValueError, DuplicateKey):
                return None
    return None


def _extract_responses_text(data: Any) -> str:
    """Text out of a /responses response OBJECT — the FALLBACK source only.

    The /responses wire is streamed, and the authoritative text is the
    accumulated `response.output_text.delta` strings (_sse_fold). This
    extractor reads the response object captured from `response.completed`,
    which has been observed live on this gateway to arrive with an EMPTY
    "output" array even when text WAS streamed — hence fallback, consulted
    only when the delta accumulation is empty and the object actually
    carries output."""
    if isinstance(data, dict) and isinstance(data.get("output_text"), str) and data["output_text"]:
        return data["output_text"]
    parts = []
    for item in (data or {}).get("output", []) if isinstance(data, dict) else []:
        for c in item.get("content", []) if isinstance(item, dict) else []:
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
    return "".join(parts)


# Transient = a retry can help: network error (no status), rate-limit/timeout/
# conflict and 5xx. 401 counts as transient too (observed flaky LB behavior in
# the reference). Deterministic client errors (400/403/404) are never retried
# on the SAME wire — a 404/405 may still earn the one-shot chat-wire attempt
# (see should_fallback_chat), which is a different wire, not a repeat.
def is_transient(status: int) -> bool:
    return status in (0, 401, 408, 409, 429) or status >= 500


def wire_may_differ(status: int) -> bool:
    """A wire-level failure the OTHER wire might not reproduce.

    WHY /responses IS THE PRIMARY WIRE — measured, not guessed. Operator
    telemetry across 13,815 logged gateway requests: of 516 chat-protocol
    requests NOT ONE ever exceeded 91 seconds, with 150 clustered at ~90s,
    while `responses` runs to 380s freely. The ~90s cluster spans unrelated
    upstreams -- Anthropic via combo/SOTA-B (logged under its since-retired
    SOAT-B spelling) and NVIDIA via combo/SOTA-C -- which do not share a
    server-side behaviour, so the common factor is the wire. 78 of 82
    combo/SOTA-B failures produced no first output token at all, while its
    successes have a p50 first-token time of 69s and a max of 84.9s: the
    successes are simply the requests whose thinking phase finished before the
    ~90s axe fell.

    Confirmed live: driving combo/SOTA-B over chat-completions with curl and NO
    client timeout ran 458 seconds to a clean [DONE], 32,000 output tokens,
    HTTP 200. The gateway imposes no wall of its own. The ceiling is the chat
    wire's SSE translation not emitting a keepalive during the thinking phase,
    and an edge idle timeout collecting the silence — while the /responses
    wire, driven with "stream": true, trickles SSE bytes at a ~2s heartbeat
    cadence through the same edge and never goes dark (380s and 458s runs on
    record). Chat-first also poisoned its own rescue: the ~90s chat death
    marked the upstream failed at the edge, so the immediate /responses
    failover answered an instant 504. That is why every attempt now STARTS on
    streaming /responses and chat/completions is the failover, not the other
    way around.

    So a 5xx or a network-level failure on /responses is worth ONE attempt on
    the chat wire before the voice is lost. Deliberately NOT 429 and NOT 401: a
    rate limit or an exhausted account pool is upstream state both wires share,
    and retrying the same condition on a second wire only burns the budget
    twice."""
    return status == 0 or status >= 500


def should_fallback_chat(status: int) -> bool:
    """A /responses failure worth the ONE chat-completions attempt: everything
    wire_may_differ() names, plus 404/405 for a gateway that does not route
    /responses at all (absent route -> 404, path present but method not wired
    -> 405). Those two are DETERMINISTIC on the /responses wire but say nothing
    about the chat wire, which such a gateway usually does serve."""
    return wire_may_differ(status) or status in (404, 405)


# should_fallback_responses() used to live here: with chat/completions PRIMARY,
# a 400/404 whose body named the Responses API meant "this model is served on
# /responses only" and earned the one-shot wire switch (round-3 panel finding:
# a 400-only model would have blocked the panel permanently). With /responses
# now the primary wire, that whole class is answered on the FIRST attempt, so
# the body-sniffing predicate is deleted rather than kept as a reader-facing
# description of a mechanism that no longer runs -- the same reasoning that
# removed MODEL_PREFERENCE/FALLBACK_MODEL above.


def _sse_fold(resp: Any) -> tuple[str, Any] | str:
    """Fold a /responses SSE stream into ``(text, completed_response_object)``.

    ``resp`` is the file-like urllib response: iterating it reads line by line
    off the socket, so the urlopen timeout applies PER READ — trivially met by
    the gateway's ~2s heartbeat cadence (max inter-event gap measured live:
    9.3s, on a 38.9s/1121-event combo/SOTA-C run). Events arrive as
    blank-line-delimited blocks of ``event:`` and ``data:`` lines; all
    ``data:`` payload lines of one block are accumulated and JSON-parsed
    together (multi-line data is legal SSE), and every payload carries a
    ``type`` field.

    Folding rules, in order of authority:
      * ``response.output_text.delta`` — the PRIMARY text source. Observed
        live on this gateway: the terminal completed object can arrive with an
        EMPTY "output" array even when text WAS streamed, so the deltas are
        accumulated verbatim and the completed object serves only as the
        fallback extractor's input (_extract_responses_text) and the usage/id
        record.
      * ``response.completed`` — the terminal event; its ``response`` object
        is captured and returned alongside the folded text.

    A stream that ends WITHOUT ``response.completed``, or that carries an
    error-typed event, returns a transient-failure MARKER (a str). The caller
    reports it as status 0 — the same class as a network failure — so the
    retry loop treats a torn stream exactly like a torn connection."""
    text_parts: list[str] = []
    data_lines: list[str] = []
    completed: Any = None
    done = False
    error = ""
    events = 0

    def _flush() -> None:
        nonlocal completed, done, error, events
        if not data_lines:
            return
        raw = "\n".join(data_lines)
        data_lines.clear()
        try:
            payload = json.loads(raw)
        except ValueError:
            return                       # not JSON (comment/keepalive token)
        if not isinstance(payload, dict):
            return
        events += 1
        typ = payload.get("type")
        if typ == "response.output_text.delta" and isinstance(payload.get("delta"), str):
            text_parts.append(payload["delta"])
        elif typ == "response.completed":
            completed, done = payload.get("response"), True
        elif typ == "error" or (isinstance(typ, str) and typ.endswith(".failed")):
            error = "stream error event (peer detail withheld)"

    for raw_line in resp:
        line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
        if not line:                     # a blank line closes the event block
            _flush()
            if done or error:
                break
        elif line.startswith("data:"):
            data_lines.append(line[5:].removeprefix(" "))
        # "event:" and ":"-comment lines carry no payload of their own
    _flush()                             # a final block may end at EOF instead
    if error:
        return error
    if not done:
        return f"stream ended without response.completed ({events} events folded)"
    return "".join(text_parts), completed


def _responses_attempt(model: str, sys_prompt: str, user_prompt: str) -> tuple[int, Any]:
    """POST {BASE}/responses with ``"stream": true`` and fold the SSE reply.

    Both request-shape details are PROVIDER-ENFORCED, verified live through
    the real edge on all three panel voices:
      * "input" must be a MESSAGE LIST. A plain string draws
        400 {"detail": "Input must be a list"} — the exact bug behind the ~29
        instant 400s combo/SOTA-A left in today's proxy log while this body
        was string-shaped.
      * "stream" must be TRUE. combo/SOTA-C's provider rejects the
        non-streaming form outright (400 {"detail": "Stream must be set to
        true"}), and a deterministic 400 never reaches the chat failover, so
        a non-streaming primary would lose that voice permanently.

    Streaming is also the mechanism that defeats the edge's 90s idle timer:
    the gateway emits SSE bytes at a ~2s heartbeat cadence while the model
    thinks, and continuous bytes keep proxy_read_timeout from ever firing
    (operator telemetry records 380s and 458s /responses runs through this
    same edge). That only works if this side reads progressively too, so the
    200 path hands the socket to _sse_fold line by line — never one blocking
    full-body read whose silence would be the client's own fault.

    Returns (200, (text, completed)) for a folded stream; (0, marker) for a
    torn or error-carrying stream — transient, the same class as a network
    failure; and (status, static diagnostic) for non-200 HTTP. Peer-controlled
    bodies are never logged; at most 4096 bytes are inspected for the one
    allowlisted account-pool classification, so retry/failover stays intact."""
    req = urllib.request.Request(  # noqa: S310 -- operator-configured https endpoint (VERIFIER_BASE_URL)
        _api_url("/responses"),
        headers={"Content-Type": "application/json", **auth_header()},
        data=json.dumps({
            "model": model,
            "instructions": sys_prompt,
            "input": [{"role": "user", "content": user_prompt}],
            "stream": True,
        }).encode(),
        method="POST")
    try:
        with _urlopen(req, timeout=180) as resp:
            folded = _sse_fold(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, _http_error_detail(exc)
    except Exception as exc:             # DNS/TLS/reset, or a per-read timeout
        return 0, _safe_diag(exc)
    if isinstance(folded, str):          # transient marker from the fold
        return 0, folded
    return 200, folded


def _chat_attempt(model: str, sys_prompt: str, user_prompt: str) -> tuple[int, Any]:
    return _http_json("/chat/completions", {
        "model": model,
        "messages": [{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": user_prompt}],
    })


def attempt_once(model: str, sys_prompt: str, user_prompt: str) -> dict:
    status, data = _responses_attempt(model, sys_prompt, user_prompt)
    if status == 200:
        text, completed = data
        # Deltas first: the completed object is consulted only when the delta
        # accumulation is empty AND it actually carries output — it has been
        # observed to arrive empty even after text was streamed.
        candidate = text or _extract_responses_text(completed)
        if _contains_protected_text(candidate):
            return {"ok": False, "status": 400,
                    "reason": "API response echoed protected verifier configuration"}
        v = parse_verdict(candidate)
        if _contains_protected_value(v):
            return {"ok": False, "status": 400,
                    "reason": "API response echoed protected verifier configuration"}
        return {"ok": True, "v": v, "decision": decide(v)}
    # The wire itself may be the fault (see wire_may_differ), or the gateway
    # may not route /responses at all (404/405). One attempt on the chat wire,
    # and the ORIGINAL /responses failure is what gets reported if that also
    # fails, because that is the one an operator needs to see.
    if should_fallback_chat(status):
        print(f"  [wire] {model}: /responses {status or 'network error'} "
              f"-> retrying once on chat/completions")
        s2, d2 = _chat_attempt(model, sys_prompt, user_prompt)
        if s2 == 200 and isinstance(d2, dict):
            content = (d2.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if not isinstance(content, str):
                return {"ok": False, "status": 400,
                        "reason": "API response carried invalid verifier content"}
            if _contains_protected_text(content):
                return {"ok": False, "status": 400,
                        "reason": "API response echoed protected verifier configuration"}
            v = parse_verdict(content or "")
            if _contains_protected_value(v):
                return {"ok": False, "status": 400,
                        "reason": "API response echoed protected verifier configuration"}
            return {"ok": True, "v": v, "decision": decide(v), "wire": "chat"}
    return {"ok": False, "status": status,
            "reason": f"API {status}: {_safe_diag(data)}"}


# A 401 whose body names an exhausted upstream account pool is a DETERMINISTIC
# gateway state, not a flaky auth hiccup: retrying burns the backoff budget and
# still fails. Observed live on the configured gateway while its upstream account pool
# was empty ("OpenAI account pool has no usable account credential").
_POOL_EXHAUSTED = re.compile(r"no usable account credential", re.I)


def verify_once(model: str, sys_prompt: str, user_prompt: str) -> dict:
    last: dict = {"ok": False, "reason": "no attempt executed"}
    for a in range(1, 4):
        last = attempt_once(model, sys_prompt, user_prompt)
        if last.get("ok"):
            return last
        if last.get("status") and not is_transient(last["status"]):
            return last            # deterministic error -> no retry
        if last.get("status") == 401 and _POOL_EXHAUSTED.search(str(last.get("reason", ""))):
            return last            # deterministic gateway state -> no retry
        if a < 3:
            # 4s, then 16s. The old 0.5s/1.0s was far too short for the real
            # failures seen against this gateway: 429 rate limits and a 504
            # gateway timeout on a model that had answered correctly minutes
            # before. A voice lost to an under-short backoff silently shrinks
            # the panel and can trip the fail-closed quorum gates.
            time.sleep(min(30.0, float(4 ** a)))
    return last


# --------------------------------------------------------------------- main --


def _md_cell(value: object, limit: int = 300) -> str:
    """Model-authored text, made safe for a markdown table cell.

    The reason comes from a language model reading an untrusted diff, so it is
    treated as hostile input to the RENDERER: pipes would break out of the
    cell, newlines out of the row, and backticks out of code spans. Collapsing
    whitespace and escaping the three metacharacters is enough — the value is
    never interpreted as anything but table text."""
    text = " ".join(str("" if value is None else value).split())
    text = text.replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`")
    return (text[: limit - 1] + "…") if len(text) > limit else (text or "—")


def write_step_summary(votes: list[dict], models: list[str], gates: list[tuple[str, dict]],
                       blocked: bool) -> None:
    """Publish the panel's findings where a reviewer will actually see them.

    GITHUB_STEP_SUMMARY renders as markdown on the run page, one click from the
    pull request's check. Chosen over posting a pull-request review because it
    needs no token, no extra permission and no API call that could itself fail
    and turn a clean verdict into a red job.

    Written on BOTH paths. A panel that only explains itself when it blocks
    leaves the reader unable to tell 'three voices examined this and agreed'
    from 'the panel never ran'."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## Independent-Verify — cross-vendor panel",
        "",
        f"**Verdict: {'BLOCKED' if blocked else 'APPROVED'}**",
        "",
        "| # | Model | Verdict | Confidence | Declared defects | Finding |",
        "|---|---|---|---|---|---|",
    ]
    for i, (vote, model) in enumerate(zip(votes, models, strict=False), start=1):
        if not vote.get("ok"):
            lines.append(f"| {i} | `{_md_cell(model, 60)}` | ⚠️ no vote | — | — "
                         f"| {_md_cell(vote.get('reason'))} |")
            continue
        v = vote.get("v") or {}
        refuted = v.get("refuted")
        # `is True` / `is False`, never truthiness: decide() fails closed on a
        # non-bool at :142, so {"refuted": "maybe"} is discarded -- but this table
        # used to label it "refutes", telling the reader a model objected when in
        # fact its vote was unreadable.
        mark = ("🔴 refutes" if refuted is True
                else "🟢 approves" if refuted is False else "⚠️ unparsable")
        # The ledger is the field the consistency gate now reads, so it is shown
        # next to the verdict: a reader must see BOTH declarations the gate
        # compares, not only the prose.
        state, declared = declared_defects(v)
        ledger = ("⚠️ " + state if state != "ok"
                  else ("none" if not declared else f"{len(declared)}: " + "; ".join(declared)))
        lines += [f"| {i} | `{_md_cell(model, 60)}` | {mark} "
                  f"| {_md_cell(v.get('confidence'), 12)} | {_md_cell(ledger, 200)} "
                  f"| {_md_cell(v.get('reason'))} |"]
    lines += ["", "### Gates", ""]
    for name, result in gates:
        state = "⛔ blocked" if result.get("block") else "✅ passed"
        lines.append(f"- **{name}** — {state}: {_md_cell(result.get('reason'), 400)}")
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        # Never let the REPORT break the VERDICT.
        print(f"[independent-verify] could not write the step summary: {exc}")


def main() -> int:
    if "--selftest" in sys.argv:
        selftest()
        return 0

    if not KEY:
        # Fork-PR bypass (found by the panel itself, Sol veto round 2): GitHub
        # withholds secrets from fork-originated pull_request runs, so "no key"
        # on a fork PR is NOT the operator's documented residual state — it is
        # an untrusted origin that must FAIL CLOSED, or a required cross-vendor
        # check would pass with zero review. The workflow sets
        # VERIFIER_REQUIRE_KEY=true for a fork-origin or Dependabot run. The
        # trusted workflow skips forks before this script and retains this
        # check as defence in depth; Dependabot is same-repo but still has
        # Actions secrets withheld.
        if (os.environ.get("VERIFIER_REQUIRE_KEY") or "").lower() == "true":
            print("BLOCK untrusted no-key origin (fork or Dependabot): the verifier "
                  "credential is unavailable, so the panel cannot review — fail-closed.",
                  file=sys.stderr)
            return 1
        print("[independent-verify] RESIDUAL: no second-vendor key (SECOND_VENDOR_API_KEY or "
              "OPENAI_API_KEY) provisioned.\n"
              "  The independent cross-vendor review panel is NOT active. Compensation: the\n"
              "  deterministic CI gate remains the sole merge authority. To activate: set the\n"
              "  secret (see docs/INDEPENDENT_REVIEW_PANEL.md).")
        return 0   # same-repo only: no fake block; the residual is documented and visible

    try:
        _api_url("/models")
        auth_header()
    except ProviderConfigError as exc:
        print(f"BLOCK provider configuration — {exc}; fail-closed.", file=sys.stderr)
        return 1

    try:
        d = build_diff()
    except DiffError as exc:
        print(f"BLOCK diff assembly failed — cannot review, fail-closed: {exc}", file=sys.stderr)
        return 1
    if not d.strip():
        # Defence in depth behind review_range()'s ancestor check: in a PR run
        # (candidate sha supplied) an empty diff means the range collapsed, not
        # that the change is harmless. Greening here is the fake-green path.
        if (os.environ.get("VERIFIER_HEAD_SHA") or "").strip():
            print("BLOCK empty diff for an explicit candidate sha — the review range "
                  "collapsed; fail-closed.", file=sys.stderr)
            return 1
        print("[independent-verify] No diff to review. Green.")
        return 0

    env_panel = [s.strip() for s in os.environ.get("VERIFIER_PANEL_MODELS", "").split(",") if s.strip()]
    wanted = env_panel or DEFAULT_PANEL_MODELS      # empty/','-input must not empty the panel
    if os.environ.get("VERIFIER_MODEL"):
        try:
            n = int(os.environ.get("VERIFIER_PANEL", ""))
            panel = min(64, n) if n >= 1 else 3
        except ValueError:
            panel = 3
    else:
        panel = max(1, len(wanted))
    required_approver = (os.environ.get("VERIFIER_REQUIRED_APPROVER") or "gpt-5.6-sol").strip()
    try:
        mo = int(os.environ.get("VERIFIER_MIN_OTHER_APPROVERS", ""))
        min_others = mo if mo >= 1 else 1
    except ValueError:
        min_others = 1

    try:
        models = resolve_panel_models(panel, wanted)
    except ProviderConfigError as exc:
        print(f"BLOCK provider configuration — no panel could be assembled, "
              f"fail-closed: {exc}", file=sys.stderr)
        return 1
    print(f"[independent-verify] Panel ({len(models)} voices, one model each): {', '.join(models)}"
          + (" (VERIFIER_MODEL pinned)" if os.environ.get("VERIFIER_MODEL") else ""))
    print(f'[independent-verify] Required approver: "{required_approver}" must approve + >= {min_others} other(s).')

    challenge = secrets.token_hex(9)   # fresh 18-hex challenge per run
    sys_base = build_system_prompt(challenge)
    user_prompt = "DIFF (overview + code excerpt):\n\n" + d

    votes = [verify_once(models[i], sys_base + LENSES[i % len(LENSES)], user_prompt)
             for i in range(panel)]
    for i, x in enumerate(votes):
        if not x.get("ok"):
            print(f"  Verifier {i + 1}/{panel} ({models[i]}): error ({x.get('reason')})")
            continue
        v = x.get("v") or {}
        # json.dumps escapes all control chars -> single-line, injection-safe log
        reason = json.dumps(v.get("reason"))[:1000] if v.get("reason") else '"(no reason given)"'
        print(f"  Verifier {i + 1}/{panel} ({models[i]}): refuted={v.get('refuted')} "
              f"confidence={v.get('confidence')} — reason: {reason}")

    # Gates in order, short-circuiting on the FIRST block — the order is the
    # semantics and does not change. What is new is that the gates evaluated so
    # far are collected, so the summary can name which one blocked and why.
    # STRICT MODE, configurable but FAIL-CLOSED ON ABSENCE.
    #
    # WHY THIS IS SAFE TO MAKE SETTABLE — the objection answered.
    # The reasonable worry is "a mutable repo variable disables the refutation
    # veto, so a required status can pass despite valid dissent". The second
    # half is true and INTENDED; the first half is not, and the difference is
    # the whole design.
    #
    # Turning strict mode off does not disable review. It changes exactly one
    # thing: whether a dissenting voice OTHER than the required approver is also
    # a veto. Everything below is untouched by it, and none of it is reachable
    # by any repo variable:
    #
    #   * the required approver's veto is ABSOLUTE and UNCONDITIONAL. Refuting
    #     at any confidence — even "low" — blocks. So does approving without a
    #     substantive own reason, or without a valid proof-of-check when a
    #     challenge is set, or not being resolved in the panel at all.
    #   * at least one DISTINCT other model must approve. Repeat votes from the
    #     same model never corroborate.
    #   * VERIFIER_MIN_OTHER_APPROVERS cannot lower that floor. Measured:
    #     0, -5, blank, "abc" and None all degrade to 1 — the approver-alone
    #     case BLOCKS under every one of them.
    #   * the distinct-voices gate and the defect-ledger gate still run.
    #
    # So the floor is TWO distinct approving voices, one of which must be the
    # required approver, and that floor is not configuration — it is code. What
    # the operator selects is whether the bar is "2 of 3 including the required
    # approver" or "unanimous". Unanimity is a defensible policy for some repos
    # and needless for others; which one applies here is an operator judgement,
    # not a safety property. The control cannot be switched off, only tuned
    # between two positions that both require real, independent, reasoned
    # approval.
    #
    # This was hardcoded "true" because "a merge control that a variable can
    # silently switch off is not a control". Operator decision: make it settable
    # again. The hole that reasoning guarded against is that the old predicate
    # treated UNSET as OFF, so deleting the variable — or misspelling it in the
    # workflow, or a repo restore that drops repo vars — retires the gate with no
    # signal anywhere. So absence now means ON: only an EXPLICIT off value turns
    # it off, and whichever way it resolves is named in the step summary, so a
    # relaxation appears in the run log instead of being invisible.
    _strict_raw = (os.environ.get("VERIFIER_STRICT_ANY_REFUTATION") or "").strip()
    strict_on = _strict_raw.lower() not in ("0", "false", "no", "off")
    strict_source = (
        "unset -> ON (fail-closed default)" if not _strict_raw
        else f"VERIFIER_STRICT_ANY_REFUTATION={_strict_raw!r} -> "
             f"{'ON' if strict_on else 'OFF'}")
    print(f"[independent-verify] strict mode: {strict_source}")
    gates_prefix: list[tuple[str, dict]] = []
    pending: list[tuple[str, str, Any]] = [
        ("distinct-voices gate", "distinct voices", lambda: require_distinct_voices(models)),
        ("required-approver gate", "required-approver",
         lambda: require_approvals(votes, models, required_approver, min_others, challenge)),
    ]
    if strict_on:
        pending.append(("strict-mode gate", "strict mode",
                        lambda: strict_any_refutation(votes, models)))
    else:
        # Recorded as an explicitly DISABLED gate rather than omitted: a gate that
        # vanishes from the report is indistinguishable from one that never
        # existed, which is how a silently switched-off control stays silent.
        gates_prefix.append(
            ("strict mode", {"block": False,
                             "reason": f"DISABLED by configuration ({strict_source}) — "
                                       "a single refuting voice no longer blocks"}))
    require_ledger = (os.environ.get("VERIFIER_REQUIRE_DEFECT_LIST") or "").lower() in (
        "1", "true", "yes")
    pending += [
        ("consistency gate", "consistency",
         lambda: attest_consistency(votes, models, require_ledger)),
        ("integrity gate (sham green)", "reason attestation", lambda: attest_reasons(votes, panel)),
        ("proof-of-check gate", "proof of check", lambda: attest_proof(votes, challenge, panel)),
    ]

    gates: list[tuple[str, dict]] = []
    gates.extend(gates_prefix)
    for log_name, summary_name, run in pending:
        result = run()
        gates.append((summary_name, result))
        if result["block"]:
            print(f"BLOCK {log_name}: {result['reason']}", file=sys.stderr)
            write_step_summary(votes, models, gates, blocked=True)
            return 1

    # The label is COMPUTED, never asserted. Printing "Cross-vendor" over a
    # single-vendor result is the defect this whole gate exists to close, and a
    # green run that hides a lost voice looks like a smaller panel that agreed.
    # NAME WHAT APPROVED. The previous form printed "Cross-vendor panel confirms"
    # unconditionally, and run 32121148827 printed it over two sibling models from
    # one vendor while the only other-vendor voice was returning API 504. This
    # script cannot see which vendor served a voice -- each voice is a group id
    # that the server resolves internally -- so it states the voices and leaves
    # the independence claim to whoever composed the groups.
    approving = approving_models(votes, models)
    label = f"Panel confirms on {len(approving)} of {panel} voices ({', '.join(approving)})"
    unreachable = [models[i] for i, x in enumerate(votes) if not x.get("ok")]
    note = (f"\n[independent-verify] NOTE: {len(unreachable)} of {panel} voices were "
            f"unreachable and did not vote: {', '.join(unreachable)}." if unreachable else "")
    print(f"[independent-verify] {label} ("
          + "; ".join(r["reason"] for _, r in gates) + f"). Green.{note}")
    write_step_summary(votes, models, gates, blocked=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
