"""Theoretical best lap and continuous delta-time trace.

The delta-time-vs-distance trace is the single most direct view for
pinpointing exactly where time is lost/gained against a reference lap.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .corners import lap_gps_trace


def _time_vs_distance(session, lap_number: int) -> tuple[np.ndarray, np.ndarray]:
    """Return (distance_m, relative_time_s) arrays for a lap, sorted and
    de-duplicated by distance so they're safe to `np.interp` against."""
    trace = lap_gps_trace(session, lap_number).dropna(subset=["lap_distance_m"])
    trace = trace.sort_values("lap_distance_m")
    if trace.empty:
        return np.array([]), np.array([])
    t0 = trace["session_time_s"].iloc[0]
    distance = trace["lap_distance_m"].to_numpy()
    rel_time = (trace["session_time_s"] - t0).to_numpy()
    # np.interp requires strictly increasing x; collapse duplicate distances.
    distance, idx = np.unique(distance, return_index=True)
    rel_time = rel_time[idx]
    return distance, rel_time


def delta_time_trace(session, lap_number: int, reference_lap_number: int, n_points: int = 400) -> pd.DataFrame:
    """Continuous time gained/lost vs. a reference lap, over a common
    distance grid spanning both laps."""
    d_lap, t_lap = _time_vs_distance(session, lap_number)
    d_ref, t_ref = _time_vs_distance(session, reference_lap_number)
    if len(d_lap) < 2 or len(d_ref) < 2:
        return pd.DataFrame(columns=["distance_m", "delta_s"])

    max_distance = min(d_lap.max(), d_ref.max())
    grid = np.linspace(0, max_distance, n_points)
    lap_t_on_grid = np.interp(grid, d_lap, t_lap)
    ref_t_on_grid = np.interp(grid, d_ref, t_ref)
    delta = lap_t_on_grid - ref_t_on_grid
    return pd.DataFrame({"distance_m": grid, "delta_s": delta})


def segment_times_for_lap(session, lap_number: int, segments: pd.DataFrame) -> pd.DataFrame:
    """Time spent in each track segment for one lap, by interpolating the
    lap's time-vs-distance curve at each segment's distance boundaries."""
    distance, rel_time = _time_vs_distance(session, lap_number)
    if len(distance) < 2:
        return pd.DataFrame(columns=["segment_label", "segment_kind", "time_s"])

    rows = []
    for _, seg in segments.iterrows():
        start_t = np.interp(seg["start_m"], distance, rel_time)
        end_t = np.interp(seg["end_m"], distance, rel_time)
        rows.append(
            {
                "segment_label": seg["label"],
                "segment_kind": seg["kind"],
                "time_s": end_t - start_t,
            }
        )
    return pd.DataFrame(rows)


def theoretical_best_lap(
    session, lap_numbers: list[int], segments: pd.DataFrame
) -> tuple[float, pd.DataFrame]:
    """Theoretical best lap: sum of the best observed time in each segment
    across the given (already outlier-filtered) laps.

    Returns (theoretical_best_s, best_segment_times) where
    `best_segment_times` records which lap produced each segment's best
    time, for traceability.
    """
    all_rows = []
    for lap_no in lap_numbers:
        seg_times = segment_times_for_lap(session, lap_no, segments)
        seg_times["lap_number"] = lap_no
        all_rows.append(seg_times)

    if not all_rows:
        return float("nan"), pd.DataFrame()

    combined = pd.concat(all_rows, ignore_index=True)
    combined = combined.dropna(subset=["time_s"])
    if combined.empty:
        return float("nan"), pd.DataFrame()

    best = combined.loc[combined.groupby("segment_label")["time_s"].idxmin()].reset_index(drop=True)
    theoretical_best_s = best["time_s"].sum()
    return theoretical_best_s, best
