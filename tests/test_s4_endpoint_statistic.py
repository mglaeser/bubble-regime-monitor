"""v4.0-s4-endpoint — s4 scores the CURRENT regime, not the sample's history.

GSADF is sup_{r2} BSADF(r2): it answers "was there ever an explosive episode
anywhere in this window?" and stays rejected for as long as a spent episode
remains in sample. BubbleGauge reports a present-tense regime, so it scores the
BSADF at the LAST observation against the endpoint row of the simulated BSADF
critical values.

The divergence is not hypothetical. Measured with exuber 1.1.0 (lag=0,
nrep=2000, seed 20260711) on CPI-deflated native Nasdaq-100 monthly log levels
from 1986 (T=487): the GSADF sup is 2.6189 against cv95 2.2604 — a rejection —
and it is attained at a window ending 2000-02, while the BSADF at the 2026-07
endpoint is 0.7562 against endpoint cv90 1.1769. Under the v3 rule a longer
history would have reported the dot-com peak as a present-day bubble signal.

These tests drive compute_snapshot, so behaviour is what is pinned: swapping the
selection back to the sup, or pointing the red flag at the unscored statistic,
must fail here.
"""

from __future__ import annotations

import json

import pytest

from app import methodology as M
from app.services.compute import compute_snapshot, scored_s4_statistic
from tests.conftest import make_golden_raw_inputs

# Measured, exuber 1.1.0, real native Nasdaq-100 from 1986 (T=487).
SUP_1986, SUP_CV90, SUP_CV95 = 2.6189, 2.0034, 2.2604          # rejects at 5%
END_1986, END_CV90, END_CV95 = 0.7562, 1.1769, 1.4315          # does not reject
_CACHED = (M.frozen_bytes, M.frozen_sha256, M.frozen_methodology)


@pytest.fixture
def frozen_statistic(monkeypatch, tmp_path):
    """Rewrite gsadf.statistic in an off-disk copy of the frozen artifact."""
    def _apply(value):
        data = json.loads(M.FROZEN_PATH.read_bytes())
        data["gsadf"]["statistic"] = value
        f = tmp_path / "frozen.json"
        f.write_text(json.dumps(data))
        monkeypatch.setattr(M, "FROZEN_PATH", f)
        for fn in _CACHED:
            fn.cache_clear()
    yield _apply
    for fn in _CACHED:
        fn.cache_clear()          # restore the real artifact for every other test


def _raw_diverging():
    """The sup rejects; the endpoint does not. The two rules disagree maximally."""
    raw = make_golden_raw_inputs()
    raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = SUP_1986, SUP_CV90, SUP_CV95
    raw.bsadf_stat, raw.bsadf_cv90, raw.bsadf_cv95 = END_1986, END_CV90, END_CV95
    raw.bsadf_argmax, raw.bsadf_n = 126, 443
    raw.gsadf_as_of = "2026-07"
    return raw


class TestTheArtifactOwnsTheChoice:
    def test_the_scored_statistic_is_the_endpoint(self):
        assert M.get_path("gsadf", "statistic") == "bsadf_endpoint"

    def test_no_setting_can_move_it(self):
        # A scored value must not move by configuration (see the removal of the
        # GSADF_CONTESTED_ASYMMETRIC runtime binding). The only lever is the
        # SHA-pinned artifact, which fails the byte guard until re-pinned.
        from app.config import Settings
        assert not [f for f in Settings.model_fields if "statistic" in f.lower()]

    def test_the_selection_reads_the_artifact_not_the_raw_order(self, frozen_statistic):
        raw = _raw_diverging()
        assert scored_s4_statistic(raw) == (END_1986, END_CV90, END_CV95)
        frozen_statistic("gsadf_sup")
        assert scored_s4_statistic(raw) == (SUP_1986, SUP_CV90, SUP_CV95)


class TestTheDivergenceIsScored:
    def test_the_endpoint_verdict_wins_end_to_end(self):
        # Non-contested so the mapping is visible: the endpoint is below cv90 ->
        # SUB_NULL. Had the sup been scored this would be 1.0 (above its cv95).
        snap = compute_snapshot(_raw_diverging(), gsadf_contested=False)
        assert snap.indicators["s4"].sub_score == 0.05
        assert snap.indicators["s4"].value == END_1986

    def test_the_red_flag_reads_the_scored_statistic(self):
        # The sup rejects at 5%. If red flag #1 still read the sup, a
        # non-contested run would fire an explosiveness flag while the scored
        # sub-score says "not explosive" — the exact incoherence v4.0 removes.
        snap = compute_snapshot(_raw_diverging(), gsadf_contested=False)
        assert snap.red_flags.gsadf_explosive_noncontested is False

    def test_scoring_the_sup_would_have_flagged_the_present(self, frozen_statistic):
        # Pins the counterfactual, so the regression this change prevents is
        # itself under test rather than only described in a comment.
        frozen_statistic("gsadf_sup")
        snap = compute_snapshot(_raw_diverging(), gsadf_contested=False)
        assert snap.indicators["s4"].sub_score == 1.0
        assert snap.red_flags.gsadf_explosive_noncontested is True

    def test_a_genuinely_explosive_endpoint_still_scores(self):
        raw = _raw_diverging()
        raw.bsadf_stat = END_CV95 + 0.01
        snap = compute_snapshot(raw, gsadf_contested=False)
        assert snap.indicators["s4"].sub_score == 1.0
        assert snap.red_flags.gsadf_explosive_noncontested is True


class TestFailClosed:
    def test_an_unknown_statistic_floors_and_does_not_guess(self, frozen_statistic):
        frozen_statistic("sadf")            # a real statistic, but not one we score
        assert scored_s4_statistic(_raw_diverging()) == (None, None, None)
        snap = compute_snapshot(_raw_diverging(), gsadf_contested=False)
        s4 = snap.indicators["s4"]
        assert (s4.state, s4.quality, s4.sub_score) == ("FLOOR", 0.0, 0.25)

    def test_a_missing_endpoint_floors_rather_than_falling_back_to_the_sup(self):
        raw = _raw_diverging()
        raw.bsadf_stat = raw.bsadf_cv90 = raw.bsadf_cv95 = None
        snap = compute_snapshot(raw, gsadf_contested=False)
        s4 = snap.indicators["s4"]
        assert (s4.state, s4.quality, s4.sub_score) == ("FLOOR", 0.0, 0.25)

    def test_a_misaligned_cv_pair_floors(self):
        raw = _raw_diverging()
        raw.bsadf_cv90, raw.bsadf_cv95 = END_CV95, END_CV90     # inverted
        assert compute_snapshot(raw, gsadf_contested=False).indicators["s4"].state == "FLOOR"


class TestPartialMetadataNeverCrashes:
    """Both cases below were raised by the cross-vendor review panel on PR #77
    and reproduced before being fixed."""

    def test_a_missing_sup_cv_does_not_crash_the_snapshot(self):
        # _s4_ok constrains the SCORED pair only. The reported-only sup pair can
        # be absent, and the note builder formatted it unconditionally with
        # :.4f — TypeError on None, i.e. an HTTP 500 from a data gap (guardrail 5).
        raw = _raw_diverging()
        raw.gsadf_cv90 = None
        snap = compute_snapshot(raw)
        s4 = snap.indicators["s4"]
        assert s4.state == "COMPUTED"                    # the scored pair is intact
        assert "BSADF@endpoint" in s4.note               # scored read still named
        assert "GSADF sup" not in s4.note                # absent half simply omitted

    def test_a_missing_sup_statistic_does_not_crash_either(self):
        raw = _raw_diverging()
        raw.gsadf_stat = raw.gsadf_cv90 = raw.gsadf_cv95 = None
        assert compute_snapshot(raw).indicators["s4"].state == "COMPUTED"

    def test_the_note_labels_follow_the_selection_not_a_hardcoded_string(
            self, frozen_statistic):
        """Panel finding #3: the labels were hardcoded, so in gsadf_sup mode the
        note called the SCORED statistic "reported, not scored" and the
        reported-only one "scored", while s4.value carried the right number."""
        frozen_statistic("gsadf_sup")
        s4 = compute_snapshot(_raw_diverging()).indicators["s4"]
        assert s4.value == SUP_1986
        assert f"scored GSADF sup {SUP_1986:.4f}" in s4.note
        assert f"BSADF@endpoint {END_1986:.4f} (cv90 {END_CV90:.4f}) reported, not scored" in s4.note
        assert "scored BSADF@endpoint" not in s4.note

    def test_the_note_labels_are_right_in_the_production_mode_too(self):
        s4 = compute_snapshot(_raw_diverging()).indicators["s4"]
        assert s4.value == END_1986
        assert f"scored BSADF@endpoint {END_1986:.4f}" in s4.note
        assert f"GSADF sup {SUP_1986:.4f} (cv90 {SUP_CV90:.4f}) reported, not scored" in s4.note
        assert "scored GSADF sup" not in s4.note

    def test_a_degenerate_cv_pair_cannot_fire_the_red_flag(self):
        # explosive_p05 compares stat > cv95 and never inspects cv90, so an
        # inverted CV pair floored the sub-score while the flag still fired off
        # the same unusable numbers. Pre-existing on main; closed here because
        # this change claims flag and score cannot disagree.
        raw = _raw_diverging()
        raw.bsadf_stat = 3.0
        raw.bsadf_cv90, raw.bsadf_cv95 = 2.0, 1.5        # inverted
        snap = compute_snapshot(raw, gsadf_contested=False)
        assert snap.indicators["s4"].state == "FLOOR"
        assert snap.red_flags.gsadf_explosive_noncontested is False

    def test_a_valid_rejection_still_fires_the_red_flag(self):
        # The gate must not silence a genuine signal.
        raw = _raw_diverging()
        raw.bsadf_stat = END_CV95 + 0.5
        snap = compute_snapshot(raw, gsadf_contested=False)
        assert snap.indicators["s4"].state == "COMPUTED"
        assert snap.red_flags.gsadf_explosive_noncontested is True


class TestTheReportCarriesBoth:
    def test_both_statistics_are_reported(self):
        extra = compute_snapshot(_raw_diverging()).indicators["s4"].extra
        assert extra["s4_statistic"]["scored"] == "bsadf_endpoint"
        assert extra["s4_statistic"]["bsadf_endpoint"]["stat"] == END_1986
        assert extra["s4_statistic"]["gsadf_sup"]["stat"] == SUP_1986
        assert extra["s4_statistic"]["sup_argmax_index"] == 126


class TestRunnerContract:
    @staticmethod
    def _run_with(monkeypatch, payload):
        import subprocess

        from app.engine import gsadf_runner as gr

        class _P:
            stdout, stderr, returncode = json.dumps(payload), "", 0

        monkeypatch.setattr(gr.shutil, "which", lambda _: "/usr/bin/Rscript")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
        return gr.run([1.0, 2.0, 3.0], timeout_s=5)

    def test_endpoint_fields_are_parsed(self, monkeypatch):
        out = self._run_with(monkeypatch, {
            "gsadf": SUP_1986, "cv90": SUP_CV90, "cv95": SUP_CV95,
            "bsadf": END_1986, "bsadf_cv90": END_CV90, "bsadf_cv95": END_CV95,
            "bsadf_n": 443, "bsadf_argmax": 126})
        assert (out.bsadf, out.bsadf_cv90, out.bsadf_cv95) == (END_1986, END_CV90, END_CV95)
        assert (out.bsadf_n, out.bsadf_argmax) == (443, 126)

    def test_null_endpoint_fields_degrade_to_none(self, monkeypatch):
        # R emits null when exuber returns no usable sequence, or when the CV
        # matrix is not row-aligned with it. That must not raise.
        out = self._run_with(monkeypatch, {
            "gsadf": SUP_1986, "cv90": SUP_CV90, "cv95": SUP_CV95,
            "bsadf": None, "bsadf_cv90": None, "bsadf_cv95": None,
            "bsadf_n": None, "bsadf_argmax": None})
        assert out.gsadf == SUP_1986
        assert (out.bsadf, out.bsadf_cv90, out.bsadf_cv95) == (None, None, None)


class TestTheRScriptReturnsTheEndpointNotTheSup:
    """Contract test against real exuber. The Python layer can select the right
    field and still be wrong if r/gsadf.R puts the SUP in it, or pairs the
    endpoint with the wrong CV row — mutations no Python test can see, because R
    is the thing producing the numbers.

    SKIPS where R/exuber is absent, which includes CI's unit job (CI installs R
    only for the image build). It runs on a developer host and inside the
    container, which is where the R contract can actually be exercised.
    """

    NREP, SEED = 40, 20260711

    @staticmethod
    def _require_r():
        import shutil
        import subprocess
        if shutil.which("Rscript") is None or subprocess.run(
                ["Rscript", "-e", "library(exuber)"], capture_output=True).returncode != 0:
            pytest.skip("R/exuber not available (CI unit job installs R only for the image build)")

    @staticmethod
    def _explosive_then_calm():
        """Deterministic: linear drift, an explosive burst, then a flat tail — so
        the sup is INTERIOR and the endpoint is demonstrably not the maximum."""
        import math
        import random
        rng = random.Random(20260711)
        y, level = [], 100.0
        for t in range(240):
            if t < 120:
                level += 0.15 + rng.uniform(-0.3, 0.3)
            elif t < 160:
                level *= 1.035 + rng.uniform(-0.002, 0.002)      # explosive
            else:
                level += rng.uniform(-0.4, 0.4)                   # calm tail
            y.append(level)
        return [math.log(v) for v in y]

    def _run(self, series, cache_dir):
        import json as _json
        import os
        import subprocess
        proc = subprocess.run(
            ["Rscript", "r/gsadf.R"],
            input=_json.dumps({"series": series,
                               # small nrep: these assertions are structural, not
                               # about CV levels, and the cache key includes nrep
                               # so this cannot collide with a production cache.
                               "params": {"lag": 0, "mc_nrep": self.NREP, "mc_seed": self.SEED}}),
            capture_output=True, text=True, timeout=600,
            env={**os.environ, "GSADF_CV_CACHE": str(cache_dir)})
        assert proc.returncode == 0, proc.stderr[-800:]
        return _json.loads(proc.stdout)

    def _r_json(self, snippet, tmp_path):
        import json as _json
        import subprocess
        proc = subprocess.run(["Rscript", "-e", snippet], capture_output=True,
                              text=True, timeout=600)
        assert proc.returncode == 0, proc.stderr[-800:]
        return _json.loads(proc.stdout[proc.stdout.index("{"):])

    def test_the_emitted_statistic_is_the_endpoint(self, tmp_path):
        import math
        self._require_r()
        series = self._explosive_then_calm()
        out = self._run(series, tmp_path)
        n = len(series)

        # Ask exuber for its OWN minimum window rather than re-deriving it here.
        # The rounding convention is not obvious and re-deriving it invites an
        # off-by-one dispute that cannot be settled by reading the test: at
        # n = 487, n*(0.01 + 1.8/sqrt(n)) = 44.5925, so floor gives 44 and round
        # gives 45. exuber::psy_minw is the authority, and it returns 44.
        minw = self._r_json('library(exuber); library(jsonlite); '
                            f'cat(toJSON(list(minw = psy_minw({n})), auto_unbox = TRUE))',
                            tmp_path)["minw"]
        assert out["bsadf_n"] == n - minw          # sequence spans endpoints only
        # The documented rule (frozen minw_rule) must agree with what exuber does.
        assert minw == math.floor(n * (0.01 + 1.8 / math.sqrt(n)))

        # The sup is interior by construction...
        assert out["bsadf_argmax"] < out["bsadf_n"]
        # ...so the endpoint value must be STRICTLY below the sup. `gsadf` is by
        # definition the maximum of the BSADF sequence, so emitting max(bs) in the
        # bsadf field — i.e. silently restoring v3 behaviour — makes these equal.
        assert out["bsadf"] < out["gsadf"]

    def test_the_endpoint_is_paired_with_the_LAST_cv_row(self, tmp_path):
        """radf_mc_cv is seeded, so an independent run reproduces the matrix
        exactly and the emitted pair can be compared to a named row."""
        self._require_r()
        series = self._explosive_then_calm()
        out = self._run(series, tmp_path)
        rows = self._r_json(
            f'library(exuber); library(jsonlite); set.seed({self.SEED}); '
            f'b <- radf_mc_cv(n = {len(series)}, nrep = {self.NREP})$bsadf_cv; '
            'cat(toJSON(list(first = as.numeric(b[1, c("90%","95%")]), '
            'last = as.numeric(b[nrow(b), c("90%","95%")]))))', tmp_path)
        assert [out["bsadf_cv90"], out["bsadf_cv95"]] == pytest.approx(rows["last"])
        # The rows must actually differ, or the assertion above proves nothing.
        assert rows["first"] != pytest.approx(rows["last"])

    def test_a_misaligned_cached_cv_matrix_yields_null_not_a_wrong_row(self, tmp_path):
        """A stale or truncated cache must NOT be silently compared against. The
        guard emits null CVs, and Python then floors s4 at the contested 0.25."""
        self._require_r()
        series = self._explosive_then_calm()
        n = len(series)
        path = tmp_path / f"mc_cv_n{n}_nrep{self.NREP}_seed{self.SEED}.rds"
        self._r_json(
            f'library(exuber); library(jsonlite); set.seed({self.SEED}); '
            f'cv <- radf_mc_cv(n = {n}, nrep = {self.NREP}); '
            'cv$bsadf_cv <- cv$bsadf_cv[1:10, , drop = FALSE]; '        # misaligned
            f'saveRDS(cv, "{path}"); cat(toJSON(list(ok = TRUE)))', tmp_path)
        out = self._run(series, tmp_path)
        assert out["bsadf"] is not None            # the statistic still computes
        assert out["bsadf_cv90"] is None and out["bsadf_cv95"] is None

    def test_the_argmax_identifies_the_sup(self, tmp_path):
        """The reported index is a PROVENANCE claim — it is what dates the sup
        to a particular window — so it is pinned against exuber's own sequence,
        not merely range-checked."""
        import json as _json
        self._require_r()
        series = self._explosive_then_calm()
        out = self._run(series, tmp_path)
        sf = tmp_path / "series.json"
        sf.write_text(_json.dumps({"series": series}))
        ref = self._r_json(
            'library(exuber); library(jsonlite); '
            f'y <- fromJSON("{sf}")$series; b <- as.numeric(radf(y, lag = 0L)$bsadf); '
            'cat(toJSON(list(argmax = which.max(b), n = length(b), '
            'maxv = max(b), lastv = b[length(b)]), auto_unbox = TRUE))', tmp_path)
        assert out["bsadf_argmax"] == ref["argmax"]        # an off-by-one dies here
        assert out["bsadf_n"] == ref["n"]
        assert out["bsadf"] == pytest.approx(ref["lastv"])
        assert out["gsadf"] == pytest.approx(ref["maxv"])

    def test_a_degenerate_series_nulls_the_statistic_and_never_crashes(self, tmp_path):
        """Zero variance: exuber's recursion is undefined. R must exit 0 with a
        null statistic (guardrail 5 — never surface a data fault as a crash);
        Python then floors s4 at the contested 0.25."""
        import math
        self._require_r()
        out = self._run([math.log(100.0)] * 240, tmp_path)
        assert out["gsadf"] is None
        assert out["bsadf"] is None
