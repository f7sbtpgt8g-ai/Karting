from telemetry.comparison import corner_comparison_across_sessions
from telemetry.corners import build_reference_segments, lap_gps_trace, segment_midpoints
from telemetry.laps import clean_lap_table, flag_outlier_laps, lap_table


def _session_data(session, label):
    laps = flag_outlier_laps(lap_table(session))
    clean = clean_lap_table(laps)
    clean_lap_numbers = clean["lap_number"].tolist()
    best_lap = int(clean.loc[clean["lap_time_s"].idxmin(), "lap_number"])
    segments = build_reference_segments(session, best_lap)
    trace = lap_gps_trace(session, best_lap)
    midpoints = segment_midpoints(trace, segments)
    return label, session, segments, midpoints, clean_lap_numbers


def _corner_1_midpoint(session_data):
    _, _, _, midpoints, _ = session_data
    row = midpoints[midpoints["segment_kind"] == "corner"].iloc[0]
    return row["mid_lat"], row["mid_lon"]


def test_corner_comparison_returns_rows_for_each_session(session1, session2):
    data1 = _session_data(session1, "Session 1")
    data2 = _session_data(session2, "Session 2")
    ref_lat, ref_lon = _corner_1_midpoint(data1)

    result = corner_comparison_across_sessions([data1, data2], ref_lat, ref_lon)

    assert not result.empty
    assert set(result["session_label"]) == {"Session 1", "Session 2"}
    expected_cols = {
        "session_label", "lap_number", "corner_time_s",
        "entry_speed_kmh", "entry_rpm", "apex_speed_kmh", "apex_rpm", "exit_speed_kmh", "exit_rpm",
    }
    assert expected_cols.issubset(result.columns)
    assert (result["corner_time_s"] > 0).all()


def test_corner_comparison_all_time_best_is_min_of_per_session_bests(session1, session2):
    data1 = _session_data(session1, "Session 1")
    data2 = _session_data(session2, "Session 2")
    ref_lat, ref_lon = _corner_1_midpoint(data1)

    result = corner_comparison_across_sessions([data1, data2], ref_lat, ref_lon)

    all_time_best = result["corner_time_s"].min()
    per_session_bests = result.groupby("session_label")["corner_time_s"].min()
    assert abs(all_time_best - per_session_bests.min()) < 1e-9


def test_corner_comparison_skips_sessions_with_no_corner_in_range(session1):
    data1 = _session_data(session1, "Session 1")
    # A reference point nowhere near the track should match nothing.
    result = corner_comparison_across_sessions([data1], reference_lat=0.0, reference_lon=0.0)
    assert result.empty
