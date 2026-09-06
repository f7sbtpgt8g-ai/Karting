"""Filters the device's session list down to a chosen time window
*before* anything is downloaded.

At the track, `list_sessions()` can return hundreds of entries (see
findings.md -- a device that's never been cleared out), and downloading
+ decoding every one of them just to check its date would be slow on a
device that is itself a small embedded system. The device's session
filenames encode the recording date/time
(`YYMMDD_HHMM_<name>.uni`, findings.md's "Filename convention" section,
confirmed against the file's own internal `DATE` chunk), so the window
can be applied to the filename alone -- no download needed for a session
that's getting skipped anyway.

A name that doesn't match the expected prefix (unexpected device/firmware
naming) is always kept rather than silently dropped: correctness (don't
lose a real session) wins over the performance optimization for that rare
case, since the optimization only matters for names it *can* parse.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_NAME_DATE_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})_")

SYNC_PERIOD_TODAY = "today"
SYNC_PERIOD_LAST_WEEK = "last_week"
SYNC_PERIOD_LAST_MONTH = "last_month"
SYNC_PERIOD_ALL = "all"

SYNC_PERIODS = (SYNC_PERIOD_TODAY, SYNC_PERIOD_LAST_WEEK, SYNC_PERIOD_LAST_MONTH, SYNC_PERIOD_ALL)

# Labels for a settings dropdown -- "today" first and default, since
# that's the expected common case (synced right after each session, at
# the track) and the cheapest one to filter.
SYNC_PERIOD_LABELS = {
    SYNC_PERIOD_TODAY: "Today only",
    SYNC_PERIOD_LAST_WEEK: "Last 7 days",
    SYNC_PERIOD_LAST_MONTH: "Last 30 days",
    SYNC_PERIOD_ALL: "Everything on the device",
}

DEFAULT_SYNC_PERIOD = SYNC_PERIOD_TODAY


def parse_session_datetime(name: str) -> datetime | None:
    """Extract the recording timestamp from a device filename, or None
    if it doesn't match the known `YYMMDD_HHMM_...` convention."""
    match = _NAME_DATE_RE.match(name)
    if match is None:
        return None
    yy, mm, dd, hh, minute = (int(g) for g in match.groups())
    try:
        return datetime(2000 + yy, mm, dd, hh, minute)
    except ValueError:
        return None


def cutoff_for(period: str, now: datetime | None = None) -> datetime | None:
    """The earliest recording time to include for a given period, or
    None for `SYNC_PERIOD_ALL` (no filtering)."""
    if period not in SYNC_PERIODS:
        raise ValueError(f"unknown sync period: {period!r}")
    now = now or datetime.now()
    if period == SYNC_PERIOD_TODAY:
        return datetime(now.year, now.month, now.day)
    if period == SYNC_PERIOD_LAST_WEEK:
        return now - timedelta(days=7)
    if period == SYNC_PERIOD_LAST_MONTH:
        return now - timedelta(days=30)
    return None


def session_in_period(name: str, cutoff: datetime | None) -> bool:
    """True if `name` should be included for a given cutoff (from
    `cutoff_for`). A cutoff of None means "everything". A name whose date
    can't be parsed is always included -- see module docstring."""
    if cutoff is None:
        return True
    recorded_at = parse_session_datetime(name)
    if recorded_at is None:
        return True
    return recorded_at >= cutoff
