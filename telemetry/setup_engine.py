"""Setup correlation engine: cross-references kart setup inputs against
telemetry patterns to flag likely mismatches.

None of this is a direct measurement -- there's no EGT/lambda, throttle, or
brake channel in the export -- so every suggestion here carries a
`confidence` ("low"/"medium") and is phrased as a hypothesis to test, never
a certainty. Defaults (e.g. the Rotax EVO peak-power RPM band) are
parameters, not hard-coded constants, since engine spec varies by year/tune
and should be confirmed against the actual engine builder's spec sheet.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .metrics import add_braking_throttle_estimates, lap_metric_trace
from .setup_config import KartSetup

# Rotax Senior EVO peak-power RPM band -- a reasonable default, NOT a
# confirmed spec. Override with the actual engine builder's numbers.
DEFAULT_PEAK_POWER_RPM_BAND = (11_500, 13_000)


def gearing_suggestion(
    session,
    clean_lap_numbers: list[int],
    segments: pd.DataFrame,
    setup: KartSetup,
    peak_power_rpm_band: tuple[int, int] = DEFAULT_PEAK_POWER_RPM_BAND,
) -> dict:
    """Flag over/under-gearing from peak RPM on the longest straight."""
    straights = segments[segments["kind"] == "straight"].copy()
    if straights.empty:
        return {"area": "gearing", "confidence": "low", "hypothesis": "No straights detected to evaluate top-end RPM."}
    straights["length_m"] = straights["end_m"] - straights["start_m"]
    longest = straights.sort_values("length_m", ascending=False).iloc[0]

    max_rpms = []
    for lap_no in clean_lap_numbers:
        trace = lap_metric_trace(session, lap_no)
        if trace.empty:
            continue
        in_seg = trace[(trace["lap_distance_m"] >= longest["start_m"]) & (trace["lap_distance_m"] < longest["end_m"])]
        if in_seg["RPM"].notna().any():
            max_rpms.append(in_seg["RPM"].max())

    if not max_rpms:
        return {"area": "gearing", "confidence": "low", "hypothesis": "No RPM data on the longest straight."}

    peak_rpm = float(np.median(max_rpms))
    low, high = peak_power_rpm_band
    result = {
        "area": "gearing",
        "peak_rpm_on_longest_straight": peak_rpm,
        "peak_power_rpm_band": peak_power_rpm_band,
        "current_ratio": setup.gearing.gear_ratio,
        "confidence": "medium",
    }

    # Engine RPM = axle RPM * (rear_teeth / front_teeth) for a direct-drive
    # kart, so to change RPM at a *given* speed you move the ratio, not just
    # "add a tooth" in whichever direction sounds right: lowering RPM means
    # lowering the ratio (fewer rear teeth or more front teeth), and raising
    # RPM means raising it (more rear teeth or fewer front teeth).
    if peak_rpm > high:
        result["hypothesis"] = (
            f"Peak RPM on the longest straight ({peak_rpm:.0f}) is above the assumed peak-power band "
            f"({low}-{high}), suggesting the kart is over-revving there -- likely under-geared."
        )
        result["suggested_action"] = "Gear up: remove a tooth from the rear sprocket (or add one to the front) to lower RPM at top speed."
    elif peak_rpm < low:
        result["hypothesis"] = (
            f"Peak RPM on the longest straight ({peak_rpm:.0f}) is below the assumed peak-power band "
            f"({low}-{high}), suggesting the kart isn't reaching peak power before the braking zone -- likely over-geared."
        )
        result["suggested_action"] = "Gear down: add a tooth to the rear sprocket (or remove one from the front) to reach peak RPM sooner."
    else:
        result["hypothesis"] = f"Peak RPM on the longest straight ({peak_rpm:.0f}) sits within the assumed peak-power band -- gearing looks reasonable for this track."
        result["suggested_action"] = None

    return result


def jetting_suggestion(session, clean_lap_numbers: list[int]) -> dict:
    """Flag possible rich/lean jetting from RPM hesitation during inferred
    power-on phases (no EGT/lambda channel exists, so this is a coarse
    proxy -- verify with a plug chop before changing jets)."""
    hesitation_events = 0
    power_on_windows = 0

    for lap_no in clean_lap_numbers:
        trace = lap_metric_trace(session, lap_no)
        if trace.empty or trace["RPM"].isna().all():
            continue
        trace = add_braking_throttle_estimates(trace)
        trace = trace.sort_values("lap_distance_m")
        rpm = trace["RPM"].ffill()
        power_on = trace["power_on_estimate"].fillna(False).to_numpy()

        rpm_diff = rpm.diff().to_numpy()
        in_window = False
        for i in range(1, len(power_on)):
            if power_on[i] and not in_window:
                in_window = True
                power_on_windows += 1
            if power_on[i] and rpm_diff[i] < -50:
                hesitation_events += 1
            if not power_on[i]:
                in_window = False

    if power_on_windows == 0:
        return {"area": "jetting", "confidence": "low", "hypothesis": "No clear power-on phases detected to evaluate."}

    hesitation_rate = hesitation_events / power_on_windows
    result = {
        "area": "jetting",
        "confidence": "low",
        "hesitation_rate": hesitation_rate,
        "power_on_windows_analyzed": power_on_windows,
    }
    if hesitation_rate > 0.3:
        result["hypothesis"] = (
            f"RPM dips/flat spots appear in ~{hesitation_rate:.0%} of power-on phases, which can indicate a "
            "rich or lean condition (or an unrelated mechanical hesitation)."
        )
        result["suggested_action"] = "Low-confidence signal only -- verify with a plug chop before changing main jet size. If confirmed lean (light/blistered plug), richen; if rich (sooty/wet plug), lean off."
    else:
        result["hypothesis"] = "No strong hesitation pattern detected in power-on RPM traces -- jetting is not flagged as a likely issue from this data alone."
        result["suggested_action"] = None
    return result


def tyre_pressure_suggestion(session, clean_lap_numbers: list[int], setup: KartSetup) -> dict:
    """Correlate lateral-G consistency / min-speed trend across the session
    (tyres coming in or going off) against the stated pressures."""
    sorted_laps = sorted(clean_lap_numbers)
    if len(sorted_laps) < 4:
        return {"area": "tyre_pressure", "confidence": "low", "hypothesis": "Not enough clean laps to assess a tyre trend across the session."}

    mid = len(sorted_laps) // 2
    first_half, second_half = sorted_laps[:mid], sorted_laps[mid:]

    def avg_lateral_g_std(laps):
        vals = []
        for lap_no in laps:
            trace = lap_metric_trace(session, lap_no)
            if not trace.empty and trace["GPS Lateral Acceleration"].notna().any():
                vals.append(trace["GPS Lateral Acceleration"].std())
        return float(np.mean(vals)) if vals else np.nan

    early = avg_lateral_g_std(first_half)
    late = avg_lateral_g_std(second_half)
    if np.isnan(early) or np.isnan(late):
        return {"area": "tyre_pressure", "confidence": "low", "hypothesis": "Insufficient lateral-G data to assess a tyre trend."}

    change = late - early
    result = {
        "area": "tyre_pressure",
        "confidence": "low",
        "lateral_g_std_early": early,
        "lateral_g_std_late": late,
        "stated_hot_pressure_front_bar": setup.tyres.hot_pressure_front_bar,
        "stated_hot_pressure_rear_bar": setup.tyres.hot_pressure_rear_bar,
    }
    if change > 0.03:
        result["hypothesis"] = "Cornering grip (lateral-G consistency) degrades noticeably through the session, consistent with tyres going off."
        result["suggested_action"] = "Consider a small pressure reduction next session, or check if current pressures are running hot/over-pressured as the tyre heats up."
    elif change < -0.03:
        result["hypothesis"] = "Cornering grip improves through the session, consistent with tyres needing heat cycles to come in."
        result["suggested_action"] = "Consider a slightly higher starting pressure to reach the working range sooner, or extend the out-lap."
    else:
        result["hypothesis"] = "No strong grip trend across the session -- tyre pressures look reasonably matched to conditions."
        result["suggested_action"] = None
    return result


def chassis_balance_suggestion(session, clean_lap_numbers: list[int], segments: pd.DataFrame) -> dict:
    """Flag left/right corner asymmetry that may point to a chassis balance
    (seat position, caster, track width) adjustment."""
    corners = segments[segments["kind"] == "corner"]
    if corners.empty:
        return {"area": "chassis_balance", "confidence": "low", "hypothesis": "No corners detected to assess balance."}

    left_speeds, right_speeds = [], []
    for lap_no in clean_lap_numbers:
        trace = lap_metric_trace(session, lap_no)
        if trace.empty:
            continue
        for _, seg in corners.iterrows():
            in_seg = trace[(trace["lap_distance_m"] >= seg["start_m"]) & (trace["lap_distance_m"] < seg["end_m"])]
            if in_seg.empty or in_seg["GPS Lateral Acceleration"].isna().all():
                continue
            mean_lat_g = in_seg["GPS Lateral Acceleration"].mean()
            min_speed = in_seg["GPS Speed"].min()
            if mean_lat_g > 0:
                right_speeds.append(min_speed)
            else:
                left_speeds.append(min_speed)

    if len(left_speeds) < 2 or len(right_speeds) < 2:
        return {"area": "chassis_balance", "confidence": "low", "hypothesis": "Not enough distinct left/right corners to assess balance asymmetry."}

    left_avg, right_avg = float(np.mean(left_speeds)), float(np.mean(right_speeds))
    gap = right_avg - left_avg
    result = {
        "area": "chassis_balance",
        "confidence": "low",
        "avg_min_speed_left_kmh": left_avg,
        "avg_min_speed_right_kmh": right_avg,
    }
    if abs(gap) > 3:
        weaker = "left-hand" if gap > 0 else "right-hand"
        result["hypothesis"] = f"Minimum corner speed is {abs(gap):.1f} km/h lower in {weaker} corners on average, suggesting an asymmetric balance issue (seat position, caster, or track width) rather than a one-off corner."
        result["suggested_action"] = f"Check seat position and caster/track-width symmetry; consider a small adjustment to help the kart rotate more evenly into {weaker} corners."
    else:
        result["hypothesis"] = "Left/right corner speeds are reasonably balanced -- no strong asymmetry flagged."
        result["suggested_action"] = None
    return result


def all_setup_suggestions(session, clean_lap_numbers: list[int], segments: pd.DataFrame, setup: KartSetup) -> list[dict]:
    peak_power_rpm_band = (setup.peak_power_rpm_low, setup.peak_power_rpm_high)
    return [
        gearing_suggestion(session, clean_lap_numbers, segments, setup, peak_power_rpm_band=peak_power_rpm_band),
        jetting_suggestion(session, clean_lap_numbers),
        tyre_pressure_suggestion(session, clean_lap_numbers, setup),
        chassis_balance_suggestion(session, clean_lap_numbers, segments),
    ]
