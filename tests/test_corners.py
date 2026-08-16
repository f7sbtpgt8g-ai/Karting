from telemetry.corners import assign_segments, build_reference_segments, compute_curvature, lap_gps_trace


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
