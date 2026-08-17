"""Gearing-change simulation: re-estimate RPM, speed, and lap time for a
different front/rear sprocket combination, using only this session's own
telemetry -- there's no dyno power curve in this data, so the model is
built empirically from the session's own measured acceleration-vs-RPM
behaviour rather than assumed physics constants.

Method, in order:
1. `fit_speed_rpm_scale` -- engine RPM is (very nearly) directly
   proportional to road speed for a fixed gear ratio and tyre size
   (RPM = k * speed_kmh); `k` is fit empirically across many samples so
   the model doesn't need to know the tyre's exact rolling radius.
2. `build_accel_rpm_curve` -- longitudinal acceleration as a function of
   RPM, binned from inferred power-on samples across a session's clean
   laps. This stands in for a torque/power curve the export doesn't
   provide.
3. `simulate_gearing_change` -- re-integrates one lap's speed trace
   forward through distance, using the *new* gear ratio's implied RPM at
   each point to look up `accel_rpm_curve` during power-on phases, and
   simply replaying the lap's own recorded speed change during
   braking/coast phases (gearing doesn't change how hard the brakes bite).

This is a first-order approximation, not a full vehicle-dynamics
simulator: it holds racing line, braking points, and driver inputs fixed,
and assumes the accel-vs-RPM relationship itself doesn't shift with the
new gearing (in reality traction and engine response can change a little
too). Treat the output as a directional estimate, not a guaranteed
lap-time number -- see the caveats surfaced alongside it in the UI.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .metrics import add_braking_throttle_estimates, lap_metric_trace
from .setup_config import KartSetup

MIN_SPEED_FOR_RATIO_FIT_KMH = 20.0  # excludes near-standstill samples, where RPM/speed is noisy
RPM_BIN_WIDTH = 250
KMH_PER_MS = 3.6
G_TO_MS2 = 9.81


def fit_speed_rpm_scale(session, lap_numbers: list[int]) -> float | None:
    """Empirical k such that RPM ~= k * GPS Speed (km/h), fit as the
    median ratio across many samples from the given laps.

    Encodes tyre rolling radius and the CURRENT gear ratio together
    without needing either as a separate input -- only the *change* in
    ratio matters for the simulation, and the tyre-radius term cancels
    out of that change. Returns None if there isn't enough RPM+speed data
    to fit anything.
    """
    ratios = []
    for lap_no in lap_numbers:
        trace = lap_metric_trace(session, lap_no)
        if trace.empty:
            continue
        valid = trace.dropna(subset=["RPM", "GPS Speed"])
        valid = valid[valid["GPS Speed"] > MIN_SPEED_FOR_RATIO_FIT_KMH]
        if valid.empty:
            continue
        ratios.extend((valid["RPM"] / valid["GPS Speed"]).tolist())
    if not ratios:
        return None
    return float(np.median(ratios))


def build_accel_rpm_curve(session, lap_numbers: list[int], rpm_bin_width: int = RPM_BIN_WIDTH) -> pd.DataFrame:
    """Median longitudinal acceleration (g) binned by RPM, from inferred
    power-on samples across the given laps -- a data-derived proxy for
    the engine+drivetrain's real accel capability at each RPM under the
    CURRENT gearing, since no dyno power curve exists for this data.
    """
    parts = []
    for lap_no in lap_numbers:
        trace = lap_metric_trace(session, lap_no)
        if trace.empty:
            continue
        trace = add_braking_throttle_estimates(trace)
        power_on = trace[
            trace["power_on_estimate"] & trace["RPM"].notna() & trace["GPS Longitudinal Acceleration"].notna()
        ]
        if power_on.empty:
            continue
        parts.append(power_on[["RPM", "GPS Longitudinal Acceleration"]])

    if not parts:
        return pd.DataFrame(columns=["rpm_bin_center", "accel_g", "n_samples"])

    combined = pd.concat(parts, ignore_index=True)
    combined["rpm_bin"] = (combined["RPM"] // rpm_bin_width * rpm_bin_width) + rpm_bin_width / 2
    curve = combined.groupby("rpm_bin")["GPS Longitudinal Acceleration"].agg(["median", "count"]).reset_index()
    curve.columns = ["rpm_bin_center", "accel_g", "n_samples"]
    return curve.sort_values("rpm_bin_center").reset_index(drop=True)


def _lookup_accel(rpm: float, curve: pd.DataFrame) -> float:
    """Interpolate accel_g for a given RPM from the empirical curve,
    clamping to the curve's own RPM range at the edges -- extrapolation
    beyond measured RPMs isn't something the data can support."""
    if curve.empty:
        return 0.0
    return float(np.interp(rpm, curve["rpm_bin_center"], curve["accel_g"]))


def simulate_gearing_change(
    session,
    lap_number: int,
    setup: KartSetup,
    rear_teeth_delta: int,
    front_teeth_delta: int,
    speed_rpm_scale: float,
    accel_curve: pd.DataFrame,
) -> pd.DataFrame:
    """Re-simulate one lap's speed/RPM trace under a changed gear ratio.

    Braking/coast phases replay the lap's own recorded speed change
    exactly (gearing doesn't change brake bite). Power-on phases
    re-integrate speed forward using `accel_curve` looked up at the
    *simulated* RPM (from `speed_rpm_scale` and the new ratio) at each
    step, so a change in acceleration capability at the new RPM feeds
    back into the simulated speed for the rest of the straight.
    """
    front = setup.gearing.front_teeth or 10
    rear = setup.gearing.rear_teeth or 80
    new_front = max(front + front_teeth_delta, 1)
    new_rear = max(rear + rear_teeth_delta, 1)
    ratio_scale = (new_rear / new_front) / (rear / front)

    trace = lap_metric_trace(session, lap_number)
    if trace.empty:
        return pd.DataFrame()
    trace = add_braking_throttle_estimates(trace)
    trace = trace.dropna(subset=["lap_distance_m", "GPS Speed"]).sort_values("lap_distance_m").reset_index(drop=True)
    if len(trace) < 2:
        return pd.DataFrame()

    distance = trace["lap_distance_m"].to_numpy(dtype=float)
    speed_actual = trace["GPS Speed"].to_numpy(dtype=float)
    power_on = trace["power_on_estimate"].fillna(False).to_numpy()
    rpm_actual = trace["RPM"].to_numpy(dtype=float)

    speed_sim = np.empty(len(trace))
    speed_sim[0] = speed_actual[0]

    for i in range(1, len(trace)):
        d_dist = max(distance[i] - distance[i - 1], 0.0)
        if power_on[i] and speed_rpm_scale:
            rpm_sim_prev = speed_sim[i - 1] * speed_rpm_scale * ratio_scale
            accel_ms2 = _lookup_accel(rpm_sim_prev, accel_curve) * G_TO_MS2
            v0_ms = speed_sim[i - 1] / KMH_PER_MS
            v1_ms = np.sqrt(max(v0_ms**2 + 2 * accel_ms2 * d_dist, 0.0))
            speed_sim[i] = v1_ms * KMH_PER_MS
        else:
            # Coast/braking: replay the actual recorded speed change --
            # gearing doesn't change how hard the brakes bite.
            speed_sim[i] = speed_sim[i - 1] + (speed_actual[i] - speed_actual[i - 1])
        speed_sim[i] = max(speed_sim[i], 0.5)  # avoid divide-by-zero downstream

    rpm_sim = speed_sim * speed_rpm_scale * ratio_scale if speed_rpm_scale else np.full(len(trace), np.nan)

    return pd.DataFrame(
        {
            "distance_m": distance,
            "session_time_s": trace["session_time_s"].to_numpy(),
            "speed_kmh_actual": speed_actual,
            "speed_kmh_sim": speed_sim,
            "rpm_actual": rpm_actual,
            "rpm_sim": rpm_sim,
            "power_on": power_on,
        }
    )


def estimate_lap_time_delta(sim_trace: pd.DataFrame, actual_lap_time_s: float) -> dict:
    """Integrate the simulated and actual speed traces over the same
    distance grid to estimate a lap-time delta, applied against the real
    recorded lap time (a relative delta, not an absolute re-derivation)
    so small numerical-integration bias cancels between the two rather
    than compounding into the headline number.
    """
    if sim_trace.empty or len(sim_trace) < 2:
        return {"sim_lap_time_s": actual_lap_time_s, "delta_s": 0.0}

    d_dist = np.clip(np.diff(sim_trace["distance_m"].to_numpy()), 0, None)
    v_actual_ms = np.clip(sim_trace["speed_kmh_actual"].to_numpy()[:-1] / KMH_PER_MS, 0.5, None)
    v_sim_ms = np.clip(sim_trace["speed_kmh_sim"].to_numpy()[:-1] / KMH_PER_MS, 0.5, None)

    integrated_actual_s = float(np.sum(d_dist / v_actual_ms))
    integrated_sim_s = float(np.sum(d_dist / v_sim_ms))
    delta_s = integrated_sim_s - integrated_actual_s

    return {
        "sim_lap_time_s": actual_lap_time_s + delta_s,
        "delta_s": delta_s,
        "integrated_actual_s": integrated_actual_s,
        "integrated_sim_s": integrated_sim_s,
    }
