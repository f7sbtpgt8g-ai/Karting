"""Parser for Unipro laptimer TSV exports.

Confirmed format (from a real sample export, see project README):
- Quoted, tab-separated header row of 28 columns.
- Decimal separator is a period.
- Every row is a sparse, asynchronous sensor event: only the channel(s) that
  fired populate that row, everything else is blank. There is no fixed
  sample rate, so rows must NOT be treated as complete samples.
- `Session Time` and `Lap Time` are in nanoseconds.
- A single file can contain multiple sessions/runs: `Lap Number` and
  `Session Time` both reset to 0 when the logger was stopped and restarted.
- `Lap Time` is the authoritative per-lap clock; lap boundaries are read
  directly from `Lap Number` increments, not re-derived from GPS.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# Column order as confirmed in the real sample export header.
COLUMNS = [
    "Start Date",
    "Start Time",
    "Lap Number",
    "Session Time",
    "Lap Time",
    "Heading",
    "Steering Angle",
    "Vertical Acceleration",
    "RPM",
    "Steering Rate",
    "GPS Speed",
    "Slip",
    "Horizontal DOP",
    "Inverse Corner Radius",
    "Latitude",
    "GPS Distance",
    "GPS Lateral Acceleration",
    "GPS Longitudinal Acceleration",
    "Internal Temperature",
    "Vertical DOP",
    "Longitude",
    "Battery Voltage",
    "Positional DOP",
    "Time",
    "Temperature 1",
    "GPS Total Acceleration",
    "RPM unfiltered",
    "Altitude",
]

# Columns present on essentially every row.
ALWAYS_ON_COLUMNS = ["Start Date", "Start Time", "Lap Number", "Session Time", "Lap Time"]

# Columns confirmed to reliably fire together on the same row as the
# position fix (Latitude non-null implies all of these are too), in both
# the original sample export and a real second export checked later.
GPS_FIX_COLUMNS = [
    "Heading",
    "Horizontal DOP",
    "Latitude",
    "Vertical DOP",
    "Longitude",
    "Positional DOP",
    "Altitude",
]

# GPS Speed and the G-force channels are GPS-derived too, but on at least
# one real export they fire on their own independent cadence -- confirmed
# to *never* share a row with Latitude there, despite the original sample
# export (a different device/firmware/config) having them co-fire as part
# of one atomic GPS fix. Treated as a separately-timed channel group and
# aligned in independently (see corners.py::lap_gps_trace) rather than
# assumed to share rows with the position fix.
GPS_MOTION_COLUMNS = [
    "Vertical Acceleration",
    "GPS Speed",
    "GPS Lateral Acceleration",
    "GPS Longitudinal Acceleration",
]

NUMERIC_COLUMNS = [c for c in COLUMNS if c not in ("Start Date", "Start Time")]

NANOSECONDS_PER_SECOND = 1_000_000_000


def load_raw(path: str) -> pd.DataFrame:
    """Read a Unipro TSV export into a raw DataFrame.

    Converts `Session Time` / `Lap Time` from nanoseconds to seconds
    (as `session_time_s` / `lap_time_s`) but otherwise leaves the sparse,
    event-based row structure untouched -- callers should use
    `extract_channel` / `align_channels` rather than reading rows directly.
    """
    df = pd.read_csv(
        path,
        sep="\t",
        quotechar='"',
        dtype=str,
        keep_default_na=False,
        na_values=[""],
    )
    df.columns = [c.strip() for c in df.columns]

    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"TSV is missing expected columns: {missing}")

    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["session_time_s"] = df["Session Time"] / NANOSECONDS_PER_SECOND
    df["lap_time_s"] = df["Lap Time"] / NANOSECONDS_PER_SECOND

    df = df.reset_index(drop=True)
    df["row_id"] = df.index
    return df


@dataclass
class Session:
    """One contiguous logger run extracted from a (possibly multi-session) file."""

    session_id: int
    source_file: str
    df: pd.DataFrame
    start_date: str | None = None
    start_time: str | None = None
    channel_cache: dict = field(default_factory=dict, repr=False)

    def extract_channel(self, column: str) -> pd.DataFrame:
        """Return the (session_time_s, value) pairs where `column` actually fired.

        Cached per-session since several analyses re-use the same channel.
        """
        if column in self.channel_cache:
            return self.channel_cache[column]
        sub = self.df.loc[self.df[column].notna(), ["session_time_s", "lap_time_s", "Lap Number", column]]
        sub = sub.sort_values("session_time_s").reset_index(drop=True)
        self.channel_cache[column] = sub
        return sub

    def gps_fixes(self) -> pd.DataFrame:
        """Return the rows that carry a position fix (Latitude and the rest
        of GPS_FIX_COLUMNS populated). Does NOT include GPS Speed or the
        G-force channels -- see GPS_MOTION_COLUMNS -- since those aren't
        guaranteed to share a row with the position fix on every export."""
        key = "__gps_fixes__"
        if key in self.channel_cache:
            return self.channel_cache[key]
        mask = self.df["Latitude"].notna()
        cols = ["session_time_s", "lap_time_s", "Lap Number"] + GPS_FIX_COLUMNS
        sub = self.df.loc[mask, cols].sort_values("session_time_s").reset_index(drop=True)
        self.channel_cache[key] = sub
        return sub

    def align_channels(
        self,
        columns: list[str],
        base: str | None = None,
        tolerance_s: float = 0.25,
    ) -> pd.DataFrame:
        """Align several sparse channels onto a common time base.

        Uses `merge_asof` (nearest prior/next reading within `tolerance_s`)
        rather than a blind forward-fill of the whole file, so fast channels
        aren't distorted by slow ones. `base` picks which channel's own
        timestamps become the output rows; defaults to the densest channel.
        """
        if not columns:
            raise ValueError("columns must be non-empty")

        series = {c: self.extract_channel(c)[["session_time_s", c]] for c in columns}
        if base is None:
            base = max(series, key=lambda c: len(series[c]))

        out = series[base].rename(columns={base: base}).copy()
        out = out.sort_values("session_time_s")
        for c, s in series.items():
            if c == base:
                continue
            s = s.sort_values("session_time_s")
            out = pd.merge_asof(
                out,
                s,
                on="session_time_s",
                direction="nearest",
                tolerance=tolerance_s,
            )
        return out.reset_index(drop=True)



# A genuine logger restart drops Session Time back to ~0 from wherever the
# previous run had reached -- a jump of seconds at minimum. Rows within a
# single continuous session can still be a few milliseconds out of exact
# chronological order (different channels are buffered/flushed
# independently), so the threshold has to sit well above that jitter or
# ordinary rows get misread as new sessions, fragmenting one real session
# into many and corrupting lap segmentation downstream.
SESSION_RESET_MIN_DROP_S = 1.0
SESSION_RESET_MAX_POST_VALUE_S = 5.0


def split_sessions(df: pd.DataFrame, source_file: str = "") -> list[Session]:
    """Split a raw dataframe into distinct logger sessions.

    Unipro resets both `Lap Number` and `Session Time` to 0 when the logger
    is stopped and restarted. A session boundary is a `session_time_s` drop
    of at least `SESSION_RESET_MIN_DROP_S`, landing back near zero
    (below `SESSION_RESET_MAX_POST_VALUE_S`) -- both conditions together
    distinguish an actual restart from ordinary row-ordering jitter.
    """
    if df.empty:
        return []

    time_diff = df["session_time_s"].diff()
    reset_mask = (time_diff < -SESSION_RESET_MIN_DROP_S) & (df["session_time_s"] < SESSION_RESET_MAX_POST_VALUE_S)
    reset_mask.iloc[0] = False
    boundaries = [0] + df.index[reset_mask].tolist() + [len(df)]

    sessions = []
    for i in range(len(boundaries) - 1):
        start, end = boundaries[i], boundaries[i + 1]
        chunk = df.iloc[start:end].reset_index(drop=True)
        if chunk.empty:
            continue
        sessions.append(
            Session(
                session_id=i,
                source_file=source_file,
                df=chunk,
                start_date=chunk["Start Date"].dropna().iloc[0] if chunk["Start Date"].notna().any() else None,
                start_time=chunk["Start Time"].dropna().iloc[0] if chunk["Start Time"].notna().any() else None,
            )
        )
    return sessions


def load_sessions(path: str) -> list[Session]:
    """Convenience wrapper: load a TSV file and split it into sessions."""
    raw = load_raw(path)
    return split_sessions(raw, source_file=path)
