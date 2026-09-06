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

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry import db as pgdb  # noqa: E402
from telemetry.analysis import analyze_session  # noqa: E402
from telemetry.analysis_store import (  # noqa: E402
    ANALYSIS_VERSION,
    has_stored_analysis,
    store_session_analysis,
)
from telemetry.parser import Session  # noqa: E402

logger = logging.getLogger("backfill")

ARCHIVE_PREFIX = "archive/session-cache"


def _candidates() -> list[dict]:
    """Every session with a blob, and whether its raw data is recoverable
    without it."""
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
                      octet_length(c.dataframe_parquet) AS blob_bytes
                 FROM sessions s
                 JOIN session_cache c ON c.session_db_id = s.id
                WHERE c.dataframe_parquet IS NOT NULL
                ORDER BY s.id"""
        )
        # `pgdb.connect()` hands back a RealDictCursor, so rows are already
        # mappings -- zipping them against cur.description would pair column
        # names with column names.
        return [dict(row) for row in cur.fetchall()]


def _load_dataframe(session_db_id: int) -> pd.DataFrame:
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
    if blob is None:
        raise RuntimeError(f"session {session_db_id} has no stored dataframe")
    return pd.read_parquet(io.BytesIO(bytes(blob)))


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
    parser.add_argument("--limit", type=int, default=None, help="process at most N sessions")
    parser.add_argument("--session-id", type=int, default=None, help="just this one session")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if not pgdb.has_postgres_configured():
        print("SUPABASE_DB_URL is not set -- nothing to do.", file=sys.stderr)
        return 1

    if args.archive and store is None:
        from worker.storage_client import SupabaseStorage

        store = SupabaseStorage.from_env()

    rows = _candidates()
    if args.session_id is not None:
        rows = [r for r in rows if r["id"] == args.session_id]
    if args.limit:
        rows = rows[: args.limit]

    total_bytes = sum(r["blob_bytes"] or 0 for r in rows)
    print(f"{len(rows)} session(s) with a stored dataframe, {total_bytes / 1e6:.1f} MB total\n")

    analyzed = archived = cleared = skipped = failed = 0
    reclaimed = 0

    for meta in rows:
        session_db_id = meta["id"]
        recoverable = meta["upload_batch_id"] is not None or meta["raw_storage_path"] is not None

        if not (args.analyze or args.clear_blobs or args.archive):
            print(
                f"  session {session_db_id:<5} {(meta['blob_bytes'] or 0) / 1e6:5.2f} MB  "
                f"raw recoverable: {'yes' if recoverable else 'NO'}  "
                f"analyzed: {'yes' if has_stored_analysis(session_db_id) else 'no'}"
            )
            continue

        try:
            frame = _load_dataframe(session_db_id)
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

            if args.clear_blobs:
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
        if cleared:
            print("Run VACUUM FULL session_cache to return the space to the filesystem.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
