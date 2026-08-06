"""Canonical serialisation and hashing.

Every hash in a review plan must be reproducible byte-for-byte on any
supported platform, or the plan cannot be revalidated at execution time and
the whole chain of evidence is decorative. The rules are fixed here, once, and
every module hashes through these helpers.

CANONICAL RULES
  * JSON: UTF-8, sorted keys, `(",", ":")` separators, no insignificant
    whitespace, `ensure_ascii=False`, `allow_nan=False`, trailing newline
    never added. NaN/Infinity are rejected: `json.dumps` would emit the
    non-JSON tokens `NaN`/`Infinity`, which other parsers reject or coerce
    differently — the exact cross-platform divergence canonical form exists
    to prevent.
  * Paths: raw bytes, base64-encoded for transport (`path_bytes_b64`). A git
    path on Linux is an arbitrary byte string; decoding it to text to compare
    or hash would conflate distinct paths and lose non-UTF-8 names entirely.
  * Base64: canonical encodings only. `unb64` rejects anything that does not
    re-encode to the identical string (missing/extra padding, whitespace,
    non-zero trailing bits), so two spellings of one value can never carry
    two different meanings past a validator that saw only one of them.
  * Unicode: NO normalisation. NFC/NFD folding would make two genuinely
    different paths (or two different source lines) hash identically.
  * Newlines: NO normalisation. A CRLF line keeps its CR as content, so a
    line-ending change is a real change and hashes differently.
  * Domain separation: every hash is prefixed with a versioned label of the
    shape `name-vN`, enforced at call time, so a digest computed for one
    purpose can never be replayed as another and an unversioned label cannot
    be introduced by accident.
  * Integers: decimal ASCII via `num`, which REJECTS bool — `num(True)` would
    be byte-identical to `num(1)`, letting a flag forge a count.
  * Money: integers only (`money_micros`), never floats — 0.1 + 0.2 style
    drift in a cost ledger is a correctness bug, not a rounding detail.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

SCHEMA_VERSION = 1

_LABEL = re.compile(rb"\A[a-z0-9][a-z0-9-]*-v[0-9]+\Z")


def canonical_json(value: Any) -> bytes:
    """The one canonical JSON encoding used for every hash and artefact."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(label: bytes, *parts: bytes) -> str:
    """A domain-separated digest over length-prefixed parts.

    The label pins both the purpose and the schema version (shape `name-vN`,
    enforced), so an atom digest can never be mistaken for (or replayed as) a
    unit digest, and bumping a schema invalidates every stored hash rather
    than silently reinterpreting it.

    Parts are length-prefixed rather than delimiter-joined: with a plain
    NUL join, (b"ab", b"c") and (b"a", b"bc") would collide whenever a part
    may itself contain the delimiter (raw path bytes can contain anything
    but NUL — and future callers should not need to know that)."""
    if not _LABEL.match(label):
        raise ValueError(
            "digest label must be a versioned domain label (name-vN); "
            f"got a label of {len(label)} byte(s)")
    h = hashlib.sha256()
    h.update(label)
    for part in parts:
        h.update(len(part).to_bytes(8, "big"))
        h.update(part)
    return h.hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    """Strict, canonical-only Base64 decoding.

    `validate=True` rejects non-alphabet bytes; the re-encode check rejects
    everything else `b64decode` quietly tolerates (missing padding, extra
    padding, non-zero trailing bits). One value, one spelling."""
    raw = base64.b64decode(text.encode("ascii"), validate=True)
    if base64.b64encode(raw).decode("ascii") != text:
        raise ValueError("non-canonical base64 encoding rejected")
    return raw


def num(value: int) -> bytes:
    """Integers enter a digest as their decimal ASCII form, never as raw ints.

    bool is rejected: it IS an int in Python, and `num(True) == num(1)` would
    make a flag and a count byte-identical inside a hash."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("num() takes a plain int (bool rejected)")
    return str(value).encode("ascii")


def money_micros(value: int) -> str:
    """Money as non-negative integer micro-units, serialised as decimal ASCII.

    Reserved for the Cycle-3 cost planner; defined here so no later module is
    tempted to put a float in a ledger."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("money_micros() takes a plain int (bool/float rejected)")
    if value < 0:
        raise ValueError("money is non-negative")
    return str(value)
