"""UI-agnostic entry points for the analysis pipeline.

Every algorithm called from here already lives in its own module in this
package, and always has -- nothing under `telemetry/` imports Streamlit or
Plotly (`app.py` is the only file in the repo that imports either). What was
missing, and what this module adds, is the **orchestration**: which functions
to call in which order, and what to thread between them.

Until now that sequence existed only inside `app.py` -- partly at module
level, partly inside individual page bodies -- interleaved with `st.*` widget
calls, `st.session_state` reads and `st.cache_resource` wrappers. So a second
caller (a background worker, a CLI, a test, a future mobile backend) had no
way to reproduce a session's analysis without reimplementing that sequence
and drifting from it. This module is that sequence, and nothing else: it
adds no new analysis, changes no thresholds, and is deliberately verifiable
as producing identical results to the Streamlit path (see
`scripts/verify_analysis_extraction.py`).

Three entry points, matching the three questions the app actually asks:

- `analyze_session()` -- everything true of a session as a whole: its lap
  table, which laps are clean, the segment map, theoretical best, and any
  kart-setup hypotheses. This is the expensive part, and the part a
  background worker computes once per upload.
- `analyze_lap()` -- one lap's own traces and per-segment times, against the
  session analysis above.
- `compare_laps()` -- the corner-by-corner causal engine for one lap against
  a reference lap, plus the ranked plain-language findings.

All three are pure with respect to their inputs: no I/O, no globals, no
caching. Callers that need caching (Streamlit's reruns, a worker's job loop)
own that themselves, because the right cache key differs per caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .corner_engine import SignificanceThresholds, calibrate_thresholds, compare_corners
from .corners import build_reference_segments, lap_gps_trace, segment_midpoints
from .delta import segment_times_for_lap, theoretical_best_lap
from .focus_areas import time_loss_per_segment
from .laps import (
    clean_lap_table,
    detect_anomalous_laps,
    flag_outlier_laps,
    lap_table,
    summarize_laps,
)
from .metrics import add_braking_throttle_estimates, lap_metric_trace
from .narrative import rank_headline_findings
from .parser import Session
from .setup_config import KartSetup
from .setup_engine import all_setup_suggestions

# Mirrors app.py's own message for this case verbatim -- a session whose
# every lap is an out-lap, in-lap or statistical outlier has nothing to
# analyze, which is a legitimate outcome of a short or aborted run rather
# than an error to raise on.
NO_CLEAN_LAPS_MESSAGE = "No clean laps found in this session after outlier filtering -- check the file."


def compute_clean_laps(session: Session) -> pd.DataFrame:
    """The lap table with outliers and likely incidents flagged -- the
    starting point for everything else.

    Kept as its own function (rather than inlined into `analyze_session`)
    because callers routinely want just this: a session list needs each
    session's best clean lap without paying for corner segmentation.
    """
    return detect_anomalous_laps(flag_outlier_laps(lap_table(session)))


@dataclass
class SessionAnalysis:
    """Everything true of one session as a whole.

    `data_error` is set (and the derived fields left empty) when the session
    has no clean laps -- callers should check it rather than assuming
    `best_lap` is populated, exactly as the Streamlit pages do today.
    """

    laps: pd.DataFrame
    clean: pd.DataFrame
    summary: dict
    clean_lap_numbers: list[int] = field(default_factory=list)
    lap_time_by_number: dict[int, float] = field(default_factory=dict)
    best_lap: int | None = None
    segments: pd.DataFrame = field(default_factory=pd.DataFrame)
    theoretical_best_s: float | None = None
    best_segment_times: pd.DataFrame = field(default_factory=pd.DataFrame)
    setup_suggestions: list[dict] = field(default_factory=list)
    # The best lap's GPS trace -- the reference line `segments` was built
    # from, and what a track map is drawn on. Returned rather than
    # recomputed by callers because `analyze_session` already needs it to
    # determine `speed_is_estimated`.
    best_lap_trace: pd.DataFrame = field(default_factory=pd.DataFrame)
    speed_is_estimated: bool = False
    data_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.data_error is None


def analyze_session(session: Session, setup: KartSetup | None = None) -> SessionAnalysis:
    """Full session-level analysis: laps, segment map, theoretical best and
    kart-setup hypotheses.

    `setup` is the kart setup saved for this specific session, if any. When
    omitted, a default `KartSetup` carrying only the session's driver name is
    used -- matching what `app.py` does for a session nobody has filled a
    setup in for yet, so the setup-correlation engine still runs (its
    suggestions are then based on its own documented Rotax EVO defaults,
    which is why every one of them is labelled with a confidence level).

    The order below is load-bearing and mirrors `app.py` exactly: segments
    are built from the *best* lap (the cleanest reference line available),
    while theoretical best is computed across every clean lap.
    """
    laps = compute_clean_laps(session)
    clean = clean_lap_table(laps)
    summary = summarize_laps(laps)

    if clean.empty:
        return SessionAnalysis(laps=laps, clean=clean, summary=summary, data_error=NO_CLEAN_LAPS_MESSAGE)

    clean_lap_numbers = clean["lap_number"].tolist()
    best_lap = int(clean.loc[clean["lap_time_s"].idxmin(), "lap_number"])
    lap_time_by_number = dict(zip(laps["lap_number"], laps["lap_time_s"]))

    segments = build_reference_segments(session, best_lap)
    theoretical_best_s, best_segment_times = theoretical_best_lap(session, clean_lap_numbers, segments)
    setup_suggestions = all_setup_suggestions(
        session, clean_lap_numbers, segments, setup or KartSetup(driver=session.driver)
    )

    # Some real exports populate Latitude/Longitude/Heading on every GPS fix
    # but never the GPS Speed channel itself -- `lap_gps_trace` falls back to
    # deriving speed from GPS Distance in that case (see corners.py). Worth
    # surfacing, since it affects every speed-based figure downstream.
    best_lap_trace = lap_gps_trace(session, best_lap)
    speed_is_estimated = bool(best_lap_trace["gps_speed_is_estimate"].any()) if not best_lap_trace.empty else False

    return SessionAnalysis(
        laps=laps,
        clean=clean,
        summary=summary,
        clean_lap_numbers=clean_lap_numbers,
        lap_time_by_number=lap_time_by_number,
        best_lap=best_lap,
        segments=segments,
        theoretical_best_s=theoretical_best_s,
        best_segment_times=best_segment_times,
        setup_suggestions=setup_suggestions,
        best_lap_trace=best_lap_trace,
        speed_is_estimated=speed_is_estimated,
    )


@dataclass
class LapAnalysis:
    """One lap's own traces and per-segment times."""

    lap_number: int
    segment_times: pd.DataFrame
    metric_trace: pd.DataFrame
    gps_trace: pd.DataFrame
    time_loss: pd.DataFrame = field(default_factory=pd.DataFrame)


def analyze_lap(session: Session, analysis: SessionAnalysis, lap_number: int) -> LapAnalysis:
    """One lap, analyzed against its session's segment map.

    `metric_trace` carries the braking/power-on inference already applied
    (`add_braking_throttle_estimates`), since every consumer in the app wants
    it and applying it twice is neither harmful nor free.

    `time_loss` is this lap's per-segment gap to the session's theoretical
    best -- the table the "where the time went" breakdown is built from.
    """
    segment_times = segment_times_for_lap(session, lap_number, analysis.segments)
    time_loss = (
        time_loss_per_segment(segment_times, analysis.best_segment_times)
        if not analysis.best_segment_times.empty
        else pd.DataFrame()
    )
    return LapAnalysis(
        lap_number=lap_number,
        segment_times=segment_times,
        metric_trace=add_braking_throttle_estimates(lap_metric_trace(session, lap_number)),
        gps_trace=lap_gps_trace(session, lap_number),
        time_loss=time_loss,
    )


@dataclass
class LapComparison:
    """One lap against a reference lap: per-corner causal classification
    plus the ranked plain-language findings drawn from it."""

    lap_number: int
    reference_lap_number: int
    corners: pd.DataFrame
    headline_findings: list[dict] = field(default_factory=list)
    thresholds: SignificanceThresholds | None = None


def compare_laps(
    session: Session,
    analysis: SessionAnalysis,
    lap_number: int,
    reference_lap_number: int,
    reference_session: Session | None = None,
    thresholds: SignificanceThresholds | None = None,
    n_findings: int = 5,
    use_anthropic: bool = False,
) -> LapComparison:
    """Corner-by-corner causal comparison of `lap_number` against
    `reference_lap_number`, with the findings ranked by net time impact.

    `reference_session` defaults to the same session, which is the common
    case (a lap against your own best). Passing a different one is what makes
    cross-driver and cross-session comparison work -- including against a
    teammate's lap, which is why this takes a session rather than assuming
    one.

    `thresholds` are the noise-aware significance thresholds; when omitted
    they're calibrated from this session's own repeat-lap variance, which is
    what `app.py` does. Diagnosis is fully deterministic and rule-based --
    `use_anthropic` only affects how the already-computed facts are phrased,
    and falls back to templates on any failure.
    """
    if thresholds is None:
        thresholds = calibrate_thresholds(session, analysis.clean_lap_numbers, analysis.segments)

    corners = compare_corners(
        session, lap_number, reference_session or session, reference_lap_number, analysis.segments, thresholds
    )
    findings = rank_headline_findings(corners, n=n_findings, use_anthropic=use_anthropic) if not corners.empty else []
    return LapComparison(
        lap_number=lap_number,
        reference_lap_number=reference_lap_number,
        corners=corners,
        headline_findings=findings,
        thresholds=thresholds,
    )


def track_map(session: Session, analysis: SessionAnalysis, lap_number: int | None = None) -> pd.DataFrame:
    """Each segment's midpoint on the GPS trace, for drawing a track map with
    corners labelled in place. Defaults to the session's best lap, the same
    reference line `analysis.segments` was built from."""
    lap = lap_number if lap_number is not None else analysis.best_lap
    if lap is None:
        return pd.DataFrame()
    return segment_midpoints(lap_gps_trace(session, lap), analysis.segments)
