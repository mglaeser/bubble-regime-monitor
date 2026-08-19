"""Regression: the Fed EBP `date` column is a vendor spelling, not a contract.

On 2026-08-06 the live file switched from `YYYY-MM` to unpadded US `M/D/YYYY`.
The parser passed the string through verbatim, so the consumer's `date[:7]`
month slice became `"1/1/202"` and `int("1/1/")` raised — outside any source
error boundary, which aborted the whole recompute. No snapshot was written for
twelve days while /healthz, /readyz and the daily digest all still answered
from the last good one.

Two independent things are pinned here, because fixing only the first leaves a
worse bug than the crash:

  1. the crash — a month slice must be a real `YYYY-MM`;
  2. the ORDER — US dates sort lexicographically to 9/1/2025, not 7/1/2026, so
     a parser that merely stopped raising would have scored S5 off an
     11-month-stale tail and said nothing.
"""

from __future__ import annotations

import pytest

from app.sources import SourceError
from app.sources.fed_ebp import _parse_ebp_csv, normalise_date

HEADER = "date,gz_spread,ebp,est_prob\n"


def _rows(dates: list[str], ebp: float = -0.25) -> str:
    """A CSV body long enough to clear the parser's 24-row floor."""
    return "".join(f"{d},1.03,{ebp},0.12\n" for d in dates)


def _us_months(year: int) -> list[str]:
    return [f"{m}/1/{year}" for m in range(1, 13)]


class TestNormaliseDate:
    @pytest.mark.parametrize(("raw", "expected"), [
        ("1/1/1973", "1973-01-01"),      # the live shape, unpadded
        ("12/1/2025", "2025-12-01"),
        ("7/1/2026", "2026-07-01"),
        (" 3/1/2026 ", "2026-03-01"),    # surrounding whitespace
        ("2026-05", "2026-05-01"),       # the historical shape
        ("2026-05-15", "2026-05-15"),    # day precision is preserved, not dropped
    ])
    def test_both_vendor_shapes_normalise_to_iso(self, raw, expected):
        assert normalise_date(raw) == expected

    @pytest.mark.parametrize("raw", [
        "13/1/2026",     # 13 cannot be a month: refused, never transposed to D/M
        "2/30/2026",     # impossible day
        "2026/05/01",    # not a shape this adapter claims to read
        "May 2026",
        "",
        "   ",
    ])
    def test_unreadable_shapes_return_none(self, raw):
        assert normalise_date(raw) is None


class TestParserOnTheLiveFormat:
    def test_parses_us_slash_dates(self):
        pairs = _parse_ebp_csv(HEADER + _rows(_us_months(2024) + _us_months(2025)))
        assert len(pairs) == 24
        assert pairs[0][0] == "2024-01-01"

    def test_orders_chronologically_not_lexicographically(self):
        """The bug a crash-only fix would have left behind.

        Sorted as strings, "9/1/2025" is the maximum of this set and "7/1/2026"
        lands mid-list — so S5's freshest reading would silently be eleven
        months old, with no staleness flag to show for it."""
        dates = _us_months(2025) + ["1/1/2026", "5/1/2026", "7/1/2026"]
        pairs = _parse_ebp_csv(HEADER + _rows(dates) + _rows(_us_months(2024)))
        assert pairs[-1][0] == "2026-07-01"
        assert pairs == sorted(pairs, key=lambda p: p[0])

    def test_month_slice_is_a_real_month(self):
        """The exact expression that raised in production.

        `app.services.compute._month_end_iso` slices `date[:7]` and calls
        `int()` on each half; `"1/1/2026"[:7]` is `"1/1/202"`."""
        from app.services.compute import _month_end_iso

        pairs = _parse_ebp_csv(HEADER + _rows(_us_months(2025) + _us_months(2026)))
        for iso_date, _ in pairs:
            month = iso_date[:7]
            assert int(month[:4]) >= 1973
            assert 1 <= int(month[5:7]) <= 12
        assert _month_end_iso(pairs[-1][0][:7]) == "2026-12-31"

    def test_iso_and_us_rows_normalise_to_one_scale(self):
        """A file mid-migration must not sort into two interleaved groups."""
        pairs = _parse_ebp_csv(
            HEADER + _rows([f"2025-{m:02d}" for m in range(1, 13)]) + _rows(_us_months(2026)))
        assert pairs[0][0] == "2025-01-01"
        assert pairs[-1][0] == "2026-12-01"
        assert pairs == sorted(pairs, key=lambda p: p[0])


class TestUnreadableFormatDegradesInsteadOfCrashing:
    def test_a_wholly_unknown_date_format_fails_the_fetch(self):
        """Guardrail #5: S5 falls back to the BAA proxy. The failure has to
        surface as SourceError here — the one exception type the gather's
        per-source boundary is built to absorb — rather than as a ValueError
        escaping from a consumer three layers up."""
        with pytest.raises(SourceError) as exc:
            _parse_ebp_csv(HEADER + _rows([f"Jan {y}" for y in range(1990, 2026)]))
        # A wholly unknown format is also a format change reaching the newest
        # rows, so it now trips the tail guard before the 24-row floor. Either
        # message is a correct refusal; both must name the unreadable dates,
        # because "the Fed is down" and "the Fed changed the format again" need
        # different responses from the operator.
        assert "unreadable" in str(exc.value)

    def test_one_bad_row_never_costs_the_series(self):
        dates = _us_months(2024) + ["not-a-date"] + _us_months(2025)
        pairs = _parse_ebp_csv(HEADER + _rows(dates))
        assert len(pairs) == 24

    def test_blank_and_na_ebp_rows_are_still_skipped(self):
        body = _rows(_us_months(2024) + _us_months(2025))
        body += "6/1/2026,1.02,NA,0.12\n7/1/2026,1.02,,0.12\n"
        pairs = _parse_ebp_csv(HEADER + body)
        assert [d for d, _ in pairs if d.startswith("2026")] == []


class TestAPartialFormatChangeFailsOver:
    """The dangerous shape is a format change that only affects RECENT rows.

    The Fed appends, so unreadable rows at the end of the file mean the current
    months are being dropped while a fifty-year legacy tail sails past the
    24-row floor — handing S5 a silently stale series instead of failing over
    to the BAA proxy. Panel finding on #64 (combo/SOTA-A)."""

    def test_unreadable_rows_at_the_end_fail_the_fetch(self):
        body = _rows(_us_months(2024) + _us_months(2025))       # 24 readable
        body += _rows(["Jan 2026", "Feb 2026", "Mar 2026"])     # the new format
        with pytest.raises(SourceError) as exc:
            _parse_ebp_csv(HEADER + body)
        assert "most recent" in str(exc.value)

    def test_a_stray_bad_row_in_the_middle_is_still_tolerated(self):
        """One bad row among fifty years is not a format change."""
        body = _rows(_us_months(2024)) + _rows(["not-a-date"]) + _rows(_us_months(2025))
        pairs = _parse_ebp_csv(HEADER + body)
        assert len(pairs) == 24
        assert pairs[-1][0] == "2025-12-01"

    def test_the_live_shape_is_unaffected(self):
        pairs = _parse_ebp_csv(HEADER + _rows(_us_months(2025) + _us_months(2026)))
        assert pairs[-1][0] == "2026-12-01"
