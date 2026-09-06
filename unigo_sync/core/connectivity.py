"""Answers one question: can the sessions database be reached right now?

This is the switch between the two halves of the offline flow this tool
exists for: while connected to the UniGo device's own WiFi access point
(which is not routed to the internet at all) this is expected to return
False, and downloaded sessions are queued locally (`core.pending_uploads`)
instead of uploaded immediately. Once the laptop rejoins a normal network,
this flips back to True and the queue is flushed automatically.

A local-SQLite deployment (no `SUPABASE_DB_URL`/`DATABASE_URL` configured)
has no network dependency at all -- the database is just a file on the
same disk -- so it is always considered "online" for this purpose; only a
Postgres/Supabase-backed deployment can actually be offline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from telemetry import db as pgdb

logger = logging.getLogger("unigo_sync.connectivity")

# Short enough that "connected to the UniGo AP, no route to the internet"
# fails fast instead of hanging for the OS's own multi-minute TCP timeout,
# long enough not to flag a slightly-slow-but-real connection as offline.
_PROBE_TIMEOUT_S = 3.0


@dataclass
class Reachability:
    """Whether the database answered, and if not, what went wrong.

    The reason exists because "reachable" is the switch that decides
    whether a downloaded session uploads now or sits in the pending queue,
    and a queue that never drains is otherwise indistinguishable from one
    that is merely waiting -- the user sees "waiting to upload" either way.
    Something has to be able to say *why*."""

    ok: bool
    reason: str | None = None


def probe() -> Reachability:
    """Best-effort reachability check for the configured sessions
    database, with the failure reason attached. Never raises -- callers
    treat a failure as "queue for later", not as a fatal error."""
    if not pgdb.has_postgres_configured():
        return Reachability(True)
    try:
        with pgdb.connect(connect_timeout_s=_PROBE_TIMEOUT_S) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return Reachability(True)
    except Exception as exc:  # noqa: BLE001 - any failure means "not reachable right now"
        reason = str(exc).strip() or exc.__class__.__name__
        logger.debug("sessions database not reachable: %s", reason)
        return Reachability(False, reason)


def is_online() -> bool:
    """Whether the sessions database is reachable right now. Use `probe`
    instead where the reason for a failure is worth reporting."""
    return probe().ok
