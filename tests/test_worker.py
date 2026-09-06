"""The ingest worker, end to end against a real Postgres.

Covers the loop that replaces Streamlit's file uploader: claim a pending
batch, fetch the raw file, parse it, persist every session in it, and mark
the batch complete -- plus the failure paths, which are the ones that decide
whether a stuck upload shows the user something useful or a spinner forever.

Storage is the `LocalDirectoryStore` rather than Supabase Storage, so this
runs with no network and no project; the seam is `worker.storage_client
.ObjectStore`, and `SupabaseStorage` is the only other implementation.

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

ADMIN_DSN = os.environ.get("RLS_TEST_ADMIN_DSN", "postgresql://postgres:postgres@localhost:5432/postgres")
WORKER_DB = os.environ.get("WORKER_TEST_DB", "worker_test")


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
def worker_db(monkeypatch):
    """A fresh database with every migration applied, wired up as the
    `SUPABASE_DB_URL` the worker's data layer reads from."""
    admin = psycopg2.connect(ADMIN_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {WORKER_DB}")
        cur.execute(f"CREATE DATABASE {WORKER_DB}")
    admin.close()

    dsn = ADMIN_DSN.rsplit("/", 1)[0] + "/" + WORKER_DB
    _psql(dsn, SIMULATION)
    for name in sorted(os.listdir(MIGRATIONS)):
        if name.endswith(".sql"):
            _psql(dsn, os.path.join(MIGRATIONS, name))

    monkeypatch.setenv("SUPABASE_DB_URL", dsn)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture
def uploader(worker_db):
    """A registered driver to own the uploads."""
    with worker_db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, external_auth_id, email_verified, display_name, created_at) "
            "VALUES ('driver@example.com','uid-1',TRUE,'Driver',now()) RETURNING id"
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO driver_profiles (display_name, user_id, claim_status, created_at, claimed_at) "
            "VALUES ('Driver',%s,'claimed',now(),now()) RETURNING id",
            (user_id,),
        )
        profile_id = cur.fetchone()[0]
    return {"user_id": user_id, "profile_id": profile_id}


def _enqueue(conn, uploader, storage_path="uid-1/upload.tsv", filename="default_session.tsv", **overrides):
    fields = {
        "storage_path": storage_path,
        "original_filename": filename,
        "uploaded_by_user_id": uploader["user_id"],
        "driver_profile_id": uploader["profile_id"],
        "track_name": "Test Track",
        "session_type": "practice",
        "visibility": "shared",
        **overrides,
    }
    cols = ", ".join(fields)
    marks = ", ".join(["%s"] * len(fields))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO upload_batches ({cols}) VALUES ({marks}) RETURNING id", tuple(fields.values())
        )
        return cur.fetchone()[0]


@pytest.fixture
def store(tmp_path):
    """A local stand-in for the Storage bucket, pre-loaded with the real
    bundled export so the worker parses genuine telemetry."""
    from worker.storage_client import LocalDirectoryStore

    folder = tmp_path / "uid-1"
    folder.mkdir()
    with open(SAMPLE_TSV, "rb") as src, open(folder / "upload.tsv", "wb") as dst:
        dst.write(src.read())
    return LocalDirectoryStore(str(tmp_path))


def _status(conn, batch_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, error_message, sessions_created FROM upload_batches WHERE id=%s", (batch_id,)
        )
        return cur.fetchone()


# ------------------------------------------------------------- happy path


def test_worker_parses_a_real_export_and_stores_every_session(worker_db, uploader, store):
    """The whole point: a file arrives, and afterwards its sessions are in
    the database with laps attached, attributed to the uploader."""
    from worker.main import run_once

    batch_id = _enqueue(worker_db, uploader)
    assert run_once(store) == 1

    status, error, created = _status(worker_db, batch_id)
    assert status == "complete", f"batch failed: {error}"
    # The bundled export really contains 11 sessions.
    assert created == 11

    with worker_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM sessions WHERE upload_batch_id=%s", (batch_id,))
        assert cur.fetchone()[0] == 11
        cur.execute(
            "SELECT count(*) FROM laps l JOIN sessions s ON s.id=l.session_db_id "
            "WHERE s.upload_batch_id=%s",
            (batch_id,),
        )
        assert cur.fetchone()[0] > 0, "sessions stored without any laps"
        # The raw dataframe must be persisted too, or nothing can re-analyze it.
        cur.execute(
            "SELECT count(*) FROM session_cache c JOIN sessions s ON s.id=c.session_db_id "
            "WHERE s.upload_batch_id=%s",
            (batch_id,),
        )
        assert cur.fetchone()[0] == 11


def test_upload_context_is_applied_to_every_session(worker_db, uploader, store):
    """Track name, conditions and sharing tier are collected once per upload
    and belong on every session the file produces."""
    from worker.main import run_once

    batch_id = _enqueue(
        worker_db, uploader, visibility="team", track_condition="dry", temperature_c=21.5
    )
    run_once(store)

    with worker_db.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT track_name, visibility, track_condition, temperature_c, "
            "driver_profile_id, uploaded_by_user_id FROM sessions WHERE upload_batch_id=%s",
            (batch_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1, "sessions from one upload disagreed about their context"
    assert rows[0] == ("Test Track", "team", "dry", 21.5, uploader["profile_id"], uploader["user_id"])


def test_queue_is_drained_in_order_and_left_empty(worker_db, uploader, store):
    from worker.main import run_once
    from worker.queue import claim_next_batch

    _enqueue(worker_db, uploader)
    _enqueue(worker_db, uploader, filename="second.tsv")
    assert run_once(store) == 2
    assert claim_next_batch() is None


# ---------------------------------------------------------- failure paths


def test_a_missing_object_fails_the_batch_with_a_usable_message(worker_db, uploader, store):
    """The uploader must be told to re-upload, not left on a spinner."""
    from worker.main import run_once

    batch_id = _enqueue(worker_db, uploader, storage_path="uid-1/does-not-exist.tsv")
    run_once(store)

    status, error, _ = _status(worker_db, batch_id)
    assert status == "failed"
    assert "upload it again" in error.lower()


def test_a_file_that_is_not_telemetry_fails_clearly(worker_db, uploader, tmp_path):
    """The most likely real user error -- the wrong export from Unipro
    Analyser -- should say so rather than surface a KeyError."""
    from worker.main import run_once
    from worker.storage_client import LocalDirectoryStore

    folder = tmp_path / "uid-1"
    folder.mkdir()
    (folder / "upload.tsv").write_text("this is not a unipro export\n")

    batch_id = _enqueue(worker_db, uploader)
    run_once(LocalDirectoryStore(str(tmp_path)))

    status, error, _ = _status(worker_db, batch_id)
    assert status == "failed"
    assert "unipro" in error.lower()


def test_one_bad_batch_does_not_stop_the_queue(worker_db, uploader, store):
    """A worker that dies on one malformed file stops serving everyone."""
    from worker.main import run_once

    bad = _enqueue(worker_db, uploader, storage_path="uid-1/missing.tsv")
    good = _enqueue(worker_db, uploader)

    assert run_once(store) == 2
    assert _status(worker_db, bad)[0] == "failed"
    assert _status(worker_db, good)[0] == "complete"


def test_reuploading_the_same_file_does_not_duplicate_sessions(worker_db, uploader, store):
    """Duplicate detection is the existing `find_session` identity match --
    re-uploading the same export must be a no-op, not 11 more sessions."""
    from worker.main import run_once

    _enqueue(worker_db, uploader)
    run_once(store)
    second = _enqueue(worker_db, uploader)
    run_once(store)

    assert _status(worker_db, second) == ("complete", None, 0)
    with worker_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM sessions")
        assert cur.fetchone()[0] == 11


# ------------------------------------------------------------ queue safety


def test_claiming_is_atomic(worker_db, uploader):
    """Two workers must never take the same batch -- otherwise an 80MB file
    is parsed twice concurrently and both write the same sessions."""
    from worker.queue import claim_next_batch

    _enqueue(worker_db, uploader)
    first = claim_next_batch()
    second = claim_next_batch()
    assert first is not None
    assert second is None, "a second worker claimed an already-claimed batch"


def test_a_batch_abandoned_mid_parse_is_requeued(worker_db, uploader):
    """A worker killed by a deploy leaves its row 'processing' forever, and
    nothing else would ever notice."""
    from worker.queue import claim_next_batch, requeue_stale_processing

    _enqueue(worker_db, uploader)
    claimed = claim_next_batch()
    assert claimed is not None

    assert requeue_stale_processing(older_than_minutes=30) == 0, "should not requeue a fresh claim"

    with worker_db.cursor() as cur:
        cur.execute(
            "UPDATE upload_batches SET claimed_at = now() - interval '2 hours' WHERE id=%s",
            (claimed.id,),
        )
    assert requeue_stale_processing(older_than_minutes=30) == 1
    assert claim_next_batch() is not None, "requeued batch should be claimable again"


def test_ingest_persists_the_analysis_for_every_session(worker_db, uploader, store):
    """Traces and sector times have to exist as rows by the time the upload
    reports complete -- the frontend cannot read them out of the Parquet
    blob, and the blob is meant to be reclaimable afterwards."""
    from worker.main import run_once

    batch_id = _enqueue(worker_db, uploader)
    assert run_once(store) == 1

    with worker_db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM session_analysis a JOIN sessions s ON s.id = a.session_db_id "
            "WHERE s.upload_batch_id=%s",
            (batch_id,),
        )
        assert cur.fetchone()[0] == 11, "not every session was analyzed at ingest"

        cur.execute(
            "SELECT count(*) FROM lap_traces t JOIN sessions s ON s.id = t.session_db_id "
            "WHERE s.upload_batch_id=%s",
            (batch_id,),
        )
        assert cur.fetchone()[0] > 0, "no lap traces stored"

        cur.execute(
            "SELECT count(*) FROM lap_segment_times l JOIN sessions s ON s.id = l.session_db_id "
            "WHERE s.upload_batch_id=%s",
            (batch_id,),
        )
        assert cur.fetchone()[0] > 0, "no per-lap segment times stored"

        # A session with no clean laps is a legitimate outcome, and gets a
        # row carrying its reason rather than no row at all -- otherwise
        # "not analyzed yet" and "nothing to analyze" look identical.
        cur.execute(
            "SELECT count(*) FROM session_analysis a JOIN sessions s ON s.id = a.session_db_id "
            "WHERE s.upload_batch_id=%s AND a.best_lap IS NOT NULL",
            (batch_id,),
        )
        assert cur.fetchone()[0] > 0


def test_a_session_that_cannot_be_analyzed_still_ingests(worker_db, uploader, store, monkeypatch):
    """Analysis is derived data a backfill can recompute. Failing the whole
    upload -- and telling the driver their file was bad -- because corner
    segmentation raised would be the wrong trade."""
    import worker.processor as processor
    from worker.main import run_once

    def boom(*args, **kwargs):
        raise RuntimeError("corner segmentation exploded")

    monkeypatch.setattr(processor, "analyze_session", boom)

    batch_id = _enqueue(worker_db, uploader)
    assert run_once(store) == 1

    status, error, created = _status(worker_db, batch_id)
    assert status == "complete", f"a failed analysis failed the whole batch: {error}"
    assert created == 11
    with worker_db.cursor() as cur:
        cur.execute("SELECT count(*) FROM laps")
        assert cur.fetchone()[0] > 0, "laps should still be stored"


# ----------------------------------------------------- compressed uploads
#
# Supabase Storage caps a single file at 50 MB on the free plan, which a full
# track day's export exceeds. The browser gzips before uploading, so the
# worker has to unwrap whatever arrives -- and has to do it by looking at the
# bytes, since the name is not reliable.


def _write(store_root, name: str, data: bytes) -> None:
    import os

    folder = os.path.join(str(store_root), "uid-1")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "wb") as handle:
        handle.write(data)


def test_a_gzipped_export_parses_exactly_like_the_plain_one(worker_db, uploader, tmp_path):
    """The whole point: the same 11 sessions come out."""
    import gzip

    from worker.main import run_once
    from worker.storage_client import LocalDirectoryStore

    with open(SAMPLE_TSV, "rb") as handle:
        raw = handle.read()
    _write(tmp_path, "upload.tsv.gz", gzip.compress(raw))

    batch_id = _enqueue(
        worker_db, uploader, storage_path="uid-1/upload.tsv.gz", filename="default_session.tsv.gz"
    )
    assert run_once(LocalDirectoryStore(str(tmp_path))) == 1

    status, error, created = _status(worker_db, batch_id)
    assert status == "complete", f"a gzipped upload failed: {error}"
    assert created == 11


def test_a_zipped_export_parses_too(worker_db, uploader, tmp_path):
    """For anyone who compresses by hand rather than letting the browser."""
    import io
    import zipfile

    from worker.main import run_once
    from worker.storage_client import LocalDirectoryStore

    with open(SAMPLE_TSV, "rb") as handle:
        raw = handle.read()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("default_session.tsv", raw)
    _write(tmp_path, "upload.zip", buffer.getvalue())

    batch_id = _enqueue(
        worker_db, uploader, storage_path="uid-1/upload.zip", filename="export.zip"
    )
    assert run_once(LocalDirectoryStore(str(tmp_path))) == 1
    assert _status(worker_db, batch_id)[0] == "complete"


def test_compression_is_detected_by_content_not_by_name(worker_db, uploader, tmp_path):
    """A driver who renames the file, or a browser that compressed without
    renaming, must still work -- and a mislabelled file should fail on its
    contents rather than its extension."""
    import gzip

    from worker.main import run_once
    from worker.storage_client import LocalDirectoryStore

    with open(SAMPLE_TSV, "rb") as handle:
        raw = handle.read()
    # Gzipped bytes under a plain .tsv name.
    _write(tmp_path, "upload.tsv", gzip.compress(raw))

    batch_id = _enqueue(worker_db, uploader)
    assert run_once(LocalDirectoryStore(str(tmp_path))) == 1
    assert _status(worker_db, batch_id)[0] == "complete"


def test_a_zip_holding_several_exports_says_so(worker_db, uploader, tmp_path):
    """Silently picking one of them would file a track day under the wrong
    session and look like it worked."""
    import io
    import zipfile

    from worker.main import run_once
    from worker.storage_client import LocalDirectoryStore

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("saturday.tsv", b"a")
        archive.writestr("sunday.tsv", b"b")
    _write(tmp_path, "upload.zip", buffer.getvalue())

    batch_id = _enqueue(worker_db, uploader, storage_path="uid-1/upload.zip")
    run_once(LocalDirectoryStore(str(tmp_path)))

    status, error, _ = _status(worker_db, batch_id)
    assert status == "failed"
    assert "one at a time" in error.lower()


def test_a_corrupt_archive_fails_with_a_usable_message(worker_db, uploader, tmp_path):
    from worker.main import run_once
    from worker.storage_client import LocalDirectoryStore

    # Gzip magic bytes, then rubbish.
    _write(tmp_path, "upload.tsv", b"\x1f\x8b" + b"not actually gzip")

    batch_id = _enqueue(worker_db, uploader)
    run_once(LocalDirectoryStore(str(tmp_path)))

    status, error, _ = _status(worker_db, batch_id)
    assert status == "failed"
    assert "gzip" in error.lower()
