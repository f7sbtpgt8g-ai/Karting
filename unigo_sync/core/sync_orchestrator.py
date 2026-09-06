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

from telemetry.storage import session_library_from_env

from ..ingest_bridge import ingest_one
from .config import SyncConfig
from .connectivity import is_online
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


@dataclass
class FlushOutcome:
    uploaded: list[str] = field(default_factory=list)
    still_pending: list[str] = field(default_factory=list)
    upload_errors: list[tuple[str, str]] = field(default_factory=list)


def sync_and_upload(
    config: SyncConfig,
    period_cutoff: datetime | None,
    driver_profile_id: int | None,
    driver_display_name: str | None,
    track: str | None = None,
    session_type: str | None = None,
    uploaded_by_user_id: int | None = None,
    client: DeviceClient | None = None,
) -> SyncOutcome:
    """One full "Connect & Sync" pass: download/decode whatever's new and
    in the chosen period, then upload each one immediately if the
    database is reachable, or queue it for later if not. `client` is
    exposed mainly for tests -- real callers let `run_sync` build its own
    from `config`."""
    sync_result = run_sync(config, client=client, period_cutoff=period_cutoff)
    outcome = SyncOutcome(sync_result=sync_result)
    if not sync_result.new_synced:
        return outcome

    queue = PendingUploadQueue(config.pending_uploads_db)
    try:
        if is_online():
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
            logger.info("database unreachable -- queuing %d session(s) for upload later", len(sync_result.new_synced))
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
            return outcome
        if not is_online():
            outcome.still_pending = [p.name for p in pending]
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
    finally:
        queue.close()
    return outcome
