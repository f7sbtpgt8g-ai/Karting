"""End-to-end tests of the corner-by-corner causal engine
(`corner_engine.compare_corners`) against full synthetic lap sessions --
the canonical "fast entry compromised the exit" scenario the whole engine
is built around, plus a lap-vs-itself sanity check."""

import pandas as pd

from telemetry.corner_engine import calibrate_thresholds, compare_corners, compare_corners_across_laps
from tests.synthetic_corner import build_lap_session, single_corner_segments

TOTAL_M = 250.0
SEGMENTS = single_corner_segments(corner_start_m=50.0, corner_end_m=110.0, total_m=TOTAL_M)

# Shared G-force shape for both laps -- same physical corner, only the
# speed carried through it differs between the two drivers/laps.
LON_G_BREAKPOINTS = ([0, 44, 45, 79, 80, 81, 129, 130, 250], [0, 0, -0.6, -0.6, 0, 0.4, 0.4, 0, 0])
LATERAL_G_BREAKPOINTS = ([0, 39, 40, 119, 120, 250], [0, 0, -0.8, -0.8, 0, 0])


def _reference_session():
    return build_lap_session(
        total_distance_m=TOTAL_M,
        speed_breakpoints=([0, 45, 80, 130, 250], [120, 120, 50, 120, 120]),
        lon_g_breakpoints=LON_G_BREAKPOINTS,
        lateral_g_breakpoints=LATERAL_G_BREAKPOINTS,
        session_id=0, lap_number=1,
    )


def _fast_entry_compromised_exit_session():
    # Carries 15 km/h more entry speed and a slightly higher apex speed
    # (faster through zone B), but never fully recovers on the straight --
    # holds at 90 km/h instead of climbing back to 120, all the way to the
    # end of the lap.
    return build_lap_session(
        total_distance_m=TOTAL_M,
        speed_breakpoints=([0, 45, 80, 130, 250], [135, 135, 55, 90, 90]),
        lon_g_breakpoints=LON_G_BREAKPOINTS,
        lateral_g_breakpoints=LATERAL_G_BREAKPOINTS,
        session_id=1, lap_number=1,
    )


def test_compromised_exit_from_fast_entry_end_to_end():
    reference = _reference_session()
    analyzed = _fast_entry_compromised_exit_session()

    result = compare_corners(analyzed, 1, reference, 1, SEGMENTS)

    assert len(result) == 1
    row = result.iloc[0]
    assert row["corner_label"] == "Corner 1"
    assert row["entry_speed_delta_kmh"] > 1.0
    assert row["zone_b_delta_s"] < 0  # gained time through the corner arc itself
    assert row["zone_c_delta_s"] > 0  # lost time down the following straight
    assert row["zone_c_delta_s"] > abs(row["zone_b_delta_s"])
    assert row["net_time_impact_s"] > 0  # net loss overall
    assert row["pattern_type"] == "compromised_exit_fast_entry"
    assert bool(row["headline"]) is True


def test_lap_compared_against_itself_is_clean():
    reference = _reference_session()
    result = compare_corners(reference, 1, reference, 1, SEGMENTS)

    assert len(result) == 1
    row = result.iloc[0]
    assert row["net_time_impact_s"] == 0
    assert row["pattern_type"] == "clean_no_significant_delta"
    assert bool(row["headline"]) is False


def test_compare_corners_across_laps_flags_recurring_pattern():
    reference = _reference_session()
    lap1 = _fast_entry_compromised_exit_session()
    lap2 = build_lap_session(
        total_distance_m=TOTAL_M,
        speed_breakpoints=([0, 45, 80, 130, 250], [133, 133, 54, 88, 88]),
        lon_g_breakpoints=LON_G_BREAKPOINTS, lateral_g_breakpoints=LATERAL_G_BREAKPOINTS,
        session_id=1, lap_number=2,
    )
    # Both laps live in one session (compare_corners_across_laps analyzes
    # several laps *within* one Session against a shared reference).
    two_lap_session = lap1
    two_lap_session.df = pd.concat([lap1.df, lap2.df], ignore_index=True)
    two_lap_session.channel_cache = {}

    combined = compare_corners_across_laps(two_lap_session, [1, 2], reference, 1, SEGMENTS)
    assert not combined.empty
    corner1 = combined[combined["corner_label"] == "Corner 1"]
    assert (corner1["pattern_type"] == "compromised_exit_fast_entry").all()
    assert corner1["is_recurring"].all()
    assert corner1["n_laps_with_pattern"].iloc[0] == 2


def test_calibrate_thresholds_falls_back_with_too_few_laps():
    reference = _reference_session()
    thresholds = calibrate_thresholds(reference, [1], SEGMENTS)
    assert thresholds.min_speed_delta_kmh == 1.0  # the fixed default, unchanged
