"""B2 canonicalization hardening (mandate 6.9).

Every hash in a plan flows through canon; a single nondeterministic or
ambiguous encoding silently breaks every identity built on it. These tests pin
the rules as behavior, including the rejections: NaN, non-canonical Base64 and
bool-as-int are not "unlikely inputs", they are the exact seams an attacker
uses to make two different records hash identically.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verifier import canon  # noqa: E402


class TestCanonicalJson:
    def test_key_order_does_not_matter(self):
        a = canon.canonical_json({"b": 1, "a": [1, 2], "c": {"y": 0, "x": 1}})
        b = canon.canonical_json({"c": {"x": 1, "y": 0}, "a": [1, 2], "b": 1})
        assert a == b

    def test_no_insignificant_whitespace_and_no_ascii_escaping(self):
        out = canon.canonical_json({"k": "café", "n": 1})
        assert out == '{"k":"café","n":1}'.encode()

    def test_nan_and_infinity_are_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValueError):
                canon.canonical_json({"v": bad})

    def test_cross_run_pin(self):
        # Pinned output: if the canonical rules drift, every stored identity
        # silently changes; this fails loudly instead.
        out = canon.canonical_json({"z": None, "a": True, "m": [0, "x"]})
        assert out == b'{"a":true,"m":[0,"x"],"z":null}'
        assert canon.sha256_hex(out) == canon.sha256_hex(out)


class TestDigestLabels:
    def test_labels_must_be_versioned_domain_labels(self):
        assert canon.digest(b"atom-v1", b"x")  # existing label shape works
        for bad in (b"", b"atom", b"Atom-v1", b"atom-v", b"atom v1"):
            with pytest.raises(ValueError):
                canon.digest(bad, b"x")

    def test_different_labels_never_collide(self):
        assert canon.digest(b"a-v1", b"x") != canon.digest(b"b-v1", b"x")

    def test_part_boundaries_are_bound(self):
        # (b"ab", b"c") must not hash like (b"a", b"bc") — and the parts must
        # stay bound even when they CONTAIN the naive join delimiter, which is
        # exactly the case a NUL-joined digest would collide on.
        assert canon.digest(b"t-v1", b"ab", b"c") != canon.digest(b"t-v1", b"a", b"bc")
        assert (canon.digest(b"t-v1", b"a\0b", b"c")
                != canon.digest(b"t-v1", b"a", b"b\0c"))

    def test_part_count_is_bound(self):
        assert canon.digest(b"t-v1", b"a", b"") != canon.digest(b"t-v1", b"a")


class TestStrictBase64:
    def test_round_trip(self):
        raw = bytes(range(256))
        assert canon.unb64(canon.b64(raw)) == raw

    def test_non_canonical_encodings_are_rejected(self):
        good = canon.b64(b"hello")
        assert canon.unb64(good) == b"hello"
        for bad in (
            "aGVsbG8",        # missing padding
            "aGVsbG8= ",      # trailing whitespace
            "aGVs bG8=",      # inner whitespace
            "aGVsbG9=",       # non-zero trailing bits (not canonical)
            "aGVsbG8==",      # over-padded
            "%%%%",           # not base64 at all
        ):
            with pytest.raises(ValueError):
                canon.unb64(bad)


class TestNum:
    def test_bool_is_rejected_as_int(self):
        # True would silently hash as b"1": two semantically different records
        # (flag vs count) becoming byte-identical is exactly the forgery seam.
        with pytest.raises(TypeError):
            canon.num(True)
        with pytest.raises(TypeError):
            canon.num(False)
        assert canon.num(1) == b"1"

    def test_money_micros_is_integer_only(self):
        assert canon.money_micros(1_500_000) == "1500000"
        for bad in (1.5, True, "1"):
            with pytest.raises(TypeError):
                canon.money_micros(bad)
        with pytest.raises(ValueError):
            canon.money_micros(-1)
