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

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".secrets.baseline"

# Trees whose contents are evidence, governance, or audit records. A
# directory-wide exemption here is exactly the failure the regime exists to
# prevent, so it is refused by name.
PROTECTED_PREFIXES = ("artifacts", "verifier", "governance", "audit")

# Exclusions that must survive any future baseline edit.
REQUIRED_EXCLUSIONS = (".venv/", "__pycache__/", ".git/", ".mypy_cache/",
                       ".pytest_cache/", ".ruff_cache/")

HOOK = ROOT / ".venv" / "bin" / "detect-secrets-hook"


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
        main = subprocess.run(
            ["git", "show",
             "b08844a0755710035d62830faa84902d9d85d3fe:.secrets.baseline"],
            cwd=ROOT, capture_output=True, text=True)
        if main.returncode != 0:
            pytest.skip("main baseline unavailable")
        base = json.loads(main.stdout)
        now = _baseline()
        grew = sum(len(v) for v in now["results"].values()) - sum(
            len(v) for v in base["results"].values())
        assert grew <= 0, f"baseline grew by {grew} entries"


@pytest.mark.skipif(not HOOK.exists(), reason="detect-secrets not installed")
class TestGateStillDetects:
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
    """The dedicated validator that justifies the exact baseline entries.

    Each entry says "this high-entropy string is one of our own digests". That
    claim is only as good as the guarantee that a public summary contains
    nothing BUT digests, counts and enums — so it is checked, not asserted.
    """

    ALLOWED_LONG_STRINGS = ("integer micro-USD",)

    def test_every_public_summary_is_digests_counts_and_enums(self):
        files = _tracked("artifacts/verifier/**/*public*.json")
        if not files:
            pytest.skip("no public summaries tracked")
        for path in files:
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
