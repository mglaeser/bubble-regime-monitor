"""V is the ratio of two CBOE quotes of ONE New York session (README "Data
sources & freshness SLAs"; app/references.py REGISTRY["v"]: ratio = VIX / VIX3M).

CBOE publishes VIX from the early New York morning and VIX3M in the regular
session only. Production, the 222 recomputes logged from 2026-10-05 to 11-10:
on 33 weekday runs before the open VIX had moved since the run before and VIX3M
had not, ten of them carried another state than the last pair of one session,
and one sent an alert (2026-11-10 14:05 UTC).

No test here may reach a provider: the suite has no network guard, and a run
with nothing kept measures. `_no_provider` closes both branches of the chain
for every test; `_quotes` then names the two CBOE quotes a test measures."""

from datetime import UTC, date, datetime

import pytest


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_provider(monkeypatch):
    from app.sources import SourceError, vix

    def closed(*args, **kwargs):
        raise SourceError("no provider in this test")

    monkeypatch.setattr(vix, "_cboe_last", closed)
    monkeypatch.setattr(vix, "fred_latest", closed)


def _quotes(monkeypatch, vix_price, vix3m_price):
    from app.sources import vix

    prices = {vix.CBOE_VIX_URL: vix_price, vix.CBOE_VIX3M_URL: vix3m_price}
    asked: list[str] = []

    def last(url, source):
        asked.append(url)
        return prices[url]

    monkeypatch.setattr(vix, "_cboe_last", last)
    return asked


def _persist_reading(reading):
    """A previous snapshot that kept `reading`."""
    from app.db import session_scope
    from app.models import Snapshot

    with session_scope() as session:
        session.add(Snapshot(
            computed_at=datetime(2026, 11, 9, 22, 0, tzinfo=UTC), service_version="t",
            median=50.0, iqr_lo=48.0, iqr_hi=52.0, band5=45.0, band95=55.0, point_score=50.0,
            action_band="trim", data_freshness={"_v_reading": reading}))


def _persist_run(ratio, as_of="2026-11-09", source="cboe_delayed"):
    """compute_snapshot + persist_snapshot of a run whose V reading is `ratio`."""
    from app.services import compute
    from tests.conftest import make_golden_raw_inputs

    raw = make_golden_raw_inputs()
    raw.vix_ratio, raw.vix_as_of, raw.vix_ratio_source = ratio, as_of, source
    data = compute.compute_snapshot(raw, mc_samples=1_000, mc_seed=20260711,
                                    gsadf_contested=True)
    return compute.persist_snapshot(data, raw)


def _kept():
    from app.services import compute

    reading = compute._last_v_reading()
    if reading is None:
        return None
    return reading.value, reading.provenance.as_of, reading.provenance.source


@pytest.mark.parametrize(("now", "session"), [
    ("2026-11-10T02:00:45", date(2026, 11, 9)),    # 21:00 New York: both closed
    ("2026-11-10T06:00:45", None),                 # 01:00: a new day, no session yet
    ("2026-11-10T10:00:45", None),                 # 05:00: VIX alone is published
    ("2026-11-10T14:00:45", None),                 # 09:00 winter time, before the open
    ("2026-11-10T18:00:45", date(2026, 11, 10)),   # 13:00: both live
    ("2026-11-10T22:00:45", date(2026, 11, 10)),   # 17:00: both closed
    ("2026-10-20T14:00:45", date(2026, 10, 20)),   # 10:00 summer time: both live
    ("2026-11-02T14:00:45", None),                 # 09:00, the first Monday of winter time
    ("2026-11-07T18:00:45", None),                 # Saturday
    ("2026-11-26T18:00:45", None),                 # Thanksgiving
    ("2026-11-27T18:00:45", date(2026, 11, 27)),   # the half day after it: 13:00
])
def test_the_pair_is_of_one_session_only_from_the_open_to_midnight_new_york(now, session):
    from app.sources import vix

    assert vix.session_of_pair(_utc(now)) == session


def test_before_the_open_the_run_keeps_the_last_sessions_reading(isolated_db, monkeypatch):
    """Production, 2026-11-10 14:00 UTC (09:00 New York): VIX 21.06 against
    the 20.85 VIX3M closed at the evening before read 1.0101, backwardation,
    and "Terminstruktur in Backwardation" went out at 14:05. The last pair of
    one session was 19.92 / 20.85 = 0.9554, flat; at 18:00 both live: 0.965."""
    from app.indicators import v_vix
    from app.services import compute

    asked = _quotes(monkeypatch, 21.06, 20.85)
    _persist_reading({"ratio": 19.92 / 20.85, "as_of": "2026-11-09", "source": "cboe_delayed"})
    reading = compute._v_reading(_utc("2026-11-10T14:00:45"))
    assert v_vix.state(reading.value) == "flat"
    assert reading.value == pytest.approx(0.9554, abs=5e-5)
    assert reading.provenance.as_of == "2026-11-09"
    assert reading.provenance.source == "cboe_delayed"
    assert asked == []


def test_in_the_session_the_run_measures_and_dates_the_pair(isolated_db, monkeypatch):
    from app.services import compute
    from app.sources import vix

    asked = _quotes(monkeypatch, 20.98, 21.74)
    _persist_reading({"ratio": 1.0101, "as_of": "2026-11-09", "source": "cboe_delayed"})
    reading = compute._v_reading(_utc("2026-11-10T18:00:45"))
    assert reading.value == pytest.approx(20.98 / 21.74)
    assert reading.provenance.as_of == "2026-11-10"
    assert asked == [vix.CBOE_VIX_URL, vix.CBOE_VIX3M_URL]


def test_with_nothing_kept_the_run_measures(isolated_db, monkeypatch):
    """A first run, or the run after a failed measurement: there is no reading
    to keep, so the run measures as every run did before, undated."""
    from app.services import compute

    _quotes(monkeypatch, 18.0, 20.0)
    reading = compute._v_reading(_utc("2026-11-10T10:00:45"))
    assert reading.value == pytest.approx(0.9)
    assert reading.provenance.as_of is None


def test_the_session_dates_the_cboe_pair_only(monkeypatch):
    """With CBOE down in the session the FRED pair answers, and it carries
    FRED's own observation date, not the session's."""
    from app.sources import Provenance, SourceResult, vix

    monkeypatch.setattr(vix, "fred_latest", lambda series_id: SourceResult(
        {"VIXCLS": 19.92, "VIX3M": 20.85}[series_id],
        Provenance(source=f"fred_{series_id}", as_of="2026-11-09")))
    reading = vix.term_structure_ratio(date(2026, 11, 10))
    assert reading.provenance.source == "fred_vix_ratio"
    assert reading.provenance.as_of == "2026-11-09"


def test_a_snapshot_keeps_the_reading_it_was_scored_with(isolated_db, monkeypatch):
    """The kept reading is the one the snapshot was scored with, and a run
    scored with a kept reading keeps the same one again: it survives any number
    of runs between two sessions and its date stays the measuring session's."""
    from app.services import compute

    measured = (0.94, "2026-11-09", "cboe_delayed")
    _persist_run(*measured)
    assert _kept() == measured

    asked = _quotes(monkeypatch, 21.06, 20.85)
    for now in ("2026-11-10T06:00:45", "2026-11-10T10:00:45", "2026-11-10T14:00:45"):
        carried = compute._v_reading(_utc(now))
        _persist_run(carried.value, carried.provenance.as_of, carried.provenance.source)
        assert _kept() == measured
    assert asked == []


@pytest.mark.parametrize("ratio", [None, float("nan"), 0.0])
def test_a_run_without_a_usable_ratio_keeps_no_reading(isolated_db, ratio):
    """Usable is what v_vix.state accepts. Nothing is carried across a failed
    measurement: the next run finds nothing kept and measures."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Snapshot

    _persist_run(0.94)
    snapshot_id = _persist_run(ratio)
    with session_scope() as session:
        stored = session.execute(
            select(Snapshot.data_freshness).where(Snapshot.id == snapshot_id)).scalar_one()
    assert "_v_reading" not in stored
    assert _kept() is None


def test_the_recompute_reads_v_through_the_session_rule():
    """gather_inputs needs every provider, so its wiring is pinned by source
    (as tests/test_gather_error_boundary.py does): the reading comes from
    _v_reading, inside _track, so a kept reading is one source_health row too."""
    import inspect

    from app.services import compute

    src = inspect.getsource(compute.gather_inputs)
    assert '_track(raw, "vix_term_structure", lambda: _v_reading(datetime.now(UTC)))' in src
    assert "term_structure_ratio" not in src
