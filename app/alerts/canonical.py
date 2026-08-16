"""Canonical bytes, content hashes and ULIDs.

Every evaluation and every delivery is tied to exact content hashes, so
"canonical" has to mean one thing everywhere: the same logical object must
produce the same bytes on any machine, in any process, in any Python version.

Pure module: no clock of its own for hashing, no session, no settings.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any

# ---------------------------------------------------------------------------
# canonical serialization
# ---------------------------------------------------------------------------

# Crockford base32 — I, L, O and U are omitted so a ULID cannot be misread.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # pragma: allowlist secret


def canonical_json(payload: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace, real UTF-8.

    `ensure_ascii=False` keeps German phrase text readable in the persisted
    bytes; the result is always encoded UTF-8 before hashing.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(payload: Any) -> bytes:
    return canonical_json(payload).encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def sha256_of(payload: Any) -> str:
    """SHA-256 of a structure's canonical bytes."""
    return sha256_hex(canonical_bytes(payload))


def identity_hash(*parts: object) -> str:
    """SHA-256 over a `|`-joined list of scalar parts.

    Used for the deterministic identities the mandate specifies as
    `sha256(a | b | c)`. `None` becomes the empty string so an absent optional
    part is stable rather than the literal text "None".
    """
    joined = "|".join("" if p is None else str(p) for p in parts)
    return sha256_hex(joined)


def sorted_hash_set(hashes: object) -> str:
    """Order-independent identity of a SET of content hashes.

    An evaluation batch may span the current ruleset plus any archived rulesets
    that still own open episodes; the batch's identity must not depend on the
    order they happened to be loaded in.
    """
    unique = sorted({str(h) for h in hashes})  # type: ignore[union-attr]
    return sha256_hex("|".join(unique))


# ---------------------------------------------------------------------------
# ULID
# ---------------------------------------------------------------------------

_ulid_lock = threading.Lock()
_last_ms = -1
_last_random = 0

_TIME_BITS = 48
_RANDOM_BITS = 80
_RANDOM_MAX = (1 << _RANDOM_BITS) - 1


def _encode_crockford(value: int, length: int) -> str:
    out = [""] * length
    for i in range(length - 1, -1, -1):
        out[i] = _CROCKFORD[value & 0x1F]
        value >>= 5
    return "".join(out)


def new_ulid(timestamp_ms: int) -> str:
    """A lexicographically sortable 26-character ULID for a given instant.

    The caller supplies the timestamp: this module owns no clock, so a replay
    can produce reproducible identifiers. Monotonic within a millisecond — if
    two ULIDs are minted in the same millisecond the random component is
    incremented rather than redrawn, so sort order still matches mint order.

    Randomness comes from `os.urandom`, not `random`: these identifiers appear
    in URLs and must not be guessable.
    """
    global _last_ms, _last_random
    if timestamp_ms < 0 or timestamp_ms >= (1 << _TIME_BITS):
        raise ValueError(f"timestamp_ms {timestamp_ms} outside the 48-bit ULID range")
    with _ulid_lock:
        if timestamp_ms == _last_ms:
            if _last_random >= _RANDOM_MAX:
                raise RuntimeError("ULID randomness exhausted within one millisecond")
            _last_random += 1
        else:
            _last_ms = timestamp_ms
            _last_random = int.from_bytes(os.urandom(10), "big")
        randomness = _last_random
    return _encode_crockford(timestamp_ms, 10) + _encode_crockford(randomness, 16)


def is_ulid(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 26
        and all(c in _CROCKFORD for c in value)
    )
