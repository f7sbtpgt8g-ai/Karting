#!/usr/bin/env python
"""Batch job: recompute the Driver Rating and activity/streak layers.

    python -m scripts.compute_ratings                # run once
    python -m scripts.compute_ratings --recheck-all   # also re-run Part 1
                                                       # validity on every
                                                       # lap, not just
                                                       # never-checked ones
    python -m scripts.compute_ratings --loop          # poll forever
                                                       # (default: daily --
                                                       # see RATING_POLL_
                                                       # INTERVAL_S below)

Two triggers feed the same `run_rating_batch()`, for two different jobs:

  * `worker/processor.py` calls it directly, best-effort, right after a
    batch's sessions are saved -- so a driver sees their own upload
    reflected without waiting on a schedule, and so an upload that
    completes someone else's cohort (the day they drove now has a field to
    compare against) doesn't sit unrated until the next scheduled pass.
  * This script, run with `--loop` (or invoked by an external
    cron/scheduler), is the backstop that keeps moving even with nobody
    uploading -- sigma decay is read live at display time regardless, but
    the historical pace-reference buckets and reference lines are only as
    fresh as the last time something recomputed them.

Both call the exact same batch, in the exact same order -- there is no
"upload-triggered" vs. "scheduled" variant of the math, only of when it
runs. See supabase/migrations/0015_driver_rating.sql and
telemetry/rating/engine.py's module docstrings for the batch's own
phase-by-phase reasoning.

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
        help="Run forever, sleeping RATING_POLL_INTERVAL_S seconds (default 86400, i.e. daily) between passes, "
             "instead of running once and exiting. This is the backstop alongside the worker's own "
             "per-upload trigger (see the module docstring) -- daily is plenty since an upload already "
             "covers the 'just drove, want to see it' case; this exists for the days nobody uploads.",
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

    interval = float(os.environ.get("RATING_POLL_INTERVAL_S", "86400"))
    logger.info("looping every %.0fs", interval)
    while True:
        try:
            run_once()
        except Exception:  # noqa: BLE001
            logger.exception("rating batch pass failed -- will retry next interval")
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
