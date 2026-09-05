"""Queues sessions that have been downloaded and decoded to the local
staging folder but not yet uploaded into the sessions database --
because the database wasn't reachable at the time (see
`core.connectivity`), which is the expected state for the whole time a
laptop is connected to the UniGo device's own WiFi access point.

Kept as its own small SQLite table (sibling to `sync_state.py`'s, same
connect-per-call convention) rather than folded into `SyncState`: sync
state answers "has this session been downloaded from the device", which
is unaffected by whether the upload half has happened yet, and a session
can legitimately be downloaded (success) while its upload is still
pending, retried, or eventually failing for an unrelated reason (e.g. the
chosen driver profile was deleted server-side in the meantime).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_uploads (
    name TEXT PRIMARY KEY,
    local_path TEXT NOT NULL,
    driver_profile_id INTEGER,
    driver_display_name TEXT,
    track_name TEXT,
    session_type TEXT,
    queued_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
"""


@dataclass
class PendingUpload:
    name: str
    local_path: str
    driver_profile_id: int | None
    driver_display_name: str | None
    track_name: str | None
    session_type: str | None
    queued_at: str
    attempts: int
    last_error: str | None


class PendingUploadQueue:
    def __init__(self, db_path: str):
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def add(
        self,
        name: str,
        local_path: str,
        driver_profile_id: int | None,
        driver_display_name: str | None,
        track_name: str | None = None,
        session_type: str | None = None,
    ) -> None:
        """Queue one session for upload once the database is reachable.
        `INSERT OR REPLACE` so re-queuing the same session (e.g. a retry
        after a fresh sync pass re-decoded it) just refreshes its record
        rather than erroring on the primary key."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT OR REPLACE INTO pending_uploads "
            "(name, local_path, driver_profile_id, driver_display_name, track_name, session_type, "
            " queued_at, attempts, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)",
            (name, local_path, driver_profile_id, driver_display_name, track_name, session_type, now),
        )
        self._conn.commit()

    def list_pending(self) -> list[PendingUpload]:
        rows = self._conn.execute(
            "SELECT name, local_path, driver_profile_id, driver_display_name, track_name, session_type, "
            "queued_at, attempts, last_error FROM pending_uploads ORDER BY queued_at ASC"
        ).fetchall()
        return [PendingUpload(*row) for row in rows]

    def record_attempt_failed(self, name: str, error: str) -> None:
        self._conn.execute(
            "UPDATE pending_uploads SET attempts = attempts + 1, last_error = ? WHERE name = ?",
            (error, name),
        )
        self._conn.commit()

    def remove(self, name: str) -> None:
        """Called once a queued session has been successfully uploaded."""
        self._conn.execute("DELETE FROM pending_uploads WHERE name = ?", (name,))
        self._conn.commit()

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM pending_uploads").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PendingUploadQueue":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
