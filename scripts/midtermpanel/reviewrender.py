"""Turn already-validated verdicts into a body that is safe to publish.

## What this module is for

The panel publishes two commit statuses and a private evidence file. That is
enough for a machine and not enough for a person: a reader looking at the pull
request sees `midterm-panel-review — panel blocked` and has no way to learn WHICH
line three models objected to or WHY. The prior review mechanism was readable;
the governed one was not, and a review nobody can read is a review that gets
overridden rather than acted on.

So this renders a human-readable summary. It renders it from data that has
ALREADY passed every gate — trusted response normalization, the engine's strict
verdict validation, challenge/lens/unit validation, the output-privacy scan, and
the role and refutation gates — because a renderer that reached back to the raw
provider reply would be a second, weaker parser of the one input this lane treats
as hostile.

## What it may never emit

The complete list is enforced rather than merely written here: raw provider
responses, credentials or headers, request or response identifiers, the
execution challenge, `proof_of_check`, raw unchanged context, unsafe Markdown or
HTML, live @mentions, and unbounded text.

Two gates, because two different things go wrong. `assert_no_forbidden_fields`
walks the review OBJECT for keys that must not exist at any depth — the failure
it catches is a future edit that copies a verdict dict wholesale instead of
selecting fields out of it. `assert_publishable` reads the finished BODY for a
live mention, a control character, an exact execution challenge and the total
bound — the failures that are properties of text rather than of structure.

`proof_of_check` deserves its own sentence. It is REQUIRED to open with the
execution challenge — that is what makes it proof — so publishing a proof
publishes the challenge, and a published challenge is one a future model could
echo without having seen the request. It is the single field whose publication
would break the anti-canned-response control, and it is therefore refused by name
rather than merely omitted by not being selected.

## Why every field is sanitized, including the ones that look trustworthy

Two of the strings here are chosen by the candidate rather than by the panel: the
FILE PATH and the git status, both read out of the plan the count job built from
the pull request's own diff. A pull request can add a file called
``@mglaeser-<img src=x onerror=alert(1)>.py``. The reason text is chosen by a
provider. So the rule is uniform and mechanical — every rendered field goes
through `sanitize`, and a field that cannot pass is withheld rather than trimmed
into something that looks fine.

Sanitization order is load-bearing:

    0. redaction of exact run tokens — the execution challenge — by identity,
       before the scan, because the challenge is high-entropy and a scan-first
       order would report every challenge-echoing reason as secret-shaped;
    1. type and control-character and charset refusals, on the RAW text;
    2. the engine's own secret scanner, on the RAW text — scanning after
       truncation would let a secret past the bound by being long;
    3. the bound, on the RAW text;
    4. HTML escaping and mention/autolink neutralisation, LAST, so that an
       escape sequence can never be split by the bound.

## What it may never do

Change a verdict. `assert_rendering_did_not_change_the_decision` is the
assertion, and it exists because this is exactly the place a "summary" grows into
a judgement: a renderer that decided a low-confidence refutation was not worth
showing would have silently converted a block into an approval.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re

from . import (
    ARCHITECTURE_FACTS,
    MIN_DISTINCT_OTHER_APPROVALS,
    PANEL_MODELS,
    REQUIRED_APPROVER,
    STRICT_ANY_REFUTATION,
)
from .errors import refuse

#: Bumped whenever the published shape changes, so a stored review artifact
#: cannot be reinterpreted under different rules.
RENDER_VERSION = "midterm-panel-readable-review-v1"

#: The ONE hidden marker. A run finds its predecessor by this exact string and
#: edits it, so a pull request accumulates one panel comment rather than one per
#: push. It is a module constant and never derived from any input: a marker a
#: candidate could influence is a marker a candidate could make un-findable,
#: and the failure mode of that is comment spam rather than a refusal.
MARKER = "<!-- midterm-panel-review: sticky, machine-written, do not edit -->"

#: The exact-head binding, hidden in the body. `reviewpublish` reads it back off
#: a comment before deciding whether that comment is this run's to edit.
HEAD_BINDING_PREFIX = "<!-- midterm-panel-head: "
HEAD_BINDING_SUFFIX = " -->"

#: Requirement 6, verbatim and as a constant: when explanation text cannot be
#: published, the finding still is, and the sentence in its place is always this
#: one. A per-case message would leak which rule tripped, and "the reason
#: contained something that looks like a credential" is itself information about
#: the credential.
WITHHELD = "Explanation withheld by output-privacy policy."

#: Requirement 8, verbatim.
NO_FINDINGS = "No actionable findings were reported by the governed panel."

#: Bounds. Every one of them is a refusal to publish unbounded text, and each is
#: named rather than inlined so that a reader can see the whole budget at once.
#:
#: `MAX_REASON_CHARS` matches the engine's own `REASON_MAX_CHARS`, so a reason
#: that satisfied review policy is normally published whole; the bound is here
#: for the case where policy moves and this file does not.
MAX_REASON_CHARS = 600
MAX_PATH_CHARS = 200
MAX_CATEGORY_CHARS = 48
MAX_CATEGORIES_RENDERED = 6
MAX_FINDINGS_RENDERED = 50
#: GitHub rejects an issue-comment body over 65536 characters. Set below that
#: with room for the header, because a body the API refuses is a review nobody
#: reads — the failure this whole module exists to remove.
MAX_BODY_CHARS = 60_000

#: What a truncated field is marked with. Inside the bound, not appended past
#: it, so the marker cannot itself push a field over.
TRUNCATION_MARK = "…[truncated]"

#: Keys that must never appear ANYWHERE in the review object, checked
#: structurally rather than by searching the rendered prose. The distinction
#: matters and the first draft got it wrong: a substring search for
#: `"challenge"` refuses a body containing the sentence "this change challenges
#: the invariant", so a model using an ordinary English word would suppress the
#: entire review. A key check refuses the thing that is actually dangerous — a
#: field copied wholesale out of a verdict — and leaves prose alone.
FORBIDDEN_FIELDS = (
    "proof_of_check",
    "proof_sha256",
    "challenge",
    "execution_challenge",
    "raw_response",
    "raw_response_sha256",
    "response_id",
    "request_id",
    "authorization",
    "headers",
    "plan_sha256",
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NON_ASCII = re.compile(r"[^\x20-\x7e]")
_SHA1 = re.compile(r"\A[0-9a-f]{40}\Z")

#: Refusal reasons, as distinct machine-readable strings. A field withheld for
#: a control character and a field withheld for a secret-shaped token are
#: different events, and the private artifact records which happened even though
#: the published sentence is the same for both.
NOT_A_STRING = "not_a_string"
EMPTY = "empty"
CONTROL_CHARACTER = "control_character"
OUTSIDE_CHARSET = "outside_ascii_printable_policy"
SECRET_SHAPED = (
    "secret_shaped_token"  # noqa: S105 - a REFUSAL REASON, not a credential
)
SCAN_FAILED = "scan_failed"
NOTHING_LEFT = "nothing_left_after_redaction"

#: What replaces a redacted run token in published text.
REDACTED = "[redacted]"


# --------------------------------------------------------------- sanitize ---


#: One character to one replacement. Applied in a SINGLE pass — see below.
_ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
    # A backtick could close a code span this module opened and continue as
    # markup; a backslash could escape a character this function relied on.
    "`": "&#96;",
    "\\": "&#92;",
    # A mention needs `@` in the SOURCE text. It is never left there.
    "@": "&#64;",
}

_ASCII_DIGITS = frozenset("0123456789")


def escape_markup(text: str) -> str:
    """HTML-escape and neutralise everything GitHub would make LIVE.

    Escaping alone is not enough. GitHub's pipeline turns `@name` into a
    notification, `#12` into a cross-reference that also notifies, and a bare
    URL into a clickable link — all of which act on the SOURCE text, before any
    HTML entity is decoded. So each becomes a numeric character reference: the
    reader still sees `@name`, and the autolinker no longer does.

    ONE PASS, and that is the whole reason this is a loop rather than a chain of
    `str.replace` calls. The chained version was written first and was wrong:
    it emitted `&#64;` for `@` and then rewrote `#` before a digit, so its own
    replacement became `&&#35;64;` and `@mglaeser` reached the body as visible
    garbage instead of a neutralised mention. A single pass cannot rewrite its
    own output, which is the only version of this function that is safe to
    extend."""
    out, length = [], len(text)
    for index, char in enumerate(text):
        replacement = _ESCAPES.get(char)
        if replacement is not None:
            out.append(replacement)
        elif (char == "#" and index + 1 < length
              and text[index + 1] in _ASCII_DIGITS):
            # `#12` cross-references and notifies. `#` alone is harmless, so
            # ordinary prose keeps its hash.
            out.append("&#35;")
        elif char == ":" and text[index:index + 3] == "://":
            # A bare URL autolinks, and the scheme separator is what the linker
            # keys on. Neutralising the colon leaves `://` visible and inert.
            out.append("&#58;")
        else:
            out.append(char)
    return "".join(out)


def sanitize(value, *, scan, limit: int, field: str, redact=()) -> dict:
    """One field, from untrusted text to something publishable — or withheld.

    `scan` is the ENGINE's secret scanner, passed in rather than imported. The
    engine is the operator-approved artifact; a scanner this module imported for
    itself would be a second implementation of the one check whose disagreement
    nobody would notice until it mattered. It is a required argument for the
    same reason: a default would let a call site scan nothing by omission.

    `redact` carries exact run tokens — in practice the execution challenge —
    that are removed by identity BEFORE anything else runs. This is the one
    place a scrub is right, and the reason is that review policy REQUIRES the
    models to echo the challenge: `verdicts._validate_one` refuses a proof that
    does not open with it, and `verdicts.normalize_reason` strips it out of
    reasons because they carry it too. So a body-level refusal on the challenge
    would suppress every review in which any model did what it was told, and a
    per-field withholding would replace every explanation with the withheld
    sentence. Removing a token this run minted, knows exactly, and controls, is
    neither of those: it is deleting a value by identity rather than filtering
    text by suspicion. The body-level check in `assert_publishable` stays as the
    backstop for a token this missed.

    Never raises for bad CONTENT. A field that cannot be published is returned
    withheld, with the reason recorded, because requirement 6 is that a finding
    survives its explanation failing — a raise here would delete the finding.
    """
    if not callable(scan):
        refuse(f"category=review_render_without_a_scanner field={field} — every "
               "rendered field is scanned with the engine's own scanner; a "
               "renderer that scanned nothing would report success over "
               "unscanned provider text")
    if not isinstance(value, str):
        return _withheld(field, NOT_A_STRING)
    if not value.strip():
        return _withheld(field, EMPTY)
    # 0. Redaction by identity, FIRST. Before the scan, deliberately: the
    #    challenge is a high-entropy token, so a scan-first order would report
    #    every challenge-echoing reason as secret-shaped and withhold all of
    #    them. Before the bound too, so a long token cannot consume the budget.
    original = value
    redacted = False
    for token in redact or ():
        if isinstance(token, str) and token and token in value:
            value = value.replace(token, REDACTED)
            redacted = True
    if not value.strip() or value.strip() == REDACTED:
        return _withheld(field, NOTHING_LEFT)
    # 1. Control characters, on the raw text. A newline in a reason is enough to
    #    inject a heading, a list, or a fake second finding into this body.
    if _CONTROL.search(value):
        return _withheld(field, CONTROL_CHARACTER)
    # 2. Charset. The engine's `REASON_CHARSET_POLICY` restricts reason and proof
    #    to ASCII printable so a homoglyph cannot make one canned sentence look
    #    like two; the same restriction applies here, and extends to the path,
    #    where a bidi override would let a rendered filename read backwards.
    if _NON_ASCII.search(value):
        return _withheld(field, OUTSIDE_CHARSET)
    # 3. The secret scan, on the FULL raw text. Before the bound, deliberately:
    #    scanning a truncated string is a scan a long secret walks past.
    try:
        findings = scan(value)
    except Exception:                       # a scan that fails withholds
        return _withheld(field, SCAN_FAILED)
    if findings:
        return _withheld(field, SECRET_SHAPED)
    # 4. The bound, still on raw text.
    truncated = len(value) > limit
    kept = value[:max(0, limit - len(TRUNCATION_MARK))] if truncated else value
    if truncated:
        kept = f"{kept}{TRUNCATION_MARK}"
    # 5. Escaping LAST, so no entity can be cut in half by the bound.
    return {"field": field, "published": True, "text": escape_markup(kept),
            "truncated": truncated, "refusal": None, "redacted": redacted,
            "source_chars": len(original)}


def _withheld(field: str, refusal: str) -> dict:
    return {"field": field, "published": False, "text": None,
            "truncated": False, "refusal": refusal, "redacted": False,
            "source_chars": None}


# ------------------------------------------------------------- locations ----


def unit_locations(plan: dict) -> dict:
    """`unit_sha256` -> the plan's OWN file and changed-line metadata.

    Requirement 3, and the reason for it is worth stating: the alternative is to
    ask the provider for the path and the line numbers, and a path a model
    reports is a path a model can invent. `final_units` already carries
    `path_bytes_b64` and `new_line_range`, computed from the diff by the engine
    before anything was sent, so the location is a fact about the candidate
    rather than a claim in a response.

    The path is Base64 in the plan because a path is content — it can carry a
    secret, and it is excluded from provider-visible material for that reason.
    Decoding it here is deliberate and bounded: it is published only after
    `sanitize`, and a path that will not decode as UTF-8 is reported by digest.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("final_units"),
                                                    list):
        refuse("category=review_render_plan_has_no_units — the location of "
               "every finding comes from the plan; without it the review could "
               "only report what a model said about where it was looking")
    located = {}
    for unit in plan["final_units"]:
        if not isinstance(unit, dict):
            continue
        unit_hash = unit.get("unit_sha256")
        if not isinstance(unit_hash, str) or not unit_hash:
            continue
        located[unit_hash] = {
            "unit_sha256": unit_hash,
            "path": _decode_path(unit.get("path_bytes_b64")),
            "path_sha256": _path_digest(unit.get("path_bytes_b64")),
            "new_line_range": _line_range(unit.get("new_line_range")),
            "old_line_range": _line_range(unit.get("old_line_range")),
            "git_status": unit.get("git_status"),
        }
    return located


def _decode_path(encoded) -> str | None:
    """The path as text, or None. Never raises — None is a rendered outcome."""
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _path_digest(encoded) -> str | None:
    """A stable identifier for a path that cannot be rendered.

    Not the path. A grouping key has to exist even when the name cannot be
    published, or two findings in one unpublishable file would read as two
    unrelated files."""
    if not isinstance(encoded, str) or not encoded:
        return None
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _line_range(value):
    """A two-integer range, or None. Booleans are not integers here.

    `isinstance(True, int)` is true in Python, and a range rendered as
    `True–False` is the kind of nonsense that makes a reader distrust the whole
    document."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    lo, hi = value
    for part in (lo, hi):
        if isinstance(part, bool) or not isinstance(part, int) or part < 0:
            return None
    return [int(lo), int(hi)]


# -------------------------------------------------------------- findings ----


def findings_from_votes(votes: list, *, locations: dict, scan,
                        redact=()) -> list:
    """Every refutation, as a sanitized finding bound to a location.

    A finding is a REFUTATION and nothing else, and that is a consequence of
    policy rather than a choice made here: `STRICT_ANY_REFUTATION` is on, so any
    refutation blocks and an approved review has none. Rendering approvals as
    "findings" would put green text under a heading a reader scans for problems.

    The verdicts arrive from `panel.execute`, which means they have already
    passed normalization, strict validation, the challenge and lens checks, the
    unit-set check and the output-privacy scan. Nothing here re-derives any of
    that; it selects, locates and sanitizes.
    """
    if not isinstance(votes, list):
        refuse("category=review_render_votes_not_a_list")
    findings = []
    for vote in votes:
        model = (vote or {}).get("model")
        if model not in PANEL_MODELS:
            refuse(f"category=review_render_ungoverned_model model={model!r} "
                   f"governed={list(PANEL_MODELS)} — a voice the governance "
                   "document does not name must not appear in a published "
                   "review as though it did")
        record = (vote or {}).get("v") or {}
        by_unit = record.get("verdicts_by_unit")
        if not isinstance(by_unit, dict):
            continue
        for unit_hash, verdict in sorted(by_unit.items()):
            if not isinstance(verdict, dict) or verdict.get("refuted") is not True:
                continue
            findings.append(_finding(model, unit_hash, verdict,
                                     locations=locations, scan=scan,
                                     redact=redact))
    # Sorted by where it is, then by who raised it, so two runs over the same
    # verdicts produce the same document and a reader can diff them.
    findings.sort(key=lambda f: (f["location"]["sort_key"], f["model"],
                                 f["unit_sha256"]))
    return findings


def _finding(model: str, unit_hash: str, verdict: dict, *, locations: dict,
             scan, redact=()) -> dict:
    location = _location_view(locations.get(unit_hash), unit_hash, scan=scan)
    reason = sanitize(verdict.get("reason"), scan=scan,
                      limit=MAX_REASON_CHARS, field="reason", redact=redact)
    return {
        "unit_sha256": unit_hash,
        "model": model,
        "confidence": _enum(verdict.get("confidence"), scan=scan,
                            field="confidence"),
        "checked_categories": _categories(verdict.get("checked_categories"),
                                          scan=scan),
        "reason": reason,
        "location": location,
    }


def _enum(value, *, scan, field: str) -> dict:
    """A short governed token — confidence, git status — sanitized anyway.

    The engine constrains `confidence` to its own enum, so this can only fail if
    the engine's guarantee has been broken. That is precisely when a renderer
    must not be the component that trusts it."""
    return sanitize(value, scan=scan, limit=MAX_CATEGORY_CHARS, field=field)


def _categories(values, *, scan) -> dict:
    """The checked categories, each sanitized, the list itself bounded."""
    if not isinstance(values, list):
        return {"shown": [], "withheld": 0, "overflow": 0}
    shown, withheld = [], 0
    for entry in values[:MAX_CATEGORIES_RENDERED]:
        got = sanitize(entry, scan=scan, limit=MAX_CATEGORY_CHARS,
                       field="checked_category")
        if got["published"]:
            shown.append(got["text"])
        else:
            withheld += 1
    return {"shown": shown, "withheld": withheld,
            "overflow": max(0, len(values) - MAX_CATEGORIES_RENDERED)}


def _location_view(located, unit_hash: str, *, scan) -> dict:
    """Where the finding is, as something publishable.

    A unit with no entry in the plan is not silently dropped and not rendered as
    an unknown file: it is reported as an unlocated unit with its own digest, so
    a reader can still find it in the private evidence. Dropping it would be the
    renderer deciding a finding did not exist."""
    if not isinstance(located, dict):
        return {"located": False, "path": None, "path_sha256": None,
                "line_range": None, "label": "location unavailable",
                "sort_key": (2, "", 0, unit_hash)}
    path = sanitize(located.get("path"), scan=scan, limit=MAX_PATH_CHARS,
                    field="path")
    line_range = located.get("new_line_range") or located.get("old_line_range")
    # An integer RANK rather than a sentinel character. Ordering by a
    # high codepoint would work and would also put a non-ASCII character into a
    # sort key that a future edit might render; the rank cannot be rendered by
    # accident because it is not text.
    if path["published"]:
        label, rank, sort_path = path["text"], 0, str(located.get("path") or "")
    else:
        digest = str(located.get("path_sha256") or "")
        label = f"path withheld by output-privacy policy (path {digest[:16]})"
        rank, sort_path = 1, digest
    if line_range:
        label = (f"{label} lines {line_range[0]}-{line_range[1]}"
                 if line_range[0] != line_range[1]
                 else f"{label} line {line_range[0]}")
    return {"located": True, "path": path, "path_sha256":
            located.get("path_sha256"), "line_range": line_range,
            "label": label,
            "sort_key": (rank, sort_path, (line_range or [0])[0], unit_hash)}


# ----------------------------------------------------------------- review ---


def build_review(*, decision: str, candidate_head_sha: str,
                 candidate_base_sha: str, votes: list, plan: dict,
                 aggregate_record: dict, scan, run_url: str,
                 run_id, evidence_sha256: str,
                 count_evidence_sha256: str | None = None,
                 challenge: str | None = None) -> dict:
    """The whole sanitized review as DATA, before any Markdown exists.

    Kept separate from `render` so that the private artifact and the published
    comment are two views of one object rather than two documents that agree
    until someone edits one of them."""
    if decision not in ("approved", "blocked"):
        refuse(f"category=review_render_unknown_decision decision={decision!r}")
    for name, sha in (("candidate_head_sha", candidate_head_sha),
                      ("candidate_base_sha", candidate_base_sha)):
        if not isinstance(sha, str) or not _SHA1.match(sha):
            refuse(f"category=review_render_sha_not_a_commit field={name} — a "
                   "published review names the exact commit it reviewed; an "
                   "abbreviated or symbolic ref names whatever it points at now")
    locations = unit_locations(plan)
    # The challenge is passed to be REMOVED, never to be rendered. It is the one
    # value this function is given for the sole purpose of proving it did not
    # travel.
    redact = tuple(t for t in (challenge,) if isinstance(t, str) and t)
    findings = findings_from_votes(votes, locations=locations, scan=scan,
                                   redact=redact)
    assert_rendering_did_not_change_the_decision(
        findings=findings, decision=decision, aggregate_record=aggregate_record)
    review = {
        "render_version": RENDER_VERSION,
        "decision": decision,
        "candidate_head_sha": candidate_head_sha,
        "candidate_base_sha": candidate_base_sha,
        "governed_models": list(PANEL_MODELS),
        "models_voting": list(aggregate_record.get("models_voting") or []),
        "required_approver": REQUIRED_APPROVER,
        "minimum_other_approvers": MIN_DISTINCT_OTHER_APPROVALS,
        "strict_any_refutation": STRICT_ANY_REFUTATION,
        "findings": findings,
        "finding_count": len(findings),
        "unit_count": len(locations),
        "run_url": run_url,
        "run_id": run_id,
        "evidence_sha256": evidence_sha256,
        "count_evidence_sha256": count_evidence_sha256,
        "architecture": dict(ARCHITECTURE_FACTS),
        "honest_scope": (
            "rendered from verdicts that already passed trusted response "
            "normalization, strict verdict validation, challenge/lens/unit "
            "validation, the output-privacy scan and the role and refutation "
            "gates. The machine decision is authoritative; this document is a "
            "reading of it and never a revision of it"),
    }
    assert_no_forbidden_fields(review)
    return review


def assert_rendering_did_not_change_the_decision(*, findings: list,
                                                 decision: str,
                                                 aggregate_record: dict) -> dict:
    """Requirement 11, as a check rather than as a promise.

    Two directions, and the second is the one a summariser gets wrong. Findings
    present with an `approved` decision would mean the panel refuted something
    and the published headline said otherwise. No findings with a `blocked`
    decision would mean the reader is told to look and given nothing — which is
    how a real refutation becomes "the bot is broken, merge it".

    The `blocked` direction is asserted against the AGGREGATE's own refuted
    count, not against the rendered list, because the rendered list is the thing
    under suspicion."""
    if decision != aggregate_record.get("decision"):
        refuse(f"category=review_render_decision_disagrees_with_aggregate "
               f"rendered={decision!r} "
               f"aggregate={aggregate_record.get('decision')!r} — the machine "
               "decision is authoritative and a rendering may never restate it")
    if decision == "approved" and findings:
        refuse(f"category=review_render_findings_under_an_approval "
               f"findings={len(findings)} — a refutation blocks under strict "
               "policy, so findings beside an approval means one of the two is "
               "wrong and the published one would be believed")
    if decision == "blocked" and not findings:
        refuse("category=review_render_block_with_nothing_to_read — a blocked "
               "review whose readable form lists nothing is the defect this "
               "publisher exists to remove; the finding must survive the block")
    return {"decision": decision, "findings": len(findings)}


def assert_no_forbidden_fields(node, *, path: str = "review") -> None:
    """No forbidden KEY anywhere in the review object, at any depth.

    Structural, so it catches the real failure — a future edit that copies a
    verdict dict into a finding instead of selecting fields out of it — without
    refusing prose that happens to contain an English word.

    `proof_of_check` is the one worth naming twice. It is required to OPEN with
    the execution challenge, so publishing a proof publishes the challenge, and
    a published challenge is one a model could echo without having seen the
    request. It is the single field whose publication would break the
    anti-canned-response control."""
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in FORBIDDEN_FIELDS:
                refuse(f"category=review_carries_a_forbidden_field "
                       f"field={key} at={path} — this field is refused by name "
                       "because omitting it is a property of today's renderer "
                       "and naming it is a property of every future one")
            assert_no_forbidden_fields(value, path=f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            assert_no_forbidden_fields(value, path=f"{path}[{index}]")


def assert_publishable(body: str, *, challenge: str | None = None) -> str:
    """The last gate before anything leaves. Named refusals, not a filter.

    Deliberately a REFUSAL and not a scrub. A scrub that removed a challenge
    from a body would publish the rest of a document that had already proved it
    was built wrong, and the next reader would have no way to know.

    `challenge` is compared as an exact VALUE, which is the precise version of
    the check: the danger is the specific per-run token appearing in the body,
    not the English word."""
    if not isinstance(body, str) or not body.strip():
        refuse("category=review_body_empty")
    if len(body) > MAX_BODY_CHARS:
        refuse(f"category=review_body_unbounded chars={len(body)} "
               f"limit={MAX_BODY_CHARS}")
    if MARKER not in body:
        refuse("category=review_body_has_no_marker — without the marker the "
               "next run cannot find this comment and posts a second one")
    without_newlines = body.replace("\n", "")
    if _CONTROL.sub("", without_newlines) != without_newlines:
        refuse("category=review_body_carries_control_characters")
    if "@" in body:
        refuse("category=review_body_carries_a_live_mention — every '@' is "
               "written as a numeric character reference so GitHub's "
               "autolinker cannot turn it into a notification")
    if isinstance(challenge, str) and challenge and challenge in body:
        refuse("category=review_body_carries_the_execution_challenge — the "
               "challenge is what proves a verdict was written for this run; "
               "published, it is a token a later model could echo without "
               "having seen the request")
    return body


# ----------------------------------------------------------------- render ---


def head_binding(candidate_head_sha: str) -> str:
    return f"{HEAD_BINDING_PREFIX}{candidate_head_sha}{HEAD_BINDING_SUFFIX}"


def head_of(body) -> str | None:
    """The head a comment was bound to, read back off its own text."""
    if not isinstance(body, str):
        return None
    found = re.search(
        re.escape(HEAD_BINDING_PREFIX) + r"([0-9a-f]{40})"
        + re.escape(HEAD_BINDING_SUFFIX), body)
    return found.group(1) if found else None


def render(review: dict, *, challenge: str | None = None) -> str:
    """The published body. Every value in it came through `sanitize`.

    Findings are appended one at a time against the total bound rather than
    rendered and then trimmed, because trimming a finished document cuts a
    finding in half and the half that survives reads like a complete one.

    `challenge` is not rendered. It is passed so the final gate can prove it is
    ABSENT — the one check that has to see the secret to confirm it did not
    travel."""
    decision = review["decision"]
    headline = ("approved — no refutation from any governed model"
                if decision == "approved"
                else "blocked — at least one governed model refuted a change")
    lines = [
        MARKER,
        head_binding(review["candidate_head_sha"]),
        "",
        f"### Mid-term panel review: {headline}",
        "",
        "| | |",
        "|---|---|",
        f"| Decision | **{decision}** |",
        f"| Reviewed head | `{review['candidate_head_sha']}` |",
        f"| Base | `{review['candidate_base_sha']}` |",
        f"| Governed models | {_code_list(review['governed_models'])} |",
        f"| Required approver | `{review['required_approver']}` |",
        f"| Refutation policy | any valid refutation blocks "
        f"(`strict_any_refutation` = {review['strict_any_refutation']}) |",
        f"| Units reviewed | {review['unit_count']} |",
        f"| Actionable findings | {review['finding_count']} |",
        f"| Actions run | [{review['run_id']}]({review['run_url']}) |",
        f"| Panel evidence | `ev={str(review['evidence_sha256'])[:16]}` "
        "(private artifact `panel-evidence.json`) |",
    ]
    if review.get("count_evidence_sha256"):
        lines.append(
            f"| Count evidence | "
            f"`ev={str(review['count_evidence_sha256'])[:16]}` |")
    lines.append("")

    if not review["findings"]:
        lines.append(NO_FINDINGS)
        lines.append("")
        lines.extend(_footer(review))
        return assert_publishable("\n".join(lines), challenge=challenge)

    # Two blanks, so the heading is followed by an empty line. A heading with
    # the next block glued to it renders, but it renders as a wall.
    lines.extend([f"#### Findings ({review['finding_count']})", "", ""])
    head = "\n".join(lines)
    footer = "\n".join(["", *_footer(review)])
    # Reserved against the LARGEST note that could be needed — the one naming
    # every finding as omitted. Reserving against `_omitted_note(0)` was one
    # character per digit short, which is the sort of off-by-a-little that only
    # ever shows up on the run that mattered.
    budget = (MAX_BODY_CHARS - len(head) - len(footer)
              - len(_omitted_note(review["finding_count"])))

    rendered, shown, used = [], 0, 0
    for finding in review["findings"][:MAX_FINDINGS_RENDERED]:
        block = _render_finding(finding)
        if used + len(block) > budget:
            break
        rendered.append(block)
        used += len(block)
        shown += 1

    body = head + "".join(rendered)
    omitted = review["finding_count"] - shown
    if omitted > 0:
        body += _omitted_note(omitted)
    return assert_publishable(body + footer, challenge=challenge)


def _render_finding(finding: dict) -> str:
    reason = (finding["reason"]["text"] if finding["reason"]["published"]
              else WITHHELD)
    categories = finding["checked_categories"]
    rendered_categories = (", ".join(f"`{c}`" for c in categories["shown"])
                           or "`(none publishable)`")
    if categories["withheld"] or categories["overflow"]:
        rendered_categories += (
            f" (+{categories['withheld'] + categories['overflow']} not shown)")
    confidence = (finding["confidence"]["text"]
                  if finding["confidence"]["published"] else "(withheld)")
    return "\n".join([
        f"**{finding['location']['label']}**",
        "",
        f"- raised by `{finding['model']}` · confidence `{confidence}` · "
        f"checked {rendered_categories}",
        f"- unit `{finding['unit_sha256'][:16]}`",
        "",
        f"> {reason}",
        "", "",
    ])


def _omitted_note(count: int) -> str:
    return ("\n_" + str(count) + " further finding(s) were omitted to stay "
            "within the published-comment size bound. The complete sanitized "
            "set is retained in the private `panel-review.json` artifact for "
            "this run._\n")


def _footer(review: dict) -> list:
    return [
        "---",
        "",
        "<sub>Mid-term single-repository panel. **Not write-separated**: it "
        "holds a repository-scoped provider secret in the repository it "
        "reviews, so this is not an independent third-party attestation and "
        "cannot satisfy a requirement written for one. The machine decision is "
        "authoritative — this comment is a reading of it. Full per-model "
        "verdicts, proofs and digests are retained privately and are not "
        "published.</sub>",
    ]


def _code_list(values) -> str:
    return ", ".join(f"`{v}`" for v in values) or "`(none)`"


__all__ = [
    "MARKER", "WITHHELD", "NO_FINDINGS", "RENDER_VERSION", "MAX_BODY_CHARS",
    "MAX_REASON_CHARS", "FORBIDDEN_FIELDS", "escape_markup", "sanitize",
    "unit_locations", "findings_from_votes", "build_review", "render",
    "assert_publishable", "assert_no_forbidden_fields",
    "assert_rendering_did_not_change_the_decision", "head_binding", "head_of",
]
