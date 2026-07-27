"""B2 strict raw-change parser (mandate 6.3).

`git diff --raw -z --no-abbrev` is the authoritative changed-file universe:
status, both modes, both object IDs, rename/copy score, raw path bytes. The
parser accepts exactly the shapes git documents and blocks everything else —
a truncated stream, an unknown status, an out-of-range score — because every
silently-dropped record here is a file nobody reviews.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import gitdiff, rawchange  # noqa: E402
from verifier.canon import unb64  # noqa: E402
from verifier.errors import DIFF_PARSE_FAILURE, BlockingError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

O1 = "1" * 40
O2 = "2" * 40
Z = "0" * 40


def rec(old_mode, new_mode, old_oid, new_oid, status, *paths):
    head = f":{old_mode} {new_mode} {old_oid} {new_oid} {status}".encode()
    return head + b"\0" + b"\0".join(paths) + b"\0"


class TestAcceptedShapes:
    def test_modify(self):
        got = rawchange.parse_raw_z(rec("100644", "100644", O1, O2, "M", b"a.py"))
        assert len(got) == 1
        c = got[0]
        assert (c.status, c.score, c.path, c.orig_path) == ("M", None, b"a.py", None)
        assert (c.old_mode, c.new_mode, c.old_oid, c.new_oid) == (
            "100644", "100644", O1, O2)
        assert c.ordinal == 0

    def test_add_delete_typechange(self):
        raw = (rec("000000", "100644", Z, O1, "A", b"n.py")
               + rec("100644", "000000", O1, Z, "D", b"gone.py")
               + rec("100644", "120000", O1, O2, "T", b"link"))
        got = rawchange.parse_raw_z(raw)
        assert [c.status for c in got] == ["A", "D", "T"]
        assert [c.ordinal for c in got] == [0, 1, 2]

    def test_rename_and_copy_keep_both_paths(self):
        raw = (rec("100644", "100644", O1, O2, "R095", b"old.py", b"new.py")
               + rec("100644", "100644", O1, O2, "C080", b"src.py", b"dup.py"))
        got = rawchange.parse_raw_z(raw)
        r, c = got
        assert (r.status, r.score, r.orig_path, r.path) == ("R", 95, b"old.py", b"new.py")
        assert (c.status, c.score, c.orig_path, c.path) == ("C", 80, b"src.py", b"dup.py")

    def test_score_bounds_inclusive(self):
        for s in ("R000", "R100", "C000", "C100"):
            got = rawchange.parse_raw_z(
                rec("100644", "100644", O1, O2, s, b"a", b"b"))
            assert 0 <= got[0].score <= 100

    def test_empty_stream_is_no_changes(self):
        assert rawchange.parse_raw_z(b"") == []

    def test_non_utf8_paths_survive(self):
        weird = b"caf\xe9.py"
        got = rawchange.parse_raw_z(rec("100644", "100644", O1, O2, "M", weird))
        assert got[0].path == weird
        assert unb64(got[0].to_record()["new_path_bytes_b64"]) == weird


class TestRejectedShapes:
    def assert_blocks(self, raw):
        with pytest.raises(BlockingError) as e:
            rawchange.parse_raw_z(raw)
        assert e.value.code == DIFF_PARSE_FAILURE
        return str(e.value)

    def test_unknown_and_unmerged_statuses_block(self):
        for status in ("U", "X", "B", "AM", "RM"):
            self.assert_blocks(rec("100644", "100644", O1, O2, status, b"a"))

    def test_empty_status_blocks(self):
        self.assert_blocks(rec("100644", "100644", O1, O2, "", b"a"))

    def test_score_out_of_range_blocks(self):
        for status in ("R101", "R999", "C101"):
            self.assert_blocks(rec("100644", "100644", O1, O2, status, b"a", b"b"))

    def test_score_must_be_three_ascii_digits(self):
        for status in ("R1", "R10", "R1000", "R0x1", "R١٠٠"):
            self.assert_blocks(rec("100644", "100644", O1, O2, status, b"a", b"b"))

    def test_truncated_records_block(self):
        whole = rec("100644", "100644", O1, O2, "R095", b"old", b"new")
        self.assert_blocks(whole[:-5])            # missing final path+NUL
        self.assert_blocks(b":100644 100644 " + O1.encode())   # header cut off
        self.assert_blocks(rec("100644", "100644", O1, O2, "M", b"a")[:-1])

    def test_rename_without_second_path_blocks(self):
        self.assert_blocks(b":100644 100644 " + O1.encode() + b" "
                           + O2.encode() + b" R095\0only-one\0")

    def test_extra_trailing_bytes_block(self):
        good = rec("100644", "100644", O1, O2, "M", b"a.py")
        self.assert_blocks(good + b"garbage")
        self.assert_blocks(good + b"\0")

    def test_malformed_oid_and_mode_block(self):
        self.assert_blocks(rec("100644", "100644", "Z" * 40, O2, "M", b"a"))
        self.assert_blocks(rec("100644", "100644", O1[:39], O2, "M", b"a"))
        self.assert_blocks(rec("100648", "100644", O1, O2, "M", b"a"))
        self.assert_blocks(rec("10064", "100644", O1, O2, "M", b"a"))

    def test_record_must_start_with_colon(self):
        self.assert_blocks(b"100644 100644 " + O1.encode() + b" "
                           + O2.encode() + b" M\0a\0")

    def test_error_carries_no_raw_bytes(self):
        secret = b"sk-proj-abcdef123456"
        msg = self.assert_blocks(rec("100644", "100644", O1, O2, "U", secret))
        assert "sk-proj" not in msg


class TestRealRepository:
    PR25_BASE = "75a093de45f73169072837c7c062fab421caaf8b"
    PR25_HEAD = "b08844a0755710035d62830faa84902d9d85d3fe"

    def _have(self, sha):
        return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                              cwd=ROOT, capture_output=True).returncode == 0

    def test_pr25_range_raw_matches_name_status(self):
        if not (self._have(self.PR25_BASE) and self._have(self.PR25_HEAD)):
            pytest.skip("PR25 objects absent")
        raw = rawchange.raw_changes(self.PR25_BASE, self.PR25_HEAD, cwd=ROOT)
        ns = gitdiff.changed_files(self.PR25_BASE, head=self.PR25_HEAD, cwd=ROOT)
        rawchange.assert_matches_name_status(raw, ns)
        assert len(raw) == 3
        added = [c for c in raw if c.status == "A"]
        assert added and all(c.old_oid == Z for c in added)
        assert all(len(c.new_oid) == 40 for c in raw)

    def test_mismatch_against_name_status_blocks(self):
        a = rawchange.parse_raw_z(rec("100644", "100644", O1, O2, "M", b"a.py"))
        ns = [gitdiff.ChangedFile(path=b"b.py", orig_path=None, status="M")]
        with pytest.raises(BlockingError):
            rawchange.assert_matches_name_status(a, ns)
