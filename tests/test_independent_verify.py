"""The independent cross-vendor review panel's decision logic, under pytest.

The pure gate functions (decide / model_matches / require_approvals /
attest_reasons / attest_proof) are the panel's entire merge-blocking logic;
this suite pins their fail-closed semantics so a future edit cannot silently
soften them. The script's own --selftest covers the identical cases at CI
runtime; here the same guarantees ride the normal pytest suite.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "independent_verify", Path(__file__).resolve().parents[1] / "scripts" / "independent_verify.py")
iv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(iv)

MDL = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
A = {"ok": True, "v": {"refuted": False, "reason": "reason long enough a"}}
A2 = {"ok": True, "v": {"refuted": False, "reason": "reason long enough b"}}
RF = {"ok": True, "v": {"refuted": True, "confidence": "high", "reason": "real bug"}}
ERR = {"ok": False, "reason": "API 500"}


class TestDecide:
    def test_fail_closed_on_unparsable(self):
        assert iv.decide(None)["block"] is True
        assert iv.decide({})["block"] is True
        assert iv.decide({"refuted": "yes"})["block"] is True   # non-bool

    def test_confidence_thresholds(self):
        assert iv.decide({"refuted": True, "confidence": "high"})["block"] is True
        assert iv.decide({"refuted": True, "confidence": "medium"})["block"] is True
        assert iv.decide({"refuted": True, "confidence": "low"})["block"] is False
        assert iv.decide({"refuted": False})["block"] is False


class TestRequiredApproverRole:
    def test_sol_veto_any_confidence(self):
        low = {"ok": True, "v": {"refuted": True, "confidence": "low", "reason": "small doubt"}}
        assert iv.require_approvals([A, low, A], MDL, "gpt-5.6-sol", 1)["block"] is True
        assert iv.require_approvals([A, RF, A], MDL, "gpt-5.6-sol", 1)["block"] is True

    def test_sol_missing_or_fallback_blocks(self):
        no_sol = ["gpt-5.3-codex", "gpt-5.6", "gpt-4.1-mini"]
        assert iv.require_approvals([A, A2, A], no_sol, "gpt-5.6-sol", 1)["block"] is True

    def test_dated_snapshot_counts_variant_does_not(self):
        assert iv.model_matches("gpt-5.6-sol-2026-07-01", "gpt-5.6-sol") is True
        for variant in ("gpt-5.6-sol-mini", "gpt-5.6-sol-codex", "gpt-5.6-sol-preview",
                        "gpt-5.6-solaris", "gpt-5.6"):
            assert iv.model_matches(variant, "gpt-5.6-sol") is False

    def test_independent_corroboration_distinct_models(self):
        assert iv.require_approvals([A, A2, A], MDL, "gpt-5.6-sol", 1)["block"] is False
        assert iv.require_approvals([RF, A, RF], MDL, "gpt-5.6-sol", 1)["block"] is True
        dup = ["gpt-4.1-mini", "gpt-5.6-sol", "gpt-4.1-mini"]
        assert iv.require_approvals([A, A2, A], dup, "gpt-5.6-sol", 2)["block"] is True
        solo = ["gpt-5.6-sol"] * 3
        assert iv.require_approvals([A, A2, A], solo, "gpt-5.6-sol", 1)["block"] is True

    def test_nan_min_others_never_fail_open(self):
        assert iv.require_approvals([RF, A2, RF], MDL, "gpt-5.6-sol", float("nan"))["block"] is True
        assert iv.require_approvals([A, A2, RF], MDL, "gpt-5.6-sol", float("nan"))["block"] is False

    def test_sol_approval_must_be_proven(self):
        ch = "selftest-challenge"
        empty = {"ok": True, "v": {"refuted": False, "reason": "", "proof": f"{ch}-7"}}
        noproof = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol"}}
        good = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol",
                                  "proof": f"{ch}-7"}}
        assert iv.require_approvals([A, empty, A], MDL, "gpt-5.6-sol", 1, ch)["block"] is True
        assert iv.require_approvals([A, noproof, A], MDL, "gpt-5.6-sol", 1, ch)["block"] is True
        assert iv.require_approvals([A, good, A], MDL, "gpt-5.6-sol", 1, ch)["block"] is False


class TestIntegrityGates:
    def test_canned_green_blocks(self):
        r = lambda s, refuted=False: {"ok": True, "v": {"refuted": refuted, "reason": s}}  # noqa: E731
        same = "reason one aaaa"
        assert iv.attest_reasons([r(same), r(same), r(same)], 3)["block"] is True
        assert iv.attest_reasons([r(same), r(same), r("real bug here", True)], 3)["block"] is True
        assert iv.attest_reasons([r("reason a x1"), r("reason b x2"), r("reason c x3")], 3)["block"] is False

    def test_proof_of_check_bounds(self):
        ch = "selftest-challenge"
        pr = lambda p: {"ok": True, "v": {"refuted": False, "reason": "reason long enough", "proof": p}}  # noqa: E731
        assert iv.attest_proof([pr(f"{ch}-1"), pr(f"{ch}-9999"), pr(f"{ch}-500")], ch, 3)["block"] is False
        assert iv.attest_proof([pr(f"{ch}-0"), pr(f"{ch}-0"), pr(f"{ch}-0")], ch, 3)["block"] is True
        assert iv.attest_proof([pr(f"{ch}-10000")] * 3, ch, 3)["block"] is True
        assert iv.attest_proof([pr("wrong-1")] * 3, ch, 3)["block"] is True
        assert iv.attest_proof([pr(f"{ch}-7")] * 3, "", 3)["block"] is True


class TestNoKeyResidualMode:
    def test_selftest_passes_and_no_key_is_green_and_visible(self, monkeypatch):
        script = str(Path(__file__).resolve().parents[1] / "scripts" / "independent_verify.py")
        out = subprocess.run([sys.executable, script, "--selftest"],
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0 and "selftest" in out.stdout
        env = {"PATH": "/usr/bin:/bin"}   # no keys at all
        out2 = subprocess.run([sys.executable, script], capture_output=True, text=True,
                              timeout=60, env=env)
        assert out2.returncode == 0                 # never fake-blocks
        assert "RESIDUAL" in out2.stdout            # never fake-green either: loudly inactive


class TestEmptyEnvVarsAreAbsent:
    def test_empty_base_url_falls_back_to_default(self, monkeypatch):
        # GitHub Actions injects EMPTY strings for unset repo variables; an
        # empty VERIFIER_BASE_URL must behave like an absent one (observed
        # live: BASE="" crashed every request with "unknown url type").
        monkeypatch.setenv("VERIFIER_BASE_URL", "")
        spec = importlib.util.spec_from_file_location(
            "independent_verify_emptyenv",
            Path(__file__).resolve().parents[1] / "scripts" / "independent_verify.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.BASE == "https://api.openai.com/v1"


class TestPanelFindingsOnItself:
    """Sol's veto on PR #21 raised two findings about the panel's own code;
    both responses are pinned here."""

    def test_privacy_excludes_are_case_insensitive(self):
        # uppercase .PNG/.SVG/.PDF must be excluded exactly like lowercase
        assert all(spec.startswith(":(exclude,icase,glob)") for spec in iv._EXCLUDES)

    def test_strict_mode_blocks_any_high_medium_refutation(self):
        models = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
        ok = {"ok": True, "v": {"refuted": False, "reason": "reason long enough"}}
        rf = {"ok": True, "v": {"refuted": True, "confidence": "high", "reason": "bug"}}
        low = {"ok": True, "v": {"refuted": True, "confidence": "low", "reason": "doubt"}}
        assert iv.strict_any_refutation([ok, ok, rf], models)["block"] is True
        assert iv.strict_any_refutation([ok, ok, low], models)["block"] is False
        assert iv.strict_any_refutation([ok, ok, ok], models)["block"] is False

    def test_default_mode_stays_reference_identical(self):
        # WITHOUT strict mode the reference semantics hold: Sol + one distinct
        # corroborator green even when a third voice refutes high-confidence.
        models = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
        ok = {"ok": True, "v": {"refuted": False, "reason": "reason long enough x"}}
        ok2 = {"ok": True, "v": {"refuted": False, "reason": "reason long enough y"}}
        rf = {"ok": True, "v": {"refuted": True, "confidence": "high", "reason": "bug"}}
        assert iv.require_approvals([rf, ok, ok2], models, "gpt-5.6-sol", 1)["block"] is False

    def test_fork_origin_without_key_fails_closed(self):
        # Sol round 2: fork PRs run with secrets withheld; no-key there must
        # BLOCK (exit 1), while same-repo no-key stays green-but-loud.
        script = str(Path(__file__).resolve().parents[1] / "scripts" / "independent_verify.py")
        env = {"PATH": "/usr/bin:/bin", "VERIFIER_REQUIRE_KEY": "true"}
        out = subprocess.run([sys.executable, script], capture_output=True, text=True,
                             timeout=60, env=env)
        assert out.returncode == 1
        assert "fork-origin" in out.stderr

    def test_round3_data_denylist_extended(self):
        for ext in ("csv", "tsv", "sql", "jsonl", "parquet", "sqlite", "dump", "bak"):
            assert ext in iv.EXCLUDE_EXTS
        # .json stays reviewable ON PURPOSE: frozen_methodology.json IS the
        # methodology and must be visible to the panel.
        assert "json" not in iv.EXCLUDE_EXTS

    def test_round3_truncation_is_explicitly_marked(self):
        long = "x" * 100
        marked = iv.truncate_marked(long, 40, "DIFF BODY")
        assert marked.startswith("x" * 40)
        assert "DIFF BODY TRUNCATED — 60 of 100 bytes omitted" in marked
        assert iv.truncate_marked("short", 40, "DIFF BODY") == "short"

    def test_round3_responses_fallback_on_400_and_404(self):
        msg = "This model is only supported in v1/responses"
        assert iv.should_fallback_responses(400, msg) is True
        assert iv.should_fallback_responses(404, msg) is True
        assert iv.should_fallback_responses(400, "bad request: temperature") is False
        assert iv.should_fallback_responses(403, msg) is False
        assert iv.should_fallback_responses(400, None) is False

    def test_round4_base_branch_from_github_base_ref(self, monkeypatch):
        monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
        assert iv.base_branch() == "main"
        monkeypatch.setenv("GITHUB_BASE_REF", "")     # Actions empty-string trap
        assert iv.base_branch() == "main"
        monkeypatch.setenv("GITHUB_BASE_REF", "develop")
        assert iv.base_branch() == "develop"

    def test_round4_file_list_unfiltered_contents_filtered(self):
        cmds = iv.diff_commands("BASE")
        assert not any(a.startswith(":(exclude") for a in cmds["names"])
        assert any(a.startswith(":(exclude") for a in cmds["stat"])
        assert any(a.startswith(":(exclude") for a in cmds["body"])

    def test_round6_sh_failure_blocks_and_bad_utf8_stays_visible(self):
        import pytest as _pytest
        with _pytest.raises(iv.DiffError):
            iv._sh(["git", "rev-parse", "--verify", "no-such-ref-xyz123"], required=True)
        assert iv._sh(["false"]) == ""            # non-required keeps soft behavior
        out = iv._sh([sys.executable, "-c",
                      "import sys; sys.stdout.buffer.write(b'ok\\xff\\xfebad')"])
        assert "ok" in out and "bad" in out       # invalid UTF-8 visible, not vanished
        assert "�" in out

    def test_round6_diff_error_blocks_main(self, monkeypatch):
        monkeypatch.setattr(iv, "KEY", "fake-key-for-test")
        def boom():
            raise iv.DiffError("git exploded")
        monkeypatch.setattr(iv, "build_diff", boom)
        assert iv.main() == 1

    def test_round7_merge_base_failure_blocks_not_narrows(self, monkeypatch):
        import pytest as _pytest
        # a missing base ref must raise DiffError (block), never silently fall
        # back to HEAD~1 (which reviews only the tip commit of a multi-commit PR)
        monkeypatch.setenv("GITHUB_BASE_REF", "definitely-not-a-branch-xyz")
        with _pytest.raises(iv.DiffError):
            iv.build_diff()

    def test_round8_low_refutation_does_not_attest(self):
        ch = "selftest-challenge"
        sol_good = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol",
                                      "proof": f"{ch}-7"}}
        low_ref = {"ok": True, "v": {"refuted": True, "confidence": "low",
                                     "reason": "substantive doubt here", "proof": f"{ch}-9"}}
        canned = {"ok": True, "v": {"refuted": False, "reason": ""}}
        # a low refutation's reason/proof must NOT count toward the green majorities
        assert iv.attest_reasons([sol_good, low_ref, canned], 3)["block"] is True
        assert iv.attest_proof([sol_good, low_ref, canned], ch, 3)["block"] is True

    def test_round8_canned_corroborator_blocked_end_to_end(self):
        # require_approvals alone would green (Sol + codex approve), but the
        # canned codex approval fails attest_reasons now that the dissenting
        # low-refutation no longer attests on its behalf.
        ch = "selftest-challenge"
        models = ["gpt-5.3-codex", "gpt-5.6-sol", "gpt-4.1-mini"]
        canned_approve = {"ok": True, "v": {"refuted": False, "reason": "", "proof": f"{ch}-3"}}
        sol_good = {"ok": True, "v": {"refuted": False, "reason": "reason long enough sol",
                                      "proof": f"{ch}-7"}}
        low_ref = {"ok": True, "v": {"refuted": True, "confidence": "low",
                                     "reason": "substantive doubt here", "proof": f"{ch}-9"}}
        votes = [canned_approve, sol_good, low_ref]
        assert iv.require_approvals(votes, models, "gpt-5.6-sol", 1, ch)["block"] is False
        assert iv.attest_reasons(votes, 3)["block"] is True   # the conjunctive gate catches it


class TestAttestConsistency:
    """A green vote whose OWN reason names a defect is an inconsistent vote, not
    an approval. decide() reads only the boolean, so without this gate such a
    vote both counts toward the quorum and supplies a substantive, distinct
    reason that helps attest_reasons pass. Found adversarially against a live
    panel; fail-closed."""

    M3 = ["m-a", "m-b", "m-c"]

    @staticmethod
    def _green(reason, refuted=False):
        return {"ok": True, "v": {"refuted": refuted, "reason": reason, "confidence": "high"}}

    def test_ordinary_approvals_pass(self):
        votes = [self._green("checked auth paths, no issue"),
                 self._green("docs only change"),
                 self._green("reviewed diff, behaviour unchanged")]
        assert iv.attest_consistency(votes, self.M3)["block"] is False

    def test_green_vote_naming_a_defect_blocks(self):
        votes = [self._green("checked auth paths"),
                 self._green("auth bypass when key is None"),
                 self._green("docs only")]
        out = iv.attest_consistency(votes, self.M3)
        assert out["block"] is True and "m-b" in out["reason"]

    def test_refuting_vote_may_name_a_defect(self):
        # Naming the defect is precisely a refutation's job -- only GREEN votes
        # are inspected, or every real finding would trip its own gate.
        votes = [self._green("fail-open on missing header", refuted=True),
                 self._green("docs only"), self._green("no issue found")]
        assert iv.attest_consistency(votes, self.M3)["block"] is False

    def test_hedging_is_not_a_defect_claim(self):
        votes = [self._green("could be more defensive; consider hardening"),
                 self._green("ok looks fine"), self._green("no problems seen")]
        assert iv.attest_consistency(votes, self.M3)["block"] is False

    def test_errored_vote_is_not_inspected(self):
        assert iv.attest_consistency([ERR, self._green("docs only"),
                                      self._green("no issue")], self.M3)["block"] is False


class TestAuthHeader:
    """The gateway's auth header is configurable because inference.klee.me runs
    providers.openai with authMode="forward" and reserves Authorization for
    upstream forwarding: Bearer answers 401 "opencodex API key required" for
    every model, X-OpenCodex-API-Key answers 200. Verified live."""

    def test_defaults_to_bearer(self, monkeypatch):
        monkeypatch.delenv("VERIFIER_AUTH_HEADER", raising=False)
        assert "Authorization" in iv.auth_header()

    def test_custom_header_replaces_authorization(self, monkeypatch):
        monkeypatch.setenv("VERIFIER_AUTH_HEADER", "X-OpenCodex-API-Key")
        assert list(iv.auth_header()) == ["X-OpenCodex-API-Key"]

    def test_empty_variable_falls_back(self, monkeypatch):
        # Actions injects an EMPTY STRING for an unset repo variable; .get() with
        # a default would keep "" and send a nameless header.
        monkeypatch.setenv("VERIFIER_AUTH_HEADER", "")
        assert "Authorization" in iv.auth_header()

    def test_authorization_by_name_is_the_default_form(self, monkeypatch):
        monkeypatch.setenv("VERIFIER_AUTH_HEADER", "authorization")
        assert "Authorization" in iv.auth_header()


class TestReviewRange:
    """The job runs from the DEFAULT BRANCH under pull_request_target, where
    HEAD *is* main. Without an explicit candidate sha the diff collapses to
    main...main, returns empty, and the panel goes permanently FAKE-GREEN --
    reproduced before this guard existed."""

    @staticmethod
    def _record_git(monkeypatch) -> list[list[str]]:
        """Record every subprocess argv rather than raising on one.

        Deliberately not a raising sentinel: `_sh` wraps subprocess.run in a
        bare `except Exception` and converts whatever it catches into DiffError
        -- so a sentinel that raised would be laundered into the very exception
        these tests expect, and they would pass for the wrong reason twice
        over. Recording keeps the assertion about REACHING git separate from
        the assertion about the error."""
        calls: list[list[str]] = []

        class _Proc:
            returncode = 0
            stdout = b"0" * 40
            stderr = b""

        def fake_run(args, *_a, **_kw):
            calls.append(list(args))
            return _Proc()

        monkeypatch.setattr(iv.subprocess, "run", fake_run)
        return calls

    def test_non_hex_sha_is_rejected_before_reaching_git(self, monkeypatch):
        # The name claims two things -- rejected, and rejected BEFORE git sees
        # it. Asserting only `raises(DiffError)` checked neither: with the
        # 40-hex guard deleted, `git merge-base` fails on the garbage ref and
        # _sh(required=True) raises DiffError anyway. Verified by mutation --
        # the guard removed, all 44 tests stayed green. Both halves are now
        # asserted, and the argv assertion is what the name is really about.
        calls = self._record_git(monkeypatch)
        monkeypatch.setenv("VERIFIER_HEAD_SHA", "not-a-sha; rm -rf /")
        with pytest.raises(iv.DiffError, match="not a 40-hex sha"):
            iv.review_range()
        assert calls == [], f"the unvalidated sha was passed to git: {calls}"

    def test_short_sha_is_rejected(self, monkeypatch):
        calls = self._record_git(monkeypatch)
        monkeypatch.setenv("VERIFIER_HEAD_SHA", "8d85424")
        with pytest.raises(iv.DiffError, match="not a 40-hex sha"):
            iv.review_range()
        assert calls == [], f"the unvalidated sha was passed to git: {calls}"

    def test_a_candidate_that_is_an_ancestor_of_the_base_blocks(self, monkeypatch):
        """Nothing to review is a FAULT in a PR context, not an approval.

        Untested until now: deleting the ancestor check left all 44 tests
        green. It is the guard that stops a collapsed range greening the
        panel, which is the failure this whole class exists to describe."""
        sha = "a" * 40
        monkeypatch.setenv("VERIFIER_HEAD_SHA", sha)
        monkeypatch.setattr(iv, "_sh", lambda _args, **_kw: sha + "\n")
        with pytest.raises(iv.DiffError, match="ancestor"):
            iv.review_range()

    def test_an_empty_merge_base_blocks(self, monkeypatch):
        monkeypatch.setenv("VERIFIER_HEAD_SHA", "a" * 40)
        monkeypatch.setattr(iv, "_sh", lambda _args, **_kw: "  \n")
        with pytest.raises(iv.DiffError, match="empty merge-base"):
            iv.review_range()

    def test_a_candidate_ahead_of_the_base_is_accepted(self, monkeypatch):
        """The complement, so the two blocking tests above cannot be satisfied
        by a guard that simply refuses everything."""
        head, mb = "b" * 40, "c" * 40
        monkeypatch.setenv("VERIFIER_HEAD_SHA", head)
        monkeypatch.setattr(
            iv, "_sh",
            lambda args, **_kw: (mb + "\n") if args[1] == "merge-base" else (head + "\n"))
        assert iv.review_range() == (mb, head)


class TestStepSummary:
    """The panel must publish its findings where a reviewer will see them.

    GITHUB_STEP_SUMMARY renders as markdown on the run page, one click from the
    pull request's check. The reason text is model-authored from an untrusted
    diff, so it is hostile input to the RENDERER and must not break out of its
    table cell."""

    OK = {"ok": True, "v": {"refuted": False, "confidence": "high", "reason": "docs only change"}}
    REF = {"ok": True, "v": {"refuted": True, "confidence": "high", "reason": "auth bypass"}}
    ERR = {"ok": False, "reason": "API 504"}
    MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "deepseek"]

    def _write(self, tmp_path, monkeypatch, votes, gates, blocked):
        target = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
        iv.write_step_summary(votes, self.MODELS, gates, blocked=blocked)
        return target.read_text(encoding="utf-8")

    def test_approved_run_lists_every_voice(self, tmp_path, monkeypatch):
        out = self._write(tmp_path, monkeypatch, [self.OK, self.OK, self.OK],
                          [("required-approver", {"block": False, "reason": "sol approves"})], False)
        assert "**Verdict: APPROVED**" in out
        for model in self.MODELS:
            assert model in out
        assert out.count("approves") >= 3

    def test_blocked_run_names_the_gate_that_blocked(self, tmp_path, monkeypatch):
        out = self._write(tmp_path, monkeypatch, [self.REF, self.OK, self.OK],
                          [("required-approver", {"block": True, "reason": "sol vetoes"})], True)
        assert "**Verdict: BLOCKED**" in out
        assert "blocked" in out and "sol vetoes" in out

    def test_a_voice_that_errored_is_shown_not_hidden(self, tmp_path, monkeypatch):
        # A panel that silently drops a failed voice looks like a smaller panel
        # that agreed, which is the opposite of what happened.
        out = self._write(tmp_path, monkeypatch, [self.OK, self.ERR, self.OK],
                          [("required-approver", {"block": False, "reason": "ok"})], False)
        assert "no vote" in out and "API 504" in out

    def test_written_on_both_paths(self, tmp_path, monkeypatch):
        for blocked in (True, False):
            out = self._write(tmp_path, monkeypatch, [self.OK] * 3,
                              [("g", {"block": blocked, "reason": "r"})], blocked)
            assert out.strip(), "a panel that only explains itself when it blocks is unreadable"

    def test_model_text_cannot_break_the_table(self, tmp_path, monkeypatch):
        hostile = {"ok": True, "v": {"refuted": False, "confidence": "high",
                                     "reason": "a|b\n| evil | row |\n`x`"}}
        out = self._write(tmp_path, monkeypatch, [hostile, self.OK, self.OK],
                          [("g", {"block": False, "reason": "r"})], False)
        body = [line for line in out.splitlines() if line.startswith("| 1 |")]
        assert len(body) == 1, "the reason injected extra table rows"
        assert "\\|" in body[0] and "\\`" in body[0]

    def test_absent_env_is_a_no_op(self, monkeypatch):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        iv.write_step_summary([self.OK], ["m"], [("g", {"block": False, "reason": "r"})],
                              blocked=False)      # must not raise

    def test_an_unwritable_target_never_breaks_the_verdict(self, tmp_path, monkeypatch):
        # The REPORT must never turn a clean verdict into a red job.
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "no-such-dir" / "s.md"))
        iv.write_step_summary([self.OK], ["m"], [("g", {"block": False, "reason": "r"})],
                              blocked=False)      # must not raise


class TestMainFailsClosedOnAnUnreviewableDiff:
    """main()'s own refusals, none of which had a test.

    The parts of this script are well covered; the ASSEMBLY was not. A mutation
    run drove fourteen edits through the suite: eleven were caught, and the
    three survivors were all here or in review_range -- including the guard the
    source itself calls "the fake-green path". These tests drive main() with
    build_diff stubbed so the refusal, not the plumbing, is what is asserted."""

    @staticmethod
    def _armed(monkeypatch):
        """A run that has a key and is not a selftest -- i.e. past every early
        return, so what follows is genuinely main()'s diff handling."""
        monkeypatch.setattr(iv, "KEY", "test-key")
        monkeypatch.setattr(iv.sys, "argv", ["independent_verify.py"])

    def test_empty_diff_with_an_explicit_candidate_sha_blocks(self, monkeypatch, capsys):
        # THE FAKE-GREEN PATH. Under pull_request_target the job runs from the
        # default branch, where HEAD is main: if the range collapses, the diff
        # is empty and greening it approves the candidate without reading it.
        self._armed(monkeypatch)
        monkeypatch.setattr(iv, "build_diff", lambda: "")
        monkeypatch.setenv("VERIFIER_HEAD_SHA", "a" * 40)
        assert iv.main() == 1
        assert "review range" in capsys.readouterr().err

    def test_empty_diff_without_a_candidate_sha_is_green(self, monkeypatch, capsys):
        # The complement: with no candidate sha there is genuinely nothing to
        # review, and blocking every such run would make the gate unusable.
        self._armed(monkeypatch)
        monkeypatch.setattr(iv, "build_diff", lambda: "")
        monkeypatch.delenv("VERIFIER_HEAD_SHA", raising=False)
        assert iv.main() == 0
        assert "No diff to review" in capsys.readouterr().out

    def test_a_failed_diff_assembly_blocks(self, monkeypatch, capsys):
        self._armed(monkeypatch)

        def boom() -> str:
            raise iv.DiffError("merge-base unavailable")

        monkeypatch.setattr(iv, "build_diff", boom)
        assert iv.main() == 1
        assert "fail-closed" in capsys.readouterr().err

    def test_a_fork_run_without_a_key_blocks(self, monkeypatch, capsys):
        # Secrets are withheld from fork PRs, so "no key" there is an untrusted
        # origin, not the operator's documented residual.
        monkeypatch.setattr(iv, "KEY", "")
        monkeypatch.setattr(iv.sys, "argv", ["independent_verify.py"])
        monkeypatch.setenv("VERIFIER_REQUIRE_KEY", "true")
        assert iv.main() == 1
        assert "fork-origin" in capsys.readouterr().err

    def test_a_same_repo_run_without_a_key_reports_the_residual(self, monkeypatch, capsys):
        monkeypatch.setattr(iv, "KEY", "")
        monkeypatch.setattr(iv.sys, "argv", ["independent_verify.py"])
        monkeypatch.delenv("VERIFIER_REQUIRE_KEY", raising=False)
        assert iv.main() == 0
        assert "RESIDUAL" in capsys.readouterr().out


class TestVendorIndependence:
    """Distinct MODEL STRINGS are not distinct VENDORS.

    require_approvals counts model strings, and was credited with the property
    docs/INDEPENDENT_REVIEW_PANEL.md actually claims: Article IV asks for a
    verifier fleet from a DIFFERENT VENDOR. Reproduced live in run 32121148827
    -- the panel was gpt-5.6-sol, gpt-5.6-terra and a deepseek voice, deepseek
    returned API 504, the two gpt siblings approved, and the run printed
    "Cross-vendor panel confirms" and posted independent-verify=success.

    The FIRST fix for this was itself refuted, by the panel, on the pull request
    that carried it: a single vendor KEY made `gpt-5.6-sol` -> "gpt" and
    `openai/gpt-4.1-mini` -> "openai", so one vendor in two spellings passed a
    cross-vendor gate. Hence token SETS and disjointness, below.
    """

    PANEL = ["gpt-5.6-sol", "gpt-5.6-terra", "nvidia/deepseek-ai/deepseek-v4-flash-0731"]

    def test_every_segment_of_a_namespaced_id_contributes_a_token(self):
        assert iv.vendor_tokens("nvidia/deepseek-ai/deepseek-v4-flash-0731") == frozenset(
            {"nvidia", "deepseek"})

    def test_sibling_models_share_a_vendor(self):
        assert iv.same_vendor("gpt-5.6-sol", "gpt-5.6-terra")
        assert iv.same_vendor("gpt-5.6-sol", "gpt-4.1-mini")

    def test_one_vendor_in_two_spellings_is_one_vendor(self):
        # The panel's refutation of the first version of this gate, as a test.
        assert iv.same_vendor("gpt-5.6-sol", "openai/gpt-4.1-mini")
        assert iv.same_vendor("openai/gpt-5.6-sol", "gpt-5.6-terra")

    def test_genuinely_different_vendors_are_separable(self):
        assert not iv.same_vendor("gpt-5.6-sol", "nvidia/deepseek-ai/deepseek-v4-flash-0731")
        assert not iv.same_vendor("gpt-5.6-sol", "claude-opus-5")

    def test_unknown_provenance_reads_as_the_same_vendor(self):
        # Fail-closed: a blank or unparsable id must never count as independent.
        assert iv.same_vendor("", "gpt-5.6-sol")
        assert iv.same_vendor("gpt-5.6-sol", None)

    def test_case_and_surrounding_space_do_not_make_a_new_vendor(self):
        assert iv.same_vendor("  GPT-5.6-Sol  ", "gpt-5.6-terra")

    def test_the_live_outage_is_refused(self):
        out = iv.require_cross_vendor([A, A2, ERR], self.PANEL, "gpt-5.6-sol")
        assert out["block"] is True
        assert "not cross-vendor" in out["reason"]
        assert "deepseek" in out["reason"], "the refusal must name the voice that was lost"

    def test_the_alias_hole_is_refused(self):
        # Two OpenAI models, one namespaced, plus an unreachable third.
        panel = ["gpt-5.6-sol", "openai/gpt-4.1-mini", "nvidia/deepseek-ai/deepseek-v4"]
        assert iv.require_cross_vendor([A, A2, ERR], panel, "gpt-5.6-sol")["block"] is True

    def test_a_reachable_approving_other_vendor_corroborates(self):
        assert iv.require_cross_vendor([A, A2, A2], self.PANEL, "gpt-5.6-sol")["block"] is False

    def test_an_other_vendor_that_refutes_does_not_corroborate(self):
        assert iv.require_cross_vendor([A, A2, RF], self.PANEL, "gpt-5.6-sol")["block"] is True

    def test_the_required_approver_cannot_corroborate_itself(self):
        same = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-4.1-mini"]
        assert iv.require_cross_vendor([A, A2, A2], same, "gpt-5.6-sol")["block"] is True

    def test_configuration_may_accept_a_same_vendor_panel_but_not_the_label(self):
        out = iv.require_cross_vendor([A, A2, ERR], self.PANEL, "gpt-5.6-sol", enabled=False)
        assert out["block"] is False
        assert "SAME-VENDOR" in out["reason"], "an opt-out must still be named honestly"

    def test_is_cross_vendor_needs_two_separable_approvals(self):
        assert iv.is_cross_vendor(["gpt-5.6-sol", "nvidia/deepseek-ai/deepseek-v4"]) is True
        assert iv.is_cross_vendor(["gpt-5.6-sol", "openai/gpt-4.1-mini"]) is False
        assert iv.is_cross_vendor(["gpt-5.6-sol"]) is False

    def test_approving_models_ignores_errored_and_refuting_voices(self):
        assert iv.approving_models([A, ERR, RF], self.PANEL) == ["gpt-5.6-sol"]
        assert iv.approving_models([A, A2, A2], self.PANEL) == self.PANEL
