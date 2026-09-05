"""Decoder for the raw .uni binary format written directly by a UniGo
laptimer device onto its own storage -- the file you get back from
`GET /file?filename=<name>` on the device's local web server, *not* a
Unipro Analyser TSV export.

Everything in this module was reverse-engineered from scratch (Unipro
publishes no format spec or SDK) and cross-validated against five real
`.uni` + real Analyser `.tsv` export pairs across three different tracks.
See `../findings.md` for the full derivation, every confidence number
below, and the negative results for what's *not* decoded here. Confirmed
against firmware `1.20.002` (device self-identifies as `"unigo-one"`) --
unverified on any other firmware version.

What's decoded, and how well (see findings.md for exact R^2/method):
  - Latitude, Longitude, Altitude, GPS Speed        R^2=1.0 / 1.0 / 1.0 / 0.998
  - Positional/Horizontal/Vertical DOP              R^2=1.0 (byte boundaries
                                                      between the three are close
                                                      together and not independently
                                                      re-derived -- see findings.md)
  - Battery Voltage, Internal Temperature           R^2=1.0
  - RPM                                             R^2=0.999, confirmed on 5 real
                                                      sessions across 3 tracks
  - Steering Angle                                  R^2=0.82-0.88 across 3 sessions --
                                                      real and reproducible, NOT
                                                      bit-perfect (~10 degree mean error)
  - Heading, GPS Distance, Lap Number               computed from the decoded GPS
                                                      track (bearing / cumulative
                                                      distance / beacon-crossing),
                                                      not raw stored channels

What's NOT decoded (left blank in the output -- see ../README.md):
  - Vertical Acceleration, GPS Longitudinal/Lateral Acceleration (G-force)
  - Temperature 1 (has real data in every session examined, but the raw
    encoding was never cracked)
  - Slip, Inverse Corner Radius, Corner Radius, GPS Total Acceleration,
    Steering Rate, "Time" -- these were never populated (n=0) in any real
    session examined on this device/config, so leaving them blank matches
    what a real Analyser export of the same data would show anyway.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

_MAGIC = b"UUni"
_RECORD_MARKER = bytes([0xDA, 0x7A])

# GPS updates at a native ~10 Hz on this device (documented independently by
# OpenLap, and consistent with this project's own record counts averaging
# ~10.00-10.03 Hz across 5 real sessions). Our own record-framing decode
# finds *every* GPS-fix record (no heuristic scan / no skipped samples), so
# treating consecutive fixes as exactly 100ms apart is the basis for
# reconstructing elapsed time for every other record type too, via linear
# interpolation over byte offset between fixes. This is a *model*, not a
# directly-decoded field -- no literal high-precision "Session Time" raw
# channel was ever found. See findings.md's "RPM SOLVED" section for the
# calibration technique this generalizes.
_GPS_FIX_INTERVAL_NS = 100_000_000

_LAT_RANGE = (-90.0, 90.0)
_LON_RANGE = (-180.0, 180.0)


class UniFormatError(ValueError):
    """Raised when a .uni file doesn't parse as expected (bad magic, no
    RECRDATA chunk, no GPS fixes found, etc.)."""


def is_uni_file(data: bytes) -> bool:
    return data[:4] == _MAGIC


# ---------------------------------------------------------------------------
# Outer chunk framing: "UUni" magic, then a sequence of 8-byte-tag / 1-byte
# version / 3-byte-big-endian-length chunks. A length of 0xFFFFFF is a
# sentinel meaning "read to end of file" (used by the final RECRDATA chunk,
# which doesn't know its own length until the device finishes writing it).
# ---------------------------------------------------------------------------


def _iter_chunks(data: bytes):
    pos = 8  # 4-byte magic + 4 bytes of unknown meaning (constant so far)
    n = len(data)
    while pos + 12 <= n:
        tag = data[pos : pos + 8]
        if not all(32 <= b < 127 for b in tag):
            break
        length = int.from_bytes(data[pos + 9 : pos + 12], "big")
        payload_start = pos + 12
        if length == 0xFFFFFF:
            length = n - payload_start
        yield tag, data[pos + 8], payload_start, length
        pos = payload_start + length


def _parse_date(payload: bytes) -> datetime | None:
    """RECRDATE payload: 1 unused byte, then raw byte values for
    [year-2000, month, day, hour, minute, second]. Decodes cleanly --
    verified to match the device's own filename timestamp exactly on
    every real file examined."""
    if len(payload) < 7:
        return None
    yy, mm, dd, hh, mi, ss = payload[1:7]
    try:
        return datetime(2000 + yy, mm, dd, hh, mi, ss, tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_track_name(payload: bytes, fallback: str) -> str:
    """RECRGLOS embeds the track/session name as a plain-ASCII run inside
    an otherwise binary payload. Take the longest printable-ASCII run
    rather than a fixed byte offset, since the surrounding fields aren't
    fully mapped."""
    best = b""
    run = bytearray()
    for b in payload:
        if 32 <= b < 127:
            run.append(b)
        else:
            if len(run) > len(best):
                best = bytes(run)
            run.clear()
    if len(run) > len(best):
        best = bytes(run)
    text = best.decode("ascii", "ignore").strip()
    return text if len(text) >= 3 else fallback


def _scan_beacon_points(payload: bytes, ref_lat: float, ref_lon: float, max_km: float = 5.0):
    """Recover the track's timing-beacon GPS coordinates from RECRGLOS.

    Beacons are stored as zero-padded pairs -- [lat_raw][0000][lon_raw][0000]
    at the same raw/1e7 degree scale as the GPS fixes -- found by scanning
    for that plausible-value pattern rather than a fixed byte offset (the
    surrounding fields in RECRGLOS aren't otherwise mapped). ref_lat/lon
    (the session's own decoded track position) keeps matches from picking
    up unrelated binary noise elsewhere in the payload.
    """
    points: list[tuple[float, float]] = []
    n = len(payload)
    i = 0
    while i + 16 <= n:
        if payload[i + 4 : i + 8] == b"\x00\x00\x00\x00" and payload[i + 12 : i + 16] == b"\x00\x00\x00\x00":
            lat_raw = int.from_bytes(payload[i : i + 4], "big", signed=True)
            lon_raw = int.from_bytes(payload[i + 8 : i + 12], "big", signed=True)
            if lat_raw or lon_raw:
                lat, lon = lat_raw / 1e7, lon_raw / 1e7
                if (
                    _LAT_RANGE[0] <= lat <= _LAT_RANGE[1]
                    and _LON_RANGE[0] <= lon <= _LON_RANGE[1]
                    and _haversine_km(lat, lon, ref_lat, ref_lon) <= max_km
                ):
                    points.append((lat, lon))
                    i += 16
                    continue
        i += 4
    return points


# ---------------------------------------------------------------------------
# RECRDATA record framing: every record starts with a 2-byte marker
# (0xDA 0x7A), a 2-byte monotonically-increasing sequence field, then a
# record-type-specific fixed-length body. Record length (2+2+len(body))
# is how the type is identified -- see findings.md's "BREAKTHROUGH"
# section for how this was found and verified (every one of 7,382+
# confirmed GPS fixes landed in a 45-byte record with zero exceptions).
# ---------------------------------------------------------------------------


def _iter_records(recrdata_payload: bytes):
    """Yield (byte_offset, record_length, body) for every marker-delimited
    record in a RECRDATA payload, in file order. `body` excludes the
    4-byte marker+sequence prefix."""
    positions = []
    start = 0
    while True:
        idx = recrdata_payload.find(_RECORD_MARKER, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + 1
    for i in range(len(positions) - 1):
        p = positions[i]
        reclen = positions[i + 1] - p
        yield p, reclen, recrdata_payload[p + 4 : p + reclen]


def _i(body: bytes, offset: int, width: int, endian: str = "big", signed: bool = True) -> int | None:
    if offset + width > len(body):
        return None
    return int.from_bytes(body[offset : offset + width], endian, signed=signed)


# Confirmed byte offsets are all *within body* (i.e. after the 4-byte
# marker+sequence prefix). Add 4 to get the offset from the very start of
# the record, if cross-referencing findings.md's earlier notes.
_GPS_FIX_RECLEN = 45
_HOUSEKEEPING_RECLEN = 32

# Steering Angle's calibration is an average across 4 independently-fit
# record-type/session combinations (slopes 0.0904-0.0941, intercepts
# 0.35-1.17) -- there is no single bit-perfect formula yet, see
# findings.md. Treat this column as a real but approximate signal
# (~10 degree typical error against Analyser's own filtered value), not a
# ground-truth-quality one.
_STEERING_SCALE = 0.092
_STEERING_INTERCEPT = 0.8


def _decode_gps_fix(body: bytes) -> dict:
    out: dict = {}
    lat = _i(body, 13, 4)
    lon = _i(body, 17, 4)
    alt = _i(body, 23, 2)
    speed = _i(body, 27, 2)
    if lat is not None:
        out["Latitude"] = lat / 1e7
    if lon is not None:
        out["Longitude"] = lon / 1e7
    if alt is not None:
        out["Altitude"] = alt / 1000
    if speed is not None:
        out["GPS Speed"] = speed / 100
    pdop = _i(body, 34, 2, "little")
    hdop = _i(body, 36, 2, "little")
    vdop = _i(body, 37, 2, "little")
    if pdop is not None:
        out["Positional DOP"] = pdop / 100
    if hdop is not None:
        out["Horizontal DOP"] = hdop / 100
    if vdop is not None:
        out["Vertical DOP"] = vdop / 25600
    return out


def _decode_housekeeping(body: bytes) -> dict:
    out: dict = {}
    battery = _i(body, 11, 2, "little")
    temp = _i(body, 12, 2, "little", signed=False)
    if battery is not None:
        out["Battery Voltage"] = battery * 0.01 - 15.36
    if temp is not None:
        out["Internal Temperature"] = temp / 25600 + 17.92
    return out


# reclen -> (rpm_offset, steering_offset_or_None)
_RPM_STEERING_OFFSETS = {
    14: (6, None),
    18: (10, None),
    24: (6, 16),
    28: (10, 20),
}


def _decode_rpm_steering(reclen: int, body: bytes) -> dict:
    out: dict = {}
    rpm_offset, steer_offset = _RPM_STEERING_OFFSETS[reclen]
    rpm = _i(body, rpm_offset, 2)
    if rpm is not None and rpm >= 0:
        out["RPM"] = float(rpm)
        out["RPM unfiltered"] = float(rpm)
    if steer_offset is not None:
        steer = _i(body, steer_offset, 2)
        if steer is not None:
            out["Steering Angle"] = steer * _STEERING_SCALE + _STEERING_INTERCEPT
    return out


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    return math.degrees(math.atan2(y, x)) % 360.0


@dataclass
class UniHeader:
    date: datetime | None
    track_name: str
    device_serial: int | None
    firmware_version: str | None
    beacons: list[tuple[float, float]] = field(default_factory=list)


def _parse_header(data: bytes, recrglos_payload_for_beacons: bytes | None, ref_lat: float | None, ref_lon: float | None) -> UniHeader:
    import json

    date = None
    track_name = "session"
    serial = None
    firmware = None
    for tag, _version, pstart, length in _iter_chunks(data):
        if tag == b"RECRDATE":
            date = _parse_date(data[pstart : pstart + length])
        elif tag == b"RECRDEVI":
            try:
                devi = json.loads(data[pstart : pstart + length].split(b"\x00")[0])
                dev_key = next(iter(devi))
                serial = devi[dev_key].get("serial_number")
                firmware = devi[dev_key].get("firmware_version")
            except Exception:
                pass
        elif tag == b"RECRGLOS":
            track_name = _parse_track_name(data[pstart : pstart + length], track_name)

    beacons: list[tuple[float, float]] = []
    if recrglos_payload_for_beacons is not None and ref_lat is not None and ref_lon is not None:
        beacons = _scan_beacon_points(recrglos_payload_for_beacons, ref_lat, ref_lon)

    return UniHeader(date=date, track_name=track_name, device_serial=serial, firmware_version=firmware, beacons=beacons)


def _detect_lap_numbers(elapsed_ns: np.ndarray, lats: np.ndarray, lons: np.ndarray, gate_lat: float, gate_lon: float, min_lap_time_s: float = 15.0, gate_radius_m: float = 30.0) -> np.ndarray:
    """Assign a lap number to each GPS fix by detecting crossings of the
    track's own timing-beacon gate, the same technique OpenLap uses:
    project every point onto a local (forward, lateral) frame centred on
    the gate (using the track's own heading as it passes closest to the
    gate), and call it a crossing where the forward coordinate goes
    negative-to-positive while still laterally within the gate radius.
    Falls back to a single lap (all zeros) if the beacon is never close
    to the track, or there aren't enough points.
    """
    n = len(lats)
    lap_nums = np.zeros(n, dtype=int)
    if n < 8:
        return lap_nums

    m_per_deg_lat = 110_540.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(gate_lat))
    east = (lons - gate_lon) * m_per_deg_lon
    north = (lats - gate_lat) * m_per_deg_lat
    dist = np.hypot(east, north)

    closest = int(np.argmin(dist))
    if dist[closest] > gate_radius_m:
        return lap_nums

    lo, hi = max(0, closest - 3), min(n - 1, closest + 3)
    if lo == hi:
        return lap_nums
    heading = math.radians(_bearing_deg(lats[lo], lons[lo], lats[hi], lons[hi]))
    fwd_e, fwd_n = math.sin(heading), math.cos(heading)
    right_e, right_n = fwd_n, -fwd_e

    fwd = east * fwd_e + north * fwd_n
    lateral = east * right_e + north * right_n

    elapsed_s = elapsed_ns / 1e9
    crossings: list[float] = []
    last_cross = -math.inf
    for i in range(1, n):
        if fwd[i - 1] < 0.0 <= fwd[i] and abs(lateral[i]) <= gate_radius_m:
            t = elapsed_s[i - 1]
            df = fwd[i] - fwd[i - 1]
            if df > 1e-9:
                t += (-fwd[i - 1] / df) * (elapsed_s[i] - elapsed_s[i - 1])
            if t - last_cross >= min_lap_time_s:
                crossings.append(t)
                last_cross = t

    if crossings:
        lap_nums = np.searchsorted(np.array(crossings), elapsed_s, side="right")
    return lap_nums


def decode_uni_bytes(data: bytes) -> pd.DataFrame:
    """Decode a raw .uni file's bytes into a DataFrame shaped like a real
    Unipro Analyser TSV export (sparse rows, one per channel-update event,
    same column names as `telemetry.parser.COLUMNS`) -- so it can be
    written straight out with `tsv_writer.write_tsv` and read back in by
    the existing `telemetry.parser` pipeline unmodified.

    Raises UniFormatError if the file doesn't look like a UniGo .uni file
    or no GPS fixes could be decoded (which this decoder needs as its
    time base -- see the module docstring on elapsed-time reconstruction).
    """
    if not is_uni_file(data):
        raise UniFormatError("not a .uni file (bad magic)")

    recrdata_start = recrdata_len = None
    recrglos_payload = None
    for tag, _version, pstart, length in _iter_chunks(data):
        if tag == b"RECRDATA":
            recrdata_start, recrdata_len = pstart, length
        elif tag == b"RECRGLOS":
            recrglos_payload = data[pstart : pstart + length]

    if recrdata_start is None:
        raise UniFormatError("no RECRDATA chunk found")

    payload = data[recrdata_start : recrdata_start + recrdata_len]
    records = list(_iter_records(payload))
    if not records:
        raise UniFormatError("no records found in RECRDATA")

    gps_offsets: list[int] = []
    gps_rows: list[dict] = []
    other_events: list[tuple[int, dict]] = []  # (byte_offset, fields)

    for offset, reclen, body in records:
        if reclen == _GPS_FIX_RECLEN:
            fields = _decode_gps_fix(body)
            if "Latitude" in fields and "Longitude" in fields:
                gps_offsets.append(offset)
                gps_rows.append(fields)
        elif reclen == _HOUSEKEEPING_RECLEN:
            fields = _decode_housekeeping(body)
            if fields:
                other_events.append((offset, fields))
        elif reclen in _RPM_STEERING_OFFSETS:
            fields = _decode_rpm_steering(reclen, body)
            if fields:
                other_events.append((offset, fields))

    if not gps_rows:
        raise UniFormatError("no GPS fixes could be decoded -- can't establish a time base")

    gps_offsets_arr = np.array(gps_offsets, dtype=np.float64)
    gps_times_ns = np.arange(len(gps_offsets)) * float(_GPS_FIX_INTERVAL_NS)

    def offset_to_elapsed_ns(byte_offsets: np.ndarray) -> np.ndarray:
        return np.interp(byte_offsets, gps_offsets_arr, gps_times_ns)

    lats = np.array([r["Latitude"] for r in gps_rows])
    lons = np.array([r["Longitude"] for r in gps_rows])

    ref_lat, ref_lon = float(np.median(lats)), float(np.median(lons))
    header = _parse_header(data, recrglos_payload, ref_lat, ref_lon)

    lap_nums = np.zeros(len(gps_rows), dtype=int)
    if header.beacons:
        gate_lat, gate_lon = header.beacons[0]
        lap_nums = _detect_lap_numbers(gps_times_ns, lats, lons, gate_lat, gate_lon)

    # Heading (bearing between consecutive fixes) and cumulative GPS
    # Distance (running haversine total in metres) -- computed the same
    # way OpenLap independently reconstructs them, since neither is a
    # confirmed raw stored channel.
    headings = np.zeros(len(gps_rows))
    cum_dist_m = np.zeros(len(gps_rows))
    for i in range(1, len(gps_rows)):
        headings[i] = _bearing_deg(lats[i - 1], lons[i - 1], lats[i], lons[i])
        cum_dist_m[i] = cum_dist_m[i - 1] + _haversine_km(lats[i - 1], lons[i - 1], lats[i], lons[i]) * 1000.0
    if len(gps_rows) > 1:
        headings[0] = headings[1]

    lap_start_ns: dict[int, float] = {}
    for i, ln in enumerate(lap_nums):
        if int(ln) not in lap_start_ns:
            lap_start_ns[int(ln)] = gps_times_ns[i]

    date_str = header.date.strftime("%Y-%m-%d") if header.date else ""
    time_str = header.date.strftime("%H:%M:%S") if header.date else ""

    rows: list[dict] = []
    for i, fields in enumerate(gps_rows):
        row = dict(fields)
        row["Start Date"] = date_str
        row["Start Time"] = time_str
        row["Lap Number"] = int(lap_nums[i])
        row["Session Time"] = gps_times_ns[i]
        row["Lap Time"] = gps_times_ns[i] - lap_start_ns[int(lap_nums[i])]
        row["Heading"] = headings[i]
        row["GPS Distance"] = cum_dist_m[i]
        rows.append(row)

    for offset, fields in other_events:
        elapsed = float(offset_to_elapsed_ns(np.array([offset]))[0])
        ln = int(np.searchsorted(gps_times_ns, elapsed, side="right") - 1) if len(gps_times_ns) else 0
        ln = max(0, min(ln, len(gps_times_ns) - 1))
        ln = int(lap_nums[ln]) if len(lap_nums) else 0
        row = dict(fields)
        row["Start Date"] = date_str
        row["Start Time"] = time_str
        row["Lap Number"] = ln
        row["Session Time"] = elapsed
        row["Lap Time"] = elapsed - lap_start_ns.get(ln, 0.0)
        rows.append(row)

    df = pd.DataFrame(rows)
    df = df.sort_values("Session Time", kind="stable").reset_index(drop=True)
    return df
