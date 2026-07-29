"""B1 atom integrity: the authoritative atomization entry point.

The atom parser defines the changed-atom denominator — the set the coverage
partition is proven against. A parser that silently skips malformed content,
or a changed file that yields zero atoms and no block, shrinks that
denominator invisibly: the proof still balances, and nobody reviewed the
change. Every test here pins one way that could happen.

Written RED-FIRST against the pre-B1 foundation, per mandate: the targeted
failures were recorded before any production change.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import atoms as A  # noqa: E402
from verifier import errors as E  # noqa: E402
from verifier.canon import b64, sha256_hex  # noqa: E402

RSHA = sha256_hex(b"fixture-repository-change")


def extract(body: bytes, *, path=b"app/x.py", orig=None, status="M"):
    return A._extract_content_atoms(
        body, path=path, original_path=orig, git_status=status,
        repository_change_sha256=RSHA)


def atomize(body: bytes, *, path=b"app/x.py", orig=None, status="M", **kw):
    return A.atomize_file_change(
        body, path=path, original_path=orig, git_status=status,
        repository_change_sha256=RSHA, **kw)


def descriptor_of(result, atom) -> dict:
    return json.loads(result.contents[atom.atom_id])


def meta_kinds(result) -> list[str]:
    return [descriptor_of(result, a)["kind"] for a in result.atoms
            if a.side == A.SIDE_META]


# ---------------------------------------------------------------- counts ----


class TestHunkCountValidation:
    """The hunk header DECLARES how many old/new lines follow. The parser must
    consume exactly that many — a mismatch means the diff is malformed or
    truncated, and inferring/repairing it would invent the denominator."""

    def test_old_under_consumption_blocks(self):
        with pytest.raises(A.AtomError):
            extract(b"@@ -1,2 +1,1 @@\n-a\n+b\n")

    def test_old_over_consumption_blocks(self):
        with pytest.raises(A.AtomError):
            extract(b"@@ -1,1 +1,1 @@\n-a\n-b\n+c\n")

    def test_new_under_consumption_blocks(self):
        with pytest.raises(A.AtomError):
            extract(b"@@ -1,1 +1,2 @@\n-a\n+b\n")

    def test_new_over_consumption_blocks(self):
        with pytest.raises(A.AtomError):
            extract(b"@@ -1,1 +1,1 @@\n-a\n+b\n+c\n")

    def test_mismatch_is_detected_at_the_next_hunk(self):
        with pytest.raises(A.AtomError):
            extract(b"@@ -1,2 +1,2 @@\n-a\n+b\n@@ -5 +5 @@\n-c\n+d\n")

    def test_mismatch_is_detected_at_end_of_input(self):
        with pytest.raises(A.AtomError):
            extract(b"@@ -1,3 +1,3 @@\n a\n-b\n+c\n")

    def test_truncated_final_hunk_blocks(self):
        with pytest.raises(A.AtomError):
            extract(b"@@ -10,4 +10,4 @@\n ctx\n-gone\n")

    def test_context_increments_both_sides(self):
        atoms, _ = extract(b"@@ -1,2 +1,2 @@\n x\n-a\n+b\n")
        assert [(a.side, a.line_number) for a in atoms] == [("old", 2), ("new", 2)]

    def test_omitted_count_means_one(self):
        atoms, _ = extract(b"@@ -3 +9 @@\n-a\n+b\n")
        assert [(a.side, a.line_number) for a in atoms] == [("old", 3), ("new", 9)]

    def test_explicit_zero_old_count(self):
        atoms, _ = extract(b"@@ -1,0 +2,1 @@\n+a\n")
        assert [(a.side, a.line_number) for a in atoms] == [("new", 2)]

    def test_explicit_zero_new_count(self):
        atoms, _ = extract(b"@@ -2,1 +1,0 @@\n-a\n")
        assert [(a.side, a.line_number) for a in atoms] == [("old", 2)]


# ------------------------------------------------------------ line forms ----


class TestHunkLineForms:
    """Inside a hunk only four forms are legal: context, removal, addition,
    and the exact no-newline marker. Anything else is malformed input that
    must block, never be skipped."""

    def test_unexpected_marker_byte_blocks(self):
        with pytest.raises(A.AtomError):
            extract(b"@@ -1,1 +1,1 @@\n-a\n+b\n*weird\n")

    def test_arbitrary_backslash_line_blocks(self):
        with pytest.raises(A.AtomError):
            extract(b"@@ -1,1 +1,1 @@\n-a\n\\ nonsense marker\n+b\n")

    def test_no_newline_marker_requires_preceding_content_line(self):
        with pytest.raises(A.AtomError):
            extract(b"@@ -1,1 +1,1 @@\n\\ No newline at end of file\n-a\n+b\n")

    def test_no_newline_marker_after_both_sides_is_legal(self):
        atoms, contents = extract(
            b"@@ -1,1 +1,1 @@\n-a\n\\ No newline at end of file\n"
            b"+b\n\\ No newline at end of file\n")
        assert [a.side for a in atoms] == ["old", "new"]
        assert contents[atoms[0].atom_id] == b"a"

    def test_raw_empty_line_mid_hunk_blocks(self):
        body = b"@@ -1,1 +1,2 @@\n-a\n" + b"\n" + b"+b\n+c\n"
        with pytest.raises(A.AtomError):
            extract(body)

    def test_metadata_line_inside_hunk_blocks(self):
        with pytest.raises(A.AtomError):
            extract(b"@@ -1,1 +1,1 @@\n-a\nindex 1111111..2222222\n+b\n")

    def test_second_file_header_after_hunks_blocks(self):
        body = (b"diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                b"@@ -1,1 +1,1 @@\n-a\n+b\n"
                b"diff --git a/y.py b/y.py\n@@ -1 +1 @@\n-c\n+d\n")
        with pytest.raises(A.AtomError):
            extract(body)

    def test_second_file_header_in_preamble_blocks(self):
        with pytest.raises(A.AtomError):
            extract(b"diff --git a/x.py b/x.py\ndiff --git a/y.py b/y.py\n")

    def test_unknown_preamble_line_blocks(self):
        with pytest.raises(A.AtomError):
            extract(b"totally unexpected preamble\n@@ -1,1 +1,1 @@\n-a\n+b\n")

    def test_final_empty_split_element_is_the_trailing_newline_artifact(self):
        # A body ending in "\n" splits into [..., b""]; that artifact is legal.
        atoms, _ = extract(b"@@ -1,1 +1,1 @@\n-a\n+b\n")
        assert len(atoms) == 2

    def test_body_without_final_newline_is_legal(self):
        atoms, _ = extract(b"@@ -1,1 +1,1 @@\n-a\n+b")
        assert len(atoms) == 2


# ------------------------------------------------- atomize: obligations ----


class TestEveryChangedFileProducesAnObligation:
    """A changed file with zero atoms makes an empty set appear fully covered.
    Every ChangedFile must reach content atoms, metadata atoms, or an explicit
    blocking state — never silence."""

    def test_empty_file_added(self):
        body = b"new file mode 100644\nindex 0000000..e69de29\n"
        result = atomize(body, path=b"app/empty.py", status="A")
        assert "new_file_mode" in meta_kinds(result)
        assert result.reviewability == A.METADATA_ONLY
        assert result.blocking_code is None

    def test_empty_file_deleted(self):
        body = b"deleted file mode 100644\nindex e69de29..0000000\n"
        result = atomize(body, path=b"app/empty.py", status="D")
        assert "deleted_file_mode" in meta_kinds(result)
        assert result.reviewability == A.METADATA_ONLY

    def test_empty_symlink_added(self):
        body = b"new file mode 120000\nindex 0000000..e69de29\n"
        result = atomize(body, path=b"app/link", status="A")
        kinds = meta_kinds(result)
        assert "new_file_mode" in kinds
        atom = next(a for a in result.atoms if a.side == A.SIDE_META)
        assert descriptor_of(result, atom)["mode"] == "120000"

    def test_empty_symlink_deleted(self):
        body = b"deleted file mode 120000\nindex e69de29..0000000\n"
        result = atomize(body, path=b"app/link", status="D")
        assert "deleted_file_mode" in meta_kinds(result)

    def test_type_only_change(self):
        body = b"old mode 100644\nnew mode 120000\n"
        result = atomize(body, path=b"app/x", status="T")
        kinds = meta_kinds(result)
        assert "type_change" in kinds
        assert "mode_change" in kinds          # each DISTINCT change, one atom

    def test_mode_only_change(self):
        body = b"old mode 100644\nnew mode 100755\n"
        result = atomize(body, path=b"deploy.sh", status="M")
        assert meta_kinds(result) == ["mode_change"]
        atom = result.atoms[0]
        d = descriptor_of(result, atom)
        assert d["old_mode"] == "100644" and d["new_mode"] == "100755"

    def test_rename_plus_mode_change_yields_two_metadata_atoms(self):
        body = (b"similarity index 100%\nrename from scripts/gate.py\n"
                b"rename to docs/notes.txt\nold mode 100755\nnew mode 100644\n")
        result = atomize(body, path=b"docs/notes.txt",
                         orig=b"scripts/gate.py", status="R100")
        assert sorted(meta_kinds(result)) == ["mode_change", "rename"]

    def test_copy_yields_a_copy_obligation(self):
        body = b"similarity index 100%\ncopy from a.py\ncopy to b.py\n"
        result = atomize(body, path=b"b.py", orig=b"a.py", status="C100")
        assert "copy" in meta_kinds(result)

    def test_generic_hunkless_fallback(self):
        # No content hunks, no specialized descriptor applies: an unknown but
        # valid hunkless shape still creates exactly one canonical obligation.
        body = b"index 1111111..2222222 100644\n"
        result = atomize(body, path=b"app/x.py", status="M")
        assert meta_kinds(result) == ["hunkless_change"]
        atom = result.atoms[0]
        d = descriptor_of(result, atom)
        assert d["diff_metadata_sha256"] == sha256_hex(body)

    def test_descriptor_carries_path_hashes_never_path_bytes(self):
        # A metadata descriptor IS transmitted content, so it states each path
        # as the one canonical private identity — sha256 of the raw bytes.
        # Schema 1 carried `path_bytes_b64`, which shipped the full path of
        # every added or renamed file to the provider while the unit payload
        # around it was carefully hashing the same path (A2-F21).
        #
        # Valid paths can contain ':' and '=', so identity must never be built
        # by concatenating raw paths with delimiters either: a rename still
        # reads as a rename because the two HASHES differ.
        weird_new = b"we:ird=path.py"
        weird_old = b"al:so=weird.py"
        body = b"similarity index 100%\nrename from x\nrename to y\n"
        result = atomize(body, path=weird_new, orig=weird_old, status="R100")
        atom = next(a for a in result.atoms if a.side == A.SIDE_META)
        d = descriptor_of(result, atom)
        assert d["path_sha256"] == sha256_hex(weird_new)
        assert d["original_path_sha256"] == sha256_hex(weird_old)
        assert d["path_sha256"] != d["original_path_sha256"]

        raw = result.contents[atom.atom_id]
        assert b"path_bytes_b64" not in raw
        for path in (weird_new, weird_old):
            assert b64(path).encode() not in raw
            assert path not in raw

    def test_zero_atoms_and_no_block_raises(self):
        # The invariant itself, driven through the production check: a result
        # with no atoms and no blocking state must refuse rather than read as
        # a silently-covered nothing.
        empty = A.AtomizationResult(atoms=(), contents={},
                                    reviewability=A.METADATA_ONLY,
                                    blocking_code=None, blocking_reason=None)
        with pytest.raises(A.AtomError):
            A._assert_obligation_or_block(empty)

    @pytest.mark.parametrize("body,status", [
        (b"", "M"),
        (b"index 1111111..2222222 100644\n", "M"),
        (b"new file mode 100644\nindex 0000000..e69de29\n", "A"),
        (b"deleted file mode 100644\nindex e69de29..0000000\n", "D"),
        (b"Binary files a/i.png and b/i.png differ\n", "M"),
        (b"@@ -1,1 +1,1 @@\n-a\n+b\n", "M"),
    ])
    def test_obligation_or_block_always_exists(self, body, status):
        result = atomize(body, status=status)
        assert result.atoms or result.blocking_code is not None


# ---------------------------------------------- reviewability semantics ----


class TestReviewabilityIsSeparateFromObligation:
    """A metadata atom proves a change EXISTS; it never proves the content was
    semantically reviewed. `metadata atom exists -> content reviewed` must be
    impossible to conclude from the result.

    B1-08 closed (B2): the atom layer records the unreviewability FACT with
    no block; the control-specific block is the policy layer's decision.
    Each test here proves BOTH halves, so the end-to-end block still exists —
    it just lives where classification is known."""

    def _policy_blocks(self, content_policy):
        from verifier import contentpolicy as P
        block = P.control_block(control_bearing=True,
                                content_policy=content_policy)
        assert block is not None
        assert block[0] == E.PRIVACY_EXCLUDED_CONTROL

    def test_binary_change_records_obligation_as_fact(self):
        body = b"Binary files a/i.png and b/i.png differ\n"
        result = atomize(body, path=b"i.png", status="M")
        assert "binary_change" in meta_kinds(result)
        assert result.reviewability == A.UNREVIEWABLE_BINARY
        assert result.blocking_code is None
        from verifier import contentpolicy as P
        self._policy_blocks(P.BINARY)

    def test_git_binary_patch_is_detected(self):
        body = (b"index 1111111..2222222 100644\nGIT binary patch\n"
                b"literal 5\nMcmZQzU|?tr\n")
        result = atomize(body, path=b"i.png", status="M")
        assert result.reviewability == A.UNREVIEWABLE_BINARY
        assert result.blocking_code is None

    def test_privacy_excluded_records_obligation_as_fact(self):
        result = atomize(b"", path=b"data/dump.sql", status="M",
                         privacy_excluded=True)
        assert result.atoms                     # evidence of the change exists
        assert result.reviewability == A.PRIVACY_EXCLUDED
        assert result.blocking_code is None
        from verifier import contentpolicy as P
        self._policy_blocks(P.PRIVACY_EXCLUDED)

    def test_content_unavailable_is_a_fact_policy_blocks_control(self):
        result = atomize(b"", path=b"app/x.py", status="M",
                         content_available=False)
        assert result.atoms
        assert result.blocking_code is None
        from verifier import contentpolicy as P
        self._policy_blocks(P.UNAVAILABLE)

    def test_binary_with_mode_change_stays_unreviewable(self):
        body = (b"old mode 100644\nnew mode 100755\n"
                b"Binary files a/i.png and b/i.png differ\n")
        result = atomize(body, path=b"i.png", status="M")
        assert "mode_change" in meta_kinds(result)
        assert result.reviewability == A.UNREVIEWABLE_BINARY
        assert result.blocking_code is None

    def test_ordinary_content_is_reviewable_and_unblocked(self):
        result = atomize(b"@@ -1,1 +1,1 @@\n-a\n+b\n")
        assert result.reviewability == A.REVIEWABLE
        assert result.blocking_code is None
        assert len(result.atoms) == 2

    def test_pure_rename_is_metadata_only_not_reviewable(self):
        body = b"similarity index 100%\nrename from a.py\nrename to b.py\n"
        result = atomize(body, path=b"b.py", orig=b"a.py", status="R100")
        assert result.reviewability == A.METADATA_ONLY


# ------------------------------------------------------- atom identity ----


class TestAtomIdentityIsPatchSpecific:
    """Atom identity is intentionally patch-specific because
    repository_change_sha256 participates in atom_id. patch_ordinal is
    excluded only so atom identity does not depend on planner or unit
    ordering within the same exact repository change."""

    BODY = b"@@ -1,1 +1,1 @@\n-a\n+b\n"

    def test_same_repository_change_sha_gives_same_id(self):
        first, _ = extract(self.BODY)
        second, _ = extract(self.BODY)
        assert [a.atom_id for a in first] == [a.atom_id for a in second]

    def test_different_repository_change_sha_gives_different_id(self):
        first, _ = extract(self.BODY)
        other, _ = A._extract_content_atoms(
            self.BODY, path=b"app/x.py", original_path=None, git_status="M",
            repository_change_sha256=sha256_hex(b"other"))
        assert first[0].atom_id != other[0].atom_id

    def test_patch_ordinal_does_not_enter_identity(self):
        atoms, _ = extract(self.BODY)
        stamped = A.assign_patch_ordinals(list(reversed(atoms)))
        assert {a.atom_id for a in stamped} == {a.atom_id for a in atoms}

    def test_same_content_at_different_positions_differs(self):
        atoms, _ = extract(b"@@ -1,0 +1,2 @@\n+same\n+same\n")
        assert atoms[0].atom_id != atoms[1].atom_id

    def test_old_and_new_sides_differ(self):
        atoms, _ = extract(b"@@ -1,1 +1,1 @@\n-v\n+v\n")
        assert atoms[0].atom_id != atoms[1].atom_id

    def test_different_metadata_kinds_differ(self):
        body = (b"similarity index 100%\nrename from a.py\nrename to b.py\n"
                b"old mode 100755\nnew mode 100644\n")
        result = atomize(body, path=b"b.py", orig=b"a.py", status="R100")
        metas = [a for a in result.atoms if a.side == A.SIDE_META]
        assert len(metas) == 2
        assert metas[0].atom_id != metas[1].atom_id
