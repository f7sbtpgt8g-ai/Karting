"""Cross-lap and cross-session comparison: reference laps, progression over
time, and driver-vs-driver benchmarking.

Works across two different `Session` objects (not just two laps within the
same session) so a reference lap can come from a teammate's file or an
earlier session's personal best.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .delta import _time_vs_distance
from .laps import clean_lap_table, flag_outlier_laps, lap_table, summarize_laps


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
    from .delta import segment_times_for_lap

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
