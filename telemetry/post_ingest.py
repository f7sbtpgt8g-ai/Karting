"""What happens to a session after it has been saved.

There are three ways a session reaches the database -- the upload worker,
the unigo_sync bridge, and `scripts/ingest.py` -- and every step that has to
happen to all three belongs here, called once from each. That is not a
stylistic preference: analysis used to live in the worker alone, so sessions
synced from the logger arrived complete and unanalysed, and nothing noticed
until a driver asked why the page wanted a script run. One function the
three of them share cannot be added to two of the three.

Everything here is best-effort. By the time it runs, the session, its laps
and its dataframe are all stored; these steps are derived data and
convenience, and a backfill can redo them at any point. Raising instead
would fail an upload batch -- telling the uploader their file did not work
when it did -- or, in the sync tool, put an already-stored session back on
the retry queue to be ingested a second time.
"""

from __future__ import annotations

from .analysis_store import analyze_and_store
from .parser import Session
from .track_naming import name_track_if_unknown


def finish_session(session_db_id: int, session: Session) -> None:
    """Name the track if it is unknown, then analyse and store.

    Naming first, because it is cheap and because the analysis does not
    depend on it -- and because doing it in this order means each session in
    a batch can serve as the position reference for the next one.
    """
    name_track_if_unknown(session_db_id, session)
    analyze_and_store(session_db_id, session)
