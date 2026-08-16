from telemetry.corners import build_reference_segments
from telemetry.delta import delta_time_trace, segment_times_for_lap, theoretical_best_lap
from telemetry.focus_areas import time_loss_per_segment, top_focus_areas
from telemetry.laps import clean_lap_table, flag_outlier_laps, lap_table


def _clean_laps_and_best(session1):
    laps = flag_outlier_laps(lap_table(session1))
    clean = clean_lap_table(laps)
    clean_lap_numbers = clean["lap_number"].tolist()
    best_lap = int(clean.loc[clean["lap_time_s"].idxmin(), "lap_number"])
    return clean_lap_numbers, best_lap


def test_theoretical_best_is_at_or_below_actual_best(session1):
    clean_lap_numbers, best_lap = _clean_laps_and_best(session1)
    segments = build_reference_segments(session1, best_lap)
    laps = flag_outlier_laps(lap_table(session1))
    actual_best_s = laps.loc[laps["lap_number"] == best_lap, "lap_time_s"].iloc[0]

    theoretical_best_s, best_segment_times = theoretical_best_lap(session1, clean_lap_numbers, segments)

    assert theoretical_best_s <= actual_best_s + 1e-6
    assert set(best_segment_times["segment_label"]) == set(segments["label"])


def test_delta_trace_against_self_is_near_zero(session1):
    _, best_lap = _clean_laps_and_best(session1)
    dt = delta_time_trace(session1, best_lap, best_lap)
    assert (dt["delta_s"].abs() < 1e-6).all()


def test_delta_trace_shows_incident_lap_losing_time(session1):
    _, best_lap = _clean_laps_and_best(session1)
    dt = delta_time_trace(session1, 4, best_lap)  # lap 4 is the injected spin
    assert dt["delta_s"].iloc[-1] > 5  # should be well down by the end of the lap


def test_top_focus_areas_returns_ranked_positive_losses(session1):
    clean_lap_numbers, best_lap = _clean_laps_and_best(session1)
    segments = build_reference_segments(session1, best_lap)
    _, best_segment_times = theoretical_best_lap(session1, clean_lap_numbers, segments)
    lap_segment_times = segment_times_for_lap(session1, best_lap, segments)

    areas = top_focus_areas(session1, best_lap, segments, lap_segment_times, best_segment_times, n=3)
    assert len(areas) <= 3
    losses = [a["time_loss_s"] for a in areas]
    assert losses == sorted(losses, reverse=True)
    assert all(loss >= 0 for loss in losses)
    for a in areas:
        assert a["coaching_note"]


def test_time_loss_per_segment_matches_manual_diff(session1):
    clean_lap_numbers, best_lap = _clean_laps_and_best(session1)
    segments = build_reference_segments(session1, best_lap)
    _, best_segment_times = theoretical_best_lap(session1, clean_lap_numbers, segments)
    lap_segment_times = segment_times_for_lap(session1, best_lap, segments)

    losses = time_loss_per_segment(lap_segment_times, best_segment_times)
    row = losses.iloc[0]
    manual = (
        lap_segment_times.loc[lap_segment_times["segment_label"] == row["segment_label"], "time_s"].iloc[0]
        - best_segment_times.loc[best_segment_times["segment_label"] == row["segment_label"], "time_s"].iloc[0]
    )
    assert abs(row["time_loss_s"] - manual) < 1e-9
