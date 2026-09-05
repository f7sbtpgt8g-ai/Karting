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

from telemetry.parser import load_sessions
from telemetry.storage import SessionLibrary

from .core.sync_engine import SyncResult

logger = logging.getLogger("unigo_sync.ingest_bridge")


def ingest_new_sessions(
    result: SyncResult,
    db_path: str,
    driver: str | None = None,
    track: str | None = None,
    session_type: str | None = None,
) -> int:
    """Load every newly-synced TSV from this sync pass into the session
    library. Returns how many were ingested. A parse/save failure on one
    file is logged and skipped, same as `scripts/ingest.py`'s own
    behaviour, rather than aborting the rest.
    """
    if not result.new_synced:
        return 0

    library = SessionLibrary(db_path)
    ingested = 0
    try:
        for name in result.new_synced:
            path = result.paths.get(name)
            if path is None:
                continue
            try:
                sessions = load_sessions(path)
            except Exception as exc:  # noqa: BLE001 - report and continue, matching scripts/ingest.py
                logger.warning("failed to parse converted TSV %s: %s", path, exc)
                continue
            for session in sessions:
                db_id = library.save_session(session, driver=driver, track_name=track, session_type=session_type)
                logger.info("ingested %s session %s -> library id %s", path, session.session_id, db_id)
                ingested += 1
    finally:
        library.close()
    return ingested
