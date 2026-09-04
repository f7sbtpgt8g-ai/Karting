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
from datetime import date

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import yaml
from plotly.offline import get_plotlyjs
from plotly.subplots import make_subplots
import streamlit as st
import streamlit.components.v1 as components

from telemetry.comparison import corner_comparison_across_sessions, cross_session_delta_trace, session_progression
from telemetry.corner_causal import corner_points_for_lap, three_zone_times
from telemetry.corner_engine import calibrate_thresholds, compare_corners
from telemetry.corners import assign_segments, build_reference_segments, lap_gps_trace, segment_midpoints
from telemetry.delta import delta_time_trace, segment_times_for_lap, theoretical_best_lap
from telemetry.focus_areas import blended_top_recommendations, recurring_weaknesses, time_loss_per_segment, top_focus_areas
from telemetry.accounts import (
    CLAIM_CLAIMED,
    CLAIM_UNCLAIMED,
    CONSENT_GRANTED,
    VISIBILITY_PRIVATE,
    VISIBILITY_SHARED,
    AccountLibrary,
    is_minor,
)
from telemetry.auth import AuthStore, LocalAuthProvider, provider_from_env
from telemetry.mailer import (
    OutboxEmailSender,
    attribution_request_email,
    claim_invite_email,
    claim_notification_email,
    guardian_consent_email,
    password_reset_email,
    sender_from_env,
    verification_email,
)
from telemetry.narrative import rank_headline_findings
from telemetry.weather import CONDITION_OPTIONS, fetch_track_conditions
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


APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8501").rstrip("/")


@st.cache_resource(show_spinner=False)
def get_account_library() -> AccountLibrary:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return AccountLibrary(DB_PATH)


@st.cache_resource(show_spinner=False)
def get_auth_store() -> AuthStore:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return AuthStore(DB_PATH)


@st.cache_resource(show_spinner=False)
def get_email_sender():
    return sender_from_env(DB_PATH)


@st.cache_resource(show_spinner=False)
def get_auth_provider(_accounts: AccountLibrary, _store: AuthStore):
    return provider_from_env(_accounts, _store)


def email_delivery_configured() -> bool:
    """Whether this deployment can actually deliver mail. When it can't
    (the default local setup, which records to an outbox instead), the
    email-verification step is skipped rather than leaving accounts stuck
    behind a link that will never arrive -- see `complete_registration`."""
    return not isinstance(get_email_sender(), OutboxEmailSender)


def dev_show_email_links() -> bool:
    """Print links that would have been emailed straight onto the page.

    Off unless explicitly enabled, and deliberately so: showing a password
    reset link for an arbitrary address on screen is account takeover, not
    a convenience. Intended only for local development against the outbox
    sender."""
    return os.environ.get("KARTING_DEV_SHOW_EMAIL_LINKS", "").strip().lower() in ("1", "true", "yes")


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


@st.cache_resource(show_spinner=False)
def calibrate_thresholds_cached(_session: Session, _key: tuple, clean_lap_numbers: tuple, segments: pd.DataFrame):
    """Noise-aware significance thresholds for the Lap Comparison page,
    derived from the reference session's own repeat-lap variance (see
    corner_engine.calibrate_thresholds) -- cached since it loops corner
    extraction over every clean lap in the reference session."""
    return calibrate_thresholds(_session, list(clean_lap_numbers), segments)


@st.cache_resource(show_spinner="Analyzing corner-by-corner causes...")
def compare_corners_cached(
    _session_a: Session, key_a: tuple, lap_a: int, _session_b: Session, key_b: tuple, lap_b: int,
    segments: pd.DataFrame, _thresholds,
) -> pd.DataFrame:
    return compare_corners(_session_a, lap_a, _session_b, lap_b, segments, _thresholds)


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


MAX_LAP_COMPARISON_LAPS = 4
MAX_HEADLINE_CARDS = 3


def page_lap_comparison() -> None:
    if not _require_data():
        return
    st.subheader("Lap comparison")
    st.caption(
        "Corner-by-corner causal breakdown between two or more laps -- not just where time was gained or lost, "
        "but why. A fast entry that gains time through the corner but costs more than that down the following "
        "straight shows up here as a net loss, not a false 'good corner'. The fastest lap among your picks is "
        "always the reference the others are compared against."
    )

    if "lc_row_ids" not in st.session_state:
        default_rows = _default_data_analysis_rows(all_sessions)[:3]
        st.session_state["lc_row_ids"] = list(range(len(default_rows)))
        st.session_state["lc_next_row_id"] = len(default_rows)
        for i, (sess_label, lap_no) in enumerate(default_rows):
            st.session_state[f"lc_session_{i}"] = sess_label
            st.session_state[f"lc_lap_{i}"] = lap_no

    compare_entries = []
    css_rules = []
    with st.expander("Laps to compare", expanded=True):
        for idx, row_id in enumerate(list(st.session_state["lc_row_ids"])):
            row_color = LAP_COLORS[idx % len(LAP_COLORS)]
            text_color = _readable_text_color(row_color)
            session_key, lap_key = f"lc_session_{row_id}", f"lc_lap_{row_id}"
            css_rules.append(
                f'.st-key-{lap_key} [role="group"] {{ background-color: {row_color} !important; }}'
                f'.st-key-{lap_key} input {{ color: {text_color} !important; }}'
            )
            rc1, rc2, rc3 = st.columns([4, 3, 1])
            label_visibility = "visible" if idx == 0 else "collapsed"
            _ensure_valid_widget_state(session_key, session_labels, active_label)
            row_session_label = rc1.selectbox("Session", session_labels, key=session_key, label_visibility=label_visibility)
            row_session = dict(all_sessions)[row_session_label]
            row_lap_numbers, row_lap_times = _session_clean_laps(row_session)
            if not row_lap_numbers:
                rc2.caption("No clean laps in this session.")
                if rc3.button("✕", key=f"lc_remove_{row_id}", help="Remove this row"):
                    st.session_state["lc_row_ids"].remove(row_id)
                    st.rerun()
                continue
            _ensure_valid_widget_state(lap_key, row_lap_numbers, row_lap_numbers[0])
            row_lap = rc2.selectbox(
                "Lap", row_lap_numbers, key=lap_key, format_func=lambda n, _t=row_lap_times: _lap_label(n, _t),
                label_visibility=label_visibility,
            )
            if rc3.button("✕", key=f"lc_remove_{row_id}", help="Remove this row"):
                st.session_state["lc_row_ids"].remove(row_id)
                st.rerun()
            compare_entries.append({
                "row_id": row_id, "session_label": row_session_label, "session": row_session, "lap_number": row_lap,
                "lap_time": row_lap_times.get(row_lap), "color": row_color, "tag": f"S{row_session.session_id}·L{row_lap}",
            })

        if css_rules:
            st.markdown(f"<style>{''.join(css_rules)}</style>", unsafe_allow_html=True)

        if len(st.session_state["lc_row_ids"]) >= MAX_LAP_COMPARISON_LAPS:
            st.caption(f"Maximum {MAX_LAP_COMPARISON_LAPS} laps at once, for readability.")
        elif st.button("+ Add lap to compare", key="lc_add_row"):
            new_id = st.session_state["lc_next_row_id"]
            st.session_state["lc_row_ids"].append(new_id)
            st.session_state["lc_next_row_id"] = new_id + 1
            st.rerun()

    if len(compare_entries) < 2:
        st.info("Add at least two laps to compare.")
        render_footer()
        return

    use_anthropic = st.checkbox(
        "Use AI phrasing for narrative sentences", value=False,
        help="Sends the already-computed corner facts (deltas, pattern classification -- never raw telemetry) to "
        "the Anthropic API to phrase 1-2 natural sentences. The analysis itself is always the same deterministic "
        "rules either way -- this only changes the wording. Requires ANTHROPIC_API_KEY to be set in the environment; "
        "silently falls back to the built-in templated sentences otherwise.",
    )

    fastest_entry = min(compare_entries, key=lambda e: e["lap_time"] if e["lap_time"] is not None else float("inf"))
    other_entries = [e for e in compare_entries if e is not fastest_entry]

    ref_session, ref_lap = fastest_entry["session"], fastest_entry["lap_number"]
    ref_segments, _ = build_segments_and_midpoints_cached(ref_session, session_cache_key(ref_session), ref_lap)
    if ref_segments.loc[ref_segments["kind"] == "corner"].empty:
        st.info("No corners detected on the reference lap -- nothing to compare corner-by-corner.")
        render_footer()
        return

    ref_clean_numbers, _ = _session_clean_laps(ref_session)
    thresholds = calibrate_thresholds_cached(ref_session, session_cache_key(ref_session), tuple(ref_clean_numbers), ref_segments)

    if len(ref_clean_numbers) >= 4:
        st.caption(
            f"Reference lap: {fastest_entry['tag']} ({fastest_entry['lap_time']:.2f}s) from {fastest_entry['session_label']}. "
            f"Significance thresholds calibrated from {len(ref_clean_numbers)} of its session's own clean laps "
            f"(±{thresholds.min_speed_delta_kmh:.1f} km/h entry/apex/exit speed, ±{thresholds.min_distance_delta_m:.0f}m braking point)."
        )
    else:
        st.caption(
            f"Reference lap: {fastest_entry['tag']} ({fastest_entry['lap_time']:.2f}s) from {fastest_entry['session_label']}. "
            "Using default significance thresholds (fewer than 4 clean laps in the reference session to calibrate noise floor from)."
        )

    all_results = []
    for entry in other_entries:
        result_df = compare_corners_cached(
            entry["session"], session_cache_key(entry["session"]), entry["lap_number"],
            ref_session, session_cache_key(ref_session), ref_lap, ref_segments, thresholds,
        )
        if result_df.empty:
            continue
        all_results.append((entry, result_df))

        # Part 5 step 2: log this comparison's structured facts unconditionally
        # (not gated behind any trend UI existing) so the Recurring Patterns
        # page has data to work with from the very first comparison ever run.
        entry_db = session_db_lookup.get((entry["session"].source_file, entry["session"].session_id, entry["session"].start_time))
        ref_db = session_db_lookup.get((ref_session.source_file, ref_session.session_id, ref_session.start_time))
        entry_points = corner_points_for_lap(entry["session"], entry["lap_number"], ref_segments)
        entry_zones = three_zone_times(entry["session"], entry["lap_number"], entry_points)
        track_name = entry_db["track_name"] if entry_db else None
        conditions = entry_db["track_condition"] if entry_db else None
        library.log_corner_metrics(
            entry_db["id"] if entry_db else None, entry["session"].driver, track_name, entry["lap_number"],
            entry_points, entry_zones, conditions=conditions,
        )
        library.log_pattern_instances(
            entry["session"].driver, track_name, entry_db["id"] if entry_db else None, entry["lap_number"],
            ref_db["id"] if ref_db else None, ref_lap, result_df, conditions=conditions,
        )

    if not all_results:
        st.info("No corner-level data could be extracted for the selected laps (check GPS coverage on these laps).")
        render_footer()
        return

    # Cross-lap recurrence: how many of the OTHER compared laps show the same
    # (corner, pattern) -- Part 3's "one-off vs. you're consistently doing X"
    # signal, applied across whatever laps happen to be selected here (they
    # don't need to all be from the same session).
    occurrence_counts: dict[tuple, int] = {}
    for _, result_df in all_results:
        significant = result_df[result_df["headline"] & (result_df["pattern_type"] != "clean_no_significant_delta")]
        for key in set(zip(significant["corner_label"], significant["pattern_type"])):
            occurrence_counts[key] = occurrence_counts.get(key, 0) + 1

    for entry, result_df in all_results:
        st.markdown(f"#### {entry['tag']} vs. reference ({fastest_entry['tag']})")
        findings = rank_headline_findings(result_df, n=MAX_HEADLINE_CARDS, use_anthropic=use_anthropic)
        if not findings:
            st.success("No significant corner-by-corner differences vs. the reference lap.")
        else:
            cards = st.columns(len(findings))
            for col, finding in zip(cards, findings):
                with col:
                    st.metric(finding["corner_label"], f"{finding['net_time_impact_s']:+.2f}s")
                    st.write(finding["narrative"])
                    n_also = occurrence_counts.get((finding["corner_label"], finding["pattern_type"]), 1)
                    if n_also >= 2:
                        st.caption(f"⚠️ Also showing up in {n_also - 1} of your other compared lap(s) here -- a repeated pattern, not a one-off.")
                    if finding.get("root_cause_corner"):
                        st.caption(f"Part of a corner complex -- traces back to {finding['root_cause_corner']}.")

        with st.expander(f"Full corner-by-corner breakdown -- {entry['tag']} ({len(result_df)} corners)"):
            table = result_df.copy()
            table["complex_group"] = table["complex_group"].apply(lambda g: " → ".join(g) if isinstance(g, list) else g)
            table["evidence"] = table["evidence"].apply(
                lambda e: ", ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in (e or {}).items())
            )
            display_cols = [
                "corner_label", "pattern_type", "confidence", "net_time_impact_s",
                "entry_speed_delta_kmh", "apex_speed_delta_kmh", "exit_speed_delta_kmh",
                "entry_distance_delta_m", "apex_distance_delta_m",
                "zone_a_delta_s", "zone_b_delta_s", "zone_c_delta_s",
                "is_complex", "root_cause_corner", "evidence",
            ]
            table = table[display_cols].round(
                {
                    "net_time_impact_s": 3, "entry_speed_delta_kmh": 1, "apex_speed_delta_kmh": 1, "exit_speed_delta_kmh": 1,
                    "entry_distance_delta_m": 1, "apex_distance_delta_m": 1,
                    "zone_a_delta_s": 3, "zone_b_delta_s": 3, "zone_c_delta_s": 3,
                }
            )
            st.dataframe(prettify_columns(table), width='stretch')

    st.subheader("Speed & delta trace")
    st.caption(
        "For visual context -- the same linked chart/map view as the Data Analysis page, scoped to your selected "
        "laps here. Delta is vs. the reference lap."
    )
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06,
        subplot_titles=["Speed (km/h)", "Delta vs reference (s) — positive = time lost"],
    )
    lap_traces: dict[int, pd.DataFrame] = {}
    for entry in compare_entries:
        trace = lap_metric_trace(entry["session"], entry["lap_number"])
        lap_traces[entry["row_id"]] = trace
        fig.add_trace(
            go.Scatter(
                x=trace["lap_distance_m"], y=trace["GPS Speed"], mode="lines", name=entry["tag"], legendgroup=entry["tag"],
                line=dict(color=entry["color"]), hovertemplate=f"{entry['tag']}: %{{y:.1f}} km/h<extra></extra>",
            ),
            row=1, col=1,
        )
        if entry is not fastest_entry:
            dt = cross_session_delta_trace(entry["session"], entry["lap_number"], ref_session, ref_lap, n_points=800)
            fig.add_trace(
                go.Scatter(
                    x=dt["distance_m"], y=dt["delta_s"], mode="lines", name=f"{entry['tag']} delta", legendgroup=entry["tag"],
                    line=dict(color=entry["color"]), showlegend=False, hovertemplate=f"{entry['tag']}: %{{y:.4f}}s<extra></extra>",
                ),
                row=2, col=1,
            )
    fig.add_hline(y=0, row=2, col=1, line_dash="dash", line_color="gray")
    fig.update_xaxes(title_text="Distance (m)", row=2, col=1)
    fig.update_layout(hovermode="closest")
    fig.update_xaxes(showspikes=False)
    row_y_domains = [_axis_y_domain(fig, r) for r in (1, 2)]

    ref_trace = lap_traces[fastest_entry["row_id"]].dropna(subset=["lap_distance_m", "Latitude", "Longitude"]).sort_values("lap_distance_m")
    map_fig = go.Figure()
    map_fig.add_trace(
        go.Scattergl(x=ref_trace["Longitude"], y=ref_trace["Latitude"], mode="lines", line=dict(color=fastest_entry["color"], width=2), showlegend=False)
    )
    if not ref_trace.empty:
        map_fig.add_trace(
            go.Scatter(
                x=[ref_trace["Longitude"].iloc[0]], y=[ref_trace["Latitude"].iloc[0]],
                mode="markers", marker=dict(size=16, color="red", line=dict(width=2, color="white")), showlegend=False,
            )
        )
    map_fig.update_layout(yaxis=dict(scaleanchor="x"), xaxis_title="Longitude", yaxis_title="Latitude", margin=dict(t=10))

    render_linked_speed_delta(
        fig, map_fig,
        ref_trace["lap_distance_m"].tolist(), ref_trace["Latitude"].tolist(), ref_trace["Longitude"].tolist(),
        height=520, map_height=260, chart_row_y_domains=row_y_domains,
    )
    render_footer()


def page_recurring_patterns() -> None:
    if not _require_data():
        return
    st.subheader("Recurring patterns")
    st.caption(
        "Trends across every comparison you've run on the Lap Comparison page, not just the most recent one -- a "
        "pattern that keeps showing up session after session is a much stronger signal than any single comparison. "
        "Only patterns seen in 2 or more sessions appear here; run more comparisons on the Lap Comparison page to "
        "build this up."
    )
    driver = active_session.driver
    summary = library.recurring_pattern_summary(driver=driver, min_occurrences=2)
    if summary.empty:
        st.info(
            "Nothing recurring yet. Patterns are logged every time you run a comparison on the Lap Comparison page -- "
            "this view fills in once the same corner + pattern shows up across 2 or more sessions."
        )
        render_footer()
        return

    for _, row in summary.iterrows():
        pattern_label = str(row["pattern_type"]).replace("_", " ")
        direction = "costing" if row["avg_net_time_impact_s"] > 0 else "gaining"
        with st.container(border=True):
            st.markdown(f"**{row['corner_label']} — {pattern_label}**")
            st.write(
                f"Showing up in {int(row['n_sessions'])} session(s) ({int(row['n_laps'])} lap comparison(s) total) -- "
                f"averaging {abs(row['avg_net_time_impact_s']):.2f}s {direction} each time it appears "
                f"(seen from {str(row['first_seen'])[:10]} to {str(row['last_seen'])[:10]})."
            )

    with st.expander("Raw pattern trend table"):
        st.dataframe(prettify_columns(summary), width='stretch')
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
    # Scoped to what this account owns or uploaded -- `library.list_sessions()`
    # would list (and offer to delete) every session on the instance,
    # including other drivers' private ones.
    session_history = library.list_sessions()
    session_history = session_history[
        (session_history["driver_profile_id"] == current_profile["id"])
        | (session_history["uploaded_by_user_id"] == current_user["id"])
    ]
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


_CONDITIONS_WIDGET_KEYS = (
    "settings_condition_select", "settings_temperature_c", "settings_humidity_pct",
    "settings_pressure_hpa", "settings_altitude_m",
)


def page_settings() -> None:
    st.title("⚙️ Settings")
    if flash := st.session_state.pop("settings_upload_result", None):
        st.success(flash)
    st.caption(
        "Uploaded files are saved into your session library, so they persist across reruns and app restarts -- "
        "no need to re-upload the same file next time. A file containing several sessions (a shared team logger, "
        "say) can be split between drivers: you attribute each session individually after it's parsed."
    )

    existing = accounts_lib.visible_sessions_for_user(current_user["id"])
    if not existing.empty:
        st.caption(f"📚 You can currently see {len(existing)} session(s).")

    track_input = st.text_input(
        "Track name for this upload", value="", placeholder="e.g. Jyllandsringen", key="settings_track_name",
    )
    uploaded_files = st.file_uploader(
        "Upload Unipro TSV export(s)", type=["tsv", "txt"], accept_multiple_files=True, key="settings_uploader",
    )

    missing = [field for field, value in (("track name", track_input),) if not value.strip()]

    # Track conditions -- entered once per upload (not per session inside a
    # multi-session file): a full day at the track is the common case, and a
    # driver whose conditions genuinely changed partway through can just
    # upload that batch of sessions separately with different values here.
    track_condition = temperature_c = humidity_pct = pressure_hpa = altitude_m = None
    conditions_source: str | None = None

    if uploaded_files:
        parsed_sessions: list[Session] = []
        for f in uploaded_files:
            parsed_sessions.extend(parse_uploaded_file(f.getvalue(), f.name))
        # The earliest-starting session in the batch, GPS/time-wise -- a
        # reasonable single representative point to fetch weather for when a
        # multi-session file spans a couple of hours, without needing a
        # separate lookup per session.
        representative_session = (
            min(parsed_sessions, key=lambda s: (s.start_date or "", s.start_time or "")) if parsed_sessions else None
        )

        st.markdown("**Track conditions for this upload**")
        st.caption(
            "Applied to every session loaded from these file(s) -- auto-detected from GPS location + session "
            "start time via Open-Meteo (free, no signup) where possible, and always editable. Used to calibrate "
            "the jetting suggestions on the Kart Setup page."
        )

        upload_fingerprint = tuple((f.name, f.size) for f in uploaded_files)
        is_new_upload = st.session_state.get("settings_conditions_fingerprint") != upload_fingerprint
        refetch_clicked = False if is_new_upload else st.button(
            "🔄 Re-fetch weather", key="settings_refetch_weather",
            help="Re-run the auto-detection (e.g. after a flaky first attempt), overwriting any manual edits below.",
        )

        if (is_new_upload or refetch_clicked) and representative_session is not None:
            with st.spinner("Looking up track conditions..."):
                fetched = fetch_track_conditions(representative_session)
            st.session_state["settings_conditions_fingerprint"] = upload_fingerprint
            st.session_state["settings_fetched_conditions"] = fetched
            if fetched is not None:
                st.session_state["settings_condition_select"] = fetched.condition
                st.session_state["settings_temperature_c"] = fetched.temperature_c
                st.session_state["settings_humidity_pct"] = fetched.humidity_pct
                st.session_state["settings_pressure_hpa"] = fetched.pressure_hpa
                st.session_state["settings_altitude_m"] = fetched.altitude_m
            else:
                for key in _CONDITIONS_WIDGET_KEYS:
                    st.session_state.pop(key, None)

        fetched = st.session_state.get("settings_fetched_conditions")
        if fetched is not None:
            st.caption(f"✅ Auto-detected from {fetched.source} -- adjust below if it looks wrong.")
        else:
            st.caption(
                "⚠️ Couldn't auto-detect conditions (no internet access, no GPS fixes in the file, or the date is "
                "out of range) -- enter these manually."
            )

        cc1, cc2, cc3, cc4, cc5 = st.columns(5)
        track_condition = cc1.selectbox(
            "Conditions", CONDITION_OPTIONS, key="settings_condition_select", index=None, placeholder="Select...",
        )
        temperature_c = cc2.number_input("Temp (°C)", key="settings_temperature_c", value=None, step=0.5, format="%.1f")
        humidity_pct = cc3.number_input(
            "Humidity (%)", key="settings_humidity_pct", value=None, min_value=0.0, max_value=100.0, step=1.0, format="%.0f",
        )
        pressure_hpa = cc4.number_input("Pressure (hPa)", key="settings_pressure_hpa", value=None, step=1.0, format="%.0f")
        altitude_m = cc5.number_input("Altitude (m)", key="settings_altitude_m", value=None, step=1.0, format="%.0f")
        conditions_source = fetched.source if fetched is not None else "manual"

        if track_condition is None or None in (temperature_c, humidity_pct, pressure_hpa, altitude_m):
            missing.append("all 5 track-conditions fields")

    if uploaded_files:
        # The review screen renders regardless of what's still missing --
        # being unable to see which sessions were even detected until every
        # other field is filled in is backwards. Saving is what's blocked.
        render_attribution_review(
            uploaded_files, track_input.strip(),
            dict(
                track_condition=track_condition, temperature_c=temperature_c, humidity_pct=humidity_pct,
                pressure_hpa=pressure_hpa, altitude_m=altitude_m, conditions_source=conditions_source,
            ),
            missing_fields=missing,
        )

    if links := st.session_state.pop("settings_invite_links", None):
        st.markdown("**Claim links** (shown because dev link display is on):")
        for link in links:
            st.code(link, language=None)

    # Sample data is loaded on request rather than seeded automatically:
    # with per-account ownership, auto-seeding on an empty view would give
    # every new account its own duplicate copy of an 82MB file.
    if os.path.exists(DEFAULT_TSV_PATH):
        st.divider()
        with st.expander("Load the bundled sample data"):
            st.caption(
                "A real multi-session export ships with the app for trying things out. It'll be filed under your "
                "own driver profile, private like anything else."
            )
            if st.button("Load sample sessions"):
                added = 0
                with st.spinner("Parsing and saving the sample file (this takes a moment)..."):
                    with open(DEFAULT_TSV_PATH, "rb") as f:
                        sample_bytes = f.read()
                    for s in parse_uploaded_file(sample_bytes, os.path.basename(DEFAULT_TSV_PATH)):
                        if library.find_session(s.source_file, s.session_id, s.start_time) is not None:
                            continue
                        s.driver = current_profile["display_name"]
                        sid = library.save_session(
                            s, driver=current_profile["display_name"], track_name="Sample Track",
                            driver_profile_id=int(current_profile["id"]), uploaded_by_user_id=current_user["id"],
                        )
                        accounts_lib.attribute_session(sid, int(current_profile["id"]), current_user["id"])
                        added += 1
                st.session_state["settings_upload_result"] = f"Loaded {added} sample session(s)."
                st.rerun()


# Attribution options offered per detected session.
ATTRIBUTE_ME = "Me"
ATTRIBUTE_REGISTERED = "Another registered driver"
ATTRIBUTE_UNCLAIMED = "An existing unclaimed profile"
ATTRIBUTE_NEW = "A new driver profile"


def render_attribution_review(
    uploaded_files, track_name: str, conditions: dict, missing_fields: list[str] | None = None
) -> None:
    """The post-upload review step: every session detected in the uploaded
    file(s), each attributed to exactly one driver before anything is saved.

    Deliberately separate from parsing -- the parser already returns a list
    of detected sessions and knows nothing about who owns them, so this is a
    UI/data layer on top rather than a change to the parsing logic.
    """
    parsed: list[tuple[str, Session]] = []
    for f in uploaded_files:
        for s in parse_uploaded_file(f.getvalue(), f.name):
            parsed.append((f.name, s))

    if not parsed:
        st.warning("No sessions were detected in those file(s).")
        return

    already_saved = [
        (name, s) for name, s in parsed
        if library.find_session(s.source_file, s.session_id, s.start_time) is not None
    ]
    new_sessions = [
        (name, s) for name, s in parsed
        if library.find_session(s.source_file, s.session_id, s.start_time) is None
    ]
    if already_saved:
        st.caption(f"{len(already_saved)} of {len(parsed)} session(s) are already in the library and will be skipped.")
    if not new_sessions:
        st.info("These sessions were already in your library -- nothing new to add.")
        return

    st.markdown("**Who drove each session?**")
    st.caption(
        "One file can hold sessions from several drivers. Each is filed under the driver it belongs to -- which "
        "doesn't have to be you, and doesn't have to be someone with an account yet."
    )

    registered = accounts_lib.list_registered_drivers()
    registered = registered[registered["user_id"] != current_user["id"]]
    unclaimed = accounts_lib.list_profiles(claim_status=CLAIM_UNCLAIMED)
    invited = accounts_lib.list_profiles(claim_status="invited")
    unclaimed_all = pd.concat([unclaimed, invited], ignore_index=True) if not invited.empty else unclaimed

    choices: list[dict] = []
    for index, (file_name, session) in enumerate(new_sessions):
        laps = compute_clean_laps(session)
        duration = laps["lap_time_s"].sum() if not laps.empty else 0.0
        label = (
            f"{file_name} · session {session.session_id} · {session.start_date or '?'} {session.start_time or ''} "
            f"· {len(laps)} laps · {duration / 60:.0f} min"
        )
        with st.container(border=True):
            st.markdown(f"**{label}**")
            mode = st.radio(
                "Attribute to", [ATTRIBUTE_ME, ATTRIBUTE_REGISTERED, ATTRIBUTE_UNCLAIMED, ATTRIBUTE_NEW],
                key=f"attr_mode_{index}", horizontal=True, label_visibility="collapsed",
            )
            entry: dict = {"file_name": file_name, "session": session, "mode": mode}

            if mode == ATTRIBUTE_REGISTERED:
                if registered.empty:
                    st.caption("No other registered drivers yet.")
                    entry["blocked"] = "no registered drivers to choose from"
                else:
                    picked = st.selectbox(
                        "Driver", registered["id"], key=f"attr_reg_{index}",
                        format_func=lambda i, _r=registered: _r.set_index("id").loc[i, "display_name"],
                    )
                    entry["profile_id"] = int(picked)
                    st.caption(
                        "They'll be asked to confirm before it's added to their history -- it won't appear "
                        "there until they accept."
                    )
            elif mode == ATTRIBUTE_UNCLAIMED:
                if unclaimed_all.empty:
                    st.caption("No unclaimed profiles exist yet.")
                    entry["blocked"] = "no unclaimed profiles to choose from"
                else:
                    picked = st.selectbox(
                        "Profile", unclaimed_all["id"], key=f"attr_unc_{index}",
                        format_func=lambda i, _u=unclaimed_all: _u.set_index("id").loc[i, "display_name"],
                    )
                    entry["profile_id"] = int(picked)
            elif mode == ATTRIBUTE_NEW:
                nc1, nc2 = st.columns(2)
                entry["new_name"] = nc1.text_input("Driver name", key=f"attr_new_name_{index}")
                entry["new_email"] = nc2.text_input(
                    "Their email (optional)", key=f"attr_new_email_{index}",
                    help=(
                        "With an email, they're invited to claim the profile and see this data. Without one, "
                        "a private placeholder is created and nobody is contacted."
                    ),
                )
                if not entry["new_email"].strip():
                    st.caption("No email -- a private placeholder is created and nobody is contacted.")
                elif not invite_emails_enabled_ui():
                    st.caption(
                        "⚠️ Invite emails are currently disabled for this deployment, so the profile is created "
                        "but no invite is sent. The claim link is still generated and shown to you."
                    )
                if not entry["new_name"].strip():
                    entry["blocked"] = "a name for the new driver profile"
            choices.append(entry)

    outstanding = sorted({c["blocked"] for c in choices if c.get("blocked")}) + list(missing_fields or [])
    if outstanding:
        st.warning(f"Before saving, still needed: {', '.join(outstanding)}.")
        return

    if not st.button("Save sessions", type="primary"):
        return

    saved, pending, invites = 0, 0, []
    with st.spinner(f"Saving {len(choices)} session(s)..."):
        for choice in choices:
            session = choice["session"]
            profile_id, requires_confirmation, claim_token = _resolve_attribution_target(choice)

            profile = accounts_lib.get_profile(profile_id)
            session.driver = profile["display_name"]
            session_db_id = library.save_session(
                session, driver=profile["display_name"], track_name=track_name,
                driver_profile_id=profile_id, uploaded_by_user_id=current_user["id"],
                kart_class=setup.class_name if setup else None, **conditions,
            )
            accounts_lib.attribute_session(
                session_db_id, profile_id, uploaded_by_user_id=current_user["id"],
                requires_confirmation=requires_confirmation,
            )
            saved += 1
            if requires_confirmation:
                pending += 1
                _send_attribution_request(profile, session, track_name)
            if claim_token:
                invites.append((profile["display_name"], profile["invite_email"], claim_token))

    for name, email, token in invites:
        _send_claim_invite(name, email, token, track_name)

    message = f"Saved {saved} session(s)."
    if pending:
        message += f" {pending} awaiting the other driver's confirmation."
    st.session_state["settings_upload_result"] = message
    if invites and dev_show_email_links():
        st.session_state["settings_invite_links"] = [_link(f"?claim={t}") for _n, _e, t in invites]
    st.rerun()


def invite_emails_enabled_ui() -> bool:
    from telemetry.mailer import invite_emails_enabled

    return invite_emails_enabled()


def _resolve_attribution_target(choice: dict) -> tuple[int, bool, str | None]:
    """Turn one review-screen choice into `(profile_id,
    requires_confirmation, claim_token)`.

    Only the "already-registered driver" path needs confirmation: there is a
    real account behind it whose history would otherwise be written to
    without their say-so. Unclaimed profiles have no account to protect --
    the check for those happens at claim time instead."""
    mode = choice["mode"]
    if mode == ATTRIBUTE_ME:
        return int(current_profile["id"]), False, None
    if mode == ATTRIBUTE_REGISTERED:
        return choice["profile_id"], True, None
    if mode == ATTRIBUTE_UNCLAIMED:
        return choice["profile_id"], False, None

    email = choice["new_email"].strip() or None
    profile_id, token = accounts_lib.create_unclaimed_profile(
        choice["new_name"].strip(), created_by_user_id=current_user["id"], invite_email=email,
    )
    return profile_id, False, token


def _send_attribution_request(profile: dict, session: Session, track_name: str) -> None:
    target_user = accounts_lib.get_user(int(profile["user_id"]))
    if not target_user:
        return
    summary = f"{track_name}, {session.start_date or 'unknown date'} {session.start_time or ''}".strip()
    get_email_sender().send(
        attribution_request_email(
            target_user["email"], current_user["display_name"] or current_user["email"], summary,
            _link("?page=pending"),
        )
    )


def _send_claim_invite(driver_name: str, email: str | None, token: str, track_name: str) -> None:
    if not email:
        return
    get_email_sender().send(
        claim_invite_email(
            email, driver_name, current_user["display_name"] or current_user["email"],
            f"{track_name} — uploaded {date.today().isoformat()}", _link(f"?claim={token}"),
        )
    )


def page_my_sessions() -> None:
    """Ownership and sharing: what this driver owns, what each session's
    visibility is, and what is waiting on them."""
    st.subheader("My sessions & sharing")
    st.caption(
        "Everything filed under your driver profile. Sessions are private until you share them -- sharing a "
        "session makes it selectable as a comparison reference by other drivers and eligible for that track's "
        "leaderboard."
    )

    pending = accounts_lib.pending_attribution_requests(int(current_profile["id"]))
    if not pending.empty:
        st.markdown("**Waiting for your confirmation**")
        st.caption("Someone else uploaded these and says they're yours. They're not in your history until you accept.")
        for _, request in pending.iterrows():
            with st.container(border=True):
                st.write(
                    f"**{request['track_name'] or 'Unknown track'}** — {request['start_date'] or '?'} "
                    f"{request['start_time'] or ''} · {int(request['n_laps'] or 0)} laps"
                )
                st.caption(f"Uploaded by {request['requested_by_email'] or 'someone'}")
                accept_col, reject_col, _ = st.columns([1, 1, 4])
                if accept_col.button("Accept", key=f"accept_{request['id']}", type="primary"):
                    accounts_lib.resolve_attribution_request(int(request["id"]), accept=True)
                    st.rerun()
                if reject_col.button("Reject", key=f"reject_{request['id']}"):
                    accounts_lib.resolve_attribution_request(int(request["id"]), accept=False)
                    st.rerun()
        st.divider()

    owned = accounts_lib.sessions_for_profile(int(current_profile["id"]))
    if owned.empty:
        st.info("No sessions filed under your profile yet -- upload one from the Settings page.")
        render_footer()
        return

    for _, row in owned.iterrows():
        with st.container(border=True):
            info_col, toggle_col = st.columns([4, 1])
            info_col.write(
                f"**{row['track_name'] or 'Unknown track'}** — {row['start_date'] or '?'} {row['start_time'] or ''}"
            )
            info_col.caption(
                f"{int(row['n_laps'] or 0)} laps · best {row['best_lap_s']:.2f}s"
                if pd.notna(row["best_lap_s"]) else f"{int(row['n_laps'] or 0)} laps"
            )
            shared = row["visibility"] == VISIBILITY_SHARED
            new_value = toggle_col.toggle("Shared", value=shared, key=f"share_{row['id']}")
            if new_value != shared:
                accounts_lib.set_session_visibility(
                    int(row["id"]), VISIBILITY_SHARED if new_value else VISIBILITY_PRIVATE
                )
                st.rerun()
    render_footer()


def page_find_profile() -> None:
    """The unprompted claim path: someone who registered on their own
    recognising an unclaimed placeholder as themselves."""
    st.subheader("Find my driver profile")
    st.caption(
        "If someone uploaded your data before you had an account, it may be sitting under an unclaimed profile. "
        "Search for your name below."
    )

    if current_profile and accounts_lib.sessions_for_profile(int(current_profile["id"])).shape[0] > 0:
        st.caption(f"You're currently linked to the profile **{current_profile['display_name']}**.")

    query = st.text_input("Search unclaimed profiles by name", key="claim_search")
    if not query.strip():
        render_footer()
        return

    matches = accounts_lib.list_profiles(name_query=query)
    matches = matches[matches["claim_status"] != CLAIM_CLAIMED]
    if matches.empty:
        st.info("No unclaimed profiles match that name.")
        render_footer()
        return

    for _, profile in matches.iterrows():
        sessions = accounts_lib.sessions_for_profile(int(profile["id"]), include_pending=True)
        with st.container(border=True):
            st.write(f"**{profile['display_name']}** — {len(sessions)} session(s)")
            if not sessions.empty:
                tracks = sorted({t for t in sessions["track_name"].dropna().unique()})
                st.caption(f"Tracks: {', '.join(tracks) if tracks else 'unknown'}")
            if st.button("This is me", key=f"claimreq_{profile['id']}"):
                accounts_lib.request_profile_claim(int(profile["id"]), current_user["id"])
                try:
                    accounts_lib.claim_profile(int(profile["id"]), current_user["id"])
                except ValueError as exc:
                    # Most often: this account already has its own profile.
                    # Recorded as a request for a human to sort out rather
                    # than merging two driver identities automatically.
                    st.warning(f"{exc} Your request has been recorded.")
                else:
                    _notify_uploader_of_claim(accounts_lib, int(profile["id"]), current_user["id"])
                    st.success("Claimed -- those sessions are now in your history.")
                    st.rerun()

    st.divider()
    with st.expander("Something attributed to you incorrectly?"):
        reason = st.text_area("What's wrong?", key="report_reason")
        if st.button("Report incorrect attribution"):
            accounts_lib.report_attribution(current_user["id"], reason=reason)
            st.success("Reported -- thanks, someone will look into it.")
    render_footer()


def page_shared_laps() -> None:
    """Browse other drivers' explicitly-shared sessions and pick one as a
    comparison reference."""
    st.subheader("Shared laps from other drivers")
    st.caption(
        "Only sessions that another driver has explicitly shared appear here. Selecting one sets it as the "
        "reference lap on the Lap Comparison page."
    )

    fc1, fc2, fc3 = st.columns(3)
    track_filter = fc1.text_input("Track", key="shared_track")
    driver_filter = fc2.text_input("Driver name", key="shared_driver")
    condition_filter = fc3.selectbox(
        "Conditions", ["Any"] + CONDITION_OPTIONS, key="shared_conditions",
    )

    results = accounts_lib.shareable_reference_sessions(
        exclude_user_id=current_user["id"],
        track_name=track_filter.strip() or None,
        driver_query=driver_filter.strip() or None,
        track_condition=None if condition_filter == "Any" else condition_filter,
    )
    if results.empty:
        st.info("No shared sessions match those filters yet.")
        render_footer()
        return

    display = results[
        ["driver_display_name", "track_name", "start_date", "track_condition", "kart_class", "n_laps", "best_lap_s"]
    ].copy()
    display["best_lap_s"] = display["best_lap_s"].round(2)
    st.dataframe(prettify_columns(display), width="stretch")

    picked = st.selectbox(
        "Use as comparison reference", results["id"],
        format_func=lambda i, _r=results: (
            f"{_r.set_index('id').loc[i, 'driver_display_name']} — "
            f"{_r.set_index('id').loc[i, 'track_name']} {_r.set_index('id').loc[i, 'start_date']}"
        ),
    )
    if st.button("Set as reference lap", type="primary"):
        st.session_state["lc_reference_session_db_id"] = int(picked)
        st.success("Set. Open the Lap Comparison page to compare against it.")
    render_footer()


def page_leaderboards() -> None:
    st.subheader("Leaderboards")
    st.caption(
        "Best lap per driver at each track. Only sessions a driver has explicitly shared are eligible -- private "
        "sessions never appear, and neither does data belonging to a profile nobody has claimed yet."
    )

    tracks = accounts_lib.leaderboard_tracks()
    if not tracks:
        st.info(
            "No shared sessions yet, so there's nothing to rank. Share one of your own from the "
            "'My sessions & sharing' page to start a board."
        )
        render_footer()
        return

    fc1, fc2, fc3 = st.columns(3)
    track = fc1.selectbox("Track", tracks, key="lb_track")
    condition = fc2.selectbox("Conditions", ["Overall"] + CONDITION_OPTIONS, key="lb_conditions")
    classes = sorted(
        {
            c for c in accounts_lib.shareable_reference_sessions(track_name=track)["kart_class"].dropna().unique()
        }
    )
    kart_class = fc3.selectbox("Class", ["All classes"] + classes, key="lb_class")

    board = accounts_lib.leaderboard(
        track,
        track_condition=None if condition == "Overall" else condition,
        kart_class=None if kart_class == "All classes" else kart_class,
    )
    if board.empty:
        st.info("Nothing on this board with those filters yet.")
        render_footer()
        return

    display = board[["rank", "driver_display_name", "best_lap_s", "qualifying_sessions"]].copy()
    display["best_lap_s"] = display["best_lap_s"].round(3)
    st.dataframe(prettify_columns(display), width="stretch", hide_index=True)
    if condition == "Overall":
        st.caption("'Overall' pools every condition and ranks on time alone -- wet and dry laps compete directly.")
    render_footer()


# ---------------------------------------------------------------------------
# Authentication gate
#
# Everything below runs before st.navigation: a signed-out visitor gets the
# sign-in / register / reset / claim screens and nothing else, so no page
# function ever has to defend itself against there being no current user.
# ---------------------------------------------------------------------------

SESSION_TOKEN_KEY = "_auth_session_token"


def current_user_id() -> int | None:
    """The signed-in account, resolved from a server-side session token so
    that signing out (or a password reset) genuinely invalidates it, rather
    than just clearing a client-side flag."""
    return get_auth_store().user_for_session(st.session_state.get(SESSION_TOKEN_KEY))


def sign_in(user_id: int) -> None:
    st.session_state[SESSION_TOKEN_KEY] = get_auth_store().start_session(user_id)


def sign_out() -> None:
    token = st.session_state.pop(SESSION_TOKEN_KEY, None)
    if token:
        get_auth_store().revoke_session(token)
    # Everything else in session_state belongs to the account that was
    # signed in -- selected laps, cached pickers, upload drafts. Dropping
    # the lot is what stops one account's state bleeding into the next
    # on a shared machine.
    for key in [k for k in st.session_state.keys() if not k.startswith("_st")]:
        st.session_state.pop(key, None)


def _link(path_and_query: str) -> str:
    return f"{APP_BASE_URL}/{path_and_query.lstrip('/')}"


def complete_registration(accounts: AccountLibrary, provider, result, guardian_email: str | None) -> None:
    """Post-registration side effects: verification mail (or auto-verify
    where no mail transport exists) and the guardian consent request."""
    sender = get_email_sender()
    user = accounts.get_user(result.user_id)

    if email_delivery_configured() and result.token:
        sender.send(verification_email(user["email"], _link(f"?verify={result.token}")))
        st.success("Account created. Check your email for a link to confirm your address.")
    elif email_delivery_configured():
        st.success("Account created. Check your email for a link to confirm your address.")
    else:
        # No mail transport configured -- holding the account behind a link
        # that can never arrive would just lock the user out of their own
        # local install.
        accounts.set_email_verified(result.user_id, True)
        st.success("Account created.")
        st.caption(
            "Email verification was skipped because no mail server is configured for this deployment "
            "(set SMTP_HOST, or use Supabase, to turn it on)."
        )

    if guardian_email:
        sender.send(
            guardian_consent_email(
                guardian_email, user["display_name"] or user["email"], _link(f"?consent={result.user_id}")
            )
        )
        st.info(
            f"Because this driver is under 16, the account stays inactive until {guardian_email} approves it. "
            "A request has been sent to them."
        )
        if dev_show_email_links():
            st.code(_link(f"?consent={result.user_id}"), language=None)


def render_claim_landing(accounts: AccountLibrary, provider, token: str) -> None:
    """The invite link's destination. Claiming *is* registration -- same
    signup path as anyone else, including the age/guardian handling -- and
    then links the existing profile instead of creating a fresh one, so
    every session already recorded under it is immediately theirs."""
    profile = accounts.get_profile_by_claim_token(token)
    if profile is None:
        st.error("That claim link is invalid, already used, or has expired.")
        st.caption("Ask whoever sent it to generate a new one.")
        return

    sessions = accounts.sessions_for_profile(int(profile["id"]), include_pending=True)
    st.subheader(f"Session data recorded for {profile['display_name']}")
    st.write(
        f"Someone uploaded karting data and recorded it under the name **{profile['display_name']}**. "
        f"There {'is' if len(sessions) == 1 else 'are'} **{len(sessions)}** session(s) waiting."
    )
    if not sessions.empty:
        preview = sessions[["track_name", "start_date", "start_time", "n_laps", "best_lap_s"]].copy()
        st.dataframe(prettify_columns(preview), width="stretch")
    st.caption(
        "This data is private -- nobody else can see it and it isn't on any leaderboard. "
        "If it isn't yours, you don't need to do anything, and you can ask for it to be deleted instead."
    )

    signed_in = current_user_id()
    if signed_in:
        st.info("You're already signed in. You can link this profile to your account.")
        if st.button("This is me -- link it to my account", type="primary"):
            try:
                accounts.claim_profile_by_token(token, signed_in)
            except ValueError as exc:
                st.error(str(exc))
            else:
                _notify_uploader_of_claim(accounts, int(profile["id"]), signed_in)
                st.query_params.clear()
                st.success("Linked. Those sessions are now in your history.")
                st.rerun()
        return

    st.divider()
    st.markdown("**Create your account to access it**")
    with st.form("claim_register"):
        email = st.text_input("Email", value=profile["invite_email"] or "")
        password = st.text_input("Password", type="password")
        dob = st.date_input("Date of birth", value=None, min_value=date(1920, 1, 1), format="YYYY-MM-DD")
        guardian = st.text_input(
            "Parent/guardian email (required if under 16)", value="",
            help="Under-16 accounts stay inactive until a parent or guardian approves them.",
        )
        submitted = st.form_submit_button("Create account and claim", type="primary")
    if submitted:
        result = provider.register(
            email, password, display_name=profile["display_name"],
            date_of_birth=dob.isoformat() if dob else None, guardian_email=guardian.strip() or None,
        )
        if not result.ok:
            st.error(result.error)
            return
        try:
            accounts.claim_profile_by_token(token, result.user_id)
        except ValueError as exc:
            st.error(str(exc))
            return
        complete_registration(accounts, provider, result, guardian.strip() or None)
        _notify_uploader_of_claim(accounts, int(profile["id"]), result.user_id)
        st.query_params.clear()


def _notify_uploader_of_claim(accounts: AccountLibrary, profile_id: int, claimed_by_user_id: int) -> None:
    """Tell whoever created a placeholder that it's been claimed. A light
    sanity check, not an approval gate -- see `request_profile_claim`."""
    profile = accounts.get_profile(profile_id)
    if not profile or not profile.get("created_by_user_id"):
        return
    uploader = accounts.get_user(int(profile["created_by_user_id"]))
    claimer = accounts.get_user(claimed_by_user_id)
    if uploader and claimer:
        get_email_sender().send(
            claim_notification_email(
                uploader["email"], profile["display_name"], claimer["display_name"] or claimer["email"]
            )
        )


def render_auth_gate(accounts: AccountLibrary, provider) -> None:
    """The entire signed-out experience."""
    st.title("🏎️ Karting Telemetry")

    params = st.query_params
    if "claim" in params:
        render_claim_landing(accounts, provider, params["claim"])
        return
    if "verify" in params:
        result = provider.verify_email(params["verify"])
        if result.ok:
            st.success("Email confirmed. You can sign in now.")
            st.query_params.clear()
        else:
            st.error(result.error)
    if "reset" in params:
        _render_reset_form(provider, params["reset"])
        return

    st.caption(
        "Sign in to analyze your telemetry. Your sessions are private by default -- nothing is shared, "
        "or appears on a leaderboard, unless you explicitly choose to share it."
    )
    sign_in_tab, register_tab, forgot_tab = st.tabs(["Sign in", "Create account", "Forgot password"])

    with sign_in_tab:
        with st.form("sign_in"):
            email = st.text_input("Email", key="signin_email")
            password = st.text_input("Password", type="password", key="signin_password")
            submitted = st.form_submit_button("Sign in", type="primary")
        if submitted:
            result = provider.login(email, password)
            if result.ok:
                sign_in(result.user_id)
                st.rerun()
            else:
                st.error(result.error)

    with register_tab:
        with st.form("register"):
            email = st.text_input("Email", key="reg_email")
            name = st.text_input("Driver name", key="reg_name", placeholder="How you want to appear to others")
            password = st.text_input("Password", type="password", key="reg_password")
            dob = st.date_input(
                "Date of birth", value=None, min_value=date(1920, 1, 1), format="YYYY-MM-DD", key="reg_dob",
                help="Used only to apply the right protections for under-16 drivers.",
            )
            guardian = st.text_input("Parent/guardian email (required if under 16)", key="reg_guardian")
            submitted = st.form_submit_button("Create account", type="primary")
        if submitted:
            result = provider.register(
                email, password, display_name=name.strip() or None,
                date_of_birth=dob.isoformat() if dob else None, guardian_email=guardian.strip() or None,
            )
            if result.ok:
                complete_registration(accounts, provider, result, guardian.strip() or None)
                if dev_show_email_links() and result.token:
                    st.code(_link(f"?verify={result.token}"), language=None)
            else:
                st.error(result.error)

    with forgot_tab:
        if not email_delivery_configured() and not dev_show_email_links():
            st.info(
                "Password reset needs a configured mail server (SMTP_HOST, or Supabase Auth). "
                "This deployment doesn't have one, so reset links can't be delivered."
            )
        with st.form("forgot"):
            email = st.text_input("Email", key="forgot_email")
            submitted = st.form_submit_button("Send reset link")
        if submitted:
            result = provider.request_password_reset(email)
            # Always the same message -- confirming whether an address is
            # registered would let anyone enumerate accounts.
            st.success("If that address has an account, a reset link is on its way.")
            if result.token:
                get_email_sender().send(password_reset_email(email, _link(f"?reset={result.token}")))
                if dev_show_email_links():
                    st.code(_link(f"?reset={result.token}"), language=None)


def _render_reset_form(provider, token: str) -> None:
    st.subheader("Choose a new password")
    with st.form("reset_form"):
        password = st.text_input("New password", type="password")
        submitted = st.form_submit_button("Set new password", type="primary")
    if submitted:
        result = provider.reset_password(token, password)
        if result.ok:
            st.query_params.clear()
            st.success("Password updated -- you can sign in with it now.")
        else:
            st.error(result.error)


def render_account_blocked(accounts: AccountLibrary, provider, user_id: int, reason: str) -> None:
    """Shown when a signed-in account isn't usable yet: unverified email,
    or a minor waiting on guardian consent."""
    user = accounts.get_user(user_id)
    st.title("🏎️ Karting Telemetry")
    st.warning(reason)

    if not user["email_verified"]:
        if st.button("Resend confirmation email"):
            result = provider.request_email_verification(user_id)
            if result.ok:
                if result.token:
                    get_email_sender().send(verification_email(user["email"], _link(f"?verify={result.token}")))
                    if dev_show_email_links():
                        st.code(_link(f"?verify={result.token}"), language=None)
                st.success("Sent.")
            else:
                st.error(result.error)

    elif is_minor(user["date_of_birth"]):
        st.caption(
            f"A consent request has gone to {user['guardian_email']}. The account stays inactive until they "
            "approve it."
        )
        if dev_show_email_links():
            st.code(_link(f"?consent={user_id}"), language=None)

    if st.button("Sign out"):
        sign_out()
        st.rerun()


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

# Auth runs before anything else is rendered: a signed-out visitor never
# gets as far as the sidebar or a page function, so no page has to defend
# itself against there being no current user.
accounts_lib = get_account_library()
auth_store = get_auth_store()
auth_provider = get_auth_provider(accounts_lib, auth_store)

# The guardian consent link is followed by a parent who has no account of
# their own, so it is handled before the sign-in gate rather than behind it.
if "consent" in st.query_params:
    _consent_user_id = int(st.query_params["consent"])
    _consent_user = accounts_lib.get_user(_consent_user_id)
    st.title("🏎️ Karting Telemetry")
    if _consent_user is None:
        st.error("That consent link doesn't match an account.")
    else:
        st.subheader(f"Permission for {_consent_user['display_name'] or _consent_user['email']}")
        st.write(
            "This account belongs to a driver under 16 and stays inactive until you approve it. It stores lap "
            "timing data from their kart's logger. Sessions are private by default and are only shared if they "
            "explicitly choose to share them."
        )
        approve_col, decline_col, _ = st.columns([1, 1, 3])
        if approve_col.button("Approve", type="primary"):
            accounts_lib.set_guardian_consent(_consent_user_id, CONSENT_GRANTED)
            st.query_params.clear()
            st.success("Approved. They can use the account now.")
        if decline_col.button("Decline"):
            accounts_lib.set_guardian_consent(_consent_user_id, "denied")
            st.query_params.clear()
            st.warning("Declined. The account stays inactive.")
    st.stop()

_signed_in_user_id = current_user_id()
if _signed_in_user_id is None:
    render_auth_gate(accounts_lib, auth_provider)
    st.stop()

_usable, _blocked_reason = accounts_lib.account_is_usable(_signed_in_user_id)
if not _usable:
    render_account_blocked(accounts_lib, auth_provider, _signed_in_user_id, _blocked_reason)
    st.stop()

current_user: dict = accounts_lib.get_user(_signed_in_user_id)
current_profile: dict = accounts_lib.get_profile_for_user(_signed_in_user_id)
if current_profile is None:
    # Every account gets a profile at registration; this only happens for a
    # row created some other way (a manual insert, an older build). Create
    # one rather than crashing every page that assumes it exists.
    _pid = accounts_lib.create_profile_for_user(
        _signed_in_user_id, current_user["display_name"] or current_user["email"]
    )
    current_profile = accounts_lib.get_profile(_pid)

st.sidebar.title("🏎️ Karting Telemetry")

page_overview_obj = st.Page(page_overview, title="Top 3 Focus Areas", icon="🎯", default=True)
page_my_sessions_obj = st.Page(page_my_sessions, title="My Sessions & Sharing", icon="🔒")
page_shared_laps_obj = st.Page(page_shared_laps, title="Shared Laps", icon="🤝")
page_leaderboards_obj = st.Page(page_leaderboards, title="Leaderboards", icon="🏆")
page_find_profile_obj = st.Page(page_find_profile, title="Find My Profile", icon="🔍")
page_lap_times_obj = st.Page(page_lap_times, title="Lap Times", icon="⏱️")
page_data_analysis_obj = st.Page(page_data_analysis, title="Data Analysis", icon="📈")
page_track_map_obj = st.Page(page_track_map, title="Track Map", icon="🗺️")
page_braking_rpm_obj = st.Page(page_braking_rpm, title="Braking / RPM", icon="🛞")
page_corner_comparison_obj = st.Page(page_corner_comparison, title="Corner Comparison", icon="📐")
page_lap_comparison_obj = st.Page(page_lap_comparison, title="Lap Comparison", icon="🔬")
page_recurring_patterns_obj = st.Page(page_recurring_patterns, title="Recurring Patterns", icon="🔁")
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
            page_braking_rpm_obj, page_corner_comparison_obj, page_lap_comparison_obj, page_recurring_patterns_obj,
            page_gearing_simulation_obj, page_consistency_obj, page_progression_obj, page_kart_setup_obj, page_history_obj,
        ],
        "Community": [page_shared_laps_obj, page_leaderboards_obj],
        "Account": [page_my_sessions_obj, page_find_profile_obj, page_settings_obj],
    }
)

st.sidebar.divider()
st.sidebar.caption(f"Signed in as **{current_profile['display_name']}**")
if st.sidebar.button("Sign out"):
    sign_out()
    st.rerun()

library = get_session_library()

# Scoped to this account: their own driver profile's confirmed sessions,
# anything they uploaded themselves, and other drivers' explicitly shared
# sessions. Nothing else is loaded, so no page can display a session the
# signed-in user isn't entitled to see.
sessions_meta = accounts_lib.visible_sessions_for_user(_signed_in_user_id)

# A tuple of DB ids, not the DataFrame itself, so this stays cheap to
# recompute every rerun while still giving load_persisted_sessions_cached a
# real cache-invalidation signal -- it only redoes the (comparatively
# expensive) unpickling work when a session is actually added or removed.
sessions_meta_key = tuple(sessions_meta["id"]) if not sessions_meta.empty else ()
all_sessions: list[tuple[str, Session]] = load_persisted_sessions_cached(library, sessions_meta, sessions_meta_key)

# (source_file, session_index, start_time) -> {db id, track name} -- the
# same identity triple SessionLibrary.find_session already matches a
# session on, used here so the Lap Comparison page can log corner metrics /
# pattern instances against the right session_db_id without re-querying
# the library on every rerun.
session_db_lookup: dict[tuple, dict] = {}
if not sessions_meta.empty:
    for _, _row in sessions_meta.iterrows():
        _key = (_row["source_file"], int(_row["session_index"]), _row["start_time"] if pd.notna(_row["start_time"]) else None)
        session_db_lookup[_key] = {
            "id": int(_row["id"]),
            "track_name": _row["track_name"] if pd.notna(_row["track_name"]) else None,
            "track_condition": _row["track_condition"] if "track_condition" in _row and pd.notna(_row["track_condition"]) else None,
        }

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
