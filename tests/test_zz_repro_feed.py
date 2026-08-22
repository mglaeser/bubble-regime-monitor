from __future__ import annotations
import json
import pytest
from app.services import dashboard_feed as df
from tests.test_dashboard_feed import (fake_tiingo, fake_fred, fake_td,
                                       fake_fear_greed, fake_imf_reserves)

SUP, SUP90, SUP95 = 2.6189, 2.0034, 2.2604
END, END90, END95 = 0.7562, 1.1769, 1.4315


@pytest.fixture()
def patched(monkeypatch):
    monkeypatch.setattr(df, "_tiingo_monthly", fake_tiingo)
    monkeypatch.setattr(df, "_fred_series", fake_fred)
    monkeypatch.setattr(df, "_td_series", fake_td)
    monkeypatch.setattr(df, "_fear_greed", fake_fear_greed)
    monkeypatch.setattr(df, "_imf_reserves", fake_imf_reserves)


def _raw():
    from tests.conftest import make_golden_raw_inputs
    raw = make_golden_raw_inputs()
    raw.gsadf_as_of = "2026-07"
    return raw


def test_case_a_diverging(isolated_db, patched):
    from app.services.compute import compute_snapshot
    raw = _raw()
    raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = SUP, SUP90, SUP95
    raw.bsadf_stat, raw.bsadf_cv90, raw.bsadf_cv95 = END, END90, END95
    raw.bsadf_argmax, raw.bsadf_n = 126, 443
    data = compute_snapshot(raw, mc_samples=500, mc_seed=20260711, gsadf_contested=False)
    feed = df.build_feed(raw, data)
    print("CASE A feed.gsadf =", json.dumps(feed["metrics"]["gsadf"]))
    s4 = data.indicators["s4"]
    print("CASE A s4.value =", s4.value, "sub=", s4.sub_score, "state=", s4.state)
    print("CASE A s4.note =", s4.note)
    print("CASE A red_flag1 =", data.red_flags.gsadf_explosive_noncontested)


def test_case_b_bsadf_none_sup_fine(isolated_db, patched):
    from app.services.compute import compute_snapshot
    raw = _raw()
    raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = SUP, SUP90, SUP95
    raw.bsadf_stat, raw.bsadf_cv90, raw.bsadf_cv95 = None, None, None
    data = compute_snapshot(raw, mc_samples=500, mc_seed=20260711)
    feed = df.build_feed(raw, data)
    print("CASE B feed.gsadf =", json.dumps(feed["metrics"]["gsadf"]))
    s4 = data.indicators["s4"]
    print("CASE B s4.value =", s4.value, "sub=", s4.sub_score, "state=", s4.state, "q=", s4.quality)
    print("CASE B s4.note =", s4.note)


def test_case_c_main_behaviour_reference(isolated_db, patched):
    # what main would publish for the same raw (sup only, no bsadf fields)
    from app.services.compute import compute_snapshot
    raw = _raw()
    raw.gsadf_stat, raw.gsadf_cv90, raw.gsadf_cv95 = SUP, SUP90, SUP95
    raw.bsadf_stat, raw.bsadf_cv90, raw.bsadf_cv95 = None, None, None
    data = compute_snapshot(raw, mc_samples=500, mc_seed=20260711, gsadf_contested=False)
    print("CASE C sub=", data.indicators["s4"].sub_score,
          "state=", data.indicators["s4"].state)
