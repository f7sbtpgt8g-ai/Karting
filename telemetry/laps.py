"""Lap segmentation and outlier/anomaly detection.

`Lap Number` + `Lap Time` from the Unipro export are treated as the
authoritative lap clock (see parser.py docstring) -- lap boundaries are read
directly, not re-derived from GPS start/finish crossings.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .parser import Session


def lap_table(session: Session) -> pd.DataFrame:
    """One row per lap: lap number, lap time (s), start/end session time.

    The lap time for a given `Lap Number` is the last (max) `Lap Time` value
    recorded before the number increments -- `Lap Time` counts up from 0
    within a lap, so `max()` per group is that value.
    """
    df = session.df
    grouped = df.groupby("Lap Number")
    rows = []
    for lap_no, g in grouped:
        g = g.sort_values("session_time_s")
        lap_time_s = g["lap_time_s"].max()
        start_t = g["session_time_s"].min()
        end_t = g["session_time_s"].max()
        rows.append(
            {
                "lap_number": int(lap_no),
                "lap_time_s": lap_time_s,
                "session_start_s": start_t,
                "session_end_s": end_t,
                "n_rows": len(g),
            }
        )
    out = pd.DataFrame(rows).sort_values("lap_number").reset_index(drop=True)
    return out


def flag_outlier_laps(
    laps: pd.DataFrame,
    mad_threshold: float = 3.5,
    exclude_first: bool = True,
    exclude_last: bool = True,
) -> pd.DataFrame:
    """Flag in/out laps and statistical outliers.

    Uses a robust modified z-score on lap time (median + MAD) rather than
    mean/stdev, since one long "stoppage" lap would otherwise blow out the
    mean and mask itself. Flagged laps are labeled, not dropped -- callers
    decide whether to exclude them from best/average stats.
    """
    laps = laps.copy()
    laps["is_outlier"] = False
    laps["outlier_reason"] = ""

    if exclude_first and len(laps) > 0:
        first_idx = laps.index[laps["lap_number"] == laps["lap_number"].min()]
        laps.loc[first_idx, "is_outlier"] = True
        laps.loc[first_idx, "outlier_reason"] = "out_lap"

    if exclude_last and len(laps) > 1:
        last_idx = laps.index[laps["lap_number"] == laps["lap_number"].max()]
        laps.loc[last_idx, "is_outlier"] = True
        laps.loc[last_idx, "outlier_reason"] = np.where(
            laps.loc[last_idx, "outlier_reason"] == "", "in_lap", laps.loc[last_idx, "outlier_reason"] + "+in_lap"
        )

    candidate = laps.loc[~laps["is_outlier"], "lap_time_s"]
    if len(candidate) >= 3:
        median = candidate.median()
        mad = (candidate - median).abs().median()
        if mad > 0:
            modified_z = 0.6745 * (laps["lap_time_s"] - median) / mad
            stat_outlier = modified_z.abs() > mad_threshold
            newly = stat_outlier & ~laps["is_outlier"]
            laps.loc[newly, "is_outlier"] = True
            laps.loc[newly, "outlier_reason"] = "statistical_outlier"

    return laps


def clean_lap_table(laps: pd.DataFrame) -> pd.DataFrame:
    """Laps with outliers excluded -- use for best/average stat calculations."""
    return laps.loc[~laps["is_outlier"]].reset_index(drop=True)


def summarize_laps(laps: pd.DataFrame) -> dict:
    """Best lap, average, std dev, and consistency stats over the clean laps."""
    clean = clean_lap_table(laps) if "is_outlier" in laps.columns else laps
    if clean.empty:
        return {}
    return {
        "best_lap_s": clean["lap_time_s"].min(),
        "best_lap_number": int(clean.loc[clean["lap_time_s"].idxmin(), "lap_number"]),
        "average_lap_s": clean["lap_time_s"].mean(),
        "median_lap_s": clean["lap_time_s"].median(),
        "std_dev_s": clean["lap_time_s"].std(),
        "n_laps": len(clean),
        "n_excluded": len(laps) - len(clean),
    }


def lap_time_with_deltas(laps: pd.DataFrame, personal_best_s: float | None = None) -> pd.DataFrame:
    """Lap table annotated with delta-to-best, delta-to-average, delta-to-PB."""
    summary = summarize_laps(laps)
    out = laps.copy()
    if not summary:
        return out
    out["delta_to_best_s"] = out["lap_time_s"] - summary["best_lap_s"]
    out["delta_to_average_s"] = out["lap_time_s"] - summary["average_lap_s"]
    if personal_best_s is not None:
        out["delta_to_personal_best_s"] = out["lap_time_s"] - personal_best_s
    return out


def detect_anomalous_laps(laps: pd.DataFrame, factor: float = 1.8) -> pd.DataFrame:
    """Flag laps that look like an incident (spin/off/stoppage) rather than a
    normal slow lap: lap time far beyond a typical multiple of the median.

    This is a coarser, single-purpose sibling of `flag_outlier_laps` intended
    for the "what looks like a mistake" UI callout -- e.g. the sample
    session's ~92s lap against a ~32s median (factor ~2.9) is exactly the
    shape this is meant to catch.
    """
    laps = laps.copy()
    median = laps["lap_time_s"].median()
    laps["likely_incident"] = laps["lap_time_s"] > median * factor
    return laps
