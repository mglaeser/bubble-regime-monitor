"""B2 classification hardening (6.5), content policy (6.6), B1-08 split.

Two separable questions, kept separate on purpose:

  * classification — is this change control-bearing? (must its atoms be
    provably covered before green?)
  * content policy — may this change's BYTES leave the origin? (privacy)

The dangerous combination is control-bearing AND content-withheld: nothing to
review, everything at stake. That produces the PRIVACY_EXCLUDED_CONTROL block
HERE, at the policy layer — atomization (B1) now reports facts only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import atoms as A  # noqa: E402
from verifier import classification as C  # noqa: E402
from verifier import contentpolicy as P  # noqa: E402
from verifier.canon import sha256_hex  # noqa: E402
from verifier.errors import PRIVACY_EXCLUDED_CONTROL  # noqa: E402
from verifier.gitdiff import ChangedFile  # noqa: E402

PSHA = sha256_hex(b"fixture")


class TestIsCodeHardening:
    def test_hook_and_shell_dotfiles_are_code(self):
        for p in (b"hooks/pre-commit.hook", b".bashrc", b".zshrc", b".envrc",
                  b".bash_profile", b"deploy/.profile"):
            assert C.is_code(p), p

    def test_dockerfile_variants_are_code_case_insensitively(self):
        for p in (b"Dockerfile.prod", b"Dockerfile.test", b"dockerfile.dev",
                  b"deploy/Containerfile.ci", b"DOCKERFILE"):
            assert C.is_code(p), p

    def test_extensionless_unknown_stays_fail_closed(self):
        assert C.is_code(b"scripts/run-all")
        assert not C.is_code(b"LICENSE")
        assert not C.is_code(b"docs/README")

    def test_known_non_code_dotfiles_stay_non_code(self):
        for p in (b".gitignore", b".gitattributes", b".dockerignore"):
            assert not C.is_code(p), p

    def test_suffix_match_is_case_insensitive(self):
        assert C.is_code(b"schema.SQL")
        assert C.is_code(b"APP/MAIN.PY")


class TestClassificationRecord:
    def test_rename_takes_the_higher_risk_side_with_reasons(self):
        entry = ChangedFile(path=b"docs/notes.md", orig_path=b"scripts/gate.py",
                            status="R100")
        rec = C.classify_record(entry, [])
        assert rec.control_bearing is True
        assert rec.effective_risk == C.path_risk(b"scripts/gate.py")
        assert rec.reasons                     # never empty for a control hit
        assert any("scripts" in r or "code" in r for r in rec.reasons)

    def test_codeowners_match_is_recorded(self):
        entry = ChangedFile(path=b"notes/special.txt", orig_path=None, status="M")
        rec = C.classify_record(entry, ["/notes/"])
        assert rec.control_bearing is True
        assert "/notes/" in rec.matched_codeowners_rules

    def test_plain_docs_file_is_not_control(self):
        entry = ChangedFile(path=b"audit/report.md", orig_path=None, status="M")
        rec = C.classify_record(entry, [])
        assert rec.control_bearing is False
        assert rec.to_record()["control_bearing"] is False


class TestContentPolicyUnion:
    """The union of BOTH prior exclusion lists (panel + B1 port)."""

    @pytest.mark.parametrize("path", [
        b"img/logo.png", b"img/LOGO.PNG", b"fonts/x.woff2", b"x.pdf",
        b"backup.sql", b"rows.csv", b"model.npz", b"clip.mp4", b"dist/pkg.whl",
        b"maps/x.geojson", b"cache.pickle", b"dump.bak", b"a.sqlite3",
        b"data/anything/at/all.txt",
    ])
    def test_privacy_excluded_paths(self, path):
        policy, reason = P.path_content_policy(path)
        assert policy == P.PRIVACY_EXCLUDED
        assert reason

    @pytest.mark.parametrize("path", [
        b"app/main.py", b"frozen_methodology.json", b"datafoo/x.txt",
        b"docs/data.md", b"scripts/verifier/plan.py",
    ])
    def test_included_paths(self, path):
        policy, reason = P.path_content_policy(path)
        assert policy == P.INCLUDED and reason is None

    def test_rename_from_excluded_path_is_excluded(self):
        # The diff body of a rename shows OLD content too; either excluded
        # endpoint withholds the body.
        policy, _ = P.record_content_policy(b"docs/report.md", b"data/raw.md")
        assert policy == P.PRIVACY_EXCLUDED


class TestPolicyBlock:
    def test_control_bearing_excluded_content_blocks(self):
        block = P.control_block(control_bearing=True,
                                content_policy=P.PRIVACY_EXCLUDED)
        assert block is not None and block[0] == PRIVACY_EXCLUDED_CONTROL

    def test_control_bearing_binary_blocks(self):
        block = P.control_block(control_bearing=True, content_policy=P.BINARY)
        assert block is not None and block[0] == PRIVACY_EXCLUDED_CONTROL

    def test_non_control_excluded_content_does_not_block(self):
        assert P.control_block(control_bearing=False,
                               content_policy=P.PRIVACY_EXCLUDED) is None

    def test_included_content_never_blocks_here(self):
        assert P.control_block(control_bearing=True,
                               content_policy=P.INCLUDED) is None


class TestAtomizationReportsFactsOnly:
    """B1-08 closed: the atom layer no longer pre-applies the control block."""

    def test_privacy_excluded_atomization_is_unblocked_fact(self):
        result = A.atomize_file_change(
            b"", path=b"data/x.csv", original_path=None, git_status="M",
            repository_change_sha256=PSHA, content_available=False,
            privacy_excluded=True)
        assert result.reviewability == A.PRIVACY_EXCLUDED
        assert result.blocking_code is None
        assert result.atoms                      # obligation still recorded

    def test_binary_atomization_is_unblocked_fact(self):
        body = (b"diff --git a/i.png b/i.png\n"
                b"Binary files a/i.png and b/i.png differ\n")
        result = A.atomize_file_change(
            body, path=b"i.png", original_path=None, git_status="M",
            repository_change_sha256=PSHA)
        assert result.reviewability == A.UNREVIEWABLE_BINARY
        assert result.blocking_code is None
        assert result.atoms

    def test_parse_integrity_failures_still_block_at_the_atom_layer(self):
        # Facts vs policy: a diff git reported but we cannot parse is a FACT
        # failure and stays a block here (unchanged from B1.1).
        result = A.atomize_file_change(
            b"", path=b"a.py", original_path=None, git_status="M",
            repository_change_sha256=PSHA)
        assert result.blocking_code is not None
