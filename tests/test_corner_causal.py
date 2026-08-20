"""Part 1 extraction tests: entry/apex/exit point detection against
hand-built traces with an independently controlled speed profile (which
point is the apex) and G-force profile (which points trigger the
braking/power-on/lateral-G gates)."""

import numpy as np
import pandas as pd

from telemetry.corner_causal import (
    corner_points_for_lap,
    detect_corner_complexes,
    extract_corner_points,
    three_zone_times,
)
from tests.synthetic_corner import build_lap_session, single_corner_segments, two_corner_segments


def _trace(distance, speed_kmh, lateral_g, lon_g):
    return pd.DataFrame(
        {
            "lap_distance_m": distance,
            "GPS Speed": speed_kmh,
            "GPS Lateral Acceleration": lateral_g,
            "GPS Longitudinal Acceleration": lon_g,
        }
    )


def test_extract_corner_points_finds_apex_entry_and_power_on_exit():
    distance = np.arange(0.0, 101.0, 1.0)
    speed = np.interp(distance, [0, 20, 40, 70, 100], [80, 80, 20, 80, 80])
    lon_g = np.interp(distance, [0, 19, 20, 39, 40, 41, 69, 70, 100], [0, 0, -0.6, -0.6, 0, 0.4, 0.4, 0, 0])
    lateral_g = np.interp(distance, [0, 14, 15, 74, 75, 100], [0, 0, -0.8, -0.8, 0, 0])
    trace = _trace(distance, speed, lateral_g, lon_g)

    segment_row = pd.Series({"label": "Corner 1", "kind": "corner", "start_m": 20.0, "end_m": 60.0})
    points = extract_corner_points(trace, segment_row, next_boundary_m=100.0)

    assert points is not None
    assert points.apex_distance_m == 40.0
    assert points.apex_speed_kmh == 20.0
    assert points.entry_distance_m == 20.0
    assert points.entry_is_estimated is False
    assert points.exit_gate_reason == "power_on"
    assert points.apex_distance_m < points.exit_distance_m < 60.0


def test_extract_corner_points_defaults_entry_when_no_braking_detected():
    distance = np.arange(0.0, 101.0, 1.0)
    speed = np.full_like(distance, 60.0)
    lon_g = np.zeros_like(distance)  # never crosses the braking threshold
    lateral_g = np.interp(distance, [0, 19, 20, 59, 60, 100], [0, 0, -0.5, -0.5, 0, 0])
    trace = _trace(distance, speed, lateral_g, lon_g)

    segment_row = pd.Series({"label": "Corner 1", "kind": "corner", "start_m": 20.0, "end_m": 60.0})
    points = extract_corner_points(trace, segment_row, next_boundary_m=100.0)

    assert points is not None
    assert points.entry_is_estimated is True
    assert points.entry_distance_m == 20.0  # defaults to the segment's own start


def test_extract_corner_points_falls_back_to_lateral_g_exit_gate():
    # No power-on phase at all (trailing-throttle exit) -- the exit gate
    # must come from lateral G dropping below threshold instead.
    distance = np.arange(0.0, 101.0, 1.0)
    speed = np.interp(distance, [0, 20, 40, 100], [80, 80, 30, 60])
    lon_g = np.interp(distance, [0, 19, 20, 39, 40, 100], [0, 0, -0.6, -0.6, 0, 0])
    lateral_g = np.interp(distance, [0, 19, 20, 69, 70, 100], [0, 0, -0.7, -0.7, 0, 0])
    trace = _trace(distance, speed, lateral_g, lon_g)

    segment_row = pd.Series({"label": "Corner 1", "kind": "corner", "start_m": 20.0, "end_m": 55.0})
    points = extract_corner_points(trace, segment_row, next_boundary_m=100.0)

    assert points is not None
    assert points.exit_gate_reason == "lateral_g"
    assert points.exit_distance_m == 70.0


def test_corner_points_for_lap_and_three_zone_times_single_corner():
    total = 250.0
    session = build_lap_session(
        total_distance_m=total,
        speed_breakpoints=([0, 45, 80, 130, 250], [120, 120, 50, 120, 120]),
        lon_g_breakpoints=([0, 44, 45, 79, 80, 81, 129, 130, 250], [0, 0, -0.6, -0.6, 0, 0.4, 0.4, 0, 0]),
        lateral_g_breakpoints=([0, 39, 40, 119, 120, 250], [0, 0, -0.8, -0.8, 0, 0]),
    )
    segments = single_corner_segments(corner_start_m=50.0, corner_end_m=110.0, total_m=total)

    points = corner_points_for_lap(session, 1, segments)
    assert len(points) == 1
    row = points.iloc[0]
    assert row["entry_distance_m"] < row["apex_distance_m"] < row["exit_distance_m"]

    zones = three_zone_times(session, 1, points)
    assert len(zones) == 1
    zrow = zones.iloc[0]
    assert zrow["zone_a_time_s"] > 0
    assert zrow["zone_b_time_s"] > zrow["zone_a_time_s"]  # zone B extends past zone A's apex boundary
    assert zrow["zone_c_time_s"] > 0  # a long clean straight follows


def test_detect_corner_complexes_singleton_for_well_separated_corner():
    total = 250.0
    session = build_lap_session(
        total_distance_m=total,
        speed_breakpoints=([0, 45, 80, 130, 250], [120, 120, 50, 120, 120]),
        lon_g_breakpoints=([0, 44, 45, 79, 80, 81, 129, 130, 250], [0, 0, -0.6, -0.6, 0, 0.4, 0.4, 0, 0]),
        lateral_g_breakpoints=([0, 39, 40, 119, 120, 250], [0, 0, -0.8, -0.8, 0, 0]),
    )
    segments = single_corner_segments(corner_start_m=50.0, corner_end_m=110.0, total_m=total)

    groups = detect_corner_complexes(session, 1, segments)
    assert groups == [["Corner 1"]]


def test_detect_corner_complexes_groups_linked_corners():
    total = 200.0
    session = build_lap_session(
        total_distance_m=total,
        speed_breakpoints=([0, 25, 45, 70, 75, 95, 110, 180, 200], [100, 100, 50, 50, 55, 50, 50, 100, 100]),
        lon_g_breakpoints=(
            [0, 19, 20, 44, 45, 107, 108, 180, 200],
            [0, 0, -0.6, -0.6, -0.1, -0.1, 0.4, 0.4, 0],
        ),
        lateral_g_breakpoints=([0, 29, 30, 109, 110, 200], [0, 0, -0.7, -0.7, 0, 0]),
    )
    segments = two_corner_segments(
        straight_end_m=30.0, corner1_end_m=70.0, corner2_start_m=75.0, corner2_end_m=110.0, total_m=total
    )

    groups = detect_corner_complexes(session, 1, segments)
    assert groups == [["Corner 1", "Corner 2"]]
