"""The blocking secret gate, and the ways it has silently stopped working.

Macro-Cycle 3 added a second `should_exclude_file` filter to the baseline to
quiet the verifier artifacts. detect-secrets keys filters by FUNCTION PATH, so
the new entry did not join the existing one — it REPLACED it, silently
dropping the exclusions for .venv/, __pycache__/, .git/, the cache trees and
audit/. The gate then failed on three unrelated audit files, and the MC3
report misread that as a pre-existing condition on main. It was not: the gate
was green on main.

Two lessons, one test file:

  * a directory-wide exemption is not an acceptable answer to a noisy
    generated tree — it turns off detection for everything that will ever be
    written there;
  * a config format where adding an entry deletes another needs a test that
    asserts the surviving set, not a review that eyeballs the diff.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import tests.secretbaselineref as secretbaselineref

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".secrets.baseline"

# Trees whose contents are evidence, governance, or audit records. A
# directory-wide exemption here is exactly the failure the regime exists to
# prevent, so it is refused by name.
PROTECTED_PREFIXES = ("artifacts", "verifier", "governance", "audit")

# Exclusions that must survive any future baseline edit.
REQUIRED_EXCLUSIONS = (".venv/", "__pycache__/", ".git/", ".mypy_cache/",
                       ".pytest_cache/", ".ruff_cache/")

def _hook() -> Path | None:
    """The gate binary: PATH first, then the local virtualenv.

    A `.venv`-only lookup made `TestGateStillDetects` skip on every hosted run —
    CI installs detect-secrets globally — so the two tests that prove the gate
    still catches a planted secret, and that the exact CI command exits zero,
    were silently absent from the one place they matter. Found by adding `-rs`
    to the workflow: the run said "5 skipped" and could not say which five."""
    found = shutil.which("detect-secrets-hook")
    if found:
        return Path(found)
    local = ROOT / ".venv" / "bin" / "detect-secrets-hook"
    return local if local.exists() else None


HOOK = _hook()


def _baseline() -> dict:
    return json.loads(BASELINE.read_text())


def _exclude_filters(baseline: dict) -> list[dict]:
    return [f for f in baseline["filters_used"]
            if f["path"].endswith("should_exclude_file")]


class TestBaselineShape:
    def test_exactly_one_exclusion_filter_survives(self):
        # detect-secrets keys filters by path: a second entry REPLACES the
        # first. More than one means an earlier exclusion set was silently
        # discarded.
        filters = _exclude_filters(_baseline())
        assert len(filters) == 1, (
            "a second should_exclude_file entry silently replaces the first; "
            "merge the patterns into one alternation instead")

    def test_required_exclusions_are_all_present(self):
        pattern = "".join(_exclude_filters(_baseline())[0]["pattern"])
        for required in REQUIRED_EXCLUSIONS:
            assert required.replace(".", "\\.") in pattern or required in pattern, (
                f"{required} lost from the exclusion pattern")

    @pytest.mark.parametrize("prefix", PROTECTED_PREFIXES)
    def test_no_directory_wide_exemption_for_a_protected_tree(self, prefix):
        # `audit/` is a PRE-EXISTING residual inherited from main. It is
        # recorded here as a known exception to remediate in the broader
        # secret work — it is explicitly not a precedent for the others.
        if prefix == "audit":
            pytest.xfail("pre-existing audit/ exclusion inherited from main; "
                         "recorded as a residual, not extended")
        pattern = "".join(_exclude_filters(_baseline())[0]["pattern"])
        assert f"{prefix}/" not in pattern, (
            f"a directory-wide exemption for {prefix}/ turns off detection "
            "for every file ever written there; use exact reviewed entries")

    def test_the_baseline_carries_no_stale_artifact_entries(self):
        # MC4's first attempt baselined ~900 high-entropy strings from MC3
        # public summaries that the same report called stale. A baseline is
        # not a place to park evidence you have already disowned: those
        # artifacts are removed, and so are their entries.
        results = _baseline()["results"]
        stale = [f for f in results if f.startswith("artifacts/")]
        assert not stale, (
            f"baseline entries for untracked/stale artifacts: {stale}")

    def test_every_baseline_entry_maps_to_a_tracked_file(self):
        results = _baseline()["results"]
        tracked = set(subprocess.run(["git", "ls-files"], cwd=ROOT,
                                     capture_output=True,
                                     text=True).stdout.split())
        for path in results:
            assert path in tracked, f"baseline entry for untracked {path}"

    def test_the_baseline_has_not_grown_against_main(self):
        # An unbounded baseline is a slow-motion wildcard exclusion.
        #
        # The reference is a PINNED commit shared with
        # `test_verifier_mc4_passc.py`, not a merge-base: a ratchet whose
        # reference moves with the branches is not a ratchet. See
        # `tests/secretbaselineref.py` for the accepted transition.
        reference = secretbaselineref.reference_baseline_bytes(root=ROOT)
        if reference is None:
            pytest.skip("accepted baseline reference unavailable")
        secretbaselineref.assert_has_not_grown(_baseline(),
                                               json.loads(reference))

    def test_the_baseline_scan_configuration_has_not_changed(self):
        """Filters and plugins decide WHAT is scanned, so they ratchet too."""
        reference = secretbaselineref.reference_baseline_bytes(root=ROOT)
        if reference is None:
            pytest.skip("accepted baseline reference unavailable")
        secretbaselineref.assert_configuration_unchanged(
            _baseline(), json.loads(reference))


class TestGateStillDetects:
    """No skipif. The gate binary is a hard requirement for these two tests.

    Skipping them is worse than failing: the run reports success for the only
    checks that prove the gate still bites."""

    def setup_method(self):
        assert HOOK is not None, (
            "detect-secrets-hook is required: these tests are the proof that "
            "the secret gate still detects a planted secret")

    def _run(self, *paths):
        return subprocess.run(
            [str(HOOK), "--baseline", str(BASELINE), *map(str, paths)],
            cwd=ROOT, capture_output=True, text=True).returncode

    def test_a_planted_secret_under_artifacts_verifier_still_fails(self,
                                                                   tmp_path):
        # The wildcard exclusion would have let this through.
        planted = ROOT / "artifacts" / "verifier" / "_planted_probe.json"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(
            '{"k": "sk-proj-PLANTEDsecretVALUE1234567890abcd"}\n')  # pragma: allowlist secret
        try:
            assert self._run(planted) == 1, (
                "a new secret under artifacts/verifier/ must still block")
        finally:
            planted.unlink()

    def test_the_exact_ci_command_exits_zero(self):
        if shutil.which("git") is None:
            pytest.skip("git absent")
        # The exact blocking invocation from .github/workflows/ci.yml, with
        # pipefail so xargs' aggregate status cannot be masked by a later
        # command in the pipeline.
        result = subprocess.run(
            ["bash", "-c",
             f"set -o pipefail; git ls-files -z | xargs -0 {HOOK} "
             f"--baseline {BASELINE}"],
            cwd=ROOT, capture_output=True, text=True, timeout=900)
        assert result.returncode == 0, result.stdout[-4000:]


class TestNoPlaintextAllowlistIsCommitted:
    def test_reviewed_literals_are_never_committed_in_plaintext(self):
        # MC3 committed three plaintext "proposed allowlist" files listing
        # credential-shaped literals verbatim. A reviewed-literal record now
        # stores the SHA-256 and the scope, never the value.
        stale = list((ROOT / "artifacts").rglob("*allowlist*.txt"))
        assert not stale, (
            f"plaintext allowlists must not be committed: {stale}")


def _tracked(pattern: str) -> list[Path]:
    out = subprocess.run(["git", "ls-files", pattern], cwd=ROOT,
                         capture_output=True, text=True).stdout.split()
    return [ROOT / p for p in out]


class TestPublicArtifactsCarryNoFreeText:
    """The dedicated validator that would justify exact baseline entries.

    Absence is the CURRENT intended state: MC4 removed the stale MC3 summaries
    and their ~900 baseline entries, and nothing under artifacts/ is tracked
    today. A2-F12: that is asserted, not skipped — a skip quietly weakens the
    count and would keep passing if a summary reappeared unreviewed.

    The walker below stays because it is the validator a future tracked
    summary must satisfy before any baseline entry is added for it.
    """

    ALLOWED_LONG_STRINGS = ("integer micro-USD",)

    def test_no_artifact_summaries_are_tracked_today(self):
        # The intended state after MC4 stale-evidence removal. If a summary is
        # ever committed again, this fails and forces the reviewer to run the
        # validator below and add exact baseline entries deliberately.
        assert _tracked("artifacts/verifier/**/*.json") == [], (
            "an artifact summary was committed; run the free-text validator "
            "and add exact reviewed baseline entries before tracking it")

    def test_the_validator_accepts_a_digests_only_summary(self):
        # The validator itself is exercised on a representative record, so it
        # cannot rot while no summary is tracked.
        good = {"artifact": "untrusted-local-summary",
                "publication_class": "local-only",
                "review_skeleton_sha256": "a" * 64,
                "final_unit_count": 3,
                "money_unit": "integer micro-USD"}
        self._walk(good, "fixture")

    def test_the_validator_rejects_free_text(self):
        bad = {"artifact": "untrusted-local-summary",
               "leaked": "x" * 200}
        with pytest.raises(AssertionError):
            self._walk(bad, "fixture")

    def test_every_tracked_summary_is_digests_counts_and_enums(self):
        for path in _tracked("artifacts/verifier/**/*public*.json"):
            record = json.loads(path.read_text())
            self._walk(record, path.name)

    def _walk(self, node, where):
        if isinstance(node, dict):
            for key, value in node.items():
                self._walk(value, f"{where}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                self._walk(value, f"{where}[{i}]")
        elif isinstance(node, str):
            if len(node) <= 64 or node in self.ALLOWED_LONG_STRINGS:
                return
            # Long free text in a public artifact is how source content leaks
            # into a published file. Prose is allowed only where the schema
            # declares an explanatory field.
            assert where.split(".")[-1] in (
                "honest_scope", "reason", "semantics", "proves",
                "does_not_prove", "fallback_position", "resolution_method",
                "fallback_policy", "money_unit",
            ), f"unexpected long string at {where}"
