"""Track segmentation into corners/straights from the GPS trace.

There is no native sector column in the Unipro export, so segments are
derived generically from heading change / curvature (or from
`Inverse Corner Radius` directly, on exports where that channel happens to
be populated) rather than depending on any one channel being present.

Segments are defined once from a reference lap (the fastest clean lap by
default) as ranges of in-lap distance, then reused to slice every other lap
so corner N means the same physical corner across the whole session.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.ndimage import gaussian_filter1d

from .parser import Session

DEFAULT_CURVATURE_THRESHOLD_DEG_PER_M = 0.8
DEFAULT_MIN_SEGMENT_LENGTH_M = 8.0
DEFAULT_SMOOTHING_SIGMA = 2.0


def _unwrap_heading_deg(heading: np.ndarray) -> np.ndarray:
    return np.degrees(np.unwrap(np.radians(heading)))


def lap_gps_trace(session: Session, lap_number: int) -> pd.DataFrame:
    """GPS fixes for one lap, with GPS Distance aligned on and normalized to
    start at 0 for that lap (the export does not document whether
    `GPS Distance` is per-lap or cumulative, so this normalization makes the
    result correct either way)."""
    fixes = session.gps_fixes()
    fixes = fixes.loc[fixes["Lap Number"] == lap_number].sort_values("session_time_s").reset_index(drop=True)
    if fixes.empty:
        return fixes

    dist = session.extract_channel("GPS Distance")
    dist = dist.loc[dist["Lap Number"] == lap_number].sort_values("session_time_s")
    if dist.empty:
        fixes["lap_distance_m"] = np.nan
        return fixes

    merged = pd.merge_asof(
        fixes,
        dist[["session_time_s", "GPS Distance"]],
        on="session_time_s",
        direction="nearest",
        tolerance=2.0,
    )
    base = merged["GPS Distance"].min(skipna=True)
    merged["lap_distance_m"] = merged["GPS Distance"] - base
    return merged


def compute_curvature(trace: pd.DataFrame, smoothing_sigma: float = DEFAULT_SMOOTHING_SIGMA) -> pd.DataFrame:
    """Curvature proxy (deg of heading change per metre) along a GPS trace.

    Prefers `Inverse Corner Radius` when the export has it populated (it is
    a direct curvature measurement); otherwise falls back to smoothed
    heading rate of change vs. distance, which is what's available in the
    confirmed sample export.
    """
    trace = trace.sort_values("lap_distance_m").reset_index(drop=True)
    if trace.empty:
        trace["curvature"] = pd.Series(dtype=float)
        return trace

    if "Inverse Corner Radius" in trace.columns and trace["Inverse Corner Radius"].notna().mean() > 0.5:
        curvature = trace["Inverse Corner Radius"].astype(float).to_numpy()
        curvature = gaussian_filter1d(np.nan_to_num(curvature), sigma=smoothing_sigma)
    else:
        heading = _unwrap_heading_deg(trace["Heading"].to_numpy(dtype=float))
        distance = trace["lap_distance_m"].to_numpy(dtype=float)
        d_dist = np.gradient(distance)
        d_dist[d_dist == 0] = np.nan
        curvature = np.gradient(heading) / d_dist
        curvature = np.nan_to_num(curvature, nan=0.0, posinf=0.0, neginf=0.0)
        curvature = gaussian_filter1d(curvature, sigma=smoothing_sigma)

    trace = trace.copy()
    trace["curvature"] = curvature
    return trace


def segment_track(
    trace: pd.DataFrame,
    curvature_threshold: float = DEFAULT_CURVATURE_THRESHOLD_DEG_PER_M,
    min_segment_length_m: float = DEFAULT_MIN_SEGMENT_LENGTH_M,
) -> pd.DataFrame:
    """Turn a curvature-annotated reference-lap trace into labeled corner/
    straight segments defined by in-lap distance ranges."""
    trace = trace.dropna(subset=["lap_distance_m", "curvature"]).sort_values("lap_distance_m")
    if trace.empty:
        return pd.DataFrame(columns=["label", "kind", "start_m", "end_m"])

    is_corner = trace["curvature"].abs() > curvature_threshold
    distance = trace["lap_distance_m"].to_numpy()

    segments = []
    seg_start = distance[0]
    seg_kind = bool(is_corner.iloc[0])
    for i in range(1, len(distance)):
        kind = bool(is_corner.iloc[i])
        if kind != seg_kind:
            segments.append((seg_start, distance[i], seg_kind))
            seg_start = distance[i]
            seg_kind = kind
    segments.append((seg_start, distance[-1], seg_kind))

    # Merge segments shorter than the minimum length into their neighbour.
    merged = []
    for start, end, kind in segments:
        if merged and (end - start) < min_segment_length_m:
            prev_start, prev_end, prev_kind = merged[-1]
            merged[-1] = (prev_start, end, prev_kind)
        else:
            merged.append((start, end, kind))

    rows = []
    corner_i, straight_i = 0, 0
    for start, end, is_corner_seg in merged:
        if is_corner_seg:
            corner_i += 1
            label = f"Corner {corner_i}"
            kind = "corner"
        else:
            straight_i += 1
            label = f"Straight {straight_i}"
            kind = "straight"
        rows.append({"label": label, "kind": kind, "start_m": start, "end_m": end})
    return pd.DataFrame(rows)


def build_reference_segments(
    session: Session,
    reference_lap_number: int,
    curvature_threshold: float = DEFAULT_CURVATURE_THRESHOLD_DEG_PER_M,
    min_segment_length_m: float = DEFAULT_MIN_SEGMENT_LENGTH_M,
) -> pd.DataFrame:
    """Convenience: build the corner/straight segment table for a session
    from a single reference lap (typically the fastest clean lap)."""
    trace = lap_gps_trace(session, reference_lap_number)
    trace = compute_curvature(trace)
    return segment_track(trace, curvature_threshold, min_segment_length_m)


def assign_segments(trace: pd.DataFrame, segments: pd.DataFrame) -> pd.DataFrame:
    """Label each row of a (any lap's) GPS trace with the segment it falls
    into, by in-lap distance."""
    trace = trace.copy()
    trace["segment_label"] = None
    trace["segment_kind"] = None
    for _, seg in segments.iterrows():
        mask = (trace["lap_distance_m"] >= seg["start_m"]) & (trace["lap_distance_m"] < seg["end_m"])
        trace.loc[mask, "segment_label"] = seg["label"]
        trace.loc[mask, "segment_kind"] = seg["kind"]
    return trace
