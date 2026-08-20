"""Streamlit UI for the karting telemetry analysis tool.

This file is UI orchestration only -- all parsing/analysis logic lives in
the `telemetry` package so it stays independently testable and reusable
(e.g. from `scripts/ingest.py` in a CI/automation context).
"""

from __future__ import annotations

import io
import json
import os
import tempfile

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import yaml
from plotly.offline import get_plotlyjs
from plotly.subplots import make_subplots
import streamlit as st
import streamlit.components.v1 as components

from telemetry.comparison import corner_comparison_across_sessions, cross_session_delta_trace, session_progression
from telemetry.corners import assign_segments, build_reference_segments, lap_gps_trace, segment_midpoints
from telemetry.delta import delta_time_trace, segment_times_for_lap, theoretical_best_lap
from telemetry.focus_areas import blended_top_recommendations, recurring_weaknesses, time_loss_per_segment, top_focus_areas
from telemetry.laps import (
    clean_lap_table,
    detect_anomalous_laps,
    flag_outlier_laps,
    lap_table,
    lap_time_with_deltas,
    summarize_laps,
)
from telemetry.metrics import (
    add_braking_throttle_estimates,
    braking_zones,
    consistency_stats,
    lap_metric_trace,
    rpm_band_summary_across_laps,
    segment_aggregates,
    time_in_rpm_band,
)
from telemetry.parser import Session, load_sessions
from telemetry.setup_config import KartSetup
from telemetry.setup_engine import all_setup_suggestions
from telemetry.simulation import (
    build_accel_rpm_curve,
    estimate_lap_time_delta,
    fit_speed_rpm_scale,
    simulate_gearing_change,
)
from telemetry.storage import SessionLibrary

st.set_page_config(page_title="Karting Telemetry", layout="wide", page_icon="🏎️")

DEFAULT_TSV_PATH = os.path.join(os.path.dirname(__file__), "sample_data", "default_session.tsv")
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "sessions.db")


@st.cache_resource(show_spinner=False)
def plotlyjs_script_tag() -> str:
    """The plotly.js bundle shipped with the installed `plotly` package,
    inlined as a <script> tag for the hand-rolled hover-linked chart (see
    render_linked_speed_delta). Tried referencing it as an external static
    file served via Streamlit's `server.enableStaticServing` first, to avoid
    re-sending several MB of JS on every rerun -- worked locally but came up
    blank on Streamlit Community Cloud (its static-file route apparently
    doesn't behave the same there), so this inlines the JS directly instead.
    Larger per-render payload, but it doesn't depend on a platform feature
    that's turned out to be unreliable, and doesn't need any outbound network
    access either. Cached so the (cheap, in-memory) lookup isn't repeated
    every rerun.
    """
    return f"<script>{get_plotlyjs()}</script>"


@st.cache_resource(show_spinner=False)
def get_session_library() -> SessionLibrary:
    """One SQLite connection reused across reruns. Local disk only --
    resets whenever this app's container reboots or redeploys (Streamlit
    Community Cloud's filesystem isn't persistent across those), which is a
    known, accepted limitation for now rather than an oversight."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return SessionLibrary(DB_PATH)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner="Parsing telemetry file...")
def parse_uploaded_file(file_bytes: bytes, filename: str) -> list[Session]:
    """`cache_resource`, not `cache_data`: returns the same Session objects
    across reruns (no deep-copy) so downstream per-session caches below stay
    warm -- Streamlit reruns this entire script on every widget interaction,
    and a 900k-row file takes ~10s to parse, so re-parsing on every rerun
    would make the app unusable at the track.
    """
    with tempfile.NamedTemporaryFile(suffix=".tsv", delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        sessions = load_sessions(tmp_path)
    finally:
        os.unlink(tmp_path)
    for s in sessions:
        s.source_file = filename
    return sessions


def session_label(driver: str | None, session: Session, best_lap_s: float | None) -> str:
    """Driver + session number + date/time + best lap -- deliberately
    excludes the source filename (meaningless once sessions from several
    drivers' exports are mixed together in one library; "default_session.tsv"
    told you nothing a driver would recognize their own session by)."""
    date = session.start_date or "?"
    time = session.start_time or "?"
    best = f"{best_lap_s:.2f}s" if best_lap_s is not None else "no clean laps"
    return f"{driver or 'Unknown driver'} — Session {session.session_id} — {date} {time} — {best}"


@st.cache_resource(show_spinner="Loading saved sessions...")
def load_persisted_sessions_cached(_library: SessionLibrary, _sessions_meta: pd.DataFrame, meta_key: tuple) -> list[tuple[str, Session]]:
    """Fully reconstruct (unpickle) every session already saved in the
    library -- this is what lets uploaded files persist across reruns and
    app restarts without re-uploading, since `all_sessions` is built from
    here instead of from a live file_uploader widget. Cached on `meta_key`
    (a tuple of session DB ids) so this only redoes the actual unpickling
    work when a session is added, not on every Streamlit rerun.
    """
    sessions: list[tuple[str, Session]] = []
    for _, row in _sessions_meta.iterrows():
        session = _library.load_session(int(row["id"]))
        best_lap_s = float(row["best_lap_s"]) if pd.notna(row["best_lap_s"]) else None
        label = session_label(session.driver, session, best_lap_s)
        sessions.append((label, session))
    return sessions


def session_cache_key(session: Session) -> tuple:
    """Cheap, stable surrogate identity for a Session, for use as an
    explicit cache-key argument alongside an underscore-prefixed (so
    Streamlit doesn't try to hash it) session parameter. Hashing the actual
    session dataframe on every cached call would be needlessly expensive for
    a 100k+ row session, and `Session.channel_cache` grows over the app's
    lifetime as different tabs pull different channels, which would make a
    content-hash of the object unstable anyway.
    """
    return (session.source_file, session.session_id, len(session.df))


def compute_clean_laps(session: Session) -> pd.DataFrame:
    """Not cached: cheap (ms-scale, verified on a 117k-row real session) to
    recompute from the already-parsed dataframe, so it's not worth the
    complexity of a cache-key scheme here."""
    laps = flag_outlier_laps(lap_table(session))
    laps = detect_anomalous_laps(laps)
    return laps


def session_best_lap_times(sessions_with_labels: list[tuple[str, Session]]) -> dict[str, float | None]:
    """Fastest clean lap time per loaded session (None if a session has no
    clean laps) -- shown in the "Session to analyze" picker so it's obvious
    which session to look at without opening each one first, and used to
    pick the default (see `fastest_lap_session_label`)."""
    times: dict[str, float | None] = {}
    for label, s in sessions_with_labels:
        clean_s = clean_lap_table(compute_clean_laps(s))
        times[label] = float(clean_s["lap_time_s"].min()) if not clean_s.empty else None
    return times


def fastest_lap_session_label(session_best_times: dict[str, float | None]) -> str | None:
    """Which loaded session has the single fastest clean lap -- used to
    default the "Session to analyze" picker so the driver doesn't have to
    manually hunt for their best session out of a multi-session file."""
    valid = {label: t for label, t in session_best_times.items() if t is not None}
    return min(valid, key=valid.get) if valid else None


@st.cache_resource(show_spinner=False)
def compute_setup_suggestions_cached(
    _session: Session, _key: tuple, clean_lap_numbers: tuple, segments: pd.DataFrame, setup: KartSetup
) -> list[dict]:
    """The setup correlation engine loops over every clean lap several times
    over (~1s on a real 18-lap session) -- caching it means other widget
    interactions elsewhere in the app don't re-run it every time, since
    that's a full-script rerun in Streamlit regardless of which view is open.
    """
    return all_setup_suggestions(_session, list(clean_lap_numbers), segments, setup)


@st.cache_resource(show_spinner=False)
def compute_session_top_focus_areas_cached(_session: Session, _key: tuple, clean_lap_numbers: tuple, best_lap: int) -> list[dict]:
    """Per-session top-3 focus areas for the cross-session "recurring
    weaknesses" view. Caching this is what keeps a multi-session file (this
    tool's real-world case -- an 11-session, 900k-row day at the track)
    from re-running full corner/theoretical-best/diagnosis analysis for
    every *other* loaded session on every single interaction.
    """
    segs = build_reference_segments(_session, best_lap)
    _, best_seg_times = theoretical_best_lap(_session, list(clean_lap_numbers), segs)
    lap_seg_times = segment_times_for_lap(_session, best_lap, segs)
    return top_focus_areas(_session, best_lap, segs, lap_seg_times, best_seg_times, n=3)


@st.cache_resource(show_spinner=False)
def build_segments_and_midpoints_cached(_session: Session, _key: tuple, best_lap: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-session segment table + each corner's GPS midpoint, cached by
    session identity -- the Corner Comparison view needs this for *every*
    loaded session (not just the active one), so without caching it'd be
    rebuilt from scratch on every rerun for every session in a
    multi-session file.
    """
    segs = build_reference_segments(_session, best_lap)
    trace = lap_gps_trace(_session, best_lap)
    mids = segment_midpoints(trace, segs)
    return segs, mids


@st.cache_resource(show_spinner="Comparing this corner across all loaded sessions...")
def compute_corner_comparison_cached(
    _sessions_data: list, cache_key: tuple, reference_lat: float, reference_lon: float
) -> pd.DataFrame:
    """Per-lap corner time + entry/apex/exit metrics for one corner, across
    every loaded session. Looping `lap_metric_trace` over every clean lap
    of every session is the expensive part (same pattern as
    `compute_session_top_focus_areas_cached` above) -- caching by
    (session set, corner location) means re-selecting a previously-viewed
    corner is instant, and only a genuinely new corner triggers the full
    recompute.
    """
    return corner_comparison_across_sessions(_sessions_data, reference_lat, reference_lon)


@st.cache_resource(show_spinner=False)
def fit_speed_rpm_scale_cached(_session: Session, _key: tuple, clean_lap_numbers: tuple) -> float | None:
    return fit_speed_rpm_scale(_session, list(clean_lap_numbers))


@st.cache_resource(show_spinner=False)
def build_accel_rpm_curve_cached(_session: Session, _key: tuple, clean_lap_numbers: tuple) -> pd.DataFrame:
    return build_accel_rpm_curve(_session, list(clean_lap_numbers))


# ---------------------------------------------------------------------------
# Linked speed/RPM/G-force/delta trace + track map (Data Analysis page)
# ---------------------------------------------------------------------------

def render_linked_speed_delta(
    chart_fig: go.Figure, map_fig: go.Figure, dist: list, lat: list, lon: list,
    height: int, map_height: int | None = None, chart_row_y_domains: list[tuple[float, float]] | None = None,
) -> None:
    """A stacked speed/RPM/delta chart and a track map, hover-linked
    entirely client-side: hovering the chart (any row, any overlaid lap)
    moves a marker to the matching point on the map, with no Streamlit
    rerun per mouse move -- replacing the old "read the distance off the
    tooltip, then drag a slider to that value" flow with an automatic one.

    Streamlit has no built-in way to sync hover state between two
    independently-rendered `st.plotly_chart` figures, and driving the sync
    through a Python rerun on every `plotly_hover` event would mean a
    round-trip for every pixel the mouse crosses. Instead this renders both
    figures as plain Plotly.js inside one `components.html` block and wires
    a hover listener in JS, so the highlight is instant and the Python side
    is untouched until a real widget (e.g. a lap selector) changes.

    `chart_fig` overlays multiple laps, each with its own distance-sampled
    trace of potentially different length, so there's no single shared
    "point index" to key off. All laps share the same x scale (distance in
    metres) though, so the hovered point's underlying *distance* (still
    present in the `plotly_hover` event payload even though the tooltip
    itself, via a custom `hovertemplate`, no longer displays it) is what's
    used to place the marker -- linearly
    interpolated client-side into the map lap's own lat/lon arrays (`dist`/
    `lat`/`lon`), the same way the old slider-driven marker used
    `np.interp` server-side.

    `map_height`, when shorter than `height`, keeps the map at that shorter
    height while the chart column renders at its full natural height beside
    it -- the whole component's iframe is sized to fit that full height, so
    the *page* scrolls it normally rather than the chart getting its own
    internal scrollbar. The map is then kept visually in view with a
    hand-rolled "sticky": genuine CSS `position: sticky` can't reach across
    the `components.html` iframe boundary (it's a separate browsing context
    with no scrolling of its own here -- sticky only ever responds to
    scrolling *within* the same document, and the outer Streamlit page's
    scroll is invisible to it), so instead this polls the iframe's own
    position in the page via `window.frameElement.getBoundingClientRect()`
    on every animation frame and translates the map down by just enough to
    keep it pinned near the top of the viewport, clamped so it never drifts
    past the bottom of the chart column.

    `chart_row_y_domains`, one (y0, y1) pair per row of `chart_fig` (see
    `_axis_y_domain`), draws a thin crosshair line at the hovered distance
    across *every* row, not just the one being hovered -- e.g. hovering a
    feature on the delta trace also marks that same distance on the speed
    and RPM rows above it. Plotly's own built-in spike lines could do this
    (`xaxis.showspikes` + `spikemode="across"`), but only in "x"/"x
    unified" hovermode, which -- as described above -- always draws a
    floating distance-value label on the axis with no way to suppress just
    that; `hovermode="closest"` avoids the label but drops spike lines as a
    side effect, so this draws the crosshair manually via `Plotly.relayout`
    on every hover/unhover instead, independent of hovermode entirely.
    """
    chart_spec = chart_fig.to_json()
    map_spec = map_fig.to_json()
    marker_trace_index = len(map_fig.data) - 1
    map_height = height if map_height is None else map_height
    floating_map = map_height < height

    html = f"""
<div style="display:flex; gap:12px; width:100%; font-family:inherit; align-items:flex-start;">
  <div id="chartDiv" style="flex:1 1 62%; min-width:0;"></div>
  <div id="mapWrap" style="flex:1 1 38%; min-width:0; height:{map_height}px; position:relative;">
    <div id="mapDiv" style="position:absolute; top:0; left:0; right:0;"></div>
  </div>
</div>
{plotlyjs_script_tag()}
<script>
(function() {{
  var chartSpec = {chart_spec};
  var mapSpec = {map_spec};
  var dist = {json.dumps(dist)};
  var lat = {json.dumps(lat)};
  var lon = {json.dumps(lon)};
  var markerTraceIndex = {marker_trace_index};

  var chartDiv = document.getElementById("chartDiv");
  var mapDiv = document.getElementById("mapDiv");
  var mapWrap = document.getElementById("mapWrap");
  var rowYDomains = {json.dumps([list(d) for d in chart_row_y_domains]) if chart_row_y_domains else "null"};
  chartSpec.layout.height = {height};
  mapSpec.layout.height = {map_height};
  Plotly.newPlot(chartDiv, chartSpec.data, chartSpec.layout, {{displayModeBar: false}});
  Plotly.newPlot(mapDiv, mapSpec.data, mapSpec.layout, {{displayModeBar: false}});

  if ({"true" if floating_map else "false"}) {{
    var topGap = 12;
    (function trackScroll() {{
      var frameEl = window.frameElement;
      if (frameEl) {{
        var rect = frameEl.getBoundingClientRect();
        var maxOffset = Math.max(0, rect.height - {map_height});
        var offset = topGap - rect.top;
        offset = Math.max(0, Math.min(offset, maxOffset));
        mapWrap.style.transform = "translateY(" + offset + "px)";
      }}
      requestAnimationFrame(trackScroll);
    }})();
  }}

  function crosshairShapes(x) {{
    return rowYDomains.map(function(d, i) {{
      var xref = i === 0 ? "x" : "x" + (i + 1);
      return {{
        type: "line", xref: xref, yref: "paper",
        x0: x, x1: x, y0: d[0], y1: d[1],
        line: {{color: "rgba(90,90,90,0.6)", width: 1, dash: "dot"}},
      }};
    }});
  }}

  function interpAt(x) {{
    if (!dist.length) return null;
    if (x <= dist[0]) return {{lat: lat[0], lon: lon[0]}};
    if (x >= dist[dist.length - 1]) return {{lat: lat[lat.length - 1], lon: lon[lon.length - 1]}};
    var lo = 0, hi = dist.length - 1;
    while (hi - lo > 1) {{
      var mid = (lo + hi) >> 1;
      if (dist[mid] <= x) {{ lo = mid; }} else {{ hi = mid; }}
    }}
    var span = dist[hi] - dist[lo];
    var t = span ? (x - dist[lo]) / span : 0;
    return {{lat: lat[lo] + t * (lat[hi] - lat[lo]), lon: lon[lo] + t * (lon[hi] - lon[lo])}};
  }}

  chartDiv.on("plotly_hover", function(evt) {{
    if (!evt.points || !evt.points.length) return;
    var x = evt.points[0].x;
    var p = interpAt(x);
    if (p && p.lat != null && p.lon != null) {{
      Plotly.restyle(mapDiv, {{x: [[p.lon]], y: [[p.lat]]}}, [markerTraceIndex]);
    }}
    if (rowYDomains) {{
      Plotly.relayout(chartDiv, {{shapes: crosshairShapes(x)}});
    }}
  }});
  if (rowYDomains) {{
    chartDiv.on("plotly_unhover", function() {{
      Plotly.relayout(chartDiv, {{shapes: []}});
    }});
  }}
}})();
</script>
"""
    components.html(html, height=height + 20, scrolling=False)


# ---------------------------------------------------------------------------
# Kart setup form (shared between the upfront onboarding gate and the
# revisit-later "Kart Setup" tab)
# ---------------------------------------------------------------------------

def render_setup_fields(setup: KartSetup) -> KartSetup:
    st.markdown("**Engine**")
    c1, c2, c3 = st.columns(3)
    setup.class_name = c1.text_input("Class", value=setup.class_name)
    setup.peak_power_rpm_low = c2.number_input(
        "Peak-power RPM band: low", value=setup.peak_power_rpm_low, step=100,
        help="Confirm against your engine builder's spec sheet -- this is a Rotax EVO ballpark default, not a measurement.",
    )
    setup.peak_power_rpm_high = c3.number_input("Peak-power RPM band: high", value=setup.peak_power_rpm_high, step=100)

    st.markdown("**Gearing / drivetrain**")
    c1, c2, c3 = st.columns(3)
    setup.gearing.front_teeth = c1.number_input("Front (clutch) teeth", value=setup.gearing.front_teeth or 12, step=1)
    setup.gearing.rear_teeth = c2.number_input("Rear axle teeth", value=setup.gearing.rear_teeth or 80, step=1)
    setup.gearing.chain_pitch = c3.text_input("Chain pitch", value=setup.gearing.chain_pitch)

    st.markdown("**Carburettor (Dellorto VHSB34 defaults)**")
    c1, c2, c3 = st.columns(3)
    setup.carburettor.main_jet = c1.number_input("Main jet", value=setup.carburettor.main_jet or 128, step=1)
    setup.carburettor.needle_clip_position = c2.number_input("Needle clip position", value=setup.carburettor.needle_clip_position or 2, step=1)
    setup.carburettor.air_screw_turns_out = c3.number_input("Air screw turns out", value=setup.carburettor.air_screw_turns_out or 1.5, step=0.25)

    st.markdown("**Tyres**")
    c1, c2 = st.columns(2)
    setup.tyres.hot_pressure_front_bar = c1.number_input("Hot pressure front (bar)", value=setup.tyres.hot_pressure_front_bar or 0.8, step=0.05)
    setup.tyres.hot_pressure_rear_bar = c2.number_input("Hot pressure rear (bar)", value=setup.tyres.hot_pressure_rear_bar or 0.8, step=0.05)

    st.markdown("**Chassis**")
    c1, c2 = st.columns(2)
    setup.chassis.seat_position_fore_aft_mm = c1.number_input("Seat position fore/aft (mm)", value=setup.chassis.seat_position_fore_aft_mm or 0.0, step=5.0)
    setup.chassis.caster = c2.number_input("Caster", value=setup.chassis.caster or 0.0, step=0.5)

    st.markdown("**Track / session context**")
    c1, c2 = st.columns(2)
    setup.track_session.track_name = c1.text_input("Track name", value=setup.track_session.track_name or "")
    setup.track_session.session_type = c2.selectbox("Session type", ["practice", "qualifying", "race"], index=["practice", "qualifying", "race"].index(setup.track_session.session_type))

    return setup


# ---------------------------------------------------------------------------
# Table display: human-readable column headers
#
# Every dataframe rendered via st.dataframe() below is built with
# code-friendly column names (snake_case, unit suffixes like `_s`/`_kmh`) so
# the analysis modules stay easy to work with -- but shown verbatim in the
# UI, those read like debug output rather than a table meant for a driver
# to glance at trackside. COLUMN_LABELS/prettify_columns rename a display
# copy just before st.dataframe(), leaving the underlying data untouched.
# ---------------------------------------------------------------------------

COLUMN_LABELS = {
    "segment_label": "Segment",
    "segment_kind": "Type",
    "time_loss_s": "Time Available (s)",
    "your_time_s": "Your Time (s)",
    "best_time_s": "Best Time (s)",
    "best_time_from_lap": "Best Set On Lap",
    "lap_number": "Lap",
    "lap_time_s": "Lap Time (s)",
    "delta_to_best_s": "Δ to Best (s)",
    "delta_to_average_s": "Δ to Average (s)",
    "delta_to_personal_best_s": "Δ to Personal Best (s)",
    "is_outlier": "Outlier",
    "outlier_reason": "Outlier Reason",
    "likely_incident": "Likely Incident",
    "brake_point_m": "Brake Point (m)",
    "end_m": "End (m)",
    "duration_s": "Duration (s)",
    "peak_decel_g": "Peak Decel (g)",
    "entry_speed_kmh": "Entry Speed (km/h)",
    "entry_rpm": "Entry RPM",
    "apex_speed_kmh": "Apex Speed (km/h)",
    "apex_rpm": "Apex RPM",
    "exit_speed_kmh": "Exit Speed (km/h)",
    "exit_rpm": "Exit RPM",
    "min_speed_kmh": "Min Speed (km/h)",
    "max_speed_kmh": "Max Speed (km/h)",
    "avg_speed_kmh": "Avg Speed (km/h)",
    "lateral_g_std": "Lateral G Std Dev",
    "corner_time_s": "Corner Time (s)",
    "session_label": "Session",
    "session": "Session",
    "best_lap_s": "Best Lap (s)",
    "average_lap_s": "Average Lap (s)",
    "std_dev_s": "Std Dev (s)",
    "n_laps": "Laps",
    "n_sessions": "Sessions",
    "avg_time_loss_s": "Avg Time Lost (s)",
    "total_time_loss_s": "Total Time Lost (s)",
    "id": "ID",
    "source_file": "Source File",
    "driver": "Driver",
    "track_name": "Track",
    "session_type": "Session Type",
    "start_date": "Date",
    "start_time": "Start Time",
    "ingested_at": "Saved At",
    "session_index": "Session #",
    "saved_at": "Saved At",
}


def prettify_columns(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns=COLUMN_LABELS)


# ---------------------------------------------------------------------------
# Shared helpers for the page functions below.
#
# `format_lap_option` and `_require_data`/`render_footer` are read by page
# functions but reference names (`lap_time_by_number`, `data_ready`,
# `speed_is_estimated`, ...) that are only assigned further down, in the
# "Sidebar navigation + shared data loading" section. That's fine: a
# module-level function resolves free variables against this module's
# globals at CALL time, not at definition time, and every page function is
# only ever called via `nav.run()` at the very end of the script, by which
# point those globals have already been populated for this rerun.
# ---------------------------------------------------------------------------

def format_lap_option(lap_no: int) -> str:
    t = lap_time_by_number.get(lap_no)
    return f"Lap {lap_no} — {t:.2f}s" if t is not None else f"Lap {lap_no}"


def _require_data() -> bool:
    """Call at the top of every page except Overview and Settings. Returns
    False (after showing an explanatory message) when there's no active
    session to analyze yet, so the page body can `return` early instead of
    rendering against empty/missing data."""
    if data_ready:
        return True
    if data_error_message:
        st.error(data_error_message)
    else:
        st.info("Upload a telemetry file on the Settings page to get started.")
    return False


def render_footer() -> None:
    st.divider()
    footer_caption = (
        "Braking, throttle/power-on, and jetting diagnostics are all inferred from RPM and GPS-derived G-forces -- "
        "there is no throttle, brake, gear, or EGT/lambda channel in this export. Treat those as estimates, not measurements."
    )
    if speed_is_estimated:
        footer_caption += " Speed itself is also estimated here, derived from GPS Distance since this export doesn't populate GPS Speed directly."
    st.caption(footer_caption)


# ---------------------------------------------------------------------------
# Pages
#
# Deliberately built as plain functions passed to st.Page(), not st.tabs():
# with st.tabs(), every `with tabs[i]:` block's code executes on *every*
# script rerun regardless of which tab is visually selected (a documented
# Streamlit behavior), and empirically, once this app's combined per-tab
# content (large Plotly figures, big tables, cross-session loops) got heavy
# enough across 9 tabs, the last couple of tabs stopped rendering at all --
# no exception, content just silently never arrived client-side. st.Page's
# callable only runs for the page currently selected in st.navigation, which
# sidesteps the problem entirely and is strictly less work every rerun
# besides.
# ---------------------------------------------------------------------------

def page_overview() -> None:
    if not all_sessions:
        st.title("Karting Telemetry Analysis")
        st.info(
            "Upload one or more Unipro laptimer TSV exports on the Settings page to get started. "
            "A single file may contain multiple sessions (the tool detects logger restarts automatically)."
        )
        st.markdown(
            "**What this tool does:** parses sparse/asynchronous Unipro telemetry, segments the track into "
            "corners from the GPS trace, and ranks where you're losing the most time -- with a plain-language "
            "coaching note for each. Fill in your kart setup from the Kart Setup page (per session, since gearing "
            "and jetting can differ session to session) to get setup-change hypotheses folded into that ranking too."
        )
        return

    if not _require_data():
        return

    st.title(f"{active_session.driver or 'Unknown driver'} — Top 3 Focus Areas")
    st.caption(f"Analyzing lap {analyzed_lap} · {active_label}")
    if speed_is_estimated:
        st.caption("ℹ️ This export doesn't populate GPS Speed directly -- speed is estimated from GPS Distance instead. Treat speed-based figures as estimates, not direct measurements.")

    # Full per-segment breakdown -- the Top 3 cards below are just the highest
    # few rows of this. The headline "available" delta is derived from the SAME
    # table (its own sum), not from the device's raw best-lap-time minus
    # theoretical-best, so the number here and the sum of the breakdown chart
    # below always agree exactly -- they're the same computation, not two
    # independent ones that happen to be close.
    full_breakdown = time_loss_per_segment(lap_segment_times, best_segment_times)
    segment_based_available_s = full_breakdown["time_loss_s"].sum()
    device_measured_gap_s = summary["best_lap_s"] - theoretical_best_s

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Best lap", f"{summary['best_lap_s']:.2f}s")
    col2.metric("Theoretical best", f"{theoretical_best_s:.2f}s", delta=f"-{segment_based_available_s:.2f}s available", delta_color="inverse")
    col3.metric("Consistency (std dev)", f"{laps['lap_time_s'].std():.2f}s")
    col4.metric("Clean laps", f"{len(clean)} / {len(laps)}")

    interpolation_residual_s = device_measured_gap_s - segment_based_available_s
    if abs(interpolation_residual_s) > 0.03:
        st.caption(
            f"ℹ️ The device's own lap clock puts the gap to theoretical best at {device_measured_gap_s:.2f}s; "
            f"the segment-by-segment breakdown below accounts for {segment_based_available_s:.2f}s of that. "
            f"The remaining {interpolation_residual_s:.2f}s is GPS-distance interpolation error at each segment "
            "boundary (small per-boundary rounding, compounding across many corners), not a missed opportunity "
            "hiding somewhere -- every segment is already listed below."
        )

    focus_areas = blended_top_recommendations(
        active_session, analyzed_lap, segments, lap_segment_times, best_segment_times, setup_suggestions, n=3
    )

    if not focus_areas:
        st.success("No significant time loss detected vs. your theoretical best in this lap -- nice and consistent!")
    else:
        n_setup_cards = sum(1 for a in focus_areas if a["kind"] == "setup")
        cards = st.columns(len(focus_areas))
        for i, (col, area) in enumerate(zip(cards, focus_areas), start=1):
            with col:
                if area["kind"] == "setup":
                    st.subheader(f"#{i} Setup: {area['segment_label']}")
                    st.caption(f"Confidence: {area['confidence']}")
                    st.write(area["coaching_note"])
                    st.caption(f"Why: {area['technical_note']}")
                else:
                    st.subheader(f"#{i} {area['segment_label']}")
                    st.metric("Time available", f"{area['time_loss_s']:.2f}s")
                    st.write(area["coaching_note"])
                    st.caption(f"Cause (inferred): {area['cause'].replace('_', ' ')}")
        if n_setup_cards:
            st.caption(
                f"Note: {n_setup_cards} of the {len(focus_areas)} card(s) above is a session-wide setup issue, not "
                "a per-corner time value -- it doesn't count toward the seconds total below. See the full breakdown "
                "for every corner's individual gap."
            )

    with st.expander(f"Full path to theoretical best — all {len(full_breakdown)} segments (sums to -{segment_based_available_s:.2f}s above)", expanded=True):
        st.caption("Every segment on this lap, ranked by time available. The Top 3 cards above are just the top rows of this same table.")
        fig_breakdown = go.Figure()
        fig_breakdown.add_trace(
            go.Bar(
                x=full_breakdown["segment_label"], y=full_breakdown["time_loss_s"],
                marker_color=["#d62728" if k == "corner" else "#1f77b4" for k in full_breakdown["segment_kind"]],
            )
        )
        fig_breakdown.update_layout(xaxis_title="Segment", yaxis_title="Time available (s)", height=350)
        st.plotly_chart(fig_breakdown, width='stretch')
        breakdown_display = full_breakdown[
            ["segment_label", "segment_kind", "time_loss_s", "time_s_lap", "time_s_best", "best_source_lap"]
        ].rename(columns={"time_s_lap": "your_time_s", "time_s_best": "best_time_s", "best_source_lap": "best_time_from_lap"})
        breakdown_display[["time_loss_s", "your_time_s", "best_time_s"]] = breakdown_display[
            ["time_loss_s", "your_time_s", "best_time_s"]
        ].round(3)
        st.dataframe(prettify_columns(breakdown_display), width='stretch')

        st.caption("Where these segments are on track (labels abbreviated: C = Corner, S = Straight):")
        segment_locations = segment_midpoints(_best_lap_trace, segments)
        if segment_locations.empty:
            st.caption("No GPS position data available on the reference lap to draw a map.")
        else:
            map_data = segment_locations.merge(full_breakdown[["segment_label", "time_loss_s"]], on="segment_label", how="left")
            map_labels = map_data["segment_label"].str.replace("Corner ", "C", regex=False).str.replace("Straight ", "S", regex=False)
            fig_map = go.Figure()
            fig_map.add_trace(
                go.Scatter(
                    x=_best_lap_trace["Longitude"], y=_best_lap_trace["Latitude"],
                    mode="lines", line=dict(color="lightgray", width=2), hoverinfo="skip", showlegend=False,
                )
            )
            fig_map.add_trace(
                go.Scatter(
                    x=map_data["mid_lon"], y=map_data["mid_lat"],
                    mode="markers+text",
                    text=map_labels,
                    textposition="top center",
                    marker=dict(
                        size=12,
                        color=map_data["time_loss_s"],
                        colorscale="RdYlGn_r",
                        showscale=True,
                        colorbar=dict(title="s available"),
                        line=dict(width=1, color="black"),
                    ),
                    hovertext=[f"{row.segment_label}: {row.time_loss_s:.2f}s available" for row in map_data.itertuples()],
                    hoverinfo="text",
                    showlegend=False,
                )
            )
            fig_map.update_layout(xaxis_title="Longitude", yaxis_title="Latitude", height=500, yaxis=dict(scaleanchor="x"))
            st.plotly_chart(fig_map, width='stretch')

    render_footer()


def page_lap_times() -> None:
    if not _require_data():
        return
    st.subheader("Lap time table")
    # Reuses the per-session best times already computed (and cached in
    # session_state) for the "Session to analyze" picker, rather than
    # re-running outlier/anomaly detection across every loaded session from
    # scratch on every visit to this page -- compute_clean_laps is
    # deliberately uncached (see its docstring), so redoing that here too
    # was showing up as a multi-second delay on a multi-session file.
    pb_across_loaded = min(t for t in session_best_times.values() if t is not None)
    annotated = lap_time_with_deltas(laps, personal_best_s=pb_across_loaded)
    display_cols = ["lap_number", "lap_time_s", "delta_to_best_s", "delta_to_average_s", "delta_to_personal_best_s", "is_outlier", "outlier_reason", "likely_incident"]
    display_cols = [c for c in display_cols if c in annotated.columns]
    st.dataframe(prettify_columns(annotated[display_cols]), width='stretch')
    st.caption("Rows flagged as an outlier are excluded from best/average stats above but shown here for review.")
    render_footer()


# Used to color-match each "Laps to compare" row to its line in the charts.
LAP_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f"]


def _axis_y_domain(fig: go.Figure, row: int) -> tuple[float, float]:
    """Paper-space [y0, y1] span of a `make_subplots` row's y-axis (row 1 ->
    `yaxis`, row 2 -> `yaxis2`, ...) -- used to position UI elements (a
    secondary legend, a cross-subplot crosshair) against one specific row
    of a combined multi-row figure."""
    axis_key = "yaxis" if row == 1 else f"yaxis{row}"
    domain = fig.layout[axis_key].domain
    return (float(domain[0]), float(domain[1]))


def _lap_label(lap_no: int, times: dict[int, float]) -> str:
    t = times.get(lap_no)
    return f"Lap {lap_no} — {t:.2f}s" if t is not None else f"Lap {lap_no}"


def _readable_text_color(hex_color: str) -> str:
    """Black or white, whichever reads better on `hex_color` -- used to keep
    the lap-selector text legible once its background is recolored to match
    that row's line color, which spans light and dark hues alike."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#1a1a1a" if luminance > 0.6 else "#ffffff"


def _session_clean_laps(session: Session) -> tuple[list[int], dict[int, float]]:
    """Clean lap numbers + their times for an arbitrary session (not
    necessarily the active one) -- used by the Data Analysis page's per-row
    session/lap pickers, which need this for whichever session each row
    currently points at, not just the sidebar's active session, and by the
    default lap selection below."""
    laps_df = clean_lap_table(compute_clean_laps(session))
    numbers = laps_df["lap_number"].tolist()
    times = dict(zip(laps_df["lap_number"], laps_df["lap_time_s"]))
    return numbers, times


def _ensure_valid_widget_state(key: str, valid_options: list, fallback) -> None:
    """Reset a widget's session_state value to `fallback` if it's no longer
    among `valid_options` -- e.g. a row's remembered lap number doesn't
    exist in a session the row was just switched to, or the loaded file set
    changed on the Settings page since this value was last set. Must run
    before the widget with this key is instantiated (the standard Streamlit
    pattern for programmatically setting a widget's value)."""
    if st.session_state.get(key) not in valid_options:
        st.session_state[key] = fallback


DATA_ANALYSIS_CHART_LABELS = {
    "speed": "Speed (km/h)",
    "rpm": "RPM",
    "lat_g": "GPS Lateral Acceleration (g)",
    "lon_g": "GPS Longitudinal Acceleration (g)",
    "delta": "Delta vs fastest lap (s) — positive = time lost",
}
DATA_ANALYSIS_CHART_KEYS = list(DATA_ANALYSIS_CHART_LABELS)


def _default_data_analysis_rows(all_sessions: list[tuple[str, Session]]) -> list[tuple[str, int]]:
    """(session_label, lap_number) pairs to preseed the "Laps to compare"
    rows with: the two fastest clean laps from the most recent session (by
    start date/time) plus the fastest clean lap from the session before
    it -- a reasonable "how am I doing today vs. last time" starting
    comparison without the user needing to pick anything themselves first.
    """
    if not all_sessions:
        return []
    ordered = sorted(all_sessions, key=lambda item: (item[1].start_date or "", item[1].start_time or ""), reverse=True)
    rows: list[tuple[str, int]] = []

    latest_label, latest_session = ordered[0]
    latest_numbers, latest_times = _session_clean_laps(latest_session)
    fastest_latest = sorted(latest_numbers, key=lambda n: latest_times[n])[:2]
    rows.extend((latest_label, n) for n in fastest_latest)

    if len(ordered) > 1:
        prev_label, prev_session = ordered[1]
        prev_numbers, prev_times = _session_clean_laps(prev_session)
        if prev_numbers:
            fastest_prev = min(prev_numbers, key=lambda n: prev_times[n])
            rows.append((prev_label, fastest_prev))

    return rows


def page_data_analysis() -> None:
    if not _require_data():
        return
    st.subheader("Data analysis")
    st.caption(
        "A deeper look across speed, RPM, G-forces and delta -- pick any laps from any loaded sessions below. "
        "Each row's color matches its line in the charts, and the fastest lap among your picks is always used "
        "as the delta reference and as the map's tracked position."
    )

    MAX_COMPARE_LAPS = 8

    if "da_row_ids" not in st.session_state:
        default_rows = _default_data_analysis_rows(all_sessions)
        st.session_state["da_row_ids"] = list(range(len(default_rows)))
        st.session_state["da_next_row_id"] = len(default_rows)
        for i, (sess_label, lap_no) in enumerate(default_rows):
            st.session_state[f"da_session_{i}"] = sess_label
            st.session_state[f"da_lap_{i}"] = lap_no

    compare_entries = []
    css_rules = []
    with st.expander("Laps to compare", expanded=True):
        for idx, row_id in enumerate(list(st.session_state["da_row_ids"])):
            row_color = LAP_COLORS[idx % len(LAP_COLORS)]
            text_color = _readable_text_color(row_color)
            session_key, lap_key = f"da_session_{row_id}", f"da_lap_{row_id}"
            # Scoped via Streamlit's auto-generated `st-key-<key>` class on
            # this specific widget's own wrapper -- recolors just this row's
            # Lap dropdown to match its line color in the charts below,
            # without touching any other widget on the page.
            css_rules.append(
                f'.st-key-{lap_key} [role="group"] {{ background-color: {row_color} !important; }}'
                f'.st-key-{lap_key} input {{ color: {text_color} !important; }}'
            )

            rc1, rc2, rc3 = st.columns([4, 3, 1])
            label_visibility = "visible" if idx == 0 else "collapsed"
            _ensure_valid_widget_state(session_key, session_labels, active_label)
            row_session_label = rc1.selectbox(
                "Session", session_labels, key=session_key, label_visibility=label_visibility,
            )
            row_session = dict(all_sessions)[row_session_label]
            row_lap_numbers, row_lap_times = _session_clean_laps(row_session)
            if not row_lap_numbers:
                rc2.caption("No clean laps in this session.")
                if rc3.button("✕", key=f"da_remove_{row_id}", help="Remove this row"):
                    st.session_state["da_row_ids"].remove(row_id)
                    st.rerun()
                continue
            _ensure_valid_widget_state(lap_key, row_lap_numbers, row_lap_numbers[0])
            row_lap = rc2.selectbox(
                "Lap", row_lap_numbers, key=lap_key, format_func=lambda n, _t=row_lap_times: _lap_label(n, _t),
                label_visibility=label_visibility,
            )
            if rc3.button("✕", key=f"da_remove_{row_id}", help="Remove this row"):
                st.session_state["da_row_ids"].remove(row_id)
                st.rerun()
            compare_entries.append({
                "row_id": row_id, "session_label": row_session_label, "session": row_session, "lap_number": row_lap,
                "lap_time": row_lap_times.get(row_lap), "color": row_color,
                "tag": f"S{row_session.session_id}·L{row_lap}",
            })

        if css_rules:
            st.markdown(f"<style>{''.join(css_rules)}</style>", unsafe_allow_html=True)

        if len(st.session_state["da_row_ids"]) >= MAX_COMPARE_LAPS:
            st.caption(f"Maximum {MAX_COMPARE_LAPS} laps at once.")
        elif st.button("+ Add lap to compare", key="da_add_row"):
            new_id = st.session_state["da_next_row_id"]
            st.session_state["da_row_ids"].append(new_id)
            st.session_state["da_next_row_id"] = new_id + 1
            st.rerun()

    if not compare_entries:
        st.info("Add at least one lap to compare.")
        render_footer()
        return

    # No manual reference/position picker on this page (unlike Speed &
    # Delta) -- the fastest lap among the current picks always plays both
    # roles, per how this page was requested.
    fastest_entry = min(compare_entries, key=lambda e: e["lap_time"] if e["lap_time"] is not None else float("inf"))

    visible_keys = st.multiselect(
        "Charts to show", DATA_ANALYSIS_CHART_KEYS, default=DATA_ANALYSIS_CHART_KEYS, key="da_visible_charts",
        format_func=lambda k: DATA_ANALYSIS_CHART_LABELS[k],
    )
    active_keys = [k for k in DATA_ANALYSIS_CHART_KEYS if k in visible_keys]
    if not active_keys:
        st.info("Select at least one chart to display.")
        render_footer()
        return

    row_index = {key: i + 1 for i, key in enumerate(active_keys)}
    n_rows = len(active_keys)
    fig = make_subplots(
        rows=n_rows, cols=1, shared_xaxes=True, vertical_spacing=0.04,
        subplot_titles=[DATA_ANALYSIS_CHART_LABELS[k] for k in active_keys],
    )

    lap_traces: dict[int, pd.DataFrame] = {}
    for entry in compare_entries:
        trace = lap_metric_trace(entry["session"], entry["lap_number"])
        lap_traces[entry["row_id"]] = trace
        color, tag = entry["color"], entry["tag"]
        if "speed" in row_index:
            fig.add_trace(
                go.Scatter(
                    x=trace["lap_distance_m"], y=trace["GPS Speed"], mode="lines", name=tag, legendgroup=tag, line=dict(color=color),
                    hovertemplate=f"{tag}: %{{y:.1f}} km/h<extra></extra>",
                ),
                row=row_index["speed"], col=1,
            )
        if "rpm" in row_index:
            fig.add_trace(
                go.Scatter(
                    x=trace["lap_distance_m"], y=trace["RPM"], mode="lines", name=f"{tag} RPM", legendgroup=tag, line=dict(color=color),
                    showlegend="speed" not in row_index, hovertemplate=f"{tag}: %{{y:.0f}} RPM<extra></extra>",
                ),
                row=row_index["rpm"], col=1,
            )
        if "lat_g" in row_index:
            fig.add_trace(
                go.Scatter(
                    x=trace["lap_distance_m"], y=trace["GPS Lateral Acceleration"], mode="lines", name=f"{tag} lat g", legendgroup=tag,
                    line=dict(color=color), showlegend=False, hovertemplate=f"{tag}: %{{y:.2f}}g<extra></extra>",
                ),
                row=row_index["lat_g"], col=1,
            )
        if "lon_g" in row_index:
            fig.add_trace(
                go.Scatter(
                    x=trace["lap_distance_m"], y=trace["GPS Longitudinal Acceleration"], mode="lines", name=f"{tag} lon g", legendgroup=tag,
                    line=dict(color=color), showlegend=False, hovertemplate=f"{tag}: %{{y:.2f}}g<extra></extra>",
                ),
                row=row_index["lon_g"], col=1,
            )
        if "delta" in row_index and entry is not fastest_entry:
            dt = cross_session_delta_trace(entry["session"], entry["lap_number"], fastest_entry["session"], fastest_entry["lap_number"], n_points=800)
            fig.add_trace(
                go.Scatter(
                    x=dt["distance_m"], y=dt["delta_s"], mode="lines", name=f"{tag} delta", legendgroup=tag, line=dict(color=color),
                    showlegend=False, hovertemplate=f"{tag}: %{{y:.4f}}s<extra></extra>",
                ),
                row=row_index["delta"], col=1,
            )

    if "delta" in row_index:
        fig.add_hline(y=0, row=row_index["delta"], col=1, line_dash="dash", line_color="gray")

    if "rpm" in row_index:
        rpm_row = row_index["rpm"]
        fig.add_hrect(
            y0=setup.peak_power_rpm_low, y1=setup.peak_power_rpm_high,
            row=rpm_row, col=1, fillcolor="green", opacity=0.1, line_width=0,
        )
        rpm_domain = _axis_y_domain(fig, rpm_row)
        fig.update_layout(
            legend2=dict(
                x=1.02, xanchor="left", y=(rpm_domain[0] + rpm_domain[1]) / 2, yanchor="middle",
                title=dict(text="% of lap in power band", font=dict(size=11)), font=dict(size=11),
                bgcolor="rgba(0,0,0,0)",
            )
        )
        for entry in compare_entries:
            band = time_in_rpm_band(entry["session"], entry["lap_number"], (setup.peak_power_rpm_low, setup.peak_power_rpm_high))
            pct_label = f"{band['fraction_in_band'] * 100:.0f}%" if band.get("lap_duration_s", 0) > 0 else "n/a"
            fig.add_trace(
                go.Scatter(
                    x=[None], y=[None], mode="markers", marker=dict(size=10, color=entry["color"], symbol="square"),
                    name=f"{entry['tag']}: {pct_label}", legend="legend2", showlegend=True, hoverinfo="skip",
                ),
                row=rpm_row, col=1,
            )

    fig.update_xaxes(title_text="Distance (m)", row=n_rows, col=1)
    # "closest" (not "x"/"x unified"): both of those hovermodes draw the
    # shared distance value as a small floating label right on the axis
    # regardless of each trace's hovertemplate -- there's no layout option
    # to suppress just that label, so "closest" is the only mode that shows
    # nothing but each hovered trace's own (distance-free) tooltip.
    fig.update_layout(hovermode="closest")
    fig.update_xaxes(showspikes=False)
    row_y_domains = [_axis_y_domain(fig, r) for r in range(1, n_rows + 1)]

    per_row_height = 260
    fig_height = per_row_height * n_rows

    primary_trace = lap_traces[fastest_entry["row_id"]].dropna(subset=["lap_distance_m", "Latitude", "Longitude"]).sort_values("lap_distance_m")
    map_fig = go.Figure()
    map_fig.add_trace(
        go.Scattergl(
            x=primary_trace["Longitude"], y=primary_trace["Latitude"], mode="lines",
            line=dict(color=fastest_entry["color"], width=2), showlegend=False,
        )
    )
    if not primary_trace.empty:
        map_fig.add_trace(
            go.Scatter(
                x=[primary_trace["Longitude"].iloc[0]], y=[primary_trace["Latitude"].iloc[0]],
                mode="markers", marker=dict(size=16, color="red", line=dict(width=2, color="white")), showlegend=False,
            )
        )
    map_fig.update_layout(yaxis=dict(scaleanchor="x"), xaxis_title="Longitude", yaxis_title="Latitude", margin=dict(t=10))

    st.caption(
        f"Fastest lap selected: {fastest_entry['tag']} ({fastest_entry['lap_time']:.2f}s) -- used as the delta "
        "reference above and the map's tracked position below. Scroll the charts on the left to see them all -- "
        "the map stays put on the right, hovering anywhere in the charts moves its marker, and a crosshair line "
        "marks the same distance across every chart so you can line up a feature in one against the others."
    )
    render_linked_speed_delta(
        fig, map_fig,
        primary_trace["lap_distance_m"].tolist(), primary_trace["Latitude"].tolist(), primary_trace["Longitude"].tolist(),
        height=fig_height, map_height=per_row_height, chart_row_y_domains=row_y_domains,
    )
    render_footer()


def page_track_map() -> None:
    if not _require_data():
        return
    st.subheader("Track map")
    map_lap = st.selectbox("Lap", clean_lap_numbers, index=clean_lap_numbers.index(best_lap), key="map_lap", format_func=format_lap_option)
    color_by = st.radio("Color by", ["Speed", "Delta vs reference (best lap)"], horizontal=True)
    trace = lap_gps_trace(active_session, map_lap)

    if color_by == "Speed":
        color_vals = trace["GPS Speed"]
        colorbar_title = "km/h"
    else:
        dt = delta_time_trace(active_session, map_lap, best_lap, n_points=200)
        if len(dt) > 0:
            color_vals = np.interp(trace["lap_distance_m"], dt["distance_m"], dt["delta_s"])
        else:
            color_vals = [0] * len(trace)
        colorbar_title = "delta (s)"

    fig4 = go.Figure()
    fig4.add_trace(
        go.Scattergl(
            x=trace["Longitude"],
            y=trace["Latitude"],
            mode="markers+lines",
            marker=dict(size=5, color=color_vals, colorscale="RdYlGn_r" if color_by != "Speed" else "Viridis", showscale=True, colorbar=dict(title=colorbar_title)),
            line=dict(color="lightgray", width=1),
        )
    )
    fig4.update_layout(xaxis_title="Longitude", yaxis_title="Latitude", height=600, yaxis=dict(scaleanchor="x"))
    st.plotly_chart(fig4, width='stretch')
    render_footer()


def page_braking_rpm() -> None:
    if not _require_data():
        return
    st.subheader("Braking zones (inferred — no brake channel in this export)")
    brake_lap = st.selectbox("Lap", clean_lap_numbers, index=clean_lap_numbers.index(best_lap), key="brake_lap", format_func=format_lap_option)
    trace = lap_metric_trace(active_session, brake_lap)
    trace = add_braking_throttle_estimates(trace)
    zones = braking_zones(trace)
    st.dataframe(prettify_columns(zones), width='stretch')

    st.subheader("RPM trace")
    fig5 = go.Figure()
    fig5.add_trace(go.Scatter(x=trace["lap_distance_m"], y=trace["RPM"], mode="lines", name="RPM"))
    if trace["RPM unfiltered"].notna().any():
        fig5.add_trace(go.Scatter(x=trace["lap_distance_m"], y=trace["RPM unfiltered"], mode="lines", name="RPM unfiltered", opacity=0.5))
    fig5.add_hrect(y0=setup.peak_power_rpm_low, y1=setup.peak_power_rpm_high, fillcolor="green", opacity=0.1, line_width=0)
    fig5.update_layout(xaxis_title="Distance (m)", yaxis_title="RPM", height=400)
    st.plotly_chart(fig5, width='stretch')

    st.subheader("Per-corner entry / apex / exit speed & RPM")
    agg = segment_aggregates(trace, segments)
    display_cols = [
        "segment_label", "segment_kind",
        "entry_speed_kmh", "entry_rpm",
        "apex_speed_kmh", "apex_rpm",
        "exit_speed_kmh", "exit_rpm",
        "min_speed_kmh", "max_speed_kmh", "avg_speed_kmh", "lateral_g_std",
    ]
    st.dataframe(prettify_columns(agg[[c for c in display_cols if c in agg.columns]]), width='stretch')

    st.subheader("Time in peak-power RPM zone")
    st.caption(f"Band: {setup.peak_power_rpm_low}-{setup.peak_power_rpm_high} RPM (edit under Kart Setup — confirm against your engine builder's spec).")
    band_result = time_in_rpm_band(active_session, brake_lap, (setup.peak_power_rpm_low, setup.peak_power_rpm_high))
    c1, c2 = st.columns(2)
    if band_result["lap_duration_s"] > 0:
        c1.metric("Time in band", f"{band_result['time_in_band_s']:.1f}s / {band_result['lap_duration_s']:.1f}s")
        c2.metric("Fraction of lap", f"{band_result['fraction_in_band']:.0%}")
    else:
        st.info("No RPM data available for this lap.")

    band_summary = rpm_band_summary_across_laps(active_session, clean_lap_numbers, (setup.peak_power_rpm_low, setup.peak_power_rpm_high))
    fig_band = go.Figure()
    fig_band.add_trace(go.Bar(x=band_summary["lap_number"], y=band_summary["fraction_in_band"] * 100))
    fig_band.update_layout(xaxis_title="Lap", yaxis_title="% of lap in peak-power band", height=350)
    st.plotly_chart(fig_band, width='stretch')
    render_footer()


def page_corner_comparison() -> None:
    if not _require_data():
        return
    st.subheader("Corner comparison")
    corner_options = segments.loc[segments["kind"] == "corner", "label"].tolist()
    if not corner_options:
        st.info("No corners detected in this session's reference lap.")
    else:
        st.caption(
            "Compares one corner across every loaded session, matched by GPS position (not just order), "
            "so it stays correct even when a session detects a different number of corners than this one."
        )
        selected_corner_label = st.selectbox("Corner to analyze", corner_options, key="corner_cmp_select")
        compare_lap = st.selectbox(
            "Lap to compare (from the active session)", clean_lap_numbers,
            index=clean_lap_numbers.index(analyzed_lap) if analyzed_lap in clean_lap_numbers else 0,
            key="corner_cmp_lap", format_func=format_lap_option,
        )

        active_midpoints = segment_midpoints(_best_lap_trace, segments)
        ref_row = active_midpoints[active_midpoints["segment_label"] == selected_corner_label].iloc[0]
        reference_lat, reference_lon = float(ref_row["mid_lat"]), float(ref_row["mid_lon"])

        sessions_data = []
        for label, s in all_sessions:
            s_laps = clean_lap_table(compute_clean_laps(s))
            if s_laps.empty:
                continue
            s_clean_nums = s_laps["lap_number"].tolist()
            s_best_lap = int(s_laps.loc[s_laps["lap_time_s"].idxmin(), "lap_number"])
            s_segments, s_midpoints = build_segments_and_midpoints_cached(s, session_cache_key(s), s_best_lap)
            sessions_data.append((label, s, s_segments, s_midpoints, s_clean_nums))

        cache_key = tuple(session_cache_key(s) for _, s in all_sessions) + (round(reference_lat, 6), round(reference_lon, 6))
        comparison_df = compute_corner_comparison_cached(sessions_data, cache_key, reference_lat, reference_lon)

        if comparison_df.empty:
            st.info("No data available for this corner -- it may not exist in enough loaded sessions.")
        else:
            metric_cols = ["corner_time_s", "entry_speed_kmh", "entry_rpm", "apex_speed_kmh", "apex_rpm", "exit_speed_kmh", "exit_rpm"]

            all_time_best_row = comparison_df.loc[comparison_df["corner_time_s"].idxmin()]
            session_rows = comparison_df[comparison_df["session_label"] == active_label]
            session_best_row = session_rows.loc[session_rows["corner_time_s"].idxmin()] if not session_rows.empty else None
            selected_candidates = session_rows[session_rows["lap_number"] == compare_lap]
            selected_row = selected_candidates.iloc[0] if not selected_candidates.empty else None

            if selected_row is None:
                st.info(f"Lap {compare_lap} has no data for this corner (likely an outlier lap or missing GPS coverage there).")
            else:
                gain_vs_session_best = selected_row["corner_time_s"] - (session_best_row["corner_time_s"] if session_best_row is not None else float("nan"))
                gain_vs_all_time_best = selected_row["corner_time_s"] - all_time_best_row["corner_time_s"]

                c1, c2 = st.columns(2)
                c1.metric(
                    "Potential gain vs. session best",
                    f"{gain_vs_session_best:.3f}s" if pd.notna(gain_vs_session_best) else "n/a",
                )
                c2.metric(
                    "Potential gain vs. all-time best",
                    f"{gain_vs_all_time_best:.3f}s",
                    help=f"All-time best from {all_time_best_row['session_label']}, lap {int(all_time_best_row['lap_number'])}.",
                )

                table_rows = {
                    f"Lap {compare_lap} (selected)": selected_row[metric_cols],
                    f"Session best (lap {int(session_best_row['lap_number'])})" if session_best_row is not None else "Session best": (
                        session_best_row[metric_cols] if session_best_row is not None else pd.Series({c: np.nan for c in metric_cols})
                    ),
                    f"All-time best ({all_time_best_row['session_label']}, lap {int(all_time_best_row['lap_number'])})": all_time_best_row[metric_cols],
                }
                comparison_table = pd.DataFrame(table_rows).T
                comparison_table = comparison_table.round(
                    {"corner_time_s": 3, "entry_speed_kmh": 1, "entry_rpm": 0, "apex_speed_kmh": 1, "apex_rpm": 0, "exit_speed_kmh": 1, "exit_rpm": 0}
                )
                st.dataframe(prettify_columns(comparison_table), width='stretch')

            st.subheader(f"Where {selected_corner_label} is on track")
            corner_row = segments[segments["label"] == selected_corner_label].iloc[0]
            fig_where = go.Figure()
            fig_where.add_trace(
                go.Scatter(
                    x=_best_lap_trace["Longitude"], y=_best_lap_trace["Latitude"], mode="lines",
                    line=dict(color="lightgray", width=2), hoverinfo="skip", showlegend=False,
                )
            )
            in_corner = _best_lap_trace[
                (_best_lap_trace["lap_distance_m"] >= corner_row["start_m"]) & (_best_lap_trace["lap_distance_m"] < corner_row["end_m"])
            ]
            fig_where.add_trace(
                go.Scatter(
                    x=in_corner["Longitude"], y=in_corner["Latitude"], mode="lines",
                    line=dict(color="#d62728", width=5), hoverinfo="skip", showlegend=False,
                )
            )
            # Every segment labeled, same as the Top 3 Focus Areas track map,
            # so it's obvious at a glance which corner is being discussed
            # relative to the rest of the track -- not just a highlighted
            # squiggle with no surrounding context.
            where_labels = active_midpoints["segment_label"].str.replace("Corner ", "C", regex=False).str.replace("Straight ", "S", regex=False)
            is_selected = active_midpoints["segment_label"] == selected_corner_label
            fig_where.add_trace(
                go.Scatter(
                    x=active_midpoints["mid_lon"], y=active_midpoints["mid_lat"],
                    mode="markers+text",
                    text=where_labels,
                    textposition="top center",
                    marker=dict(
                        size=[18 if sel else 10 for sel in is_selected],
                        color=["#d62728" if sel else "#1f77b4" for sel in is_selected],
                        line=dict(width=1, color="black"),
                    ),
                    hovertext=active_midpoints["segment_label"],
                    hoverinfo="text",
                    showlegend=False,
                )
            )
            fig_where.update_layout(xaxis_title="Longitude", yaxis_title="Latitude", height=400, yaxis=dict(scaleanchor="x"))
            st.plotly_chart(fig_where, width='stretch')

            with st.expander(f"All laps analyzed for {selected_corner_label} ({len(comparison_df)} rows across {comparison_df['session_label'].nunique()} session(s))"):
                st.dataframe(prettify_columns(comparison_df.sort_values("corner_time_s")), width='stretch')
    render_footer()


def page_gearing_simulation() -> None:
    if not _require_data():
        return
    st.subheader("Gearing change simulation")
    st.caption(
        "Re-estimates RPM, speed, and lap time for a different front/rear sprocket combination, built "
        "entirely from this session's own telemetry -- there's no dyno power curve in this data. Braking "
        "points and racing line are held fixed; only the engine's RPM at a given speed changes, and the "
        "acceleration this session actually showed at that RPM. Treat the lap-time number as a directional "
        "estimate, not a guarantee -- see \"How this estimate works\" below."
    )

    sim_lap = st.selectbox("Lap to simulate", clean_lap_numbers, index=clean_lap_numbers.index(best_lap), key="sim_lap", format_func=format_lap_option)
    c1, c2 = st.columns(2)
    rear_delta = c1.number_input(
        "Δ rear sprocket teeth", value=1, step=1,
        help="Positive = add teeth (raises RPM everywhere, lowers top speed). Negative = remove teeth.",
    )
    front_delta = c2.number_input("Δ front (clutch) teeth", value=0, step=1)

    current_front = setup.gearing.front_teeth or 12
    current_rear = setup.gearing.rear_teeth or 80
    new_front = max(current_front + front_delta, 1)
    new_rear = max(current_rear + rear_delta, 1)

    if rear_delta == 0 and front_delta == 0:
        st.info("Set a tooth change above to simulate its effect (defaults to +1 rear tooth).")
    else:
        speed_rpm_scale = fit_speed_rpm_scale_cached(active_session, session_cache_key(active_session), tuple(clean_lap_numbers))
        accel_curve = build_accel_rpm_curve_cached(active_session, session_cache_key(active_session), tuple(clean_lap_numbers))

        if speed_rpm_scale is None or accel_curve.empty:
            st.warning("Not enough RPM / speed / G-force data in this session to build a gearing simulation.")
        else:
            sim_trace = simulate_gearing_change(active_session, sim_lap, setup, rear_delta, front_delta, speed_rpm_scale, accel_curve)
            if sim_trace.empty:
                st.warning("Couldn't build a simulated trace for this lap (missing GPS/RPM data).")
            else:
                actual_lap_time_s = float(laps.loc[laps["lap_number"] == sim_lap, "lap_time_s"].iloc[0])
                delta_result = estimate_lap_time_delta(sim_trace, actual_lap_time_s)
                delta_s = delta_result["delta_s"]

                c1, c2, c3 = st.columns(3)
                c1.metric("Current ratio", f"{current_rear}/{current_front} = {current_rear / current_front:.3f}")
                c2.metric("Simulated ratio", f"{new_rear}/{new_front} = {new_rear / new_front:.3f}")
                c3.metric("Estimated lap time", f"{delta_result['sim_lap_time_s']:.2f}s", delta=f"{delta_s:+.2f}s", delta_color="inverse")

                max_sim_rpm = sim_trace["rpm_sim"].max()
                extrapolated = pd.notna(max_sim_rpm) and not accel_curve.empty and max_sim_rpm > accel_curve["rpm_bin_center"].max()
                beats_theoretical_best = delta_result["sim_lap_time_s"] < theoretical_best_s
                if extrapolated or beats_theoretical_best:
                    warning_lines = []
                    if extrapolated:
                        warning_lines.append(
                            f"Simulated RPM reaches {max_sim_rpm:.0f}, above the {accel_curve['rpm_bin_center'].max():.0f} RPM "
                            "this session actually reached. That part of the estimate assumes acceleration "
                            "capability stays the same as the highest RPM this session ever measured -- a real "
                            "engine's acceleration typically falls off as it approaches its rev limiter, which "
                            "this simulation has no way to know about from data that never reached there, so the "
                            "estimated gain above is likely optimistic."
                        )
                    if beats_theoretical_best:
                        warning_lines.append(
                            f"The estimated lap time ({delta_result['sim_lap_time_s']:.2f}s) is faster than this "
                            f"session's theoretical best ({theoretical_best_s:.2f}s, the sum of the best-ever "
                            "segment across every clean lap) -- a strong sign this particular estimate is "
                            "overstated, most likely for the extrapolation reason above."
                        )
                    st.warning(" ".join(warning_lines))

                fig_rpm = go.Figure()
                fig_rpm.add_trace(go.Scatter(x=sim_trace["distance_m"], y=sim_trace["rpm_actual"], mode="lines", name="Current gearing", line=dict(color="#1f77b4")))
                fig_rpm.add_trace(go.Scatter(x=sim_trace["distance_m"], y=sim_trace["rpm_sim"], mode="lines", name="Simulated gearing", line=dict(color="#d62728")))
                fig_rpm.add_hrect(y0=setup.peak_power_rpm_low, y1=setup.peak_power_rpm_high, fillcolor="green", opacity=0.1, line_width=0)
                fig_rpm.update_layout(xaxis_title="Distance (m)", yaxis_title="RPM", height=380, title="RPM: current vs. simulated gearing")
                st.plotly_chart(fig_rpm, width='stretch')

                fig_speed = go.Figure()
                fig_speed.add_trace(go.Scatter(x=sim_trace["distance_m"], y=sim_trace["speed_kmh_actual"], mode="lines", name="Current gearing", line=dict(color="#1f77b4")))
                fig_speed.add_trace(go.Scatter(x=sim_trace["distance_m"], y=sim_trace["speed_kmh_sim"], mode="lines", name="Simulated gearing", line=dict(color="#d62728")))
                fig_speed.update_layout(xaxis_title="Distance (m)", yaxis_title="Speed (km/h)", height=380, title="Speed: current vs. simulated gearing")
                st.plotly_chart(fig_speed, width='stretch')

                band = (setup.peak_power_rpm_low, setup.peak_power_rpm_high)
                actual_in_band = sim_trace["rpm_actual"].between(*band).mean()
                sim_in_band = sim_trace["rpm_sim"].between(*band).mean()
                c1, c2 = st.columns(2)
                c1.metric("Time in peak-power band (current)", f"{actual_in_band:.0%}")
                c2.metric("Time in peak-power band (simulated)", f"{sim_in_band:.0%}", delta=f"{(sim_in_band - actual_in_band) * 100:+.0f}pp")

                with st.expander("How this estimate works, and what it can't account for"):
                    st.markdown(
                        "- **RPM** at each point is rescaled by the ratio change: engine RPM = axle RPM × "
                        "(rear teeth / front teeth), and axle RPM only depends on road speed and tyre size, "
                        "not gearing -- so a ratio change scales RPM at any given speed directly.\n"
                        "- **Acceleration** at each simulated RPM is looked up from a curve built from this "
                        "session's own power-on samples (RPM vs. longitudinal G), used as a stand-in for a "
                        "torque/power curve, which the export doesn't provide.\n"
                        "- **Speed** is then re-integrated forward through each power-on zone using that "
                        "looked-up acceleration, so a change in accel capability at the new RPM changes the "
                        "simulated speed for the rest of the straight -- but braking points and coast-down "
                        "phases replay the *actual* recorded deceleration unchanged, since gearing doesn't "
                        "affect brake bite.\n"
                        "- This assumes the same racing line, braking points, and driver inputs as the lap "
                        "being simulated, and that the accel-vs-RPM relationship itself doesn't shift with "
                        "the new gearing (traction, wheelspin, and engine response can all change a little in "
                        "reality). Treat the lap-time number as directional, not a guarantee.\n"
                        "- **RPM beyond what this session ever measured is extrapolated flat** -- the "
                        "acceleration curve simply repeats its highest-measured-RPM value rather than modeling "
                        "any fall-off, since there's no data to show what fall-off looks like. A real engine "
                        "generally loses acceleration as it nears its rev limiter, so any part of the estimate "
                        "relying on RPM above the session's measured range (flagged above when it happens) is "
                        "the most likely to be optimistic."
                    )
    render_footer()


def page_consistency() -> None:
    if not _require_data():
        return
    st.subheader("Lap time consistency")
    stats = consistency_stats(laps)
    c1, c2 = st.columns(2)
    c1.metric("Std dev", f"{stats.get('std_dev_s', 0):.2f}s")
    c2.metric("Trend", stats.get("trend_direction", "n/a"))
    fig6 = go.Figure()
    fig6.add_trace(go.Bar(x=laps["lap_number"], y=laps["lap_time_s"], marker_color=["crimson" if o else "steelblue" for o in laps["is_outlier"]]))
    fig6.update_layout(xaxis_title="Lap", yaxis_title="Lap time (s)", height=400)
    st.plotly_chart(fig6, width='stretch')
    st.caption("Red bars are flagged as outliers (in/out lap or statistical anomaly) and excluded from best/average stats.")
    render_footer()


def page_progression() -> None:
    if not _require_data():
        return
    st.subheader("Session-over-session progression")
    if len(all_sessions) < 2:
        st.info("Load more than one session (or a file with multiple sessions) to see progression across sessions.")
    else:
        progression = session_progression(all_sessions)
        st.dataframe(prettify_columns(progression), width='stretch')
        fig7 = go.Figure()
        fig7.add_trace(go.Scatter(x=progression["session"], y=progression["best_lap_s"], mode="lines+markers", name="Best lap"))
        fig7.add_trace(go.Scatter(x=progression["session"], y=progression["average_lap_s"], mode="lines+markers", name="Average lap"))
        fig7.update_layout(xaxis_title="Session", yaxis_title="Lap time (s)", height=400)
        st.plotly_chart(fig7, width='stretch')

        st.subheader("Recurring weaknesses across loaded sessions")
        per_session_focus = {}
        for label, s in all_sessions:
            s_laps = clean_lap_table(compute_clean_laps(s))
            if s_laps.empty:
                continue
            s_clean_nums = tuple(s_laps["lap_number"].tolist())
            s_best_lap = int(s_laps.loc[s_laps["lap_time_s"].idxmin(), "lap_number"])
            per_session_focus[label] = compute_session_top_focus_areas_cached(
                s, session_cache_key(s), s_clean_nums, s_best_lap
            )
        recurring = recurring_weaknesses(per_session_focus)
        if recurring.empty:
            st.info("No segment shows up as a top-3 focus area in more than one loaded session yet.")
        else:
            st.dataframe(prettify_columns(recurring), width='stretch')
            st.caption("Segments appearing here are a recurring habit across sessions, not a one-off mistake.")
    render_footer()


def page_kart_setup() -> None:
    global setup
    if not _require_data():
        return
    st.subheader("Kart setup")
    st.caption(
        f"Setup for **{active_label}** specifically -- other sessions keep their own (see the session picker "
        "in the sidebar). Edit and re-save any time; changes update the Top 3 Focus Areas and correlation "
        "suggestions below on the next run, and are remembered for next time you open this session."
    )

    with st.form("setup_form"):
        edited_setup = render_setup_fields(st.session_state.kart_setup)
        submitted = st.form_submit_button("Save setup & re-run correlation engine")

    if submitted:
        st.session_state.kart_setup = edited_setup
        setup = edited_setup
        library.save_kart_setup(edited_setup, *active_session_key, driver=active_session.driver)
        st.success(f"Setup saved for {active_label} -- remembered for next time (see History page), and reflected in Top 3 Focus Areas on the next run.")

    yaml_bytes = io.BytesIO()
    yaml_bytes.write(yaml.safe_dump(setup.to_dict(), sort_keys=False).encode())
    st.download_button("Download setup as YAML", yaml_bytes.getvalue(), file_name="kart_setup.yaml")

    st.subheader("Setup correlation suggestions")
    for s in setup_suggestions:
        with st.expander(f"{s['area'].replace('_', ' ').title()} — confidence: {s['confidence']}"):
            st.write(s.get("hypothesis", ""))
            if s.get("suggested_action"):
                st.markdown(f"**Suggested action:** {s['suggested_action']}")
            st.caption("This is a hypothesis inferred from telemetry patterns, not a direct sensor confirmation -- verify before acting on it.")
    render_footer()


def page_history() -> None:
    if not _require_data():
        return
    st.subheader("Session history")
    if flash := st.session_state.pop("history_delete_result", None):
        st.success(flash)
    st.caption(
        "Every session uploaded on the Settings page is saved here so you can track progression over time, "
        "no re-uploading needed. Note: this storage lives on the app's local disk, which is wiped on every "
        "redeploy/reboot -- treat it as a within-deploy convenience for now, not durable long-term history."
    )
    session_history = library.list_sessions()
    if session_history.empty:
        st.info("No sessions saved yet.")
    else:
        display_history = session_history[
            ["id", "source_file", "driver", "track_name", "session_type", "start_date", "start_time", "best_lap_s", "average_lap_s", "n_laps", "ingested_at"]
        ].sort_values("ingested_at", ascending=False)
        st.dataframe(prettify_columns(display_history), width='stretch')

        by_id = display_history.set_index("id")
        delete_id = st.selectbox(
            "Delete a session",
            display_history["id"],
            format_func=lambda i: (
                f"#{i} — {by_id.loc[i, 'driver']} — {by_id.loc[i, 'source_file']} session "
                f"{by_id.loc[i, 'start_date']} {by_id.loc[i, 'start_time']}"
            ),
            key="history_delete_select",
        )
        if st.session_state.get("_confirm_delete_session_id") != delete_id:
            if st.button("🗑️ Delete session", key="history_delete_btn"):
                st.session_state["_confirm_delete_session_id"] = delete_id
                st.rerun()
        else:
            st.warning("This permanently deletes the session's telemetry and lap data (not its kart setup history) -- this can't be undone.")
            dc1, dc2 = st.columns(2)
            if dc1.button("Yes, delete it", key="history_delete_confirm"):
                library.delete_session(int(delete_id))
                del st.session_state["_confirm_delete_session_id"]
                st.session_state["history_delete_result"] = "Session deleted."
                st.rerun()
            if dc2.button("Cancel", key="history_delete_cancel"):
                del st.session_state["_confirm_delete_session_id"]
                st.rerun()

    st.subheader("Kart setup history")
    st.caption("Setups are saved per session (see the Kart Setup page) -- every snapshot ever saved, across every session, is listed here.")
    setup_history = library.list_kart_setups()
    if setup_history.empty:
        st.info("No setup snapshots saved yet.")
    else:
        st.dataframe(prettify_columns(setup_history), width='stretch')
        restore_id = st.selectbox(
            f"Copy a past setup into the active session ({active_label})",
            setup_history["id"],
            format_func=lambda i: (
                f"#{i} — {setup_history.set_index('id').loc[i, 'source_file']} session "
                f"{setup_history.set_index('id').loc[i, 'session_index']} — {setup_history.set_index('id').loc[i, 'saved_at']}"
            ),
        )
        if st.button("Copy selected setup into this session"):
            st.session_state.kart_setup = library.load_kart_setup(int(restore_id))
            st.success(f"Copied into {active_label} -- open the Kart Setup page to review and save it there.")
            st.rerun()
    render_footer()


def page_settings() -> None:
    st.title("⚙️ Settings")
    if flash := st.session_state.pop("settings_upload_result", None):
        st.success(flash)
    st.caption(
        "Uploaded files are saved into your session library, so they persist across reruns and app restarts -- "
        "no need to re-upload the same file next time. Upload each driver's files separately (with their name "
        "below) to build up sessions you can compare across drivers, same as comparing across your own sessions."
    )

    existing = library.list_sessions()
    if not existing.empty:
        n_drivers = existing["driver"].nunique()
        st.caption(f"📚 Library currently has {len(existing)} session(s) from {n_drivers} driver(s).")

    dc1, dc2 = st.columns(2)
    driver_input = dc1.text_input(
        "Driver name for this upload", value="", placeholder="e.g. Alex Smith", key="settings_driver_name",
    )
    track_input = dc2.text_input(
        "Track name for this upload", value="", placeholder="e.g. Jyllandsringen", key="settings_track_name",
    )
    uploaded_files = st.file_uploader(
        "Upload Unipro TSV export(s)", type=["tsv", "txt"], accept_multiple_files=True, key="settings_uploader",
    )

    missing = [
        field for field, value in (("driver name", driver_input), ("track name", track_input)) if not value.strip()
    ]
    if uploaded_files and missing:
        st.warning(f"Enter a {' and '.join(missing)} above before loading -- every saved session needs both.")
    elif uploaded_files and st.button("Load file(s) into session library"):
        driver, track = driver_input.strip(), track_input.strip()
        added = 0
        with st.spinner(f"Parsing and saving {len(uploaded_files)} file(s)..."):
            for f in uploaded_files:
                for s in parse_uploaded_file(f.getvalue(), f.name):
                    if library.find_session(s.source_file, s.session_id, s.start_time, driver=driver) is not None:
                        continue
                    s.driver = driver
                    library.save_session(s, driver=driver, track_name=track)
                    added += 1
        if added:
            # Flashed on the *next* run instead of shown here directly --
            # the rerun below is what makes the sidebar's session picker and
            # every other page see the new sessions immediately, but it also
            # wipes any message set on this run before the user ever sees it.
            st.session_state["settings_upload_result"] = f"Loaded {added} new session(s) for {driver} at {track}."
            st.rerun()
        else:
            st.info("These sessions were already in your library -- nothing new to add.")


# ---------------------------------------------------------------------------
# Sidebar navigation + shared data loading
#
# st.navigation()/st.Page() render the sidebar menu (mobile-friendly out of
# the box, unlike a horizontal st.radio row that wraps across two lines on
# a narrow screen) and return a Page whose .run() -- called at the very end
# of this script -- executes just the selected page's function body. Data
# that needs to survive a page switch (uploaded sessions, driver name) lives
# in st.session_state, written from page_settings() and read here
# unconditionally on every rerun regardless of which page is selected.
# ---------------------------------------------------------------------------

st.sidebar.title("🏎️ Karting Telemetry")

page_overview_obj = st.Page(page_overview, title="Top 3 Focus Areas", icon="🎯", default=True)
page_lap_times_obj = st.Page(page_lap_times, title="Lap Times", icon="⏱️")
page_data_analysis_obj = st.Page(page_data_analysis, title="Data Analysis", icon="📈")
page_track_map_obj = st.Page(page_track_map, title="Track Map", icon="🗺️")
page_braking_rpm_obj = st.Page(page_braking_rpm, title="Braking / RPM", icon="🛞")
page_corner_comparison_obj = st.Page(page_corner_comparison, title="Corner Comparison", icon="📐")
page_gearing_simulation_obj = st.Page(page_gearing_simulation, title="Gearing Simulation", icon="🧮")
page_consistency_obj = st.Page(page_consistency, title="Consistency", icon="📊")
page_progression_obj = st.Page(page_progression, title="Progression", icon="📅")
page_kart_setup_obj = st.Page(page_kart_setup, title="Kart Setup", icon="🔧")
page_history_obj = st.Page(page_history, title="History", icon="🗂️")
page_settings_obj = st.Page(page_settings, title="Settings", icon="⚙️")

nav = st.navigation(
    {
        "Analysis": [
            page_overview_obj, page_lap_times_obj, page_data_analysis_obj, page_track_map_obj,
            page_braking_rpm_obj, page_corner_comparison_obj, page_gearing_simulation_obj,
            page_consistency_obj, page_progression_obj, page_kart_setup_obj, page_history_obj,
        ],
        "Settings": [page_settings_obj],
    }
)

st.sidebar.divider()

library = get_session_library()

sessions_meta = library.list_sessions()
if sessions_meta.empty and os.path.exists(DEFAULT_TSV_PATH):
    # Build-phase convenience: a default file ships with the app so it
    # doesn't need uploading on the very first visit. This ingests it into
    # the same session library real uploads use, exactly once ever -- every
    # later run finds it already there via `sessions_meta` above and skips
    # straight to loading, rather than re-parsing the raw TSV on every visit.
    # The parse step alone runs ~10s on this file (shows its own spinner,
    # see parse_uploaded_file); saving 11 sessions to the library on top of
    # that adds a few more seconds with no feedback of its own otherwise --
    # wrapped here so that doesn't read as a silent hang.
    with st.spinner("Setting up your session library for the first time (this happens once)..."):
        with open(DEFAULT_TSV_PATH, "rb") as f:
            default_bytes = f.read()
        for s in parse_uploaded_file(default_bytes, os.path.basename(DEFAULT_TSV_PATH)):
            s.driver = "Sample Driver"
            library.save_session(s, driver="Sample Driver")
        sessions_meta = library.list_sessions()

# A tuple of DB ids, not the DataFrame itself, so this stays cheap to
# recompute every rerun while still giving load_persisted_sessions_cached a
# real cache-invalidation signal -- it only redoes the (comparatively
# expensive) unpickling work when a session is actually added or removed.
sessions_meta_key = tuple(sessions_meta["id"]) if not sessions_meta.empty else ()
all_sessions: list[tuple[str, Session]] = load_persisted_sessions_cached(library, sessions_meta, sessions_meta_key)

data_ready = False
data_error_message: str | None = None
active_session = None
active_label = None
active_session_key = None
setup: KartSetup | None = None
laps = pd.DataFrame()
clean = pd.DataFrame()
clean_lap_numbers: list[int] = []
best_lap = None
analyzed_lap = None
lap_time_by_number: dict[int, float] = {}
segments = pd.DataFrame()
theoretical_best_s = None
best_segment_times = None
lap_segment_times = None
summary = None
setup_suggestions: list[dict] = []
_best_lap_trace = pd.DataFrame()
speed_is_estimated = False

if all_sessions:
    session_labels = [label for label, _ in all_sessions]

    # Default to the session with the single fastest clean lap. Only
    # recomputed when the loaded session set actually changes (not on every
    # rerun/slider drag) -- fastest_lap_session_label loops over every
    # session's laps, and Streamlit reruns this whole script on every
    # interaction regardless of which page is open.
    if st.session_state.get("_session_labels_seen") != session_labels:
        st.session_state["_session_labels_seen"] = session_labels
        st.session_state["_session_best_times"] = session_best_lap_times(all_sessions)
        st.session_state["_default_session_label"] = fastest_lap_session_label(st.session_state["_session_best_times"])

    session_best_times: dict[str, float | None] = st.session_state.get("_session_best_times", {})
    default_session_label = st.session_state.get("_default_session_label")
    default_session_index = session_labels.index(default_session_label) if default_session_label in session_labels else 0

    active_label = st.sidebar.selectbox(
        "Session to analyze", session_labels, index=default_session_index,
    )
    active_session = dict(all_sessions)[active_label]

    # Kart setup is stored per session, not globally -- different sessions on
    # the same track day can genuinely run different gearing/jetting/tyre
    # pressure, so a single "the" setup asked once upfront silently assumed
    # every session shared it. Reloaded only when the *active session itself*
    # changes (not on every rerun), same cache-invalidation pattern as the
    # session-picker default above; edits made in the Kart Setup page live in
    # session_state until explicitly saved, same as before.
    active_session_key = (active_session.source_file, active_session.session_id, active_session.start_time)
    if st.session_state.get("_kart_setup_session_key") != active_session_key:
        st.session_state["_kart_setup_session_key"] = active_session_key
        loaded_setup = library.load_latest_kart_setup_for_session(*active_session_key)
        st.session_state["kart_setup"] = loaded_setup if loaded_setup is not None else KartSetup(driver=active_session.driver)

    setup = st.session_state["kart_setup"]

    # No auto-save-on-select here: every session in `all_sessions` already
    # came from the library (see load_persisted_sessions_cached above), so
    # by construction there's nothing left to save the first time a session
    # is selected -- unlike before this page loaded sessions from a live
    # upload widget each rerun, uploading is now the only thing that saves.

    if st.sidebar.button("🔧 Edit kart setup"):
        st.switch_page(page_kart_setup_obj)

    laps = compute_clean_laps(active_session)
    clean = clean_lap_table(laps)

    if clean.empty:
        data_error_message = "No clean laps found in this session after outlier filtering -- check the file."
    else:
        clean_lap_numbers = clean["lap_number"].tolist()
        best_lap = int(clean.loc[clean["lap_time_s"].idxmin(), "lap_number"])

        # Shared by every lap-number selectbox/multiselect for the active
        # session (sidebar and every page) so a lap is never just a bare
        # number -- picking "which lap" without seeing its time meant
        # opening it first to find out.
        lap_time_by_number = dict(zip(laps["lap_number"], laps["lap_time_s"]))

        analyzed_lap = st.sidebar.selectbox(
            "Lap to analyze against theoretical best",
            clean_lap_numbers,
            index=clean_lap_numbers.index(best_lap),
            format_func=format_lap_option,
        )

        segments = build_reference_segments(active_session, best_lap)
        theoretical_best_s, best_segment_times = theoretical_best_lap(active_session, clean_lap_numbers, segments)
        lap_segment_times = segment_times_for_lap(active_session, analyzed_lap, segments)
        summary = summarize_laps(laps)
        setup_suggestions = compute_setup_suggestions_cached(
            active_session, session_cache_key(active_session), tuple(clean_lap_numbers), segments, setup
        )

        # Some real exports populate Latitude/Longitude/Heading on every GPS
        # fix but never the GPS Speed channel itself -- lap_gps_trace falls
        # back to deriving speed from GPS Distance in that case (see
        # corners.py), which is worth disclosing since it affects every
        # speed-based chart/metric in this app.
        _best_lap_trace = lap_gps_trace(active_session, best_lap)
        speed_is_estimated = bool(_best_lap_trace["gps_speed_is_estimate"].any()) if not _best_lap_trace.empty else False

        data_ready = True

nav.run()
