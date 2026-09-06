"""Persisting a session's analysis, and reclaiming its blob afterwards.

The reason this exists: `session_cache.dataframe_parquet` is 3-5 MB per
session and is the only place a lap's traces, sector times and deltas live.
That both fills a 500 MB database in about ten track days and makes Lap
Analysis unbuildable on a frontend that cannot read Parquet out of BYTEA.

So the bar for these tests is not "rows appeared" -- it is that what was
read back is the *same numbers* `analyze_session` produced, because the
whole point is being able to delete the source afterwards. A silent
rounding or ordering difference here would be discovered only once the
blob was gone.

Requires a local Postgres, same as tests/test_rls_policies.py:
    RLS_TEST_ADMIN_DSN=postgresql://postgres:postgres@localhost:5432/postgres
"""

from __future__ import annotations

import os
import subprocess

import pytest

psycopg2 = pytest.importorskip("psycopg2")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS = os.path.join(REPO, "supabase", "migrations")
SIMULATION = os.path.join(REPO, "supabase", "testing", "simulate_supabase.sql")
SAMPLE_TSV = os.path.join(REPO, "sample_data", "default_session.tsv")

ADMIN_DSN = os.environ.get(
    "RLS_TEST_ADMIN_DSN", "postgresql://postgres:postgres@localhost:5432/postgres"
)
TEST_DB = os.environ.get("ANALYSIS_STORE_TEST_DB", "analysis_store_test")


def _server_available() -> bool:
    try:
        psycopg2.connect(ADMIN_DSN, connect_timeout=3).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _server_available(), reason="no local Postgres reachable (set RLS_TEST_ADMIN_DSN)"
)


def _psql(dsn: str, path: str) -> None:
    result = subprocess.run(
        ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", path], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AssertionError(f"applying {os.path.basename(path)} failed:\n{result.stderr}")


@pytest.fixture(scope="module")
def db():
    admin = psycopg2.connect(ADMIN_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        cur.execute(f"CREATE DATABASE {TEST_DB}")
    admin.close()

    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + TEST_DB
    _psql(dsn, SIMULATION)
    for name in sorted(os.listdir(MIGRATIONS)):
        if name.endswith(".sql"):
            _psql(dsn, os.path.join(MIGRATIONS, name))

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    yield conn, dsn
    conn.close()


@pytest.fixture(scope="module")
def analyzed(db):
    """One real session out of the bundled export: parsed, saved, analyzed
    and persisted -- the exact sequence the worker runs."""
    conn, dsn = db
    os.environ["SUPABASE_DB_URL"] = dsn

    from telemetry.analysis import analyze_session
    from telemetry.analysis_store import store_session_analysis
    from telemetry.parser import load_sessions
    from telemetry.storage import session_library_from_env

    session = load_sessions(SAMPLE_TSV)[0]
    library = session_library_from_env(os.path.join(REPO, "data", "unused.db"))
    session_db_id = library.save_session(session, driver="Tester", track_name="Ring")

    analysis = analyze_session(session)
    assert analysis.ok, analysis.data_error
    assert store_session_analysis(session_db_id, session, analysis)

    return {"conn": conn, "id": session_db_id, "session": session, "analysis": analysis}


# ------------------------------------------------------- session-level facts


def test_session_analysis_row_matches_what_was_computed(analyzed):
    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "SELECT best_lap, theoretical_best_s, speed_is_estimated, clean_lap_numbers, "
            "summary, data_error FROM session_analysis WHERE session_db_id=%s",
            (analyzed["id"],),
        )
        best_lap, theo, estimated, clean_laps, summary, error = cur.fetchone()

    expected = analyzed["analysis"]
    assert error is None
    assert best_lap == expected.best_lap
    assert theo == pytest.approx(expected.theoretical_best_s)
    assert estimated == expected.speed_is_estimated
    assert clean_laps == expected.clean_lap_numbers
    assert summary["best_lap_s"] == pytest.approx(expected.summary["best_lap_s"])
    assert summary["n_laps"] == expected.summary["n_laps"]


def test_the_segment_map_survives_the_round_trip(analyzed):
    """`segments` is the reference line every sector time and the track map
    are cut against -- if its boundaries drift, every sector is wrong."""
    with analyzed["conn"].cursor() as cur:
        cur.execute("SELECT segments FROM session_analysis WHERE session_db_id=%s", (analyzed["id"],))
        stored = cur.fetchone()[0]

    expected = analyzed["analysis"].segments.to_dict(orient="records")
    assert len(stored) == len(expected)
    for got, want in zip(stored, expected):
        assert got["label"] == want["label"]
        assert got["kind"] == want["kind"]
        assert got["start_m"] == pytest.approx(want["start_m"])
        assert got["end_m"] == pytest.approx(want["end_m"])


def test_setup_suggestions_survive_despite_numpy_and_tuples(analyzed):
    """The setup engine returns numpy floats and a tuple for the peak-power
    band; psycopg2 adapts neither. This is the field most likely to break
    silently on a real session."""
    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "SELECT setup_suggestions FROM session_analysis WHERE session_db_id=%s",
            (analyzed["id"],),
        )
        stored = cur.fetchone()[0]

    expected = analyzed["analysis"].setup_suggestions
    assert len(stored) == len(expected)
    if expected:
        assert stored[0]["area"] == expected[0]["area"]
        assert stored[0]["hypothesis"] == expected[0]["hypothesis"]


# ------------------------------------------------------------- per-lap facts


def test_every_lap_has_a_trace_with_matching_samples(analyzed):
    from telemetry.analysis import analyze_lap

    lap_numbers = analyzed["analysis"].laps["lap_number"].tolist()
    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "SELECT lap_number, sample_count FROM lap_traces WHERE session_db_id=%s "
            "ORDER BY lap_number",
            (analyzed["id"],),
        )
        stored = cur.fetchall()

    assert [row[0] for row in stored] == [int(n) for n in lap_numbers]

    lap = analyze_lap(analyzed["session"], analyzed["analysis"], int(lap_numbers[0]))
    assert stored[0][1] == len(lap.metric_trace)


def test_a_traces_values_match_the_computed_trace(analyzed):
    """The arrays are what Lap Analysis plots. Checked element-wise on the
    best lap rather than by length, because a silently reordered or
    off-by-one array still has the right length."""
    from telemetry.analysis import analyze_lap

    best = analyzed["analysis"].best_lap
    lap = analyze_lap(analyzed["session"], analyzed["analysis"], best)
    trace = lap.metric_trace

    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "SELECT distance_m, lap_time_s, speed_kmh, latitude, longitude, braking, power_on "
            "FROM lap_traces WHERE session_db_id=%s AND lap_number=%s",
            (analyzed["id"], best),
        )
        distance, lap_time, speed, lat, lon, braking, power_on = cur.fetchone()

    assert distance == pytest.approx(trace["lap_distance_m"].tolist(), nan_ok=True)
    assert lap_time == pytest.approx(trace["lap_time_s"].tolist(), nan_ok=True)
    assert speed == pytest.approx(trace["GPS Speed"].tolist(), nan_ok=True)
    assert lat == pytest.approx(trace["Latitude"].tolist(), nan_ok=True)
    assert lon == pytest.approx(trace["Longitude"].tolist(), nan_ok=True)
    assert braking == [bool(v) for v in trace["braking_estimate"].fillna(False)]
    assert power_on == [bool(v) for v in trace["power_on_estimate"].fillna(False)]


def test_lap_time_is_stored_so_any_pair_of_laps_can_be_deltaed(analyzed):
    """Storing elapsed lap time per sample -- rather than a delta against
    one fixed reference -- is what lets the frontend change reference lap
    without new data. It must therefore be monotonic and start at ~0."""
    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "SELECT lap_time_s FROM lap_traces WHERE session_db_id=%s AND lap_number=%s",
            (analyzed["id"], analyzed["analysis"].best_lap),
        )
        lap_time = [v for v in cur.fetchone()[0] if v is not None]

    assert lap_time[0] == pytest.approx(0.0, abs=0.5)
    assert lap_time == sorted(lap_time), "lap time must increase along the lap"


def test_segment_times_match_per_lap(analyzed):
    from telemetry.analysis import analyze_lap

    best = analyzed["analysis"].best_lap
    expected = analyze_lap(analyzed["session"], analyzed["analysis"], best).segment_times

    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "SELECT segment_label, segment_kind, time_s FROM lap_segment_times "
            "WHERE session_db_id=%s AND lap_number=%s ORDER BY segment_index",
            (analyzed["id"], best),
        )
        stored = cur.fetchall()

    assert len(stored) == len(expected)
    for got, (_, want) in zip(stored, expected.iterrows()):
        assert got[0] == want["segment_label"]
        assert got[1] == want["segment_kind"]
        assert got[2] == pytest.approx(want["time_s"], nan_ok=True)


# ------------------------------------------------------------- re-analysis


def test_re_analyzing_replaces_rather_than_duplicates(analyzed):
    """A backfill has to be safe to re-run, and the worker has to be safe to
    re-process a batch. Segment rows are deleted and rewritten rather than
    upserted, because a re-analysis can produce a different number of
    segments and leftovers would read as real sectors."""
    from telemetry.analysis_store import store_session_analysis

    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM lap_segment_times WHERE session_db_id=%s", (analyzed["id"],)
        )
        before = cur.fetchone()[0]

    store_session_analysis(analyzed["id"], analyzed["session"], analyzed["analysis"])

    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM lap_segment_times WHERE session_db_id=%s", (analyzed["id"],)
        )
        assert cur.fetchone()[0] == before
        cur.execute("SELECT count(*) FROM session_analysis WHERE session_db_id=%s", (analyzed["id"],))
        assert cur.fetchone()[0] == 1


def test_the_stored_analysis_is_far_smaller_than_the_blob(analyzed):
    """The reason for doing any of this. Measured on the bundled export the
    derived rows come to a few percent of the Parquet they came from -- if
    that ever stops being true, clearing blobs stops being worthwhile and
    this should fail rather than quietly bloat the database."""
    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "SELECT octet_length(dataframe_parquet) FROM session_cache WHERE session_db_id=%s",
            (analyzed["id"],),
        )
        blob = cur.fetchone()[0]
        cur.execute(
            "SELECT sum(pg_column_size(t.*)) FROM lap_traces t WHERE session_db_id=%s",
            (analyzed["id"],),
        )
        traces = cur.fetchone()[0] or 0
        cur.execute(
            "SELECT sum(pg_column_size(s.*)) FROM lap_segment_times s WHERE session_db_id=%s",
            (analyzed["id"],),
        )
        segments = cur.fetchone()[0] or 0

    derived = traces + segments
    assert derived < blob * 0.5, (
        f"derived analysis is {derived / 1e6:.2f} MB against a {blob / 1e6:.2f} MB blob -- "
        "reclaiming the blob is no longer a clear win"
    )


def test_the_blob_can_be_cleared_once_analysis_is_stored(analyzed):
    """`dataframe_parquet` was NOT NULL until 0005; clearing it is the whole
    objective, so prove the column actually permits it."""
    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "UPDATE session_cache SET raw_storage_path='archive/session-cache/x.parquet', "
            "dataframe_parquet = NULL WHERE session_db_id=%s",
            (analyzed["id"],),
        )
        cur.execute(
            "SELECT dataframe_parquet, raw_storage_path FROM session_cache WHERE session_db_id=%s",
            (analyzed["id"],),
        )
        blob, path = cur.fetchone()

    assert blob is None
    assert path == "archive/session-cache/x.parquet"

    # And the analysis is still there afterwards -- that is what makes the
    # deletion safe to do at all.
    with analyzed["conn"].cursor() as cur:
        cur.execute("SELECT count(*) FROM lap_traces WHERE session_db_id=%s", (analyzed["id"],))
        assert cur.fetchone()[0] > 0


def test_per_lap_peaks_are_stored_for_the_summary_cards(analyzed):
    """Speed and RPM are derivable from the trace arrays, but only by
    shipping every lap's full trace to a browser to render two numbers."""
    from telemetry.analysis import analyze_lap

    best = analyzed["analysis"].best_lap
    trace = analyze_lap(analyzed["session"], analyzed["analysis"], best).metric_trace

    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "SELECT max_speed_kmh, max_rpm FROM lap_traces "
            "WHERE session_db_id=%s AND lap_number=%s",
            (analyzed["id"], best),
        )
        speed, rpm = cur.fetchone()

    assert speed == pytest.approx(float(trace["GPS Speed"].max()))
    assert rpm == pytest.approx(float(trace["RPM"].max()))

    # And they have to be present on every lap, not just the best one --
    # the cards take a max across the session.
    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM lap_traces WHERE session_db_id=%s AND max_speed_kmh IS NULL",
            (analyzed["id"],),
        )
        assert cur.fetchone()[0] == 0


def test_engine_figures_are_stored_per_lap(analyzed):
    """The engine page is a table of ten numbers per lap. Computing them in
    the browser would mean shipping every lap's whole trace to render it."""
    from telemetry.analysis import analyze_lap

    from telemetry.metrics import add_engine_temperature

    best = analyzed["analysis"].best_lap
    trace = add_engine_temperature(
        analyzed["session"],
        best,
        analyze_lap(analyzed["session"], analyzed["analysis"], best).metric_trace,
    )

    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "SELECT min_speed_kmh, min_rpm, avg_rpm, max_temp_c, min_temp_c, avg_temp_c "
            "FROM lap_traces WHERE session_db_id=%s AND lap_number=%s",
            (analyzed["id"], best),
        )
        min_speed, min_rpm, avg_rpm, max_temp, min_temp, avg_temp = cur.fetchone()

    assert min_speed == pytest.approx(float(trace["GPS Speed"].dropna().min()))
    assert min_rpm == pytest.approx(float(trace["RPM"].dropna().min()))
    assert avg_rpm == pytest.approx(float(trace["RPM"].dropna().mean()))
    assert max_temp == pytest.approx(float(trace["Temperature 1"].dropna().max()))
    assert min_temp == pytest.approx(float(trace["Temperature 1"].dropna().min()))
    assert avg_temp == pytest.approx(float(trace["Temperature 1"].dropna().mean()))
    # A real engine temperature, not the logger's own internal one, which
    # sits around 24-26C and would be a plausible-looking wrong answer.
    assert max_temp > 30


def test_powerzone_is_the_share_of_the_lap_in_the_rotax_band(analyzed):
    from telemetry.analysis import analyze_lap
    from telemetry.analysis_store import POWERZONE_RPM

    best = analyzed["analysis"].best_lap
    revs = analyze_lap(analyzed["session"], analyzed["analysis"], best).metric_trace["RPM"].dropna()
    expected = float(revs.between(*POWERZONE_RPM).sum()) / len(revs) * 100.0

    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "SELECT powerzone_pct FROM lap_traces WHERE session_db_id=%s AND lap_number=%s",
            (analyzed["id"], best),
        )
        stored = cur.fetchone()[0]

    assert stored == pytest.approx(expected)
    assert 0.0 <= stored <= 100.0


def test_the_g_traces_the_charts_plot_are_stored(analyzed):
    """This logger has no brake or throttle channel, so lateral and
    longitudinal G are the only read on cornering load and braking."""
    from telemetry.analysis import analyze_lap

    from telemetry.metrics import add_engine_temperature

    best = analyzed["analysis"].best_lap
    trace = add_engine_temperature(
        analyzed["session"],
        best,
        analyze_lap(analyzed["session"], analyzed["analysis"], best).metric_trace,
    )

    with analyzed["conn"].cursor() as cur:
        cur.execute(
            "SELECT lateral_g, longitudinal_g, temp_c FROM lap_traces "
            "WHERE session_db_id=%s AND lap_number=%s",
            (analyzed["id"], best),
        )
        lat, lon, temp = cur.fetchone()

    assert lat == pytest.approx(trace["GPS Lateral Acceleration"].tolist(), nan_ok=True)
    assert lon == pytest.approx(trace["GPS Longitudinal Acceleration"].tolist(), nan_ok=True)
    assert temp == pytest.approx(trace["Temperature 1"].tolist(), nan_ok=True)
