"""Persisting a session's analysis as rows.

`analysis.py` computes everything; this writes it down. The two are kept
apart on purpose -- `analyze_session` is pure and has no idea a database
exists, which is what lets the same call serve the worker, a test and a
CLI backfill.

Why persist at all, when the raw dataframe is already stored: a browser
cannot read Parquet out of a BYTEA column over PostgREST, so every trace,
delta and sector time Lap Analysis draws was unreachable from the Next.js
frontend. Writing the derived output as ordinary rows makes it queryable
under the same RLS policies as everything else -- and, measured on the
bundled export, costs about 4% of the blob it came from, which is what then
makes clearing the blob worthwhile.

Postgres only. The SQLite path exists for offline single-machine use where
Streamlit reads the dataframe directly and needs none of this, so rather
than a second implementation that would never be exercised, the writer
no-ops when no Postgres is configured.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np
import pandas as pd

from . import db as pgdb
from .analysis import SessionAnalysis, analyze_lap
from .metrics import add_engine_temperature
from .parser import Session

logger = logging.getLogger(__name__)

# Bumped when the stored shape changes or an analysis threshold moves, so a
# backfill can find rows that predate the change instead of inferring it
# from a timestamp.
#
# 2: added lap_traces.max_speed_kmh / max_rpm (0006). Forgetting to bump this
#    when those columns arrived left every already-analysed session with them
#    NULL and no way to notice: `has_stored_analysis` reported the rows
#    current, so the backfill skipped them and the summary cards stayed blank
#    forever. Adding a stored field without bumping this is the same bug
#    again -- the version is the only thing that makes old rows findable.
# 3: added the per-lap engine figures and the G/temperature arrays (0007).
ANALYSIS_VERSION = 3

# The trace columns Lap Analysis and the track map actually draw. Named here
# rather than storing the whole metric trace because the parsed dataframe
# carries ~20 channels, most of them GPS dilution-of-precision figures that
# nothing plots.
_TRACE_COLUMNS = {
    "distance_m": "lap_distance_m",
    "lap_time_s": "lap_time_s",
    "speed_kmh": "GPS Speed",
    "rpm": "RPM",
    "latitude": "Latitude",
    "longitude": "Longitude",
    # This logger has no brake or throttle channel, so lateral and
    # longitudinal G are how cornering load and braking are read at all.
    "lateral_g": "GPS Lateral Acceleration",
    "longitudinal_g": "GPS Longitudinal Acceleration",
    # "Temperature 1" is the engine sensor. "Internal Temperature" is the
    # logger's own, and is not what an engine page means by temperature.
    "temp_c": "Temperature 1",
}

# The Rotax peak-power band. Only meaningful for a Rotax, but "share of the
# lap in this RPM window" is computed for every lap regardless -- the class
# decides whether the number is worth showing, not whether it can be
# measured.
POWERZONE_RPM = (9000.0, 12000.0)
_BOOL_COLUMNS = {"braking": "braking_estimate", "power_on": "power_on_estimate"}


def _clean(value: Any) -> Any:
    """JSON-safe. numpy scalars, NaN and tuples all appear in the analysis
    output (`summary` holds np.float64, `setup_suggestions` holds a tuple
    for the peak-power band), and psycopg2 adapts none of them."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        # NaN and infinity are not JSON, and "unknown" is what they mean
        # here -- a single clean lap has no standard deviation.
        return None if not np.isfinite(number) else number
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, pd.DataFrame):
        return [_clean(row) for row in value.to_dict(orient="records")]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _floats(series: pd.Series | None) -> list[float | None] | None:
    """A float8[] column's worth of values, with NaN as SQL NULL."""
    if series is None:
        return None
    return [None if pd.isna(v) else float(v) for v in series]


def store_session_analysis(
    session_db_id: int, session: Session, analysis: SessionAnalysis
) -> bool:
    """Write one session's analysis: the session row, every lap's segment
    times, and every lap's trace. Returns False when no Postgres is
    configured (the offline case), True once written.

    Idempotent: re-analyzing a session replaces its rows rather than
    accumulating a second copy, so a backfill can be re-run and the worker
    can re-process a batch safely.
    """
    if not pgdb.has_postgres_configured():
        return False

    segment_rows: list[tuple] = []
    trace_rows: list[tuple] = []

    if analysis.ok:
        for lap_number in analysis.laps["lap_number"].tolist():
            lap = analyze_lap(session, analysis, int(lap_number))

            for index, row in enumerate(lap.segment_times.to_dict(orient="records")):
                segment_rows.append(
                    (
                        session_db_id,
                        int(lap_number),
                        index,
                        row.get("segment_label"),
                        row.get("segment_kind"),
                        _clean(row.get("time_s")),
                    )
                )

            trace = lap.metric_trace
            if trace.empty or "lap_distance_m" not in trace.columns:
                continue
            # Only the engine page reads temperature, so it is merged on here
            # rather than being carried through the whole analysis pipeline.
            trace = add_engine_temperature(session, int(lap_number), trace)
            values = {
                name: _floats(trace[column]) if column in trace.columns else None
                for name, column in _TRACE_COLUMNS.items()
            }
            booleans = {
                name: (
                    [bool(v) for v in trace[column].fillna(False)]
                    if column in trace.columns
                    else None
                )
                for name, column in _BOOL_COLUMNS.items()
            }
            # Per-lap aggregates. Derivable from the arrays above, but only
            # by shipping every lap's full trace to a browser to render a
            # table of numbers.
            def agg(column: str, how: str) -> float | None:
                if column not in trace.columns:
                    return None
                series = trace[column].dropna()
                if series.empty:
                    return None
                return float(getattr(series, how)())

            peak = {
                "max_speed": agg("GPS Speed", "max"),
                "min_speed": agg("GPS Speed", "min"),
                "max_rpm": agg("RPM", "max"),
                "min_rpm": agg("RPM", "min"),
                "avg_rpm": agg("RPM", "mean"),
                "max_temp": agg("Temperature 1", "max"),
                "min_temp": agg("Temperature 1", "min"),
                "avg_temp": agg("Temperature 1", "mean"),
            }

            powerzone = None
            if "RPM" in trace.columns:
                revs = trace["RPM"].dropna()
                if not revs.empty:
                    inside = revs.between(*POWERZONE_RPM).sum()
                    powerzone = float(inside) / float(len(revs)) * 100.0
            trace_rows.append(
                (
                    session_db_id,
                    int(lap_number),
                    len(trace),
                    values["distance_m"],
                    values["lap_time_s"],
                    values["speed_kmh"],
                    values["rpm"],
                    values["latitude"],
                    values["longitude"],
                    booleans["braking"],
                    booleans["power_on"],
                    peak["max_speed"],
                    peak["max_rpm"],
                    peak["min_speed"],
                    peak["min_rpm"],
                    peak["avg_rpm"],
                    peak["max_temp"],
                    peak["min_temp"],
                    peak["avg_temp"],
                    powerzone,
                    values["lateral_g"],
                    values["longitudinal_g"],
                    values["temp_c"],
                )
            )

    with pgdb.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO session_analysis
                   (session_db_id, analysis_version, best_lap, theoretical_best_s,
                    speed_is_estimated, clean_lap_numbers, summary, segments,
                    best_segment_times, setup_suggestions, data_error, analyzed_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
               ON CONFLICT (session_db_id) DO UPDATE SET
                   analysis_version = EXCLUDED.analysis_version,
                   best_lap = EXCLUDED.best_lap,
                   theoretical_best_s = EXCLUDED.theoretical_best_s,
                   speed_is_estimated = EXCLUDED.speed_is_estimated,
                   clean_lap_numbers = EXCLUDED.clean_lap_numbers,
                   summary = EXCLUDED.summary,
                   segments = EXCLUDED.segments,
                   best_segment_times = EXCLUDED.best_segment_times,
                   setup_suggestions = EXCLUDED.setup_suggestions,
                   data_error = EXCLUDED.data_error,
                   analyzed_at = now()""",
            (
                session_db_id,
                ANALYSIS_VERSION,
                analysis.best_lap,
                _clean(analysis.theoretical_best_s),
                bool(analysis.speed_is_estimated),
                [int(n) for n in analysis.clean_lap_numbers],
                json.dumps(_clean(analysis.summary)),
                json.dumps(_clean(analysis.segments)),
                json.dumps(_clean(analysis.best_segment_times)),
                json.dumps(_clean(analysis.setup_suggestions)),
                analysis.data_error,
            ),
        )

        # Replaced wholesale rather than upserted row by row: a re-analysis
        # can produce a different number of segments per lap, and leftover
        # rows from the previous shape would read as real sectors.
        cur.execute("DELETE FROM lap_segment_times WHERE session_db_id = %s", (session_db_id,))
        cur.executemany(
            "INSERT INTO lap_segment_times "
            "(session_db_id, lap_number, segment_index, segment_label, segment_kind, time_s) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            segment_rows,
        )

        cur.execute("DELETE FROM lap_traces WHERE session_db_id = %s", (session_db_id,))
        cur.executemany(
            "INSERT INTO lap_traces "
            "(session_db_id, lap_number, sample_count, distance_m, lap_time_s, speed_kmh, "
            " rpm, latitude, longitude, braking, power_on, max_speed_kmh, max_rpm, "
            " min_speed_kmh, min_rpm, avg_rpm, max_temp_c, min_temp_c, avg_temp_c, "
            " powerzone_pct, lateral_g, longitudinal_g, temp_c) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            trace_rows,
        )
        conn.commit()

    logger.info(
        "stored analysis for session %s: %s segment time(s), %s lap trace(s)",
        session_db_id,
        len(segment_rows),
        len(trace_rows),
    )
    return True


def has_stored_analysis(session_db_id: int, version: int = ANALYSIS_VERSION) -> bool:
    """Whether this session already has analysis at the current version --
    what a backfill skips on."""
    if not pgdb.has_postgres_configured():
        return False
    with pgdb.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM session_analysis WHERE session_db_id = %s AND analysis_version >= %s",
            (session_db_id, version),
        )
        return cur.fetchone() is not None
