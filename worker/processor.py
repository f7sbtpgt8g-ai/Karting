"""Turning one claimed `upload_batches` row into stored sessions.

The actual parsing and analysis is `telemetry.parser` / `telemetry.storage`
called unchanged -- this module is the glue that was previously spread
through `app.py`'s upload page: write the bytes somewhere the parser can
read, load every session in the file, save each one with the batch's
context attached, and attribute it.
"""

from __future__ import annotations

import logging
import os
import tempfile

from telemetry.accounts import ATTRIBUTION_CONFIRMED, account_library_from_env
from telemetry.analysis import analyze_session
from telemetry.analysis_store import store_session_analysis
from telemetry.parser import load_sessions
from telemetry.storage import session_library_from_env

from .queue import UploadBatch
from .storage_client import ObjectNotFound, ObjectStore

logger = logging.getLogger("worker.processor")


class BatchFailed(RuntimeError):
    """A batch that cannot succeed however many times it is retried. The
    message is shown to the uploader, so it says what they can do."""


def process_batch(batch: UploadBatch, store: ObjectStore) -> int:
    """Parse one uploaded file and persist every session in it.

    Returns the number of sessions created. Raises `BatchFailed` with a
    user-facing message for anything terminal.
    """
    try:
        raw = store.download(batch.storage_path)
    except ObjectNotFound as exc:
        raise BatchFailed(
            "The uploaded file could not be found in storage. Please upload it again."
        ) from exc

    if not raw:
        raise BatchFailed("The uploaded file was empty.")

    # `load_sessions` takes a path, not bytes -- the parser streams a file
    # that can be ~80MB, so handing it a path keeps it out of memory twice.
    suffix = os.path.splitext(batch.original_filename or "upload.tsv")[1] or ".tsv"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    try:
        try:
            sessions = load_sessions(tmp_path)
        except Exception as exc:
            # The parser fails loudly on a file that isn't a Unipro export
            # (a missing expected column, usually). That is the single most
            # likely user error here, so say so rather than surfacing a
            # KeyError.
            raise BatchFailed(
                f"This file could not be read as a Unipro TSV export ({exc}). "
                "Check it is the full data log rather than a summary or lap-times-only export."
            ) from exc

        if not sessions:
            raise BatchFailed("No sessions were detected in that file.")

        library = session_library_from_env(_sqlite_fallback_path())
        accounts = account_library_from_env(_sqlite_fallback_path())

        driver_name = _driver_display_name(accounts, batch)
        created = 0
        for session in sessions:
            # Keep the uploader's original filename as the session's
            # identity, not the storage key -- duplicate detection
            # (`find_session`) matches on it, and a UUID storage path would
            # make every re-upload look new.
            session.source_file = batch.original_filename or session.source_file
            if library.find_session(session.source_file, session.session_id, session.start_time, driver_name) is not None:
                logger.info("batch %s: session %s already ingested, skipping", batch.id, session.session_id)
                continue

            session.driver = driver_name
            session_db_id = library.save_session(
                session,
                driver=driver_name,
                track_name=batch.track_name,
                session_type=batch.session_type,
                driver_profile_id=batch.driver_profile_id,
                uploaded_by_user_id=batch.uploaded_by_user_id,
                visibility=batch.visibility,
                **batch.conditions,
            )
            _link_to_batch(session_db_id, batch.id)
            _store_analysis(session_db_id, session)

            if batch.driver_profile_id is not None:
                accounts.attribute_session(
                    session_db_id,
                    batch.driver_profile_id,
                    uploaded_by_user_id=batch.uploaded_by_user_id,
                    requires_confirmation=False,
                )
            created += 1
        return created
    finally:
        os.unlink(tmp_path)


def _driver_display_name(accounts, batch: UploadBatch) -> str | None:
    if batch.driver_profile_id is None:
        return None
    profile = accounts.get_profile(batch.driver_profile_id)
    return profile["display_name"] if profile else None


def _store_analysis(session_db_id: int, session) -> None:
    """Run the analysis and persist it as rows, so the frontend can query
    traces and sector times without reading the Parquet blob.

    Best-effort, and deliberately so: the session, its laps and its raw
    dataframe are already saved by this point, and analysis is derived data
    that a backfill can recompute at any time. Failing the whole batch --
    and telling the uploader their file didn't work -- because one session's
    corner segmentation raised would be the wrong trade.
    """
    try:
        analysis = analyze_session(session)
        store_session_analysis(session_db_id, session, analysis)
    except Exception:  # noqa: BLE001
        logger.warning("could not store analysis for session %s", session_db_id, exc_info=True)


def _link_to_batch(session_db_id: int, batch_id: int) -> None:
    """Record which upload produced this session. Best-effort: a database
    that predates 0003 has no such column, and failing an otherwise
    successful ingest over a traceability field would be the wrong trade."""
    from telemetry import db as pgdb

    if not pgdb.has_postgres_configured():
        return
    try:
        with pgdb.connect() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE sessions SET upload_batch_id=%s WHERE id=%s", (batch_id, session_db_id))
            conn.commit()
    except Exception:  # noqa: BLE001
        logger.warning("could not link session %s to batch %s", session_db_id, batch_id, exc_info=True)


def _sqlite_fallback_path() -> str:
    """Only ever used if the worker is run with no Postgres configured --
    which is a misconfiguration for a deployed worker, but keeps the local
    development story identical to the rest of the repo."""
    return os.environ.get("KARTING_SQLITE_DB", "data/sessions.db")
