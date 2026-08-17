import numpy as np
import pandas as pd

from telemetry.corners import assign_segments, build_reference_segments, compute_curvature, lap_gps_trace
from telemetry.parser import Session


def test_lap_gps_trace_distance_starts_near_zero(session1):
    trace = lap_gps_trace(session1, 1)
    assert not trace.empty
    assert trace["lap_distance_m"].min() < 5
    assert trace["lap_distance_m"].is_monotonic_increasing or trace["lap_distance_m"].diff().dropna().min() >= -1e-6


def test_compute_curvature_adds_column(session1):
    trace = lap_gps_trace(session1, 1)
    trace = compute_curvature(trace)
    assert "curvature" in trace.columns
    assert trace["curvature"].notna().any()


def test_build_reference_segments_finds_track_shape(session1):
    segments = build_reference_segments(session1, reference_lap_number=1)
    assert not segments.empty
    corners = segments[segments["kind"] == "corner"]
    straights = segments[segments["kind"] == "straight"]
    # the synthetic track has 4 corners and 4 straights
    assert len(corners) == 4
    assert len(straights) == 4
    # segments should be contiguous and cover the lap without overlap
    ordered = segments.sort_values("start_m")
    assert (ordered["end_m"].to_numpy()[:-1] == ordered["start_m"].to_numpy()[1:]).all()


def test_assign_segments_labels_every_row_in_range(session1):
    segments = build_reference_segments(session1, reference_lap_number=1)
    trace = lap_gps_trace(session1, 1)
    labeled = assign_segments(trace, segments)
    in_range = labeled[
        (labeled["lap_distance_m"] >= segments["start_m"].min()) & (labeled["lap_distance_m"] < segments["end_m"].max())
    ]
    assert in_range["segment_label"].notna().all()


def test_normal_session_does_not_flag_speed_as_estimate(session1):
    # the synthetic fixture has a genuinely populated GPS Speed channel
    trace = lap_gps_trace(session1, 1)
    assert not trace["gps_speed_is_estimate"].any()


def test_lap_gps_trace_derives_speed_when_native_channel_absent():
    """Regression for a real export confirmed to populate Latitude/
    Longitude/Heading on every GPS fix but never GPS Speed itself -- the
    trace must fall back to deriving speed from GPS Distance rather than
    leaving it entirely NaN."""
    n = 60
    t = np.linspace(0, 6, n)  # 10 Hz GPS fixes over 6s
    distance = 25.0 * t  # constant 25 m/s = 90 km/h
    lat = 55.35 + t * 1e-5
    lon = 11.16 + t * 1e-5

    rows = []
    for i in range(n):
        rows.append(
            {
                "Start Date": "16-08-2026", "Start Time": "10:00:00",
                "Lap Number": 0, "Session Time": t[i] * 1e9, "Lap Time": t[i] * 1e9,
                "session_time_s": t[i], "lap_time_s": t[i],
                "Latitude": lat[i], "Longitude": lon[i], "Heading": 90.0,
                "Vertical Acceleration": 0.0, "GPS Speed": np.nan,
                "Horizontal DOP": 0.9, "GPS Lateral Acceleration": 0.0,
                "GPS Longitudinal Acceleration": 0.0, "Vertical DOP": 1.0,
                "Positional DOP": 1.0, "Altitude": 10.0,
                "GPS Distance": distance[i],
            }
        )
    df = pd.DataFrame(rows)
    session = Session(session_id=0, source_file="test", df=df)

    trace = lap_gps_trace(session, 0)
    assert trace["gps_speed_is_estimate"].all()
    assert trace["GPS Speed"].notna().mean() > 0.8
    # derived speed should land close to the true constant 90 km/h
    assert (trace["GPS Speed"].dropna() - 90.0).abs().median() < 10.0
