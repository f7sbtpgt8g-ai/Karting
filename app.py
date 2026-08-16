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
import streamlit as st

from telemetry.comparison import cross_session_delta_trace, session_progression
from telemetry.corners import assign_segments, build_reference_segments, lap_gps_trace
from telemetry.delta import delta_time_trace, segment_times_for_lap, theoretical_best_lap
from telemetry.focus_areas import recurring_weaknesses, top_focus_areas
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
    segment_aggregates,
)
from telemetry.parser import Session, load_sessions
from telemetry.setup_config import KartSetup
from telemetry.setup_engine import all_setup_suggestions

st.set_page_config(page_title="Karting Telemetry", layout="wide", page_icon="🏎️")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Parsing telemetry file...")
def parse_uploaded_file(file_bytes: bytes, filename: str) -> list[Session]:
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


def compute_clean_laps(session: Session) -> pd.DataFrame:
    """Not cached: `Session` isn't stably hashable across reruns, and
    re-computing this from the already-parsed dataframe is cheap (ms-scale
    even for a full session) -- caching it risks silently returning another
    session's laps if two sessions happen to share cache-key inputs."""
    laps = flag_outlier_laps(lap_table(session))
    laps = detect_anomalous_laps(laps)
    return laps


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
        "coaching note for each. Load your kart setup alongside the telemetry to get setup-change hypotheses too."
    )
    st.stop()

session_labels = [label for label, _ in all_sessions]
active_label = st.sidebar.selectbox("Session to analyze", session_labels)
active_session = dict(all_sessions)[active_label]

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


# ---------------------------------------------------------------------------
# Headline: Top 3 focus areas
# ---------------------------------------------------------------------------

st.title(f"{driver_name} — Top 3 Focus Areas")
st.caption(f"Analyzing lap {analyzed_lap} · {active_label}")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Best lap", f"{summary['best_lap_s']:.2f}s")
col2.metric("Theoretical best", f"{theoretical_best_s:.2f}s", delta=f"-{summary['best_lap_s'] - theoretical_best_s:.2f}s available", delta_color="inverse")
col3.metric("Consistency (std dev)", f"{laps['lap_time_s'].std():.2f}s")
col4.metric("Clean laps", f"{len(clean)} / {len(laps)}")

focus_areas = top_focus_areas(active_session, analyzed_lap, segments, lap_segment_times, best_segment_times, n=3)

if not focus_areas:
    st.success("No significant time loss detected vs. your theoretical best in this lap -- nice and consistent!")
else:
    cards = st.columns(len(focus_areas))
    for i, (col, area) in enumerate(zip(cards, focus_areas), start=1):
        with col:
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
    st.dataframe(annotated[display_cols], use_container_width=True)
    st.caption("Rows flagged `is_outlier` are excluded from best/average stats above but shown here for review.")

# --- Speed & Delta ---
with tabs[1]:
    st.subheader("Speed trace comparison")
    compare_laps = st.multiselect("Laps to overlay", clean_lap_numbers, default=clean_lap_numbers[: min(4, len(clean_lap_numbers))])
    fig = go.Figure()
    for lap_no in compare_laps:
        trace = lap_gps_trace(active_session, lap_no)
        fig.add_trace(go.Scatter(x=trace["lap_distance_m"], y=trace["GPS Speed"], mode="lines", name=f"Lap {lap_no}"))
    fig.update_layout(xaxis_title="Distance (m)", yaxis_title="GPS Speed (km/h)", height=400)
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Delta-time trace vs. reference lap")
    reference_lap = st.selectbox("Reference lap", clean_lap_numbers, index=clean_lap_numbers.index(best_lap), key="ref_lap_delta")
    fig2 = go.Figure()
    for lap_no in compare_laps:
        if lap_no == reference_lap:
            continue
        dt = delta_time_trace(active_session, lap_no, reference_lap)
        fig2.add_trace(go.Scatter(x=dt["distance_m"], y=dt["delta_s"], mode="lines", name=f"Lap {lap_no} vs {reference_lap}"))
    fig2.add_hline(y=0, line_dash="dash", line_color="gray")
    fig2.update_layout(xaxis_title="Distance (m)", yaxis_title="Delta (s) — positive = time lost", height=400)
    st.plotly_chart(fig2, use_container_width=True)
    st.caption("Positive delta = slower than the reference lap at that point on track; negative = faster.")

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
    st.plotly_chart(fig3, use_container_width=True)
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
    st.plotly_chart(fig4, use_container_width=True)

# --- Braking / RPM ---
with tabs[4]:
    st.subheader("Braking zones (inferred — no brake channel in this export)")
    brake_lap = st.selectbox("Lap", clean_lap_numbers, index=clean_lap_numbers.index(best_lap), key="brake_lap")
    trace = lap_metric_trace(active_session, brake_lap)
    trace = add_braking_throttle_estimates(trace)
    zones = braking_zones(trace)
    st.dataframe(zones, use_container_width=True)

    st.subheader("RPM trace")
    fig5 = go.Figure()
    fig5.add_trace(go.Scatter(x=trace["lap_distance_m"], y=trace["RPM"], mode="lines", name="RPM"))
    if trace["RPM unfiltered"].notna().any():
        fig5.add_trace(go.Scatter(x=trace["lap_distance_m"], y=trace["RPM unfiltered"], mode="lines", name="RPM unfiltered", opacity=0.5))
    fig5.update_layout(xaxis_title="Distance (m)", yaxis_title="RPM", height=400)
    st.plotly_chart(fig5, use_container_width=True)

    st.subheader("Per-segment speed / RPM")
    agg = segment_aggregates(trace, segments)
    st.dataframe(agg, use_container_width=True)

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
    st.plotly_chart(fig6, use_container_width=True)
    st.caption("Red bars are flagged as outliers (in/out lap or statistical anomaly) and excluded from best/average stats.")

# --- Progression ---
with tabs[6]:
    st.subheader("Session-over-session progression")
    if len(all_sessions) < 2:
        st.info("Load more than one session (or a file with multiple sessions) to see progression across sessions.")
    else:
        progression = session_progression(all_sessions)
        st.dataframe(progression, use_container_width=True)
        fig7 = go.Figure()
        fig7.add_trace(go.Scatter(x=progression["session"], y=progression["best_lap_s"], mode="lines+markers", name="Best lap"))
        fig7.add_trace(go.Scatter(x=progression["session"], y=progression["average_lap_s"], mode="lines+markers", name="Average lap"))
        fig7.update_layout(xaxis_title="Session", yaxis_title="Lap time (s)", height=400)
        st.plotly_chart(fig7, use_container_width=True)

        st.subheader("Recurring weaknesses across loaded sessions")
        per_session_focus = {}
        for label, s in all_sessions:
            s_laps = clean_lap_table(compute_clean_laps(s))
            if s_laps.empty:
                continue
            s_clean_nums = s_laps["lap_number"].tolist()
            s_best_lap = int(s_laps.loc[s_laps["lap_time_s"].idxmin(), "lap_number"])
            s_segments = build_reference_segments(s, s_best_lap)
            _, s_best_seg_times = theoretical_best_lap(s, s_clean_nums, s_segments)
            s_lap_seg_times = segment_times_for_lap(s, s_best_lap, s_segments)
            per_session_focus[label] = top_focus_areas(s, s_best_lap, s_segments, s_lap_seg_times, s_best_seg_times, n=3)
        recurring = recurring_weaknesses(per_session_focus)
        if recurring.empty:
            st.info("No segment shows up as a top-3 focus area in more than one loaded session yet.")
        else:
            st.dataframe(recurring, use_container_width=True)
            st.caption("Segments appearing here are a recurring habit across sessions, not a one-off mistake.")

# --- Kart Setup ---
with tabs[7]:
    st.subheader("Kart setup")
    if "kart_setup" not in st.session_state:
        st.session_state.kart_setup = KartSetup(driver=driver_name)

    setup: KartSetup = st.session_state.kart_setup

    with st.form("setup_form"):
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

        submitted = st.form_submit_button("Save setup & run correlation engine")

    if submitted:
        st.session_state.kart_setup = setup
        st.success("Setup saved for this session (in-app only -- download below to persist it).")

    yaml_bytes = io.BytesIO()
    import yaml as _yaml

    yaml_bytes.write(_yaml.safe_dump(setup.to_dict(), sort_keys=False).encode())
    st.download_button("Download setup as YAML", yaml_bytes.getvalue(), file_name="kart_setup.yaml")

    st.subheader("Setup correlation suggestions")
    suggestions = all_setup_suggestions(active_session, clean_lap_numbers, segments, setup)
    for s in suggestions:
        with st.expander(f"{s['area'].replace('_', ' ').title()} — confidence: {s['confidence']}"):
            st.write(s.get("hypothesis", ""))
            if s.get("suggested_action"):
                st.markdown(f"**Suggested action:** {s['suggested_action']}")
            st.caption("This is a hypothesis inferred from telemetry patterns, not a direct sensor confirmation -- verify before acting on it.")

st.divider()
st.caption(
    "Braking, throttle/power-on, and jetting diagnostics are all inferred from RPM and GPS-derived G-forces -- "
    "there is no throttle, brake, gear, or EGT/lambda channel in this export. Treat those as estimates, not measurements."
)
