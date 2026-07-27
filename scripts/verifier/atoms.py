"""Changed atoms — the authoritative coverage primitive.

A review is complete when every CHANGED ATOM belongs to exactly one review
unit. Line ranges and Markdown/Python structure are presentation context only;
they never establish coverage.

Why atoms and not inclusive line ranges: adjacent git hunks and shared context
lines make ranges overlap and abut in ways that produce both false overlap and
false coverage. An atom is one changed line on one side of the diff, bound to
its exact bytes, so "covered exactly once" is a set partition question with no
interpretation left in it.

An added line and a removed line at the same position are DISTINCT atoms: a
modification is a deletion plus an addition. Context lines are never atoms —
a unit that merely *shows* a line has not reviewed a change to it.

The parser here defines the changed-atom DENOMINATOR — the set the coverage
partition is proven against. It validates the diff EXACTLY and fails closed on
anything malformed: whatever a parser skips silently disappears from the
denominator, the partition still balances, and nobody reviewed the skipped
change. The preamble is a typed state machine (B1-06), not a prefix filter,
and ONE parse drives both content and metadata atomization so the two can
never disagree about the same bytes.

`atomize_file_change` is the ONLY public production entry point (B1-02); the
low-level parser and metadata helpers are private so no caller can extract
content, forget the metadata half, and prove an empty set covered. Error
messages carry repository-owned categories, coordinates, lengths and hashes —
never raw diff bytes (B1-05): a failure path must not become an uncontrolled
text channel.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .canon import b64, canonical_json, digest, num, sha256_hex
from .errors import (
    DIFF_PARSE_FAILURE,
    UNRECOGNIZED_HUNKLESS_CHANGE,
)

__all__ = [
    "METADATA_ONLY",
    "PRIVACY_EXCLUDED",
    "REVIEWABLE",
    "SIDE_META",
    "SIDE_NEW",
    "SIDE_OLD",
    "UNREVIEWABLE_BINARY",
    "UNREVIEWABLE_METADATA",
    "AtomError",
    "AtomizationResult",
    "ChangedAtom",
    "assign_patch_ordinals",
    "atomize_file_change",
    "is_binary_diff",
    "path_key",
]

SIDE_OLD = "old"
SIDE_NEW = "new"
# A change with no content hunks — a rename, a mode flip, a type change, an
# empty file, a binary blob — still changes the repository. Without a metadata
# atom it would yield an EMPTY atom set, and an empty set is trivially "fully
# covered".
SIDE_META = "meta"

# Reviewability of a file change — deliberately SEPARATE from whether atoms
# exist. A metadata atom proves a change EXISTS, never that its content was
# semantically reviewed, so `metadata atom exists -> content reviewed` must be
# impossible to conclude from an AtomizationResult.
REVIEWABLE = "reviewable"
METADATA_ONLY = "metadata_only"            # a specialized descriptor fully
#                                            STATES the change (rename, mode,
#                                            empty add/delete, type change)
UNREVIEWABLE_BINARY = "unreviewable_binary"
UNREVIEWABLE_METADATA = "unreviewable_metadata"  # obligation recorded, but the
#                                            descriptor is a hash marker, not a
#                                            reviewable statement (B1-01)
PRIVACY_EXCLUDED = "privacy_excluded"

import re  # noqa: E402  (grouped after constants for readability)

# @@ -old_start[,old_count] +new_start[,new_count] @@ [section heading]
# An omitted count means 1; an explicit ",0" means zero.
_HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_NO_NEWLINE = b"\\ No newline at end of file"
_MODE_DIGITS = re.compile(rb"^\d{6}$")


class AtomError(RuntimeError):
    """A diff could not be turned into a provable set of changed atoms.

    Messages are built by `_err`: repository-owned category tokens plus
    structural facts only — never raw diff bytes, never decoded excerpts."""


def _err(category: str, **facts) -> AtomError:
    parts = [category]
    parts += [f"{key}={value}" for key, value in facts.items()]
    return AtomError(" ".join(parts))


def _line_facts(raw: bytes) -> dict:
    """Safe structural facts about a malformed line: length + hash + first
    marker byte in hex. Enough to locate and reproduce; nothing to leak."""
    return {
        "line_bytes": len(raw),
        "line_sha256": sha256_hex(raw),
        "marker_byte": f"0x{raw[0]:02x}" if raw else "none",
    }


@dataclass(frozen=True)
class ChangedAtom:
    """One changed line (or one metadata change), on one side, with its exact
    bytes.

    `sequence` is the dense, deterministic order within the file's final atom
    list: content atoms in hunk order, then metadata atoms in canonical
    detected order, then the generic fallback (B1-04). `patch_ordinal` is the
    canonical order across the whole patch, stamped later."""

    atom_id: str
    path: bytes                     # the side-appropriate path (see path_key)
    original_path: bytes | None
    git_status: str
    side: str                       # "old" | "new" | "meta"
    line_number: int                # 0 for metadata atoms
    content_sha256: str
    hunk_id: str
    sequence: int
    patch_ordinal: int = -1

    def to_record(self) -> dict:
        return {
            "atom_id": self.atom_id,
            "path_bytes_b64": b64(self.path),
            "original_path_bytes_b64": (
                b64(self.original_path) if self.original_path is not None else None),
            "git_status": self.git_status,
            "side": self.side,
            "line_number": self.line_number,
            "content_sha256": self.content_sha256,
            "hunk_id": self.hunk_id,
            "sequence": self.sequence,
            "patch_ordinal": self.patch_ordinal,
        }


@dataclass(frozen=True)
class AtomizationResult:
    """Everything the planner needs to know about one file change.

    `contents` is an IMMUTABLE mapping validated against the atoms (B1-03):
    exactly one entry per atom id, each hashing to that atom's content_sha256,
    so the model payload can never diverge from the identity the coverage
    proof was computed over.

    Facts vs policy (B1-08, closed in B2): this layer records FACTS —
    reviewability, atoms, and blocking codes for PARSE-INTEGRITY failures
    only (an empty body for a reported change, an unrecognised hunkless
    shape). Whether an unreviewable change BLOCKS is a policy question that
    depends on classification, which this layer does not know; that decision
    lives in verifier.contentpolicy.control_block."""

    atoms: tuple[ChangedAtom, ...]
    contents: Mapping[str, bytes]
    reviewability: str
    blocking_code: str | None
    blocking_reason: str | None


def path_key(path: bytes, original_path: bytes | None, side: str) -> bytes:
    """For a rename or copy the OLD side belongs to the original path and the
    NEW side to the destination; metadata atoms bind to the destination (their
    descriptor carries both)."""
    if side == SIDE_OLD and original_path is not None:
        return original_path
    return path


def _hunk_id(path: bytes, header: bytes, index: int) -> str:
    return digest(b"hunk-v1", path, header, num(index))


def _atom_id(repository_change_sha256: str, key: bytes, side: str,
             line_number: int, hunk_id: str, content_sha: str) -> str:
    # Atom identity is intentionally patch-specific because
    # repository_change_sha256 participates in atom_id. patch_ordinal is
    # excluded only so atom identity does not depend on planner or unit
    # ordering within the same exact repository change.
    return digest(
        b"atom-v1",
        repository_change_sha256.encode("ascii"),
        key,
        side.encode("ascii"),
        num(line_number),
        hunk_id.encode("ascii"),
        content_sha.encode("ascii"),
    )


def is_binary_diff(diff_body: bytes) -> bool:
    """git emits a `Binary files ... differ` stanza or a `GIT binary patch`
    section instead of hunks."""
    return (b"\nBinary files " in diff_body
            or diff_body.startswith(b"Binary files ")
            or b"\nGIT binary patch" in diff_body
            or diff_body.startswith(b"GIT binary patch"))


# ------------------------------------------------------ preamble parsing ----


@dataclass
class _Preamble:
    """The TYPED result of parsing a per-file diff's header records (B1-06).

    One parse populates this; both content handling and metadata atomization
    read it. There is no second regex scan that could disagree with the
    parser, and no known record can be silently discarded — every recognised
    record lands in a field, every unrecognised one raises."""

    diff_header_seen: bool = False
    index_seen: bool = False
    old_file_header_seen: bool = False
    new_file_header_seen: bool = False
    old_mode: str | None = None
    new_mode: str | None = None
    new_file_mode: str | None = None
    deleted_file_mode: str | None = None
    similarity_seen: bool = False
    dissimilarity_seen: bool = False
    rename_from_seen: bool = False
    rename_to_seen: bool = False
    copy_from_seen: bool = False
    copy_to_seen: bool = False
    binary_kind: str | None = None      # None | "binary_files" | "binary_patch"
    any_record_seen: bool = False

    def finalize(self, git_status: str) -> None:
        """Cross-record consistency, applied once the preamble is complete
        (first hunk, or end of body). Inconsistent known metadata is malformed
        input and blocks — a silently dropped half-pair would vanish from the
        obligation denominator."""
        if (self.old_mode is None) != (self.new_mode is None):
            raise _err("unpaired_mode_records",
                       has_old_mode=self.old_mode is not None,
                       has_new_mode=self.new_mode is not None)
        if self.rename_from_seen != self.rename_to_seen:
            raise _err("unpaired_rename_records",
                       has_rename_from=self.rename_from_seen,
                       has_rename_to=self.rename_to_seen)
        if self.copy_from_seen != self.copy_to_seen:
            raise _err("unpaired_copy_records",
                       has_copy_from=self.copy_from_seen,
                       has_copy_to=self.copy_to_seen)
        if self.old_file_header_seen != self.new_file_header_seen:
            raise _err("unpaired_file_headers",
                       has_old_header=self.old_file_header_seen,
                       has_new_header=self.new_file_header_seen)
        if self.rename_from_seen and git_status[:1] != "R":
            raise _err("rename_records_status_mismatch", git_status=git_status)
        if self.copy_from_seen and git_status[:1] != "C":
            raise _err("copy_records_status_mismatch", git_status=git_status)
        if self.new_file_mode is not None and self.deleted_file_mode is not None:
            raise _err("conflicting_new_and_deleted_file_modes")
        if self.new_file_mode is not None and self.old_mode is not None:
            raise _err("conflicting_new_file_and_mode_change_records")


def _mode_of(raw: bytes, prefix: bytes) -> str:
    value = raw[len(prefix):]
    if not _MODE_DIGITS.fullmatch(value):
        raise _err("malformed_mode_record", **_line_facts(raw))
    return value.decode("ascii")


# ---------------------------------------------------------- diff parsing ----


class _HunkState:
    """Exact bookkeeping for one hunk. The header DECLARES how many old/new
    lines follow; consumption is validated exactly, and nothing malformed is
    inferred or repaired — a repaired count is an invented denominator."""

    __slots__ = ("hunk_id", "old_no", "new_no", "expected_old", "expected_new",
                 "consumed_old", "consumed_new", "last_kind", "header")

    def __init__(self, hunk_id: str, header: bytes,
                 old_start: int, expected_old: int,
                 new_start: int, expected_new: int):
        self.hunk_id = hunk_id
        self.header = header
        self.old_no = old_start
        self.new_no = new_start
        self.expected_old = expected_old
        self.expected_new = expected_new
        self.consumed_old = 0
        self.consumed_new = 0
        self.last_kind: str | None = None   # "content" | "nonl" | None

    def _header_facts(self) -> dict:
        return {"header_bytes": len(self.header),
                "header_sha256": sha256_hex(self.header)}

    def close(self) -> None:
        if (self.consumed_old != self.expected_old
                or self.consumed_new != self.expected_new):
            raise _err("hunk_line_count_mismatch",
                       old_expected=self.expected_old,
                       old_consumed=self.consumed_old,
                       new_expected=self.expected_new,
                       new_consumed=self.consumed_new,
                       **self._header_facts())

    def bump(self, side: str) -> None:
        if side in ("old", "both"):
            self.consumed_old += 1
            if self.consumed_old > self.expected_old:
                raise _err("hunk_old_lines_overconsumed",
                           old_expected=self.expected_old,
                           old_consumed=self.consumed_old,
                           **self._header_facts())
        if side in ("new", "both"):
            self.consumed_new += 1
            if self.consumed_new > self.expected_new:
                raise _err("hunk_new_lines_overconsumed",
                           new_expected=self.expected_new,
                           new_consumed=self.consumed_new,
                           **self._header_facts())


@dataclass
class _ParsedDiff:
    preamble: _Preamble
    atoms: list[ChangedAtom] = field(default_factory=list)
    contents: dict[str, bytes] = field(default_factory=dict)
    hunk_count: int = 0


def _parse_file_diff(diff_body: bytes, *, path: bytes,
                     original_path: bytes | None, git_status: str,
                     repository_change_sha256: str) -> _ParsedDiff:
    """The ONE parse of a per-file unified diff: typed preamble state machine
    plus exact hunk state machine, all on raw bytes with no normalisation."""
    parsed = _ParsedDiff(preamble=_Preamble())
    pre = parsed.preamble
    lines = diff_body.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()                 # trailing artifact of the final newline
    hunk: _HunkState | None = None
    in_binary_payload = False
    binary_stanza_done = False

    for raw in lines:
        if raw.startswith(b"diff --git "):
            # Legal only as the very first record of the body.
            if pre.any_record_seen or hunk is not None:
                raise _err("second_file_header_record", **_line_facts(raw))
            pre.diff_header_seen = True
            pre.any_record_seen = True
            continue

        if raw.startswith(b"@@"):
            if pre.binary_kind is not None:
                raise _err("hunks_after_binary_stanza")
            m = _HUNK_RE.match(raw)
            if not m:
                raise _err("unparseable_hunk_header", **_line_facts(raw))
            if hunk is None:
                pre.finalize(git_status)
            else:
                hunk.close()
            parsed.hunk_count += 1
            hunk = _HunkState(
                _hunk_id(path, raw, parsed.hunk_count - 1), raw,
                old_start=int(m.group(1)),
                expected_old=int(m.group(2)) if m.group(2) is not None else 1,
                new_start=int(m.group(3)),
                expected_new=int(m.group(4)) if m.group(4) is not None else 1,
            )
            continue

        if hunk is not None:
            _consume_hunk_line(raw, hunk, parsed, path=path,
                               original_path=original_path,
                               git_status=git_status,
                               repository_change_sha256=repository_change_sha256)
            continue

        # ---- preamble records ----
        if in_binary_payload:
            # Inside a GIT binary patch, payload lines (base85 data, literal/
            # delta section markers, the terminating blank) are opaque and
            # legal; file/hunk records are handled above and still block.
            continue
        pre.any_record_seen = True
        if raw == b"":
            raise _err("empty_line_in_preamble")
        if raw.startswith(b"index "):
            if pre.index_seen:
                raise _err("duplicate_index_record")
            pre.index_seen = True
        elif raw.startswith(b"--- "):
            if pre.old_file_header_seen:
                raise _err("duplicate_old_file_header")
            if pre.new_file_header_seen:
                raise _err("file_headers_wrong_order")
            pre.old_file_header_seen = True
        elif raw.startswith(b"+++ "):
            if pre.new_file_header_seen:
                raise _err("duplicate_new_file_header")
            if not pre.old_file_header_seen:
                raise _err("file_headers_wrong_order")
            pre.new_file_header_seen = True
        elif raw.startswith(b"old mode "):
            if pre.old_mode is not None:
                raise _err("duplicate_old_mode_record")
            pre.old_mode = _mode_of(raw, b"old mode ")
        elif raw.startswith(b"new mode "):
            if pre.new_mode is not None:
                raise _err("duplicate_new_mode_record")
            pre.new_mode = _mode_of(raw, b"new mode ")
        elif raw.startswith(b"new file mode "):
            if pre.new_file_mode is not None:
                raise _err("duplicate_new_file_mode_record")
            pre.new_file_mode = _mode_of(raw, b"new file mode ")
        elif raw.startswith(b"deleted file mode "):
            if pre.deleted_file_mode is not None:
                raise _err("duplicate_deleted_file_mode_record")
            pre.deleted_file_mode = _mode_of(raw, b"deleted file mode ")
        elif raw.startswith(b"similarity index "):
            if pre.similarity_seen:
                raise _err("duplicate_similarity_record")
            pre.similarity_seen = True
        elif raw.startswith(b"dissimilarity index "):
            if pre.dissimilarity_seen:
                raise _err("duplicate_dissimilarity_record")
            pre.dissimilarity_seen = True
        elif raw.startswith(b"rename from "):
            if pre.rename_from_seen:
                raise _err("duplicate_rename_from_record")
            pre.rename_from_seen = True
        elif raw.startswith(b"rename to "):
            if pre.rename_to_seen:
                raise _err("duplicate_rename_to_record")
            pre.rename_to_seen = True
        elif raw.startswith(b"copy from "):
            if pre.copy_from_seen:
                raise _err("duplicate_copy_from_record")
            pre.copy_from_seen = True
        elif raw.startswith(b"copy to "):
            if pre.copy_to_seen:
                raise _err("duplicate_copy_to_record")
            pre.copy_to_seen = True
        elif raw.startswith(b"Binary files "):
            if pre.binary_kind is not None:
                raise _err("duplicate_binary_stanza")
            pre.binary_kind = "binary_files"
            binary_stanza_done = True
        elif raw == b"GIT binary patch":
            if pre.binary_kind is not None:
                raise _err("duplicate_binary_stanza")
            pre.binary_kind = "binary_patch"
            in_binary_payload = True
        elif binary_stanza_done:
            raise _err("record_after_binary_stanza", **_line_facts(raw))
        else:
            # Includes literal/delta outside a GIT binary patch: payload
            # markers are legal only inside the binary-payload state.
            raise _err("unrecognized_preamble_record", **_line_facts(raw))

    if hunk is not None:
        hunk.close()
    else:
        pre.finalize(git_status)
    if parsed.hunk_count > 0 and not parsed.atoms:
        raise _err("context_only_hunks", hunk_count=parsed.hunk_count)
    _assert_unique(parsed.atoms)
    return parsed


def _consume_hunk_line(raw: bytes, hunk: _HunkState, parsed: _ParsedDiff, *,
                       path: bytes, original_path: bytes | None,
                       git_status: str, repository_change_sha256: str) -> None:
    """Inside a hunk exactly four forms are legal: context, removal, addition,
    and the exact no-newline marker (which requires a preceding content line
    and increments neither counter)."""
    if raw == _NO_NEWLINE:
        if hunk.last_kind != "content":
            raise _err("no_newline_marker_without_preceding_content")
        hunk.last_kind = "nonl"
        return
    if raw == b"":
        raise _err("empty_line_inside_hunk")
    marker, content = raw[:1], raw[1:]
    if marker == b"\\":
        raise _err("unrecognized_backslash_line", **_line_facts(raw))
    if marker == b" ":
        hunk.bump("both")
        hunk.old_no += 1
        hunk.new_no += 1
        hunk.last_kind = "content"
        return
    if marker == b"-":
        hunk.bump("old")
        side, line_no = SIDE_OLD, hunk.old_no
        hunk.old_no += 1
    elif marker == b"+":
        hunk.bump("new")
        side, line_no = SIDE_NEW, hunk.new_no
        hunk.new_no += 1
    else:
        raise _err("unexpected_hunk_line_form", **_line_facts(raw))
    hunk.last_kind = "content"
    key = path_key(path, original_path, side)
    content_sha = sha256_hex(content)
    atom_id = _atom_id(repository_change_sha256, key, side, line_no,
                       hunk.hunk_id, content_sha)
    parsed.atoms.append(ChangedAtom(
        atom_id=atom_id, path=key, original_path=original_path,
        git_status=git_status, side=side, line_number=line_no,
        content_sha256=content_sha, hunk_id=hunk.hunk_id,
        sequence=len(parsed.atoms)))
    parsed.contents[atom_id] = content


def _assert_unique(atoms: list[ChangedAtom]) -> None:
    seen: set[str] = set()
    for a in atoms:
        if a.atom_id in seen:
            raise _err("duplicate_atom_id", atom_id_prefix=a.atom_id[:16],
                       side=a.side, line_number=a.line_number)
        seen.add(a.atom_id)


# --------------------------------------------------- test-facing privates ----


def _extract_content_atoms(diff_body: bytes, *, path: bytes,
                           original_path: bytes | None, git_status: str,
                           repository_change_sha256: str,
                           ) -> tuple[list[ChangedAtom], dict[str, bytes]]:
    """PRIVATE content parser. Fails closed on the two shapes a forgetful
    caller could mistake for 'nothing changed' (B1-02): an EMPTY body and a
    BINARY body both raise instead of returning an empty success. Production
    code must use atomize_file_change."""
    if not diff_body.strip():
        raise _err("empty_diff_body_has_no_content_atoms")
    parsed = _parse_file_diff(
        diff_body, path=path, original_path=original_path,
        git_status=git_status,
        repository_change_sha256=repository_change_sha256)
    if parsed.preamble.binary_kind is not None:
        raise _err("binary_body_has_no_line_reviewable_content",
                   binary_kind=parsed.preamble.binary_kind)
    return parsed.atoms, parsed.contents


def _metadata_atoms(diff_body: bytes, *, path: bytes,
                    original_path: bytes | None, git_status: str,
                    repository_change_sha256: str,
                    ) -> tuple[list[ChangedAtom], dict[str, bytes]]:
    """PRIVATE metadata derivation for tests: parses (the SAME parse the
    entry point uses), then derives — never an independent regex scan."""
    parsed = _parse_file_diff(
        diff_body, path=path, original_path=original_path,
        git_status=git_status,
        repository_change_sha256=repository_change_sha256)
    return _metadata_from_preamble(
        parsed.preamble, path=path, original_path=original_path,
        git_status=git_status,
        repository_change_sha256=repository_change_sha256)


# ------------------------------------------------------- metadata atoms ----


def _descriptor(kind: str, path: bytes, original_path: bytes | None,
                git_status: str, extra: dict) -> bytes:
    """Canonical JSON with raw byte paths as Base64. Identities are NEVER
    built by concatenating raw paths with delimiters (':', '='), because
    valid paths can contain those very bytes."""
    return canonical_json({
        "schema_version": 1,
        "kind": kind,
        "git_status": git_status,
        "path_bytes_b64": b64(path),
        "original_path_bytes_b64": (
            b64(original_path) if original_path is not None else None),
        **extra,
    })


def _meta_atom(kind: str, descriptor: bytes, *, path: bytes,
               original_path: bytes | None, git_status: str,
               repository_change_sha256: str, index: int) -> ChangedAtom:
    hunk_id = _hunk_id(path, b"@@meta:" + kind.encode("ascii"), index)
    content_sha = sha256_hex(descriptor)
    key = path_key(path, original_path, SIDE_META)
    return ChangedAtom(
        atom_id=_atom_id(repository_change_sha256, key, SIDE_META, 0,
                         hunk_id, content_sha),
        path=key, original_path=original_path, git_status=git_status,
        side=SIDE_META, line_number=0, content_sha256=content_sha,
        hunk_id=hunk_id, sequence=index)


def _metadata_from_preamble(pre: _Preamble, *, path: bytes,
                            original_path: bytes | None, git_status: str,
                            repository_change_sha256: str,
                            ) -> tuple[list[ChangedAtom], dict[str, bytes]]:
    """Each DISTINCT detected metadata change yields one metadata atom —
    not "one per file": a rename can also flip the mode, and each distinct
    change is its own reviewable obligation. Driven ENTIRELY by the single
    typed preamble parse plus the git status (B1-06)."""
    detected: list[tuple[str, dict]] = []
    if git_status[:1] == "R" and original_path is not None:
        detected.append(("rename", {}))
    elif git_status[:1] == "C" and original_path is not None:
        detected.append(("copy", {}))
    if pre.old_mode is not None and pre.new_mode is not None:
        detected.append(("mode_change",
                         {"old_mode": pre.old_mode, "new_mode": pre.new_mode}))
    if pre.new_file_mode is not None:
        detected.append(("new_file_mode", {"mode": pre.new_file_mode}))
    if pre.deleted_file_mode is not None:
        detected.append(("deleted_file_mode", {"mode": pre.deleted_file_mode}))
    if git_status[:1] == "T":
        detected.append(("type_change", {}))
    if pre.binary_kind is not None:
        detected.append(("binary_change",
                         {"note": "content-not-line-reviewable",
                          "binary_kind": pre.binary_kind}))

    atoms: list[ChangedAtom] = []
    contents: dict[str, bytes] = {}
    for index, (kind, extra) in enumerate(detected):
        desc = _descriptor(kind, path, original_path, git_status, extra)
        atom = _meta_atom(kind, desc, path=path, original_path=original_path,
                          git_status=git_status,
                          repository_change_sha256=repository_change_sha256,
                          index=index)
        atoms.append(atom)
        contents[atom.atom_id] = desc
    _assert_unique(atoms)
    return atoms, contents


def _generic_hunkless_atoms(diff_body: bytes, *, path: bytes,
                            original_path: bytes | None, git_status: str,
                            repository_change_sha256: str,
                            ) -> tuple[list[ChangedAtom], dict[str, bytes]]:
    """The last-resort OBLIGATION for a hunkless change no specialized
    descriptor recognised. The descriptor is a hash marker so the change
    cannot vanish from the denominator — but a hash is not a reviewable
    statement of what happened, which is why the caller marks this
    unreviewable and blocking (B1-01), never METADATA_ONLY."""
    desc = _descriptor("hunkless_change", path, original_path, git_status,
                       {"diff_metadata_sha256": sha256_hex(diff_body)})
    atom = _meta_atom("hunkless_change", desc, path=path,
                      original_path=original_path, git_status=git_status,
                      repository_change_sha256=repository_change_sha256,
                      index=0)
    return [atom], {atom.atom_id: desc}


# --------------------------------------------------------- invariants ----


def _validated_contents(atoms: list[ChangedAtom],
                        contents: dict[str, bytes]) -> Mapping[str, bytes]:
    """B1-03: the binding between atoms and their bytes is proven at creation
    and frozen. Exactly one content entry per atom id; every entry hashes to
    its atom's content_sha256; the returned mapping is immutable, so a later
    consumer cannot make the model payload diverge from the identity the
    coverage proof was computed over."""
    atom_ids = {a.atom_id for a in atoms}
    if set(contents) != atom_ids:
        missing = sorted(atom_ids - set(contents))
        extra = sorted(set(contents) - atom_ids)
        raise _err("content_binding_key_mismatch",
                   missing_count=len(missing), extra_count=len(extra),
                   first_missing=(missing[0][:16] if missing else "none"),
                   first_extra=(extra[0][:16] if extra else "none"))
    for atom in atoms:
        if sha256_hex(contents[atom.atom_id]) != atom.content_sha256:
            raise _err("content_binding_hash_mismatch",
                       atom_id_prefix=atom.atom_id[:16], side=atom.side,
                       line_number=atom.line_number)
    return MappingProxyType(dict(contents))


def _assert_obligation_or_block(result: AtomizationResult) -> None:
    """The B1 invariant: a changed file can never atomize to silence."""
    if not result.atoms and result.blocking_code is None:
        raise AtomError(
            "changed file produced neither review obligations nor a blocking "
            "state")


# ------------------------------------------------ authoritative entry ----


def atomize_file_change(diff_body: bytes, *, path: bytes,
                        original_path: bytes | None, git_status: str,
                        repository_change_sha256: str,
                        content_available: bool = True,
                        privacy_excluded: bool = False) -> AtomizationResult:
    """The ONE public entry point that turns a file change into review
    obligations. Always performs, in order: exact parsing (typed preamble +
    hunk state machine, unless content is withheld), metadata obligations from
    the SAME parse, the generic hunkless fallback, reviewability
    classification, sequence restamping, content-binding validation, and the
    obligation-or-block invariant.

    Semantics (B1-01): a specialized descriptor that fully STATES the change
    (rename, copy, mode change, empty add/delete, type change) is
    METADATA_ONLY and unblocked at this layer. Everything the descriptor
    cannot state — an empty body, an unknown hunkless shape — records its
    obligation AND blocks, because a hash marker is not semantic review."""
    if privacy_excluded or not content_available:
        # Content withheld: evidence of the change is still created from the
        # status (rename/copy/type are status-driven), but nothing is parsed
        # and nothing is reviewed. FACT only (B1-08 closed): whether an
        # unreviewed change blocks depends on classification, which the
        # policy layer (contentpolicy.control_block) decides.
        meta, meta_contents = _metadata_from_preamble(
            _Preamble(), path=path, original_path=original_path,
            git_status=git_status,
            repository_change_sha256=repository_change_sha256)
        content_atoms: list[ChangedAtom] = []
        contents: dict[str, bytes] = {}
        reviewability = PRIVACY_EXCLUDED
        blocking_code: str | None = None
        blocking_reason: str | None = None
    elif not diff_body.strip():
        # An empty body for a REPORTED change is malformed input, not a
        # reviewable no-op: git said the file changed and gave us nothing.
        meta, meta_contents = [], {}
        content_atoms, contents = [], {}
        reviewability = UNREVIEWABLE_METADATA
        blocking_code = DIFF_PARSE_FAILURE
        blocking_reason = (
            "empty diff body for a reported change — there is nothing to "
            "review and nothing stated; refusing to synthesize an unblocked "
            "no-op from empty bytes")
    else:
        parsed = _parse_file_diff(
            diff_body, path=path, original_path=original_path,
            git_status=git_status,
            repository_change_sha256=repository_change_sha256)
        meta, meta_contents = _metadata_from_preamble(
            parsed.preamble, path=path, original_path=original_path,
            git_status=git_status,
            repository_change_sha256=repository_change_sha256)
        content_atoms, contents = parsed.atoms, parsed.contents
        if parsed.preamble.binary_kind is not None:
            # FACT only (B1-08 closed): the policy layer decides whether an
            # unreviewable binary blocks, because that depends on whether
            # the file is control-bearing.
            reviewability = UNREVIEWABLE_BINARY
            blocking_code = None
            blocking_reason = None
        elif content_atoms:
            reviewability = REVIEWABLE
            blocking_code = None
            blocking_reason = None
        elif meta:
            # Specialized descriptors fully state the change.
            reviewability = METADATA_ONLY
            blocking_code = None
            blocking_reason = None
        else:
            reviewability = UNREVIEWABLE_METADATA
            blocking_code = UNRECOGNIZED_HUNKLESS_CHANGE
            blocking_reason = (
                "hunkless change with no recognised metadata descriptor; the "
                "hash-marker obligation records THAT it happened, not WHAT "
                "happened, so it is not reviewable and must not pass as "
                "metadata_only")

    # Order (documented, B1-04): content atoms in hunk order, then metadata
    # atoms in canonical detected order, then the generic fallback.
    all_atoms = list(content_atoms) + list(meta)
    all_contents = {**contents, **meta_contents}
    if not all_atoms:
        fallback, fallback_contents = _generic_hunkless_atoms(
            diff_body, path=path, original_path=original_path,
            git_status=git_status,
            repository_change_sha256=repository_change_sha256)
        all_atoms.extend(fallback)
        all_contents.update(fallback_contents)

    # Dense per-file sequence restamp (B1-04). atom_id does not include
    # sequence, so restamping never changes identity.
    all_atoms = [dataclasses.replace(a, sequence=i)
                 for i, a in enumerate(all_atoms)]
    _assert_unique(all_atoms)

    result = AtomizationResult(
        atoms=tuple(all_atoms),
        contents=_validated_contents(all_atoms, all_contents),
        reviewability=reviewability,
        blocking_code=blocking_code,
        blocking_reason=blocking_reason,
    )
    _assert_obligation_or_block(result)
    return result


# ------------------------------------------------------- patch ordinals ----


def assign_patch_ordinals(atoms: list[ChangedAtom]) -> list[ChangedAtom]:
    """Stamp the canonical whole-patch order onto every atom.

    The ordinal is assigned in the order the caller supplies — git's own file
    order, then each file's final atom order — so it is a property of the
    PATCH, not of any one file. Unit ordering later sorts on
    (min ordinal, max ordinal, unit hash) rather than on `side`, because a
    modified unit legitimately contains both old and new atoms and `side`
    cannot order it.

    Atom identity is intentionally patch-specific because
    repository_change_sha256 participates in atom_id. patch_ordinal is
    excluded only so atom identity does not depend on planner or unit
    ordering within the same exact repository change."""
    return [dataclasses.replace(a, patch_ordinal=index)
            for index, a in enumerate(atoms)]
