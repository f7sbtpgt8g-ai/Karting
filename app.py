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


def session_label(filename: str, session: Session) -> str:
    date = session.start_date or "?"
    time = session.start_time or "?"
    return f"{filename} — session {session.session_id} ({date} {time})"


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


def fastest_lap_session_label(sessions_with_labels: list[tuple[str, Session]]) -> str | None:
    """Which loaded session has the single fastest clean lap -- used to
    default the "Session to analyze" picker so the driver doesn't have to
    manually hunt for their best session out of a multi-session file."""
    best_label, best_time = None, float("inf")
    for label, s in sessions_with_labels:
        clean_s = clean_lap_table(compute_clean_laps(s))
        if clean_s.empty:
            continue
        t = clean_s["lap_time_s"].min()
        if t < best_time:
            best_time = t
            best_label = label
    return best_label


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
# Linked speed/RPM/delta trace + track map (Speed & Delta view)
# ---------------------------------------------------------------------------

def render_linked_speed_delta(chart_fig: go.Figure, map_fig: go.Figure, dist: list, lat: list, lon: list, height: int) -> None:
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
    metres) though, via `hovermode="x unified"` on `chart_fig`, so the
    hovered *distance* is what's used to place the marker -- linearly
    interpolated client-side into the map lap's own lat/lon arrays (`dist`/
    `lat`/`lon`), the same way the old slider-driven marker used
    `np.interp` server-side.
    """
    chart_spec = chart_fig.to_json()
    map_spec = map_fig.to_json()
    marker_trace_index = len(map_fig.data) - 1

    html = f"""
<div style="display:flex; gap:12px; width:100%; font-family:inherit;">
  <div id="chartDiv" style="flex:1 1 62%; min-width:0;"></div>
  <div id="mapDiv" style="flex:1 1 38%; min-width:0;"></div>
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
  chartSpec.layout.height = {height};
  mapSpec.layout.height = {height};
  Plotly.newPlot(chartDiv, chartSpec.data, chartSpec.layout, {{displayModeBar: false}});
  Plotly.newPlot(mapDiv, mapSpec.data, mapSpec.layout, {{displayModeBar: false}});

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
    var p = interpAt(evt.points[0].x);
    if (!p || p.lat == null || p.lon == null) return;
    Plotly.restyle(mapDiv, {{x: [[p.lon]], y: [[p.lat]]}}, [markerTraceIndex]);
  }});
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
    setup.gearing.front_teeth = c1.number_input("Front (clutch) teeth", value=setup.gearing.front_teeth or 10, step=1)
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
# Sidebar: file upload + context
# ---------------------------------------------------------------------------

st.sidebar.title("🏎️ Karting Telemetry")
uploaded_files = st.sidebar.file_uploader(
    "Upload Unipro TSV export(s)", type=["tsv", "txt"], accept_multiple_files=True
)
driver_name = st.sidebar.text_input("Driver name", value="Driver")

all_sessions: list[tuple[str, Session]] = []
if uploaded_files:
    for f in uploaded_files:
        for s in parse_uploaded_file(f.getvalue(), f.name):
            all_sessions.append((session_label(f.name, s), s))
elif os.path.exists(DEFAULT_TSV_PATH):
    # Build-phase convenience: a default file ships with the app so it
    # doesn't need re-uploading on every visit. Uploading anything in the
    # sidebar above takes over immediately (this branch only runs when
    # uploaded_files is empty) -- it does not overwrite the file on disk.
    st.sidebar.caption("📎 Using the default sample file (upload above to use your own).")
    with open(DEFAULT_TSV_PATH, "rb") as f:
        default_bytes = f.read()
    for s in parse_uploaded_file(default_bytes, os.path.basename(DEFAULT_TSV_PATH)):
        all_sessions.append((session_label(os.path.basename(DEFAULT_TSV_PATH), s), s))

if not all_sessions:
    st.title("Karting Telemetry Analysis")
    st.info(
        "Upload one or more Unipro laptimer TSV exports in the sidebar to get started. "
        "A single file may contain multiple sessions (the tool detects logger restarts automatically)."
    )
    st.markdown(
        "**What this tool does:** parses sparse/asynchronous Unipro telemetry, segments the track into "
        "corners from the GPS trace, and ranks where you're losing the most time -- with a plain-language "
        "coaching note for each. Load your kart setup right after uploading to get setup-change hypotheses "
        "folded into that ranking too."
    )
    st.stop()

library = get_session_library()


# ---------------------------------------------------------------------------
# Upfront kart setup gate: asked once per file load, before any analysis is
# shown, so medium/high-confidence setup hypotheses can be folded into the
# Top 3 Focus Areas ranking rather than living only in a separate tab.
# ---------------------------------------------------------------------------

if "kart_setup" not in st.session_state:
    # Pre-fill from the most recently saved setup in the history library, if
    # any, rather than always starting blank -- this is what makes values
    # actually "stick" across visits within this deploy's lifetime.
    saved_setup = library.load_latest_kart_setup()
    st.session_state.kart_setup = saved_setup if saved_setup is not None else KartSetup(driver=driver_name)
if "setup_confirmed" not in st.session_state:
    st.session_state.setup_confirmed = False

if not st.session_state.setup_confirmed:
    st.title("Kart Setup")
    st.info(
        "Tell us your kart setup before diving into the analysis. If it points to a likely gearing, "
        "jetting, tyre-pressure, or chassis-balance issue, that'll be folded straight into your Top 3 "
        "Focus Areas rather than buried in a separate tab. You can skip this and fill it in later from "
        "the Kart Setup tab. Saved setups are remembered for next time (see the History tab)."
    )
    with st.form("onboarding_setup_form"):
        edited_setup = render_setup_fields(st.session_state.kart_setup)
        col_a, col_b = st.columns(2)
        continue_clicked = col_a.form_submit_button("Continue to analysis", type="primary")
        skip_clicked = col_b.form_submit_button("Skip for now (use defaults)")

    if continue_clicked:
        st.session_state.kart_setup = edited_setup
        st.session_state.setup_confirmed = True
        library.save_kart_setup(edited_setup, driver=driver_name)
        st.rerun()
    if skip_clicked:
        st.session_state.setup_confirmed = True
        st.rerun()
    st.stop()

setup: KartSetup = st.session_state.kart_setup


session_labels = [label for label, _ in all_sessions]

# Default to the session with the single fastest clean lap. Only recomputed
# when the loaded session set actually changes (not on every rerun/slider
# drag) -- fastest_lap_session_label loops over every session's laps, and
# Streamlit reruns this whole script on every interaction regardless of tab.
if st.session_state.get("_session_labels_seen") != session_labels:
    st.session_state["_session_labels_seen"] = session_labels
    st.session_state["_default_session_label"] = fastest_lap_session_label(all_sessions)

default_session_label = st.session_state.get("_default_session_label")
default_session_index = session_labels.index(default_session_label) if default_session_label in session_labels else 0

active_label = st.sidebar.selectbox("Session to analyze", session_labels, index=default_session_index)
active_session = dict(all_sessions)[active_label]

# Auto-save only the session actually being analyzed, not every session in
# a multi-session file upfront -- saving pickles the full raw dataframe to
# disk per session, and doing that eagerly for all 11 sessions in a real
# multi-session file blocked the very first render for minutes. Switching
# the "Session to analyze" picker saves whichever session is newly selected,
# so browsing through sessions still builds up history over a visit.
if library.find_session(active_session.source_file, active_session.session_id, active_session.start_time) is None:
    library.save_session(active_session, driver=driver_name, track_name=st.session_state.get("kart_setup", KartSetup()).track_session.track_name)

if st.sidebar.button("⚙️ Edit kart setup"):
    st.session_state.setup_confirmed = False
    st.rerun()

laps = compute_clean_laps(active_session)
clean = clean_lap_table(laps)

if clean.empty:
    st.error("No clean laps found in this session after outlier filtering -- check the file.")
    st.stop()

clean_lap_numbers = clean["lap_number"].tolist()
best_lap = int(clean.loc[clean["lap_time_s"].idxmin(), "lap_number"])

analyzed_lap = st.sidebar.selectbox(
    "Lap to analyze against theoretical best",
    clean_lap_numbers,
    index=clean_lap_numbers.index(best_lap),
)

segments = build_reference_segments(active_session, best_lap)
theoretical_best_s, best_segment_times = theoretical_best_lap(active_session, clean_lap_numbers, segments)
lap_segment_times = segment_times_for_lap(active_session, analyzed_lap, segments)
summary = summarize_laps(laps)
setup_suggestions = compute_setup_suggestions_cached(
    active_session, session_cache_key(active_session), tuple(clean_lap_numbers), segments, setup
)

# Some real exports populate Latitude/Longitude/Heading on every GPS fix but
# never the GPS Speed channel itself -- lap_gps_trace falls back to deriving
# speed from GPS Distance in that case (see corners.py), which is worth
# disclosing since it affects every speed-based chart/metric in this app.
_best_lap_trace = lap_gps_trace(active_session, best_lap)
speed_is_estimated = bool(_best_lap_trace["gps_speed_is_estimate"].any()) if not _best_lap_trace.empty else False


# ---------------------------------------------------------------------------
# Headline: Top 3 focus areas (blends corner time-loss with medium/high
# confidence setup hypotheses)
# ---------------------------------------------------------------------------

st.title(f"{driver_name} — Top 3 Focus Areas")
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
    st.dataframe(breakdown_display, width='stretch')

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

st.divider()


# ---------------------------------------------------------------------------
# Tabs: deeper technical views
#
# Deliberately NOT st.tabs(): every `with tabs[i]:` block's code executes on
# *every* script rerun regardless of which tab is visually selected (a
# documented Streamlit behavior), and empirically, once this app's combined
# per-tab content (large Plotly figures, big tables, cross-session loops)
# got heavy enough across 9 tabs, the last couple of tabs stopped rendering
# at all -- no exception, content just silently never arrived client-side.
# A minimal repro with 9 trivial st.tabs() worked fine, and moving a real
# library call to tab index 0 worked fine too, isolating the cause to
# cumulative per-run payload/compute across *all* tabs, not any specific
# tab's code. A plain radio-driven if/elif only executes the selected
# section, which sidesteps the problem entirely and is strictly less work
# every rerun besides.
# ---------------------------------------------------------------------------

selected_view = st.radio(
    "View",
    [
        "Lap Times", "Speed & Delta", "Track Map", "Braking / RPM", "Corner Comparison",
        "Gearing Simulation", "Consistency", "Progression", "Kart Setup", "History",
    ],
    horizontal=True,
    label_visibility="collapsed",
)

# --- Lap Times ---
if selected_view == "Lap Times":
    st.subheader("Lap time table")
    pb_across_loaded = min(
        clean_lap_table(compute_clean_laps(s))["lap_time_s"].min()
        for _, s in all_sessions
        if not clean_lap_table(compute_clean_laps(s)).empty
    )
    annotated = lap_time_with_deltas(laps, personal_best_s=pb_across_loaded)
    display_cols = ["lap_number", "lap_time_s", "delta_to_best_s", "delta_to_average_s", "delta_to_personal_best_s", "is_outlier", "outlier_reason", "likely_incident"]
    display_cols = [c for c in display_cols if c in annotated.columns]
    st.dataframe(annotated[display_cols], width='stretch')
    st.caption("Rows flagged `is_outlier` are excluded from best/average stats above but shown here for review.")

# --- Speed & Delta ---
elif selected_view == "Speed & Delta":
    st.subheader("Speed, RPM & delta trace")
    compare_laps = st.multiselect("Laps to overlay", clean_lap_numbers, default=clean_lap_numbers[: min(4, len(clean_lap_numbers))])
    reference_lap = st.selectbox("Reference lap (for delta)", clean_lap_numbers, index=clean_lap_numbers.index(best_lap), key="ref_lap_delta")

    if not compare_laps:
        st.info("Select at least one lap to overlay.")
    else:
        colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f"]
        lap_colors = {lap_no: colors[i % len(colors)] for i, lap_no in enumerate(compare_laps)}

        fig = make_subplots(
            rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06,
            subplot_titles=("Speed (km/h)", "RPM", "Delta vs reference (s) — positive = time lost"),
        )
        lap_traces: dict[int, pd.DataFrame] = {}
        for lap_no in compare_laps:
            trace = lap_metric_trace(active_session, lap_no)
            lap_traces[lap_no] = trace
            color = lap_colors[lap_no]
            fig.add_trace(
                go.Scatter(x=trace["lap_distance_m"], y=trace["GPS Speed"], mode="lines", name=f"Lap {lap_no}", legendgroup=f"lap{lap_no}", line=dict(color=color)),
                row=1, col=1,
            )
            fig.add_trace(
                go.Scatter(x=trace["lap_distance_m"], y=trace["RPM"], mode="lines", name=f"Lap {lap_no} RPM", legendgroup=f"lap{lap_no}", line=dict(color=color), showlegend=False),
                row=2, col=1,
            )
            if lap_no != reference_lap:
                dt = delta_time_trace(active_session, lap_no, reference_lap)
                fig.add_trace(
                    go.Scatter(x=dt["distance_m"], y=dt["delta_s"], mode="lines", name=f"Lap {lap_no} delta", legendgroup=f"lap{lap_no}", line=dict(color=color), showlegend=False),
                    row=3, col=1,
                )
        fig.add_hline(y=0, row=3, col=1, line_dash="dash", line_color="gray")
        fig.update_xaxes(title_text="Distance (m)", row=3, col=1)
        fig.update_layout(hovermode="x unified")

        map_lap_choice = st.selectbox("Show position for lap", compare_laps, index=0, key="speed_tab_map_lap")
        primary_trace = lap_traces[map_lap_choice].dropna(subset=["lap_distance_m", "Latitude", "Longitude"]).sort_values("lap_distance_m")

        map_fig = go.Figure()
        map_fig.add_trace(
            go.Scattergl(
                x=primary_trace["Longitude"], y=primary_trace["Latitude"], mode="lines",
                line=dict(color=lap_colors[map_lap_choice], width=2), showlegend=False,
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

        st.caption(f"Hover the chart below to move the marker along lap {map_lap_choice}'s track position -- no need to read off the distance and find it manually.")
        render_linked_speed_delta(
            fig, map_fig,
            primary_trace["lap_distance_m"].tolist(), primary_trace["Latitude"].tolist(), primary_trace["Longitude"].tolist(),
            height=700,
        )

# --- Track Map ---
elif selected_view == "Track Map":
    st.subheader("Track map")
    map_lap = st.selectbox("Lap", clean_lap_numbers, index=clean_lap_numbers.index(best_lap), key="map_lap")
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

# --- Braking / RPM ---
elif selected_view == "Braking / RPM":
    st.subheader("Braking zones (inferred — no brake channel in this export)")
    brake_lap = st.selectbox("Lap", clean_lap_numbers, index=clean_lap_numbers.index(best_lap), key="brake_lap")
    trace = lap_metric_trace(active_session, brake_lap)
    trace = add_braking_throttle_estimates(trace)
    zones = braking_zones(trace)
    st.dataframe(zones, width='stretch')

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
    st.dataframe(agg[[c for c in display_cols if c in agg.columns]], width='stretch')

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

# --- Corner Comparison ---
elif selected_view == "Corner Comparison":
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
            key="corner_cmp_lap",
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
                st.dataframe(comparison_table, width='stretch')

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
            fig_where.update_layout(xaxis_title="Longitude", yaxis_title="Latitude", height=400, yaxis=dict(scaleanchor="x"))
            st.plotly_chart(fig_where, width='stretch')

            with st.expander(f"All laps analyzed for {selected_corner_label} ({len(comparison_df)} rows across {comparison_df['session_label'].nunique()} session(s))"):
                st.dataframe(comparison_df.sort_values("corner_time_s"), width='stretch')

# --- Gearing Simulation ---
elif selected_view == "Gearing Simulation":
    st.subheader("Gearing change simulation")
    st.caption(
        "Re-estimates RPM, speed, and lap time for a different front/rear sprocket combination, built "
        "entirely from this session's own telemetry -- there's no dyno power curve in this data. Braking "
        "points and racing line are held fixed; only the engine's RPM at a given speed changes, and the "
        "acceleration this session actually showed at that RPM. Treat the lap-time number as a directional "
        "estimate, not a guarantee -- see \"How this estimate works\" below."
    )

    sim_lap = st.selectbox("Lap to simulate", clean_lap_numbers, index=clean_lap_numbers.index(best_lap), key="sim_lap")
    c1, c2 = st.columns(2)
    rear_delta = c1.number_input(
        "Δ rear sprocket teeth", value=1, step=1,
        help="Positive = add teeth (raises RPM everywhere, lowers top speed). Negative = remove teeth.",
    )
    front_delta = c2.number_input("Δ front (clutch) teeth", value=0, step=1)

    current_front = setup.gearing.front_teeth or 10
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

# --- Consistency ---
elif selected_view == "Consistency":
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

# --- Progression ---
elif selected_view == "Progression":
    st.subheader("Session-over-session progression")
    if len(all_sessions) < 2:
        st.info("Load more than one session (or a file with multiple sessions) to see progression across sessions.")
    else:
        progression = session_progression(all_sessions)
        st.dataframe(progression, width='stretch')
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
            st.dataframe(recurring, width='stretch')
            st.caption("Segments appearing here are a recurring habit across sessions, not a one-off mistake.")

# --- Kart Setup ---
elif selected_view == "Kart Setup":
    st.subheader("Kart setup")
    st.caption("Edit and re-save your setup any time -- changes here update the Top 3 Focus Areas and correlation suggestions below on the next run, and are remembered for next time you open the app.")

    with st.form("setup_form"):
        edited_setup = render_setup_fields(st.session_state.kart_setup)
        submitted = st.form_submit_button("Save setup & re-run correlation engine")

    if submitted:
        st.session_state.kart_setup = edited_setup
        setup = edited_setup
        library.save_kart_setup(edited_setup, driver=driver_name)
        st.success("Setup saved -- remembered for next time (see History tab), and reflected in Top 3 Focus Areas above on the next run.")

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

# --- History ---
elif selected_view == "History":
    st.subheader("Session history")
    st.caption(
        "Every loaded session is auto-saved locally so you can track progression over time. "
        "Note: this storage lives on the app's local disk, which is wiped on every redeploy/reboot -- "
        "treat it as a within-deploy convenience for now, not durable long-term history."
    )
    session_history = library.list_sessions()
    if session_history.empty:
        st.info("No sessions saved yet.")
    else:
        display_history = session_history[
            ["id", "source_file", "driver", "track_name", "session_type", "start_date", "start_time", "best_lap_s", "average_lap_s", "n_laps", "ingested_at"]
        ].sort_values("ingested_at", ascending=False)
        st.dataframe(display_history, width='stretch')

    st.subheader("Kart setup history")
    setup_history = library.list_kart_setups()
    if setup_history.empty:
        st.info("No setup snapshots saved yet.")
    else:
        st.dataframe(setup_history, width='stretch')
        restore_id = st.selectbox("Restore a past setup", setup_history["id"], format_func=lambda i: f"#{i} — {setup_history.set_index('id').loc[i, 'saved_at']}")
        if st.button("Restore selected setup"):
            st.session_state.kart_setup = library.load_kart_setup(int(restore_id))
            st.success("Restored -- open the Kart Setup tab to review and save it again if it looks right.")
            st.rerun()

st.divider()
footer_caption = (
    "Braking, throttle/power-on, and jetting diagnostics are all inferred from RPM and GPS-derived G-forces -- "
    "there is no throttle, brake, gear, or EGT/lambda channel in this export. Treat those as estimates, not measurements."
)
if speed_is_estimated:
    footer_caption += " Speed itself is also estimated here, derived from GPS Distance since this export doesn't populate GPS Speed directly."
st.caption(footer_caption)
