"""Strict raw-change records (B2, mandate 6.3).

`git diff --raw -z --no-abbrev` is the authoritative changed-file universe.
One record carries everything the repository identity must bind: status,
rename/copy score, both modes, both full object IDs, raw path bytes, and a
deterministic ordinal. The parser accepts EXACTLY the documented shapes and
blocks everything else — every record this layer dropped silently would be a
file nobody reviews and no coverage proof misses.

Accepted statuses: A M D T, R000..R100, C000..C100. Everything else —
unmerged (U), unknown (X), broken pair (B), empty, non-ASCII — blocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .canon import b64
from .errors import DIFF_PARSE_FAILURE, BlockingError
from .gitdiff import DIFF_COMMON, ChangedFile, run_git

_MODE = re.compile(rb"\A[0-7]{6}\Z")
_OID = re.compile(rb"\A[0-9a-f]{40}\Z")
_STATUS = re.compile(rb"\A(?:[AMDT]|[RC][0-9]{3})\Z")
ZERO_OID = "0" * 40


def _err(category: str, **facts) -> BlockingError:
    """Sanitized parse failure: coordinates and lengths, never raw bytes."""
    detail = " ".join(f"{k}={v}" for k, v in sorted(facts.items()))
    return BlockingError(DIFF_PARSE_FAILURE,
                         f"category={category} {detail}".strip())


@dataclass(frozen=True)
class RawChange:
    """One raw record. `path` is the destination (or only) path; `orig_path`
    is the rename/copy source, exactly as for ChangedFile."""

    ordinal: int
    status: str                 # "A" "M" "D" "T" "R" "C"
    score: int | None           # 0..100 for R/C, else None
    old_mode: str
    new_mode: str
    old_oid: str
    new_oid: str
    path: bytes
    orig_path: bytes | None

    def to_record(self) -> dict:
        old_path = self.orig_path if self.orig_path is not None else (
            None if self.status == "A" else self.path)
        new_path = None if self.status == "D" else self.path
        return {
            "ordinal": self.ordinal,
            "status": self.status,
            "score": self.score,
            "old_mode": self.old_mode,
            "new_mode": self.new_mode,
            "old_oid": self.old_oid,
            "new_oid": self.new_oid,
            "old_path_bytes_b64": b64(old_path) if old_path is not None else None,
            "new_path_bytes_b64": b64(new_path) if new_path is not None else None,
        }

    def as_changed_file(self) -> ChangedFile:
        status = (f"{self.status}{self.score:03d}"
                  if self.score is not None else self.status)
        return ChangedFile(path=self.path, orig_path=self.orig_path,
                           status=status)


def parse_raw_z(raw: bytes) -> list[RawChange]:
    """Parse `git diff --raw -z --no-abbrev` output, fail-closed.

    Record shape: `:OLDMODE NEWMODE OLDOID NEWOID STATUS` NUL PATH NUL, with
    a second PATH NUL for R/C. A merge-style record (two leading colons) is
    not a two-endpoint diff and blocks. Trailing bytes after the final
    complete record block ("extra records")."""
    if raw == b"":
        return []
    parts = raw.split(b"\0")
    if parts[-1] != b"":
        raise _err("unterminated_final_record", tail_bytes=len(parts[-1]))
    parts.pop()
    out: list[RawChange] = []
    i = 0
    while i < len(parts):
        header = parts[i]
        if not header.startswith(b":"):
            raise _err("record_missing_colon", record_index=len(out),
                       header_bytes=len(header))
        if header.startswith(b"::"):
            raise _err("merge_record_unsupported", record_index=len(out))
        fields = header[1:].split(b" ")
        if len(fields) != 5:
            raise _err("header_field_count", record_index=len(out),
                       fields=len(fields))
        old_mode, new_mode, old_oid, new_oid, status_b = fields
        if not _MODE.match(old_mode):
            raise _err("old_mode_invalid", record_index=len(out),
                       field_bytes=len(old_mode))
        if not _MODE.match(new_mode):
            raise _err("new_mode_invalid", record_index=len(out),
                       field_bytes=len(new_mode))
        if not _OID.match(old_oid):
            raise _err("old_oid_invalid", record_index=len(out),
                       field_bytes=len(old_oid))
        if not _OID.match(new_oid):
            raise _err("new_oid_invalid", record_index=len(out),
                       field_bytes=len(new_oid))
        if not _STATUS.match(status_b):
            raise _err("status_invalid", record_index=len(out),
                       status_bytes=len(status_b))
        status = status_b[:1].decode("ascii")
        score: int | None = None
        if status in ("R", "C"):
            score = int(status_b[1:].decode("ascii"))
            if not 0 <= score <= 100:
                raise _err("score_out_of_range", record_index=len(out),
                           score=score)
        two_paths = status in ("R", "C")
        need = 2 if two_paths else 1
        if i + need >= len(parts):
            raise _err("truncated_record", record_index=len(out),
                       missing_paths=need - (len(parts) - i - 1))
        if two_paths:
            orig, path = parts[i + 1], parts[i + 2]
            i += 3
        else:
            orig, path = None, parts[i + 1]
            i += 2
        if not path or (two_paths and not orig):
            raise _err("empty_path", record_index=len(out))
        old_m = old_mode.decode("ascii")
        new_m = new_mode.decode("ascii")
        old_o = old_oid.decode("ascii")
        new_o = new_oid.decode("ascii")
        _validate_status_semantics(len(out), status, old_m, new_m,
                                   old_o, new_o)
        out.append(RawChange(
            ordinal=len(out), status=status, score=score,
            old_mode=old_m, new_mode=new_m, old_oid=old_o, new_oid=new_o,
            path=path, orig_path=orig))
    return out


def _validate_status_semantics(index: int, status: str, old_mode: str,
                               new_mode: str, old_oid: str,
                               new_oid: str) -> None:
    """Status-specific mode/OID consistency (MC1-F14).

    Git guarantees these combinations; a record that violates them is
    malformed or forged and must not become a silently-accepted atom."""
    old_zero = (old_mode == "000000" and old_oid == ZERO_OID)
    new_zero = (new_mode == "000000" and new_oid == ZERO_OID)
    if status == "A":
        if not old_zero or new_zero:
            raise _err("add_endpoint_invalid", record_index=index)
    elif status == "D":
        if old_zero or not new_zero:
            raise _err("delete_endpoint_invalid", record_index=index)
    elif status in ("M", "R", "C"):
        if old_zero or new_zero:
            raise _err("modify_endpoint_invalid", record_index=index,
                       status=status)
    elif status == "T":
        if old_zero or new_zero:
            raise _err("typechange_endpoint_invalid", record_index=index)
        if old_mode == new_mode:
            raise _err("typechange_modes_equal", record_index=index)


def raw_changes(base_sha: str, head_sha: str, *, cwd=None) -> list[RawChange]:
    """The authoritative raw universe for base..head (both proven commits).

    Rendering flags are pinned explicitly (attack finding C0) so ambient
    git config — diff.renames, diff.orderFile, core.abbrev, color —
    cannot change what two runs of the same plan see."""
    raw = run_git(["git", "diff", "--raw", "-z", "--no-abbrev",
                   *DIFF_COMMON, base_sha, head_sha],
                  cwd=cwd, operation="raw-changes")
    return parse_raw_z(raw)


def assert_matches_name_status(raw: list[RawChange],
                               name_status: list[ChangedFile]) -> None:
    """Two independent parses of the same question must agree exactly.

    The raw records are authoritative; the name-status list is the
    cross-check. Any divergence means one parser dropped or reshaped a
    record, and a dropped record is an unreviewed file."""
    if len(raw) != len(name_status):
        raise _err("universe_size_mismatch", raw_count=len(raw),
                   name_status_count=len(name_status))
    for index, (r, ns) in enumerate(zip(raw, name_status, strict=True)):
        expected_status = (f"{r.status}{r.score:03d}"
                           if r.score is not None else r.status)
        if (r.path != ns.path or r.orig_path != ns.orig_path
                or expected_status != ns.status):
            raise _err("universe_record_mismatch", record_index=index)
