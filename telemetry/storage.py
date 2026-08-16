"""SQLite session library.

Persists parsed sessions so trend/progression views don't require
re-uploading every TSV file each time. Lap tables live in SQLite for fast
querying; each session's full sparse dataframe (needed for corner/G/RPM
re-analysis) is cached alongside as a pickle keyed by session id, since a
pandas DataFrame with mixed sparse columns doesn't map cleanly onto SQL rows.

Designed to be driven by `scripts/ingest.py` as well as the UI, so a race
day's exports can be ingested by an automation script (e.g. a GitHub Action)
without going through Streamlit.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

import pandas as pd

from .laps import flag_outlier_laps, lap_table, summarize_laps
from .parser import Session

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT,
    session_index INTEGER,
    driver TEXT,
    track_name TEXT,
    session_type TEXT,
    start_date TEXT,
    start_time TEXT,
    ingested_at TEXT,
    best_lap_s REAL,
    average_lap_s REAL,
    std_dev_s REAL,
    n_laps INTEGER,
    cache_path TEXT
);

CREATE TABLE IF NOT EXISTS laps (
    session_db_id INTEGER,
    lap_number INTEGER,
    lap_time_s REAL,
    is_outlier INTEGER,
    outlier_reason TEXT,
    FOREIGN KEY (session_db_id) REFERENCES sessions (id)
);
"""


class SessionLibrary:
    def __init__(self, db_path: str, cache_dir: str | None = None):
        self.db_path = db_path
        self.cache_dir = cache_dir or os.path.join(os.path.dirname(os.path.abspath(db_path)) or ".", "session_cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def save_session(self, session: Session, driver: str | None = None, track_name: str | None = None, session_type: str | None = None) -> int:
        """Persist a parsed session: lap summary rows to SQLite, full raw
        dataframe to a pickle cache. Returns the new session's DB id."""
        laps = flag_outlier_laps(lap_table(session))
        summary = summarize_laps(laps)

        cur = self._conn.cursor()
        cur.execute(
            """INSERT INTO sessions
               (source_file, session_index, driver, track_name, session_type,
                start_date, start_time, ingested_at, best_lap_s, average_lap_s,
                std_dev_s, n_laps, cache_path)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session.source_file,
                session.session_id,
                driver,
                track_name,
                session_type,
                session.start_date,
                session.start_time,
                datetime.now(timezone.utc).isoformat(),
                summary.get("best_lap_s"),
                summary.get("average_lap_s"),
                summary.get("std_dev_s"),
                summary.get("n_laps"),
                None,
            ),
        )
        session_db_id = cur.lastrowid

        cache_path = os.path.join(self.cache_dir, f"session_{session_db_id}.pkl")
        session.df.to_pickle(cache_path)
        cur.execute("UPDATE sessions SET cache_path = ? WHERE id = ?", (cache_path, session_db_id))

        for _, row in laps.iterrows():
            cur.execute(
                "INSERT INTO laps (session_db_id, lap_number, lap_time_s, is_outlier, outlier_reason) VALUES (?,?,?,?,?)",
                (session_db_id, int(row["lap_number"]), float(row["lap_time_s"]), int(row["is_outlier"]), row["outlier_reason"]),
            )

        self._conn.commit()
        return session_db_id

    def list_sessions(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM sessions ORDER BY ingested_at", self._conn)

    def load_session(self, session_db_id: int) -> Session:
        row = pd.read_sql_query("SELECT * FROM sessions WHERE id = ?", self._conn, params=(session_db_id,))
        if row.empty:
            raise KeyError(f"No session with id {session_db_id}")
        r = row.iloc[0]
        df = pd.read_pickle(r["cache_path"])
        return Session(
            session_id=int(r["session_index"]),
            source_file=r["source_file"],
            df=df,
            start_date=r["start_date"],
            start_time=r["start_time"],
        )

    def laps_for_session(self, session_db_id: int) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM laps WHERE session_db_id = ? ORDER BY lap_number", self._conn, params=(session_db_id,))
