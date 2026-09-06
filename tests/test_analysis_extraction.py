"""Proves the Part 1 extraction changed no analysis.

`telemetry/analysis.py` lifted an orchestration sequence that previously
existed only inside `app.py` -- partly at module level, partly in page
bodies, interleaved with Streamlit widget calls. The risk in a move like
that isn't that it fails loudly; it's that a subtly different call order or
a dropped argument silently shifts a lap time or a corner classification by
a hair, and nobody notices because there's nothing to compare against.

So this file keeps the *pre-extraction* sequence, transcribed verbatim from
`app.py` as it stood before the façade existed, and asserts the façade
reproduces it exactly on a real session. It is deliberately duplicated code:
the whole point is to have an independent second implementation to compare
against. If `analysis.py` legitimately changes behaviour later, this test
should fail and be updated in the same commit, with the change explained --
that's the intent, not an inconvenience.
"""

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from telemetry.analysis import analyze_lap, analyze_session, compare_laps
from telemetry.corner_engine import calibrate_thresholds, compare_corners
from telemetry.corners import build_reference_segments, lap_gps_trace
from telemetry.delta import segment_times_for_lap, theoretical_best_lap
from telemetry.laps import (
    clean_lap_table,
    detect_anomalous_laps,
    flag_outlier_laps,
    lap_table,
    summarize_laps,
)
from telemetry.metrics import add_braking_throttle_estimates, lap_metric_trace
from telemetry.narrative import rank_headline_findings
from telemetry.setup_config import KartSetup
from telemetry.setup_engine import all_setup_suggestions


def equal_with_nan(a, b) -> bool:
    """Dict/scalar equality that treats NaN as equal to NaN.

    `summarize_laps` returns `std_dev_s: nan` for a session with a single
    clean lap (a short run, or one where everything else was an out-lap),
    and `nan == nan` is False in Python -- so a plain `==` on the summary
    dict reports a mismatch between two genuinely identical results. Found
    by running the real-export verification, where such a session exists;
    the synthetic fixture has no single-clean-lap session and never hit it.
    """
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal_with_nan(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(equal_with_nan(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and isinstance(b, float) and pd.isna(a) and pd.isna(b):
        return True
    return bool(a == b)


def _app_py_session_orchestration(session):
    """`app.py`'s module-level analysis block, transcribed verbatim as it
    stood before `telemetry/analysis.py` existed, with the `st.*` calls and
    `st.cache_resource` wrappers removed (those affect *when* a value is
    computed, never *what* it is)."""
    # app.py: compute_clean_laps()
    laps = flag_outlier_laps(lap_table(session))
    laps = detect_anomalous_laps(laps)
    clean = clean_lap_table(laps)

    if clean.empty:
        return {"laps": laps, "clean": clean, "data_error": True}

    clean_lap_numbers = clean["lap_number"].tolist()
    best_lap = int(clean.loc[clean["lap_time_s"].idxmin(), "lap_number"])
    lap_time_by_number = dict(zip(laps["lap_number"], laps["lap_time_s"]))

    segments = build_reference_segments(session, best_lap)
    theoretical_best_s, best_segment_times = theoretical_best_lap(session, clean_lap_numbers, segments)
    summary = summarize_laps(laps)
    # app.py passes the session's saved setup, or KartSetup(driver=...) when
    # nothing has been saved for it yet.
    setup_suggestions = all_setup_suggestions(
        session, clean_lap_numbers, segments, KartSetup(driver=session.driver)
    )

    _best_lap_trace = lap_gps_trace(session, best_lap)
    speed_is_estimated = (
        bool(_best_lap_trace["gps_speed_is_estimate"].any()) if not _best_lap_trace.empty else False
    )

    return {
        "laps": laps,
        "clean": clean,
        "clean_lap_numbers": clean_lap_numbers,
        "best_lap": best_lap,
        "lap_time_by_number": lap_time_by_number,
        "segments": segments,
        "theoretical_best_s": theoretical_best_s,
        "best_segment_times": best_segment_times,
        "summary": summary,
        "setup_suggestions": setup_suggestions,
        "speed_is_estimated": speed_is_estimated,
        "data_error": False,
    }


def test_analyze_session_matches_app_py_orchestration(session1):
    """The headline equivalence check, on a real (fixture) session."""
    before = _app_py_session_orchestration(session1)
    after = analyze_session(session1)

    assert after.ok is (not before["data_error"])
    assert after.best_lap == before["best_lap"]
    assert after.clean_lap_numbers == before["clean_lap_numbers"]
    assert after.lap_time_by_number == before["lap_time_by_number"]
    assert after.theoretical_best_s == before["theoretical_best_s"]
    assert after.speed_is_estimated == before["speed_is_estimated"]
    assert equal_with_nan(after.summary, before["summary"])
    assert equal_with_nan(after.setup_suggestions, before["setup_suggestions"])

    assert_frame_equal(after.laps, before["laps"])
    assert_frame_equal(after.clean, before["clean"])
    assert_frame_equal(after.segments, before["segments"])
    assert_frame_equal(after.best_segment_times, before["best_segment_times"])


def test_analyze_session_matches_on_a_second_session(session2):
    """Run it against a different session too -- one session agreeing could
    hide an ordering bug that only bites when the best lap isn't the first
    clean one."""
    before = _app_py_session_orchestration(session2)
    after = analyze_session(session2)

    assert after.best_lap == before["best_lap"]
    assert after.theoretical_best_s == before["theoretical_best_s"]
    assert_frame_equal(after.laps, before["laps"])
    assert_frame_equal(after.segments, before["segments"])


def test_analyze_lap_matches_app_py(session1):
    """Per-lap traces: `app.py` computed these inline in the Lap Analysis
    page body."""
    before = _app_py_session_orchestration(session1)
    analysis = analyze_session(session1)
    lap = before["best_lap"]

    expected_segment_times = segment_times_for_lap(session1, lap, before["segments"])
    expected_metric = add_braking_throttle_estimates(lap_metric_trace(session1, lap))
    expected_gps = lap_gps_trace(session1, lap)

    got = analyze_lap(session1, analysis, lap)
    assert got.lap_number == lap
    assert_frame_equal(got.segment_times, expected_segment_times)
    assert_frame_equal(got.metric_trace, expected_metric)
    assert_frame_equal(got.gps_trace, expected_gps)


def test_compare_laps_matches_app_py(session1):
    """The causal engine, as the Lap Analysis / Lap Comparison pages drive
    it: calibrate thresholds from the session's own clean laps, then compare
    the selected lap against a reference."""
    before = _app_py_session_orchestration(session1)
    analysis = analyze_session(session1)
    clean_numbers = before["clean_lap_numbers"]
    if len(clean_numbers) < 2:
        pytest.skip("needs at least two clean laps to compare")

    lap = before["best_lap"]
    reference = next(n for n in clean_numbers if n != lap)

    expected_thresholds = calibrate_thresholds(session1, clean_numbers, before["segments"])
    expected_corners = compare_corners(
        session1, lap, session1, reference, before["segments"], expected_thresholds
    )
    expected_findings = (
        rank_headline_findings(expected_corners, n=5) if not expected_corners.empty else []
    )

    got = compare_laps(session1, analysis, lap, reference)
    assert got.lap_number == lap
    assert got.reference_lap_number == reference
    assert got.thresholds == expected_thresholds
    assert_frame_equal(got.corners, expected_corners)
    assert got.headline_findings == expected_findings


def test_analysis_module_imports_no_ui_libraries():
    """The property that makes the background worker possible at all: the
    analysis path must not drag in Streamlit or Plotly. Asserted on the
    imported module's own source rather than trusting convention."""
    import inspect

    from telemetry import analysis

    source = inspect.getsource(analysis)
    assert "streamlit" not in source
    assert "plotly" not in source


def test_no_clean_laps_is_reported_not_raised(session1):
    """A session with nothing analyzable is a legitimate outcome (a short or
    aborted run), and must surface as `data_error` rather than an exception
    -- a worker processing a batch shouldn't die on one bad session."""
    empty = session1.__class__(
        session_id=session1.session_id,
        source_file=session1.source_file,
        df=session1.df.iloc[0:0].copy(),
        start_date=session1.start_date,
        start_time=session1.start_time,
        driver=session1.driver,
    )
    result = analyze_session(empty)
    assert result.ok is False
    assert result.data_error
    assert result.best_lap is None
