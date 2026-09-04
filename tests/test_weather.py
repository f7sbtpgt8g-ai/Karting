"""Tests for telemetry/weather.py. No live network calls -- `_http_get_json`
is monkeypatched with canned Open-Meteo-shaped responses, since this repo's
test suite must stay reliable offline and the actual Open-Meteo contract
(response shape, `timezone=auto` local-time alignment) is documented, not
something to depend on a real API call succeeding in CI."""

import numpy as np
import pandas as pd
import pytest

from telemetry.parser import Session
from telemetry.weather import (
    _classify_condition,
    fetch_track_conditions,
    session_gps_altitude_m,
    session_location_and_time,
)


def _session_with_gps(start_date="2024-06-15", start_time="14:00:00", lats=None, lons=None, altitudes=None):
    lats = lats or [55.670, 55.671, 55.672]
    lons = lons or [12.560, 12.561, 12.562]
    altitudes = altitudes if altitudes is not None else [10.0, 12.0, 11.0]
    n = len(lats)
    df = pd.DataFrame(
        {
            "session_time_s": np.arange(n, dtype=float), "lap_time_s": np.arange(n, dtype=float),
            "Lap Number": [1] * n, "Heading": [0.0] * n, "Horizontal DOP": [0.9] * n,
            "Latitude": lats, "Vertical DOP": [1.0] * n, "Longitude": lons,
            "Positional DOP": [1.0] * n, "Altitude": altitudes,
        }
    )
    return Session(session_id=0, source_file="test", df=df, start_date=start_date, start_time=start_time)


def _hourly_payload(elevation=42.0, temp_at_14=18.5, precip=None):
    hours = [f"2024-06-15T{h:02d}:00" for h in range(24)]
    precip = precip if precip is not None else [0.0] * 24
    return {
        "elevation": elevation,
        "hourly": {
            "time": hours,
            "temperature_2m": [10.0 + h * 0.1 for h in range(24)][:14] + [temp_at_14] + [10.0] * 9,
            "relative_humidity_2m": [60.0] * 24,
            "surface_pressure": [1013.0] * 24,
            "precipitation": precip,
        },
    }


def test_session_location_and_time_uses_median_gps_fix():
    session = _session_with_gps()
    result = session_location_and_time(session)
    assert result is not None
    lat, lon, dt = result
    assert lat == pytest.approx(55.671, abs=1e-6)
    assert lon == pytest.approx(12.561, abs=1e-6)
    assert dt.year == 2024 and dt.month == 6 and dt.day == 15 and dt.hour == 14


def test_session_location_and_time_none_without_gps_fixes():
    session = _session_with_gps(lats=[np.nan, np.nan], lons=[np.nan, np.nan], altitudes=[np.nan, np.nan])
    assert session_location_and_time(session) is None


def test_session_gps_altitude_m_is_median():
    session = _session_with_gps(altitudes=[10.0, 20.0, 30.0])
    assert session_gps_altitude_m(session) == 20.0


def test_classify_condition_dry():
    assert _classify_condition([0.0] * 10, 5) == "Dry"


def test_classify_condition_wet_when_raining_at_target_hour():
    precip = [0.0] * 10
    precip[5] = 1.5
    assert _classify_condition(precip, 5) == "Wet"


def test_classify_condition_mixed_when_recently_wet_but_dry_now():
    precip = [0.0] * 10
    precip[3] = 2.0  # rained a couple hours before the session started
    assert _classify_condition(precip, 5) == "Mixed"


def test_fetch_track_conditions_uses_archive_response(monkeypatch):
    session = _session_with_gps()
    monkeypatch.setattr("telemetry.weather._http_get_json", lambda url: _hourly_payload())

    result = fetch_track_conditions(session)
    assert result is not None
    assert result.condition == "Dry"
    assert result.temperature_c == 18.5
    assert result.humidity_pct == 60.0
    assert result.pressure_hpa == 1013.0
    assert result.altitude_m == 11.0  # from the session's own GPS trace, not the API's elevation
    assert "archive" in result.source
    assert "GPS altitude" in result.source


def test_fetch_track_conditions_falls_back_to_forecast_when_archive_fails(monkeypatch):
    session = _session_with_gps()

    def fake_get(url):
        if "archive-api" in url:
            return None
        return _hourly_payload(temp_at_14=22.0)

    monkeypatch.setattr("telemetry.weather._http_get_json", fake_get)
    result = fetch_track_conditions(session)
    assert result is not None
    assert result.temperature_c == 22.0
    assert "forecast" in result.source


def test_fetch_track_conditions_none_when_both_endpoints_fail(monkeypatch):
    session = _session_with_gps()
    monkeypatch.setattr("telemetry.weather._http_get_json", lambda url: None)
    assert fetch_track_conditions(session) is None


def test_fetch_track_conditions_falls_back_to_api_elevation_without_gps_altitude(monkeypatch):
    session = _session_with_gps(altitudes=[np.nan, np.nan, np.nan])
    monkeypatch.setattr("telemetry.weather._http_get_json", lambda url: _hourly_payload(elevation=99.0))
    result = fetch_track_conditions(session)
    assert result.altitude_m == 99.0
    assert "GPS altitude" not in result.source


def test_fetch_track_conditions_none_without_location():
    session = _session_with_gps(lats=[np.nan, np.nan], lons=[np.nan, np.nan], altitudes=[np.nan, np.nan])
    session.start_date = None
    session.start_time = None
    assert fetch_track_conditions(session) is None
