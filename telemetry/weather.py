"""Track-conditions lookup: derives a dry/wet/mixed summary plus
temperature/humidity/pressure/altitude for a session from its own GPS
location and start date/time, via Open-Meteo (https://open-meteo.com) --
free, keyless, and global, unlike most weather APIs that need a signup and
only cover one country or region. Used to *default* the jetting-calibration
fields on the Settings page, never to silently decide them: any value here
is editable, and any failure (no internet, no GPS fixes, a date outside
both endpoints' coverage) returns `None` rather than raising, so uploading
a session never hard-depends on network access.

Two endpoints are tried in order:
- archive-api.open-meteo.com: ERA5 reanalysis -- authoritative, but only
  available up to ~5 days behind real time.
- api.open-meteo.com (forecast endpoint, which also serves recent history
  via `start_date`/`end_date`): covers the gap for a session logged today
  or this week.

Altitude is read from the session's own GPS trace (median `Altitude` across
its fixes) rather than the weather API's grid-cell elevation -- it's data
already recorded for this exact spot, not a network lookup, and jetting
cares about the kart's own altitude, not a nearby station's.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from .parser import Session

REQUEST_TIMEOUT_S = 6.0
# Hourly precipitation (mm) above this counts as "raining" for that hour.
WET_PRECIPITATION_MM = 0.2
# How many hours before the session's start to look back for "recently wet,
# drying out" -> mixed, rather than a clean dry/wet call.
MIXED_LOOKBACK_HOURS = 3

CONDITION_OPTIONS = ["Dry", "Wet", "Mixed"]


@dataclass
class TrackConditions:
    condition: str  # "Dry" | "Wet" | "Mixed"
    temperature_c: float | None
    humidity_pct: float | None
    pressure_hpa: float | None
    altitude_m: float | None
    source: str  # e.g. "open-meteo (archive)", "open-meteo (forecast) + GPS altitude"


def session_location_and_time(session: Session) -> tuple[float, float, datetime] | None:
    """Representative (latitude, longitude, start datetime) for a session,
    from its own GPS fixes and `Start Date`/`Start Time` columns -- the "GPS
    data and date/time" a weather lookup is keyed on. `None` if the session
    has no GPS fixes or an unparseable start date/time."""
    fixes = session.gps_fixes()
    if fixes.empty or session.start_date is None or session.start_time is None:
        return None
    lat = float(fixes["Latitude"].median())
    lon = float(fixes["Longitude"].median())
    try:
        dt = datetime.strptime(f"{session.start_date} {session.start_time}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return lat, lon, dt


def session_gps_altitude_m(session: Session) -> float | None:
    """Median GPS altitude across the session's own fixes."""
    fixes = session.gps_fixes()
    if fixes.empty or "Altitude" not in fixes.columns or fixes["Altitude"].isna().all():
        return None
    return float(fixes["Altitude"].median())


def _http_get_json(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_S) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None


def _fetch_hourly(lat: float, lon: float, date_str: str, archive: bool) -> dict | None:
    base = "https://archive-api.open-meteo.com/v1/archive" if archive else "https://api.open-meteo.com/v1/forecast"
    # timezone=auto resolves to the track's own local time zone so the
    # returned hourly timestamps line up directly with the logger's
    # (local, tz-naive) Start Date/Start Time rather than needing a
    # separate UTC-offset lookup.
    url = (
        f"{base}?latitude={lat:.5f}&longitude={lon:.5f}"
        f"&start_date={date_str}&end_date={date_str}"
        "&hourly=temperature_2m,relative_humidity_2m,surface_pressure,precipitation"
        "&timezone=auto"
    )
    return _http_get_json(url)


def _classify_condition(precip: list, target_index: int) -> str:
    if not precip:
        return "Dry"
    lo = max(0, target_index - MIXED_LOOKBACK_HOURS)
    hi = min(len(precip), target_index + 1)
    window = [p for p in precip[lo:hi] if p is not None]
    at_target = precip[target_index] if target_index < len(precip) and precip[target_index] is not None else 0.0
    if at_target >= WET_PRECIPITATION_MM:
        return "Wet"
    if window and max(window) >= WET_PRECIPITATION_MM:
        return "Mixed"
    return "Dry"


def fetch_track_conditions(session: Session) -> TrackConditions | None:
    """Best-effort dry/wet/mixed + temperature/humidity/pressure/altitude
    for a session, keyed on its own GPS location and start date/time.
    Returns `None` if location/time can't be determined, or both the
    archive and forecast endpoints fail -- callers should fall back to
    asking the driver to fill the fields in manually rather than blocking
    on this."""
    located = session_location_and_time(session)
    if located is None:
        return None
    lat, lon, dt = located
    altitude = session_gps_altitude_m(session)
    date_str = dt.strftime("%Y-%m-%d")
    target_prefix = dt.strftime("%Y-%m-%dT%H")

    for archive in (True, False):
        data = _fetch_hourly(lat, lon, date_str, archive=archive)
        hourly = (data or {}).get("hourly")
        times = (hourly or {}).get("time") or []
        if not times:
            continue

        index = next((i for i, t in enumerate(times) if t.startswith(target_prefix)), None)
        if index is None:
            continue

        def _at(key: str) -> float | None:
            values = hourly.get(key) or []
            return float(values[index]) if index < len(values) and values[index] is not None else None

        precip = hourly.get("precipitation") or []
        source = "open-meteo (archive)" if archive else "open-meteo (forecast)"
        if altitude is not None:
            source += " + GPS altitude"
        return TrackConditions(
            condition=_classify_condition(precip, index),
            temperature_c=_at("temperature_2m"),
            humidity_pct=_at("relative_humidity_2m"),
            pressure_hpa=_at("surface_pressure"),
            altitude_m=altitude if altitude is not None else (data or {}).get("elevation"),
            source=source,
        )
    return None
