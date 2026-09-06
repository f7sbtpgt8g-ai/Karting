#!/usr/bin/env python
"""Verify the Part 1 extraction against a real Unipro export.

`tests/test_analysis_extraction.py` asserts the same equivalence on the
synthetic fixture as part of the normal suite. This script does it against a
real export instead -- by default the one bundled with the app, or any file
passed as an argument -- and prints a per-field report rather than just
passing or failing, so the "nothing changed" claim is inspectable rather
than taken on trust.

Usage:
    python scripts/verify_analysis_extraction.py [path/to/export.tsv]
"""

from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry.analysis import analyze_lap, analyze_session, compare_laps  # noqa: E402
from telemetry.delta import segment_times_for_lap  # noqa: E402
from telemetry.parser import load_sessions  # noqa: E402

from tests.test_analysis_extraction import (  # noqa: E402
    _app_py_session_orchestration,
    equal_with_nan,
)

DEFAULT_TSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample_data", "default_session.tsv"
)


def _compare(label: str, before, after) -> bool:
    """Report one field, returning whether it matched."""
    if isinstance(before, pd.DataFrame):
        try:
            pd.testing.assert_frame_equal(after, before)
            detail = f"{len(before)} rows x {len(before.columns)} cols identical"
            ok = True
        except AssertionError as exc:
            detail = str(exc).splitlines()[0]
            ok = False
    else:
        # NaN-aware: a single-clean-lap session has std_dev_s = NaN, and
        # NaN != NaN would report identical results as a mismatch.
        ok = equal_with_nan(before, after)
        detail = f"{before!r}" if ok else f"before={before!r} after={after!r}"
    print(f"  [{'OK ' if ok else 'FAIL'}] {label:<26} {detail}")
    return ok


def verify_session(session) -> bool:
    print(f"\nSession {session.session_id} ({session.start_date} {session.start_time}) -- {len(session.df):,} rows")
    before = _app_py_session_orchestration(session)
    after = analyze_session(session)

    if before["data_error"]:
        ok = after.ok is False
        print(f"  [{'OK ' if ok else 'FAIL'}] no clean laps            both paths agree" if ok else "  [FAIL] data_error disagreement")
        return ok

    results = [
        _compare("best_lap", before["best_lap"], after.best_lap),
        _compare("clean_lap_numbers", before["clean_lap_numbers"], after.clean_lap_numbers),
        _compare("theoretical_best_s", before["theoretical_best_s"], after.theoretical_best_s),
        _compare("summary", before["summary"], after.summary),
        _compare("speed_is_estimated", before["speed_is_estimated"], after.speed_is_estimated),
        _compare("setup_suggestions", before["setup_suggestions"], after.setup_suggestions),
        _compare("laps", before["laps"], after.laps),
        _compare("clean", before["clean"], after.clean),
        _compare("segments", before["segments"], after.segments),
        _compare("best_segment_times", before["best_segment_times"], after.best_segment_times),
    ]

    # Per-lap and comparison paths, on the best lap vs the next-fastest.
    lap = before["best_lap"]
    others = [n for n in before["clean_lap_numbers"] if n != lap]
    lap_result = analyze_lap(session, after, lap)
    results.append(
        _compare(
            "lap.segment_times",
            segment_times_for_lap(session, lap, before["segments"]),
            lap_result.segment_times,
        )
    )
    if others:
        comparison = compare_laps(session, after, lap, others[0])
        print(
            f"  [OK ] compare_laps               lap {lap} vs {others[0]}: "
            f"{len(comparison.corners)} corners, {len(comparison.headline_findings)} findings"
        )

    return all(results)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    path = argv[0] if argv else DEFAULT_TSV
    if not os.path.exists(path):
        print(f"No such file: {path}", file=sys.stderr)
        return 1

    print(f"Verifying analysis extraction against {path}")
    sessions = load_sessions(path)
    print(f"{len(sessions)} session(s) detected in this export.")

    all_ok = all(verify_session(s) for s in sessions)
    print("\n" + ("ALL SESSIONS IDENTICAL -- extraction changed nothing." if all_ok else "MISMATCH FOUND -- see above."))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
