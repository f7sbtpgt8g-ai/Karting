"""Corner-by-corner metric extraction: entry/apex/exit points, three-zone
time deltas, and corner-complex detection.

Part 1 of the corner-by-corner causal coaching engine (see `pattern_rules.py`
for Part 2's classification and `corner_engine.py` for the orchestration
that ties them together). Deliberately pure and deterministic -- no
classification or narrative here -- so it can be unit tested against
hand-built synthetic traces before trusting it on real, noisy GPS data.

Reuses `metrics.py`'s braking/throttle inference (`add_braking_throttle_estimates`)
rather than re-deriving entry/exit detection from scratch, per the same
"every inferred value is labeled `_estimate`" convention used everywhere
else in this codebase.

Zone definitions -- a documented default, not a single unambiguous spec, so
picked deliberately and noted here rather than guessed silently:
  Zone A (braking/entry): entry point (brake onset) -> apex point.
  Zone B (corner arc): entry point (brake onset) -> exit point (exit gate).
    Includes zone A -- this is "the whole corner" in the traditional
    segment-time sense, used for the fast-entry/compromised-exit family of
    comparisons.
  Zone C (following straight): exit point (exit gate) -> the next corner's
    own entry point on the same lap (or the lap's end distance, for the
    last corner).
Each lap's zones are built from that lap's OWN entry/apex/exit points, not
the reference lap's -- the point is to capture how differently two laps
actually drove the corner, and zone C's end boundary (the next corner's own
brake point) is itself a meaningful in-lap event, not an arbitrary cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .corners import lap_gps_trace
from .delta import _time_vs_distance
from .metrics import add_braking_throttle_estimates

# How far before a corner segment's own start to look for the brake point --
# braking almost always begins on the approach, before the geometric corner
# boundary. Matches focus_areas.py's APPROACH_WINDOW_M for consistency with
# the rest of the codebase's existing diagnostic logic.
ENTRY_SEARCH_WINDOW_M = 30.0

# How far past a corner segment's own end to keep looking for the exit gate
# -- power-on / "running straight again" often lands just past the
# geometric corner boundary, especially on a late-exit line.
EXIT_SEARCH_WINDOW_M = 60.0

# "Sustained" power-on / straight-running, in consecutive qualifying GPS
# fixes, so a single noisy sample isn't picked as the exit gate.
EXIT_GATE_MIN_CONSECUTIVE_SAMPLES = 3

# Lateral-G magnitude below this reads as "running straight" for the
# lateral-G-based exit-gate fallback (in g) -- used alongside power-on so a
# corner exited under trailing throttle (never registers power_on_estimate)
# still gets a usable exit gate.
EXIT_GATE_LATERAL_G_THRESHOLD = 0.15

# Corner-complex detection: two corners are treated as a linked sequence
# when the straight between them is this short in time, or...
COMPLEX_MIN_STRAIGHT_TIME_S = 0.5
# ...lateral G over that straight averages above this (still visibly
# turning, not "straight" in any meaningful sense).
COMPLEX_LATERAL_G_THRESHOLD = 0.2


@dataclass
class CornerPoints:
    corner_label: str
    entry_distance_m: float
    entry_speed_kmh: float
    entry_is_estimated: bool  # True if no braking zone was found; entry defaulted to the segment's own start
    apex_distance_m: float
    apex_speed_kmh: float
    exit_distance_m: float
    exit_speed_kmh: float
    exit_gate_reason: str  # "power_on" | "lateral_g" | "search_window_exhausted"


def _first_sustained_run(mask: np.ndarray, min_consecutive: int) -> int | None:
    """Index of the first True in `mask` that starts a run of at least
    `min_consecutive` consecutive True values, or None if there isn't one."""
    run = 0
    for i, v in enumerate(mask):
        run = run + 1 if v else 0
        if run >= min_consecutive:
            return i - min_consecutive + 1
    return None


def extract_corner_points(
    trace: pd.DataFrame, segment_row: pd.Series, next_boundary_m: float | None
) -> CornerPoints | None:
    """Entry/apex/exit point for one corner segment on one lap's trace.

    `trace` is a full-lap trace (e.g. from `lap_gps_trace`, with
    `add_braking_throttle_estimates` already applied or not -- applied here
    if missing) sorted by distance. `segment_row` is this corner's row from
    the segments table. `next_boundary_m` caps how far past the segment's
    own end to search for the exit gate -- typically the next segment's end
    distance, or None to search up to `EXIT_SEARCH_WINDOW_M` unconstrained.
    """
    if trace.empty or "GPS Speed" not in trace.columns:
        return None
    trace = trace.dropna(subset=["lap_distance_m"]).sort_values("lap_distance_m").reset_index(drop=True)
    if "braking_estimate" not in trace.columns:
        trace = add_braking_throttle_estimates(trace)

    start_m, end_m = float(segment_row["start_m"]), float(segment_row["end_m"])
    search_end = end_m + EXIT_SEARCH_WINDOW_M
    if next_boundary_m is not None:
        search_end = min(search_end, next_boundary_m)

    approach = trace[(trace["lap_distance_m"] >= start_m - ENTRY_SEARCH_WINDOW_M) & (trace["lap_distance_m"] < end_m)]
    entry_distance_m, entry_is_estimated = start_m, True
    if not approach.empty and approach["braking_estimate"].any():
        first_braking = approach.loc[approach["braking_estimate"]].iloc[0]
        entry_distance_m = float(first_braking["lap_distance_m"])
        entry_is_estimated = False

    corner_zone = trace[(trace["lap_distance_m"] >= entry_distance_m) & (trace["lap_distance_m"] <= end_m)]
    if corner_zone.empty or corner_zone["GPS Speed"].isna().all():
        return None
    apex_row = corner_zone.loc[corner_zone["GPS Speed"].idxmin()]
    apex_distance_m = float(apex_row["lap_distance_m"])
    apex_speed_kmh = float(apex_row["GPS Speed"])

    entry_row = trace.loc[(trace["lap_distance_m"] - entry_distance_m).abs().idxmin()]
    entry_speed_kmh = float(entry_row["GPS Speed"]) if pd.notna(entry_row["GPS Speed"]) else float("nan")

    post_apex = trace[
        (trace["lap_distance_m"] >= apex_distance_m) & (trace["lap_distance_m"] <= search_end)
    ].reset_index(drop=True)

    exit_distance_m, exit_speed_kmh, exit_gate_reason = None, None, "search_window_exhausted"
    if not post_apex.empty:
        power_on = post_apex["power_on_estimate"].fillna(False).to_numpy()
        lat_g = post_apex.get("GPS Lateral Acceleration")
        straight_running = (
            (lat_g.abs() < EXIT_GATE_LATERAL_G_THRESHOLD).fillna(False).to_numpy()
            if lat_g is not None else np.zeros(len(post_apex), dtype=bool)
        )

        i_power = _first_sustained_run(power_on, EXIT_GATE_MIN_CONSECUTIVE_SAMPLES)
        i_straight = _first_sustained_run(straight_running, EXIT_GATE_MIN_CONSECUTIVE_SAMPLES)
        candidates = [(i, reason) for i, reason in [(i_power, "power_on"), (i_straight, "lateral_g")] if i is not None]
        if candidates:
            i_exit, exit_gate_reason = min(candidates, key=lambda c: c[0])
            exit_row = post_apex.iloc[i_exit]
            exit_distance_m = float(exit_row["lap_distance_m"])
            exit_speed_kmh = float(exit_row["GPS Speed"]) if pd.notna(exit_row["GPS Speed"]) else float("nan")

    if exit_distance_m is None:
        # Neither gate condition was ever met within the search window --
        # fall back to the window's own end rather than leaving the exit
        # undefined, so downstream zone-time math always has a boundary to
        # work with (flagged via exit_gate_reason for anything that wants
        # to treat this case differently).
        fallback_row = post_apex.iloc[-1] if not post_apex.empty else apex_row
        exit_distance_m = float(fallback_row["lap_distance_m"])
        exit_speed_kmh = float(fallback_row["GPS Speed"]) if pd.notna(fallback_row["GPS Speed"]) else apex_speed_kmh

    return CornerPoints(
        corner_label=str(segment_row["label"]),
        entry_distance_m=entry_distance_m, entry_speed_kmh=entry_speed_kmh, entry_is_estimated=entry_is_estimated,
        apex_distance_m=apex_distance_m, apex_speed_kmh=apex_speed_kmh,
        exit_distance_m=exit_distance_m, exit_speed_kmh=exit_speed_kmh, exit_gate_reason=exit_gate_reason,
    )


def corner_points_for_lap(session, lap_number: int, segments: pd.DataFrame) -> pd.DataFrame:
    """Entry/apex/exit points for every corner segment, for one lap."""
    trace = lap_gps_trace(session, lap_number)
    if trace.empty:
        return pd.DataFrame()
    trace = add_braking_throttle_estimates(trace)

    ordered = segments.sort_values("start_m").reset_index(drop=True)
    rows = []
    for idx, seg in ordered.iterrows():
        if seg["kind"] != "corner":
            continue
        next_boundary = float(ordered.iloc[idx + 1]["end_m"]) if idx + 1 < len(ordered) else None
        points = extract_corner_points(trace, seg, next_boundary)
        if points is not None:
            rows.append(points.__dict__)
    return pd.DataFrame(rows)


def three_zone_times(session, lap_number: int, corner_points: pd.DataFrame) -> pd.DataFrame:
    """Zone A/B/C duration (seconds) for each corner on one lap, using that
    lap's own entry/apex/exit points (see module docstring for the zone
    definitions and why each lap uses its own boundaries rather than the
    reference lap's)."""
    distance, rel_time = _time_vs_distance(session, lap_number)
    if len(distance) < 2 or corner_points.empty:
        return pd.DataFrame()

    ordered = corner_points.sort_values("entry_distance_m").reset_index(drop=True)
    lap_end_m = float(distance.max())
    rows = []
    for i, cp in ordered.iterrows():
        next_entry_m = float(ordered.iloc[i + 1]["entry_distance_m"]) if i + 1 < len(ordered) else lap_end_m
        t_entry = float(np.interp(cp["entry_distance_m"], distance, rel_time))
        t_apex = float(np.interp(cp["apex_distance_m"], distance, rel_time))
        t_exit = float(np.interp(cp["exit_distance_m"], distance, rel_time))
        t_next_entry = float(np.interp(next_entry_m, distance, rel_time))
        rows.append(
            {
                "corner_label": cp["corner_label"],
                "zone_a_time_s": t_apex - t_entry,
                "zone_b_time_s": t_exit - t_entry,
                "zone_c_time_s": t_next_entry - t_exit,
                "zone_c_end_distance_m": next_entry_m,
            }
        )
    return pd.DataFrame(rows)


def detect_corner_complexes(
    session,
    reference_lap_number: int,
    segments: pd.DataFrame,
    min_straight_time_s: float = COMPLEX_MIN_STRAIGHT_TIME_S,
    lateral_g_threshold: float = COMPLEX_LATERAL_G_THRESHOLD,
) -> list[list[str]]:
    """Group corner labels into complexes: consecutive corners with no
    meaningful straight between them on the reference lap (either a very
    short zone-C duration, or non-trivial lateral G throughout the
    following straight) -- so a compromised entry doesn't get silently
    misattributed to the wrong corner.

    Returns a list of groups, each a list of corner labels in track order;
    every corner appears in exactly one group (a singleton group for a
    corner followed by a clean, distinct straight).
    """
    corner_labels_in_order = segments.loc[segments["kind"] == "corner"].sort_values("start_m")["label"].tolist()
    if not corner_labels_in_order:
        return []

    corner_points = corner_points_for_lap(session, reference_lap_number, segments)
    zone_times = three_zone_times(session, reference_lap_number, corner_points)
    if corner_points.empty or zone_times.empty:
        return [[label] for label in corner_labels_in_order]

    trace = add_braking_throttle_estimates(lap_gps_trace(session, reference_lap_number))
    ordered = corner_points.sort_values("entry_distance_m").reset_index(drop=True)
    zone_by_label = zone_times.set_index("corner_label")

    groups: list[list[str]] = []
    current_group = [ordered.iloc[0]["corner_label"]]
    for i in range(len(ordered) - 1):
        this_label = ordered.iloc[i]["corner_label"]
        next_label = ordered.iloc[i + 1]["corner_label"]
        if this_label not in zone_by_label.index:
            groups.append(current_group)
            current_group = [next_label]
            continue

        zone_c = zone_by_label.loc[this_label]
        straight = trace[
            (trace["lap_distance_m"] >= ordered.iloc[i]["exit_distance_m"])
            & (trace["lap_distance_m"] <= zone_c["zone_c_end_distance_m"])
        ]
        lateral_present = (
            straight["GPS Lateral Acceleration"].abs().mean()
            if not straight.empty and straight["GPS Lateral Acceleration"].notna().any() else 0.0
        )
        is_short_or_curved = (zone_c["zone_c_time_s"] < min_straight_time_s) or (lateral_present > lateral_g_threshold)
        if is_short_or_curved:
            current_group.append(next_label)
        else:
            groups.append(current_group)
            current_group = [next_label]
    groups.append(current_group)

    # Corners with no extracted points (e.g. no GPS speed data in that
    # window) still need a home so callers can rely on every corner
    # appearing in exactly one group.
    grouped_labels = {label for group in groups for label in group}
    for label in corner_labels_in_order:
        if label not in grouped_labels:
            groups.append([label])
    return groups
