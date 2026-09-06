"""Turning one claimed `upload_batches` row into stored sessions.

The actual parsing and analysis is `telemetry.parser` / `telemetry.storage`
called unchanged -- this module is the glue that was previously spread
through `app.py`'s upload page: write the bytes somewhere the parser can
read, load every session in the file, save each one with the batch's
context attached, and attribute it.
"""

from __future__ import annotations

import gzip
import io
import logging
import os
import tempfile
import zipfile

from telemetry.accounts import ATTRIBUTION_CONFIRMED, account_library_from_env
from telemetry.analysis_store import analyze_and_store
from telemetry.parser import load_sessions
from telemetry.storage import session_library_from_env

from .queue import UploadBatch
from .storage_client import ObjectNotFound, ObjectStore

logger = logging.getLogger("worker.processor")


class BatchFailed(RuntimeError):
    """A batch that cannot succeed however many times it is retried. The
    message is shown to the uploader, so it says what they can do."""


# Magic bytes, not the filename. A driver who renames "export.tsv.gz" to
# "export.tsv" -- or whose browser did the compressing without renaming
# anything -- still gets the right answer, and a mislabelled file fails on
# its contents rather than on its extension.
_GZIP_MAGIC = b"\x1f\x8b"
_ZIP_MAGIC = b"PK\x03\x04"


def decompress_upload(raw: bytes) -> bytes:
    """The telemetry inside an upload, whatever it arrived wrapped in.

    Supabase Storage caps a single file at 50 MB on the free plan, and a
    full track day's export is comfortably past that. A Unipro TSV is highly
    compressible text -- about 6x, measured on the bundled 82 MB export -- so
    the browser gzips before uploading and this unwraps it. Files compressed
    by hand (.gz or .zip) work the same way.
    """
    if raw[:2] == _GZIP_MAGIC:
        try:
            return gzip.decompress(raw)
        except OSError as exc:
            raise BatchFailed(f"This file looks gzipped but could not be read ({exc}).") from exc

    if raw[:4] == _ZIP_MAGIC:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                names = [n for n in archive.namelist() if not n.endswith("/")]
                # A zip made by right-clicking a folder carries macOS resource
                # forks and similar; pick the telemetry rather than the first
                # entry, which may well be one of those.
                telemetry = [n for n in names if n.lower().endswith((".tsv", ".txt"))]
                chosen = (telemetry or names)
                if not chosen:
                    raise BatchFailed("That zip file is empty.")
                if len(telemetry) > 1:
                    raise BatchFailed(
                        f"That zip contains {len(telemetry)} telemetry files "
                        f"({', '.join(sorted(telemetry)[:3])}...). Please upload one at a time."
                    )
                return archive.read(chosen[0])
        except zipfile.BadZipFile as exc:
            raise BatchFailed(f"This file looks like a zip but could not be read ({exc}).") from exc

    return raw


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

    raw = decompress_upload(raw)
    if not raw:
        raise BatchFailed("That archive unpacked to an empty file.")

    # `load_sessions` takes a path, not bytes -- the parser streams a file
    # that can be ~80MB, so handing it a path keeps it out of memory twice.
    # Named for what it now contains: the upload may have been "x.tsv.gz",
    # and the parser is handed plain text either way.
    name = (batch.original_filename or "upload.tsv").removesuffix(".gz").removesuffix(".zip")
    suffix = os.path.splitext(name)[1] or ".tsv"
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
            analyze_and_store(session_db_id, session)

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
