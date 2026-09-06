"""Per-lap metric traces: speed, RPM, G-forces, braking/throttle inference,
and per-corner aggregates.

There is no throttle, brake, or gear channel in the confirmed export, so
braking and throttle-application are inferred from longitudinal G / RPM
rate-of-change against GPS speed. Every value derived this way is labeled
`_estimate` in its column name / output so downstream code and the UI can't
accidentally present it as a direct measurement.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .corners import assign_segments, lap_gps_trace
from .parser import Session

BRAKING_DECEL_THRESHOLD_G = -0.3  # longitudinal G more negative than this counts as braking
POWER_ON_ACCEL_THRESHOLD_G = 0.15  # longitudinal G more positive than this counts as power-on


def lap_metric_trace(session: Session, lap_number: int) -> pd.DataFrame:
    """Distance-aligned trace of speed, RPM, and G-forces for one lap.

    Anchored on the GPS fix rows (speed/lat-g/lon-g/heading all update
    together), with RPM merged on by nearest timestamp -- RPM is the fastest
    channel so this avoids distorting it by forward-filling onto a slower
    grid.
    """
    trace = lap_gps_trace(session, lap_number)
    if trace.empty:
        return trace

    rpm = session.extract_channel("RPM")
    rpm = rpm.loc[rpm["Lap Number"] == lap_number].sort_values("session_time_s")
    rpm_unfiltered = session.extract_channel("RPM unfiltered")
    rpm_unfiltered = rpm_unfiltered.loc[rpm_unfiltered["Lap Number"] == lap_number].sort_values("session_time_s")

    trace = trace.sort_values("session_time_s")
    if not rpm.empty:
        trace = pd.merge_asof(
            trace, rpm[["session_time_s", "RPM"]], on="session_time_s", direction="nearest", tolerance=0.2
        )
    else:
        trace["RPM"] = np.nan
    if not rpm_unfiltered.empty:
        trace = pd.merge_asof(
            trace,
            rpm_unfiltered[["session_time_s", "RPM unfiltered"]],
            on="session_time_s",
            direction="nearest",
            tolerance=0.2,
        )
    else:
        trace["RPM unfiltered"] = np.nan

    trace = trace.sort_values("lap_distance_m").reset_index(drop=True)
    return trace


def add_engine_temperature(session: Session, lap_number: int, trace: pd.DataFrame) -> pd.DataFrame:
    """Merge the engine temperature channel onto a lap trace.

    Kept out of `lap_metric_trace` so that function keeps the exact shape the
    extraction was verified against; nothing in the coaching pipeline reads
    temperature, and only the engine page does.

    "Temperature 1" is the engine sensor. "Internal Temperature" is the
    logger's own -- it sits around 24-26C and would be a plausible-looking
    wrong answer on an engine page.

    Merged by nearest timestamp like RPM, because temperature logs on its own
    cadence and never shares a row with the GPS fix (see the parser's notes on
    sparse channels).
    """
    if trace.empty:
        return trace

    temperature = session.extract_channel("Temperature 1")
    temperature = temperature.loc[temperature["Lap Number"] == lap_number].sort_values(
        "session_time_s"
    )
    if temperature.empty:
        trace = trace.copy()
        trace["Temperature 1"] = np.nan
        return trace

    merged = pd.merge_asof(
        trace.sort_values("session_time_s"),
        temperature[["session_time_s", "Temperature 1"]],
        on="session_time_s",
        direction="nearest",
        tolerance=1.0,
    )
    return merged.sort_values("lap_distance_m").reset_index(drop=True)


def add_braking_throttle_estimates(trace: pd.DataFrame) -> pd.DataFrame:
    """Flag inferred braking / power-on phases from longitudinal G and RPM.

    `braking_estimate` / `power_on_estimate` are booleans, not measurements
    of an actual brake or throttle channel -- neither exists in this export.
    """
    trace = trace.copy()
    lon_g = trace.get("GPS Longitudinal Acceleration")
    if lon_g is None:
        trace["braking_estimate"] = False
        trace["power_on_estimate"] = False
        return trace

    trace["braking_estimate"] = lon_g < BRAKING_DECEL_THRESHOLD_G
    trace["power_on_estimate"] = lon_g > POWER_ON_ACCEL_THRESHOLD_G
    return trace


def braking_zones(trace: pd.DataFrame) -> pd.DataFrame:
    """Contiguous braking zones (estimate) as distance ranges with peak
    deceleration and duration."""
    trace = trace.dropna(subset=["lap_distance_m"]).sort_values("lap_distance_m").reset_index(drop=True)
    if "braking_estimate" not in trace.columns:
        trace = add_braking_throttle_estimates(trace)

    zones = []
    in_zone = False
    start_i = None
    for i, row in trace.iterrows():
        if row["braking_estimate"] and not in_zone:
            in_zone = True
            start_i = i
        elif not row["braking_estimate"] and in_zone:
            in_zone = False
            zone_df = trace.loc[start_i : i - 1]
            zones.append(_summarize_zone(zone_df))
    if in_zone:
        zone_df = trace.loc[start_i:]
        zones.append(_summarize_zone(zone_df))
    return pd.DataFrame(zones)


def _summarize_zone(zone_df: pd.DataFrame) -> dict:
    return {
        "brake_point_m": zone_df["lap_distance_m"].iloc[0],
        "end_m": zone_df["lap_distance_m"].iloc[-1],
        "duration_s": zone_df["session_time_s"].iloc[-1] - zone_df["session_time_s"].iloc[0],
        "peak_decel_g": zone_df["GPS Longitudinal Acceleration"].min(),
        "entry_speed_kmh": zone_df["GPS Speed"].iloc[0],
        "exit_speed_kmh": zone_df["GPS Speed"].iloc[-1],
    }


def segment_aggregates(trace: pd.DataFrame, segments: pd.DataFrame) -> pd.DataFrame:
    """Min/max/avg speed and entry/apex/exit RPM+speed per segment for one
    lap's trace.

    Entry = first point in the segment (by distance), exit = last point.
    Apex = the segment's minimum-speed point -- for a corner that's the
    conventional apex; for a straight it's just wherever the trace reads
    slowest within it (typically right at entry), included for consistency
    rather than because "apex" is a meaningful concept on a straight.
    """
    trace = assign_segments(trace, segments)
    rows = []
    for label, g in trace.groupby("segment_label"):
        if label is None or g.empty:
            continue
        g = g.sort_values("lap_distance_m")
        kind = g["segment_kind"].iloc[0]
        entry = g.iloc[0]
        exit_ = g.iloc[-1]
        apex = g.loc[g["GPS Speed"].idxmin()] if g["GPS Speed"].notna().any() else None

        row = {
            "segment_label": label,
            "segment_kind": kind,
            "min_speed_kmh": g["GPS Speed"].min(),
            "max_speed_kmh": g["GPS Speed"].max(),
            "avg_speed_kmh": g["GPS Speed"].mean(),
            "entry_speed_kmh": entry["GPS Speed"],
            "entry_rpm": entry["RPM"],
            "apex_speed_kmh": apex["GPS Speed"] if apex is not None else np.nan,
            "apex_rpm": apex["RPM"] if apex is not None else np.nan,
            "exit_speed_kmh": exit_["GPS Speed"],
            "exit_rpm": exit_["RPM"],
            "lateral_g_std": g["GPS Lateral Acceleration"].std(),
        }
        rows.append(row)
    return pd.DataFrame(rows)


def gg_diagram_points(trace: pd.DataFrame) -> pd.DataFrame:
    """Lateral vs. longitudinal G points for a lap's friction-circle plot."""
    cols = ["GPS Lateral Acceleration", "GPS Longitudinal Acceleration", "lap_distance_m", "GPS Speed"]
    available = [c for c in cols if c in trace.columns]
    return trace[available].dropna(subset=["GPS Lateral Acceleration", "GPS Longitudinal Acceleration"])


def time_in_rpm_band(session: Session, lap_number: int, rpm_band: tuple[float, float]) -> dict:
    """Fraction of a lap's time spent with RPM inside `rpm_band` (the
    engine's peak-power range).

    Uses the RPM channel's own native samples directly (not the GPS-fix-rate
    `lap_metric_trace`), so the fraction reflects RPM's full time resolution
    rather than being coarsened to the ~10Hz GPS update rate.
    """
    rpm_series = session.extract_channel("RPM")
    rpm_series = rpm_series.loc[rpm_series["Lap Number"] == lap_number].sort_values("session_time_s")
    if len(rpm_series) < 2:
        return {"time_in_band_s": 0.0, "lap_duration_s": 0.0, "fraction_in_band": float("nan")}

    times = rpm_series["session_time_s"].to_numpy()
    rpm = rpm_series["RPM"].to_numpy()
    dt = np.diff(times)
    # each interval's RPM state is taken from the sample at its start
    in_band = (rpm[:-1] >= rpm_band[0]) & (rpm[:-1] <= rpm_band[1])

    time_in_band = float(dt[in_band].sum())
    lap_duration = float(times[-1] - times[0])
    return {
        "time_in_band_s": time_in_band,
        "lap_duration_s": lap_duration,
        "fraction_in_band": time_in_band / lap_duration if lap_duration > 0 else float("nan"),
    }


def rpm_band_summary_across_laps(session: Session, lap_numbers: list[int], rpm_band: tuple[float, float]) -> pd.DataFrame:
    """Per-lap time-in-peak-power-band, for the "how much of the lap is
    spent in the engine's peak-power zone" view."""
    rows = []
    for lap_no in lap_numbers:
        result = time_in_rpm_band(session, lap_no, rpm_band)
        result["lap_number"] = lap_no
        rows.append(result)
    return pd.DataFrame(rows)[["lap_number", "time_in_band_s", "lap_duration_s", "fraction_in_band"]]


def consistency_stats(laps: pd.DataFrame) -> dict:
    """Lap-time consistency: std dev and a simple fade/improvement trend."""
    if laps.empty:
        return {}
    times = laps.sort_values("lap_number")["lap_time_s"]
    trend_slope = np.polyfit(range(len(times)), times, 1)[0] if len(times) >= 2 else 0.0
    return {
        "std_dev_s": times.std(),
        "trend_s_per_lap": trend_slope,
        "trend_direction": "fading" if trend_slope > 0.02 else ("improving" if trend_slope < -0.02 else "stable"),
    }
