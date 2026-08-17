"""Streamlit UI for the karting telemetry analysis tool.

This file is UI orchestration only -- all parsing/analysis logic lives in
the `telemetry` package so it stays independently testable and reusable
(e.g. from `scripts/ingest.py` in a CI/automation context).
"""

from __future__ import annotations

import io
import os
import tempfile

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import yaml
from plotly.subplots import make_subplots
import streamlit as st

from telemetry.comparison import cross_session_delta_trace, session_progression
from telemetry.corners import assign_segments, build_reference_segments, lap_gps_trace
from telemetry.delta import delta_time_trace, segment_times_for_lap, theoretical_best_lap
from telemetry.focus_areas import blended_top_recommendations, recurring_weaknesses, top_focus_areas
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
    gg_diagram_points,
    lap_metric_trace,
    rpm_band_summary_across_laps,
    segment_aggregates,
    time_in_rpm_band,
)
from telemetry.parser import Session, load_sessions
from telemetry.setup_config import KartSetup
from telemetry.setup_engine import all_setup_suggestions

st.set_page_config(page_title="Karting Telemetry", layout="wide", page_icon="🏎️")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner="Parsing telemetry file...")
def parse_uploaded_file(file_bytes: bytes, filename: str) -> list[Session]:
    """`cache_resource`, not `cache_data`: returns the same Session objects
    across reruns (no deep-copy) so downstream per-session caches below stay
    warm -- Streamlit reruns this entire script on every interaction
    (including dragging the Speed & Delta position slider), and a 900k-row
    file takes ~10s to parse, so re-parsing on every rerun would make the
    app unusable at the track.
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


@st.cache_resource(show_spinner=False)
def compute_setup_suggestions_cached(
    _session: Session, _key: tuple, clean_lap_numbers: tuple, segments: pd.DataFrame, setup: KartSetup
) -> list[dict]:
    """The setup correlation engine loops over every clean lap several times
    over (~1s on a real 18-lap session) -- caching it means dragging the
    Speed & Delta position slider doesn't re-run it on every tick, since
    that's a full-script rerun in Streamlit regardless of which tab is open.
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
    setup.carburettor.main_jet = c1.number_input("Main jet", value=setup.carburettor.main_jet or 168, step=1)
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
for f in uploaded_files or []:
    for s in parse_uploaded_file(f.getvalue(), f.name):
        all_sessions.append((session_label(f.name, s), s))

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


# ---------------------------------------------------------------------------
# Upfront kart setup gate: asked once per file load, before any analysis is
# shown, so medium/high-confidence setup hypotheses can be folded into the
# Top 3 Focus Areas ranking rather than living only in a separate tab.
# ---------------------------------------------------------------------------

if "kart_setup" not in st.session_state:
    st.session_state.kart_setup = KartSetup(driver=driver_name)
if "setup_confirmed" not in st.session_state:
    st.session_state.setup_confirmed = False

if not st.session_state.setup_confirmed:
    st.title("Kart Setup")
    st.info(
        "Tell us your kart setup before diving into the analysis. If it points to a likely gearing, "
        "jetting, tyre-pressure, or chassis-balance issue, that'll be folded straight into your Top 3 "
        "Focus Areas rather than buried in a separate tab. You can skip this and fill it in later from "
        "the Kart Setup tab."
    )
    with st.form("onboarding_setup_form"):
        edited_setup = render_setup_fields(st.session_state.kart_setup)
        col_a, col_b = st.columns(2)
        continue_clicked = col_a.form_submit_button("Continue to analysis", type="primary")
        skip_clicked = col_b.form_submit_button("Skip for now (use defaults)")

    if continue_clicked:
        st.session_state.kart_setup = edited_setup
        st.session_state.setup_confirmed = True
        st.rerun()
    if skip_clicked:
        st.session_state.setup_confirmed = True
        st.rerun()
    st.stop()

setup: KartSetup = st.session_state.kart_setup


session_labels = [label for label, _ in all_sessions]
active_label = st.sidebar.selectbox("Session to analyze", session_labels)
active_session = dict(all_sessions)[active_label]

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

col1, col2, col3, col4 = st.columns(4)
col1.metric("Best lap", f"{summary['best_lap_s']:.2f}s")
col2.metric("Theoretical best", f"{theoretical_best_s:.2f}s", delta=f"-{summary['best_lap_s'] - theoretical_best_s:.2f}s available", delta_color="inverse")
col3.metric("Consistency (std dev)", f"{laps['lap_time_s'].std():.2f}s")
col4.metric("Clean laps", f"{len(clean)} / {len(laps)}")

focus_areas = blended_top_recommendations(
    active_session, analyzed_lap, segments, lap_segment_times, best_segment_times, setup_suggestions, n=3
)

if not focus_areas:
    st.success("No significant time loss detected vs. your theoretical best in this lap -- nice and consistent!")
else:
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

st.divider()


# ---------------------------------------------------------------------------
# Tabs: deeper technical views
# ---------------------------------------------------------------------------

tabs = st.tabs(
    [
        "Lap Times",
        "Speed & Delta",
        "G-G Diagram",
        "Track Map",
        "Braking / RPM",
        "Consistency",
        "Progression",
        "Kart Setup",
    ]
)

# --- Lap Times ---
with tabs[0]:
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
with tabs[1]:
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
        fig.update_layout(height=750, hovermode="x unified")

        col_chart, col_map = st.columns([2, 1])
        with col_map:
            map_lap_choice = st.selectbox("Show position for lap", compare_laps, index=0, key="speed_tab_map_lap")
            primary_trace = lap_traces[map_lap_choice].dropna(subset=["lap_distance_m", "Latitude", "Longitude"])
            max_dist = float(primary_trace["lap_distance_m"].max()) if not primary_trace.empty else 0.0
            position_m = st.slider(
                "Highlight position on track (m)", 0.0, max(max_dist, 0.1), 0.0,
                step=max(max_dist / 200, 0.1) if max_dist > 0 else 0.1,
            )

            map_fig = go.Figure()
            map_fig.add_trace(
                go.Scattergl(
                    x=primary_trace["Longitude"], y=primary_trace["Latitude"], mode="lines",
                    line=dict(color=lap_colors[map_lap_choice], width=2), showlegend=False,
                )
            )
            if not primary_trace.empty:
                lat_at = float(np.interp(position_m, primary_trace["lap_distance_m"], primary_trace["Latitude"]))
                lon_at = float(np.interp(position_m, primary_trace["lap_distance_m"], primary_trace["Longitude"]))
                map_fig.add_trace(
                    go.Scatter(x=[lon_at], y=[lat_at], mode="markers", marker=dict(size=16, color="red", line=dict(width=2, color="white")), showlegend=False)
                )
            map_fig.update_layout(height=680, yaxis=dict(scaleanchor="x"), xaxis_title="Longitude", yaxis_title="Latitude", margin=dict(t=10))
            st.plotly_chart(map_fig, width='stretch')
            st.caption(f"Lap {map_lap_choice} — drag the slider to move the marker along the track.")

        with col_chart:
            fig.add_vline(x=position_m, line_dash="dot", line_color="black", line_width=1)
            st.plotly_chart(fig, width='stretch')

# --- G-G Diagram ---
with tabs[2]:
    st.subheader("G-G diagram (friction circle)")
    gg_lap = st.selectbox("Lap", clean_lap_numbers, index=clean_lap_numbers.index(best_lap), key="gg_lap")
    trace = lap_metric_trace(active_session, gg_lap)
    points = gg_diagram_points(trace)
    fig3 = go.Figure()
    fig3.add_trace(
        go.Scatter(
            x=points["GPS Lateral Acceleration"],
            y=points["GPS Longitudinal Acceleration"],
            mode="markers",
            marker=dict(size=4, color=points["GPS Speed"], colorscale="Viridis", showscale=True, colorbar=dict(title="km/h")),
        )
    )
    fig3.update_layout(xaxis_title="Lateral G", yaxis_title="Longitudinal G", height=500, xaxis=dict(scaleanchor="y"))
    st.plotly_chart(fig3, width='stretch')
    st.caption("Points farther from the origin use more of the available grip. A tighter, rounder envelope usually means grip is being left on the table somewhere.")

# --- Track Map ---
with tabs[3]:
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
with tabs[4]:
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

# --- Consistency ---
with tabs[5]:
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
with tabs[6]:
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
with tabs[7]:
    st.subheader("Kart setup")
    st.caption("Edit and re-save your setup any time -- changes here update the Top 3 Focus Areas and correlation suggestions below on the next run.")

    with st.form("setup_form"):
        edited_setup = render_setup_fields(st.session_state.kart_setup)
        submitted = st.form_submit_button("Save setup & re-run correlation engine")

    if submitted:
        st.session_state.kart_setup = edited_setup
        setup = edited_setup
        st.success("Setup saved (in-app only -- download below to persist it). Re-run analysis to see it reflected in Top 3 Focus Areas.")

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

st.divider()
footer_caption = (
    "Braking, throttle/power-on, and jetting diagnostics are all inferred from RPM and GPS-derived G-forces -- "
    "there is no throttle, brake, gear, or EGT/lambda channel in this export. Treat those as estimates, not measurements."
)
if speed_is_estimated:
    footer_caption += " Speed itself is also estimated here, derived from GPS Distance since this export doesn't populate GPS Speed directly."
st.caption(footer_caption)
