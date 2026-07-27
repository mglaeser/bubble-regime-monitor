"""B1-07 smoke evidence: explicit-SHA ranges with machine assertions.

The previous smoke claim ("origin/main~1..origin/main = 12 files / 2,505
atoms") was FALSE: the helper diffs against HEAD, HEAD was the precursor
branch, so the range silently included the branch's own files. These tests pin
the correction — every range is named by exact SHAs, the changed-file count is
asserted against git's own name-status output, and every changed file is
atomized exactly once.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import atoms, gitdiff, identity, rawchange  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

PR25_BASE = "75a093de45f73169072837c7c062fab421caaf8b"
PR25_HEAD = "b08844a0755710035d62830faa84902d9d85d3fe"
PR25_FILES = {
    b".github/workflows/openai-verifier-capability-probe.yml",
    b"docs/VERIFIER_CAPABILITY_PROBE.md",
    b"tests/test_probe_error_redaction.py",
}


def _have(sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                          cwd=ROOT, capture_output=True).returncode == 0


def _smoke(base: str, head: str):
    """Atomize every changed file in base..head; return per-file results.

    The repository identity is the canonical raw-record identity (B2, 6.7) —
    never a hash of privacy-filtered patch text."""
    files = gitdiff.changed_files(base, head=head, cwd=ROOT)
    raw = rawchange.raw_changes(base, head, cwd=ROOT)
    rawchange.assert_matches_name_status(raw, files)
    rsha = identity.repository_change_sha256(base, head, raw)
    results = {}
    for entry in files:
        body = gitdiff.file_diff(base, entry, head=head, cwd=ROOT)
        assert entry.path not in results, "file atomized more than once"
        results[entry.path] = atoms.atomize_file_change(
            body, path=entry.path, original_path=entry.orig_path,
            git_status=entry.status, repository_change_sha256=rsha)
    return files, results


class TestExplicitShaSmoke:
    def test_pr25_range_is_exactly_its_three_files(self):
        if not (_have(PR25_BASE) and _have(PR25_HEAD)):
            pytest.skip("PR25 range objects not present in this clone")
        files, results = _smoke(PR25_BASE, PR25_HEAD)
        # the changed-file universe is git's, not ours — assert exact equality
        assert {f.path for f in files} == PR25_FILES
        assert len(results) == len(files) == 3
        for path, result in results.items():
            assert result.atoms, f"{path!r} atomized to nothing"
            assert result.reviewability == atoms.REVIEWABLE
            assert result.blocking_code is None

    def test_current_branch_range_matches_git_name_status_exactly(self):
        if not _have(PR25_HEAD):
            pytest.skip("main base object not present in this clone")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True).stdout.strip()
        files, results = _smoke(PR25_HEAD, head)
        raw = subprocess.run(
            ["git", "diff", "--name-status", "-z", f"{PR25_HEAD}...{head}"],
            cwd=ROOT, capture_output=True).stdout
        git_count = len(gitdiff.parse_name_status_z(raw))
        assert len(files) == len(results) == git_count
        for result in results.values():
            assert result.atoms or result.blocking_code is not None
