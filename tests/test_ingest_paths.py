"""Every way a session can get into the database, analysed the same way.

There are three ingestion paths -- the worker behind a web upload, the
unigo_sync bridge, and `scripts/ingest.py` -- and for a while only the first
of them stored a `session_analysis` row. Sessions synced from the logger
therefore arrived complete and unanalysed, and Lap Analysis met their owner
with "this session is stored, but its analysis has not been computed", which
reads as a bug in the page rather than a gap in the path that ingested them.

Nothing about that was visible from either end: the sessions listed on Home
normally, and the worker's own tests passed throughout. So the invariant is
tested here directly, per path, against a real Postgres -- because "did we
remember to call it here too" is exactly what nothing else checks.

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
TEST_DB = os.environ.get("INGEST_PATHS_TEST_DB", "ingest_paths_test")


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


@pytest.fixture
def db(monkeypatch):
    """A fresh database with every migration applied, wired up as the
    `SUPABASE_DB_URL` that `session_library_from_env` and the analysis
    writer both read."""
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

    monkeypatch.setenv("SUPABASE_DB_URL", dsn)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    yield conn
    conn.close()


def _counts(conn) -> tuple[int, int, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM sessions), "
            "       (SELECT count(*) FROM session_analysis), "
            "       (SELECT count(*) FROM lap_traces)"
        )
        return cur.fetchone()


def _sync_one(_db) -> None:
    """The unigo_sync bridge: a TSV the sync tool has just converted, handed
    straight to the session library."""
    from telemetry.storage import session_library_from_env
    from unigo_sync.ingest_bridge import ingest_one

    library = session_library_from_env("unused-sqlite-path")
    try:
        ingest_one(library, SAMPLE_TSV, driver="Driver", track="Test Track")
    finally:
        library.close()


def _cli_one(_db) -> None:
    """`scripts/ingest.py`, the standalone path."""
    from scripts.ingest import main

    assert main([SAMPLE_TSV, "--driver", "Driver", "--track", "Test Track"]) == 0


@pytest.mark.parametrize("ingest", [_sync_one, _cli_one], ids=["unigo_sync", "scripts.ingest"])
def test_every_ingestion_path_stores_the_analysis(db, ingest):
    """A session is equally usable however it arrived: the frontend reads
    `session_analysis` and `lap_traces`, never the Parquet blob, so a path
    that saves a session without them produces one no page can open."""
    ingest(db)
    sessions, analyses, traces = _counts(db)

    assert sessions > 0, "nothing was ingested at all"
    assert analyses == sessions, (
        f"{sessions} session(s) ingested but only {analyses} analysed -- "
        "this path saves sessions the frontend cannot open"
    )
    assert traces > 0, "no lap traces stored, so no chart or track map can be drawn"


@pytest.mark.parametrize("ingest", [_sync_one, _cli_one], ids=["unigo_sync", "scripts.ingest"])
def test_a_session_that_cannot_be_analyzed_still_ingests(db, ingest, monkeypatch):
    """The same trade the worker makes. Analysis is derived data a backfill
    can recompute; the session, its laps and its dataframe are already saved
    by the time it runs. Raising here would lose the ingest -- and in the
    sync tool would put an already-stored session back on the retry queue,
    to be ingested a second time."""
    import telemetry.analysis_store as analysis_store

    def boom(*args, **kwargs):
        raise RuntimeError("corner segmentation exploded")

    monkeypatch.setattr(analysis_store, "analyze_session", boom)

    ingest(db)
    sessions, analyses, _ = _counts(db)
    assert sessions > 0, "a failed analysis lost the session itself"
    assert analyses == 0

    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM laps")
        assert cur.fetchone()[0] > 0, "laps should still be stored"
