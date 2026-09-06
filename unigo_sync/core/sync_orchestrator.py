"""Ties `sync_engine.run_sync` (download + decode to the staging folder,
`core.sync_state`-deduped) together with uploading into the sessions
database, and is the one place that decides whether an upload happens now
or gets queued.

This is deliberately the boundary between "always safe, no network
needed" and "needs the database reachable": `run_sync` runs unconditionally
-- while connected to the UniGo device's own WiFi AP there is no route to
the internet at all, but the device itself only needs its own local
address, so downloading and decoding to `.unigo_sync.tsv` in the staging
folder always proceeds. Whether the *upload* half of that also happens
immediately depends on `core.connectivity.is_online()`; when it can't,
sessions land in `core.pending_uploads.PendingUploadQueue` instead, and
`flush_pending_uploads` (called both right after a sync and from a
background poll -- see `platform_windows/gui_app.py`) drains that queue
the moment the database is reachable again, with no user action needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from telemetry.storage import session_library_from_env

from ..ingest_bridge import ingest_one
from .config import SyncConfig
from .connectivity import probe
from .device_client import DeviceClient
from .pending_uploads import PendingUploadQueue
from .sync_engine import SyncResult, run_sync

logger = logging.getLogger("unigo_sync.sync_orchestrator")


@dataclass
class SyncOutcome:
    sync_result: SyncResult
    uploaded: list[str] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    upload_errors: list[tuple[str, str]] = field(default_factory=list)
    # Why the sessions were queued rather than uploaded, when the database
    # itself was unreachable. None when it answered.
    offline_reason: str | None = None


@dataclass
class FlushOutcome:
    uploaded: list[str] = field(default_factory=list)
    still_pending: list[str] = field(default_factory=list)
    upload_errors: list[tuple[str, str]] = field(default_factory=list)
    # Why nothing could be uploaded, when the database itself was the
    # problem rather than any individual session. None when it answered.
    offline_reason: str | None = None

    @property
    def blocked_reason(self) -> str | None:
        """One line explaining why the queue did not drain, or None if
        there was nothing to drain or it drained cleanly. This is what the
        GUI shows next to the pending count -- a queue that is stuck and a
        queue that is merely waiting for the network look identical
        otherwise."""
        if self.offline_reason:
            return f"database not reachable: {self.offline_reason}"
        if self.upload_errors:
            _name, error = self.upload_errors[0]
            return f"upload failed: {error}"
        return None


# `flush_pending_uploads` runs on a 15-second background poll, so logging
# an unreachable database at WARNING every time would bury the log file in
# thousands of identical lines. Reported on the first occurrence and on
# every change instead, which is what makes the log useful for working out
# why a queue is not draining.
_last_blocked_reason: str | None = None


def _report_blocked(reason: str | None, pending_count: int) -> None:
    global _last_blocked_reason
    if reason == _last_blocked_reason:
        return
    _last_blocked_reason = reason
    if reason is None:
        logger.info("pending uploads are flowing again")
    else:
        logger.warning("%d session(s) still queued -- %s", pending_count, reason)


def sync_and_upload(
    config: SyncConfig,
    period_cutoff: datetime | None,
    driver_profile_id: int | None,
    driver_display_name: str | None,
    track: str | None = None,
    session_type: str | None = None,
    uploaded_by_user_id: int | None = None,
    client: DeviceClient | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> SyncOutcome:
    """One full "Connect & Sync" pass: download/decode whatever's new and
    in the chosen period, then upload each one immediately if the
    database is reachable, or queue it for later if not. `client` is
    exposed mainly for tests -- real callers let `run_sync` build its own
    from `config`. `on_progress` is passed straight through to `run_sync`
    -- see its docstring."""
    sync_result = run_sync(config, client=client, period_cutoff=period_cutoff, on_progress=on_progress)
    outcome = SyncOutcome(sync_result=sync_result)
    if not sync_result.new_synced:
        return outcome

    queue = PendingUploadQueue(config.pending_uploads_db)
    try:
        reachability = probe()
        if reachability.ok:
            library = session_library_from_env(config.sessions_db)
            try:
                for name in sync_result.new_synced:
                    path = sync_result.paths.get(name)
                    if path is None:
                        continue
                    try:
                        ingest_one(
                            library, path, driver=driver_display_name, track=track,
                            session_type=session_type, driver_profile_id=driver_profile_id,
                            uploaded_by_user_id=uploaded_by_user_id,
                        )
                        outcome.uploaded.append(name)
                    except Exception as exc:  # noqa: BLE001 - queue it rather than lose it
                        logger.warning("upload failed for %s, queuing for retry: %s", name, exc)
                        queue.add(name, path, driver_profile_id, driver_display_name, track, session_type)
                        outcome.queued.append(name)
                        outcome.upload_errors.append((name, str(exc)))
            finally:
                library.close()
        else:
            outcome.offline_reason = reachability.reason
            logger.info(
                "database unreachable (%s) -- queuing %d session(s) for upload later",
                reachability.reason, len(sync_result.new_synced),
            )
            for name in sync_result.new_synced:
                path = sync_result.paths.get(name)
                if path is None:
                    continue
                queue.add(name, path, driver_profile_id, driver_display_name, track, session_type)
                outcome.queued.append(name)
    finally:
        queue.close()
    return outcome


def flush_pending_uploads(config: SyncConfig) -> FlushOutcome:
    """Upload everything currently queued, if the database is reachable.
    Safe to call opportunistically and often (e.g. from a background poll
    every `poll_interval_s`) -- a no-op with an empty queue, and a no-op
    (returns everything as still-pending) while offline, without raising."""
    outcome = FlushOutcome()
    queue = PendingUploadQueue(config.pending_uploads_db)
    try:
        pending = queue.list_pending()
        if not pending:
            _report_blocked(None, 0)
            return outcome

        reachability = probe()
        if not reachability.ok:
            outcome.still_pending = [p.name for p in pending]
            outcome.offline_reason = reachability.reason
            _report_blocked(outcome.blocked_reason, len(pending))
            return outcome

        library = session_library_from_env(config.sessions_db)
        try:
            for item in pending:
                try:
                    ingest_one(
                        library, item.local_path, driver=item.driver_display_name,
                        track=item.track_name, session_type=item.session_type,
                        driver_profile_id=item.driver_profile_id,
                    )
                    queue.remove(item.name)
                    outcome.uploaded.append(item.name)
                    logger.info("flushed queued upload for %s", item.name)
                except Exception as exc:  # noqa: BLE001 - stays queued, retried next flush
                    logger.warning("retry upload failed for %s: %s", item.name, exc)
                    queue.record_attempt_failed(item.name, str(exc))
                    outcome.still_pending.append(item.name)
                    outcome.upload_errors.append((item.name, str(exc)))
        finally:
            library.close()
        _report_blocked(outcome.blocked_reason, len(outcome.still_pending))
    finally:
        queue.close()
    return outcome
