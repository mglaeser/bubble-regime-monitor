"""B2 two patch identities (mandate 6.7).

repository_change_sha256 answers "did the repository change differently?" and
binds EVERY raw record — modes, object IDs, paths, scores, order — including
files whose content never reaches a provider. reviewable_content_sha256
answers "did the provider-visible material change?" and binds bodies plus
explicit exclusion markers. Conflating them is how an excluded-file change
either invalidates nothing or, worse, invalidates a review that never saw it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import contentpolicy as P  # noqa: E402
from verifier import identity, rawchange  # noqa: E402
from verifier.canon import sha256_hex  # noqa: E402

BASE = "a" * 40
HEAD = "b" * 40
O1, O2, O3, Z = "1" * 40, "2" * 40, "3" * 40, "0" * 40


def ch(status="M", path=b"app/x.py", orig=None, score=None,
       old_mode="100644", new_mode="100644", old_oid=O1, new_oid=O2, ordinal=0):
    return rawchange.RawChange(
        ordinal=ordinal, status=status, score=score, old_mode=old_mode,
        new_mode=new_mode, old_oid=old_oid, new_oid=new_oid,
        path=path, orig_path=orig)


class TestRepositoryChangeIdentity:
    def test_deterministic(self):
        a = identity.repository_change_sha256(BASE, HEAD, [ch()])
        b = identity.repository_change_sha256(BASE, HEAD, [ch()])
        assert a == b and len(a) == 64

    def test_every_field_is_bound(self):
        base_id = identity.repository_change_sha256(BASE, HEAD, [ch()])
        variants = [
            [ch(new_oid=O3)],
            [ch(new_mode="100755")],
            [ch(path=b"app/y.py")],
            [ch(status="T")],
            [ch(status="R", score=95, orig=b"app/old.py")],
        ]
        for v in variants:
            assert identity.repository_change_sha256(BASE, HEAD, v) != base_id
        assert identity.repository_change_sha256(BASE, "c" * 40, [ch()]) != base_id

    def test_order_is_bound(self):
        two = [ch(path=b"a.py", ordinal=0), ch(path=b"b.py", ordinal=1)]
        flipped = [ch(path=b"b.py", ordinal=0), ch(path=b"a.py", ordinal=1)]
        assert (identity.repository_change_sha256(BASE, HEAD, two)
                != identity.repository_change_sha256(BASE, HEAD, flipped))

    def test_excluded_file_changes_repository_identity(self):
        with_png_v1 = [ch(), ch(path=b"img/x.png", new_oid=O2, ordinal=1)]
        with_png_v2 = [ch(), ch(path=b"img/x.png", new_oid=O3, ordinal=1)]
        assert (identity.repository_change_sha256(BASE, HEAD, with_png_v1)
                != identity.repository_change_sha256(BASE, HEAD, with_png_v2))

    def test_not_derived_from_patch_text(self):
        src = (Path(__file__).resolve().parents[1]
               / "scripts/verifier/identity.py").read_text()
        assert "patch_bytes" not in src
        assert "file_diff" not in src


class TestReviewableContentIdentity:
    def _files(self, png_oid=O2, body=b"+x\n"):
        return [
            identity.reviewable_file_record(
                ch(), P.INCLUDED, body_sha256=sha256_hex(body)),
            identity.reviewable_file_record(
                ch(path=b"img/x.png", new_oid=png_oid, ordinal=1),
                P.PRIVACY_EXCLUDED, body_sha256=None),
        ]

    def test_excluded_only_change_leaves_reviewable_identity_unchanged(self):
        a = identity.reviewable_content_sha256(BASE, HEAD, self._files(png_oid=O2))
        b = identity.reviewable_content_sha256(BASE, HEAD, self._files(png_oid=O3))
        assert a == b

    def test_included_body_change_moves_both_identities(self):
        a = identity.reviewable_content_sha256(BASE, HEAD, self._files(body=b"+x\n"))
        b = identity.reviewable_content_sha256(BASE, HEAD, self._files(body=b"+y\n"))
        assert a != b

    def test_exclusion_marker_itself_is_bound(self):
        # The SAME file flipping from included to excluded must change the
        # reviewable identity even if no body hash is present either way.
        inc = [identity.reviewable_file_record(ch(), P.INCLUDED,
                                               body_sha256=sha256_hex(b""))]
        exc = [identity.reviewable_file_record(ch(), P.PRIVACY_EXCLUDED,
                                               body_sha256=None)]
        assert (identity.reviewable_content_sha256(BASE, HEAD, inc)
                != identity.reviewable_content_sha256(BASE, HEAD, exc))

    def test_body_hash_required_iff_included(self):
        with pytest.raises(ValueError):
            identity.reviewable_file_record(ch(), P.INCLUDED, body_sha256=None)
        with pytest.raises(ValueError):
            identity.reviewable_file_record(
                ch(), P.PRIVACY_EXCLUDED, body_sha256=sha256_hex(b"x"))


class TestAtomsBindRepositoryIdentity:
    def test_atom_ids_change_with_repository_identity(self):
        from verifier import atoms as A
        body = b"@@ -1 +1 @@\n-a\n+b\n"
        r1 = identity.repository_change_sha256(BASE, HEAD, [ch()])
        r2 = identity.repository_change_sha256(BASE, HEAD, [ch(new_oid=O3)])
        a1 = A.atomize_file_change(body, path=b"app/x.py", original_path=None,
                                   git_status="M", repository_change_sha256=r1)
        a2 = A.atomize_file_change(body, path=b"app/x.py", original_path=None,
                                   git_status="M", repository_change_sha256=r2)
        assert {x.atom_id for x in a1.atoms}.isdisjoint(
            {x.atom_id for x in a2.atoms})
