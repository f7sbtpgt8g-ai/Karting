"""Tests for core/tsv_writer.py -- output format correctness and a real
round-trip through telemetry.parser."""

import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from telemetry.parser import COLUMNS as PARSER_COLUMNS  # noqa: E402
from telemetry.parser import load_sessions  # noqa: E402
from unigo_sync.core.tsv_writer import COLUMNS, to_tsv_text, write_tsv  # noqa: E402


def test_columns_match_telemetry_parser():
    """A drift here would silently break telemetry.parser.load_raw's
    "missing expected columns" check -- see tsv_writer.py's comment on
    why this is a literal copy rather than an import."""
    assert COLUMNS == PARSER_COLUMNS


def test_header_row_is_quoted_and_tab_separated():
    df = pd.DataFrame([{"Start Date": "2026-01-01", "Start Time": "12:00:00", "Session Time": 0, "Lap Number": 0, "Lap Time": 0}])
    text = to_tsv_text(df)
    header_line = text.split("\r\n")[0]
    assert header_line.startswith('"Start Date"\t"Start Time"\t"Lap Number"\t"Session Time"\t"Lap Time"')
    assert header_line.count("\t") == len(COLUMNS) - 1


def test_missing_columns_are_blank_not_nan_text():
    df = pd.DataFrame([{"Start Date": "2026-01-01", "Start Time": "12:00:00", "Session Time": 0, "Lap Number": 0, "Lap Time": 0, "RPM": 5000.0}])
    text = to_tsv_text(df)
    data_line = text.split("\r\n")[1]
    cells = data_line.split("\t")
    assert "nan" not in data_line.lower()
    # Vertical Acceleration (never decoded) should be a blank cell, not "nan"/"None".
    va_idx = COLUMNS.index("Vertical Acceleration")
    assert cells[va_idx] == ""


def test_integer_columns_have_no_decimal_point():
    df = pd.DataFrame([{"Start Date": "2026-01-01", "Start Time": "12:00:00", "Session Time": 123_000_000, "Lap Number": 2, "Lap Time": 1_000_000}])
    text = to_tsv_text(df)
    data_line = text.split("\r\n")[1]
    cells = dict(zip(COLUMNS, data_line.split("\t")))
    assert cells["Session Time"] == "123000000"
    assert cells["Lap Number"] == "2"
    assert cells["Lap Time"] == "1000000"


def test_round_trip_through_telemetry_parser():
    df = pd.DataFrame(
        [
            {"Start Date": "2026-01-01", "Start Time": "12:00:00", "Session Time": 0, "Lap Number": 0, "Lap Time": 0, "Latitude": 55.05, "Longitude": 11.91},
            {"Start Date": "2026-01-01", "Start Time": "12:00:00", "Session Time": 100_000_000, "Lap Number": 0, "Lap Time": 100_000_000, "RPM": 5000.0},
        ]
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "out.tsv")
        write_tsv(df, path)
        sessions = load_sessions(path)
        assert len(sessions) == 1
        s = sessions[0]
        assert s.start_date == "2026-01-01"
        rpm = s.extract_channel("RPM")
        assert len(rpm) == 1
        assert rpm["RPM"].iloc[0] == pytest.approx(5000.0)
        fixes = s.gps_fixes()
        assert len(fixes) == 1
        assert fixes["Latitude"].iloc[0] == pytest.approx(55.05)
