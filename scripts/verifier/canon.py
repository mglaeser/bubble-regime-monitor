"""Canonical serialisation and hashing.

Every hash in a review plan must be reproducible byte-for-byte on any
supported platform, or the plan cannot be revalidated at execution time and
the whole chain of evidence is decorative. The rules are fixed here, once, and
every module hashes through these helpers.

CANONICAL RULES
  * JSON: UTF-8, sorted keys, `(",", ":")` separators, no insignificant
    whitespace, `ensure_ascii=False`, trailing newline never added.
  * Paths: raw bytes, base64-encoded for transport (`path_bytes_b64`). A git
    path on Linux is an arbitrary byte string; decoding it to text to compare
    or hash would conflate distinct paths and lose non-UTF-8 names entirely.
  * Unicode: NO normalisation. NFC/NFD folding would make two genuinely
    different paths (or two different source lines) hash identically.
  * Newlines: NO normalisation. A CRLF line keeps its CR as content, so a
    line-ending change is a real change and hashes differently.
  * Domain separation: every hash is prefixed with a versioned label so a
    digest computed for one purpose can never be replayed as another.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

SCHEMA_VERSION = 1


def canonical_json(value: Any) -> bytes:
    """The one canonical JSON encoding used for every hash and artefact."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(label: bytes, *parts: bytes) -> str:
    """A domain-separated digest over NUL-joined parts.

    The label pins both the purpose and the schema version, so an atom digest
    can never be mistaken for (or replayed as) a unit digest, and bumping a
    schema invalidates every stored hash rather than silently reinterpreting
    it."""
    return sha256_hex(label + b"\0" + b"\0".join(parts))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def num(value: int) -> bytes:
    """Integers enter a digest as their decimal ASCII form, never as raw ints."""
    return str(int(value)).encode("ascii")
