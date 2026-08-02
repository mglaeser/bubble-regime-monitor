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

# Two fixed commits this file compares against. They are git object ids, and a
# bare 40-hex literal is indistinguishable from a token to an entropy detector
# — which is the correct default, so the pragma states what they are rather
# than the baseline growing to accommodate them.
MAIN = "409cc5d8d9c2687e228db98cee0fad096fe523c3"  # pragma: allowlist secret
PR23 = "a9062aa656a5a6f3dbe5991d16ce9c218aad0454"  # pragma: allowlist secret

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

    def test_the_baseline_grows_only_where_a_reviewer_dispositioned_it(self):
        """An unbounded baseline is a slow-motion wildcard exclusion.

        This asserted `grew <= 0` against a fixed main commit, which forbade
        ANY new entry forever, on any branch. The reason was right and the
        implementation could not tell 'someone widened the baseline to hide a
        secret' from 'a reviewed file legitimately contains a hex digest' — so
        it forbade the second in order to forbid the first.

        That is not a safe place to leave a gate. The first person to hit it
        legitimately has one obvious move — bump the reference commit — and
        that removes the ratchet entirely, quietly, in a one-line diff nobody
        reads twice. A gate whose only escape is to disable it will be
        disabled.

        Growth is now allowed only for a file named in
        `.secrets-baseline-dispositions.json`, only up to the count recorded
        there, and only with a written reason. Undispositioned growth still
        fails, which is the property being protected. Found while rebuilding
        the PR #23 remediation stack on current main: package 1 adds seven
        sha256 digests of governance documents to a JSON manifest, which
        carries no comments and so cannot take the in-line pragma this
        repository uses elsewhere."""
        main = subprocess.run(
            ["git", "show",
             "b08844a0755710035d62830faa84902d9d85d3fe:.secrets.baseline"],
            cwd=ROOT, capture_output=True, text=True)
        if main.returncode != 0:
            pytest.skip("main baseline unavailable")
        base = json.loads(main.stdout)["results"]
        now = _baseline()["results"]

        # Committed, so no skip: a register that can be absent is a gate that
        # can be turned off by deleting a file, and `test_f3b_every_skip_in_
        # the_verifier_suite_is_a_declared_precondition` would rightly want to
        # know why a new skip reason had appeared.
        register = ROOT / ".secrets-baseline-dispositions.json"
        assert register.is_file(), (
            "the disposition register is missing; without it this gate has no "
            "record of which baseline growth a reviewer accepted")
        allowed = {}
        if True:
            document = json.loads(register.read_text(encoding="utf-8"))
            for entry in document["dispositions"]:
                for field in ("path", "max_entries", "what_they_are",
                              "classification_rationale", "reviewed_in"):
                    assert entry.get(field), (
                        f"disposition for {entry.get('path')!r} is missing "
                        f"{field}; an entry with no stated reason is a "
                        "wildcard with extra steps")
                allowed[entry["path"]] = entry["max_entries"]

        undispositioned = {}
        for path, entries in now.items():
            grew = len(entries) - len(base.get(path, []))
            if grew <= 0:
                continue
            budget = allowed.get(path)
            if budget is None:
                undispositioned[path] = grew
            else:
                assert len(entries) <= budget, (
                    f"{path} has {len(entries)} baseline entries, "
                    f"dispositioned for at most {budget}")
        assert undispositioned == {}, (
            f"baseline grew for undispositioned files: {undispositioned}. "
            "Add each to .secrets-baseline-dispositions.json with what the "
            "entries are and why they are not secrets, or remove them")

    def test_every_disposition_names_a_file_that_still_needs_one(self):
        """A disposition for a file with no baseline entries is a standing
        permission nobody is using — and the next person to add entries there
        inherits it without review."""
        register = ROOT / ".secrets-baseline-dispositions.json"
        assert register.is_file()
        now = _baseline()["results"]
        document = json.loads(register.read_text(encoding="utf-8"))
        stale = [e["path"] for e in document["dispositions"]
                 if not now.get(e["path"])]
        assert stale == [], f"dispositions with no baseline entries: {stale}"


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


class TestPr23UnsafeWorkflowsAreNotTransplanted:
    """Topology F-01/F-02, as a gate rather than as a note.

    PR #23 carries two workflow files from before Exchange 2:

      .github/workflows/independent-verify.yml   injects a provider credential
                                                 into a job running PR-controlled
                                                 code — V-TRUST, reintroduced
      .github/workflows/ci.yml                   unpins both actions to moving
                                                 tags and carries the inactive
                                                 job under its old name

    The remediation stack drops both and keeps main's. That decision lives in a
    document, and a document is not enforcement — the next person rebuilding the
    stack has no way to discover it except by reading prose. This is the same
    finding stated in a way that fails.
    """

    UNSAFE = ("independent-verify.yml", "ci.yml")

    def _at(self, ref, path):
        got = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=ROOT,
                             capture_output=True)
        return got.stdout if got.returncode == 0 else None

    def test_no_package_moved_a_workflow_away_from_mains_version(self):
        """Byte-identical to main, for both files, on this branch.

        Not 'the file is absent' — it must be PRESENT and it must be MAIN's.
        An absent `ci.yml` would also pass an absence check and would be a
        repository with no CI.

        No skip guard, deliberately. This is the load-bearing half, and a gate
        that excuses itself when its reference object is missing is a gate any
        shallow clone switches off — the failure mode reads as green. `test`
        checks out at `fetch-depth: 0`, so main is always reachable there; if
        it is ever not, that is worth a red rather than a silent pass."""
        for name in self.UNSAFE:
            path = f".github/workflows/{name}"
            ours = (ROOT / path).read_bytes()
            theirs = self._at(MAIN, path)
            assert theirs is not None, (
                f"{path} is not readable at main ({MAIN[:12]}…). Either the "
                "clone is too shallow for this gate to run, or main no longer "
                "carries the file this branch is being held to")
            assert ours == theirs, (
                f"{path} differs from main's. PR #23's version reintroduces "
                "V-TRUST and unpins both actions; the stack keeps main's")

    def test_the_pr23_versions_really_are_the_ones_being_refused(self):
        """The probe would be worthless if PR #23's files happened to equal
        main's — the test above would pass by coincidence. This asserts they
        genuinely differ, so the check above has something to catch.

        This half may skip where the other may not: it reads PR #23's head,
        which is optional history rather than the merge base. The reason string
        is the one `test_f3b`'s register already declares, and `test_f3c`
        already proves it does not fire under `fetch-depth: 0` — a second
        near-identical wording would have been a second thing to keep true."""
        if self._at(PR23, ".github/workflows/ci.yml") is None:
            pytest.skip("PR23 objects absent")
        for name in self.UNSAFE:
            path = f".github/workflows/{name}"
            assert self._at(PR23, path) != (ROOT / path).read_bytes(), (
                f"PR #23's {path} is identical to ours, so the retention "
                "check above proves nothing")
