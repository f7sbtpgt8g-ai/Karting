"""Builds small, synthetic .uni-format byte blobs matching the confirmed
byte layout in ../../findings.md, for tests that don't depend on a real
device capture (none is available in this environment -- the device has
to be physically present). Every offset/width/formula here is a literal
mirror of core/uni_format.py's decode side, so these tests are really
checking "does the decoder correctly invert what we believe the encoder
does," not testing against real device output directly (that validation
already happened during the reverse-engineering, real files, documented
in findings.md).

Known limitation carried over from findings.md: the DOP fields (byte 34
onward in a GPS-fix record) and the battery/internal-temperature fields
(byte 11 onward in a housekeeping record) have byte ranges that overlap
by one byte and were never independently re-derived to a clean boundary.
Fixtures here still populate them (so decode doesn't crash and returns
plausible floats), but tests should not assert an exact round-trip value
for those specific fields -- see the comments at each call site.
"""

from __future__ import annotations

_MAGIC = b"UUni"
_MARKER = bytes([0xDA, 0x7A])


def _chunk(tag: bytes, payload: bytes, version: int = 1, length_override: int | None = None) -> bytes:
    assert len(tag) == 8
    length = length_override if length_override is not None else len(payload)
    return tag + bytes([version]) + length.to_bytes(3, "big") + payload


def date_chunk(year: int, month: int, day: int, hour: int, minute: int, second: int) -> bytes:
    payload = bytes([0, year - 2000, month, day, hour, minute, second])
    return _chunk(b"RECRDATE", payload)


def glos_chunk(track_name: str, beacons: list[tuple[float, float]] | None = None) -> bytes:
    header = b"UGse" + b"\x00" * 4 + track_name.encode("ascii") + b"\x00" * 8
    # _scan_beacon_points (like the real decoder) steps through the
    # payload 4 bytes at a time looking for the beacon pattern, so the
    # beacon data must start on a 4-byte boundary -- pad up to one if the
    # track name made the header an odd length.
    header += b"\x00" * ((-len(header)) % 4)
    payload = header
    for lat, lon in beacons or []:
        payload += (
            int(round(lat * 1e7)).to_bytes(4, "big", signed=True)
            + b"\x00\x00\x00\x00"
            + int(round(lon * 1e7)).to_bytes(4, "big", signed=True)
            + b"\x00\x00\x00\x00"
        )
    return _chunk(b"RECRGLOS", payload)


def _record(body: bytes, counter: int = 0) -> bytes:
    return _MARKER + counter.to_bytes(2, "little") + body


def gps_fix_body(lat: float, lon: float, alt: float, speed: float, pdop: float = 1.0, hdop: float = 1.0, vdop: float = 1.0) -> bytes:
    """41-byte body for a type-45 (GPS fix) record. lat/lon/alt/speed are
    exact round-trips; pdop/hdop/vdop share overlapping byte ranges (see
    module docstring) so are populated but not guaranteed exact."""
    body = bytearray(41)
    body[13:17] = int(round(lat * 1e7)).to_bytes(4, "big", signed=True)
    body[17:21] = int(round(lon * 1e7)).to_bytes(4, "big", signed=True)
    body[23:25] = int(round(alt * 1000)).to_bytes(2, "big", signed=True)
    body[27:29] = int(round(speed * 100)).to_bytes(2, "big", signed=True)
    body[34:36] = int(round(pdop * 100)).to_bytes(2, "little", signed=True)
    body[36:38] = int(round(hdop * 100)).to_bytes(2, "little", signed=True)
    body[37:39] = int(round(vdop * 25600)).to_bytes(2, "little", signed=True)
    return bytes(body)


def gps_fix_record(*args, counter: int = 0, **kwargs) -> bytes:
    return _record(gps_fix_body(*args, **kwargs), counter=counter)


def housekeeping_body(battery_v: float = 3.76, internal_temp_c: float = 19.0) -> bytes:
    """28-byte body for a type-32 (housekeeping) record. Battery/temp
    share an overlapping byte range (see module docstring) -- populated
    but not guaranteed to both round-trip exactly at once."""
    body = bytearray(28)
    battery_raw = int(round((battery_v + 15.36) / 0.01))
    temp_raw = int(round((internal_temp_c - 17.92) * 25600))
    body[11:13] = battery_raw.to_bytes(2, "little", signed=True)
    body[12:14] = (temp_raw & 0xFFFF).to_bytes(2, "little", signed=False)
    return bytes(body)


def housekeeping_record(*args, counter: int = 0, **kwargs) -> bytes:
    return _record(housekeeping_body(*args, **kwargs), counter=counter)


# reclen -> (body length, rpm offset, steering offset or None)
_RPM_STEERING_LAYOUT = {
    14: (10, 6, None),
    18: (14, 10, None),
    24: (20, 6, 16),
    28: (24, 10, 20),
}

_STEERING_SCALE = 0.092
_STEERING_INTERCEPT = 0.8


def rpm_steering_body(reclen: int, rpm: float, steering_deg: float | None = None) -> bytes:
    body_len, rpm_offset, steer_offset = _RPM_STEERING_LAYOUT[reclen]
    body = bytearray(body_len)
    body[rpm_offset : rpm_offset + 2] = int(round(rpm)).to_bytes(2, "big", signed=True)
    if steer_offset is not None and steering_deg is not None:
        raw = int(round((steering_deg - _STEERING_INTERCEPT) / _STEERING_SCALE))
        body[steer_offset : steer_offset + 2] = raw.to_bytes(2, "big", signed=True)
    return bytes(body)


def rpm_steering_record(reclen: int, rpm: float, steering_deg: float | None = None, counter: int = 0) -> bytes:
    return _record(rpm_steering_body(reclen, rpm, steering_deg), counter=counter)


def build_uni_file(
    date=(2026, 8, 29, 14, 41, 20),
    track_name: str = "Test Track",
    beacons: list[tuple[float, float]] | None = None,
    records: list[bytes] | None = None,
) -> bytes:
    """Assemble a minimal but structurally valid .uni file: magic + 4
    unused bytes, RECRDATE, RECRGLOS (with the track name and optional
    beacon points), then RECRDATA holding the given pre-built records
    concatenated in order, with a real (non-sentinel) length."""
    header = _MAGIC + b"\x00\x00\x00\x04"
    chunks = date_chunk(*date) + glos_chunk(track_name, beacons)
    # Real records are only recognized when *bounded* by a following
    # marker (the decoder measures a record's length as the gap to the
    # next marker -- see core/uni_format.py's _iter_records, which real
    # captures also always drop their final, unbounded record for the
    # same reason). Append a bare trailing marker so the *last* real
    # record here gets a proper boundary too.
    recrdata_payload = b"".join(records or []) + (_MARKER if records else b"")
    chunks += _chunk(b"RECRDATA", recrdata_payload)
    return header + chunks
