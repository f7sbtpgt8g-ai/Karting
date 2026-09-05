#!/usr/bin/env python
"""OS-agnostic CLI for unigo_sync -- manual "sync now" and status checks,
usable directly (for testing, Linux/Mac use, or CI) without the Windows
tray wrapper in platform_windows/. That wrapper calls the same
`core.sync_engine.run_sync` this CLI calls.

Usage:
    python -m unigo_sync.cli sync [--ingest] [--driver NAME] [--track NAME]
    python -m unigo_sync.cli watch [--ingest ...]   # polls repeatedly; Ctrl+C to stop
    python -m unigo_sync.cli status
"""

from __future__ import annotations

import argparse
import sys
import time

from .core.config import load_config
from .core.period import DEFAULT_SYNC_PERIOD, SYNC_PERIODS, cutoff_for
from .core.sync_engine import configure_logging, run_sync
from .core.sync_state import SyncState


def _add_common_sync_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default=None, help="Path to config.yaml (default: unigo_sync/config.yaml)")
    p.add_argument("--ingest", action="store_true", help="Also load newly-synced sessions into the analysis session library")
    p.add_argument("--db", default="data/sessions.db", help="Session library path (only used with --ingest)")
    p.add_argument("--driver", default=None)
    p.add_argument("--track", default=None)
    p.add_argument("--session-type", default=None, choices=["practice", "qualifying", "race"])
    p.add_argument(
        "--period", default=DEFAULT_SYNC_PERIOD, choices=SYNC_PERIODS,
        help="Only sync sessions recorded within this window (default: %(default)s). "
        "Filtered on the device's own filename before downloading, so 'all' is the only "
        "choice that can be slow on a device with a long history.",
    )


def _do_sync(args) -> int:
    config = load_config(args.config)
    configure_logging(config)
    result = run_sync(config, period_cutoff=cutoff_for(args.period))

    if args.ingest and result.new_synced:
        from .ingest_bridge import ingest_new_sessions

        n = ingest_new_sessions(result, args.db, driver=args.driver, track=args.track, session_type=args.session_type)
        print(f"Ingested {n} session(s) into {args.db}")

    print(
        f"Sync complete: {len(result.new_synced)} new, "
        f"{len(result.already_synced)} already synced, {len(result.failed)} failed, "
        f"{len(result.skipped_out_of_period)} outside the '{args.period}' window"
    )
    for name, error in result.failed:
        print(f"  FAILED: {name}: {error}", file=sys.stderr)
    return 1 if result.failed and not result.new_synced else 0


def _do_watch(args) -> int:
    config = load_config(args.config)
    configure_logging(config)
    print(f"Watching for new sessions every {config.poll_interval_s:.0f}s (Ctrl+C to stop)...")
    try:
        while True:
            result = run_sync(config, period_cutoff=cutoff_for(args.period))
            if args.ingest and result.new_synced:
                from .ingest_bridge import ingest_new_sessions

                ingest_new_sessions(result, args.db, driver=args.driver, track=args.track, session_type=args.session_type)
            if result.new_synced:
                print(f"Synced {len(result.new_synced)} new session(s): {', '.join(result.new_synced)}")
            time.sleep(config.poll_interval_s)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


def _do_status(args) -> int:
    config = load_config(args.config)
    state = SyncState(config.sync_state_db)
    records = state.list_all()
    state.close()
    if not records:
        print("No sync history yet.")
        return 0
    for r in records:
        print(f"{r.last_attempt_at}  {r.status:8s}  attempts={r.attempts}  {r.name}")
        if r.status == "failed" and r.error:
            print(f"    error: {r.error}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sync_p = sub.add_parser("sync", help="Sync new sessions from the device once")
    _add_common_sync_args(sync_p)
    sync_p.set_defaults(func=_do_sync)

    watch_p = sub.add_parser("watch", help="Poll for new sessions repeatedly until stopped")
    _add_common_sync_args(watch_p)
    watch_p.set_defaults(func=_do_watch)

    status_p = sub.add_parser("status", help="Show sync history")
    status_p.add_argument("--config", default=None)
    status_p.set_defaults(func=_do_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
