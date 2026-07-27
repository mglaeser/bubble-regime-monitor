"""B1.1 corrections — written RED-FIRST against B1 head eafa3fb.

Each class pins one B1.1 finding:

  B1-01  the generic fallback false-cleared unknown/empty changes
  B1-02  "one authoritative entrypoint" was not enforced
  B1-03  content binding was mutable and never revalidated
  B1-04  sequence was not unique across content + metadata
  B1-05  error diagnostics exposed raw diff text
  B1-06  the preamble was prefix-filtered, not exactly parsed
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import atoms as A  # noqa: E402
from verifier import errors as E  # noqa: E402
from verifier.canon import sha256_hex  # noqa: E402

RSHA = sha256_hex(b"fixture-repository-change")
SECRET = "sk-proj-abcdef123456"  # pragma: allowlist secret -- synthetic leak needle


def atomize(body: bytes, *, path=b"app/x.py", orig=None, status="M", **kw):
    return A.atomize_file_change(
        body, path=path, original_path=orig, git_status=status,
        repository_change_sha256=RSHA, **kw)


def kinds(result) -> list[str]:
    return [json.loads(result.contents[a.atom_id])["kind"]
            for a in result.atoms if a.side == A.SIDE_META]


# ------------------------------------------------------------- B1-01 ------


class TestFallbackDoesNotFalseClear:
    """A hash-only descriptor is an obligation marker, not semantic review.
    Unknown or empty changes must carry an explicit block; only specialized
    descriptors that fully STATE the change may stay unblocked."""

    def test_empty_body_with_content_available_blocks(self):
        result = atomize(b"", status="M")
        assert result.blocking_code == E.DIFF_PARSE_FAILURE
        assert result.reviewability == A.UNREVIEWABLE_METADATA

    def test_whitespace_only_body_blocks(self):
        result = atomize(b"   \n\t\n", status="M")
        assert result.blocking_code == E.DIFF_PARSE_FAILURE

    def test_index_only_body_creates_obligation_and_blocks(self):
        result = atomize(b"index 1111111..2222222 100644\n", status="M")
        assert kinds(result) == ["hunkless_change"]
        assert result.reviewability == A.UNREVIEWABLE_METADATA
        assert result.blocking_code == E.UNRECOGNIZED_HUNKLESS_CHANGE
        assert result.blocking_reason

    def test_context_only_hunk_blocks(self):
        with pytest.raises(A.AtomError):
            atomize(b"@@ -1,2 +1,2 @@\n a\n b\n", status="M")

    def test_specialized_rename_stays_metadata_only_and_unblocked(self):
        body = b"similarity index 100%\nrename from a.py\nrename to b.py\n"
        result = atomize(body, path=b"b.py", orig=b"a.py", status="R100")
        assert result.reviewability == A.METADATA_ONLY
        assert result.blocking_code is None

    def test_specialized_mode_change_stays_metadata_only_and_unblocked(self):
        result = atomize(b"old mode 100644\nnew mode 100755\n",
                         path=b"deploy.sh", status="M")
        assert result.reviewability == A.METADATA_ONLY
        assert result.blocking_code is None

    def test_empty_file_add_stays_metadata_only_and_unblocked(self):
        result = atomize(b"new file mode 100644\nindex 0000000..e69de29\n",
                         path=b"app/empty.py", status="A")
        assert result.reviewability == A.METADATA_ONLY
        assert result.blocking_code is None

    def test_empty_file_delete_stays_metadata_only_and_unblocked(self):
        result = atomize(b"deleted file mode 100644\nindex e69de29..0000000\n",
                         path=b"app/empty.py", status="D")
        assert result.reviewability == A.METADATA_ONLY
        assert result.blocking_code is None


# ------------------------------------------------------------- B1-02 ------


class TestPublicSurfaceIsTheEntryPoint:
    """A production caller must not be able to call the content parser,
    forget the metadata half, and prove an empty set covered."""

    def test_public_exports_are_exactly_the_entrypoint_surface(self):
        assert "atomize_file_change" in A.__all__
        assert "AtomizationResult" in A.__all__
        assert "assign_patch_ordinals" in A.__all__
        for private in ("extract", "extract_atoms", "metadata_atoms"):
            assert private not in A.__all__
            assert not hasattr(A, private), (
                f"{private} is still a public attribute — the low-level "
                "surface is bypassable")

    def test_no_production_source_calls_private_helpers(self):
        # Every git-tracked production module (scripts/, app/) except atoms.py
        # itself; tests are explicitly allowed to exercise privates.
        forbidden = {"_extract_content_atoms", "_metadata_atoms",
                     "_metadata_from_preamble", "_generic_hunkless_atoms",
                     "_parse_file_diff"}
        root = Path(__file__).resolve().parents[1]
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--", "scripts/*.py", "app/*.py",
             "scripts/**/*.py", "app/**/*.py"],
            cwd=root, capture_output=True, text=True)
        offenders = []
        for rel in {p for p in listing.stdout.split("\0") if p}:
            if rel.endswith("verifier/atoms.py") or rel.startswith("tests/"):
                continue
            tree = ast.parse((root / rel).read_text(errors="replace"))
            for node in ast.walk(tree):
                name = None
                if isinstance(node, ast.Attribute):
                    name = node.attr
                elif isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name in forbidden:
                            offenders.append(f"{rel}: import {alias.name}")
                if name in forbidden:
                    offenders.append(f"{rel}: {name}")
        assert not offenders, offenders

    def test_binary_low_level_parse_cannot_return_empty_success(self):
        body = b"Binary files a/i.png and b/i.png differ\n"
        with pytest.raises(A.AtomError):
            A._extract_content_atoms(body, path=b"i.png", original_path=None,
                                     git_status="M",
                                     repository_change_sha256=RSHA)

    def test_empty_low_level_parse_cannot_return_empty_success(self):
        with pytest.raises(A.AtomError):
            A._extract_content_atoms(b"", path=b"app/x.py", original_path=None,
                                     git_status="M",
                                     repository_change_sha256=RSHA)


# ------------------------------------------------------------- B1-03 ------


class TestContentBindingIsValidatedAndImmutable:
    """The model payload must never be able to diverge from the atom identity
    the coverage proof was computed over."""

    BODY = b"@@ -1,1 +1,1 @@\n-a\n+b\n"

    def test_returned_mapping_is_immutable(self):
        result = atomize(self.BODY)
        atom_id = result.atoms[0].atom_id
        with pytest.raises(TypeError):
            result.contents[atom_id] = b"tampered"
        with pytest.raises(TypeError):
            result.contents["new-key"] = b"x"

    def test_missing_content_entry_blocks(self):
        result = atomize(self.BODY)
        broken = dict(result.contents)
        broken.pop(result.atoms[0].atom_id)
        with pytest.raises(A.AtomError):
            A._validated_contents(list(result.atoms), broken)

    def test_extra_content_entry_blocks(self):
        result = atomize(self.BODY)
        broken = dict(result.contents)
        broken["not-an-atom-id"] = b"orphan"
        with pytest.raises(A.AtomError):
            A._validated_contents(list(result.atoms), broken)

    def test_tampered_content_bytes_block(self):
        result = atomize(self.BODY)
        broken = dict(result.contents)
        broken[result.atoms[0].atom_id] = b"tampered"
        with pytest.raises(A.AtomError):
            A._validated_contents(list(result.atoms), broken)

    def test_exact_key_equality_holds_end_to_end(self):
        result = atomize(self.BODY)
        assert set(result.contents) == {a.atom_id for a in result.atoms}
        for atom in result.atoms:
            assert sha256_hex(result.contents[atom.atom_id]) == atom.content_sha256

    def test_repeated_atomization_gives_identical_bindings(self):
        first = atomize(self.BODY)
        second = atomize(self.BODY)
        assert dict(first.contents) == dict(second.contents)


# ------------------------------------------------------------- B1-04 ------


class TestSequenceIsDensePerFile:
    """Documented order: content atoms in hunk order, then metadata atoms in
    canonical detected order, then the generic fallback. `sequence` must be
    dense and unique under that order — a duplicated 'deterministic order'
    value is a false contract."""

    def test_content_plus_one_metadata_is_dense(self):
        body = (b"new file mode 100644\nindex 0000000..1111111\n"
                b"--- /dev/null\n+++ b/app/n.py\n"
                b"@@ -0,0 +1,2 @@\n+one\n+two\n")
        result = atomize(body, path=b"app/n.py", status="A")
        seqs = [a.sequence for a in result.atoms]
        assert seqs == list(range(len(result.atoms))), seqs
        assert result.atoms[-1].side == A.SIDE_META   # metadata after content

    def test_content_plus_multiple_metadata_is_dense(self):
        body = (b"old mode 100644\nnew mode 100755\n"
                b"--- a/deploy.sh\n+++ b/deploy.sh\n"
                b"@@ -1,1 +1,1 @@\n-a\n+b\n")
        result = atomize(body, path=b"deploy.sh", status="M")
        seqs = [a.sequence for a in result.atoms]
        assert seqs == list(range(len(result.atoms)))
        assert len(seqs) == len(set(seqs))

    def test_rename_with_content_is_dense(self):
        body = (b"similarity index 90%\nrename from a.py\nrename to b.py\n"
                b"--- a/a.py\n+++ b/b.py\n"
                b"@@ -1,1 +1,1 @@\n-x\n+y\n")
        result = atomize(body, path=b"b.py", orig=b"a.py", status="R090")
        seqs = [a.sequence for a in result.atoms]
        assert seqs == list(range(len(result.atoms)))

    def test_restamping_does_not_change_atom_ids(self):
        body = (b"old mode 100644\nnew mode 100755\n"
                b"--- a/deploy.sh\n+++ b/deploy.sh\n"
                b"@@ -1,1 +1,1 @@\n-a\n+b\n")
        first = atomize(body, path=b"deploy.sh", status="M")
        second = atomize(body, path=b"deploy.sh", status="M")
        assert [a.atom_id for a in first.atoms] == [a.atom_id for a in second.atoms]
        assert [a.sequence for a in first.atoms] == [a.sequence for a in second.atoms]


# ------------------------------------------------------------- B1-05 ------


class TestErrorsCarryNoRawDiffText:
    """PR #25's lesson applies here too: a failure path must not become an
    uncontrolled text channel. Errors carry repository-owned categories,
    coordinates, lengths and hashes — never raw diff bytes."""

    def test_secret_in_hunk_section_heading_is_absent(self):
        body = (f"@@ -1,2 +1,2 @@ def {SECRET}(x):\n".encode()
                + b"-a\n+b\n")                       # declared 2/2, carries 1/1
        with pytest.raises(A.AtomError) as excinfo:
            atomize(body)
        assert SECRET not in str(excinfo.value)
        assert "sk-proj" not in str(excinfo.value)

    def test_secret_in_malformed_hunk_line_is_absent(self):
        body = b"@@ -1,1 +1,1 @@\n-a\n+b\n*" + SECRET.encode() + b"\n"
        with pytest.raises(A.AtomError) as excinfo:
            atomize(body)
        assert SECRET not in str(excinfo.value)

    def test_secret_in_unknown_preamble_line_is_absent(self):
        body = SECRET.encode() + b" leaked-preamble\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        with pytest.raises(A.AtomError) as excinfo:
            atomize(body)
        assert SECRET not in str(excinfo.value)

    def test_huge_malformed_header_produces_bounded_error(self):
        body = b"@@ -1,2 +1,2 @@ " + b"x" * 50_000 + b"\n-a\n+b\n"
        with pytest.raises(A.AtomError) as excinfo:
            atomize(body)
        assert len(str(excinfo.value)) < 600

    def test_non_utf8_malformed_bytes_are_not_replacement_decoded(self):
        body = b"\xff\xfe\x80 garbage preamble\n@@ -1,1 +1,1 @@\n-a\n+b\n"
        with pytest.raises(A.AtomError) as excinfo:
            atomize(body)
        assert "�" not in str(excinfo.value)
        assert "garbage" not in str(excinfo.value)

    def test_error_carries_category_and_structural_facts(self):
        body = b"@@ -1,3 +1,3 @@\n a\n-b\n+c\n"     # declared 3/3, carries 2/2
        with pytest.raises(A.AtomError) as excinfo:
            atomize(body)
        text = str(excinfo.value)
        assert "hunk_line_count_mismatch" in text
        assert "old_expected=3" in text and "old_consumed=2" in text
        assert "header_sha256=" in text


# ------------------------------------------------------------- B1-06 ------


class TestPreambleIsExactlyParsed:
    """One typed preamble parse drives BOTH content and metadata atomization;
    inconsistent known metadata can never be silently discarded."""

    def test_duplicate_old_mode_blocks(self):
        with pytest.raises(A.AtomError):
            atomize(b"old mode 100644\nold mode 100755\nnew mode 100755\n")

    def test_duplicate_new_mode_blocks(self):
        with pytest.raises(A.AtomError):
            atomize(b"old mode 100644\nnew mode 100755\nnew mode 100644\n")

    def test_unpaired_old_mode_blocks(self):
        with pytest.raises(A.AtomError):
            atomize(b"old mode 100644\nindex 1111111..2222222\n")

    def test_unmatched_rename_from_blocks(self):
        with pytest.raises(A.AtomError):
            atomize(b"similarity index 100%\nrename from a.py\n",
                    path=b"b.py", orig=b"a.py", status="R100")

    def test_unmatched_rename_to_blocks(self):
        with pytest.raises(A.AtomError):
            atomize(b"similarity index 100%\nrename to b.py\n",
                    path=b"b.py", orig=b"a.py", status="R100")

    def test_unmatched_copy_pair_blocks(self):
        with pytest.raises(A.AtomError):
            atomize(b"copy from a.py\n", path=b"b.py", orig=b"a.py",
                    status="C100")

    def test_rename_records_with_non_rename_status_block(self):
        with pytest.raises(A.AtomError):
            atomize(b"rename from a.py\nrename to b.py\n",
                    path=b"b.py", status="M")

    def test_duplicate_old_file_header_blocks(self):
        with pytest.raises(A.AtomError):
            atomize(b"--- a/x.py\n--- a/x.py\n+++ b/x.py\n"
                    b"@@ -1,1 +1,1 @@\n-a\n+b\n")

    def test_file_headers_in_wrong_order_block(self):
        with pytest.raises(A.AtomError):
            atomize(b"+++ b/x.py\n--- a/x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n")

    def test_unpaired_file_header_blocks(self):
        with pytest.raises(A.AtomError):
            atomize(b"--- a/x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n")

    def test_binary_payload_marker_outside_binary_state_blocks(self):
        with pytest.raises(A.AtomError):
            atomize(b"index 1111111..2222222\nliteral 5\nMcmZQzU|?tr\n")

    def test_new_file_mode_conflicts_with_deleted_file_mode(self):
        with pytest.raises(A.AtomError):
            atomize(b"new file mode 100644\ndeleted file mode 100644\n")

    def test_metadata_atoms_derive_from_the_single_parse(self):
        # The mode atom must reflect the PARSED record, proving there is no
        # second independent regex scan that could disagree with the parser.
        body = b"old mode 100644\nnew mode 100755\n"
        result = atomize(body, path=b"deploy.sh", status="M")
        d = json.loads(result.contents[result.atoms[0].atom_id])
        assert (d["kind"], d["old_mode"], d["new_mode"]) == \
            ("mode_change", "100644", "100755")
