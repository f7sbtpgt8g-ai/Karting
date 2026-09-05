"""Write a decoded .uni DataFrame (see `uni_format.decode_uni_bytes`) out as
a TSV file in the same shape `telemetry.parser.load_raw` expects: quoted
header row, tab-separated, sparse rows (blank cells, not "nan"), decimal
point. Matches a real Unipro Analyser export closely enough to be read
back in by the existing pipeline unmodified -- it just has fewer of the 28
columns populated (see uni_format's module docstring for exactly which).
"""

from __future__ import annotations

import pandas as pd

# Mirrors telemetry.parser.COLUMNS exactly -- kept as a literal copy
# rather than an import so this module has no dependency on the rest of
# the repo (the portable core should work standalone; see ../README.md).
# A test in ../tests asserts this stays in sync with telemetry.parser.
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

_INT_COLUMNS = {"Lap Number", "Session Time", "Lap Time"}


def _format_cell(col: str, value) -> str:
    if pd.isna(value):
        return ""
    if col in ("Start Date", "Start Time"):
        return str(value)
    if col in _INT_COLUMNS:
        return str(int(round(value)))
    return f"{float(value):.6f}"


def to_tsv_text(df: pd.DataFrame) -> str:
    """Render a decoded DataFrame as TSV text (in memory, for tests or
    piping elsewhere) using the exact column set/order real exports use."""
    for col in COLUMNS:
        if col not in df.columns:
            df = df.assign(**{col: pd.NA})
    df = df[COLUMNS]

    lines = ["\t".join(f'"{c}"' for c in COLUMNS)]
    for row in df.itertuples(index=False):
        lines.append("\t".join(_format_cell(col, val) for col, val in zip(COLUMNS, row)))
    return "\r\n".join(lines) + "\r\n"


def write_tsv(df: pd.DataFrame, path: str) -> None:
    """Write a decoded DataFrame to `path` as a TSV file matching a real
    Unipro Analyser export's shape closely enough for
    `telemetry.parser.load_sessions` to read back in unmodified."""
    text = to_tsv_text(df)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
