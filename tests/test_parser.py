import os

import pandas as pd
import pytest

from telemetry.parser import COLUMNS, load_raw, split_sessions

FIXTURE_PATH = "tests/fixtures/synthetic_session.tsv"
# A real Danish-locale Unipro Analyser export (see sample_data/README.md) --
# some but not all channel headers come out translated (e.g. "Breddegrad"
# for Latitude), which is what COLUMN_ALIASES exists to undo.
DANISH_FIXTURE_PATH = "sample_data/danish_session.tsv"
# Committed separately from the code that reads it (a real person's GPS
# trace, so it goes in on its own), which means a checkout of this code can
# briefly not have it yet -- skip those two tests rather than fail the suite
# in that window.
_danish_fixture_available = os.path.exists(DANISH_FIXTURE_PATH)
_skip_without_danish_fixture = pytest.mark.skipif(
    not _danish_fixture_available, reason=f"{DANISH_FIXTURE_PATH} not committed yet"
)


def test_load_raw_has_all_columns_and_seconds():
    df = load_raw(FIXTURE_PATH)
    for col in COLUMNS:
        assert col in df.columns
    assert "session_time_s" in df.columns
    assert "lap_time_s" in df.columns
    # nanosecond -> second conversion sanity: lap times should be plausible for a kart lap
    assert df["lap_time_s"].max() < 120
    assert df["lap_time_s"].min() >= 0


def test_rows_are_sparse_single_channel_events():
    df = load_raw(FIXTURE_PATH)
    always_on = {"Start Date", "Start Time", "Lap Number", "Session Time", "Lap Time"}
    sparse_cols = [c for c in COLUMNS if c not in always_on]
    non_null_counts = df[sparse_cols].notna().sum(axis=1)
    # every row should carry at most a handful of populated sparse columns
    # (the GPS block fires ~11 columns together; RPM/GPS Distance/etc fire alone)
    assert non_null_counts.max() <= len(sparse_cols)
    assert (non_null_counts <= 11).all()
    # most rows should carry very few populated sparse columns, not all of them
    assert non_null_counts.mean() < 3


def test_never_populated_channels_are_all_nan():
    df = load_raw(FIXTURE_PATH)
    for col in ["Steering Rate", "Slip", "Inverse Corner Radius", "Time", "GPS Total Acceleration"]:
        assert df[col].notna().sum() == 0


def test_split_sessions_detects_reset():
    df = load_raw(FIXTURE_PATH)
    sessions = split_sessions(df, source_file=FIXTURE_PATH)
    assert len(sessions) == 2
    for s in sessions:
        # each split chunk's session_time_s should be non-decreasing (monotonic within a session)
        diffs = s.df["session_time_s"].diff().dropna()
        assert (diffs >= -1e-6).all()
        assert s.df["session_time_s"].iloc[0] == 0 or s.df["session_time_s"].iloc[0] < 1.0


def test_extract_channel_only_returns_populated_rows(session1):
    rpm = session1.extract_channel("RPM")
    assert rpm["RPM"].notna().all()
    assert len(rpm) < len(session1.df)


def test_align_channels_produces_common_frame(session1):
    aligned = session1.align_channels(["RPM", "GPS Speed"])
    assert "RPM" in aligned.columns
    assert "GPS Speed" in aligned.columns
    assert len(aligned) > 0


def test_gps_fixes_all_columns_populated_together(session1):
    fixes = session1.gps_fixes()
    from telemetry.parser import GPS_FIX_COLUMNS

    for col in GPS_FIX_COLUMNS:
        assert fixes[col].notna().all()


@_skip_without_danish_fixture
def test_load_raw_translates_danish_headers():
    df = load_raw(DANISH_FIXTURE_PATH)
    for col in COLUMNS:
        assert col in df.columns
    # A couple of the translated channels should actually carry data, not
    # just be present as empty columns from a name that happened to match.
    assert df["Latitude"].notna().any()
    assert df["RPM"].notna().any()


@_skip_without_danish_fixture
def test_load_raw_keeps_an_unmapped_extra_danish_column():
    # The real export this fixture comes from has one channel -- "Corner
    # Radius" -- that isn't in COLUMNS at all (distinct from the "Inverse
    # Corner Radius" channel, which is). It should survive untouched rather
    # than being dropped or colliding with anything the rename produces.
    df = load_raw(DANISH_FIXTURE_PATH)
    assert "Corner Radius" in df.columns
