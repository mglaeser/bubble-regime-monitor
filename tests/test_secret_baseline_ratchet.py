"""The secret-baseline ratchet, and proof that each of its checks can fail.

## Why this file exists

Two tests guarded `.secrets.baseline` and each hardcoded its own reference
commit. Both pinned a commit from before the mid-term panel existed, so merging
PR #34 — which legitimately added one entry — turned both red simultaneously,
and the obvious repair (re-point them) would have been two independent edits to
two copies of one authority.

The reference now lives once, in `tests/secretbaselineref.py`, as a PINNED
commit id. This file proves the arrangement actually holds:

  * every guarded property reddens when violated — asserted by feeding a
    mutated baseline through the SAME function the real test calls, so a check
    that silently stopped comparing would be caught here;
  * the reference cannot be moved by branch or merge-base movement;
  * both baseline tests read the one reference and neither carries its own.

The mutations are applied to the REAL accepted baseline rather than to a
hand-written fixture. A hand-written one would drift from the shape the gate
actually produces, and then these tests would be about a document nobody ships.
"""

from __future__ import annotations

import ast
import copy
import json
import subprocess
from pathlib import Path

import pytest

import tests.secretbaselineref as ref

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def accepted():
    """The accepted baseline, as bytes and as a parsed document."""
    raw = ref.reference_baseline_bytes(root=ROOT)
    if raw is None:
        pytest.skip("accepted baseline reference unavailable in this checkout")
    return {"bytes": raw, "doc": json.loads(raw)}


def _rewrite(document: dict) -> bytes:
    """Serialise a mutated document the way the baseline file is written."""
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def _some_entry(document: dict):
    """One real entry, with the path it sits under.

    Asserted rather than skipped: the accepted baseline is a pinned document
    that is known to carry entries, so an empty one is a finding and not a
    reason to stop checking."""
    for path, entries in sorted(document["results"].items()):
        if entries:
            return path, entries[0]
    raise AssertionError(
        "the accepted baseline carries no entries — the mutation proofs below "
        "would all pass vacuously")


# --------------------------------------------------------------- growth ------


class TestTheGrowthRatchetReddens:
    """The ratchet is the load-bearing one: an unbounded baseline is a
    slow-motion wildcard exclusion."""

    def test_the_accepted_baseline_passes_against_itself(self, accepted):
        ref.assert_has_not_grown(accepted["doc"], accepted["doc"])

    def test_one_added_entry_after_the_new_reference_reddens(self, accepted):
        """The case that matters: someone adds an entry and says nothing."""
        grown = copy.deepcopy(accepted["doc"])
        path, entry = _some_entry(grown)
        extra = dict(entry)
        # A different hash under the same file: one more entry, nothing else.
        extra["hashed_secret"] = "0" * 40  # pragma: allowlist secret
        extra["line_number"] = int(entry.get("line_number") or 1) + 1
        grown["results"][path] = list(grown["results"][path]) + [extra]

        assert ref.entry_count(grown) == ref.entry_count(accepted["doc"]) + 1
        with pytest.raises(AssertionError, match="grew by 1 entries"):
            ref.assert_has_not_grown(grown, accepted["doc"])

    def test_an_entry_added_under_a_brand_new_file_also_reddens(self, accepted):
        grown = copy.deepcopy(accepted["doc"])
        _, entry = _some_entry(grown)
        grown["results"]["app/newly_added.py"] = [dict(
            entry, hashed_secret="1" * 40)]  # pragma: allowlist secret
        with pytest.raises(AssertionError, match="grew by 1 entries"):
            ref.assert_has_not_grown(grown, accepted["doc"])


class TestRemovalIsNotSilentlyEquivalent:
    """A removal must not be able to pay for an addition.

    The growth check compares TOTALS, so one entry removed and one added nets
    to zero and slips through it. That is exactly why byte-identity is enforced
    alongside it, and this class is the proof that the pair is doing the work
    rather than the total alone."""

    def test_removing_an_entry_is_not_caught_by_the_growth_check_alone(
            self, accepted):
        """Stated as a limitation, not papered over."""
        shrunk = copy.deepcopy(accepted["doc"])
        path, _ = _some_entry(shrunk)
        shrunk["results"][path] = shrunk["results"][path][1:]
        if not shrunk["results"][path]:
            del shrunk["results"][path]
        # Fewer entries, so the ratchet is satisfied — by design.
        ref.assert_has_not_grown(shrunk, accepted["doc"])
        assert ref.entry_count(shrunk) < ref.entry_count(accepted["doc"])

    def test_but_removing_an_entry_reddens_the_identity_check(self, accepted):
        shrunk = copy.deepcopy(accepted["doc"])
        path, _ = _some_entry(shrunk)
        shrunk["results"][path] = shrunk["results"][path][1:]
        if not shrunk["results"][path]:
            del shrunk["results"][path]
        with pytest.raises(AssertionError, match="entries 6 -> 5|entries"):
            ref.assert_identical_to_reference(_rewrite(shrunk),
                                              accepted["bytes"])

    def test_a_swap_that_keeps_the_total_reddens_the_identity_check(
            self, accepted):
        """One removed, one added: the total is unchanged and the ratchet is
        blind to it. Byte-identity is not."""
        swapped = copy.deepcopy(accepted["doc"])
        path, entry = _some_entry(swapped)
        replacement = dict(entry, hashed_secret="2" * 40)  # pragma: allowlist secret
        swapped["results"][path] = (
            [replacement] + list(swapped["results"][path][1:]))
        assert ref.entry_count(swapped) == ref.entry_count(accepted["doc"])
        ref.assert_has_not_grown(swapped, accepted["doc"])      # blind, as designed
        with pytest.raises(AssertionError):
            ref.assert_identical_to_reference(_rewrite(swapped),
                                              accepted["bytes"])


# -------------------------------------------------- filters and exclusions ----


class TestConfigurationChangesRedden:
    """detect-secrets keys filters by FUNCTION PATH and a second entry REPLACES
    the first — which is how the `.venv/`, `__pycache__/` and cache exclusions
    were once dropped without anyone editing them."""

    def test_the_accepted_configuration_passes_against_itself(self, accepted):
        ref.assert_configuration_unchanged(accepted["doc"], accepted["doc"])

    def test_removing_a_filter_reddens(self, accepted):
        mutated = copy.deepcopy(accepted["doc"])
        assert mutated.get("filters_used"), (
            "the accepted baseline declares no filters; the exclusion set this "
            "check defends would not exist")
        mutated["filters_used"] = mutated["filters_used"][1:]
        with pytest.raises(AssertionError, match="filters_used"):
            ref.assert_configuration_unchanged(mutated, accepted["doc"])

    def test_adding_a_filter_reddens(self, accepted):
        mutated = copy.deepcopy(accepted["doc"])
        mutated["filters_used"] = list(mutated.get("filters_used", [])) + [
            {"path": "detect_secrets.filters.regex.should_exclude_file",
             "pattern": ["^governance/"]}]
        with pytest.raises(AssertionError, match="filters_used"):
            ref.assert_configuration_unchanged(mutated, accepted["doc"])

    def test_changing_an_exclusion_pattern_reddens(self, accepted):
        mutated = copy.deepcopy(accepted["doc"])
        target = None
        for entry in mutated.get("filters_used", []):
            if "pattern" in entry:
                target = entry
                break
        assert target is not None, (
            "no pattern-bearing filter in the accepted baseline; the "
            "exclusion patterns this check defends would not exist")
        target["pattern"] = list(target["pattern"]) + ["^scripts/"]
        with pytest.raises(AssertionError, match="filters_used"):
            ref.assert_configuration_unchanged(mutated, accepted["doc"])

    def test_a_plugin_change_reddens(self, accepted):
        mutated = copy.deepcopy(accepted["doc"])
        assert mutated.get("plugins_used"), (
            "the accepted baseline declares no plugins; nothing would be "
            "detected at all")
        mutated["plugins_used"] = mutated["plugins_used"][1:]
        with pytest.raises(AssertionError, match="plugins_used"):
            ref.assert_configuration_unchanged(mutated, accepted["doc"])

    def test_a_configuration_change_also_reddens_the_identity_check(
            self, accepted):
        mutated = copy.deepcopy(accepted["doc"])
        mutated["filters_used"] = list(mutated.get("filters_used", []))[1:]
        with pytest.raises(AssertionError, match="configuration changed"):
            ref.assert_identical_to_reference(_rewrite(mutated),
                                              accepted["bytes"])

    def test_a_regenerated_baseline_is_named_as_such(self, accepted):
        """`generated_at` moving on its own is the signature of a rescan.

        The operator's standing instruction is not to regenerate the baseline,
        so the failure says which thing happened rather than 'bytes differ'."""
        mutated = copy.deepcopy(accepted["doc"])
        mutated["generated_at"] = "2099-01-01T00:00:00Z"
        with pytest.raises(AssertionError, match="REGENERATED"):
            ref.assert_identical_to_reference(_rewrite(mutated),
                                              accepted["bytes"])


# ------------------------------------------------- the reference cannot move --


class TestTheReferenceCannotMove:
    """A ratchet whose reference moves is not a ratchet."""

    def test_the_reference_is_an_immutable_commit_id(self):
        import re
        assert re.fullmatch(r"[0-9a-f]{40}",
                            ref.ACCEPTED_SECRET_BASELINE_COMMIT)

    def test_the_reference_module_resolves_no_ref_and_no_merge_base(self):
        """Checked over the module's own string literals.

        `merge-base`, `rev-parse` or an `origin/` ref anywhere in the
        resolution path would make the accepted baseline a function of where
        branches happen to point."""
        source = (ROOT / "tests" / "secretbaselineref.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        # Docstring nodes are excluded BY IDENTITY. `ast.get_docstring`
        # returns cleaned, dedented text that never equals the raw literal, so
        # comparing against it excludes nothing — the same "the comment
        # describing the rule is read as the rule" trap this repository keeps
        # walking into.
        docstring_nodes = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.FunctionDef,
                                     ast.ClassDef)):
                continue
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstring_nodes.add(id(first.value))
        code_literals = [n.value for n in ast.walk(tree)
                         if isinstance(n, ast.Constant)
                         and isinstance(n.value, str)
                         and id(n) not in docstring_nodes]
        for forbidden in ("merge-base", "rev-parse", "origin/", "HEAD"):
            offenders = [s for s in code_literals if forbidden in s]
            assert not offenders, (
                f"the reference resolution mentions {forbidden!r}: {offenders}")

    def test_branch_movement_cannot_change_the_accepted_baseline(self, tmp_path):
        """Empirical: move `main` in a clone and re-read the reference.

        The bytes come from a pinned commit, so they must be identical before
        and after the branch moves."""
        clone = tmp_path / "clone"
        got = subprocess.run(  # noqa: S603
            ["git", "clone", "-q", "--no-single-branch", str(ROOT), str(clone)],
            capture_output=True, text=True)
        if got.returncode != 0:
            pytest.skip("clone unavailable")
        before = ref.reference_baseline_bytes(root=clone)
        if before is None:
            pytest.skip("accepted baseline reference unavailable in the clone")

        # Move main somewhere else entirely, and change the baseline there.
        subprocess.run(["git", "checkout", "-q", "-B", "main"],  # noqa: S603
                       cwd=str(clone), capture_output=True)
        (clone / ".secrets.baseline").write_text('{"results": {}}\n',
                                                 encoding="utf-8")
        subprocess.run(["git", "-c", "user.email=t@example.invalid",  # noqa: S603
                        "-c", "user.name=t", "commit", "-qam", "move main"],
                       cwd=str(clone), capture_output=True)

        after = ref.reference_baseline_bytes(root=clone)
        assert after == before, (
            "the accepted baseline changed when main moved — the reference is "
            "not pinned")


class TestBothBaselineTestsShareOneReference:
    """The defect this whole file exists because of: two copies of one
    authority."""

    GUARDED = ("tests/test_secret_gate_policy.py",
               "tests/test_verifier_mc4_passc.py")

    def test_both_read_the_shared_reference_module(self):
        for name in self.GUARDED:
            source = (ROOT / name).read_text(encoding="utf-8")
            assert "secretbaselineref" in source, (
                f"{name} does not use the shared baseline reference")

    def test_neither_carries_its_own_baseline_commit_literal(self):
        """No bare 40-hex commit may sit in either file's baseline code.

        `test_verifier_mc4_passc.py` legitimately pins OTHER commits (PR #25's
        base and head), so the check is scoped to the accepted-baseline value
        and to the retired pin it replaced."""
        retired = "b08844a0755710035d62830faa84902d9d85d3fe"  # pragma: allowlist secret
        for name in self.GUARDED:
            source = (ROOT / name).read_text(encoding="utf-8")
            assert retired not in source, (
                f"{name} still pins the retired baseline reference {retired[:12]}…")
            assert ref.ACCEPTED_SECRET_BASELINE_COMMIT not in source, (
                f"{name} carries its own copy of the accepted reference; it "
                "must import it from tests/secretbaselineref.py")

    def test_the_accepted_reference_is_defined_exactly_once(self):
        """Counted over the Python sources on disk, not over `git ls-files`.

        A git-index query would report a file that has not been staged yet as
        absent, so this test would pass by finding nothing — a check that is
        satisfied by the thing it is looking for being missing."""
        holders = sorted(
            str(p.relative_to(ROOT))
            for directory in ("tests", "scripts", "app")
            for p in (ROOT / directory).rglob("*.py")
            if ref.ACCEPTED_SECRET_BASELINE_COMMIT
            in p.read_text(encoding="utf-8", errors="replace"))
        assert holders == ["tests/secretbaselineref.py"], holders
