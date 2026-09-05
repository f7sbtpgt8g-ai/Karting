"""Tracks which device sessions have already been synced, so re-running
sync doesn't re-download/re-convert everything every time. SQLite so it
survives restarts and is trivially inspectable (`sqlite3 sync_state.db`)
if something looks wrong.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class SyncRecord:
    name: str
    size: int
    status: str  # "success" | "failed"
    local_path: str | None
    error: str | None
    attempts: int
    first_seen_at: str
    last_attempt_at: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS synced_sessions (
    name TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    status TEXT NOT NULL,
    local_path TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_attempt_at TEXT NOT NULL
);
"""


class SyncState:
    def __init__(self, db_path: str):
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def is_synced(self, name: str, size: int) -> bool:
        """True if this session was already successfully synced with the
        same size. A size mismatch (the device somehow has a different
        file under the same name) is treated as "not synced" so it gets
        picked up again, rather than silently skipped."""
        row = self._conn.execute(
            "SELECT size FROM synced_sessions WHERE name = ? AND status = 'success'", (name,)
        ).fetchone()
        return row is not None and row[0] == size

    def record_attempt(
        self,
        name: str,
        size: int,
        status: str,
        local_path: str | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        existing = self._conn.execute(
            "SELECT attempts, first_seen_at FROM synced_sessions WHERE name = ?", (name,)
        ).fetchone()
        if existing is None:
            self._conn.execute(
                "INSERT INTO synced_sessions (name, size, status, local_path, error, attempts, first_seen_at, last_attempt_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (name, size, status, local_path, error, now, now),
            )
        else:
            attempts = existing[0] + 1
            self._conn.execute(
                "UPDATE synced_sessions SET size = ?, status = ?, local_path = ?, error = ?, attempts = ?, last_attempt_at = ? "
                "WHERE name = ?",
                (size, status, local_path, error, attempts, now, name),
            )
        self._conn.commit()

    def list_failed(self) -> list[SyncRecord]:
        rows = self._conn.execute(
            "SELECT name, size, status, local_path, error, attempts, first_seen_at, last_attempt_at "
            "FROM synced_sessions WHERE status = 'failed'"
        ).fetchall()
        return [SyncRecord(*row) for row in rows]

    def list_all(self) -> list[SyncRecord]:
        rows = self._conn.execute(
            "SELECT name, size, status, local_path, error, attempts, first_seen_at, last_attempt_at "
            "FROM synced_sessions ORDER BY last_attempt_at DESC"
        ).fetchall()
        return [SyncRecord(*row) for row in rows]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SyncState":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
