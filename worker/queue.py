"""Claiming and completing `upload_batches` rows.

The claim is a single `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP
LOCKED LIMIT 1)`. That shape matters: `SKIP LOCKED` is what lets a second
worker be started without two of them racing to parse the same 80MB file,
so scaling out later needs no coordination and no change here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telemetry import db as pgdb

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"


@dataclass
class UploadBatch:
    id: int
    storage_path: str
    original_filename: str | None
    uploaded_by_user_id: int | None
    driver_profile_id: int | None
    track_name: str | None
    session_type: str | None
    visibility: str
    track_condition: str | None = None
    temperature_c: float | None = None
    humidity_pct: float | None = None
    pressure_hpa: float | None = None
    altitude_m: float | None = None
    conditions_source: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "UploadBatch":
        return cls(
            id=int(row["id"]),
            storage_path=row["storage_path"],
            original_filename=row.get("original_filename"),
            uploaded_by_user_id=row.get("uploaded_by_user_id"),
            driver_profile_id=row.get("driver_profile_id"),
            track_name=row.get("track_name"),
            session_type=row.get("session_type"),
            visibility=row.get("visibility") or "shared",
            track_condition=row.get("track_condition"),
            temperature_c=row.get("temperature_c"),
            humidity_pct=row.get("humidity_pct"),
            pressure_hpa=row.get("pressure_hpa"),
            altitude_m=row.get("altitude_m"),
            conditions_source=row.get("conditions_source"),
        )

    @property
    def conditions(self) -> dict[str, Any]:
        """The upload-level context passed straight through to every session
        this file produces."""
        return {
            "track_condition": self.track_condition,
            "temperature_c": self.temperature_c,
            "humidity_pct": self.humidity_pct,
            "pressure_hpa": self.pressure_hpa,
            "altitude_m": self.altitude_m,
            "conditions_source": self.conditions_source,
        }


def claim_next_batch() -> UploadBatch | None:
    """Atomically take the oldest pending batch, or None if the queue is
    empty. Safe to run from several workers at once."""
    with pgdb.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE upload_batches
               SET status = '{STATUS_PROCESSING}', claimed_at = now()
             WHERE id = (
                 SELECT id FROM upload_batches
                  WHERE status = '{STATUS_PENDING}'
                  ORDER BY created_at
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
             )
            RETURNING *
            """
        )
        row = cur.fetchone()
        conn.commit()
    return UploadBatch.from_row(dict(row)) if row else None


def mark_complete(batch_id: int, sessions_created: int) -> None:
    with pgdb.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE upload_batches SET status='{STATUS_COMPLETE}', sessions_created=%s, "
            f"finished_at=now(), error_message=NULL WHERE id=%s",
            (sessions_created, batch_id),
        )
        conn.commit()


def mark_failed(batch_id: int, error: str) -> None:
    """Record why a batch failed, in language an uploader can act on -- this
    string is shown in the UI, so it should say what to do about it, not
    just what raised."""
    with pgdb.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE upload_batches SET status='{STATUS_FAILED}', error_message=%s, "
            f"finished_at=now() WHERE id=%s",
            (error[:2000], batch_id),
        )
        conn.commit()


def requeue_stale_processing(older_than_minutes: int = 30) -> int:
    """Return batches a worker claimed but never finished to the queue.

    A worker killed mid-parse (a deploy, an OOM on a very large file) leaves
    its row stuck in 'processing' forever, and the uploader sees a spinner
    that never resolves. Nothing else would ever notice, so the worker sweeps
    for these on startup and periodically. Returns how many were requeued.
    """
    with pgdb.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""UPDATE upload_batches
                   SET status='{STATUS_PENDING}', claimed_at=NULL
                 WHERE status='{STATUS_PROCESSING}'
                   AND claimed_at < now() - (%s * interval '1 minute')""",
            (older_than_minutes,),
        )
        n = cur.rowcount
        conn.commit()
    return n
