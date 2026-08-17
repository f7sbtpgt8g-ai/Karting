"""Top 3 focus areas: the headline "where to gain the most time" report.

For each track segment, scores cumulative time loss vs. the driver's own
theoretical best (or a loaded reference/teammate lap), ranks segments by
gainable time, and attaches a diagnosed technical cause plus a plain-
language coaching note wherever the data supports one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .corners import assign_segments, lap_gps_trace
from .metrics import add_braking_throttle_estimates, braking_zones, lap_metric_trace, segment_aggregates

APPROACH_WINDOW_M = 30.0  # how far before a corner to look for its braking zone


def time_loss_per_segment(lap_segment_times: pd.DataFrame, best_segment_times: pd.DataFrame) -> pd.DataFrame:
    """Time lost in each segment vs. the theoretical best, for one analyzed lap."""
    merged = lap_segment_times.merge(
        best_segment_times[["segment_label", "segment_kind", "time_s", "lap_number"]].rename(
            columns={"lap_number": "best_source_lap"}
        ),
        on=["segment_label", "segment_kind"],
        suffixes=("_lap", "_best"),
    )
    merged["time_loss_s"] = merged["time_s_lap"] - merged["time_s_best"]
    return merged.sort_values("time_loss_s", ascending=False).reset_index(drop=True)


def _segment_window_aggregates(session, lap_number: int, segment_row: pd.Series) -> dict:
    """Diagnostic stats for one lap within one segment's distance range."""
    trace = lap_metric_trace(session, lap_number)
    if trace.empty:
        return {}
    trace = add_braking_throttle_estimates(trace)
    in_seg = trace[(trace["lap_distance_m"] >= segment_row["start_m"]) & (trace["lap_distance_m"] < segment_row["end_m"])]
    if in_seg.empty:
        return {}

    approach = trace[
        (trace["lap_distance_m"] >= segment_row["start_m"] - APPROACH_WINDOW_M)
        & (trace["lap_distance_m"] < segment_row["end_m"])
    ]
    zones = braking_zones(approach)
    brake_point_m = zones["brake_point_m"].min() if not zones.empty else np.nan

    exit_rows = in_seg.sort_values("lap_distance_m")
    exit_rpm = exit_rows["RPM"].dropna().iloc[-1] if exit_rows["RPM"].notna().any() else np.nan

    return {
        "min_speed_kmh": in_seg["GPS Speed"].min(),
        "exit_rpm": exit_rpm,
        "brake_point_m": brake_point_m,
        "brake_point_before_corner_m": (segment_row["start_m"] - brake_point_m) if pd.notna(brake_point_m) else np.nan,
        "lateral_g_std": in_seg["GPS Lateral Acceleration"].std(),
    }


def diagnose_segment(session, segment_row: pd.Series, analyzed_lap: int, best_source_lap: int) -> dict:
    """Compare the analyzed lap against the lap that produced this segment's
    best time, and infer the most likely technical cause of the gap.

    Every field here is derived from GPS/RPM/G traces, not a direct brake or
    throttle measurement -- treat as a coaching hypothesis, not a fact.
    """
    analyzed = _segment_window_aggregates(session, analyzed_lap, segment_row)
    best = _segment_window_aggregates(session, best_source_lap, segment_row)
    if not analyzed or not best:
        return {"cause": "insufficient_data", "note": "Not enough GPS/RPM data in this segment to diagnose a cause."}

    diagnoses = []

    if pd.notna(analyzed.get("min_speed_kmh")) and pd.notna(best.get("min_speed_kmh")):
        speed_gap = best["min_speed_kmh"] - analyzed["min_speed_kmh"]
        if segment_row["kind"] == "corner" and speed_gap > 2:
            diagnoses.append(("low_min_corner_speed", speed_gap, f"minimum speed is {speed_gap:.1f} km/h down on your best-lap pace through here"))

    if pd.notna(analyzed.get("brake_point_before_corner_m")) and pd.notna(best.get("brake_point_before_corner_m")):
        brake_gap = analyzed["brake_point_before_corner_m"] - best["brake_point_before_corner_m"]
        if abs(brake_gap) > 3:
            direction = "earlier" if brake_gap > 0 else "later"
            diagnoses.append(("brake_point", abs(brake_gap), f"you're braking {abs(brake_gap):.0f}m {direction} than your best-lap reference into this segment"))

    if pd.notna(analyzed.get("exit_rpm")) and pd.notna(best.get("exit_rpm")):
        rpm_gap = best["exit_rpm"] - analyzed["exit_rpm"]
        if abs(rpm_gap) > 300:
            diagnoses.append(("exit_rpm_mismatch", abs(rpm_gap), f"exit RPM is {abs(rpm_gap):.0f} rpm {'down' if rpm_gap > 0 else 'up'} on your best lap, suggesting exit speed or a gearing mismatch"))

    if pd.notna(analyzed.get("lateral_g_std")) and pd.notna(best.get("lateral_g_std")):
        g_gap = analyzed["lateral_g_std"] - best["lateral_g_std"]
        if g_gap > 0.05:
            diagnoses.append(("inconsistent_line", g_gap, "lateral G is noisier here than your best lap, suggesting an inconsistent line"))

    if not diagnoses:
        return {"cause": "unclear", "note": "Time is being lost here but no single dominant cause stands out from the available channels -- worth reviewing the onboard/GPS trace directly."}

    diagnoses.sort(key=lambda d: d[1], reverse=True)
    cause, _, note = diagnoses[0]
    return {"cause": cause, "note": note, "all_factors": diagnoses}


def top_focus_areas(
    session,
    analyzed_lap: int,
    segments: pd.DataFrame,
    lap_segment_times: pd.DataFrame,
    best_segment_times: pd.DataFrame,
    n: int = 3,
) -> list[dict]:
    """The headline output: the top N segments with the most gainable time,
    each with a diagnosed cause and a plain-language coaching note."""
    losses = time_loss_per_segment(lap_segment_times, best_segment_times)
    losses = losses[losses["time_loss_s"] > 0].head(n)

    results = []
    for _, row in losses.iterrows():
        seg_row = segments.loc[segments["label"] == row["segment_label"]].iloc[0]
        diagnosis = diagnose_segment(session, seg_row, analyzed_lap, int(row["best_source_lap"]))
        coaching_note = _coaching_sentence(row["segment_label"], row["time_loss_s"], diagnosis)
        results.append(
            {
                "segment_label": row["segment_label"],
                "segment_kind": row["segment_kind"],
                "time_loss_s": row["time_loss_s"],
                "cause": diagnosis.get("cause"),
                "technical_note": diagnosis.get("note"),
                "coaching_note": coaching_note,
            }
        )
    return results


def _coaching_sentence(segment_label: str, time_loss_s: float, diagnosis: dict) -> str:
    note = diagnosis.get("note", "")
    return f"{segment_label}: about {time_loss_s:.2f}s available. {note.capitalize() if note else 'Review this section against your best lap.'}"


# Setup suggestions don't carry a directly comparable "seconds available"
# figure the way a corner time-loss does, but a medium/high-confidence
# setup mismatch is a session-wide constant (affecting every lap, every
# corner) rather than a one-off -- worth a guaranteed slot ahead of
# marginal corner items rather than being left to a separate tab nobody
# necessarily opens.
SETUP_SUGGESTION_PRIORITY = {"high": 1_000_000, "medium": 500_000, "low": 0}


def blended_top_recommendations(
    session,
    analyzed_lap: int,
    segments: pd.DataFrame,
    lap_segment_times: pd.DataFrame,
    best_segment_times: pd.DataFrame,
    setup_suggestions: list[dict] | None = None,
    n: int = 3,
) -> list[dict]:
    """Top N recommendations, blending corner-based time-loss areas with
    medium/high-confidence kart setup hypotheses into one ranked list.

    Every returned dict has a `kind` field ("corner" or "setup") so the UI
    can render them differently -- a setup item has no `time_loss_s` figure,
    only a `confidence` label.
    """
    corner_areas = top_focus_areas(session, analyzed_lap, segments, lap_segment_times, best_segment_times, n=n)
    for area in corner_areas:
        area["kind"] = "corner"
        area["priority"] = area["time_loss_s"]

    setup_areas = []
    for s in setup_suggestions or []:
        if s.get("confidence") not in ("medium", "high") or not s.get("suggested_action"):
            continue
        setup_areas.append(
            {
                "kind": "setup",
                "segment_label": s["area"].replace("_", " ").title(),
                "time_loss_s": None,
                "cause": s["area"],
                "confidence": s["confidence"],
                "technical_note": s.get("hypothesis"),
                "coaching_note": s["suggested_action"],
                "priority": SETUP_SUGGESTION_PRIORITY[s["confidence"]],
            }
        )

    combined = sorted(setup_areas + corner_areas, key=lambda a: a["priority"], reverse=True)
    return combined[:n]


def recurring_weaknesses(per_session_focus: dict[str, list[dict]], min_sessions: int = 2) -> pd.DataFrame:
    """Given {session_label: [top focus area dicts]} across multiple loaded
    sessions, find segments that show up as a top focus area repeatedly --
    i.e. a recurring habit rather than a one-off mistake."""
    rows = []
    for session_label, areas in per_session_focus.items():
        for area in areas:
            rows.append({"session": session_label, **area})
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    counts = df.groupby("segment_label").agg(
        n_sessions=("session", "nunique"),
        avg_time_loss_s=("time_loss_s", "mean"),
        total_time_loss_s=("time_loss_s", "sum"),
    ).reset_index()
    recurring = counts[counts["n_sessions"] >= min_sessions].sort_values("total_time_loss_s", ascending=False)
    return recurring.reset_index(drop=True)
