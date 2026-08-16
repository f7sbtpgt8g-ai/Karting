from telemetry.metrics import (
    add_braking_throttle_estimates,
    braking_zones,
    consistency_stats,
    gg_diagram_points,
    lap_metric_trace,
    segment_aggregates,
)
from telemetry.corners import build_reference_segments
from telemetry.laps import flag_outlier_laps, lap_table


def test_lap_metric_trace_has_speed_and_rpm(session1):
    trace = lap_metric_trace(session1, 1)
    assert "GPS Speed" in trace.columns
    assert "RPM" in trace.columns
    assert trace["GPS Speed"].notna().any()


def test_braking_zones_found_before_each_corner(session1):
    trace = lap_metric_trace(session1, 1)
    trace = add_braking_throttle_estimates(trace)
    zones = braking_zones(trace)
    # synthetic track has 4 corners, expect at least a few braking zones
    assert len(zones) >= 3
    assert (zones["peak_decel_g"] < 0).all()
    assert (zones["exit_speed_kmh"] <= zones["entry_speed_kmh"]).all()


def test_gg_diagram_points_bounded(session1):
    trace = lap_metric_trace(session1, 1)
    points = gg_diagram_points(trace)
    assert not points.empty
    # G values for a kart should be modest, sanity bound to catch unit errors
    assert points["GPS Lateral Acceleration"].abs().max() < 5
    assert points["GPS Longitudinal Acceleration"].abs().max() < 5


def test_segment_aggregates_min_le_max(session1):
    segments = build_reference_segments(session1, reference_lap_number=1)
    trace = lap_metric_trace(session1, 1)
    agg = segment_aggregates(trace, segments)
    assert (agg["min_speed_kmh"] <= agg["max_speed_kmh"]).all()
    corner_agg = agg[agg["segment_kind"] == "corner"]
    straight_agg = agg[agg["segment_kind"] == "straight"]
    # corners should have lower average speed than straights on this track
    assert corner_agg["avg_speed_kmh"].mean() < straight_agg["avg_speed_kmh"].mean()


def test_consistency_stats_detects_high_stddev_from_incident_lap(session1):
    laps = lap_table(session1)
    stats = consistency_stats(laps)
    assert stats["std_dev_s"] > 5  # inflated by the ~53s incident lap in a ~30s session
