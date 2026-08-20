"""Shared synthetic single/double-corner track builder for the corner-causal-
engine tests (corner_causal.py / corner_engine.py). Not a test module itself
(no test_ functions) -- just a helper importable by the actual test modules.

Speed-vs-distance and G-vs-distance are controlled independently via
separate breakpoint profiles: the speed profile determines actual lap
timing (via ds/v(s) integration to build a session_time_s column), while
the longitudinal/lateral-G profiles independently control
braking_estimate/power_on_estimate triggering. Real telemetry has G
approximately track the speed derivative, but decoupling them here makes it
possible to construct an exact, predictable entry/apex/exit point without
fighting numerical-differentiation noise in a hand-built fixture.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from telemetry.parser import Session

SAMPLE_SPACING_M = 1.0


def build_lap_session(
    total_distance_m: float,
    speed_breakpoints: tuple[list[float], list[float]],
    lon_g_breakpoints: tuple[list[float], list[float]],
    lateral_g_breakpoints: tuple[list[float], list[float]],
    session_id: int = 0,
    lap_number: int = 1,
) -> Session:
    """One synthetic lap as a `Session`, sampled every `SAMPLE_SPACING_M`
    along distance. `*_breakpoints` are each an (x, y) pair passed straight
    to `np.interp`."""
    distance = np.arange(0.0, total_distance_m + SAMPLE_SPACING_M, SAMPLE_SPACING_M)
    speed_kmh = np.interp(distance, *speed_breakpoints)
    lon_g = np.interp(distance, *lon_g_breakpoints)
    lateral_g = np.interp(distance, *lateral_g_breakpoints)

    speed_ms = np.maximum(speed_kmh / 3.6, 0.5)
    dt = np.diff(distance) / speed_ms[:-1]
    t = np.concatenate([[0.0], np.cumsum(dt)])

    lat0, lon0 = 55.0, 11.0
    n = len(distance)
    df = pd.DataFrame(
        {
            "Start Date": ["16-08-2026"] * n, "Start Time": ["10:00:00"] * n,
            "Lap Number": [lap_number] * n, "Session Time": t * 1e9, "Lap Time": t * 1e9,
            "session_time_s": t, "lap_time_s": t,
            "Latitude": lat0 + distance * 1e-6, "Longitude": np.full(n, lon0), "Heading": np.zeros(n),
            "Vertical Acceleration": np.zeros(n), "GPS Speed": speed_kmh,
            "Horizontal DOP": np.full(n, 0.9), "GPS Lateral Acceleration": lateral_g,
            "GPS Longitudinal Acceleration": lon_g, "Vertical DOP": np.ones(n),
            "Positional DOP": np.ones(n), "Altitude": np.full(n, 10.0),
            "GPS Distance": distance, "RPM": np.full(n, np.nan), "RPM unfiltered": np.full(n, np.nan),
        }
    )
    return Session(session_id=session_id, source_file="synthetic", df=df)


def single_corner_segments(corner_start_m: float, corner_end_m: float, total_m: float) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"label": "Straight 1", "kind": "straight", "start_m": 0.0, "end_m": corner_start_m},
            {"label": "Corner 1", "kind": "corner", "start_m": corner_start_m, "end_m": corner_end_m},
            {"label": "Straight 2", "kind": "straight", "start_m": corner_end_m, "end_m": total_m},
        ]
    )


def two_corner_segments(
    straight_end_m: float, corner1_end_m: float, corner2_start_m: float, corner2_end_m: float, total_m: float
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"label": "Straight 1", "kind": "straight", "start_m": 0.0, "end_m": straight_end_m},
            {"label": "Corner 1", "kind": "corner", "start_m": straight_end_m, "end_m": corner1_end_m},
            {"label": "Corner 2", "kind": "corner", "start_m": corner2_start_m, "end_m": corner2_end_m},
            {"label": "Straight 2", "kind": "straight", "start_m": corner2_end_m, "end_m": total_m},
        ]
    )
