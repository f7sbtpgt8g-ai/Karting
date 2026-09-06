"""The backfill, and specifically its refusal to delete what it cannot replace.

`--clear-blobs` is the only destructive operation in this repo. It exists to
return ~46 MB per track day to a 500 MB database, which is worth doing --
but the blob it clears is, for older sessions, the only copy of the raw
telemetry. So the property actually worth testing is not that it frees
space; it is that it declines to free space when doing so would lose data.

Requires a local Postgres, same as tests/test_rls_policies.py.
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
TEST_DB = os.environ.get("BACKFILL_TEST_DB", "backfill_test")


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
def seeded(monkeypatch):
    """Two real sessions stored the pre-0005 way: blobs, no analysis. One
    came from an upload (so its TSV is still in Storage), one did not."""
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

    from telemetry.parser import load_sessions
    from telemetry.storage import session_library_from_env

    library = session_library_from_env(os.path.join(REPO, "data", "unused.db"))
    sessions = load_sessions(SAMPLE_TSV)[:2]
    ids = [library.save_session(s, driver="Tester", track_name="Ring") for s in sessions]

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        # The first session arrived through the upload pipeline, so its
        # original TSV is still in the bucket and it is safe to clear.
        cur.execute(
            "INSERT INTO upload_batches (storage_path, uploaded_by_user_id, status) "
            "VALUES ('uid/x.tsv', NULL, 'complete') RETURNING id"
        )
        batch_id = cur.fetchone()[0]
        cur.execute("UPDATE sessions SET upload_batch_id=%s WHERE id=%s", (batch_id, ids[0]))

    yield {"conn": conn, "uploaded": ids[0], "orphan": ids[1]}
    conn.close()


def _blobs(conn) -> dict[int, int | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT session_db_id, octet_length(dataframe_parquet) FROM session_cache "
            "ORDER BY session_db_id"
        )
        return dict(cur.fetchall())


def test_analyze_writes_rows_without_touching_the_blobs(seeded):
    from scripts.backfill_analysis import main

    before = _blobs(seeded["conn"])
    assert main(["--analyze"]) == 0

    with seeded["conn"].cursor() as cur:
        cur.execute("SELECT count(*) FROM session_analysis")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM lap_traces")
        assert cur.fetchone()[0] > 0
    assert _blobs(seeded["conn"]) == before, "--analyze alone must not clear anything"


def test_clear_blobs_refuses_a_session_it_cannot_recover(seeded):
    """The session with no upload behind it: its blob is the only copy of
    the raw telemetry, so clearing it would make re-analysis impossible."""
    from scripts.backfill_analysis import main

    assert main(["--analyze", "--clear-blobs"]) == 0

    blobs = _blobs(seeded["conn"])
    assert blobs[seeded["uploaded"]] is None, "a recoverable session's blob should be cleared"
    assert blobs[seeded["orphan"]] is not None, (
        "cleared a blob that was the only copy of the raw data"
    )


def test_archive_makes_an_orphan_session_clearable(seeded, tmp_path):
    """...and once its Parquet is in Storage, it is safe to clear."""
    from scripts.backfill_analysis import main
    from worker.storage_client import LocalDirectoryStore

    store = LocalDirectoryStore(str(tmp_path))
    assert main(["--analyze", "--archive", "--clear-blobs"], store=store) == 0

    blobs = _blobs(seeded["conn"])
    assert blobs[seeded["orphan"]] is None, "an archived session should now be clearable"

    with seeded["conn"].cursor() as cur:
        cur.execute(
            "SELECT raw_storage_path FROM session_cache WHERE session_db_id=%s",
            (seeded["orphan"],),
        )
        path = cur.fetchone()[0]
    assert path and path.endswith(f"{seeded['orphan']}.parquet")
    assert os.path.exists(os.path.join(str(tmp_path), path)), "archive recorded but not written"


def test_the_archived_parquet_still_reanalyzes(seeded, tmp_path):
    """The archive is only worth anything if it round-trips -- otherwise
    'recoverable' is a claim rather than a fact."""
    import io

    import pandas as pd

    from scripts.backfill_analysis import main
    from telemetry.analysis import analyze_session
    from telemetry.parser import Session
    from worker.storage_client import LocalDirectoryStore

    store = LocalDirectoryStore(str(tmp_path))
    main(["--analyze", "--archive", "--clear-blobs"], store=store)

    with seeded["conn"].cursor() as cur:
        cur.execute(
            "SELECT c.raw_storage_path, s.source_file, s.session_index, s.start_date, "
            "s.start_time, s.driver FROM session_cache c JOIN sessions s ON s.id=c.session_db_id "
            "WHERE c.session_db_id=%s",
            (seeded["orphan"],),
        )
        path, source_file, index, start_date, start_time, driver = cur.fetchone()

    frame = pd.read_parquet(io.BytesIO(store.download(path)))
    restored = Session(
        session_id=index,
        source_file=source_file,
        df=frame,
        start_date=start_date,
        start_time=start_time,
        driver=driver,
    )
    analysis = analyze_session(restored)
    assert analysis.ok, analysis.data_error
    assert analysis.best_lap is not None


def test_the_backfill_is_safe_to_rerun(seeded):
    from scripts.backfill_analysis import main

    main(["--analyze"])
    main(["--analyze"])

    with seeded["conn"].cursor() as cur:
        cur.execute("SELECT count(*) FROM session_analysis")
        assert cur.fetchone()[0] == 2


def test_vacuum_actually_returns_the_space(seeded):
    """Clearing a blob only marks the row version dead -- the file on disk
    stays the same size until the table is rewritten. Without this step the
    whole exercise frees nothing you can measure, which is the failure mode
    most likely to go unnoticed.

    Also a regression test for running VACUUM at all: psycopg2 opens a
    transaction on the first statement, and VACUUM cannot run inside one
    (the same reason it fails in the Supabase SQL editor).
    """
    from scripts.backfill_analysis import _table_sizes, main
    from worker.storage_client import LocalDirectoryStore
    import tempfile

    with tempfile.TemporaryDirectory() as archive:
        main(["--analyze", "--archive", "--clear-blobs"], store=LocalDirectoryStore(archive))
        before = _table_sizes()["session_cache"]
        assert main(["--vacuum"]) == 0
        after = _table_sizes()["session_cache"]

    assert after < before, (
        f"session_cache stayed at {after / 1e6:.1f} MB after vacuuming -- "
        "clearing blobs freed nothing on disk"
    )
