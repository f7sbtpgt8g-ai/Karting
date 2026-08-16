#!/usr/bin/env python
"""CLI ingestion script: parse Unipro TSV export(s) into the session library.

Designed to be run standalone (not just from the Streamlit UI) so it can be
triggered automatically after a race-day upload -- e.g. from a GitHub Action
that watches a folder or receives an uploaded file, matching the existing
automation pattern used elsewhere in this project.

Usage:
    python scripts/ingest.py session1.tsv session2.tsv \
        --driver "Austin" --track "Jyllandsringen" --session-type practice \
        --db data/sessions.db
"""

from __future__ import annotations

import argparse
import sys

from telemetry.parser import load_sessions
from telemetry.storage import SessionLibrary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", help="One or more Unipro TSV export files")
    parser.add_argument("--driver", default=None, help="Driver name to tag these sessions with")
    parser.add_argument("--track", default=None, help="Track name to tag these sessions with")
    parser.add_argument("--session-type", default=None, choices=["practice", "qualifying", "race"])
    parser.add_argument("--db", default="data/sessions.db", help="Path to the SQLite session library")
    args = parser.parse_args(argv)

    library = SessionLibrary(args.db)
    total = 0
    for path in args.files:
        try:
            sessions = load_sessions(path)
        except Exception as exc:  # noqa: BLE001 - report and continue with remaining files
            print(f"FAILED to parse {path}: {exc}", file=sys.stderr)
            continue

        for session in sessions:
            db_id = library.save_session(
                session, driver=args.driver, track_name=args.track, session_type=args.session_type
            )
            print(f"Ingested {path} session {session.session_id} -> library id {db_id}")
            total += 1

    library.close()
    print(f"Done. {total} session(s) ingested into {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
