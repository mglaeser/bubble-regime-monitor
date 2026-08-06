"""Content policy: may this change's BYTES leave the origin? (B2, 6.6)

Separate from classification on purpose. Classification answers "must this
change's atoms be provably covered?"; content policy answers "may its bytes
be shown to a provider?". The dangerous combination — control-bearing AND
content withheld — produces PRIVACY_EXCLUDED_CONTROL here, at the POLICY
layer. Atomization (B1) reports facts only; B1-08 is closed by this module.

The exclusion list is the UNION of every prior exclusion in this repository:
the live panel's list in scripts/independent_verify.py (which the panel
itself extended twice: round-3 data-file classes, Sol's icase veto on PR #21)
and the B1 port's binary/asset list. Paths are NEVER excluded from the
authoritative changed-file universe — only their content is withheld — and a
denylist is inherently incomplete; that residual stays documented in
docs/INDEPENDENT_REVIEW_PANEL.md.
"""

from __future__ import annotations

from .errors import PRIVACY_EXCLUDED_CONTROL

# The four content-policy states (mandate 6.6).
INCLUDED = "included"
PRIVACY_EXCLUDED = "privacy_excluded"
BINARY = "binary"
UNAVAILABLE = "unavailable"

# Union of scripts/independent_verify.py EXCLUDE_EXTS (panel, authoritative)
# and the B1 gitdiff port's list. Sorted for a deterministic record.
UNION_EXCLUDE_EXTS: tuple[str, ...] = tuple(sorted({
    # panel list (independent_verify.py)
    "webp", "png", "jpg", "jpeg", "gif", "ico", "svg", "avif", "bmp", "tiff",
    "woff", "woff2", "ttf", "otf", "eot", "pdf", "geojson", "db", "rds",
    "xlsx", "csv", "tsv", "sql", "jsonl", "ndjson", "parquet", "feather",
    "sqlite", "sqlite3", "dump", "bak", "pickle", "pkl", "npz", "npy",
    # B1 port additions
    "zip", "gz", "tar", "whl", "so", "dylib", "dll", "mp4", "mov", "mp3",
    "wav",
}))

# Root-anchored directory exclusion (the runtime DB volume).
EXCLUDED_DIR_PREFIXES: tuple[bytes, ...] = (b"data/",)

_EXT_SET = {e.encode("ascii") for e in UNION_EXCLUDE_EXTS}


def path_content_policy(path: bytes) -> tuple[str, str | None]:
    """(policy, reason) from the PATH alone; case-insensitive extensions.

    Binary/unavailable are facts discovered later (from the diff body or a
    fetch failure) — this function only ever answers INCLUDED or
    PRIVACY_EXCLUDED."""
    lowered = path.lower()
    for prefix in EXCLUDED_DIR_PREFIXES:
        if lowered.startswith(prefix):
            return PRIVACY_EXCLUDED, f"directory:{prefix.decode('ascii')}"
    base = lowered.rsplit(b"/", 1)[-1]
    if b"." in base:
        ext = base.rsplit(b".", 1)[-1]
        if ext in _EXT_SET:
            return PRIVACY_EXCLUDED, f"extension:.{ext.decode('ascii')}"
    return INCLUDED, None


def record_content_policy(path: bytes,
                          orig_path: bytes | None) -> tuple[str, str | None]:
    """EITHER endpoint excluded excludes the record: a rename's diff body
    shows OLD content too, so an excluded source withholds the body even
    when the destination looks harmless."""
    policy, reason = path_content_policy(path)
    if policy != INCLUDED:
        return policy, reason
    if orig_path is not None:
        old_policy, old_reason = path_content_policy(orig_path)
        if old_policy != INCLUDED:
            return old_policy, f"orig-{old_reason}"
    return INCLUDED, None


def control_block(*, control_bearing: bool,
                  content_policy: str) -> tuple[str, str] | None:
    """The policy decision B1-08 moved here: unreviewable control blocks.

    Returns (code, reason) or None. A non-control file with withheld content
    is legitimate (its metadata obligation still exists); a CONTROL file with
    withheld content means the thing that most needs review was reviewed by
    nobody, and nothing downstream may treat that as green."""
    if content_policy == INCLUDED:
        return None
    if not control_bearing:
        return None
    return (PRIVACY_EXCLUDED_CONTROL,
            f"control-bearing change with content_policy={content_policy}: "
            "its content was reviewed by NOBODY and a hash marker is not "
            "semantic review")
