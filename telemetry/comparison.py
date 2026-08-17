"""Cross-lap and cross-session comparison: reference laps, progression over
time, and driver-vs-driver benchmarking.

Works across two different `Session` objects (not just two laps within the
same session) so a reference lap can come from a teammate's file or an
earlier session's personal best.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .delta import _time_vs_distance, segment_times_for_lap
from .laps import clean_lap_table, flag_outlier_laps, lap_table, summarize_laps
from .metrics import lap_metric_trace, segment_aggregates

M_PER_DEG_LAT = 111_320.0  # matches the approximation used elsewhere for track-scale distances
DEFAULT_MAX_CORNER_MATCH_DISTANCE_M = 40.0


def cross_session_delta_trace(
    session_a, lap_a: int, session_b, lap_b: int, n_points: int = 400
) -> pd.DataFrame:
    """Delta-time-vs-distance between a lap in `session_a` and a reference
    lap in `session_b` (may be the same session, a teammate's file, or an
    earlier session -- e.g. a personal best)."""
    d_a, t_a = _time_vs_distance(session_a, lap_a)
    d_b, t_b = _time_vs_distance(session_b, lap_b)
    if len(d_a) < 2 or len(d_b) < 2:
        return pd.DataFrame(columns=["distance_m", "delta_s"])

    max_distance = min(d_a.max(), d_b.max())
    grid = np.linspace(0, max_distance, n_points)
    delta = np.interp(grid, d_a, t_a) - np.interp(grid, d_b, t_b)
    return pd.DataFrame({"distance_m": grid, "delta_s": delta})


def session_progression(labeled_sessions: list[tuple[str, object]]) -> pd.DataFrame:
    """Best-lap and consistency trend across multiple loaded sessions.

    `labeled_sessions` is a list of (label, Session) pairs, e.g.
    [("2026-06-01 AM", session1), ("2026-06-01 PM", session2), ...].
    """
    rows = []
    for label, session in labeled_sessions:
        laps = flag_outlier_laps(lap_table(session))
        summary = summarize_laps(laps)
        if not summary:
            continue
        rows.append(
            {
                "session": label,
                "best_lap_s": summary["best_lap_s"],
                "average_lap_s": summary["average_lap_s"],
                "std_dev_s": summary["std_dev_s"],
                "n_laps": summary["n_laps"],
            }
        )
    return pd.DataFrame(rows)


def driver_comparison(
    driver_a_label: str,
    session_a,
    lap_a: int,
    driver_b_label: str,
    session_b,
    lap_b: int,
    segments: pd.DataFrame,
) -> dict:
    """Corner-by-corner diff between two drivers' laps from (typically) the
    same session -- teammate benchmarking."""
    times_a = segment_times_for_lap(session_a, lap_a, segments).set_index("segment_label")
    times_b = segment_times_for_lap(session_b, lap_b, segments).set_index("segment_label")
    diff = (times_a["time_s"] - times_b["time_s"]).rename("time_diff_s").reset_index()
    diff[f"{driver_a_label}_faster_by_s"] = -diff["time_diff_s"]

    return {
        "driver_a": driver_a_label,
        "driver_b": driver_b_label,
        "delta_trace": cross_session_delta_trace(session_a, lap_a, session_b, lap_b),
        "segment_diff": diff,
    }


def _flat_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Flat-earth approximate distance in metres between two lat/lon
    points -- accurate enough at the scale of a single kart track (tens to
    hundreds of metres), not meant for long-range geodesy."""
    m_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * M_PER_DEG_LAT
    dx = (lon2 - lon1) * m_per_deg_lon
    return math.hypot(dx, dy)


def corner_comparison_across_sessions(
    sessions_data: list[tuple[str, object, pd.DataFrame, pd.DataFrame, list[int]]],
    reference_lat: float,
    reference_lon: float,
    max_match_distance_m: float = DEFAULT_MAX_CORNER_MATCH_DISTANCE_M,
) -> pd.DataFrame:
    """Per-lap time and entry/apex/exit speed+RPM for "the same corner" as
    (`reference_lat`, `reference_lon`), across every given session's clean
    laps.

    `sessions_data` is a list of (label, session, segments, corner_midpoints,
    clean_lap_numbers) tuples, one per already-analyzed session -- segments,
    each corner's GPS midpoint (see `corners.py::segment_midpoints`), and
    clean laps are passed in rather than recomputed here so the caller can
    cache the (more expensive) per-session analysis independently of which
    corner is selected.

    Each session's corners are detected independently from its own GPS
    trace, so corner *counts* can differ session to session (a kink picked
    up as its own corner in one session's data but not another's) -- matching
    by GPS position (nearest corner midpoint, within `max_match_distance_m`)
    is robust to that in a way matching by order ("the Nth corner
    encountered") isn't. A session with no corner within range is skipped.
    """
    rows = []
    for label, session, segments, corner_midpoints, clean_lap_numbers in sessions_data:
        candidates = corner_midpoints[corner_midpoints["segment_kind"] == "corner"]
        if candidates.empty:
            continue
        distances = candidates.apply(
            lambda r: _flat_distance_m(reference_lat, reference_lon, r["mid_lat"], r["mid_lon"]), axis=1
        )
        nearest_idx = distances.idxmin()
        if distances.loc[nearest_idx] > max_match_distance_m:
            continue
        corner_label = candidates.loc[nearest_idx, "segment_label"]

        for lap_no in clean_lap_numbers:
            seg_times = segment_times_for_lap(session, lap_no, segments)
            time_row = seg_times.loc[seg_times["segment_label"] == corner_label]
            if time_row.empty or pd.isna(time_row["time_s"].iloc[0]):
                continue

            trace = lap_metric_trace(session, lap_no)
            agg = segment_aggregates(trace, segments)
            agg_row = agg.loc[agg["segment_label"] == corner_label]

            row = {
                "session_label": label,
                "lap_number": lap_no,
                "corner_time_s": float(time_row["time_s"].iloc[0]),
                "entry_speed_kmh": np.nan,
                "entry_rpm": np.nan,
                "apex_speed_kmh": np.nan,
                "apex_rpm": np.nan,
                "exit_speed_kmh": np.nan,
                "exit_rpm": np.nan,
            }
            if not agg_row.empty:
                a = agg_row.iloc[0]
                row.update(
                    {
                        "entry_speed_kmh": a["entry_speed_kmh"],
                        "entry_rpm": a["entry_rpm"],
                        "apex_speed_kmh": a["apex_speed_kmh"],
                        "apex_rpm": a["apex_rpm"],
                        "exit_speed_kmh": a["exit_speed_kmh"],
                        "exit_rpm": a["exit_rpm"],
                    }
                )
            rows.append(row)

    return pd.DataFrame(rows)
