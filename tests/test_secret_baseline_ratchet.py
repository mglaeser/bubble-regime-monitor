"""The secret baseline's two pins, and proof that each of their checks can fail.

## Why this file exists

Two tests guarded `.secrets.baseline` and each hardcoded its own reference
commit. Both pinned a commit from before the mid-term panel existed, so merging
PR #34 — which legitimately added one entry — turned both red simultaneously,
and the obvious repair (re-point them) would have been two independent edits to
two copies of one authority.

The reference now lives once, in `tests/secretbaselineref.py`: the entries
the baseline may hold as a literal (`ACCEPTED_ENTRIES`), its scan configuration
as a PINNED commit id. This file proves the arrangement actually holds:

  * every guarded property reddens when violated — asserted by feeding a
    mutated baseline through the SAME function the real test calls, so a check
    that silently stopped comparing would be caught here;
  * the reference commit cannot be moved by branch or merge-base movement;
  * both baseline tests read the one reference and neither carries its own.

The mutations are applied to REAL baselines rather than to a hand-written
fixture: the entry mutations to the committed file, the configuration mutations
to the baseline at the pinned commit. A hand-written one would drift from the
shape the gate actually produces, and then these tests would be about a
document nobody ships.

## `.secrets.baseline` cannot carry 64-hex digests

The baseline's accepted-entries check and the entropy detector are in tension:
detect-secrets cannot distinguish a published sha256 digest from a leaked
token, so any doc or fixture that gains a literal 64-hex string needs a
`# pragma: allowlist secret` rather than a baseline entry. Discovered while
adding gate evidence. Not a defect in either tool — a constraint worth writing
down, because the obvious fix (grow the baseline) is the wrong one.
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
    """The baseline at the pinned commit, as bytes and as a parsed document:
    the reference for the scan configuration."""
    raw = ref.reference_baseline_bytes(root=ROOT)
    if raw is None:
        pytest.skip("accepted baseline reference unavailable in this checkout")
    return {"bytes": raw, "doc": json.loads(raw)}


@pytest.fixture
def committed():
    """The committed baseline, parsed: the document the real-file test reads.

    Read from the working tree, so no git object is needed and nothing skips."""
    return json.loads((ROOT / ref.BASELINE_PATH).read_text(encoding="utf-8"))


def _some_entry(document: dict):
    """One real entry, with the path it sits under.

    Asserted rather than skipped: the committed baseline is known to carry
    entries (`ref.ACCEPTED_ENTRIES`), so an empty one is a finding and not a
    reason to stop checking."""
    for path, entries in sorted(document["results"].items()):
        if entries:
            return path, entries[0]
    raise AssertionError(
        "the committed baseline carries no entries — the mutation proofs below "
        "would all pass vacuously")


# -------------------------------------------------------------- entries ------


class TestTheAcceptedEntriesRedden:
    """The committed baseline holds exactly `ref.ACCEPTED_ENTRIES`, per file.

    This class replaces TestTheGrowthRatchetReddens and
    TestRemovalIsNotSilentlyEquivalent. Those fed mutated copies of the baseline
    at the pinned commit (b047c3d: 19 entries in 4 files) through
    `assert_has_not_grown` and `assert_identical_to_reference`, and both
    reddened there. Neither protected the committed file, which holds 4 entries
    in 1 file. Executed on 9f79c7a with the assertions the real-file tests
    called:

        committed baseline + 1, + 2, + 15 new entries in app/config.py   accepted
        committed baseline + 16                                          refused: "baseline grew by 1 entries"
        the identity check on the committed baseline                     fails: "entries 19 -> 4"; no test
                                                                         had applied it since 2026-08-17

    Equality with a literal leaves no such room: one entry more or one fewer is
    refused. And every mutation below is applied to the committed file through
    the function the real-file test calls
    (tests/test_secret_gate_policy.py::TestBaselineShape::
    test_the_baseline_holds_exactly_the_accepted_entries), so this class cannot
    go on proving a check that nothing applies to that file."""

    def test_the_committed_baseline_is_accepted(self, committed):
        ref.assert_accepted_entries(committed)

    def test_one_added_entry_reddens(self, committed):
        """The case that matters: someone adds an entry and says nothing.

        The growth check accepted exactly this document on 9f79c7a."""
        grown = copy.deepcopy(committed)
        path, entry = _some_entry(grown)
        # A different hash under the same file: one more entry, nothing else.
        extra = dict(entry, hashed_secret="0" * 40,  # pragma: allowlist secret
                     line_number=int(entry.get("line_number") or 1) + 1)
        grown["results"][path] = list(grown["results"][path]) + [extra]
        with pytest.raises(AssertionError, match="are not the accepted"):
            ref.assert_accepted_entries(grown)

    def test_an_entry_under_a_new_file_reddens(self, committed):
        grown = copy.deepcopy(committed)
        _, entry = _some_entry(grown)
        grown["results"]["app/newly_added.py"] = [dict(
            entry, hashed_secret="1" * 40)]  # pragma: allowlist secret
        with pytest.raises(AssertionError, match="app/newly_added.py"):
            ref.assert_accepted_entries(grown)

    def test_a_removed_entry_reddens_too(self, committed):
        """A shrink has to lower the literal in the same change. The growth
        check let it pass in silence, which is how the room for 15 additions
        opened (19 entries -> 5 -> 4 while the reference stayed at 19)."""
        shrunk = copy.deepcopy(committed)
        path, _ = _some_entry(shrunk)
        shrunk["results"][path] = shrunk["results"][path][1:]
        with pytest.raises(AssertionError, match="are not the accepted"):
            ref.assert_accepted_entries(shrunk)

    def test_an_entry_moved_to_another_file_reddens(self, committed):
        """One removed here, one added there: the total is unchanged, which is
        all the growth check compared. The literal is per file."""
        moved = copy.deepcopy(committed)
        path, entry = _some_entry(moved)
        moved["results"][path] = moved["results"][path][1:]
        moved["results"]["app/newly_added.py"] = [dict(
            entry, hashed_secret="2" * 40)]  # pragma: allowlist secret
        assert sum(ref.entries_by_file(moved).values()) \
            == sum(ref.ACCEPTED_ENTRIES.values())
        with pytest.raises(AssertionError, match="app/newly_added.py"):
            ref.assert_accepted_entries(moved)

    def test_an_entry_exchanged_inside_one_file_is_not_seen(self, committed):
        """The stated limit, pinned so that it reads as a decision.

        The literal counts the entries per file and does not name them: a
        hash is credential-shaped, so naming the four would put four allowlist
        pragmas into the reference. One entry exchanged for another in the
        same file therefore passes. The growth check did not see that either,
        and the secret scan still refuses every secret whose hash the baseline
        does not hold."""
        exchanged = copy.deepcopy(committed)
        path, entry = _some_entry(exchanged)
        exchanged["results"][path] = [dict(
            entry, hashed_secret="3" * 40)  # pragma: allowlist secret
        ] + list(exchanged["results"][path][1:])
        ref.assert_accepted_entries(exchanged)


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

    # Two tests of `assert_identical_to_reference` stood here: a configuration
    # change reddened it too, and a baseline whose `generated_at` alone had
    # moved was named as REGENERATED. The function is deleted (see
    # TestTheAcceptedEntriesRedden): no test applied it to the committed file,
    # where it fails. A rescan that adds or removes an entry is refused by the
    # accepted entries, and one that changes a filter, a plugin or the version
    # by the checks of this class. A rescan that moves the timestamp and
    # nothing else changes nothing the gate decides.


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

    # test_verifier_mc4_passc.py was retired with the verifier package. The
    # tuple is still enumerated EXPLICITLY rather than discovered, so that
    # deleting the last consumer makes this list empty and trips the guard
    # below — a check must not be satisfied by having nothing left to check.
    GUARDED = ("tests/test_secret_gate_policy.py",)

    def test_the_guarded_set_is_not_empty(self):
        """Deleting every consumer must fail loudly, not silently pass."""
        assert self.GUARDED, "no file guards the shared baseline reference"

    def test_both_read_the_shared_reference_module(self):
        for name in self.GUARDED:
            source = (ROOT / name).read_text(encoding="utf-8")
            assert "secretbaselineref" in source, (
                f"{name} does not use the shared baseline reference")

    def test_neither_carries_its_own_baseline_commit_literal(self):
        """No bare 40-hex commit may sit in a guarded file's baseline code.

        Scoped to the accepted-baseline value and the retired pin it replaced,
        rather than to any 40-hex string: a guarded file may legitimately pin
        OTHER commits for unrelated reasons."""
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
