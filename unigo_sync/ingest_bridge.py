"""Bridges unigo_sync's output into this repo's existing analysis
pipeline. Not part of the portable core (`core/`) -- this module is
allowed to depend on the rest of the repo, since its whole job is gluing
`unigo_sync`'s output onto the analysis side; `core/` stays repo- and
OS-agnostic so it can be reused as-is (or ported) elsewhere.

Note on the integration point: as flagged in ../README.md, this repo has
no watched-folder auto-ingest daemon -- ingestion is either the
Streamlit app's file uploader or `scripts/ingest.py`'s explicit
file-path arguments. Rather than adding a folder-watcher poller as a
third ingestion path, this bridge calls the same loading/saving code
`scripts/ingest.py` uses, directly, right after a session is written --
so a manual "sync now" (or the optional background watcher) can hand
freshly-converted TSVs straight to the session library without a
human running `scripts/ingest.py` by hand afterwards.
"""

from __future__ import annotations

import logging

from telemetry.analysis_store import analyze_and_store
from telemetry.parser import load_sessions
from telemetry.storage import SessionLibrary, SupabaseSessionLibrary, session_library_from_env

from .core.sync_engine import SyncResult

logger = logging.getLogger("unigo_sync.ingest_bridge")


def ingest_one(
    library: SessionLibrary | SupabaseSessionLibrary,
    path: str,
    driver: str | None = None,
    track: str | None = None,
    session_type: str | None = None,
    driver_profile_id: int | None = None,
    uploaded_by_user_id: int | None = None,
) -> int:
    """Load one already-converted TSV and save each session in it into
    `library`. Returns how many sessions were ingested (a TSV can contain
    more than one, per `telemetry.parser.load_sessions`). Raises on a
    parse/save failure -- unlike `ingest_new_sessions`'s loop, a single
    call here doesn't know whether the caller wants failures skipped
    (a live sync pass) or retried later (the pending-upload queue,
    `core.pending_uploads`), so that decision is left to the caller.
    """
    sessions = load_sessions(path)
    ingested = 0
    for session in sessions:
        db_id = library.save_session(
            session, driver=driver, track_name=track, session_type=session_type,
            driver_profile_id=driver_profile_id, uploaded_by_user_id=uploaded_by_user_id,
        )
        # Same step the worker runs behind a web upload. Without it a synced
        # session is stored but has no `session_analysis` row, and every page
        # built on one -- Lap Analysis, Engine Analysis -- tells its owner to
        # go and run a backfill script. Best-effort inside, so a session that
        # saved but would not analyse is not reported as a failed ingest and
        # retried into a duplicate.
        analyze_and_store(db_id, session)
        logger.info("ingested %s session %s -> library id %s", path, session.session_id, db_id)
        ingested += 1
    return ingested


def ingest_new_sessions(
    result: SyncResult,
    db_path: str,
    driver: str | None = None,
    track: str | None = None,
    session_type: str | None = None,
    driver_profile_id: int | None = None,
    uploaded_by_user_id: int | None = None,
) -> int:
    """Load every newly-synced TSV from this sync pass into the session
    library. Returns how many were ingested. A parse/save failure on one
    file is logged and skipped, same as `scripts/ingest.py`'s own
    behaviour, rather than aborting the rest.
    """
    if not result.new_synced:
        return 0

    # Postgres/Supabase-backed when SUPABASE_DB_URL/DATABASE_URL is
    # configured, the local SQLite file at `db_path` otherwise -- same
    # choice `scripts/ingest.py` makes, so a sync pass lands sessions in
    # the same database the Streamlit app and every other ingestion path
    # read from, rather than a local-only file that silently diverges
    # from a deployed Supabase project.
    library = session_library_from_env(db_path)
    ingested = 0
    try:
        for name in result.new_synced:
            path = result.paths.get(name)
            if path is None:
                continue
            try:
                ingested += ingest_one(
                    library, path, driver=driver, track=track, session_type=session_type,
                    driver_profile_id=driver_profile_id, uploaded_by_user_id=uploaded_by_user_id,
                )
            except Exception as exc:  # noqa: BLE001 - report and continue, matching scripts/ingest.py
                logger.warning("failed to ingest converted TSV %s: %s", path, exc)
    finally:
        library.close()
    return ingested
