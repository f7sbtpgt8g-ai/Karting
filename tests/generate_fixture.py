"""Generates a synthetic Unipro-format TSV fixture for tests.

Not a pytest file -- run directly (`python tests/generate_fixture.py`) to
(re)write `tests/fixtures/synthetic_session.tsv`.

This exists because the real sample export (`Unipro__1_.tsv`) referenced by
the original spec was not actually available in this environment. The
generator reproduces the *structural* quirks confirmed from that file --
sparse single-channel-per-row events, nanosecond time units, a mid-file
session reset, an outlier "incident" lap -- from a simple simulated oval
track, so the parser/analysis modules can be exercised end-to-end. It is a
stand-in for real telemetry, not a substitute for validating against an
actual export.
"""

from __future__ import annotations

import csv
import math
import os
import random

import numpy as np

from telemetry.parser import COLUMNS, GPS_FIX_COLUMNS

random.seed(42)
np.random.seed(42)

LAT0, LON0 = 55.350, 11.160
M_PER_DEG_LAT = 111_320.0


def m_to_latlon(x, y):
    lat = LAT0 + y / M_PER_DEG_LAT
    lon = LON0 + x / (M_PER_DEG_LAT * math.cos(math.radians(LAT0)))
    return lat, lon


# Track as (kind, length_m_or_arc_angle_deg, radius_m, turn_sign, target_speed_kmh)
# turn_sign: +1 = right-hand turn, -1 = left-hand turn.
TRACK_SEGMENTS = [
    ("straight", 250, None, 0, 108),
    ("corner", 90, 15, +1, 46),
    ("straight", 120, None, 0, 90),
    ("corner", 135, 10, -1, 36),
    ("straight", 180, None, 0, 100),
    ("corner", 90, 20, +1, 55),
    ("straight", 80, None, 0, 75),
    ("corner", 45, 25, -1, 65),
]


def build_track(ds=0.5):
    """Walk the segment list and return arrays of (s, x, y, heading_deg,
    target_speed_kmh) at fine resolution, forming one lap's path."""
    s_list, x_list, y_list, heading_list, speed_list = [], [], [], [], []
    x, y, heading = 0.0, 0.0, 0.0
    s = 0.0

    for kind, length_or_angle, radius, turn_sign, target_kmh in TRACK_SEGMENTS:
        if kind == "straight":
            n = int(length_or_angle / ds)
            for _ in range(n):
                x += ds * math.cos(math.radians(heading))
                y += ds * math.sin(math.radians(heading))
                s += ds
                s_list.append(s)
                x_list.append(x)
                y_list.append(y)
                heading_list.append(heading)
                speed_list.append(target_kmh)
        else:
            arc_len = radius * math.radians(length_or_angle)
            n = max(int(arc_len / ds), 1)
            dtheta = length_or_angle / n
            for _ in range(n):
                heading += turn_sign * dtheta
                x += ds * math.cos(math.radians(heading))
                y += ds * math.sin(math.radians(heading))
                s += ds
                s_list.append(s)
                x_list.append(x)
                y_list.append(y)
                heading_list.append(heading)
                speed_list.append(target_kmh)

    return (
        np.array(s_list),
        np.array(x_list),
        np.array(y_list),
        np.array(heading_list),
        np.array(speed_list),
    )


def smooth(arr, sigma_pts):
    from scipy.ndimage import gaussian_filter1d

    return gaussian_filter1d(arr, sigma=sigma_pts, mode="wrap")


def simulate_lap(speed_noise_scale=1.0, incident_at_frac=None):
    """Return per-distance-point kinematics for one lap, in SI-ish units
    (speed in km/h, time in s from lap start)."""
    s, x, y, heading, target_kmh = build_track()
    target_kmh = smooth(target_kmh, sigma_pts=12)  # realistic braking/accel ramps, not step changes
    noise = np.random.normal(0, 1.5 * speed_noise_scale, size=len(target_kmh))
    speed_kmh = np.clip(target_kmh + smooth(noise, 6), 15, None)

    if incident_at_frac is not None:
        # A pronounced stoppage/spin: speed collapses to a near-stationary
        # crawl for a while before recovering, roughly tripling lap time --
        # matching the confirmed sample file's ~92s lap against a ~32s median.
        center = int(incident_at_frac * len(s))
        width = int(0.22 * len(s))
        dip = np.zeros(len(s))
        idx = np.arange(len(s))
        dip += 98 * np.exp(-0.5 * ((idx - center) / (width / 2)) ** 2)
        speed_kmh = np.clip(speed_kmh - dip, 4, None)

    speed_ms = speed_kmh / 3.6
    ds = np.gradient(s)
    dt = ds / speed_ms
    t = np.concatenate([[0.0], np.cumsum(dt)[:-1]])

    lat_g = np.zeros(len(s))
    for seg_start, seg_end, radius, turn_sign in _corner_ranges(s):
        mask = (s >= seg_start) & (s < seg_end)
        lat_g[mask] = turn_sign * (speed_ms[mask] ** 2 / radius) / 9.81

    lon_g = np.gradient(speed_ms) / np.gradient(t) / 9.81
    lon_g = smooth(lon_g, 4)

    rpm = 400 + speed_kmh * 131 + np.random.normal(0, 60, size=len(s))
    rpm = np.clip(rpm, 1200, None)
    rpm_unfiltered = rpm + np.random.normal(0, 220, size=len(s))

    lat, lon = [], []
    for xi, yi in zip(x, y):
        la, lo = m_to_latlon(xi, yi)
        lat.append(la)
        lon.append(lo)

    return {
        "s": s,
        "t": t,
        "heading": heading,
        "speed_kmh": speed_kmh,
        "lat_g": lat_g,
        "lon_g": lon_g,
        "rpm": rpm,
        "rpm_unfiltered": rpm_unfiltered,
        "lat": np.array(lat),
        "lon": np.array(lon),
    }


def _corner_ranges(s):
    """Recompute (start_s, end_s, radius, turn_sign) for each corner segment
    in TRACK_SEGMENTS, for lateral-G calculation."""
    ranges = []
    cursor = 0.0
    for kind, length_or_angle, radius, turn_sign, _ in TRACK_SEGMENTS:
        if kind == "straight":
            cursor += length_or_angle
        else:
            arc_len = radius * math.radians(length_or_angle)
            ranges.append((cursor, cursor + arc_len, radius, turn_sign))
            cursor += arc_len
    return ranges


def generate_session_rows(lap_specs, start_date, start_time, session_time_offset_s=0.0):
    """lap_specs: list of dicts with speed_noise_scale / incident_at_frac.
    Returns list of row dicts (session_time already offset) and the ending
    session time in seconds."""
    rows = []
    session_time_s = session_time_offset_s

    for lap_number, spec in enumerate(lap_specs):
        lap = simulate_lap(**spec)
        lap_duration = lap["t"][-1]
        n = len(lap["s"])

        # channel sample times (own native rate), in-lap seconds
        rpm_times = np.arange(0, lap_duration, 1 / 25.0)
        rpm_unf_times = np.arange(0, lap_duration, 1 / 25.0) + 0.003
        gps_times = np.arange(0, lap_duration, 1 / 10.0)
        dist_times = np.arange(0, lap_duration, 1 / 15.0)
        steer_times = np.arange(0, lap_duration, 1 / 6.0)
        temp_times = np.arange(0, lap_duration, 1 / 6.0) + 0.05
        battery_times = np.arange(0, lap_duration, 4.0)

        def interp(field, at):
            return np.interp(at, lap["t"], lap[field])

        def emit(t_in_lap, values: dict):
            row = {c: "" for c in COLUMNS}
            row["Start Date"] = start_date
            row["Start Time"] = start_time
            row["Lap Number"] = lap_number
            row["Session Time"] = round((session_time_s + t_in_lap) * 1_000_000_000)
            row["Lap Time"] = round(t_in_lap * 1_000_000_000)
            row.update(values)
            rows.append(row)

        for tt in rpm_times:
            emit(tt, {"RPM": round(float(interp("rpm", tt)), 3)})
        for tt in rpm_unf_times:
            if tt > lap_duration:
                continue
            emit(tt, {"RPM unfiltered": round(float(interp("rpm_unfiltered", tt)), 3)})
        for tt in gps_times:
            emit(
                tt,
                {
                    "Heading": round(float(interp("heading", tt)) % 360, 2),
                    "Vertical Acceleration": round(float(np.random.normal(0, 0.05)), 4),
                    "GPS Speed": round(float(interp("speed_kmh", tt)), 2),
                    "Horizontal DOP": 0.9,
                    "Latitude": round(float(interp("lat", tt)), 7),
                    "GPS Lateral Acceleration": round(float(interp("lat_g", tt)), 4),
                    "GPS Longitudinal Acceleration": round(float(interp("lon_g", tt)), 4),
                    "Vertical DOP": 1.1,
                    "Longitude": round(float(interp("lon", tt)), 7),
                    "Positional DOP": 1.4,
                    "Altitude": 12.0,
                },
            )
        for tt in dist_times:
            emit(tt, {"GPS Distance": round(session_time_offset_dist + float(interp("s", tt)), 2)})
        for tt in steer_times:
            emit(tt, {"Steering Angle": round(float(np.random.normal(0, 8)), 2)})
        for tt in temp_times:
            emit(tt, {"Temperature 1": round(20 + float(np.random.normal(0, 0.5)), 2)})
        for tt in battery_times:
            emit(tt, {"Battery Voltage": round(12.4 + float(np.random.normal(0, 0.05)), 2)})

        session_time_s += lap_duration

    return rows, session_time_s


session_time_offset_dist = 0.0  # module-level: GPS Distance is cumulative across the session, not reset per lap


def main():
    out_dir = os.path.join(os.path.dirname(__file__), "fixtures")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "synthetic_session.tsv")

    global session_time_offset_dist
    all_rows = []

    # Session 1: out-lap, 5 normal laps, one incident lap (spin mid-lap), one more normal lap.
    session_time_offset_dist = 0.0
    specs_1 = [
        {"speed_noise_scale": 1.6},  # lap 0: out-lap (slower, noisier)
        {"speed_noise_scale": 1.0},
        {"speed_noise_scale": 1.0},
        {"speed_noise_scale": 0.8},
        {"speed_noise_scale": 1.0, "incident_at_frac": 0.55},  # simulated spin
        {"speed_noise_scale": 1.0},
        {"speed_noise_scale": 0.9},
    ]
    rows_1, end_time_1 = generate_session_rows(specs_1, "16-08-2026", "10:00:00", session_time_offset_s=0.0)
    rows_1.sort(key=lambda r: r["Session Time"])  # real logger rows are chronological within a session
    all_rows.extend(rows_1)

    # Session 2 (new logger run: Session Time and Lap Number both reset to 0).
    session_time_offset_dist = 0.0
    specs_2 = [
        {"speed_noise_scale": 1.4},
        {"speed_noise_scale": 0.9},
        {"speed_noise_scale": 0.9},
        {"speed_noise_scale": 0.85},
    ]
    rows_2, _ = generate_session_rows(specs_2, "16-08-2026", "11:15:00", session_time_offset_s=0.0)
    rows_2.sort(key=lambda r: r["Session Time"])
    all_rows.extend(rows_2)

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f, delimiter="\t", quoting=csv.QUOTE_ALL)
        writer.writerow(COLUMNS)
        for row in all_rows:
            writer.writerow([row[c] for c in COLUMNS])

    print(f"Wrote {len(all_rows)} rows to {out_path}")


if __name__ == "__main__":
    main()
