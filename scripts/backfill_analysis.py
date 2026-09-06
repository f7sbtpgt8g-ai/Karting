#!/usr/bin/env python
"""Analyze already-stored sessions, and optionally reclaim their blobs.

Sessions ingested before the analysis tables existed have their entire
parsed dataframe sitting in `session_cache.dataframe_parquet` -- 3-5 MB
each, ~46 MB per track day, against a 500 MB database -- and nothing
queryable for the frontend to draw. This reads each blob back, runs the
same `analyze_session` the worker runs, writes the result as rows, and can
then clear the blob.

Three phases, each opt-in, so nothing destructive happens by accident:

    python -m scripts.backfill_analysis                 # report only
    python -m scripts.backfill_analysis --analyze       # write analysis rows
    python -m scripts.backfill_analysis --analyze --archive --clear-blobs

`--clear-blobs` will not touch a session whose raw data it cannot account
for. Raw data is recoverable two ways:

  * the session came from an upload, so its original TSV is still in the
    `telemetry` Storage bucket (uploads are never deleted); or
  * `--archive` has copied its Parquet to Storage and recorded the path in
    `session_cache.raw_storage_path`.

A session with neither is skipped and reported, because clearing it would
make the analysis above the only copy -- and re-running it with a corrected
corner threshold (which telemetry/corners.py documents as likely) would
then be impossible.

Environment: SUPABASE_DB_URL, plus SUPABASE_URL and
SUPABASE_SERVICE_ROLE_KEY when using --archive.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import tempfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry import db as pgdb  # noqa: E402
from telemetry.analysis import analyze_session  # noqa: E402
from telemetry.analysis_store import (  # noqa: E402
    ANALYSIS_VERSION,
    has_stored_analysis,
    store_session_analysis,
)
from telemetry.parser import Session, load_sessions  # noqa: E402

logger = logging.getLogger("backfill")

ARCHIVE_PREFIX = "archive/session-cache"


def _candidates() -> list[dict]:
    """Every session, with wherever its raw telemetry can be found.

    Deliberately not "every session that still has a blob". Clearing blobs is
    the point of this script, and a blob-only query means that the moment it
    succeeds it can never analyse anything again -- which is exactly what
    happened when the peak-speed columns arrived after a clearing run: nothing
    left to iterate over, and no way to repair them.

    Raw telemetry survives in three places, checked in this order by
    `_load_dataframe`: the blob, the archived Parquet in Storage, and the
    original uploaded TSV, which is never deleted from the bucket.
    """
    with pgdb.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT s.id,
                      s.source_file,
                      s.session_index,
                      s.start_date,
                      s.start_time,
                      s.driver,
                      s.upload_batch_id,
                      c.raw_storage_path,
                      octet_length(c.dataframe_parquet) AS blob_bytes,
                      b.storage_path AS upload_storage_path
                 FROM sessions s
                 LEFT JOIN session_cache c  ON c.session_db_id = s.id
                 LEFT JOIN upload_batches b ON b.id = s.upload_batch_id
                ORDER BY s.id"""
        )
        # `pgdb.connect()` hands back a RealDictCursor, so rows are already
        # mappings -- zipping them against cur.description would pair column
        # names with column names.
        return [dict(row) for row in cur.fetchall()]


def _load_blob(session_db_id: int) -> bytes | None:
    """The stored Parquet, read back through a plain cursor.

    Not `pd.read_sql_query`: pandas' non-SQLAlchemy DBAPI2 path mangles a
    BYTEA column, and the RealDictCursor this project uses elsewhere makes
    it worse.
    """
    with pgdb.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT dataframe_parquet FROM session_cache WHERE session_db_id = %s",
            (session_db_id,),
        )
        row = cur.fetchone()
    blob = row["dataframe_parquet"] if row else None
    return bytes(blob) if blob is not None else None


def _load_dataframe(meta: dict, store=None, tsv_cache: dict | None = None) -> pd.DataFrame:
    """This session's raw dataframe, from whichever copy still exists.

    In order of cost: the BYTEA blob (free), the archived Parquet in Storage
    (one download), then the original uploaded TSV (a download and a full
    re-parse). The TSV is memoised across sessions because one export holds
    a whole track day -- re-parsing 80 MB once per session would turn a
    backfill into an afternoon.
    """
    session_db_id = meta["id"]

    blob = _load_blob(session_db_id)
    if blob is not None:
        return pd.read_parquet(io.BytesIO(blob))

    if meta.get("raw_storage_path"):
        if store is None:
            raise RuntimeError(
                f"session {session_db_id}'s dataframe is archived at "
                f"{meta['raw_storage_path']}, but Storage is not configured -- "
                "set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY"
            )
        return pd.read_parquet(io.BytesIO(store.download(meta["raw_storage_path"])))

    if meta.get("upload_storage_path"):
        if store is None:
            raise RuntimeError(
                f"session {session_db_id} can be re-parsed from its upload, but "
                "Storage is not configured -- set SUPABASE_URL and "
                "SUPABASE_SERVICE_ROLE_KEY"
            )
        cache = tsv_cache if tsv_cache is not None else {}
        path = meta["upload_storage_path"]
        if path not in cache:
            logger.info("re-parsing %s (holds several sessions; parsed once)", path)
            with tempfile.NamedTemporaryFile(suffix=".tsv", delete=False) as tmp:
                tmp.write(store.download(path))
                tmp_path = tmp.name
            try:
                cache[path] = {s.session_id: s.df for s in load_sessions(tmp_path)}
            finally:
                os.unlink(tmp_path)

        frame = cache[path].get(meta["session_index"])
        if frame is None:
            raise RuntimeError(
                f"session {session_db_id} (index {meta['session_index']}) is not in {path}"
            )
        return frame

    raise RuntimeError(
        f"session {session_db_id} has no raw telemetry left: no blob, no archived "
        "Parquet, and no upload in Storage"
    )


def _rebuild_session(meta: dict, frame: pd.DataFrame) -> Session:
    return Session(
        session_id=meta["session_index"],
        source_file=meta["source_file"],
        df=frame,
        start_date=meta["start_date"],
        start_time=meta["start_time"],
        driver=meta["driver"],
    )


def _archive(store, session_db_id: int, frame: pd.DataFrame) -> str:
    """Copy this session's Parquet to Storage and record where it went."""
    buffer = io.BytesIO()
    frame.to_parquet(buffer, index=False)
    path = f"{ARCHIVE_PREFIX}/{session_db_id}.parquet"
    store.upload(path, buffer.getvalue(), content_type="application/octet-stream")
    with pgdb.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE session_cache SET raw_storage_path = %s WHERE session_db_id = %s",
            (path, session_db_id),
        )
        conn.commit()
    return path


def _clear_blob(session_db_id: int) -> None:
    with pgdb.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE session_cache SET dataframe_parquet = NULL WHERE session_db_id = %s",
            (session_db_id,),
        )
        conn.commit()


def _table_sizes() -> dict[str, int]:
    with pgdb.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT pg_total_relation_size('session_cache') AS session_cache,
                      pg_database_size(current_database())    AS database"""
        )
        return dict(cur.fetchone())


def _vacuum_full() -> None:
    """Hand the space back to the filesystem.

    Clearing a blob only marks the row version dead; the file on disk stays
    the same size until the table is rewritten, so without this the whole
    exercise frees nothing measurable.

    Run here rather than in the Supabase SQL editor because that editor
    wraps statements in a transaction and VACUUM cannot run inside one
    ("ERROR: 25001: VACUUM cannot run inside a transaction block"). psycopg2
    opens a transaction on the first statement too, hence autocommit.

    Takes an ACCESS EXCLUSIVE lock and needs free disk roughly equal to the
    table's current size, so it belongs in a quiet moment rather than
    mid-upload.
    """
    with pgdb.connect() as conn:
        conn.autocommit = True
        cur = conn.cursor()
        # The multi-MB Parquet values live in this table's TOAST table, not
        # its heap; VACUUM FULL rewrites both, which is what actually
        # returns the megabytes.
        cur.execute("VACUUM (FULL, ANALYZE) session_cache")


def main(argv: list[str] | None = None, store=None) -> int:
    """`store` is injectable so the tests can drive --archive against a
    local directory instead of a Supabase project."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", action="store_true", help="write analysis rows")
    parser.add_argument("--archive", action="store_true", help="copy Parquet blobs to Storage")
    parser.add_argument(
        "--clear-blobs",
        action="store_true",
        help="NULL the blob for sessions whose raw data is recoverable (implies --analyze)",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="VACUUM FULL session_cache afterwards, returning freed space to the filesystem",
    )
    parser.add_argument("--limit", type=int, default=None, help="process at most N sessions")
    parser.add_argument("--session-id", type=int, default=None, help="just this one session")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if not pgdb.has_postgres_configured():
        print("SUPABASE_DB_URL is not set -- nothing to do.", file=sys.stderr)
        return 1

    # Storage is needed for more than --archive now: once blobs are cleared,
    # reading a session's raw telemetry back means fetching the archived
    # Parquet or the original upload. Built whenever it is configured, and
    # simply absent otherwise -- a run against a database with blobs still in
    # place needs no Storage at all.
    if store is None:
        try:
            from worker.storage_client import SupabaseStorage

            store = SupabaseStorage.from_env()
        except Exception as exc:  # noqa: BLE001
            if args.archive:
                raise
            logger.info("Storage not configured (%s) -- using stored blobs only", exc)

    rows = _candidates()
    if args.session_id is not None:
        rows = [r for r in rows if r["id"] == args.session_id]
    if args.limit:
        rows = rows[: args.limit]

    total_bytes = sum(r["blob_bytes"] or 0 for r in rows)
    with_blobs = sum(1 for r in rows if r["blob_bytes"])
    print(
        f"{len(rows)} session(s); {with_blobs} still holding a dataframe blob, "
        f"{total_bytes / 1e6:.1f} MB total\n"
    )

    analyzed = archived = cleared = skipped = failed = 0
    reclaimed = 0
    tsv_cache: dict = {}

    for meta in rows:
        session_db_id = meta["id"]
        recoverable = meta["upload_batch_id"] is not None or meta["raw_storage_path"] is not None

        if not (args.analyze or args.clear_blobs or args.archive):
            source = (
                "blob"
                if meta["blob_bytes"]
                else "archive"
                if meta["raw_storage_path"]
                else "upload"
                if meta["upload_storage_path"]
                else "NONE"
            )
            print(
                f"  session {session_db_id:<5} {(meta['blob_bytes'] or 0) / 1e6:5.2f} MB  "
                f"raw from: {source:<8} "
                f"analyzed at v{ANALYSIS_VERSION}: "
                f"{'yes' if has_stored_analysis(session_db_id) else 'no'}"
            )
            continue

        try:
            # Nothing to do, and re-reading the raw telemetry to discover that
            # would mean a download per session.
            if args.analyze and not (args.archive or args.clear_blobs):
                if has_stored_analysis(session_db_id):
                    logger.info("session %s already analyzed at v%s", session_db_id, ANALYSIS_VERSION)
                    continue

            frame = _load_dataframe(meta, store=store, tsv_cache=tsv_cache)
            session = _rebuild_session(meta, frame)

            if args.analyze or args.clear_blobs:
                if has_stored_analysis(session_db_id):
                    logger.info("session %s already analyzed at v%s", session_db_id, ANALYSIS_VERSION)
                else:
                    store_session_analysis(session_db_id, session, analyze_session(session))
                    analyzed += 1

            if args.archive and meta["raw_storage_path"] is None:
                path = _archive(store, session_db_id, frame)
                meta["raw_storage_path"] = path
                recoverable = True
                archived += 1

            if args.clear_blobs and meta["blob_bytes"]:
                if not recoverable:
                    logger.warning(
                        "session %s: no upload in Storage and no archived Parquet -- "
                        "not clearing (re-run with --archive first)",
                        session_db_id,
                    )
                    skipped += 1
                    continue
                _clear_blob(session_db_id)
                reclaimed += meta["blob_bytes"] or 0
                cleared += 1
        except Exception:  # noqa: BLE001
            logger.exception("session %s failed", session_db_id)
            failed += 1

    if args.analyze or args.clear_blobs or args.archive:
        print(
            f"\nanalyzed {analyzed} · archived {archived} · blobs cleared {cleared} "
            f"({reclaimed / 1e6:.1f} MB) · skipped {skipped} · failed {failed}"
        )
        if cleared and not args.vacuum:
            print("Re-run with --vacuum to return the freed space to the filesystem.")

    if args.vacuum:
        before = _table_sizes()
        print(
            f"\nvacuuming session_cache ({before['session_cache'] / 1e6:.1f} MB, "
            f"database {before['database'] / 1e6:.1f} MB)..."
        )
        _vacuum_full()
        after = _table_sizes()
        print(
            f"session_cache {before['session_cache'] / 1e6:.1f} -> "
            f"{after['session_cache'] / 1e6:.1f} MB · database "
            f"{before['database'] / 1e6:.1f} -> {after['database'] / 1e6:.1f} MB"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
