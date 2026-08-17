"""Regression tests for the "combined outlier lap that doesn't match the
dash" bug report: split_sessions was over-sensitive to ordinary row-order
jitter, and lap_table merged rows by raw Lap Number value instead of by
contiguous run, silently combining two physically distinct laps whenever a
lap number repeated non-adjacently.
"""

import pandas as pd

from telemetry.laps import lap_table
from telemetry.parser import Session, split_sessions

ALWAYS_ON_DEFAULTS = {"Start Date": "16-08-2026", "Start Time": "10:00:00"}


def _row(session_time_s, lap_number, lap_time_s, **extra):
    row = dict(ALWAYS_ON_DEFAULTS)
    row["Lap Number"] = lap_number
    row["Session Time"] = session_time_s * 1_000_000_000
    row["Lap Time"] = lap_time_s * 1_000_000_000
    row["session_time_s"] = session_time_s
    row["lap_time_s"] = lap_time_s
    row.update(extra)
    return row


def _session_from_rows(rows):
    df = pd.DataFrame(rows)
    return Session(session_id=0, source_file="test", df=df)


def test_split_sessions_ignores_millisecond_jitter():
    """A few milliseconds of backward jitter between rows (different
    channels buffered/flushed independently) must NOT be read as a logger
    restart -- only a large drop back near zero should split."""
    rows = []
    t = 0.0
    for i in range(200):
        rows.append(_row(t, 0, t))
        t += 0.02
    # inject a tiny (5ms) backward jitter mid-session -- not a real restart
    rows.insert(100, _row(rows[99]["session_time_s"] - 0.005, 0, rows[99]["session_time_s"] - 0.005))
    df = pd.DataFrame(rows)

    sessions = split_sessions(df, source_file="test")
    assert len(sessions) == 1


def test_split_sessions_still_detects_a_real_restart():
    rows = []
    t = 0.0
    for i in range(50):
        rows.append(_row(t, i // 10, t))
        t += 0.1
    restart_start = len(rows)
    t2 = 0.0
    for i in range(50):
        rows.append(_row(t2, i // 10, t2))
        t2 += 0.1
    df = pd.DataFrame(rows)

    sessions = split_sessions(df, source_file="test")
    assert len(sessions) == 2
    assert len(sessions[0].df) == restart_start
    assert len(sessions[1].df) == len(rows) - restart_start


def test_lap_table_does_not_merge_nonadjacent_same_lap_number():
    """Two physically distinct laps that happen to share a Lap Number value
    (e.g. from a missed session-restart split slipping through) must show
    up as two separate lap_table rows, not get merged into one inflated
    "outlier" lap."""
    rows = []
    # lap 0: a normal ~15s lap
    for i, t in enumerate([x * 0.1 for x in range(150)]):
        rows.append(_row(t, 0, t))
    # lap 1: a normal ~15s lap
    offset = rows[-1]["session_time_s"] + 0.1
    for i, t in enumerate([x * 0.1 for x in range(150)]):
        rows.append(_row(offset + t, 1, t))
    # a SECOND, later occurrence of "lap 0" (simulating a missed restart-split)
    offset2 = rows[-1]["session_time_s"] + 0.1
    for i, t in enumerate([x * 0.1 for x in range(150)]):
        rows.append(_row(offset2 + t, 0, t))

    session = _session_from_rows(rows)
    laps = lap_table(session)

    assert len(laps) == 3
    assert list(laps["lap_number"]) == [0, 1, 0]
    # none of these laps should show an inflated combined duration
    assert laps["lap_time_s"].max() < 16


def test_lap_table_merges_a_lone_stray_mistagged_row():
    """A single stray row carrying the previous lap's number (e.g. a
    delayed buffered channel write) chronologically after the real
    transition should be folded into the lap it actually belongs to, not
    reported as its own bogus micro-lap."""
    rows = []
    for t in [x * 0.1 for x in range(150)]:
        rows.append(_row(t, 0, t))
    lap0_end = rows[-1]["session_time_s"]
    for t in [x * 0.1 for x in range(150)]:
        rows.append(_row(lap0_end + 0.1 + t, 1, t))
    # one stray row, chronologically well inside lap 1's span, mistagged as lap 0
    stray_time = lap0_end + 5.0
    rows.append(_row(stray_time, 0, lap0_end))

    session = _session_from_rows(rows)
    laps = lap_table(session)

    assert len(laps) == 2
    assert list(laps["lap_number"]) == [0, 1]
