import pandas as pd

from telemetry.parser import COLUMNS, load_raw, split_sessions

FIXTURE_PATH = "tests/fixtures/synthetic_session.tsv"


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
