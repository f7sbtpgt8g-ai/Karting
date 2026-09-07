"""Part 1: the validity gate, against synthetic laps/traces -- no database."""

from __future__ import annotations

import math

from telemetry.rating.config import RatingConfig
from telemetry.rating.validity import (
    EXCLUDED,
    FLAGGED,
    VERIFIED,
    LapPositionTrace,
    LapValidityInput,
    build_reference_line,
    classify_lap,
    implausible_floor,
    speed_drop_without_braking,
    track_cut_deviation,
)

CONFIG = RatingConfig()


def _straight_trace(length_m: float = 300.0, n: int = 60, lateral_offset_m: float = 0.0) -> LapPositionTrace:
    """A straight north-south "track" at the equator, where 1 degree of
    latitude and longitude are both ~111.32km -- convenient round numbers
    for asserting exact metre offsets."""
    distance = [i * (length_m / (n - 1)) for i in range(n)]
    lat = [d / 111_320.0 for d in distance]
    lon = [lateral_offset_m / 111_320.0] * n
    return LapPositionTrace(distance_m=distance, latitude=lat, longitude=lon)


def test_reference_line_needs_a_minimum_sample():
    traces = [_straight_trace() for _ in range(CONFIG.reference_line_min_laps - 1)]
    assert build_reference_line(traces, CONFIG) is None


def test_reference_line_from_identical_laps_has_floor_tolerance():
    traces = [_straight_trace() for _ in range(CONFIG.reference_line_min_laps)]
    ref = build_reference_line(traces, CONFIG)
    assert ref is not None
    assert ref.built_from_lap_count == CONFIG.reference_line_min_laps
    # No spread across identical laps -- tolerance floors out rather than
    # collapsing to zero (real GPS jitter always exists).
    assert all(t == CONFIG.reference_line_tolerance_floor_m for t in ref.tolerance_m)


def test_track_cut_flags_a_lap_that_cuts_the_corridor():
    traces = [_straight_trace() for _ in range(CONFIG.reference_line_min_laps)]
    ref = build_reference_line(traces, CONFIG)
    # Offset by 20m for a long central stretch -- well past the metres-wide
    # tolerance a set of identical laps produces.
    cut_lap = _straight_trace(lateral_offset_m=20.0)
    check = track_cut_deviation(cut_lap, ref, CONFIG)
    assert check.is_cut
    assert check.offending_points >= CONFIG.track_cut_min_offending_points
    assert math.isclose(check.max_deviation_m, 20.0, rel_tol=0.05)


def test_track_cut_does_not_flag_a_lap_on_the_racing_line():
    traces = [_straight_trace() for _ in range(CONFIG.reference_line_min_laps)]
    ref = build_reference_line(traces, CONFIG)
    clean_lap = _straight_trace()
    check = track_cut_deviation(clean_lap, ref, CONFIG)
    assert not check.is_cut


def test_track_cut_ignores_a_single_stray_point():
    # Real laps sample GPS far more densely (~300 fixes) than the
    # reference line's 120 resampled points, so a single bad fix is
    # smoothed away by its many clean neighbours either side rather than
    # dominating the interpolation -- matched here with a denser trace
    # than the other tests use, since a too-sparse source trace makes one
    # bad sample disproportionately visible after resampling.
    traces = [_straight_trace(n=300) for _ in range(CONFIG.reference_line_min_laps)]
    ref = build_reference_line(traces, CONFIG)
    lap = _straight_trace(n=300)
    lap.latitude = list(lap.latitude)
    lap.latitude[len(lap.latitude) // 2] += 100.0 / 111_320.0
    check = track_cut_deviation(lap, ref, CONFIG)
    assert check.offending_points < CONFIG.track_cut_min_offending_points
    assert not check.is_cut


def test_implausible_floor_needs_a_minimum_sample():
    assert implausible_floor([30.0, 30.1], CONFIG) is None


def test_implausible_floor_is_a_fraction_of_the_median():
    times = [30.0, 30.1, 29.9, 30.2, 30.0]
    floor = implausible_floor(times, CONFIG)
    assert floor == 30.0 * CONFIG.implausible_floor_factor


def test_speed_drop_without_braking_flags_an_unbraked_drop():
    speed = [80.0, 79.0, 40.0, 39.0, 38.0]
    braking = [False, False, False, False, False]
    assert speed_drop_without_braking(speed, braking, CONFIG)


def test_speed_drop_with_braking_is_not_flagged():
    speed = [80.0, 79.0, 40.0, 39.0, 38.0]
    braking = [False, False, True, True, False]
    assert not speed_drop_without_braking(speed, braking, CONFIG)


def test_gradual_slowdown_is_not_flagged():
    speed = [80.0, 78.0, 76.0, 74.0, 72.0]
    braking = [False, False, False, False, False]
    assert not speed_drop_without_braking(speed, braking, CONFIG)


def test_classify_lap_excludes_pre_existing_outlier():
    lap = LapValidityInput(lap_id=1, lap_time_s=90.0, is_outlier=True, outlier_reason="out_lap", excluded_by_user=False)
    result = classify_lap(lap, config=CONFIG)
    assert result.status == EXCLUDED
    assert "out_lap" in result.reason


def test_classify_lap_excludes_driver_exclusion_even_if_otherwise_clean():
    lap = LapValidityInput(lap_id=1, lap_time_s=30.0, is_outlier=False, outlier_reason=None, excluded_by_user=True)
    result = classify_lap(lap, implausible_floor_s=20.0, config=CONFIG)
    assert result.status == EXCLUDED


def test_classify_lap_flags_rather_than_excludes_implausible_time():
    lap = LapValidityInput(lap_id=1, lap_time_s=10.0, is_outlier=False, outlier_reason=None, excluded_by_user=False)
    result = classify_lap(lap, implausible_floor_s=20.0, config=CONFIG)
    assert result.status == FLAGGED
    assert "implausible_time" in result.reason


def test_classify_lap_verified_when_clean_on_every_signal():
    lap = LapValidityInput(lap_id=1, lap_time_s=30.0, is_outlier=False, outlier_reason=None, excluded_by_user=False)
    result = classify_lap(lap, implausible_floor_s=20.0, config=CONFIG)
    assert result.status == VERIFIED
    assert result.reason is None


def test_classify_lap_combines_multiple_flag_reasons():
    traces = [_straight_trace() for _ in range(CONFIG.reference_line_min_laps)]
    ref = build_reference_line(traces, CONFIG)
    cut_position = _straight_trace(lateral_offset_m=20.0)
    lap = LapValidityInput(
        lap_id=1,
        lap_time_s=10.0,
        is_outlier=False,
        outlier_reason=None,
        excluded_by_user=False,
        position=cut_position,
        speed_kmh=[80.0, 40.0],
        braking=[False, False],
    )
    result = classify_lap(lap, implausible_floor_s=20.0, reference=ref, config=CONFIG)
    assert result.status == FLAGGED
    assert "implausible_time" in result.reason
    assert "possible_track_cut" in result.reason
    assert "speed_drop_without_braking" in result.reason
