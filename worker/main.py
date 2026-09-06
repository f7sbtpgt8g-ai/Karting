#!/usr/bin/env python
"""Worker entry point: poll for pending uploads, parse them, mark them done.

    python -m worker.main

Environment:
    SUPABASE_DB_URL               Postgres connection (service-role/postgres)
    SUPABASE_URL                  Project URL, for Storage
    SUPABASE_SERVICE_ROLE_KEY     Service key -- this process only, never the
                                  Next.js client bundle
    WORKER_POLL_INTERVAL_S        Seconds between empty polls (default 5)
    WORKER_ONCE=1                 Drain the queue and exit (for tests/CI)

Polling rather than listening: a `LISTEN/NOTIFY` or webhook would react
faster, but an upload that takes ~18s to parse does not need sub-second
pickup, and a poll survives the worker being restarted or the queue being
written to by something that never learned to notify (scripts/ingest.py,
unigo_sync). `claim_next_batch()` is atomic, so adding a second worker needs
no coordination.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker.processor import BatchFailed, process_batch  # noqa: E402
from worker.queue import claim_next_batch, mark_complete, mark_failed, requeue_stale_processing  # noqa: E402
from worker.storage_client import ObjectStore, SupabaseStorage  # noqa: E402

logging.basicConfig(
    level=os.environ.get("WORKER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("worker")

_should_stop = False


def _handle_signal(signum, _frame):
    """Finish the batch in flight, then exit. Killing a worker mid-parse
    leaves its row stuck in 'processing' until the stale sweep finds it, so
    it is worth draining cleanly on a deploy."""
    global _should_stop
    logger.info("signal %s received -- finishing current batch then stopping", signum)
    _should_stop = True


def run_once(store: ObjectStore) -> int:
    """Drain every pending batch. Returns how many were processed."""
    handled = 0
    while not _should_stop:
        batch = claim_next_batch()
        if batch is None:
            break
        logger.info("batch %s: processing %s", batch.id, batch.original_filename or batch.storage_path)
        started = time.monotonic()
        try:
            created = process_batch(batch, store)
        except BatchFailed as exc:
            logger.warning("batch %s failed: %s", batch.id, exc)
            mark_failed(batch.id, str(exc))
        except Exception as exc:  # noqa: BLE001
            # An unexpected error is still that batch's problem, not the
            # worker's -- record it and keep serving the queue rather than
            # crash-looping on one bad file.
            logger.exception("batch %s errored unexpectedly", batch.id)
            mark_failed(batch.id, f"Unexpected error while processing this file: {exc}")
        else:
            elapsed = time.monotonic() - started
            logger.info("batch %s: %s session(s) in %.1fs", batch.id, created, elapsed)
            mark_complete(batch.id, created)
        handled += 1
    return handled


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    store = SupabaseStorage.from_env()
    interval = float(os.environ.get("WORKER_POLL_INTERVAL_S", "5"))

    requeued = requeue_stale_processing()
    if requeued:
        logger.info("requeued %s batch(es) left in 'processing' by a previous run", requeued)

    if os.environ.get("WORKER_ONCE"):
        return 0 if run_once(store) >= 0 else 1

    logger.info("polling for uploads every %.0fs", interval)
    last_sweep = time.monotonic()
    while not _should_stop:
        if run_once(store) == 0:
            time.sleep(interval)
        # Periodically reclaim anything a crashed worker abandoned.
        if time.monotonic() - last_sweep > 600:
            requeue_stale_processing()
            last_sweep = time.monotonic()
    logger.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
