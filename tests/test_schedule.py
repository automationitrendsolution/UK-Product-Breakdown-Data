"""Scheduled-job window, catch-up after missed runs, cron coverage and the run lock."""
from __future__ import annotations

import datetime as dt

import pytest

from app.schedule import AlreadyRunning, RunLock, report_window

D = dt.date


def test_window_is_the_last_three_full_days():
    assert report_window(D(2026, 10, 20)) == (D(2026, 10, 17), D(2026, 10, 19))


def test_window_catches_up_after_missed_runs():
    # Power BI has data up to 10 Oct; two runs were missed.
    assert report_window(D(2026, 10, 20), last_synced_end=D(2026, 10, 10)) == (D(2026, 10, 11), D(2026, 10, 19))


def test_catch_up_is_capped():
    assert report_window(D(2026, 10, 20), last_synced_end=D(2026, 1, 1), max_days=31) == (
        D(2026, 9, 19), D(2026, 10, 19))


def test_recent_sync_does_not_shrink_the_window():
    assert report_window(D(2026, 10, 20), last_synced_end=D(2026, 10, 19)) == (D(2026, 10, 17), D(2026, 10, 19))


def _cron_every_third_day(start: dt.date, end: dt.date):
    """Days on which `0 13 */3 * *` fires: day-of-month 1, 4, 7, ..., 31."""
    day = start
    while day <= end:
        if (day.day - 1) % 3 == 0:
            yield day
        day += dt.timedelta(days=1)


def test_cron_every_three_days_covers_every_day_for_two_years():
    covered: set[dt.date] = set()
    last_end = None
    for run_day in _cron_every_third_day(D(2027, 1, 1), D(2028, 12, 31)):  # 2028 is a leap year
        start, end = report_window(run_day, 3, last_end)
        covered.update(start + dt.timedelta(days=n) for n in range((end - start).days + 1))
        last_end = end
    first, last = D(2027, 1, 1) - dt.timedelta(days=3), D(2028, 12, 30)
    missing = [first + dt.timedelta(days=n) for n in range((last - first).days + 1)
               if first + dt.timedelta(days=n) not in covered]
    assert missing == []


def test_cron_coverage_survives_a_failed_run():
    covered: set[dt.date] = set()
    last_end = None
    for index, run_day in enumerate(_cron_every_third_day(D(2027, 3, 1), D(2027, 4, 30))):
        start, end = report_window(run_day, 3, last_end)
        if index == 3:
            continue  # this run failed: nothing reached Power BI
        covered.update(start + dt.timedelta(days=n) for n in range((end - start).days + 1))
        last_end = end
    days = [D(2027, 2, 26) + dt.timedelta(days=n) for n in range((D(2027, 4, 27) - D(2027, 2, 26)).days + 1)]
    assert [d for d in days if d not in covered] == []


def test_run_lock_blocks_a_second_run(tmp_path):
    path = tmp_path / ".lock"
    with RunLock(path):
        with pytest.raises(AlreadyRunning):
            with RunLock(path):
                pass
    with RunLock(path):  # released after the first run ends
        pass
