#!/usr/bin/env python
"""Batch job: recompute the Driver Rating and activity/streak layers.

    python -m scripts.compute_ratings                # run once
    python -m scripts.compute_ratings --recheck-all   # also re-run Part 1
                                                       # validity on every
                                                       # lap, not just
                                                       # never-checked ones
    python -m scripts.compute_ratings --loop          # poll forever
                                                       # (WORKER_POLL_INTERVAL_S
                                                       # style deployment)

Deliberately its own process, not called from worker/processor.py's upload
path -- see supabase/migrations/0015_driver_rating.sql and
telemetry/rating/engine.py's module docstrings for why cohort-based rating
updates need to run as a separate batch/background job rather than
synchronously on upload.

Environment: SUPABASE_DB_URL (or DATABASE_URL) -- same as every other
Postgres-backed script in this repo (telemetry/db.py). This script has no
SQLite fallback: cross-driver rating fundamentally needs the shared
Supabase deployment, not a local single-user database.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry.rating.config import DEFAULT_RATING_CONFIG  # noqa: E402
from telemetry.rating.engine import run_rating_batch  # noqa: E402

logging.basicConfig(
    level=os.environ.get("RATING_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("scripts.compute_ratings")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--recheck-all", action="store_true",
        help="Re-run Part 1 validity classification on every lap, not just laps never checked before "
             "(useful after tuning telemetry/rating/config.py's validity thresholds).",
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="Run forever, sleeping RATING_POLL_INTERVAL_S seconds (default 3600, i.e. hourly) between passes, "
             "instead of running once and exiting.",
    )
    args = parser.parse_args()

    def run_once() -> None:
        started = time.monotonic()
        result = run_rating_batch(DEFAULT_RATING_CONFIG, recheck_all=args.recheck_all)
        elapsed = time.monotonic() - started
        logger.info("batch complete in %.1fs: %s", elapsed, result)

    if not args.loop:
        run_once()
        return 0

    interval = float(os.environ.get("RATING_POLL_INTERVAL_S", "3600"))
    logger.info("looping every %.0fs", interval)
    while True:
        try:
            run_once()
        except Exception:  # noqa: BLE001
            logger.exception("rating batch pass failed -- will retry next interval")
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
