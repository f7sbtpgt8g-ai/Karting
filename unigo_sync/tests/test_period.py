"""Tests for core/period.py -- filename-based date parsing and the
sync-period cutoff/filter logic."""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from unigo_sync.core.period import (  # noqa: E402
    SYNC_PERIOD_ALL,
    SYNC_PERIOD_LAST_MONTH,
    SYNC_PERIOD_LAST_WEEK,
    SYNC_PERIOD_TODAY,
    cutoff_for,
    parse_session_datetime,
    session_in_period,
)


def test_parses_documented_filename_convention():
    # From findings.md's "Filename convention" section.
    assert parse_session_datetime("260829_1441_Barmosen GPS_AUSTIN.uni") == datetime(2026, 8, 29, 14, 41)


def test_parses_short_serial_style_name():
    assert parse_session_datetime("240218_1753_003625.uni") == datetime(2024, 2, 18, 17, 53)


def test_unparseable_name_returns_none():
    assert parse_session_datetime("not_a_device_filename.uni") is None
    assert parse_session_datetime("") is None


def test_invalid_embedded_date_returns_none_not_raises():
    # Matches the regex shape but month 13 doesn't exist.
    assert parse_session_datetime("261399_1441_Test.uni") is None


def test_cutoff_today_is_midnight():
    now = datetime(2026, 8, 29, 14, 41)
    assert cutoff_for(SYNC_PERIOD_TODAY, now) == datetime(2026, 8, 29, 0, 0)


def test_cutoff_last_week_is_seven_days_back():
    now = datetime(2026, 8, 29, 14, 41)
    assert cutoff_for(SYNC_PERIOD_LAST_WEEK, now) == datetime(2026, 8, 22, 14, 41)


def test_cutoff_last_month_is_thirty_days_back():
    now = datetime(2026, 8, 29, 14, 41)
    assert cutoff_for(SYNC_PERIOD_LAST_MONTH, now) == datetime(2026, 7, 30, 14, 41)


def test_cutoff_all_is_none():
    assert cutoff_for(SYNC_PERIOD_ALL) is None


def test_session_in_period_excludes_older_session():
    cutoff = datetime(2026, 8, 29, 0, 0)
    assert session_in_period("260828_2359_Barmosen_AUSTIN.uni", cutoff) is False
    assert session_in_period("260829_0001_Barmosen_AUSTIN.uni", cutoff) is True


def test_session_in_period_with_no_cutoff_includes_everything():
    assert session_in_period("240101_0000_x.uni", None) is True


def test_session_in_period_keeps_unparseable_names_regardless_of_cutoff():
    cutoff = datetime(2026, 8, 29, 0, 0)
    assert session_in_period("weird_device_export.uni", cutoff) is True
