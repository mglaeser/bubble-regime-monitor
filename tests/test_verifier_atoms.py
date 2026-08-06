"""Changed-atom extraction: the coverage primitive must be exactly right.

Every case here is one the planner would otherwise get wrong in a way that
reads as success: a header counted as content, content mistaken for a header,
a change with no hunks reporting an empty (hence "fully covered") atom set.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import atoms as A  # noqa: E402
from verifier.canon import sha256_hex  # noqa: E402

PSHA = sha256_hex(b"fixture-patch")


def extract(body: bytes, *, path=b"app/x.py", orig=None, status="M"):
    return A._extract_content_atoms(body, path=path, original_path=orig,
                     git_status=status, repository_change_sha256=PSHA)


def sides(atoms):
    return [(a.side, a.line_number, a.content_sha256) for a in atoms]


class TestHeadersAreNeverAtoms:
    def test_file_headers_are_not_content(self):
        body = (b"diff --git a/app/x.py b/app/x.py\n"
                b"index 1111111..2222222 100644\n"
                b"--- a/app/x.py\n"
                b"+++ b/app/x.py\n"
                b"@@ -1,2 +1,2 @@\n"
                b" ctx\n"
                b"-gone\n"
                b"+here\n")
        got, _ = extract(body)
        assert len(got) == 2, sides(got)
        assert [(a.side, a.line_number) for a in got] == [("old", 2), ("new", 2)]

    def test_new_file_dev_null_header_is_not_content(self):
        body = (b"diff --git a/n.py b/n.py\n"
                b"new file mode 100644\n"
                b"index 0000000..3333333\n"
                b"--- /dev/null\n"
                b"+++ b/n.py\n"
                b"@@ -0,0 +1,1 @@\n"
                b"+only\n")
        got, _ = extract(body, status="A")
        assert len(got) == 1
        assert got[0].side == "new" and got[0].line_number == 1

    def test_content_that_itself_begins_with_dashes_is_an_atom(self):
        # A removed line whose text is "--- not a header" arrives as
        # b"----- not a header": one marker byte, then the content.
        body = (b"@@ -1,1 +1,1 @@\n"
                b"---- three dashes\n"
                b"++++ three plusses\n")
        got, contents = extract(body)
        assert len(got) == 2
        assert contents[got[0].atom_id] == b"--- three dashes"
        assert contents[got[1].atom_id] == b"+++ three plusses"

    def test_yaml_document_marker_content_is_an_atom(self):
        # An ADDED YAML document marker is emitted as b"+---": one marker byte
        # then the literal content b"---". It must not be mistaken for the
        # b"--- a/path" file header, which only ever appears before a hunk.
        body = b"@@ -1,0 +1 @@\n+---\n"
        got, contents = extract(body)
        assert len(got) == 1
        assert contents[got[0].atom_id] == b"---"
        assert got[0].side == "new"


class TestLineShapes:
    def test_no_newline_marker_is_not_an_atom(self):
        body = (b"@@ -1 +1 @@\n"
                b"-old\n"
                b"\\ No newline at end of file\n"
                b"+new\n"
                b"\\ No newline at end of file\n")
        got, _ = extract(body)
        assert [a.side for a in got] == ["old", "new"]

    def test_crlf_is_content_not_normalised(self):
        body = b"@@ -1 +1 @@\n-a\r\n+a\n"
        got, contents = extract(body)
        # The removed line keeps its CR, so it differs from the added line.
        assert contents[got[0].atom_id] == b"a\r"
        assert contents[got[1].atom_id] == b"a"
        assert got[0].content_sha256 != got[1].content_sha256

    def test_added_empty_line_is_an_atom_with_empty_content(self):
        body = b"@@ -1,0 +1,2 @@\n+\n+x\n"
        got, contents = extract(body)
        assert len(got) == 2
        assert contents[got[0].atom_id] == b""
        assert got[0].content_sha256 == sha256_hex(b"")

    def test_empty_context_line_advances_both_sides(self):
        # b" " is an empty CONTEXT line: not an atom, but it must still move
        # the line counters or every following atom is misnumbered.
        body = b"@@ -1,3 +1,3 @@\n a\n \n-x\n+y\n"
        got, _ = extract(body)
        assert [(a.side, a.line_number) for a in got] == [("old", 3), ("new", 3)]

    def test_context_lines_are_never_atoms(self):
        body = b"@@ -1,2 +1,3 @@\n keep\n keep2\n+add\n"
        got, _ = extract(body)
        assert len(got) == 1 and got[0].side == "new"

    def test_line_numbers_track_across_multiple_hunks(self):
        body = (b"@@ -10 +10 @@\n-a\n+b\n"
                b"@@ -50 +50 @@\n-c\n+d\n")
        got, _ = extract(body)
        assert [(a.side, a.line_number) for a in got] == [
            ("old", 10), ("new", 10), ("old", 50), ("new", 50)]
        assert len({a.hunk_id for a in got}) == 2

    def test_unparseable_hunk_header_fails_closed(self):
        with pytest.raises(A.AtomError):
            extract(b"@@ this is not a hunk header @@\n+x\n")


class TestIdentity:
    def test_identical_lines_at_different_positions_are_distinct(self):
        body = b"@@ -1,0 +1,2 @@\n+same\n+same\n"
        got, _ = extract(body)
        assert got[0].atom_id != got[1].atom_id

    def test_old_and_new_side_of_a_modification_are_distinct_atoms(self):
        body = b"@@ -1 +1 @@\n-v\n+v\n"
        got, _ = extract(body)
        assert got[0].atom_id != got[1].atom_id
        assert {a.side for a in got} == {"old", "new"}

    def test_rename_binds_old_side_to_the_original_path(self):
        body = b"@@ -1 +1 @@\n-a\n+b\n"
        got, _ = extract(body, path=b"docs/new.md", orig=b"scripts/gate.py",
                         status="R100")
        old = next(a for a in got if a.side == "old")
        new = next(a for a in got if a.side == "new")
        assert old.path == b"scripts/gate.py"
        assert new.path == b"docs/new.md"

    def test_non_utf8_path_survives_as_raw_bytes(self):
        weird = b"app/caf\xe9_backdoor.py"
        got, _ = extract(b"@@ -1,0 +1 @@\n+x\n", path=weird)
        assert got[0].path == weird


class TestChangesWithoutHunks:
    """An empty atom set is trivially 'fully covered' — the dangerous case."""

    def test_pure_rename_yields_a_metadata_atom(self):
        body = (b"diff --git a/scripts/gate.py b/docs/notes.txt\n"
                b"similarity index 100%\n"
                b"rename from scripts/gate.py\n"
                b"rename to docs/notes.txt\n")
        content, _ = extract(body, path=b"docs/notes.txt",
                             orig=b"scripts/gate.py", status="R100")
        assert content == []                      # no hunks -> no content atoms
        meta, contents = A._metadata_atoms(
            body, path=b"docs/notes.txt", original_path=b"scripts/gate.py",
            git_status="R100", repository_change_sha256=PSHA)
        assert len(meta) == 1 and meta[0].side == "meta"
        import json as _json

        from verifier.canon import sha256_hex
        d = _json.loads(contents[meta[0].atom_id])
        assert d["kind"] == "rename"
        # Both endpoints of the rename are stated, as hashes: the descriptor
        # is transmitted content, so it never carries the path bytes (A2-F21).
        assert d["original_path_sha256"] == sha256_hex(b"scripts/gate.py")
        assert d["path_sha256"] == sha256_hex(b"docs/notes.txt")

    def test_mode_only_change_yields_a_metadata_atom(self):
        body = (b"diff --git a/deploy.sh b/deploy.sh\n"
                b"old mode 100644\n"
                b"new mode 100755\n")
        meta, contents = A._metadata_atoms(
            body, path=b"deploy.sh", original_path=None, git_status="M",
            repository_change_sha256=PSHA)
        assert len(meta) == 1
        assert b"mode" in contents[meta[0].atom_id]
        assert b"100755" in contents[meta[0].atom_id]

    def test_binary_change_is_detected_and_recorded(self):
        body = b"diff --git a/i.png b/i.png\nBinary files a/i.png and b/i.png differ\n"
        assert A.is_binary_diff(body)
        meta, contents = A._metadata_atoms(
            body, path=b"i.png", original_path=None, git_status="M",
            repository_change_sha256=PSHA)
        assert len(meta) == 1
        assert b"not-line-reviewable" in contents[meta[0].atom_id]

    def test_a_plain_modification_gets_no_metadata_atom(self):
        body = b"@@ -1 +1 @@\n-a\n+b\n"
        meta, _ = A._metadata_atoms(body, path=b"app/x.py", original_path=None,
                                   git_status="M", repository_change_sha256=PSHA)
        assert meta == []


class TestPatchOrdinals:
    def test_ordinals_are_dense_and_ordered_across_the_patch(self):
        a, _ = extract(b"@@ -1 +1 @@\n-a\n+b\n", path=b"app/a.py")
        b, _ = extract(b"@@ -1 +1 @@\n-c\n+d\n", path=b"app/b.py")
        stamped = A.assign_patch_ordinals(a + b)
        assert [x.patch_ordinal for x in stamped] == [0, 1, 2, 3]

    def test_ordinal_does_not_change_atom_identity(self):
        got, _ = extract(b"@@ -1,0 +1 @@\n+x\n")
        before = got[0].atom_id
        after = A.assign_patch_ordinals(got)[0]
        assert after.atom_id == before
        assert after.patch_ordinal == 0

    def test_metadata_atoms_participate_in_ordering(self):
        content, _ = extract(b"@@ -1,0 +1 @@\n+x\n", path=b"app/a.py")
        meta, _ = A._metadata_atoms(
            b"old mode 100644\nnew mode 100755\n", path=b"deploy.sh",
            original_path=None, git_status="M", repository_change_sha256=PSHA)
        stamped = A.assign_patch_ordinals(content + meta)
        assert [x.patch_ordinal for x in stamped] == [0, 1]
        assert stamped[1].side == "meta"
