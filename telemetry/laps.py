"""Lap segmentation and outlier/anomaly detection.

`Lap Number` + `Lap Time` from the Unipro export are treated as the
authoritative lap clock (see parser.py docstring) -- lap boundaries are read
directly, not re-derived from GPS start/finish crossings.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .parser import Session

# A contiguous run of fewer than this many rows is treated as a stray
# mis-tagged row (e.g. a buffered channel write flushed slightly late,
# still carrying the previous lap's number) rather than a genuine momentary
# lap -- any real lap, even a very short one, spans far more rows than this
# given the channel rates in a Unipro export.
MIN_LAP_RUN_ROWS = 5


def lap_table(session: Session) -> pd.DataFrame:
    """One row per lap: lap number, lap time (s), start/end session time.

    The lap time for a given `Lap Number` is the last (max) `Lap Time` value
    recorded before the number increments -- `Lap Time` counts up from 0
    within a lap, so `max()` per group is that value.

    Segments by *contiguous* runs of the same `Lap Number`, not by grouping
    all rows that share the value anywhere in the session. Grouping by raw
    value would silently merge two physically distinct laps into one row
    whenever a lap number repeats non-adjacently (e.g. a missed
    session-restart split, or a device-side quirk) -- exactly the "combined
    outlier lap that doesn't match the dash" symptom this replaces.

    Runs shorter than `MIN_LAP_RUN_ROWS` are noise (a lone stray/mistimed
    reading), not a real momentary lap. A stray row *inside* a real lap
    splits it into two raw runs either side of itself, so absorbing the
    stray alone isn't enough -- the two real runs it split apart are
    re-merged afterwards since they end up adjacent with the same resolved
    label.
    """
    df = session.df.sort_values("session_time_s", kind="stable").reset_index(drop=True)
    lap_numbers = df["Lap Number"].to_numpy()
    n = len(lap_numbers)

    if n == 0:
        return pd.DataFrame(columns=["lap_number", "lap_time_s", "session_start_s", "session_end_s", "n_rows"])

    # Pass 1: raw contiguous runs by value.
    run_id = np.zeros(n, dtype=int)
    for i in range(1, n):
        run_id[i] = run_id[i - 1] + (1 if lap_numbers[i] != lap_numbers[i - 1] else 0)
    n_runs = run_id[-1] + 1
    run_sizes = np.bincount(run_id, minlength=n_runs)
    run_label = np.zeros(n_runs, dtype=lap_numbers.dtype)
    run_label[run_id] = lap_numbers  # uniform within a raw run, last write wins harmlessly

    # Pass 2: resolve each noise run's label from its nearest substantial
    # neighbour -- the preceding real run if there is one, otherwise (a
    # leading noise run) the next real run.
    clean_label = run_label.copy()
    resolved = run_sizes >= MIN_LAP_RUN_ROWS  # real runs are already their own resolution
    last_real = None
    for rid in range(n_runs):
        if run_sizes[rid] < MIN_LAP_RUN_ROWS:
            if last_real is not None:
                clean_label[rid] = clean_label[last_real]
                resolved[rid] = True
        else:
            last_real = rid
    for rid in range(n_runs):
        if not resolved[rid]:
            # Leading noise run with no preceding real run to absorb into --
            # fall back to the nearest following real run, if any.
            following = next((r for r in range(rid + 1, n_runs) if run_sizes[r] >= MIN_LAP_RUN_ROWS), None)
            if following is not None:
                clean_label[rid] = clean_label[following]

    # Pass 3: adjacent original runs that now share the same resolved label
    # (because the noise between them got absorbed away) are one real lap.
    final_id = np.zeros(n_runs, dtype=int)
    for rid in range(1, n_runs):
        final_id[rid] = final_id[rid - 1] + (1 if clean_label[rid] != clean_label[rid - 1] else 0)

    df = df.assign(_final_id=final_id[run_id])
    rows = []
    for _, g in df.groupby("_final_id"):
        rows.append(
            {
                "lap_number": int(g["Lap Number"].mode().iloc[0]),
                "lap_time_s": g["lap_time_s"].max(),
                "session_start_s": g["session_time_s"].min(),
                "session_end_s": g["session_time_s"].max(),
                "n_rows": len(g),
            }
        )
    out = pd.DataFrame(rows).reset_index(drop=True)
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
