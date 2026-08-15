"""The two mutations `adapterv2.model_identity_matches` survived, as tests.

## What this file is, and what it is not

It is NOT a change to `scripts/trustedlane/adapterv2.py`. That module's
committed behaviour is correct and stays byte-identical: it accepts the exact
governed id and its ASCII dated snapshot, and refuses everything else. What was
missing is a table that goes RED when it stops being correct.

Two mutations survived the existing suite — each one a single-token edit a
reasonable person might make while "loosening" the matcher, and each one a hole
in the rule that decides whether the answer came from the model this run priced
and approved:

  1. `observed == requested`  ->  `observed.lower() == requested.lower()`

     Every existing case-variant fixture carries a DATE
     (`GPT-4.1-MINI-2025-04-14`), so it never reaches the equality branch at
     all — it falls through to the regex and dies there for a reason that has
     nothing to do with case. The bare `GPT-4.1-MINI` was untested, so the
     mutant passed the whole suite. A case-insensitive matcher accepts a
     response whose `model` field is not the string the plan priced, and that
     string is written verbatim into `response_model`.

  2. `[0-9]`  ->  `[0-9a-f]`

     The existing malformed-date table is all shape errors — wrong widths,
     missing separators, extra suffixes. Not one entry puts a NON-DECIMAL
     character in a date position, so a hex-digit class matched every fixture
     the decimal class matched. `gpt-4.1-mini-2025-04-1f` is not a date and is
     not a model this repository governs.

## Why the mutants are derived and not retyped

Both are produced by TEXT SUBSTITUTION over the real `adapterv2.py` source,
loaded as a private module under a different package name. A hand-written copy
of the matcher would drift from the real one, and the day it drifted this file
would be proving something about its own copy. The substitutions assert they
changed something, so a refactor that renames the guard or the pattern reddens
here rather than silently mutating nothing and "killing" a mutant identical to
the original.

`TestTheSurvivingMutantsAreKilled` is the actual mutation proof: it runs THIS
FILE'S OWN tables against each mutant and requires at least one row to accept
what the real function refuses. A table asserted only against the correct
implementation cannot say which edits it would catch.

No test here opens a socket, reads a credential, or calls a provider.
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from midtermpanel import PANEL_MODELS  # noqa: E402
from trustedlane import adapterv2  # noqa: E402

ADAPTER_SOURCE = ROOT / "scripts" / "trustedlane" / "adapterv2.py"

#: A realistic dated snapshot for every governed model. These MUST keep
#: matching: an alias resolving to its own snapshot is the whole reason this
#: rule is not equality.
SNAPSHOTS = {
    "gpt-4.1-mini": "gpt-4.1-mini-2025-04-14",
    "gpt-5.3-codex": "gpt-5.3-codex-2025-11-04",
    "gpt-5.6-sol": "gpt-5.6-sol-2026-02-19",
}


# ------------------------------------------------------------ the mutants ---

#: `if observed == requested:` is the exact-equality branch. Mutating it to a
#: case-folded comparison is mutation 1.
CASE_INSENSITIVE_MUTATION = (
    "    if observed == requested:\n",
    "    if observed.lower() == requested.lower():\n",
)

#: The dated-snapshot pattern. Widening the digit class to hex is mutation 2.
HEX_DIGIT_MUTATION = (
    '_DATED_SNAPSHOT = r"\\A{escaped}-[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}\\Z"',
    '_DATED_SNAPSHOT = r"\\A{escaped}-[0-9a-f]{{4}}-[0-9a-f]{{2}}-[0-9a-f]{{2}}\\Z"',
)


def _load_mutant(tmp_path_factory, label: str, *substitutions):
    """`adapterv2` with the given substitutions applied, as an importable module.

    Loaded under the package name `mutantlane` rather than `trustedlane`, so the
    real module stays in `sys.modules` untouched and `from .errors import refuse`
    still resolves — against a copy of the real `errors.py`, because a mutant
    whose refusals behaved differently would not be the same experiment."""
    source = ADAPTER_SOURCE.read_text(encoding="utf-8")
    for old, new in substitutions:
        assert old in source, (
            f"the {label!r} mutation no longer matches adapterv2.py:\n{old!r}\n"
            "This file cannot prove which edits its tables catch while its "
            "mutations apply to nothing.")
        assert new != old
        source = source.replace(old, new, 1)

    home = tmp_path_factory.mktemp(label) / "mutantlane"
    home.mkdir()
    (home / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy(ROOT / "scripts" / "trustedlane" / "errors.py",
                home / "errors.py")
    (home / "adapterv2.py").write_text(source, encoding="utf-8")

    package_root = str(home.parent)
    if package_root not in sys.path:
        sys.path.insert(0, package_root)
    spec = importlib.util.spec_from_file_location(
        f"mutantlane.{label}", home / "adapterv2.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def case_insensitive_mutant(tmp_path_factory):
    return _load_mutant(tmp_path_factory, "caseinsensitive",
                        CASE_INSENSITIVE_MUTATION)


@pytest.fixture(scope="module")
def hex_digit_mutant(tmp_path_factory):
    return _load_mutant(tmp_path_factory, "hexdigits", HEX_DIGIT_MUTATION)


# ------------------------------------------------- 1. case-folded equality ---

#: Bare case variants — no date, so the EQUALITY branch is what decides them.
#: That is the branch the existing suite never exercised with a case variant.
BARE_CASE_VARIANTS = tuple(
    (variant, model)
    for model in PANEL_MODELS
    for variant in (model.upper(), model.lower().title(), model.capitalize(),
                    model.swapcase())
    if variant != model
) + (
    ("GPT-4.1-MINI", "gpt-4.1-mini"),
    ("Gpt-4.1-Mini", "gpt-4.1-mini"),
    ("gpt-4.1-MINI", "gpt-4.1-mini"),
    ("GPT-4.1-mini", "gpt-4.1-mini"),
)


class TestCaseInsensitiveExactEqualityIsRefused:
    """Mutation 1. `gpt-4.1-mini` and `GPT-4.1-MINI` are not the same string.

    The matcher's job is to say whether the answer came from the model this run
    priced and approved. `response_model` is the one provider-controlled value
    the adapter persists, and a matcher that folds case writes a string the plan
    never named into it and calls the run governed."""

    @pytest.mark.parametrize("observed,requested", BARE_CASE_VARIANTS)
    def test_a_bare_case_variant_is_refused(self, observed, requested):
        assert not adapterv2.model_identity_matches(observed, requested)

    @pytest.mark.parametrize("model", PANEL_MODELS)
    def test_a_case_variant_of_a_dated_snapshot_is_refused(self, model):
        """The regex branch is case-sensitive too, and for the same reason."""
        snapshot = SNAPSHOTS[model]
        assert not adapterv2.model_identity_matches(snapshot.upper(), model)
        assert not adapterv2.model_identity_matches(snapshot, model.upper())

    def test_the_requested_id_is_not_folded_either(self):
        """Both sides. A rule folding only the observed value is half a hole."""
        assert not adapterv2.model_identity_matches("gpt-4.1-mini",
                                                    "GPT-4.1-MINI")
        assert not adapterv2.model_identity_matches("gpt-4.1-mini-2025-04-14",
                                                    "GPT-4.1-MINI")


# ------------------------------------------------ 2. non-decimal date chars ---

#: A non-decimal character in EVERY date position, including the three the
#: mandate names. `[0-9a-f]` accepts every one of them; `[0-9]` accepts none.
HEX_DATES = (
    # the three named in the mandate
    "gpt-4.1-mini-2025-04-1f",
    "gpt-4.1-mini-2025-04-ff",
    "gpt-4.1-mini-202a-04-14",
    # year, each position
    "gpt-4.1-mini-a025-04-14",
    "gpt-4.1-mini-2f25-04-14",
    "gpt-4.1-mini-20b5-04-14",
    "gpt-4.1-mini-ffff-04-14",
    # month, each position
    "gpt-4.1-mini-2025-a4-14",
    "gpt-4.1-mini-2025-0e-14",
    "gpt-4.1-mini-2025-ab-14",
    # day, each position
    "gpt-4.1-mini-2025-04-c4",
    "gpt-4.1-mini-2025-04-1a",
    # every position at once
    "gpt-4.1-mini-abcd-ef-ab",
    "gpt-4.1-mini-deed-be-ef",
)

#: Non-decimal characters OUTSIDE `[0-9a-f]` as well, so the table does not
#: only describe the one mutation it was written for.
NON_DIGIT_DATES = (
    "gpt-4.1-mini-2025-04-1z",
    "gpt-4.1-mini-20x5-04-14",
    "gpt-4.1-mini-2025-0*-14",
    "gpt-4.1-mini-2O25-04-14",       # capital O, not zero
    "gpt-4.1-mini-2025-04-l4",       # lowercase L, not one
)


class TestNonDecimalDateCharactersAreRefused:
    """Mutation 2. A date is ten characters wide in three groups of DECIMALS.

    `[0-9]` and not `\\d` was already asserted, over the Unicode `Nd` category.
    This is the other half of the same rule and the half nothing tested: the
    class must not widen in the ASCII direction either. `gpt-4.1-mini-2025-04-1f`
    is not a dated snapshot of anything."""

    @pytest.mark.parametrize("observed", HEX_DATES)
    def test_a_hex_digit_in_any_date_position_is_refused(self, observed):
        assert not adapterv2.model_identity_matches(observed, "gpt-4.1-mini")

    @pytest.mark.parametrize("observed", NON_DIGIT_DATES)
    def test_any_other_non_decimal_character_is_refused(self, observed):
        assert not adapterv2.model_identity_matches(observed, "gpt-4.1-mini")

    @pytest.mark.parametrize("model", PANEL_MODELS)
    def test_the_rule_holds_for_every_governed_model(self, model):
        """Not just the mini. The required approver is the one that matters
        most, and `gpt-5.6-sol-2026-02-1f` must be as refusable as the rest."""
        broken = SNAPSHOTS[model][:-1] + "f"
        assert broken != SNAPSHOTS[model]
        assert not adapterv2.model_identity_matches(broken, model)

    def test_the_digit_class_is_decimal_in_the_source(self):
        """Stated over the pattern too, so widening it fails here as well as in
        the table above — two independent statements of one rule."""
        assert "[0-9]" in adapterv2._DATED_SNAPSHOT
        assert "[0-9a-f]" not in adapterv2._DATED_SNAPSHOT
        assert "\\d" not in adapterv2._DATED_SNAPSHOT


# ----------------------------------------------------- 3. the separators ---

#: Every character that renders like a hyphen and is not one. A separator rule
#: loose enough to admit any of these admits a string the provider cannot
#: legitimately return.
HYPHEN_LOOKALIKES = (
    "‐",   # HYPHEN
    "‑",   # NON-BREAKING HYPHEN
    "‒",   # FIGURE DASH
    "–",   # EN DASH
    "—",   # EM DASH
    "―",   # HORIZONTAL BAR
    "−",   # MINUS SIGN
    "－",   # FULLWIDTH HYPHEN-MINUS
    "­",   # SOFT HYPHEN
)

#: Ordinary characters someone might use as a separator instead.
OTHER_SEPARATORS = ("_", ".", "/", ":", " ", "", "|", "+")

#: The three separator positions in `<model>-YYYY-MM-DD`.
SEPARATOR_POSITIONS = (0, 1, 2)


def _with_separator(model: str, date_parts, index: int, separator: str) -> str:
    """`<model>-YYYY-MM-DD` with one of its three separators replaced."""
    separators = ["-", "-", "-"]
    separators[index] = separator
    year, month, day = date_parts
    return f"{model}{separators[0]}{year}{separators[1]}{month}{separators[2]}{day}"


class TestSeparatorsRemainLiteralAsciiHyphens:
    """A dash that is not U+002D is not a separator this rule accepts.

    Same argument as the digit class, in the other dimension. The suffix is a
    DATE matched by shape; if the shape's punctuation is negotiable then
    `gpt-4.1-mini–2025-04-14` is a governed model, and it is not."""

    @pytest.mark.parametrize("separator", HYPHEN_LOOKALIKES)
    @pytest.mark.parametrize("index", SEPARATOR_POSITIONS)
    def test_a_hyphen_lookalike_in_any_position_is_refused(self, separator,
                                                           index):
        observed = _with_separator("gpt-4.1-mini", ("2025", "04", "14"),
                                   index, separator)
        assert observed != "gpt-4.1-mini-2025-04-14"
        assert not adapterv2.model_identity_matches(observed, "gpt-4.1-mini")

    @pytest.mark.parametrize("separator", OTHER_SEPARATORS)
    @pytest.mark.parametrize("index", SEPARATOR_POSITIONS)
    def test_any_other_separator_in_any_position_is_refused(self, separator,
                                                            index):
        observed = _with_separator("gpt-4.1-mini", ("2025", "04", "14"),
                                   index, separator)
        assert not adapterv2.model_identity_matches(observed, "gpt-4.1-mini")

    def test_the_pattern_separators_are_ascii_in_the_source(self):
        """The pattern itself is ASCII. A lookalike pasted into it would be
        invisible in review and would widen the rule silently."""
        pattern = adapterv2._DATED_SNAPSHOT
        assert pattern.isascii(), (
            "the dated-snapshot pattern carries a non-ASCII character; a "
            "hyphen lookalike in the rule reads exactly like a hyphen")
        # Counted as "separator immediately followed by a digit class" rather
        # than as bare hyphens: `[0-9]` contains one of its own, and a bare
        # count would be asserting six and meaning three.
        assert pattern.count("-[0-9]") == 3, (
            f"expected three literal ASCII hyphen separators, each introducing "
            f"a decimal group: {pattern!r}")


# ------------------------------------------------- 4. what must still pass ---

class TestTheGovernedIdentitiesStillMatch:
    """The refusals above are only correct if the acceptances still hold.

    A matcher that refuses everything kills every mutant and blocks every run.
    Panel run 31853893226 is what over-strictness cost: two models answered,
    `gpt-4.1-mini` was refused for its own snapshot id, and the batch ended at
    `PROVIDER_RESPONSE_INVALID` with four generation attempts and no verdict."""

    @pytest.mark.parametrize("model", PANEL_MODELS)
    def test_the_exact_governed_id_matches(self, model):
        assert adapterv2.model_identity_matches(model, model)

    @pytest.mark.parametrize("model", PANEL_MODELS)
    def test_the_ascii_dated_snapshot_matches(self, model):
        assert adapterv2.model_identity_matches(SNAPSHOTS[model], model)

    @pytest.mark.parametrize("date", ["2025-04-14", "2026-02-19", "1999-01-01",
                                      "0000-00-00", "9999-99-99"])
    def test_every_ten_character_decimal_suffix_matches(self, date):
        """Matched by SHAPE, not by calendar validity. The rule exists to
        separate a date from `-preview`/`-latest`/`-codex`, and a calendar check
        here would be this module deciding which snapshots a provider may
        publish."""
        assert adapterv2.model_identity_matches(f"gpt-4.1-mini-{date}",
                                                "gpt-4.1-mini")


# ------------------------------------------------- 5. the mutation proof ---

class TestTheSurvivingMutantsAreKilled:
    """Each mutant, run against this file's own tables, must accept something
    the real matcher refuses.

    This is what makes the tables above a mutation test rather than a list of
    strings that happen to be false today."""

    def test_the_case_insensitive_mutant_accepts_a_refused_identity(
            self, case_insensitive_mutant):
        killed = [(observed, requested)
                  for observed, requested in BARE_CASE_VARIANTS
                  if case_insensitive_mutant.model_identity_matches(
                      observed, requested)]
        assert killed, (
            "`observed.lower() == requested.lower()` passed every row in "
            "BARE_CASE_VARIANTS, so this file does not actually catch that "
            "mutation")

    def test_the_case_insensitive_mutant_still_agrees_everywhere_else(
            self, case_insensitive_mutant):
        """The mutant is a ONE-line edit and must behave identically on every
        governed acceptance — otherwise the row above could be killing some
        unrelated breakage rather than the mutation named."""
        for model, snapshot in SNAPSHOTS.items():
            assert case_insensitive_mutant.model_identity_matches(model, model)
            assert case_insensitive_mutant.model_identity_matches(snapshot,
                                                                  model)

    @pytest.mark.parametrize("table", [HEX_DATES])
    def test_the_hex_digit_mutant_accepts_a_refused_date(self, hex_digit_mutant,
                                                         table):
        killed = [observed for observed in table
                  if hex_digit_mutant.model_identity_matches(observed,
                                                             "gpt-4.1-mini")]
        assert killed, (
            "`[0-9a-f]` passed every row in HEX_DATES, so this file does not "
            "actually catch that mutation")

    def test_the_hex_digit_mutant_still_accepts_every_real_snapshot(
            self, hex_digit_mutant):
        for model, snapshot in SNAPSHOTS.items():
            assert hex_digit_mutant.model_identity_matches(snapshot, model)

    def test_the_real_matcher_refuses_every_row_both_mutants_accept(self):
        """The pairing, stated once: everything the mutants let through, the
        committed implementation refuses."""
        for observed, requested in BARE_CASE_VARIANTS:
            assert not adapterv2.model_identity_matches(observed, requested)
        for observed in HEX_DATES:
            assert not adapterv2.model_identity_matches(observed,
                                                        "gpt-4.1-mini")


class TestThisFileChangesNothingItTests:
    """`adapterv2.py` is production code in the credential-bearing job and this
    pull request does not touch it. Asserted here rather than trusted, because a
    test file that quietly edited its subject would be the most expensive
    possible place to find that out."""

    def test_the_committed_adapter_is_unmodified(self):
        import subprocess
        blob = subprocess.run(
            ["git", "hash-object", "scripts/trustedlane/adapterv2.py"],
            cwd=str(ROOT), capture_output=True, text=True, check=True
        ).stdout.strip()
        committed = subprocess.run(
            ["git", "rev-parse", "HEAD:scripts/trustedlane/adapterv2.py"],
            cwd=str(ROOT), capture_output=True, text=True)
        if committed.returncode != 0:      # pragma: no cover - not a git tree
            pytest.skip("not a git checkout")
        assert blob == committed.stdout.strip(), (
            "the working tree's adapterv2.py differs from the committed blob. "
            "This lane's model matcher must not change in the same breath as "
            "the tests that pin it")
