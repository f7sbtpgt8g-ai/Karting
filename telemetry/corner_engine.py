"""Orchestration for the corner-by-corner causal coaching engine: ties
together `corner_causal.py`'s extraction (Part 1) and `pattern_rules.py`'s
classification (Part 2), and adds corner-complex causal-chain attribution,
multi-lap recurrence confidence, and noise-floor calibration (Part 3).

This is still diagnosis, not narrative -- see `narrative.py` for the
language-generation step that consumes this module's output. Kept as a
separate module specifically so extraction+classification stay
independently testable (per the spec this implements) without dragging in
any UI or LLM-call concerns.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .corner_causal import corner_points_for_lap, detect_corner_complexes, three_zone_times
from .pattern_rules import CornerComparison, PatternMatch, SignificanceThresholds, build_corner_comparison, classify_corner

# A later corner's entry-speed delta must be at least this fraction of the
# earlier corner's exit-speed delta (same sign) before the later corner's
# deficit is attributed back to the earlier one as its root cause.
COMPLEX_PROPAGATION_MIN_CORRELATION_RATIO = 0.5


def compare_corners(
    session_a, lap_a: int, session_b, lap_b: int, segments: pd.DataFrame,
    thresholds: SignificanceThresholds | None = None,
) -> pd.DataFrame:
    """Full corner-by-corner comparison + classification for one lap (`a`,
    the lap being analyzed) against a reference lap (`b`, e.g. a personal
    best, teammate lap, or prior session). One row per corner, ranked by
    |net_time_impact_s| descending -- empty DataFrame if either lap has no
    extractable corner points.
    """
    thresholds = thresholds or SignificanceThresholds()
    lap_points = corner_points_for_lap(session_a, lap_a, segments)
    ref_points = corner_points_for_lap(session_b, lap_b, segments)
    if lap_points.empty or ref_points.empty:
        return pd.DataFrame()
    lap_zones = three_zone_times(session_a, lap_a, lap_points)
    ref_zones = three_zone_times(session_b, lap_b, ref_points)
    if lap_zones.empty or ref_zones.empty:
        return pd.DataFrame()

    lap_points_i = lap_points.set_index("corner_label")
    ref_points_i = ref_points.set_index("corner_label")
    lap_zones_i = lap_zones.set_index("corner_label")
    ref_zones_i = ref_zones.set_index("corner_label")

    complexes = detect_corner_complexes(session_b, lap_b, segments)
    complex_group_by_label: dict[str, list[str]] = {label: group for group in complexes for label in group}

    common_labels = [
        label for label in lap_points_i.index
        if label in ref_points_i.index and label in lap_zones_i.index and label in ref_zones_i.index
    ]

    rows = []
    for label in common_labels:
        cmp = build_corner_comparison(
            label, lap_points_i.loc[label], ref_points_i.loc[label], lap_zones_i.loc[label], ref_zones_i.loc[label]
        )
        match = classify_corner(cmp, thresholds)
        group = complex_group_by_label.get(label, [label])
        rows.append(
            {
                "corner_label": label,
                "is_complex": len(group) > 1,
                "complex_group": group,
                "entry_speed_delta_kmh": cmp.entry_speed_delta_kmh,
                "apex_speed_delta_kmh": cmp.apex_speed_delta_kmh,
                "exit_speed_delta_kmh": cmp.exit_speed_delta_kmh,
                "entry_distance_delta_m": cmp.entry_distance_delta_m,
                "apex_distance_delta_m": cmp.apex_distance_delta_m,
                "zone_a_delta_s": cmp.zone_a_delta_s,
                "zone_b_delta_s": cmp.zone_b_delta_s,
                "zone_c_delta_s": cmp.zone_c_delta_s,
                "net_time_impact_s": cmp.net_delta_s,
                "pattern_type": match.pattern_type,
                "confidence": match.confidence,
                "headline": match.headline,
                "evidence": match.evidence,
            }
        )

    result = pd.DataFrame(rows)
    if result.empty:
        return result
    result = _attribute_complex_causes(result, complexes)
    result = result.reindex(result["net_time_impact_s"].abs().sort_values(ascending=False).index).reset_index(drop=True)
    return result


def _attribute_complex_causes(result: pd.DataFrame, complexes: list[list[str]]) -> pd.DataFrame:
    """Trace a later corner's compromised entry back to an earlier corner's
    exit within the same complex (Part 1/2's "corner-complex propagation" --
    report the causal chain rather than just flagging the segment where the
    time actually shows up as lost)."""
    result = result.copy()
    result["root_cause_corner"] = None
    by_label = result.set_index("corner_label")
    for group in complexes:
        if len(group) < 2:
            continue
        for i in range(1, len(group)):
            prev_label, this_label = group[i - 1], group[i]
            if prev_label not in by_label.index or this_label not in by_label.index:
                continue
            prev_exit_delta = by_label.loc[prev_label, "exit_speed_delta_kmh"]
            this_entry_delta = by_label.loc[this_label, "entry_speed_delta_kmh"]
            if pd.isna(prev_exit_delta) or pd.isna(this_entry_delta) or prev_exit_delta == 0:
                continue
            same_sign = (prev_exit_delta < 0) == (this_entry_delta < 0)
            if same_sign and abs(this_entry_delta) >= COMPLEX_PROPAGATION_MIN_CORRELATION_RATIO * abs(prev_exit_delta):
                result.loc[result["corner_label"] == this_label, "root_cause_corner"] = prev_label
    return result


def compare_corners_across_laps(
    session_a, lap_numbers: list[int], session_b, lap_b: int, segments: pd.DataFrame,
    thresholds: SignificanceThresholds | None = None,
) -> pd.DataFrame:
    """Per-corner pattern classification across several of the driver's own
    laps vs. the same reference, so a one-off can be distinguished from a
    repeated pattern (Part 3: "this lap" vs. "you're consistently doing
    X"). Returns one row per (corner, lap) with `n_laps_with_pattern` /
    `is_recurring` columns merged in from the per-(corner, pattern) count
    across `lap_numbers`.
    """
    per_lap = []
    for lap_no in lap_numbers:
        df = compare_corners(session_a, lap_no, session_b, lap_b, segments, thresholds)
        if df.empty:
            continue
        df = df.copy()
        df["lap_number"] = lap_no
        per_lap.append(df)
    if not per_lap:
        return pd.DataFrame()
    combined = pd.concat(per_lap, ignore_index=True)

    counts = (
        combined.groupby(["corner_label", "pattern_type"])["lap_number"]
        .nunique().rename("n_laps_with_pattern").reset_index()
    )
    total_laps = len(lap_numbers)
    # A pattern needs to show up in at least half the analyzed laps (and at
    # least twice) before it's called "recurring" rather than a one-off.
    counts["is_recurring"] = counts["n_laps_with_pattern"] >= max(2, int(np.ceil(total_laps * 0.5)))
    combined = combined.merge(counts, on=["corner_label", "pattern_type"], how="left")
    return combined


# Multiplier applied to a driver's own measured repeat-lap standard
# deviation to get a significance threshold -- roughly a 2-sigma bar, so an
# apparent difference has to clearly exceed normal lap-to-lap variance
# before it's treated as a real behavioral difference rather than noise.
NOISE_CALIBRATION_MULTIPLIER = 2.0
MIN_SPEED_DELTA_FLOOR_KMH = 0.5
MIN_DISTANCE_DELTA_FLOOR_M = 3.0
MIN_LAPS_FOR_CALIBRATION = 4


def calibrate_thresholds(session, clean_lap_numbers: list[int], segments: pd.DataFrame) -> SignificanceThresholds:
    """Derive noise-aware significance thresholds from a driver's own
    lap-to-lap variability in entry/apex/exit speed and braking-point
    distance, rather than trusting the fixed defaults blindly once enough
    of a driver's own data exists (Part 3's noise-floor calibration, and
    the cheapest layer of Part 5's "improves as more data accumulates" --
    same-session repeat-lap variance).

    Falls back to `SignificanceThresholds()`'s fixed defaults when there
    aren't enough clean laps to measure variance from. Zone-time thresholds
    are left at their fixed defaults even when calibrating -- doing the same
    for those would need per-corner zone times across many laps against a
    shared reference, which is a heavier computation than is justified here;
    a documented simplification, not an oversight.
    """
    if len(clean_lap_numbers) < MIN_LAPS_FOR_CALIBRATION:
        return SignificanceThresholds()

    all_points = []
    for lap_no in clean_lap_numbers:
        pts = corner_points_for_lap(session, lap_no, segments)
        if not pts.empty:
            all_points.append(pts)
    if not all_points:
        return SignificanceThresholds()
    combined = pd.concat(all_points, ignore_index=True)

    speed_cols = ["entry_speed_kmh", "apex_speed_kmh", "exit_speed_kmh"]
    speed_std = combined.groupby("corner_label")[speed_cols].std().to_numpy()
    speed_std = float(np.nanmean(speed_std)) if speed_std.size else float("nan")
    entry_dist_std = combined.groupby("corner_label")["entry_distance_m"].std().mean()

    min_speed_delta = max(MIN_SPEED_DELTA_FLOOR_KMH, NOISE_CALIBRATION_MULTIPLIER * (speed_std if pd.notna(speed_std) else 0.0))
    min_distance_delta = max(
        MIN_DISTANCE_DELTA_FLOOR_M, NOISE_CALIBRATION_MULTIPLIER * (entry_dist_std if pd.notna(entry_dist_std) else 0.0)
    )

    return SignificanceThresholds(
        min_speed_delta_kmh=min_speed_delta,
        min_distance_delta_m=min_distance_delta,
        braking_point_delta_m=max(6.0, 2 * min_distance_delta),
    )
