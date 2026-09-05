"""Tests for core/uni_format.py against synthetic .uni fixtures -- see
uni_fixtures.py's module docstring for what these can and can't verify
without a real device capture."""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for `unigo_sync` + `telemetry` imports
import unigo_sync.tests.uni_fixtures as fx  # noqa: E402
from unigo_sync.core import uni_format  # noqa: E402


def test_is_uni_file():
    assert uni_format.is_uni_file(fx.build_uni_file(records=[]))
    assert not uni_format.is_uni_file(b"not a uni file at all")


def test_bad_magic_raises():
    with pytest.raises(uni_format.UniFormatError):
        uni_format.decode_uni_bytes(b"XXXX" + b"\x00" * 20)


def test_no_gps_fixes_raises():
    # two housekeeping records (so the first one gets a proper trailing
    # boundary and actually decodes) -- still no GPS fixes anywhere.
    data = fx.build_uni_file(records=[fx.housekeeping_record(3.76, 19.0), fx.housekeeping_record(3.77, 19.1)])
    with pytest.raises(uni_format.UniFormatError, match="GPS fixes"):
        uni_format.decode_uni_bytes(data)


def test_gps_fix_decodes_lat_lon_alt_speed_exactly():
    records = [
        fx.gps_fix_record(55.05, 11.91, 0.5, 42.0, counter=0),
        fx.gps_fix_record(55.0501, 11.9101, 0.6, 45.0, counter=1),
    ]
    data = fx.build_uni_file(records=records)
    df = uni_format.decode_uni_bytes(data)

    fixes = df[df["Latitude"].notna()].sort_values("Session Time")
    assert len(fixes) == 2
    assert fixes["Latitude"].iloc[0] == pytest.approx(55.05, abs=1e-6)
    assert fixes["Longitude"].iloc[0] == pytest.approx(11.91, abs=1e-6)
    assert fixes["Altitude"].iloc[0] == pytest.approx(0.5, abs=1e-3)
    assert fixes["GPS Speed"].iloc[0] == pytest.approx(42.0, abs=1e-2)
    assert fixes["Latitude"].iloc[1] == pytest.approx(55.0501, abs=1e-6)
    # DOP fields have a known unresolved byte-overlap (see uni_fixtures.py) --
    # only check they decoded to *something*, not an exact value.
    assert fixes["Positional DOP"].notna().all()


def test_gps_fix_reconstructs_elapsed_time_at_10hz():
    records = [fx.gps_fix_record(55.05, 11.91, 0.5, 0.0, counter=i) for i in range(5)]
    data = fx.build_uni_file(records=records)
    df = uni_format.decode_uni_bytes(data)
    fixes = df[df["Latitude"].notna()].sort_values("Session Time")
    session_times_ns = fixes["Session Time"].tolist()
    assert session_times_ns == [0, 100_000_000, 200_000_000, 300_000_000, 400_000_000]


def test_housekeeping_decodes_without_crashing():
    records = [fx.gps_fix_record(55.05, 11.91, 0.5, 0.0), fx.housekeeping_record(3.76, 19.0)]
    data = fx.build_uni_file(records=records)
    df = uni_format.decode_uni_bytes(data)
    hk = df[df["Battery Voltage"].notna()]
    assert len(hk) == 1
    # Not bounded to a "plausible" range on purpose: the documented byte
    # overlap between Battery Voltage and Internal Temperature (see
    # uni_fixtures.py) means writing both in one synthetic record has one
    # clobber the other's shared byte, so the decoded value here is not
    # meaningful -- this test only checks decoding runs without crashing
    # and produces a real (non-NaN) number, matching what a genuinely
    # ambiguous real-world byte position should do.
    assert not pd.isna(hk["Battery Voltage"].iloc[0])


@pytest.mark.parametrize("reclen", [14, 18, 24, 28])
def test_rpm_decodes_exactly_across_all_four_record_types(reclen):
    records = [fx.gps_fix_record(55.05, 11.91, 0.5, 0.0), fx.rpm_steering_record(reclen, rpm=5432.0)]
    data = fx.build_uni_file(records=records)
    df = uni_format.decode_uni_bytes(data)
    rpm_rows = df[df["RPM"].notna()]
    assert len(rpm_rows) == 1
    assert rpm_rows["RPM"].iloc[0] == pytest.approx(5432.0, abs=1e-6)
    assert rpm_rows["RPM unfiltered"].iloc[0] == pytest.approx(5432.0, abs=1e-6)


@pytest.mark.parametrize("reclen", [24, 28])
def test_steering_decodes_within_calibration_precision(reclen):
    records = [fx.gps_fix_record(55.05, 11.91, 0.5, 0.0), fx.rpm_steering_record(reclen, rpm=1000.0, steering_deg=-30.0)]
    data = fx.build_uni_file(records=records)
    df = uni_format.decode_uni_bytes(data)
    steer_rows = df[df["Steering Angle"].notna()]
    assert len(steer_rows) == 1
    # The calibration itself is only accurate to ~10 degrees against a real
    # device (findings.md) -- this test checks the *arithmetic* round-trips
    # to sub-degree precision (i.e. no encode/decode bug), not that -30.0
    # would match a real sensor.
    assert steer_rows["Steering Angle"].iloc[0] == pytest.approx(-30.0, abs=0.1)


def test_records_of_other_types_do_not_crash_decode():
    """Any record type not in the known set (45/32/14/18/24/28) should be
    silently skipped, not crash the decoder -- real files have several
    such types (noise/edge records, see findings.md)."""
    unknown_body = b"\x01\x02\x03"
    records = [fx.gps_fix_record(55.05, 11.91, 0.5, 0.0), fx._record(unknown_body)]
    data = fx.build_uni_file(records=records)
    df = uni_format.decode_uni_bytes(data)
    assert len(df) == 1


def test_lap_detection_via_beacon_crossing():
    """Build a tiny synthetic loop that passes the same beacon point
    twice, heading the same direction both times, with enough real time
    between passes to clear the detector's minimum-lap-time filter, and
    check the second pass increments Lap Number."""
    beacon = (55.0500, 11.9100)
    # South-to-north approach that crosses the beacon (fwd goes
    # negative -> positive) around the middle point.
    approach = [(55.0500 + d, 11.9100) for d in (-0.0002, -0.0001, 0.0, 0.0001, 0.0002)]
    # Keep driving north, away from the gate, for long enough that the
    # *next* crossing is >15s later at the synthetic 100ms/tick GPS rate
    # (160 ticks = 16s, comfortably past the detector's min_lap_time_s=15).
    away = [(55.0502 + i * 0.0001, 11.9100) for i in range(1, 161)]
    # Loop back around for a second south-to-north pass through the gate.
    second_approach = list(approach)

    points = approach + away + second_approach
    records = [fx.gps_fix_record(lat, lon, 0.5, 10.0, counter=i) for i, (lat, lon) in enumerate(points)]
    data = fx.build_uni_file(beacons=[beacon], records=records)
    df = uni_format.decode_uni_bytes(data)
    fixes = df[df["Latitude"].notna()].sort_values("Session Time")
    lap_numbers = fixes["Lap Number"].tolist()

    assert lap_numbers[0] == 0
    assert lap_numbers[-1] >= 1, f"expected at least one lap increment, got {lap_numbers}"
