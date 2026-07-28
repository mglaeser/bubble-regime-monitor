"""All-text secret preflight, before ANY provider call (MC3 §33).

A token-count request TRANSMITS content, so preflight runs before
/v1/responses/input_tokens, not merely before generation. It scans the exact
assembled request body — instructions, unit content, any path or structural
metadata inside it, the response schema, every batch section — because the
thing that must be clean is the thing that leaves.

Layered and fail-closed:

  * known credential shapes (provider keys, GitHub tokens, AWS ids, private
    key headers, connection strings with inline passwords, JWTs);
  * high-entropy long tokens that survive a conservative allowlist of
    repository-owned shapes (hex digests, base64 of known-size digests);
  * an explicit reviewed allowlist keyed by exact string;
  * a scan failure of any kind is a BLOCK.

HONEST SCOPE: this is a denylist plus an entropy heuristic. It cannot prove
absence of secrets. The content-policy extension list proves even less — it
is a path rule, not a proof about text. Both facts are recorded in the
evidence so no reader mistakes a pass for a guarantee.
"""

from __future__ import annotations

import base64
import math
import re

from .canon import canonical_json, digest, sha256_hex
from .errors import SECRET_PREFLIGHT_FAILED, BlockingError

# Known credential shapes. Each pattern is deliberately specific; the
# entropy sweep below is the generic net.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("openai_project_key", re.compile(r"sk-proj-[A-Za-z0-9_-]{8,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}")),
    ("github_fine_grained", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("private_key_header",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
                       r"[A-Za-z0-9_-]{10,}\b")),
    ("url_inline_password",
     re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s/@]{4,}@")),
    ("authorization_header",
     re.compile(r"(?i)\bauthorization\s*:\s*(?:bearer|basic)\s+\S{8,}")),
)

# Repository-owned shapes that are high-entropy by construction and are NOT
# credentials: hex digests (sha1/sha256), and canonical base64 of small path
# bytes. Anything else long and dense is treated as a possible secret.
_HEX_DIGEST = re.compile(r"\A[0-9a-f]{32,64}\Z")
# A dense run of token characters. '/' is deliberately EXCLUDED: in ordinary
# text it is a path separator, and joining path segments into one 32-char
# "token" produced false positives on repository paths. '=' is excluded for
# the same reason ("exit_raises=RuntimeError" is two identifiers, not a
# token). Canonical base64 — the only place '+', '/' and '=' legitimately
# appear inside one value — gets its own sweep below.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{24,}")
_B64_BLOB = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")
# A plain source identifier (CamelCase class, CONSTANT_NAME, snake_case) is
# long and dense but is not a credential. It is only suspicious when it also
# looks random: mixed case AND digits AND >= 32 chars.
_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
# Hyphen/underscore-joined lowercase WORDS ("documentation-versus-hash") are
# prose, not credentials. A segment carrying digits (sk-proj-abcdef123456)
# does not qualify — and those are caught by the explicit patterns anyway.
_WORDS = re.compile(r"\A[a-z]{2,}(?:[-_][a-z]{2,})+\Z")

_ENTROPY_BITS_PER_CHAR = 3.6      # conservative: base64-ish text sits ~4.5


def _shannon_bits_per_char(text: str) -> float:
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _decodes_to_clean_text(value: str, depth: int) -> bool:
    """True when `value` is canonical base64 of ORDINARY text.

    The repository legitimately carries base64 of small strings (a path, a
    label). Rather than trusting the encoding, the plaintext is scanned with
    the SAME rules: base64 is therefore never an escape hatch — base64 of a
    credential decodes to a credential and is flagged. Depth is bounded so
    nested encodings cannot spin."""
    if depth <= 0 or len(value) % 4:
        return False
    try:
        text = base64.b64decode(value, validate=True).decode("ascii")
    except Exception:
        return False
    if not text.isprintable():
        return False
    return not scan_text(text, depth=depth - 1)


def scan_text(text: str, *, allowlist: frozenset[str] = frozenset(),
              depth: int = 2) -> list[dict]:
    """Return sanitized findings. A finding NEVER contains the secret."""
    findings: list[dict] = []

    # An operator-reviewed string clears its whole SPAN, not merely its exact
    # value: the sweeps below tokenize differently (the base64 sweep excludes
    # '-', so it sees only the tail of "sk-proj-…"), and a substring of a
    # reviewed fixture is still that reviewed fixture.
    cleared: list[tuple[int, int]] = []
    for entry in allowlist:
        if not entry:
            continue
        at = text.find(entry)
        while at != -1:
            cleared.append((at, at + len(entry)))
            at = text.find(entry, at + 1)

    def _covered(start: int) -> bool:
        return any(f["offset"] <= start < f["offset"] + f["length"]
                   for f in findings)

    def _is_cleared(start: int, end: int) -> bool:
        return any(lo <= start and end <= hi for lo, hi in cleared)

    def _report(category: str, match: re.Match[str]) -> None:
        value = match.group(0)
        findings.append({
            "category": category,
            "offset": match.start(),
            "length": len(value),
            "value_sha256": sha256_hex(value.encode("utf-8",
                                                    "surrogateescape")),
        })

    for name, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            if _is_cleared(match.start(), match.end()):
                continue
            _report(name, match)

    # Base64 sweeps FIRST: only it sees a blob together with its '=' padding,
    # which is what decodes. A blob cleared here records its span so the
    # generic sweep cannot re-report the same bytes minus the padding.
    for regex in (_B64_BLOB, _TOKEN):
        for match in regex.finditer(text):
            value = match.group(0)
            if _is_cleared(match.start(), match.end()):
                continue
            if _HEX_DIGEST.match(value):
                continue
            if _WORDS.match(value):
                continue                   # hyphen/underscore-joined prose
            if _IDENTIFIER.match(value) and not (
                    len(value) >= 32
                    and any(c.isdigit() for c in value)
                    and any(c.islower() for c in value)
                    and any(c.isupper() for c in value)):
                continue                   # ordinary source identifier
            if _shannon_bits_per_char(value) < _ENTROPY_BITS_PER_CHAR:
                continue
            if regex is _B64_BLOB and _decodes_to_clean_text(value, depth):
                cleared.append((match.start(), match.end()))
                continue                   # base64 of ordinary text
            if _covered(match.start()):
                continue                   # already reported by a pattern
            _report("high_entropy_token", match)
    return findings


def preflight_request(text: str, *, label: str,
                      allowlist: frozenset[str] = frozenset()) -> dict:
    """Scan one assembled request body. Findings BLOCK before any call."""
    try:
        findings = scan_text(text, allowlist=allowlist)
    except Exception as exc:                       # a scan failure blocks
        raise BlockingError(
            SECRET_PREFLIGHT_FAILED,
            f"category=preflight_scan_failed label={label} "
            f"exception_class={type(exc).__name__}") from exc
    if findings:
        categories = sorted({f["category"] for f in findings})
        raise BlockingError(
            SECRET_PREFLIGHT_FAILED,
            f"category=secret_preflight_failed label={label} "
            f"finding_count={len(findings)} categories={categories} — zero "
            "count calls and zero generation calls will be made")
    return {
        "label": label,
        "scanned_chars": len(text),
        "scanned_sha256": sha256_hex(text.encode("utf-8", "surrogateescape")),
        "finding_count": 0,
        "patterns_version": len(_PATTERNS),
        "entropy_threshold_bits_per_char": str(_ENTROPY_BITS_PER_CHAR),
    }


def evidence_record(entries: list[dict]) -> dict:
    record = {
        "entries": entries,
        "scanned_request_count": len(entries),
        "honest_scope": "denylist plus an entropy heuristic; it cannot prove "
                        "the absence of secrets, and the content-policy "
                        "extension list is a PATH rule that proves nothing "
                        "about text",
    }
    record["preflight_evidence_sha256"] = digest(
        b"secret-preflight-v1", canonical_json(record))
    return record
