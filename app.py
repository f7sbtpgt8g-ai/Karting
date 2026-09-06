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
import plotly.io as pio
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
    TEAM_MEMBERSHIP_ACTIVE,
    TEAM_MEMBERSHIP_PENDING,
    TEAM_ROLE_ADMIN,
    TEAM_ROLE_MANAGER,
    TEAM_ROLE_MEMBER,
    VISIBILITY_CHOICES,
    VISIBILITY_DEFAULT,
    VISIBILITY_PRIVATE,
    VISIBILITY_SHARED,
    VISIBILITY_TEAM,
    AccountLibrary,
    account_library_from_env,
    is_minor,
)
from telemetry.auth import AuthStore, LocalAuthProvider, auth_store_from_env, provider_from_env
from telemetry.mailer import (
    OutboxEmailSender,
    SupabaseOutboxEmailSender,
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
from telemetry.storage import SessionLibrary, session_library_from_env

st.set_page_config(page_title="Karting Telemetry", layout="wide", page_icon="🏎️")

# ---------------------------------------------------------------------------
# App-wide dark theme -- design_handoff_karting_telemetry/README.md's 1a
# ("channel-stack, pit-wall dark") token palette and type system, applied to
# every page (not just Lap Analysis, which additionally rebuilds its own
# layout to match the mockup's specific content). Two parts:
#   1. `_inject_global_theme_css()` -- one CSS block, injected once here,
#      re-skinning Streamlit's own chrome (sidebar, buttons, inputs,
#      dataframes, metrics, expanders, headings) everywhere.
#   2. `KARTING_DARK_TEMPLATE` -- a Plotly template matching the same
#      palette, registered as the default so every chart on every page
#      (most of which build a plain `go.Figure()`/`make_subplots()` with no
#      per-page styling) picks up the dark look automatically.
# This is route 1 from that README ("custom CSS + Plotly... expect the
# chrome to be approximate; the charts can be exact") applied globally
# rather than to one page -- a from-scratch bespoke layout for all ~20
# pages the way Lap Analysis got was judged not worth the risk/time for
# pages whose own content/interactions aren't changing, versus one shared
# theme layer that make every page consistently look and feel like the
# design system.
# ---------------------------------------------------------------------------

_DA1A = {  # design 1a's dark token palette (README "Design tokens" table)
    "canvas": "#0b0d0f",
    "surface": "#0d1114",
    "surface_raised": "#101417",
    "row_alt": "#0f1316",
    "row_selected": "#181e22",
    "ink": "#eef0f1",
    "ink2": "#c9cfd4",
    "ink_muted": "#8c959c",
    "ink_faint": "#6d767d",
    "ink_faint2": "#565f66",
    "hairline": "rgba(255,255,255,.10)",
    "hairline_strong": "rgba(255,255,255,.14)",
    "neutral_bar": "#22282c",
    "neutral_bar2": "#2a3136",
    "accent": "#ff3b1f",
    "gain": "#2fd07a",
    "gain_card": "#3ddb85",
    "loss": "#ff4a3d",
    "loss_card": "#ff6a58",
    "reference": "#b06cff",
    "theoretical": "#ffd23d",
}


def _inject_global_theme_css() -> None:
    t = _DA1A
    st.markdown(
        f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap');

html, body, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {{
    background: {t['canvas']};
    font-family: 'Archivo', sans-serif;
    color: {t['ink']};
}}
[data-testid="stHeader"] {{ background: transparent; }}
/* No blanket `* {{ font-family }}` override here on purpose: font-family
   already inherits from the rule above to every descendant that doesn't
   set its own, and a blanket override was clobbering Streamlit's Material
   Symbols icon glyphs (they're rendered as literal text -- "keyboard_
   double_arrow_left" instead of a chevron -- once their icon font is
   overridden), breaking icons across the whole app (sidebar collapse
   arrow, expander chevrons, password visibility toggle, file uploader).
   The .streamlit/config.toml `font`/`headingFont` keys are the supported,
   icon-safe way this app sets its base typeface. */

h1, h2, h3, h4, h5, h6, [data-testid="stMarkdownContainer"] h1, [data-testid="stMarkdownContainer"] h2,
[data-testid="stMarkdownContainer"] h3 {{
    color: {t['ink']} !important; font-family: 'Archivo', sans-serif; font-weight: 700;
}}
[data-testid="stCaptionContainer"], .stCaption {{ color: {t['ink_muted']} !important; }}
[data-testid="stMarkdownContainer"] p {{ color: {t['ink2']}; }}
hr {{ border-color: {t['hairline']} !important; }}

/* Top navigation (st.navigation(position="top")) -- no sidebar in this
   app at all any more; every page's nav lives in this top bar instead. */
[data-testid="stTopNav"] a[aria-current="page"] {{
    box-shadow: inset 0 -2px 0 {t['accent']}; color: {t['ink']} !important;
}}
[data-testid="stTopNav"] a {{ font-family: 'Archivo', sans-serif; font-weight: 600; }}
.st-key-app_top_bar, .st-key-app_session_bar {{
    border-bottom: 1px solid {t['hairline']}; padding-bottom: 10px; margin-bottom: 12px;
}}

/* Numbers -- tabular mono everywhere a metric/dataframe cell shows one */
[data-testid="stMetricValue"], [data-testid="stMetricDelta"], .stDataFrame, [data-testid="stTable"] {{
    font-family: 'JetBrains Mono', monospace;
}}
[data-testid="stMetric"] {{
    background: {t['surface_raised']}; border: 1px solid {t['hairline']}; border-radius: 5px; padding: 10px 14px;
}}
[data-testid="stMetricLabel"] {{ color: {t['ink_faint']} !important; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; font-size: 11px !important; }}
[data-testid="stMetricValue"] {{ color: {t['ink']} !important; }}

/* Buttons */
.stButton button, .stDownloadButton button, .stFormSubmitButton button {{
    background: {t['neutral_bar']}; color: {t['ink']}; border: 1px solid {t['hairline']}; border-radius: 3px;
    font-family: 'Archivo', sans-serif; font-weight: 600;
}}
.stButton button:hover, .stDownloadButton button:hover, .stFormSubmitButton button:hover {{
    border-color: {t['accent']}; color: {t['ink']};
}}
.stButton button[kind="primary"], .stFormSubmitButton button[kind="primary"] {{
    background: {t['accent']}; color: #17100e; border: none;
}}

/* Inputs / selects / checkboxes */
.stTextInput input, .stNumberInput input, .stDateInput input, .stTextArea textarea,
[data-baseweb="select"] > div, [data-baseweb="input"], [data-testid="stTextInputRootElement"] {{
    background: {t['surface_raised']} !important; color: {t['ink']} !important; border-color: {t['hairline']} !important;
}}
/* The built-in show/hide-password icon button renders with the light theme's
   own background by default -- without this override it shows as a stray
   white square over a dark input. */
[data-testid="stTextInputRootElement"] button {{
    background: transparent !important; color: {t['ink_muted']} !important;
}}
[data-testid="stTextInputRootElement"] [data-testid="stIconMaterial"] {{ color: {t['ink_muted']} !important; }}
[data-testid="stWidgetLabel"] p {{ color: {t['ink_faint']} !important; font-size: 12px; font-weight: 600; }}

/* Containers: expander, tabs, dataframe/table chrome, alerts */
[data-testid="stExpander"], [data-testid="stDataFrame"], [data-testid="stTable"] {{
    background: {t['surface_raised']}; border: 1px solid {t['hairline']} !important; border-radius: 5px;
}}
[data-testid="stTabs"] [data-baseweb="tab"] {{ color: {t['ink_muted']}; font-weight: 600; }}
[data-testid="stTabs"] [aria-selected="true"] {{ color: {t['ink']}; box-shadow: inset 0 -2px 0 {t['accent']}; }}
div[data-testid="stAlertContainer"] {{ border-radius: 5px; }}
[data-testid="stSuccess"] {{ background: rgba(47,208,122,.12); }}
[data-testid="stError"] {{ background: rgba(255,74,61,.12); }}
[data-testid="stWarning"] {{ background: rgba(255,210,61,.10); }}
[data-testid="stInfo"] {{ background: rgba(176,108,255,.10); }}
</style>
""",
        unsafe_allow_html=True,
    )


# A Plotly template mirroring the same tokens, registered as the default so
# every existing `go.Figure()`/`make_subplots()` across every page (most of
# which apply no per-figure styling of their own) renders dark/tokenized
# automatically -- the single highest-leverage move for making "every
# chart in the app" match design 1a without hand-restyling each one.
KARTING_DARK_TEMPLATE = go.layout.Template(
    layout=go.Layout(
        paper_bgcolor=_DA1A["surface"],
        plot_bgcolor=_DA1A["surface"],
        font=dict(family="Archivo, sans-serif", size=12, color=_DA1A["ink2"]),
        title=dict(font=dict(color=_DA1A["ink"])),
        colorway=[
            _DA1A["accent"], _DA1A["reference"], _DA1A["gain"], _DA1A["theoretical"],
            "#4d7cff", _DA1A["loss"], _DA1A["ink_muted"], "#8c564b",
        ],
        xaxis=dict(
            gridcolor=_DA1A["hairline"], zerolinecolor=_DA1A["hairline"], linecolor=_DA1A["hairline_strong"],
            tickfont=dict(color=_DA1A["ink_muted"]), title=dict(font=dict(color=_DA1A["ink_faint"])),
        ),
        yaxis=dict(
            gridcolor=_DA1A["hairline"], zerolinecolor=_DA1A["hairline"], linecolor=_DA1A["hairline_strong"],
            tickfont=dict(color=_DA1A["ink_muted"]), title=dict(font=dict(color=_DA1A["ink_faint"])),
        ),
        legend=dict(font=dict(color=_DA1A["ink2"]), bgcolor="rgba(0,0,0,0)"),
        coloraxis=dict(colorbar=dict(tickfont=dict(color=_DA1A["ink_muted"]))),
        hoverlabel=dict(bgcolor=_DA1A["surface_raised"], font=dict(color=_DA1A["ink"], family="JetBrains Mono, monospace")),
    )
)
pio.templates["karting_dark"] = KARTING_DARK_TEMPLATE
pio.templates.default = "karting_dark"

_inject_global_theme_css()

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
def get_account_library():
    """Postgres/Supabase-backed when SUPABASE_DB_URL/DATABASE_URL is set,
    the local SQLite AccountLibrary otherwise -- see
    `telemetry.accounts.account_library_from_env`."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return account_library_from_env(DB_PATH)


@st.cache_resource(show_spinner=False)
def get_auth_store():
    """Postgres/Supabase-backed when SUPABASE_DB_URL/DATABASE_URL is set,
    the local SQLite AuthStore otherwise -- see
    `telemetry.auth.auth_store_from_env`."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return auth_store_from_env(DB_PATH)


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
    return not isinstance(get_email_sender(), (OutboxEmailSender, SupabaseOutboxEmailSender))


def dev_show_email_links() -> bool:
    """Print links that would have been emailed straight onto the page.

    Off unless explicitly enabled, and deliberately so: showing a password
    reset link for an arbitrary address on screen is account takeover, not
    a convenience. Intended only for local development against the outbox
    sender."""
    return os.environ.get("KARTING_DEV_SHOW_EMAIL_LINKS", "").strip().lower() in ("1", "true", "yes")


@st.cache_resource(show_spinner=False)
def get_session_library():
    """Postgres/Supabase-backed when SUPABASE_DB_URL/DATABASE_URL is set --
    the recommended production setup, since it survives a container
    reboot/redeploy. Falls back to the local-SQLite-file SessionLibrary
    otherwise, which does NOT survive a redeploy on a platform without
    persistent disk (e.g. Streamlit Community Cloud) -- a within-run/
    within-deploy convenience in that mode, not durable history. See
    `telemetry.storage.session_library_from_env`.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return session_library_from_env(DB_PATH)


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


def render_mobile_linked_chart(
    chart_fig: go.Figure, map_fig: go.Figure, dist: list, lat: list, lon: list,
    chart_height: int = 420, map_height: int = 170,
) -> None:
    """Mobile-optimized counterpart to `render_linked_speed_delta`: one
    full-width chart at a time (the caller already picked the metric)
    stacked below a compact track map, in a single `components.html` block
    so the same hover/tap-to-mark-position wiring works with no server
    round-trip.

    Built around touch interaction specifically, which the desktop
    component (side-by-side layout, hover-only, mouse-oriented rubber-band
    zoom box) is not:
    - `dragmode: "pan"` + `scrollZoom: true`, so a one-finger drag scrolls
      across the lap -- the literal "scroll across" ask -- and a pinch
      zooms in for detail, instead of a drag drawing a box-zoom selection
      that doesn't correspond to any natural touch gesture.
    - The map marker updates on *tap* (`plotly_click`) as well as hover,
      since touch devices don't reliably fire hover from a tap the way a
      mouse fires it from a pointer move.
    - The y-axis re-fits to whatever's actually visible after a pan/zoom
      (recomputed client-side from each trace's own data on every
      `plotly_relayout`), so zooming into one corner doesn't leave the
      y-axis stretched to the whole lap's range with the detail you zoomed
      in for squashed flat.
    - No sticky-scroll tracking hack: unlike the desktop page's stack of
      2-5 subplots, one chart at a time is short enough that the whole
      component fits without internal scrolling, so the map can just sit
      in normal document flow above the chart rather than needing to track
      the page's scroll position.
    """
    chart_spec = chart_fig.to_json()
    map_spec = map_fig.to_json()
    marker_trace_index = len(map_fig.data) - 1

    html = f"""
<div style="width:100%; font-family:inherit;">
  <div id="mapDiv" style="width:100%; height:{map_height}px;"></div>
  <div id="chartDiv" style="width:100%; height:{chart_height}px; margin-top:6px;"></div>
  <div style="text-align:right; margin-top:4px;">
    <button id="resetZoomBtn" type="button"
      style="font-size:12px; padding:4px 12px; border-radius:6px; border:1px solid #999; background:#f5f5f5; color:#333; cursor:pointer;">
      ↺ Reset zoom
    </button>
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
  chartSpec.layout.height = {chart_height};
  chartSpec.layout.dragmode = "pan";
  mapSpec.layout.height = {map_height};

  var chartConfig = {{displayModeBar: false, scrollZoom: true, doubleClick: "reset+autosize", responsive: true}};
  Plotly.newPlot(chartDiv, chartSpec.data, chartSpec.layout, chartConfig);
  Plotly.newPlot(mapDiv, mapSpec.data, mapSpec.layout, {{displayModeBar: false, responsive: true}});

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

  function markAt(x) {{
    var p = interpAt(x);
    if (p && p.lat != null && p.lon != null) {{
      Plotly.restyle(mapDiv, {{x: [[p.lon]], y: [[p.lat]]}}, [markerTraceIndex]);
    }}
    Plotly.relayout(chartDiv, {{
      shapes: [{{type: "line", xref: "x", yref: "paper", x0: x, x1: x, y0: 0, y1: 1,
                 line: {{color: "rgba(90,90,90,0.6)", width: 1, dash: "dot"}}}}]
    }});
  }}

  chartDiv.on("plotly_hover", function(evt) {{
    if (evt.points && evt.points.length) markAt(evt.points[0].x);
  }});
  chartDiv.on("plotly_click", function(evt) {{
    if (evt.points && evt.points.length) markAt(evt.points[0].x);
  }});

  // Re-fit the y-axis to whatever's actually visible after a pan/zoom --
  // otherwise zooming into one corner leaves the y-axis stretched to the
  // whole lap's range and the detail you zoomed in for looks flat.
  function visibleYRange(x0, x1) {{
    var lo = Infinity, hi = -Infinity;
    chartDiv.data.forEach(function(trace) {{
      if (!trace.x || !trace.y) return;
      for (var i = 0; i < trace.x.length; i++) {{
        var xv = trace.x[i];
        if (xv >= x0 && xv <= x1) {{
          var yv = trace.y[i];
          if (yv === null || yv === undefined) continue;
          if (yv < lo) lo = yv;
          if (yv > hi) hi = yv;
        }}
      }}
    }});
    if (!isFinite(lo) || !isFinite(hi)) return null;
    if (lo === hi) {{ lo -= 1; hi += 1; }}
    var pad = (hi - lo) * 0.08;
    return [lo - pad, hi + pad];
  }}

  chartDiv.on("plotly_relayout", function(evt) {{
    if (evt["xaxis.autorange"]) {{
      Plotly.relayout(chartDiv, {{"yaxis.autorange": true}});
      return;
    }}
    var x0 = evt["xaxis.range[0]"], x1 = evt["xaxis.range[1]"];
    if (x0 === undefined || x1 === undefined) return;
    var yr = visibleYRange(x0, x1);
    if (yr) {{
      Plotly.relayout(chartDiv, {{"yaxis.range": yr, "yaxis.autorange": false}});
    }}
  }});

  document.getElementById("resetZoomBtn").addEventListener("click", function() {{
    Plotly.relayout(chartDiv, {{"xaxis.autorange": true, "yaxis.autorange": true, shapes: []}});
  }});
}})();
</script>
"""
    components.html(html, height=chart_height + map_height + 70, scrolling=False)


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
    "rank": "Rank",
    "driver_display_name": "Driver",
    "team_name": "Team",
    "fastest_driver_name": "Fastest Driver",
    "member_count": "Members",
    "role": "Role",
    "qualifying_sessions": "Sessions",
    "track_condition": "Conditions",
    "kart_class": "Class",
    "attribution_status": "Status",
    "visibility": "Visibility",
    "temperature_c": "Temp (°C)",
    "humidity_pct": "Humidity (%)",
    "pressure_hpa": "Pressure (hPa)",
    "altitude_m": "Altitude (m)",
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

_HOME_SESSION_TYPE_COLORS = {
    "practice": _DA1A["ink_muted"], "qualifying": _DA1A["theoretical"], "race": _DA1A["accent"],
}


def _home_inject_css() -> None:
    t = _DA1A
    st.markdown(
        f"""
<style>
.st-key-home_root {{ font-family: 'Archivo', sans-serif; }}
.home-stat-row {{ display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }}
.home-stat-card {{
    background: {t['surface_raised']}; border: 1px solid {t['hairline']}; border-radius: 6px;
    padding: 10px 18px; min-width: 110px;
}}
.home-stat-card .da1a-label {{ margin-bottom: 2px; }}
.home-stat-value {{ font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 22px; color: {t['ink']}; }}
.home-driver-header {{
    display: flex; align-items: baseline; gap: 10px; margin: 18px 0 8px 0; padding-bottom: 6px;
    border-bottom: 1px solid {t['hairline']};
}}
.home-driver-header .name {{ font-size: 16px; font-weight: 700; color: {t['ink']}; }}
.home-driver-header .meta {{ font-size: 11px; color: {t['ink_muted']}; }}
.home-badge {{
    display: inline-block; font-size: 9px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
    padding: 2px 7px; border-radius: 3px; background: {t['neutral_bar']}; color: {t['ink2']}; white-space: nowrap;
}}
.home-track {{ font-weight: 700; font-size: 13px; color: {t['ink']}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.home-meta {{ font-size: 11px; color: {t['ink_muted']}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.home-best-lap {{ font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 14px; color: {t['ink']}; text-align: right; }}
.home-badges-cell {{ white-space: nowrap; overflow: hidden; }}

/* Compact 1-row-per-session list -- tight padding on the bordered row
   container (Streamlit's default has enough vertical padding to make a
   single-line row look two lines tall) and no visible checkbox label. */
.st-key-home_root [data-testid="stVerticalBlockBorderWrapper"] {{ margin-bottom: 2px; }}
.home-row-container [data-testid="stVerticalBlock"] > [data-testid="stElementContainer"] {{ margin: 0; }}
.home-row [data-testid="stHorizontalBlock"] {{ align-items: center; }}
.home-row {{ padding: 3px 8px; }}
.home-row [data-testid="stCheckbox"] label {{ min-height: 0; }}
.home-row [data-testid="stCheckbox"] p {{ font-size: 0; }}  /* hide the "Compare" text, keep the box */

/* Sortable column headers rendered as tertiary buttons -- look like plain
   labels, not action buttons. */
.st-key-home_header_row button {{
    background: transparent !important; border: none !important; padding: 2px 4px !important;
    font-family: 'Archivo', sans-serif; font-weight: 600; font-size: 10px; letter-spacing: .08em;
    text-transform: uppercase; color: {t['ink_faint']} !important; box-shadow: none !important;
}}
.st-key-home_header_row button:hover {{ color: {t['ink']} !important; }}
</style>
""",
        unsafe_allow_html=True,
    )


def _home_badge(text: str, color: str | None = None) -> str:
    style = f' style="color:{color}; border:1px solid {color};background:transparent;"' if color else ""
    return f'<span class="home-badge"{style}>{text}</span>'


def _home_sort_header(container, label: str, col_key: str, default_desc: bool) -> None:
    """One clickable column-header button. Clicking the already-active
    column flips its direction; clicking a different one switches to it at
    `default_desc` -- descending for "most recent"/"slowest first"-style
    defaults (Date), ascending for "fastest first" (Best lap) or
    alphabetical (Track)."""
    active = st.session_state.get("home_sort_col") == col_key
    arrow = ("▼" if st.session_state.get("home_sort_desc") else "▲") if active else "⇅"
    if container.button(f"{label} {arrow}", key=f"home_sort_btn_{col_key}", type="tertiary"):
        if active:
            st.session_state["home_sort_desc"] = not st.session_state["home_sort_desc"]
        else:
            st.session_state["home_sort_col"] = col_key
            st.session_state["home_sort_desc"] = default_desc
        st.rerun()


def _home_seed_lap_comparison(selected_labels: list[str]) -> None:
    """Pre-fills the Lap Comparison page's row picker with one row per
    selected session (its own fastest clean lap), the same session_state
    keys `page_lap_comparison` already reads -- see that page's "Laps to
    compare" expander."""
    st.session_state["lc_row_ids"] = list(range(len(selected_labels)))
    st.session_state["lc_next_row_id"] = len(selected_labels)
    sessions_by_label = dict(all_sessions)
    for i, label in enumerate(selected_labels):
        st.session_state[f"lc_session_{i}"] = label
        session_obj = sessions_by_label.get(label)
        if session_obj is None:
            continue
        numbers, times = _session_clean_laps(session_obj)
        if numbers:
            st.session_state[f"lc_lap_{i}"] = min(numbers, key=lambda n: times[n])


def page_home() -> None:
    """Landing page: every session the signed-in account can see, grouped
    by driver, filterable and sortable -- see the chat request this
    implements for the reference screenshot (a third-party app's "Driver
    Database" screen). Recreated in this app's own dark token palette, not
    that screenshot's colors/chrome.

    Scope, deliberately narrower than `sessions_meta` (which also includes
    every OTHER driver's publicly-shared session, needed elsewhere for
    cross-driver comparison pickers): a regular account sees only its own
    sessions here; a team manager/admin additionally sees every teammate's
    session already visible to the team (sessions_meta's own team-visibility
    join already restricts those to 'team'/'shared' ones -- this page does
    not grant any access `visible_sessions_for_user` didn't already grant).
    """
    _home_inject_css()
    root = st.container(key="home_root")
    with root:
        st.title("🏠 Home")

        if sessions_meta.empty:
            st.info(
                "No sessions yet. Upload one on the Settings page to get started -- once you have, they'll show up "
                "here, organized by driver."
            )
            render_footer()
            return

        my_profile_id = int(current_profile["id"])
        scope_ids = {my_profile_id}
        is_team_elevated = False
        if (
            _active_team_membership is not None
            and _active_team_membership["status"] == TEAM_MEMBERSHIP_ACTIVE
            and _active_team_membership["role"] in (TEAM_ROLE_MANAGER, TEAM_ROLE_ADMIN)
        ):
            is_team_elevated = True
            roster = accounts_lib.team_roster(int(_active_team_membership["team_id"]))
            scope_ids |= {int(x) for x in roster["driver_profile_id"]}

        labels_by_row = [label for label, _ in all_sessions]
        meta = sessions_meta.reset_index(drop=True).copy()
        meta["label"] = labels_by_row
        # `driver_profile_id` reads back as float64 (nullable columns elsewhere
        # in `sessions` force the whole read to upcast), so compare via a
        # notna-guarded int cast rather than a plain `.isin(scope_ids)` --
        # a float/int-set membership check that happened to always match
        # here isn't something to rely on across pandas/driver versions.
        scoped = meta[meta["driver_profile_id"].apply(lambda x: pd.notna(x) and int(x) in scope_ids)].copy()

        if scoped.empty:
            st.info("No sessions in scope yet -- upload one, or (if you manage a team) wait for a teammate to share one.")
            render_footer()
            return

        if is_team_elevated:
            st.caption(f"Showing your sessions plus every teammate's team-or-shared session -- you're this team's {_active_team_membership['role']}.")

        # -- header stat cards (reflect the full in-scope set, before the
        # filters below are applied) -------------------------------------
        stat_cells = [
            ("Drivers", scoped["driver_profile_id"].nunique()),
            ("Sessions", len(scoped)),
            ("Total laps", int(scoped["n_laps"].fillna(0).sum())),
            ("Tracks", scoped["track_name"].nunique()),
        ]
        st.markdown(
            '<div class="home-stat-row">' + "".join(
                f'<div class="home-stat-card"><div class="da1a-label">{label}</div>'
                f'<div class="home-stat-value">{value}</div></div>'
                for label, value in stat_cells
            ) + "</div>",
            unsafe_allow_html=True,
        )

        # -- filters ---------------------------------------------------------
        f1, f2, f3, f4 = st.columns(4)
        track_options = ["All tracks"] + sorted(scoped["track_name"].dropna().unique().tolist())
        track_pick = f1.selectbox("Track", track_options, key="home_filter_track")
        type_options = ["All types"] + sorted(scoped["session_type"].dropna().unique().tolist())
        type_pick = f2.selectbox("Session type", type_options, key="home_filter_type")
        condition_options = ["All conditions"] + [c for c in CONDITION_OPTIONS if c in scoped["track_condition"].dropna().unique()]
        condition_pick = f3.selectbox("Conditions", condition_options, key="home_filter_condition")

        valid_dates = pd.to_datetime(scoped["start_date"], errors="coerce").dropna()
        if not valid_dates.empty:
            date_range = f4.date_input(
                "Date range", value=(valid_dates.min().date(), valid_dates.max().date()),
                min_value=valid_dates.min().date(), max_value=valid_dates.max().date(), key="home_filter_dates",
            )
        else:
            date_range = None

        filtered = scoped.copy()
        if track_pick != "All tracks":
            filtered = filtered[filtered["track_name"] == track_pick]
        if type_pick != "All types":
            filtered = filtered[filtered["session_type"] == type_pick]
        if condition_pick != "All conditions":
            filtered = filtered[filtered["track_condition"] == condition_pick]
        if date_range and isinstance(date_range, tuple) and len(date_range) == 2:
            parsed = pd.to_datetime(filtered["start_date"], errors="coerce")
            filtered = filtered[(parsed.dt.date >= date_range[0]) & (parsed.dt.date <= date_range[1])]

        st.session_state.setdefault("home_sort_col", "start_date")
        st.session_state.setdefault("home_sort_desc", True)
        filtered = filtered.sort_values(
            st.session_state["home_sort_col"], ascending=not st.session_state["home_sort_desc"], na_position="last",
        )

        if filtered.empty:
            st.info("No sessions match these filters.")
            render_footer()
            return

        # Column widths shared between the clickable header row and every
        # session row below it, so the two stay aligned.
        ROW_WIDTHS = [2.3, 1.6, 1.7, 1.0, 0.4, 0.4, 0.4, 0.4, 1.3]

        header_row = st.container(key="home_header_row")
        with header_row:
            hc = st.columns(ROW_WIDTHS)
            _home_sort_header(hc[0], "Track", "track_name", default_desc=False)
            _home_sort_header(hc[1], "Date", "start_date", default_desc=True)
            hc[2].markdown('<div class="da1a-label" style="padding:6px 0;">Type / class / conditions</div>', unsafe_allow_html=True)
            _home_sort_header(hc[3], "Best lap", "best_lap_s", default_desc=False)

        # -- grouped-by-driver session list -----------------------------------
        # `selected_labels` is rebuilt fresh from each checkbox's own return
        # value on every render (each checkbox already persists its own
        # checked state across reruns via its `key` -- mirroring that into a
        # second, separately-tracked set as well would just be two sources
        # of truth that can drift, e.g. after "Compare" below clears one but
        # not the other).
        selected_labels: list[tuple[int, str]] = []
        driver_order = sorted(
            filtered["driver_profile_id"].dropna().unique(),
            key=lambda pid: (pid != my_profile_id, str(filtered.loc[filtered["driver_profile_id"] == pid, "driver_display_name"].iloc[0])),
        )
        on_a_team = accounts_lib.get_active_membership_for_profile(my_profile_id) is not None
        visibility_options = list(VISIBILITY_CHOICES) if on_a_team else [VISIBILITY_PRIVATE, VISIBILITY_SHARED]
        visibility_labels = {VISIBILITY_PRIVATE: "Private", VISIBILITY_TEAM: "Team", VISIBILITY_SHARED: "Shared"}

        for pid in driver_order:
            group = filtered[filtered["driver_profile_id"] == pid]
            driver_name = group["driver_display_name"].iloc[0] or "Unknown driver"
            is_mine = int(pid) == my_profile_id
            st.markdown(
                f'<div class="home-driver-header"><span class="name">{"👤 " if is_mine else "🏁 "}{driver_name}'
                f'{" (you)" if is_mine else ""}</span>'
                f'<span class="meta">{len(group)} session(s) · {group["track_name"].nunique()} track(s)</span></div>',
                unsafe_allow_html=True,
            )
            for _, row in group.iterrows():
                row_id = int(row["id"])
                with st.container(border=True, key=f"home_row_{row_id}"):
                    st.markdown('<div class="home-row">', unsafe_allow_html=True)
                    c1, c2, c3, c4, c5, c6, c7, c8, c9 = st.columns(ROW_WIDTHS)
                    c1.markdown(f'<div class="home-track">{row["track_name"] or "Unknown track"}</div>', unsafe_allow_html=True)
                    c2.markdown(
                        f'<div class="home-meta">{row["start_date"] or "?"} · {row["start_time"] or "?"} · '
                        f'{int(row["n_laps"]) if pd.notna(row["n_laps"]) else 0} laps</div>',
                        unsafe_allow_html=True,
                    )
                    badges = _home_badge(row["session_type"] or "unknown", _HOME_SESSION_TYPE_COLORS.get(row["session_type"]))
                    if pd.notna(row.get("kart_class")):
                        badges += " " + _home_badge(row["kart_class"])
                    if pd.notna(row.get("track_condition")):
                        badges += " " + _home_badge(row["track_condition"])
                    c3.markdown(f'<div class="home-badges-cell">{badges}</div>', unsafe_allow_html=True)
                    c4.markdown(
                        f'<div class="home-best-lap">{_da1a_time_str(row["best_lap_s"]) if pd.notna(row["best_lap_s"]) else "—"}</div>',
                        unsafe_allow_html=True,
                    )

                    checked = c5.checkbox("Compare", key=f"home_compare_{row_id}", label_visibility="collapsed")
                    if checked:
                        selected_labels.append((row_id, row["label"]))

                    if c6.button("🔧", key=f"home_setup_{row_id}", type="tertiary", help="Kart setup"):
                        st.session_state["_pending_active_session"] = row["label"]
                        st.switch_page(page_kart_setup_obj)

                    if is_mine:
                        with c7.popover("✏️", help="Edit session details"):
                            new_track = st.text_input("Track name", value=row["track_name"] or "", key=f"home_edit_track_{row_id}")
                            type_choices = ["practice", "qualifying", "race"]
                            current_type = row["session_type"] if row["session_type"] in type_choices else "practice"
                            new_type = st.selectbox("Session type", type_choices, index=type_choices.index(current_type), key=f"home_edit_type_{row_id}")
                            cond_choices = ["Unknown"] + CONDITION_OPTIONS
                            current_cond = row["track_condition"] if row["track_condition"] in CONDITION_OPTIONS else "Unknown"
                            new_cond = st.selectbox("Conditions", cond_choices, index=cond_choices.index(current_cond), key=f"home_edit_cond_{row_id}")
                            current_vis = row["visibility"] if row["visibility"] in visibility_options else VISIBILITY_PRIVATE
                            new_vis = st.selectbox(
                                "Visibility", visibility_options, index=visibility_options.index(current_vis),
                                format_func=lambda v: visibility_labels[v], key=f"home_edit_vis_{row_id}",
                            )
                            if st.button("Save", key=f"home_edit_save_{row_id}", type="primary"):
                                accounts_lib.update_session_details(
                                    row_id, new_track.strip() or None, new_type, None if new_cond == "Unknown" else new_cond,
                                )
                                if new_vis != row["visibility"]:
                                    accounts_lib.set_session_visibility(row_id, new_vis)
                                st.rerun()
                        with c8.popover("🗑️", help="Delete this session"):
                            st.write(f"Delete this session at **{row['track_name'] or 'Unknown track'}**? This can't be undone.")
                            if st.button("Confirm delete", key=f"home_delete_confirm_{row_id}", type="primary"):
                                library.delete_session(row_id)
                                st.rerun()
                    else:
                        c7.write("")
                        c8.write("")

                    if c9.button("Open →", key=f"home_open_{row_id}", type="primary", use_container_width=True):
                        st.session_state["_pending_active_session"] = row["label"]
                        st.switch_page(page_data_analysis_obj)
                    st.markdown("</div>", unsafe_allow_html=True)

        if selected_labels:
            st.divider()
            if st.button(f"🔬 Compare {len(selected_labels)} selected session(s) →", type="primary"):
                _home_seed_lap_comparison([label for _, label in selected_labels])
                for row_id, _ in selected_labels:
                    st.session_state.pop(f"home_compare_{row_id}", None)
                st.switch_page(page_lap_comparison_obj)

    render_footer()


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


MAX_COMPARE_LAPS = 8


def _render_lap_picker(key_prefix: str, max_laps: int = MAX_COMPARE_LAPS) -> list[dict]:
    """The "Laps to compare" row picker: add/remove rows, each an
    independent (session, lap) pick, color-matched to its line in whatever
    chart is drawn from the result.

    Shared by the desktop and mobile Data Analysis pages under the same
    `key_prefix` ("da") -- they read and write the exact same
    `session_state` keys, so picking laps on one view and switching to the
    other keeps the same selection instead of starting over. This is the
    literal sense in which the mobile page "keeps the same data": the
    underlying (session, lap, color) rows are identical, only the chart
    rendering differs.
    """
    if f"{key_prefix}_row_ids" not in st.session_state:
        default_rows = _default_data_analysis_rows(all_sessions)
        st.session_state[f"{key_prefix}_row_ids"] = list(range(len(default_rows)))
        st.session_state[f"{key_prefix}_next_row_id"] = len(default_rows)
        for i, (sess_label, lap_no) in enumerate(default_rows):
            st.session_state[f"{key_prefix}_session_{i}"] = sess_label
            st.session_state[f"{key_prefix}_lap_{i}"] = lap_no

    compare_entries = []
    css_rules = []
    with st.expander("Laps to compare", expanded=True):
        for idx, row_id in enumerate(list(st.session_state[f"{key_prefix}_row_ids"])):
            row_color = LAP_COLORS[idx % len(LAP_COLORS)]
            text_color = _readable_text_color(row_color)
            session_key, lap_key = f"{key_prefix}_session_{row_id}", f"{key_prefix}_lap_{row_id}"
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
                if rc3.button("✕", key=f"{key_prefix}_remove_{row_id}", help="Remove this row"):
                    st.session_state[f"{key_prefix}_row_ids"].remove(row_id)
                    st.rerun()
                continue
            _ensure_valid_widget_state(lap_key, row_lap_numbers, row_lap_numbers[0])
            row_lap = rc2.selectbox(
                "Lap", row_lap_numbers, key=lap_key, format_func=lambda n, _t=row_lap_times: _lap_label(n, _t),
                label_visibility=label_visibility,
            )
            if rc3.button("✕", key=f"{key_prefix}_remove_{row_id}", help="Remove this row"):
                st.session_state[f"{key_prefix}_row_ids"].remove(row_id)
                st.rerun()
            compare_entries.append({
                "row_id": row_id, "session_label": row_session_label, "session": row_session, "lap_number": row_lap,
                "lap_time": row_lap_times.get(row_lap), "color": row_color,
                "tag": f"S{row_session.session_id}·L{row_lap}",
            })

        if css_rules:
            st.markdown(f"<style>{''.join(css_rules)}</style>", unsafe_allow_html=True)

        if len(st.session_state[f"{key_prefix}_row_ids"]) >= max_laps:
            st.caption(f"Maximum {max_laps} laps at once.")
        elif st.button("+ Add lap to compare", key=f"{key_prefix}_add_row"):
            new_id = st.session_state[f"{key_prefix}_next_row_id"]
            st.session_state[f"{key_prefix}_row_ids"].append(new_id)
            st.session_state[f"{key_prefix}_next_row_id"] = new_id + 1
            st.rerun()
    return compare_entries


# ---------------------------------------------------------------------------
# Data Analysis page -- design 1a ("channel-stack, pit-wall dark")
#
# Recreates design_handoff_karting_telemetry/README.md's 1a spec inside this
# Streamlit app (route 1 from that README: custom CSS + Plotly, not a
# separate frontend). Token values, spacing and type sizes below are taken
# verbatim from that README's "Design tokens" section -- change the token
# constant if something needs to change, not a one-off inline value. The
# token palette itself (`_DA1A`) and the global theme it drives now live near
# the top of this file (see `_inject_global_theme_css` / `KARTING_DARK_TEMPLATE`)
# since design 1a's look is applied app-wide, not just on this page.
#
# Known, deliberate departures from the literal spec (chrome is expected to
# be approximate per the README; the numbers/charts are exact):
#   - No fake top-bar nav (Analyse/Theoretical/Leaderboards/Garage) -- this
#     app already has a real, working sidebar nav; duplicating it as inert
#     pills would be worse than not having it. The context strip (circuit/
#     class/session/temp/tyre) is recreated since it carries real data.
#   - Scoped to the sidebar's single active session, matching the design's
#     own "Laps · Run 4" lap list (one run, pick two of its laps) -- no
#     cross-session/teammate/theoretical reference picker yet (README lists
#     this as a real interaction the design supports; not built here).
#   - The four "S1-S4" sectors are a fixed equal-distance quartering of the
#     lap, independent of the corner/straight segmentation used elsewhere in
#     this app -- broadcast-style sectors, not this app's corner numbering.
#   - The track map keeps the real hover-synced cursor dot (render_linked_
#     speed_delta's existing, working mechanism) rather than the literal
#     SVG pathLength/dasharray technique the README describes -- the README
#     itself calls the linked cursor "the single most important
#     interaction," so that took priority over the exact map-drawing method.
#   - The 74px trace-stack gutter shows each channel's static name/unit
#     (via the y-axis title) but not a live per-lap cursor readout pinned
#     there -- Streamlit has no cheap way to push a JS hover position back
#     into a Python-rendered sidebar without a rerun per pixel moved (see
#     render_linked_speed_delta's docstring); the crosshair + hover tooltip
#     still surface the same numbers.
# ---------------------------------------------------------------------------

DA1A_N_SECTORS = 4


def _da1a_inject_css() -> None:
    t = _DA1A
    st.markdown(
        f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap');

.st-key-da1a_root {{
    background: {t['canvas']};
    padding: 14px 16px 20px 16px;
    border-radius: 6px;
    font-family: 'Archivo', sans-serif;
    color: {t['ink']};
}}
.st-key-da1a_root .da1a-mono {{
    font-family: 'JetBrains Mono', monospace;
    font-variant-numeric: tabular-nums;
}}
.st-key-da1a_root .da1a-label {{
    font-family: 'Archivo', sans-serif;
    font-weight: 600;
    font-size: 9px;
    letter-spacing: .14em;
    text-transform: uppercase;
    line-height: 1;
    color: {t['ink_faint']};
}}
.st-key-da1a_root hr {{ border-color: {t['hairline']}; margin: 10px 0; }}
.st-key-da1a_root [data-testid="stVerticalBlockBorderWrapper"] {{ border-color: {t['hairline']} !important; }}

/* Context strip */
.da1a-strip {{
    display: flex; align-items: center; gap: 0; background: {t['surface']};
    border: 1px solid {t['hairline']}; border-radius: 5px; padding: 0 2px; margin-bottom: 12px;
    flex-wrap: wrap;
}}
.da1a-strip-cell {{ padding: 8px 18px; border-right: 1px solid {t['hairline']}; }}
.da1a-strip-cell .da1a-label {{ margin-bottom: 3px; }}
.da1a-strip-cell .da1a-value {{ font-weight: 600; font-size: 12px; color: {t['ink']}; }}

/* Lap list rows */
.st-key-da1a_root .stButton button {{
    background: transparent; border: none; text-align: left; padding: 4px 8px;
    font-family: 'JetBrains Mono', monospace; font-size: 11px; color: {t['ink2']};
    width: 100%; border-radius: 3px;
}}
.st-key-da1a_root .stButton button:hover {{ background: {t['neutral_bar']}; color: {t['ink']}; }}
.da1a-sector-bars {{ display: flex; gap: 3px; padding: 0 8px; margin-top: 2px; }}
.da1a-sector-bars > div {{ flex: 1; height: 4px; border-radius: 1px; background: {t['neutral_bar2']}; }}

/* Hero header */
.da1a-hero-time {{
    font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 44px;
    line-height: .92; letter-spacing: -.02em; color: {t['ink']};
}}
.da1a-hero-delta {{ font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 30px; line-height: .92; }}
.da1a-ref-block {{ border-left: 1px solid {t['hairline']}; padding-left: 18px; }}
.da1a-ref-row {{ display: flex; justify-content: space-between; gap: 10px; font-size: 12px; margin-bottom: 3px; }}
.da1a-ref-row .da1a-label {{ min-width: 96px; }}

/* Sector delta mini-chart */
.da1a-sector-chart {{ display: flex; gap: 6px; align-items: flex-end; height: 56px; }}
.da1a-sector-col {{ width: 52px; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }}
.da1a-sector-col-bar {{ width: 100%; border-radius: 2px; }}

/* Coach note card */
.da1a-coach-card {{
    background: {t['surface_raised']}; border: 1px solid {t['hairline']}; border-radius: 5px; padding: 14px;
    font-size: 12px; line-height: 1.5; color: {t['ink2']};
}}

/* Track map legend / sector table */
.da1a-legend-swatch {{ display: inline-block; width: 14px; height: 2px; margin-right: 5px; vertical-align: middle; }}

/* Class standing card */
.da1a-standing-card {{ background: {t['surface_raised']}; border: 1px solid {t['hairline']}; border-radius: 5px; padding: 14px; }}
.da1a-standing-p {{ font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 26px; }}
.da1a-progress-track {{ background: {t['neutral_bar']}; border-radius: 3px; height: 5px; overflow: hidden; margin: 8px 0; }}
.da1a-progress-fill {{ background: {t['accent']}; height: 100%; }}
</style>
""",
        unsafe_allow_html=True,
    )


def _da1a_time_str(seconds: float | None) -> str:
    """`SS.mmm`, 3 decimals -- design 1a's lap-time format (README:
    "Lap times render as SS.mmm")."""
    if seconds is None or (isinstance(seconds, float) and np.isnan(seconds)):
        return "--.---"
    return f"{seconds:.3f}"


def _da1a_delta_str(seconds: float | None, plus_is_loss: bool = True) -> str:
    """Signed delta with a real minus sign (U+2212), 3 decimals -- design
    1a's delta format. `plus_is_loss` controls which sign is colored as a
    loss vs. a gain when the caller also wants a color (see
    `_da1a_delta_color`); the string itself always shows the literal sign.
    """
    if seconds is None or (isinstance(seconds, float) and np.isnan(seconds)):
        return "--"
    sign = "+" if seconds >= 0 else "−"
    return f"{sign}{abs(seconds):.3f}"


def _da1a_delta_color(seconds: float | None, plus_is_loss: bool = True) -> str:
    if seconds is None or (isinstance(seconds, float) and np.isnan(seconds)):
        return _DA1A["ink_muted"]
    is_loss = (seconds > 0) if plus_is_loss else (seconds < 0)
    if seconds == 0:
        return _DA1A["ink_muted"]
    return _DA1A["loss"] if is_loss else _DA1A["gain"]


def _make_sector_segments(total_distance_m: float, n_sectors: int = DA1A_N_SECTORS) -> pd.DataFrame:
    """Fixed equal-length distance sectors (S1..S4) spanning one lap --
    broadcast-timing-style sectors, deliberately independent of this app's
    own corner/straight segmentation (`telemetry.corners.segment_track`).
    Built here (not in `telemetry/`) purely by composing the existing
    `segment_times_for_lap` / `theoretical_best_lap` functions against a
    synthetic segment table shaped like their real ones -- no telemetry
    code needed changing for this.
    """
    if not total_distance_m or total_distance_m <= 0 or n_sectors < 1:
        return pd.DataFrame(columns=["label", "kind", "start_m", "end_m"])
    edges = np.linspace(0, total_distance_m, n_sectors + 1)
    return pd.DataFrame(
        [{"label": f"S{i + 1}", "kind": "sector", "start_m": edges[i], "end_m": edges[i + 1]} for i in range(n_sectors)]
    )


def _da1a_pick_lap_colors(selected_lap: int, reference_lap: int) -> tuple[str, str]:
    return _DA1A["accent"], _DA1A["reference"]


def _da1a_sector_delta_color(delta_s: float | None, best_owner_lap: int | None, lap_no: int) -> str:
    """Sector-bar color per design 1a: purple if `lap_no` owns the
    session-best time for that sector, else green/red by whether it beat
    the comparison reference, else neutral grey."""
    if best_owner_lap is not None and lap_no == best_owner_lap:
        return _DA1A["reference"]
    if delta_s is None or (isinstance(delta_s, float) and np.isnan(delta_s)):
        return _DA1A["neutral_bar2"]
    if delta_s < -1e-6:
        return _DA1A["gain"]
    if delta_s > 1e-6:
        return _DA1A["loss"]
    return _DA1A["neutral_bar2"]


def _da1a_sector_times_table(session: Session, lap_numbers: list[int], sector_segments: pd.DataFrame) -> dict[int, list[float]]:
    """Per-lap sector times (one list of `time_s`, S1..S4 order) for every
    lap in `lap_numbers`, by composing the existing `segment_times_for_lap`
    against the synthetic fixed-sector table -- no telemetry changes."""
    out: dict[int, list[float]] = {}
    for lap_no in lap_numbers:
        times = segment_times_for_lap(session, lap_no, sector_segments)
        out[lap_no] = times.set_index("segment_label")["time_s"].reindex(sector_segments["label"]).tolist()
    return out


def page_data_analysis() -> None:
    """Design 1a ("channel-stack, pit-wall dark") -- see the long comment
    block above this function for the handful of deliberate departures from
    the literal design_handoff_karting_telemetry/README.md spec."""
    if not _require_data():
        return

    _da1a_inject_css()
    root = st.container(key="da1a_root")
    with root:
        # -- lap list source: every lap in the active session (incl. flagged
        # ones -- faded/struck-through, not hidden, per the README's
        # "empty and degraded states" note on incident laps) --------------
        all_lap_numbers = laps["lap_number"].tolist()
        max_lap_no = max(all_lap_numbers) if all_lap_numbers else None

        default_selected = analyzed_lap if analyzed_lap in clean_lap_numbers else best_lap
        default_reference = best_lap
        if default_reference == default_selected and len(clean) >= 2:
            # Picking "the fastest lap that isn't the selected one" rather
            # than sort_values(...)[1]: with an exact lap-time tie,
            # sort_values' default quicksort isn't stable, so the tied lap
            # can land at either position and this can silently pick the
            # same lap as default_selected again.
            remaining = clean[clean["lap_number"] != default_selected]
            if not remaining.empty:
                default_reference = remaining.loc[remaining["lap_time_s"].idxmin(), "lap_number"]

        if st.session_state.get("da1a_context_key") != active_session_key:
            st.session_state["da1a_context_key"] = active_session_key
            st.session_state["da1a_selected_lap"] = default_selected
            st.session_state["da1a_reference_lap"] = default_reference
            st.session_state["da1a_pinned_corners"] = set()

        selected_lap = st.session_state.get("da1a_selected_lap", default_selected)
        reference_lap = st.session_state.get("da1a_reference_lap", default_reference)
        if selected_lap not in all_lap_numbers:
            selected_lap = default_selected
        if reference_lap not in all_lap_numbers or reference_lap == selected_lap:
            reference_lap = default_reference if default_reference != selected_lap else selected_lap
        st.session_state["da1a_selected_lap"] = selected_lap
        st.session_state["da1a_reference_lap"] = reference_lap
        st.session_state.setdefault("da1a_pinned_corners", set())
        st.session_state.setdefault("da1a_axis_basis", "Distance")
        st.session_state.setdefault("da1a_trace_view", "Split channels")
        st.session_state.setdefault("da1a_show_throttle_brake", True)

        sel_color, ref_color = _da1a_pick_lap_colors(selected_lap, reference_lap)

        # -- context strip --------------------------------------------------
        active_db = session_db_lookup.get(active_session_key) or {}
        circuit = active_db.get("track_name") or "Unknown circuit"
        session_dt = " · ".join(x for x in [active_session.start_date, active_session.start_time] if x) or "Unknown"
        air_temp = setup.carburettor.ambient_temp_c if setup else None
        track_temp = setup.track_session.track_temp_c if setup else None
        temp_str = (
            f"{air_temp:.1f}°C / {track_temp:.1f}°C" if air_temp is not None and track_temp is not None
            else (f"{air_temp:.1f}°C air" if air_temp is not None else (f"{track_temp:.1f}°C track" if track_temp is not None else "—"))
        )
        tyre = setup.tyres if setup else None
        tyre_str = tyre.compound if tyre and tyre.compound else "—"
        if tyre and tyre.hot_pressure_front_bar and tyre.hot_pressure_rear_bar:
            tyre_str += f" · {tyre.hot_pressure_front_bar:.2f}/{tyre.hot_pressure_rear_bar:.2f} bar"

        strip_cells = [
            ("Circuit", circuit), ("Class", setup.class_name if setup else "—"),
            ("Session", f"{session_dt} · {len(laps)} laps"), ("Air / Track temp", temp_str), ("Tyre", tyre_str),
        ]
        st.markdown(
            '<div class="da1a-strip">' + "".join(
                f'<div class="da1a-strip-cell"><div class="da1a-label">{label}</div>'
                f'<div class="da1a-value da1a-mono">{value}</div></div>'
                for label, value in strip_cells
            ) + "</div>",
            unsafe_allow_html=True,
        )

        lap_col, center_col, rail_col = st.columns([1, 3.8, 1.3], gap="medium")

        # === LAP LIST (left) ===============================================
        with lap_col:
            st.markdown(f'<div class="da1a-label">Laps · {len(laps)} total</div>', unsafe_allow_html=True)
            sector_total_m = float(segments["end_m"].max()) if not segments.empty else 0.0
            sector_segments = _make_sector_segments(sector_total_m, DA1A_N_SECTORS)
            list_lap_numbers = [selected_lap, reference_lap] + sorted(
                (n for n in all_lap_numbers if n not in (selected_lap, reference_lap)), reverse=True
            )
            sector_times_by_lap: dict[int, list[float]] = {}
            best_owner_by_sector: list[int | None] = [None] * DA1A_N_SECTORS
            if not sector_segments.empty and clean_lap_numbers:
                sector_times_by_lap = _da1a_sector_times_table(active_session, list_lap_numbers, sector_segments)
                _, best_sector_df = theoretical_best_lap(active_session, clean_lap_numbers, sector_segments)
                if not best_sector_df.empty:
                    owner_by_label = dict(zip(best_sector_df["segment_label"], best_sector_df["lap_number"]))
                    best_owner_by_sector = [owner_by_label.get(lbl) for lbl in sector_segments["label"]]

            for lap_no in list_lap_numbers:
                lap_row = laps.loc[laps["lap_number"] == lap_no]
                if lap_row.empty:
                    continue
                lap_row = lap_row.iloc[0]
                lap_time_s = lap_row["lap_time_s"]
                is_selected, is_reference = lap_no == selected_lap, lap_no == reference_lap
                delta_vs_ref = (lap_time_s - lap_time_by_number.get(reference_lap, float("nan"))) if not is_reference else None

                age = (max_lap_no - lap_no) if max_lap_no is not None else 0
                opacity = 1.0 if is_selected or is_reference or age <= 2 else (0.6 if age <= 5 else 0.45)
                bg = _DA1A["row_selected"] if is_selected else ("transparent")
                border = f"inset 2px 0 0 {_DA1A['accent']}" if is_selected else (f"inset 2px 0 0 {_DA1A['reference']}" if is_reference else "none")
                incident = bool(lap_row.get("likely_incident", False))

                row_key = f"da1a_lap_row_{lap_no}"
                with st.container(key=row_key):
                    st.markdown(
                        f"<style>.st-key-{row_key} {{ background:{bg}; box-shadow:{border}; opacity:{opacity}; "
                        f"border-radius:2px; margin-bottom:2px; }}</style>",
                        unsafe_allow_html=True,
                    )
                    bars_html = ""
                    if lap_no in sector_times_by_lap:
                        for i, t in enumerate(sector_times_by_lap[lap_no]):
                            ref_t = sector_times_by_lap.get(reference_lap, [None] * DA1A_N_SECTORS)[i]
                            d = (t - ref_t) if (t is not None and ref_t is not None and not (np.isnan(t) or np.isnan(ref_t))) else None
                            color = _da1a_sector_delta_color(d, best_owner_by_sector[i], lap_no)
                            bars_html += f'<div style="background:{color};"></div>'
                    st.markdown(f'<div class="da1a-sector-bars">{bars_html}</div>', unsafe_allow_html=True)

                    time_txt = _da1a_time_str(lap_time_s)
                    if incident:
                        time_txt = f"~~{time_txt}~~"
                    tag = "ref" if is_reference else (_da1a_delta_str(delta_vs_ref) if delta_vs_ref is not None else "")
                    label = f"{lap_no:>2}  {time_txt}  {tag}"
                    if st.button(label, key=f"da1a_select_{lap_no}", use_container_width=True):
                        if lap_no != selected_lap:
                            st.session_state["da1a_reference_lap"] = selected_lap
                            st.session_state["da1a_selected_lap"] = lap_no
                            st.rerun()

            st.markdown("<hr/>", unsafe_allow_html=True)
            st.markdown(
                f'<div class="da1a-label">Reference</div>'
                f'<div style="font-size:11px; margin-top:4px;"><span class="da1a-legend-swatch" style="background:{_DA1A["reference"]};"></span>'
                f'Session best · Lap {best_lap}</div>'
                f'<div style="font-size:11px; margin-top:3px;"><span class="da1a-legend-swatch" style="background:{_DA1A["theoretical"]};"></span>'
                f'Theoretical best · {_da1a_time_str(theoretical_best_s)}s</div>',
                unsafe_allow_html=True,
            )

        # Data shared by the center column and the right rail.
        selected_trace = add_braking_throttle_estimates(lap_metric_trace(active_session, selected_lap))
        reference_trace = add_braking_throttle_estimates(lap_metric_trace(active_session, reference_lap))
        selected_time_s = lap_time_by_number.get(selected_lap)
        reference_time_s = lap_time_by_number.get(reference_lap)
        delta_vs_best = (selected_time_s - summary["best_lap_s"]) if summary else None

        sector_deltas: list[float | None] = []
        if sector_times_by_lap.get(selected_lap) and sector_times_by_lap.get(reference_lap):
            for t_sel, t_ref in zip(sector_times_by_lap[selected_lap], sector_times_by_lap[reference_lap]):
                sector_deltas.append(t_sel - t_ref if not (np.isnan(t_sel) or np.isnan(t_ref)) else None)

        # Corner-by-corner causal comparison (selected vs reference), reused
        # from the same engine the Lap Comparison page already uses.
        thresholds = calibrate_thresholds_cached(active_session, session_cache_key(active_session), tuple(clean_lap_numbers), segments)
        corner_result = compare_corners_cached(
            active_session, session_cache_key(active_session), selected_lap,
            active_session, session_cache_key(active_session), reference_lap,
            segments, thresholds,
        )
        selected_aggs = segment_aggregates(selected_trace, segments)

        # === CENTER COLUMN ==================================================
        with center_col:
            hero_l, hero_r = st.columns([1, 1])
            with hero_l:
                st.markdown(f'<div class="da1a-label">Lap {selected_lap} · selected</div>', unsafe_allow_html=True)
                st.markdown(f'<div class="da1a-hero-time da1a-mono">{_da1a_time_str(selected_time_s)}</div>', unsafe_allow_html=True)
                if reference_time_s is not None and selected_time_s is not None:
                    lap_vs_lap = selected_time_s - reference_time_s
                    st.markdown(
                        f'<div class="da1a-label" style="margin-top:6px;">vs Lap {reference_lap}</div>'
                        f'<div class="da1a-hero-delta da1a-mono" style="color:{_da1a_delta_color(lap_vs_lap)};">'
                        f'{_da1a_delta_str(lap_vs_lap)}</div>',
                        unsafe_allow_html=True,
                    )
            with hero_r:
                st.markdown(
                    '<div class="da1a-ref-block">'
                    + f'<div class="da1a-ref-row"><span class="da1a-label">Best lap</span>'
                      f'<span class="da1a-mono" style="color:{_DA1A["reference"]};">{_da1a_time_str(summary["best_lap_s"]) if summary else "--.---"}</span></div>'
                    + f'<div class="da1a-ref-row"><span class="da1a-label">Theoretical</span>'
                      f'<span class="da1a-mono" style="color:{_DA1A["theoretical"]};">{_da1a_time_str(theoretical_best_s)}</span></div>'
                    + f'<div class="da1a-ref-row"><span class="da1a-label">Δ vs best</span>'
                      f'<span class="da1a-mono" style="color:{_da1a_delta_color(delta_vs_best)};">{_da1a_delta_str(delta_vs_best)}</span></div>'
                    + "</div>",
                    unsafe_allow_html=True,
                )

            if sector_deltas:
                max_abs = max((abs(d) for d in sector_deltas if d is not None), default=0.0) or 1.0
                cols_html = ""
                for i, d in enumerate(sector_deltas):
                    label = sector_segments.iloc[i]["label"]
                    if d is None:
                        cols_html += f'<div class="da1a-sector-col"><div class="da1a-label">{label}</div></div>'
                        continue
                    pct = min(100, abs(d) / max_abs * 100)
                    color = _da1a_delta_color(d)
                    if d <= 0:  # gaining: bar grows up from the bottom
                        bar = f'<div style="height:{pct}%; align-self:flex-end;" class="da1a-sector-col-bar" ></div>'
                    else:  # losing: bar hangs down from the top
                        bar = f'<div style="height:{pct}%; align-self:flex-start; margin-top:auto;" class="da1a-sector-col-bar"></div>'
                    cols_html += (
                        f'<div class="da1a-sector-col" style="justify-content:{"flex-end" if d <= 0 else "flex-start"};">'
                        f'<div class="da1a-sector-col-bar" style="height:{pct}%; background:{color};"></div></div>'
                        f'<div class="da1a-mono" style="font-size:10px; text-align:center; width:52px;">{_da1a_delta_str(d)}</div>'
                        f'<div class="da1a-label" style="text-align:center; width:52px;">{label}</div>'
                    )
                st.markdown(f'<div class="da1a-sector-chart">{cols_html}</div>', unsafe_allow_html=True)

            st.markdown("<hr/>", unsafe_allow_html=True)

            ctrl_l, ctrl_r1, ctrl_r2 = st.columns([2, 1, 1])
            ctrl_l.markdown(
                f'<span class="da1a-mono" style="color:{sel_color};">■</span> Lap {selected_lap} &nbsp;&nbsp;'
                f'<span class="da1a-mono" style="color:{ref_color};">■</span> Lap {reference_lap}',
                unsafe_allow_html=True,
            )
            axis_basis = ctrl_r1.segmented_control("Axis", ["Distance", "Time"], key="da1a_axis_basis")
            trace_view = ctrl_r2.segmented_control("View", ["Overlay", "Split channels"], key="da1a_trace_view")
            show_throttle_brake = st.checkbox("Show throttle / brake (estimated)", key="da1a_show_throttle_brake")

            use_distance = axis_basis == "Distance"
            if use_distance:
                sel_x, ref_x = selected_trace["lap_distance_m"], reference_trace["lap_distance_m"]
                x_title = "Distance (m)"
            else:
                sel_x = selected_trace["session_time_s"] - selected_trace["session_time_s"].iloc[0] if not selected_trace.empty else selected_trace["session_time_s"]
                ref_x = reference_trace["session_time_s"] - reference_trace["session_time_s"].iloc[0] if not reference_trace.empty else reference_trace["session_time_s"]
                x_title = "Elapsed time (s)"

            channels: list[tuple[str, str, int]] = [("Speed", "km/h", 158)]
            if use_distance:
                channels.append(("Delta t", "s · cumulative", 76))
            if show_throttle_brake:
                channels.append(("Throttle (est.)", "% · 0-100", 56))
                channels.append(("Brake (est.)", "on/off · est.", 46))

            if trace_view == "Overlay":
                fig = go.Figure()

                def _norm(series: pd.Series) -> np.ndarray:
                    arr = series.to_numpy(dtype=float)
                    finite = arr[np.isfinite(arr)]
                    if finite.size == 0:
                        return arr
                    lo, hi = finite.min(), finite.max()
                    return (arr - lo) / (hi - lo) if hi > lo else arr * 0
                fig.add_trace(go.Scatter(x=sel_x, y=_norm(selected_trace["GPS Speed"]), mode="lines", name="Speed (sel)", line=dict(color=sel_color, width=2)))
                fig.add_trace(go.Scatter(x=ref_x, y=_norm(reference_trace["GPS Speed"]), mode="lines", name="Speed (ref)", line=dict(color=ref_color, width=1.6)))
                if use_distance:
                    dt = cross_session_delta_trace(active_session, selected_lap, active_session, reference_lap, n_points=800)
                    fig.add_trace(go.Scatter(x=dt["distance_m"], y=_norm(dt["delta_s"]), mode="lines", name="Delta t", line=dict(color=_DA1A["gain"], width=1.4)))
                row_y_domains = None
                fig.update_layout(height=280, yaxis_title="Normalized 0-1")
                fig.update_xaxes(title_text=x_title)
            else:
                row_index = {name: i + 1 for i, (name, _, _) in enumerate(channels)}
                n_rows = len(channels)
                weights = [w for _, _, w in channels]
                fig = make_subplots(
                    rows=n_rows, cols=1, shared_xaxes=True, vertical_spacing=0.03,
                    row_heights=[w / sum(weights) for w in weights],
                )
                fig.add_trace(
                    go.Scatter(x=ref_x, y=reference_trace["GPS Speed"], mode="lines", name=f"Lap {reference_lap}", line=dict(color=ref_color, width=1.6)),
                    row=row_index["Speed"], col=1,
                )
                fig.add_trace(
                    go.Scatter(x=sel_x, y=selected_trace["GPS Speed"], mode="lines", name=f"Lap {selected_lap}", line=dict(color=sel_color, width=2)),
                    row=row_index["Speed"], col=1,
                )
                if not segments.empty:
                    corners_only = segments.loc[segments["kind"] == "corner"].reset_index(drop=True)
                    for i, corner in corners_only.iterrows():
                        fig.add_vline(
                            x=corner["start_m"], row=row_index["Speed"], col=1,
                            line=dict(color=_DA1A["hairline_strong"], width=1),
                            annotation_text=f"T{i + 1}", annotation_position="top", annotation_font=dict(size=9, color=_DA1A["ink_faint"]),
                        )
                if "Delta t" in row_index:
                    delta_row = row_index["Delta t"]
                    dt = cross_session_delta_trace(active_session, selected_lap, active_session, reference_lap, n_points=800)
                    gain_fill = np.minimum(dt["delta_s"], 0.0)
                    fig.add_trace(
                        go.Scatter(x=dt["distance_m"], y=gain_fill, mode="lines", line=dict(width=0), fill="tozeroy",
                                   fillcolor="rgba(47,208,122,.16)", hoverinfo="skip", showlegend=False),
                        row=delta_row, col=1,
                    )
                    fig.add_trace(
                        go.Scatter(x=dt["distance_m"], y=dt["delta_s"], mode="lines", name="Delta t", line=dict(color=sel_color, width=1.6), showlegend=False,
                                   hovertemplate="%{y:.3f}s<extra></extra>"),
                        row=delta_row, col=1,
                    )
                    fig.add_hline(y=0, row=delta_row, col=1, line=dict(color="rgba(255,255,255,.22)", dash="dash", width=1))
                    d0, d1 = _axis_y_domain(fig, delta_row)
                    fig.add_annotation(text="GAINING", xref="paper", yref="paper", x=0.01, y=d1, showarrow=False,
                                        font=dict(size=9, color=_DA1A["gain"]), xanchor="left", yanchor="top")
                    fig.add_annotation(text="LOSING", xref="paper", yref="paper", x=0.01, y=d0, showarrow=False,
                                        font=dict(size=9, color=_DA1A["loss"]), xanchor="left", yanchor="bottom")
                if "Throttle (est.)" in row_index:
                    t_row = row_index["Throttle (est.)"]
                    fig.add_trace(
                        go.Scatter(x=ref_x, y=reference_trace["power_on_estimate"].astype(int) * 100, mode="lines",
                                   line=dict(color=ref_color, width=1.6, shape="hv"), showlegend=False, hoverinfo="skip"),
                        row=t_row, col=1,
                    )
                    fig.add_trace(
                        go.Scatter(x=sel_x, y=selected_trace["power_on_estimate"].astype(int) * 100, mode="lines",
                                   line=dict(color=sel_color, width=2, shape="hv"), showlegend=False, hoverinfo="skip"),
                        row=t_row, col=1,
                    )
                if "Brake (est.)" in row_index:
                    b_row = row_index["Brake (est.)"]
                    fig.add_trace(
                        go.Scatter(x=ref_x, y=reference_trace["braking_estimate"].astype(int) * 100, mode="lines",
                                   line=dict(color=ref_color, width=1.6, shape="hv"), showlegend=False, hoverinfo="skip"),
                        row=b_row, col=1,
                    )
                    fig.add_trace(
                        go.Scatter(x=sel_x, y=selected_trace["braking_estimate"].astype(int) * 100, mode="lines",
                                   line=dict(color=sel_color, width=2, shape="hv"), showlegend=False, hoverinfo="skip"),
                        row=b_row, col=1,
                    )
                for name, unit, _ in channels:
                    fig.update_yaxes(title_text=f"{name}<br><span style='font-size:9px'>{unit}</span>", title_font=dict(size=10), row=row_index[name], col=1)
                fig.update_xaxes(title_text=x_title, row=n_rows, col=1)
                fig.update_layout(hovermode="closest", showlegend=False)
                fig.update_xaxes(showspikes=False)
                row_y_domains = [_axis_y_domain(fig, r) for r in range(1, n_rows + 1)]

            fig.update_layout(
                paper_bgcolor=_DA1A["surface"], plot_bgcolor=_DA1A["surface"],
                font=dict(family="Archivo, sans-serif", size=11, color=_DA1A["ink2"]),
                margin=dict(l=10, r=10, t=24, b=10),
            )
            fig.update_xaxes(gridcolor=_DA1A["hairline"], zerolinecolor=_DA1A["hairline"])
            fig.update_yaxes(gridcolor=_DA1A["hairline"], zerolinecolor=_DA1A["hairline"])

            map_trace = selected_trace.dropna(subset=["lap_distance_m", "Latitude", "Longitude"]).sort_values("lap_distance_m")
            map_fig = go.Figure()
            if not sector_segments.empty and not map_trace.empty:
                for i, seg in sector_segments.iterrows():
                    seg_pts = map_trace[(map_trace["lap_distance_m"] >= seg["start_m"]) & (map_trace["lap_distance_m"] <= seg["end_m"])]
                    color = _da1a_sector_delta_color(
                        sector_deltas[i] if i < len(sector_deltas) else None, best_owner_by_sector[i] if i < len(best_owner_by_sector) else None, selected_lap,
                    )
                    map_fig.add_trace(go.Scattergl(x=seg_pts["Longitude"], y=seg_pts["Latitude"], mode="lines", line=dict(color=color, width=4), showlegend=False, hoverinfo="skip"))
            else:
                map_fig.add_trace(go.Scattergl(x=map_trace["Longitude"], y=map_trace["Latitude"], mode="lines", line=dict(color=sel_color, width=3), showlegend=False))
            if not map_trace.empty:
                map_fig.add_trace(
                    go.Scatter(x=[map_trace["Longitude"].iloc[0]], y=[map_trace["Latitude"].iloc[0]], mode="markers",
                               marker=dict(size=14, color=_DA1A["accent"], line=dict(width=2, color=_DA1A["surface"])), showlegend=False)
                )
            map_fig.update_layout(
                yaxis=dict(scaleanchor="x", visible=False), xaxis=dict(visible=False), margin=dict(t=4, b=4, l=4, r=4),
                paper_bgcolor=_DA1A["surface_raised"], plot_bgcolor=_DA1A["surface_raised"],
            )

            if use_distance:
                st.caption("Hovering any channel moves the crosshair across every row and the dot on the track map.")
                render_linked_speed_delta(
                    fig, map_fig, map_trace["lap_distance_m"].tolist(), map_trace["Latitude"].tolist(), map_trace["Longitude"].tolist(),
                    height=(280 if trace_view == "Overlay" else sum(w for _, _, w in channels)),
                    map_height=180, chart_row_y_domains=row_y_domains,
                )
            else:
                st.caption("The linked cursor + track map only sync in Distance mode (Time-basis laps don't share a common x-axis).")
                fig.update_layout(height=280 if trace_view == "Overlay" else sum(w for _, _, w in channels))
                st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

            # -- "Where the time went" -----------------------------------------
            st.markdown('<div class="da1a-label" style="margin-top:14px;">Where the time went · '
                        f'lap {selected_lap} vs lap {reference_lap}</div>', unsafe_allow_html=True)
            if corner_result.empty:
                st.caption("No corner-level data could be extracted for this pair of laps (check GPS coverage).")
            else:
                table = corner_result.merge(
                    selected_aggs[["segment_label", "min_speed_kmh"]], left_on="corner_label", right_on="segment_label", how="left"
                )
                table = table.sort_values("net_time_impact_s", key=lambda s: s.abs(), ascending=False).head(8)
                rows_html = ""
                for i, (_, row) in enumerate(table.iterrows()):
                    corner_no = row["corner_label"].replace("Corner ", "")
                    pinned = row["corner_label"] in st.session_state["da1a_pinned_corners"]
                    bg = _DA1A["row_alt"] if i % 2 else "transparent"
                    delta_color = _da1a_delta_color(row["net_time_impact_s"])
                    pin_mark = "📌 " if pinned else ""
                    rows_html += (
                        f'<div style="display:grid; grid-template-columns:30px 1fr 70px 70px 70px; padding:7px 8px; '
                        f'background:{bg}; font-size:11px; align-items:center;">'
                        f'<span class="da1a-mono" style="color:{_DA1A["ink_faint"]};">T{corner_no}</span>'
                        f'<span>{pin_mark}{row["corner_label"]}</span>'
                        f'<span class="da1a-mono" style="text-align:right;">{row["min_speed_kmh"]:.1f}</span>'
                        f'<span class="da1a-mono" style="text-align:right;">{_da1a_delta_str(row["apex_distance_delta_m"])} m</span>'
                        f'<span class="da1a-mono" style="text-align:right; color:{delta_color};">{_da1a_delta_str(row["net_time_impact_s"])}</span>'
                        "</div>"
                    )
                st.markdown(
                    '<div style="display:grid; grid-template-columns:30px 1fr 70px 70px 70px; padding:0 8px 6px 8px;">'
                    + "".join(f'<span class="da1a-label">{h}</span>' for h in ["Trn", "Corner", "Min spd", "Apex", "Δ"])
                    + "</div>" + rows_html,
                    unsafe_allow_html=True,
                )

                worst = rank_headline_findings(corner_result, n=1)
                st.markdown('<div style="height:10px;"></div>', unsafe_allow_html=True)
                if worst:
                    finding = worst[0]
                    st.markdown(
                        f'<div class="da1a-coach-card"><div class="da1a-label" style="margin-bottom:6px;">Coach note</div>'
                        f"{finding['narrative']}</div>",
                        unsafe_allow_html=True,
                    )
                    note_c1, note_c2 = st.columns(2)
                    if note_c1.button("Send to driver", key="da1a_send_note"):
                        st.toast(f"Note for {finding['corner_label']} recorded -- no driver messaging backend is wired up yet, so this just confirms the action.")
                    if note_c2.button("Pin corner", key="da1a_pin_note"):
                        st.session_state["da1a_pinned_corners"].add(finding["corner_label"])
                        st.rerun()
                else:
                    st.success("No significant corner-by-corner difference between these two laps.")

        # === RIGHT RAIL ======================================================
        with rail_col:
            st.markdown(f'<div class="da1a-label">Track map · sector delta</div>', unsafe_allow_html=True)
            st.markdown(
                f'<div style="font-size:10px; margin:4px 0 8px 0;">'
                f'<span class="da1a-legend-swatch" style="background:{_DA1A["gain"]};"></span>faster&nbsp;&nbsp;'
                f'<span class="da1a-legend-swatch" style="background:{_DA1A["loss"]};"></span>slower&nbsp;&nbsp;'
                f'<span class="da1a-legend-swatch" style="background:{_DA1A["reference"]};"></span>personal-best sector</div>',
                unsafe_allow_html=True,
            )
            if not use_distance:
                # The map above is embedded in the same hover-linked component
                # as the chart only in Distance mode -- render a static copy
                # here too so the right rail still shows it in Time mode.
                st.plotly_chart(map_fig, width="stretch", config={"displayModeBar": False})

            if not sector_segments.empty and sector_times_by_lap:
                st.markdown('<div class="da1a-label" style="margin-top:10px;">Sector table</div>', unsafe_allow_html=True)
                sec_rows = ""
                for i, seg in sector_segments.iterrows():
                    owner = best_owner_by_sector[i]
                    owner_time = sector_times_by_lap.get(owner, [None] * DA1A_N_SECTORS)[i] if owner is not None else None
                    d = sector_deltas[i] if i < len(sector_deltas) else None
                    sec_rows += (
                        '<div style="display:grid; grid-template-columns:28px 1fr 54px 54px; padding:5px 4px; font-size:11px;">'
                        f'<span class="da1a-mono">{seg["label"]}</span>'
                        f'<span>{("Lap " + str(owner)) if owner is not None else "—"}</span>'
                        f'<span class="da1a-mono" style="text-align:right;">{_da1a_time_str(owner_time) if owner_time is not None else "—"}</span>'
                        f'<span class="da1a-mono" style="text-align:right; color:{_da1a_delta_color(d)};">{_da1a_delta_str(d)}</span>'
                        "</div>"
                    )
                st.markdown(sec_rows, unsafe_allow_html=True)

            st.markdown('<div class="da1a-label" style="margin-top:14px;">Class standing</div>', unsafe_allow_html=True)
            rankings = accounts_lib.driver_rankings(int(current_profile["id"])) if active_db.get("track_name") else pd.DataFrame()
            mine = rankings[rankings["track_name"] == active_db.get("track_name")] if not rankings.empty else pd.DataFrame()
            if mine.empty:
                st.markdown(
                    '<div class="da1a-standing-card" style="font-size:11px; color:'
                    f'{_DA1A["ink_muted"]};">Not enough shared session data at this circuit yet to show a class '
                    "standing -- share sessions from the My Sessions page to appear on this board.</div>",
                    unsafe_allow_html=True,
                )
            else:
                row = mine.iloc[0]
                pct = max(0.0, min(100.0, 100.0 * (1 - (int(row["rank"]) - 1) / max(1, int(row["field_size"]) - 1))))
                st.markdown(
                    f'<div class="da1a-standing-card"><div class="da1a-standing-p da1a-mono">P{int(row["rank"])}</div>'
                    f'<div style="font-size:11px; color:{_DA1A["ink_muted"]};">of {int(row["field_size"])} at this circuit</div>'
                    f'<div class="da1a-progress-track"><div class="da1a-progress-fill" style="width:{pct:.0f}%;"></div></div>'
                    f'<div style="font-size:11px;">Best lap: <span class="da1a-mono">{_da1a_time_str(row["best_lap_s"])}</span></div>'
                    "</div>",
                    unsafe_allow_html=True,
                )
                if st.button("Compare with next-fastest lap", key="da1a_compare_next", use_container_width=True):
                    st.switch_page(page_leaderboards_obj)

    render_footer()


DATA_ANALYSIS_CHART_SHORT_LABELS = {
    "speed": "Speed", "rpm": "RPM", "lat_g": "Lat G", "lon_g": "Lon G", "delta": "Delta",
}


def _mobile_track_map_figure(
    primary_trace: pd.DataFrame, corner_midpoints: pd.DataFrame, line_color: str,
    marker_size: int = 16, label_font_size: int = 10,
) -> go.Figure:
    """Track outline + corner labels + a movable position marker (last
    trace), axes hidden -- lat/lon numbers tell a driver nothing, this is
    purely "where on the track is this," which the outline shape and corner
    numbers answer on their own. Shared by the compact inline map and the
    bigger expanded-dialog one, just at different sizes.
    """
    fig = go.Figure()
    fig.add_trace(
        go.Scattergl(
            x=primary_trace["Longitude"], y=primary_trace["Latitude"], mode="lines",
            line=dict(color=line_color, width=2), hoverinfo="skip", showlegend=False,
        )
    )
    if not corner_midpoints.empty:
        labels = corner_midpoints["segment_label"].str.replace("Corner ", "C", regex=False)
        fig.add_trace(
            go.Scatter(
                x=corner_midpoints["mid_lon"], y=corner_midpoints["mid_lat"], mode="markers+text",
                text=labels, textposition="top center", textfont=dict(size=label_font_size, color="#666"),
                marker=dict(size=6, color="#999"), hoverinfo="skip", showlegend=False,
            )
        )
    if not primary_trace.empty:
        fig.add_trace(
            go.Scatter(
                x=[primary_trace["Longitude"].iloc[0]], y=[primary_trace["Latitude"].iloc[0]],
                mode="markers", marker=dict(size=marker_size, color="red", line=dict(width=2, color="white")),
                showlegend=False,
            )
        )
    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0), showlegend=False,
        xaxis=dict(visible=False), yaxis=dict(visible=False, scaleanchor="x"),
    )
    return fig


@st.dialog("Track map", width="large")
def _show_expanded_map_dialog(primary_trace: pd.DataFrame, corner_midpoints: pd.DataFrame, tag: str, color: str) -> None:
    st.caption(f"{tag} -- full track outline with corner numbers, for orientation only (no live marker in here).")
    big_fig = _mobile_track_map_figure(primary_trace, corner_midpoints, color, marker_size=0, label_font_size=13)
    big_fig.update_layout(height=520)
    st.plotly_chart(big_fig, width="stretch", config={"displayModeBar": False})


def page_data_analysis_mobile() -> None:
    if not _require_data():
        return
    st.subheader("Data analysis (mobile)")
    st.caption(
        "The same laps as Lap Analysis, redrawn one chart at a time for a small screen. Drag the chart to scroll "
        "across the lap, pinch (or scroll) to zoom into any part of it in detail, and tap anywhere on it to move "
        "the map marker to that point."
    )

    compare_entries = _render_lap_picker("da", MAX_COMPARE_LAPS)
    if not compare_entries:
        st.info("Add at least one lap to compare.")
        render_footer()
        return

    fastest_entry = min(compare_entries, key=lambda e: e["lap_time"] if e["lap_time"] is not None else float("inf"))

    metric_key = st.segmented_control(
        "Chart", DATA_ANALYSIS_CHART_KEYS, default="speed", key="da_mobile_metric",
        format_func=lambda k: DATA_ANALYSIS_CHART_SHORT_LABELS[k],
    ) or "speed"

    lap_traces: dict[int, pd.DataFrame] = {}
    fig = go.Figure()
    channel_by_key = {
        "speed": "GPS Speed", "rpm": "RPM", "lat_g": "GPS Lateral Acceleration", "lon_g": "GPS Longitudinal Acceleration",
    }
    unit_by_key = {"speed": " km/h", "rpm": " RPM", "lat_g": "g", "lon_g": "g"}
    fmt_by_key = {"speed": ".1f", "rpm": ".0f", "lat_g": ".2f", "lon_g": ".2f"}

    for entry in compare_entries:
        trace = lap_metric_trace(entry["session"], entry["lap_number"])
        lap_traces[entry["row_id"]] = trace
        color, tag = entry["color"], entry["tag"]
        if metric_key in channel_by_key:
            fig.add_trace(
                go.Scatter(
                    x=trace["lap_distance_m"], y=trace[channel_by_key[metric_key]], mode="lines", name=tag,
                    line=dict(color=color), hovertemplate=f"{tag}: %{{y:{fmt_by_key[metric_key]}}}{unit_by_key[metric_key]}<extra></extra>",
                )
            )
        elif metric_key == "delta" and entry is not fastest_entry:
            dt = cross_session_delta_trace(
                entry["session"], entry["lap_number"], fastest_entry["session"], fastest_entry["lap_number"], n_points=800,
            )
            fig.add_trace(
                go.Scatter(
                    x=dt["distance_m"], y=dt["delta_s"], mode="lines", name=tag, line=dict(color=color),
                    hovertemplate=f"{tag}: %{{y:.4f}}s<extra></extra>",
                )
            )

    if metric_key == "delta":
        fig.add_hline(y=0, line_dash="dash", line_color="gray")
        if len(compare_entries) == 1:
            st.info("Delta needs at least one more lap to compare against -- add another row above.")
    if metric_key == "rpm":
        fig.add_hrect(y0=setup.peak_power_rpm_low, y1=setup.peak_power_rpm_high, fillcolor="green", opacity=0.1, line_width=0)

    fig.update_layout(
        xaxis_title="Distance (m)", yaxis_title=DATA_ANALYSIS_CHART_LABELS[metric_key],
        margin=dict(l=48, r=12, t=8, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5, font=dict(size=11)),
        hovermode="closest", font=dict(size=12),
    )
    fig.update_xaxes(showspikes=False)

    if metric_key == "rpm":
        band_bits = []
        for entry in compare_entries:
            band = time_in_rpm_band(entry["session"], entry["lap_number"], (setup.peak_power_rpm_low, setup.peak_power_rpm_high))
            pct = f"{band['fraction_in_band'] * 100:.0f}%" if band.get("lap_duration_s", 0) > 0 else "n/a"
            band_bits.append(f"{entry['tag']}: {pct}")
        st.caption(
            f"Shaded = the {setup.peak_power_rpm_low}-{setup.peak_power_rpm_high} RPM power band. "
            f"% of lap spent in it -- {' · '.join(band_bits)}"
        )

    # The map always shows the fastest pick's own lap -- from that lap's
    # OWN session, which isn't necessarily the sidebar's active session
    # since compare rows can pull from any loaded session.
    primary_trace = lap_traces[fastest_entry["row_id"]].dropna(subset=["lap_distance_m", "Latitude", "Longitude"]).sort_values("lap_distance_m")
    primary_session = fastest_entry["session"]
    primary_clean_numbers, primary_times = _session_clean_laps(primary_session)
    primary_best_lap = min(primary_clean_numbers, key=lambda n: primary_times[n]) if primary_clean_numbers else fastest_entry["lap_number"]
    _primary_segments, primary_midpoints = build_segments_and_midpoints_cached(
        primary_session, session_cache_key(primary_session), primary_best_lap
    )
    corner_midpoints = (
        primary_midpoints[primary_midpoints["segment_kind"] == "corner"] if not primary_midpoints.empty else primary_midpoints
    )

    map_col, expand_col = st.columns([5, 1])
    map_col.caption(
        f"Map tracks {fastest_entry['tag']} ({fastest_entry['lap_time']:.2f}s) -- tap the chart to move the marker."
    )
    if expand_col.button("🔍 Expand", key="da_mobile_expand_map"):
        _show_expanded_map_dialog(primary_trace, corner_midpoints, fastest_entry["tag"], fastest_entry["color"])

    map_fig = _mobile_track_map_figure(primary_trace, corner_midpoints, fastest_entry["color"])
    render_mobile_linked_chart(
        fig, map_fig,
        primary_trace["lap_distance_m"].tolist(), primary_trace["Latitude"].tolist(), primary_trace["Longitude"].tolist(),
        chart_height=420, map_height=170,
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
        "For visual context -- the same linked chart/map view as the Lap Analysis page, scoped to your selected "
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

    stats = accounts_lib.community_stats()
    if stats["shared_sessions"]:
        st.caption(
            f"🤝 These will be shared by default, joining {stats['shared_sessions']} session(s) from "
            f"{stats['drivers']} driver(s) across {stats['tracks']} track(s) that everyone here can learn from. "
            "You can switch any session back to private later from 'My Sessions & Sharing'."
        )
    else:
        st.caption(
            "🤝 These will be shared by default, so other drivers can compare against your laps and you'll "
            "appear on the track leaderboard. You can switch any session back to private later from "
            "'My Sessions & Sharing'."
        )
    keep_private = st.checkbox(
        "Keep this upload private for now", key="attr_keep_private",
        help="Nothing here reaches a leaderboard or another driver. You can share individual sessions later.",
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
                kart_class=setup.class_name if setup else None,
                visibility=VISIBILITY_PRIVATE if keep_private else VISIBILITY_DEFAULT,
                **conditions,
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
        "Everything filed under your driver profile. Sessions are shared by default -- a shared session is "
        "selectable as a comparison reference by other drivers and eligible for that track's leaderboard. "
        "'Team' is a narrower middle ground, if you're on one: visible to your teammates only, off every "
        "public leaderboard and the shared-laps browser. Change any of them below; it takes effect immediately."
    )

    contribution = accounts_lib.driver_contribution(int(current_profile["id"]))
    stats = accounts_lib.community_stats()
    mc1, mc2, mc3 = st.columns(3)
    mc1.metric("Your shared sessions", contribution["shared"])
    mc2.metric("Kept private", contribution["private"])
    mc3.metric("Available to compare against", contribution["available_from_others"])

    rankings = accounts_lib.driver_rankings(int(current_profile["id"]))
    if not rankings.empty:
        placings = ", ".join(
            f"P{int(row['rank'])} of {int(row['field_size'])} at {row['track_name']}"
            for _, row in rankings.iterrows()
        )
        st.caption(f"🏆 You're currently {placings}.")
    elif contribution["shared"]:
        st.caption("Your shared sessions are in the pool -- they'll show on a leaderboard once a track has a board.")
    elif stats["shared_sessions"]:
        st.caption(
            f"Nothing of yours is shared right now. {stats['drivers']} other driver(s) have shared "
            f"{stats['shared_sessions']} session(s) you can still compare against."
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

    # 'Team' is only offered as an option when there's an actual team to
    # share with -- picking it with no team would be a silent no-op
    # (identical to private, since no one else could ever match the
    # team-visibility join), which is more confusing than just not
    # offering it.
    on_a_team = accounts_lib.get_active_membership_for_profile(int(current_profile["id"])) is not None
    visibility_options = list(VISIBILITY_CHOICES) if on_a_team else [VISIBILITY_PRIVATE, VISIBILITY_SHARED]
    visibility_labels = {VISIBILITY_PRIVATE: "Private", VISIBILITY_TEAM: "Team", VISIBILITY_SHARED: "Shared"}

    for _, row in owned.iterrows():
        with st.container(border=True):
            info_col, select_col = st.columns([3, 2])
            info_col.write(
                f"**{row['track_name'] or 'Unknown track'}** — {row['start_date'] or '?'} {row['start_time'] or ''}"
            )
            info_col.caption(
                f"{int(row['n_laps'] or 0)} laps · best {row['best_lap_s']:.2f}s"
                if pd.notna(row["best_lap_s"]) else f"{int(row['n_laps'] or 0)} laps"
            )
            current_visibility = row["visibility"] if row["visibility"] in visibility_options else VISIBILITY_PRIVATE
            new_visibility = select_col.selectbox(
                "Visibility", visibility_options, index=visibility_options.index(current_visibility),
                format_func=lambda v: visibility_labels[v], key=f"share_{row['id']}", label_visibility="collapsed",
            )
            if new_visibility != row["visibility"]:
                accounts_lib.set_session_visibility(int(row["id"]), new_visibility)
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
        "Sessions other drivers are sharing. Selecting one sets it as the reference lap on the Lap Comparison "
        "page, so you can run the full corner-by-corner breakdown against their lap."
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
        stats = accounts_lib.community_stats()
        if stats["shared_sessions"]:
            st.info("No shared sessions match those filters yet -- try widening them.")
        else:
            st.info(
                "Nobody has shared a session yet. Sessions are shared by default, so as drivers upload, "
                "their laps will show up here to compare against."
            )
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


def page_team() -> None:
    """Team hub: not on a team yet (browse/request-to-join, or create one
    and become its manager), a pending request, or an active member's
    roster + manager/admin tools + a per-track team comparison table.

    A team is a second, narrower sharing circle alongside the existing
    public shared/private toggle (see VISIBILITY_TEAM in accounts.py) --
    joining one makes a driver's 'team'/'shared' sessions visible to
    everyone else active on it, which is exactly why joining isn't instant:
    it needs the driver's own explicit confirmation *and* a manager/admin's
    acceptance, the same two-sided care the cross-account attribution flow
    on the Settings page already takes.
    """
    profile_id = int(current_profile["id"])
    membership = accounts_lib.get_membership_for_profile(profile_id)

    if membership is None or membership["status"] not in (TEAM_MEMBERSHIP_PENDING, TEAM_MEMBERSHIP_ACTIVE):
        _render_no_team_state(profile_id)
        return

    team = accounts_lib.get_team(int(membership["team_id"]))
    if team is None:
        _render_no_team_state(profile_id)
        return

    if membership["status"] == TEAM_MEMBERSHIP_PENDING:
        st.subheader(f"👥 {team['name']}")
        st.info("Your request to join is waiting for a manager or admin to accept it.")
        if st.button("Withdraw request"):
            accounts_lib.resolve_join_request(int(membership["id"]), accept=False, decided_by_user_id=current_user["id"])
            st.rerun()
        render_footer()
        return

    role = membership["role"]
    st.subheader(f"👥 {team['name']}")
    st.caption(f"You're this team's **{role}**.")

    roster = accounts_lib.team_roster(int(team["id"]))
    st.markdown("**Roster**")
    st.dataframe(
        prettify_columns(roster[["driver_display_name", "role"]]).rename(columns={"Driver": "Member"}),
        width="stretch", hide_index=True,
    )

    if role in (TEAM_ROLE_MANAGER, TEAM_ROLE_ADMIN):
        _render_team_management(team, roster, role, profile_id)

    st.divider()
    st.markdown("**Compare drivers on your team**")
    st.caption(
        "Best lap per driver per track, from sessions team members have set to 'Team' or 'Shared' visibility "
        "(see 'My Sessions & Sharing'). Team members' sessions are also already selectable in every comparison "
        "page's own session picker -- this table is just a quick per-track summary."
    )
    best_times = accounts_lib.team_track_best_times(int(team["id"]))
    if best_times.empty:
        st.info("No team-visible sessions yet -- once a member sets a session to 'Team' or 'Shared', it'll show up here.")
    else:
        track_options = sorted(best_times["track_name"].dropna().unique())
        track_pick = st.selectbox("Track", track_options, key="team_track_pick")
        subset = best_times[best_times["track_name"] == track_pick].sort_values("best_lap_s").reset_index(drop=True)
        display = subset[["driver_display_name", "best_lap_s", "qualifying_sessions"]].copy()
        display["best_lap_s"] = display["best_lap_s"].round(3)
        st.dataframe(prettify_columns(display), width="stretch", hide_index=True)

        picked = st.selectbox(
            "Use as comparison reference", subset["session_db_id"],
            format_func=lambda i, _s=subset: _s.set_index("session_db_id").loc[i, "driver_display_name"],
            key="team_compare_pick",
        )
        if st.button("Set as reference lap", key="team_compare_set"):
            st.session_state["lc_reference_session_db_id"] = int(picked)
            st.success("Set. Open the Lap Comparison page to compare against it.")

    if role == TEAM_ROLE_MEMBER:
        st.divider()
        if st.button("Leave team"):
            accounts_lib.leave_team(profile_id)
            st.rerun()
    render_footer()


def _render_no_team_state(profile_id: int) -> None:
    st.subheader("👥 Team")
    st.caption(
        "Teams are a second, narrower way to share telemetry -- set a session to 'Team' visibility (from "
        "'My Sessions & Sharing') and only your teammates see it, not the public leaderboard or shared-laps "
        "browser. Joining needs both your own confirmation and a manager or admin's acceptance."
    )
    tab_join, tab_create = st.tabs(["Join a team", "Create a team"])

    with tab_join:
        query = st.text_input("Search teams by name", key="team_search")
        teams = accounts_lib.list_teams(name_query=query.strip() or None)
        if teams.empty:
            st.caption("No teams match that search." if query.strip() else "No teams exist yet -- create one instead.")
        else:
            for _, row in teams.iterrows():
                with st.container(border=True):
                    st.write(f"**{row['name']}** — {int(row['member_count'])} member(s)")
                    confirm_key = f"team_confirm_{row['id']}"
                    confirmed = st.checkbox(
                        "I understand that once accepted, any session I mark 'Team' or 'Shared' visibility "
                        "becomes visible to every other active member of this team.",
                        key=confirm_key,
                    )
                    if st.button("Request to join", key=f"team_join_{row['id']}", disabled=not confirmed, type="primary"):
                        try:
                            accounts_lib.request_to_join_team(int(row["id"]), profile_id)
                        except ValueError as exc:
                            st.error(str(exc))
                        else:
                            st.success("Request sent -- waiting for a manager or admin to accept it.")
                            st.rerun()

    with tab_create:
        st.caption("You'll become this team's manager immediately -- there's no one else yet to ask.")
        name = st.text_input("Team name", key="team_create_name")
        if st.button("Create team", type="primary", disabled=not name.strip()):
            try:
                accounts_lib.create_team(name.strip(), current_user["id"])
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(f"Created '{name.strip()}'.")
                st.rerun()
    render_footer()


def _render_team_management(team: dict, roster: pd.DataFrame, role: str, profile_id: int) -> None:
    """Manager/admin tools: accept/reject pending join requests, and manage
    other members. Promoting/demoting/removing an admin and transferring
    the manager role are manager-only -- an admin can still remove a plain
    member and decide join requests, but can't act on a fellow admin, to
    keep "N admins" from being able to unilaterally out-vote each other."""
    st.divider()
    st.markdown("**Manage team**")

    pending = accounts_lib.pending_join_requests_for_team(int(team["id"]))
    if not pending.empty:
        st.markdown("*Pending join requests*")
        for _, req in pending.iterrows():
            with st.container(border=True):
                st.write(f"**{req['driver_display_name']}**")
                accept_col, reject_col, _ = st.columns([1, 1, 4])
                if accept_col.button("Accept", key=f"team_accept_{req['id']}", type="primary"):
                    accounts_lib.resolve_join_request(int(req["id"]), accept=True, decided_by_user_id=current_user["id"])
                    st.rerun()
                if reject_col.button("Reject", key=f"team_reject_{req['id']}"):
                    accounts_lib.resolve_join_request(int(req["id"]), accept=False, decided_by_user_id=current_user["id"])
                    st.rerun()

    others = roster[roster["driver_profile_id"] != profile_id]
    if others.empty:
        return
    st.markdown("*Members*")
    for _, member in others.iterrows():
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
            c1.write(f"{member['driver_display_name']} — {member['role']}")
            member_role = member["role"]
            if role == TEAM_ROLE_MANAGER:
                if member_role == TEAM_ROLE_MEMBER and c2.button("Make admin", key=f"team_promote_{member['id']}"):
                    accounts_lib.set_member_role(int(member["id"]), TEAM_ROLE_ADMIN)
                    st.rerun()
                if member_role == TEAM_ROLE_ADMIN and c2.button("Make member", key=f"team_demote_{member['id']}"):
                    accounts_lib.set_member_role(int(member["id"]), TEAM_ROLE_MEMBER)
                    st.rerun()
                if c4.button("Transfer manager here", key=f"team_transfer_{member['id']}"):
                    accounts_lib.transfer_team_manager(int(team["id"]), int(member["id"]))
                    st.rerun()
            can_remove = member_role == TEAM_ROLE_MEMBER or role == TEAM_ROLE_MANAGER
            if can_remove and c3.button("Remove", key=f"team_remove_{member['id']}"):
                accounts_lib.remove_team_member(int(member["id"]), current_user["id"])
                st.rerun()


def page_leaderboards() -> None:
    st.subheader("Leaderboards")

    stats = accounts_lib.community_stats()
    if stats["shared_sessions"]:
        sc1, sc2, sc3 = st.columns(3)
        sc1.metric("Drivers", stats["drivers"])
        sc2.metric("Shared sessions", stats["shared_sessions"])
        sc3.metric("Tracks", stats["tracks"])

    tab_individual, tab_teams = st.tabs(["Individual", "Teams"])

    with tab_individual:
        st.caption(
            "Best lap per driver at each track, from sessions drivers are sharing. Sessions anyone has switched "
            "to private (or team-only) never appear here, and neither does data belonging to a profile nobody "
            "has claimed yet."
        )
        tracks = accounts_lib.leaderboard_tracks()
        if not tracks:
            st.info(
                "No shared sessions yet, so there's nothing to rank. Upload a session -- they're shared by "
                "default -- and this is where you'll see how you stack up."
            )
        else:
            fc1, fc2, fc3 = st.columns(3)
            track = fc1.selectbox("Track", tracks, key="lb_track")
            condition = fc2.selectbox("Conditions", ["Overall"] + CONDITION_OPTIONS, key="lb_conditions")
            classes = sorted(
                {c for c in accounts_lib.shareable_reference_sessions(track_name=track)["kart_class"].dropna().unique()}
            )
            kart_class = fc3.selectbox("Class", ["All classes"] + classes, key="lb_class")

            board = accounts_lib.leaderboard(
                track,
                track_condition=None if condition == "Overall" else condition,
                kart_class=None if kart_class == "All classes" else kart_class,
            )
            if board.empty:
                st.info("Nothing on this board with those filters yet.")
            else:
                display = board[["rank", "driver_display_name", "team_name", "best_lap_s", "qualifying_sessions"]].copy()
                display["team_name"] = display["team_name"].fillna("—")
                display["best_lap_s"] = display["best_lap_s"].round(3)
                st.dataframe(prettify_columns(display), width="stretch", hide_index=True)
                if condition == "Overall":
                    st.caption("'Overall' pools every condition and ranks on time alone -- wet and dry laps compete directly.")

    with tab_teams:
        st.caption(
            "Each team's fastest member at each track. Unlike the individual board, a session only needs to be "
            "'Team' or 'Shared' visibility to count here -- not necessarily on the public board too."
        )
        team_tracks = accounts_lib.team_leaderboard_tracks()
        if not team_tracks:
            st.info("No team-visible sessions yet -- see the Team page to create or join a team.")
            render_footer()
            return

        tfc1, tfc2, tfc3 = st.columns(3)
        t_track = tfc1.selectbox("Track", team_tracks, key="team_lb_track")
        t_condition = tfc2.selectbox("Conditions", ["Overall"] + CONDITION_OPTIONS, key="team_lb_conditions")
        t_classes = sorted(
            {c for c in accounts_lib.shareable_reference_sessions(track_name=t_track)["kart_class"].dropna().unique()}
        )
        t_kart_class = tfc3.selectbox("Class", ["All classes"] + t_classes, key="team_lb_class")

        team_board = accounts_lib.team_leaderboard(
            t_track,
            track_condition=None if t_condition == "Overall" else t_condition,
            kart_class=None if t_kart_class == "All classes" else t_kart_class,
        )
        if team_board.empty:
            st.info("Nothing on this board with those filters yet.")
        else:
            t_display = team_board[["rank", "team_name", "fastest_driver_name", "best_lap_s", "qualifying_sessions"]].copy()
            t_display["best_lap_s"] = t_display["best_lap_s"].round(3)
            st.dataframe(prettify_columns(t_display), width="stretch", hide_index=True)

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
        "Right now this is private -- nobody else can see it and it isn't on any leaderboard, because it "
        "hasn't been confirmed as yours. If it isn't yours, you don't need to do anything, and you can ask "
        "for it to be deleted instead."
    )
    st.info(
        "Once you claim it, sessions are shared by default: your lap times would appear on that track's "
        "leaderboard under your driver name, and other drivers could compare their laps against yours. "
        "You can switch any session back to private with one toggle, at any time."
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
        "Sign in to analyze your telemetry. Uploaded sessions are shared with other drivers by default, so "
        "everyone has laps to compare against -- you can switch any session to private with one toggle."
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
            "timing data from their kart's logger -- lap times, speed and GPS traces of the track."
        )
        st.write(
            "**What other people can see:** uploaded sessions are shared by default, which means their lap "
            "times appear on that track's leaderboard under their driver name, and other drivers can compare "
            "their own laps against them. No contact details are ever shown. Either of you can switch any "
            "session back to private at any time from 'My sessions & sharing', and it comes off those "
            "leaderboards immediately."
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

page_home_obj = st.Page(page_home, title="Home", icon="🏠", default=True)
page_overview_obj = st.Page(page_overview, title="Top 3 Focus Areas", icon="🎯")
page_my_sessions_obj = st.Page(page_my_sessions, title="My Sessions & Sharing", icon="🔒")
page_shared_laps_obj = st.Page(page_shared_laps, title="Shared Laps", icon="🤝")
page_team_obj = st.Page(page_team, title="Team", icon="👥")
page_leaderboards_obj = st.Page(page_leaderboards, title="Leaderboards", icon="🏆")
page_find_profile_obj = st.Page(page_find_profile, title="Find My Profile", icon="🔍")
page_lap_times_obj = st.Page(page_lap_times, title="Lap Times", icon="⏱️")
page_data_analysis_obj = st.Page(page_data_analysis, title="Lap Analysis", icon="📈")
page_data_analysis_mobile_obj = st.Page(page_data_analysis_mobile, title="Data Analysis (Mobile)", icon="📱")
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

# Single top-level "Home" link (an empty-string section is displayed
# inline, before any collapsible ones -- see st.navigation's docstring)
# plus every other page folded into one "Debug" dropdown for now, per
# request -- these existing pages aren't being redesigned yet, just
# de-emphasized until they are.
nav = st.navigation(
    {
        "": [page_home_obj],
        "Debug": [
            page_overview_obj, page_lap_times_obj, page_data_analysis_obj, page_data_analysis_mobile_obj, page_track_map_obj,
            page_braking_rpm_obj, page_corner_comparison_obj, page_lap_comparison_obj, page_recurring_patterns_obj,
            page_gearing_simulation_obj, page_consistency_obj, page_progression_obj, page_kart_setup_obj, page_history_obj,
            page_shared_laps_obj, page_team_obj, page_leaderboards_obj,
            page_my_sessions_obj, page_find_profile_obj, page_settings_obj,
        ],
    },
    position="top",
)

# ---------------------------------------------------------------------------
# Top bar (replaces the old sidebar entirely): brand mark + account info on
# every page, plus the global session/lap pickers other (still-sidebar-era)
# pages read from -- skipped on Home, which does its own session picking via
# its session list, so nothing here would apply to it anyway.
# ---------------------------------------------------------------------------
with st.container(key="app_top_bar"):
    tb1, tb2, tb3 = st.columns([2, 5, 2])
    tb1.markdown(
        f"""
<div style="display:flex; align-items:center; gap:8px; padding:6px 0;">
  <div style="width:13px; height:15px; background:{_DA1A['accent']}; transform:skewX(-14deg); flex-shrink:0;"></div>
  <div style="font-family:'Archivo', sans-serif; font-weight:700; font-size:14px; letter-spacing:.14em;
              text-transform:uppercase; color:{_DA1A['ink']};">Karting Telemetry</div>
</div>
""",
        unsafe_allow_html=True,
    )
    _active_team_membership = accounts_lib.get_active_membership_for_profile(int(current_profile["id"]))
    _active_team = None
    if _active_team_membership is not None:
        _active_team = accounts_lib.get_team(int(_active_team_membership["team_id"]))
    signed_in_line = f"Signed in as **{current_profile['display_name']}**"
    if _active_team is not None:
        signed_in_line += f" · Team: **{_active_team['name']}** ({_active_team_membership['role']})"
    tb2.markdown(f'<div style="padding-top:10px;">{signed_in_line}</div>', unsafe_allow_html=True)
    if tb3.button("Sign out", key="topbar_sign_out", use_container_width=True):
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

# The session/lap pickers below aren't needed on Home -- it does its own
# per-row session picking -- so they're skipped there entirely rather than
# cluttering the one page that doesn't use them. Every other ("Debug")
# page still reads the same active_session/analyzed_lap globals these set,
# same as when they lived in the sidebar.
show_session_controls = nav is not page_home_obj

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

    # Keyed (not left implicit) so the Home page's "Open"/"Kart setup"
    # buttons can jump straight to a specific session. They can't write
    # `active_session_select` directly though -- by the time a button
    # inside a page's own function runs (via nav.run(), which happens
    # after this top-bar code in the same script run), this widget has
    # already been instantiated this run, and Streamlit forbids writing to
    # an already-instantiated widget's key. So they stash the target label
    # in the plain `_pending_active_session` key instead, consumed here --
    # strictly before this widget is instantiated -- on the rerun that
    # follows the click.
    if "_pending_active_session" in st.session_state:
        pending = st.session_state.pop("_pending_active_session")
        if pending in session_labels:
            st.session_state["active_session_select"] = pending
    if st.session_state.get("active_session_select") not in session_labels:
        st.session_state["active_session_select"] = default_session_label if default_session_label in session_labels else session_labels[0]

    if show_session_controls:
        with st.container(key="app_session_bar"):
            sb1, sb2, sb3 = st.columns([3, 3, 1.5])
            active_label = sb1.selectbox("Session to analyze", session_labels, key="active_session_select")
    else:
        active_label = st.session_state["active_session_select"]
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

    laps = compute_clean_laps(active_session)
    clean = clean_lap_table(laps)

    if clean.empty:
        data_error_message = "No clean laps found in this session after outlier filtering -- check the file."
    else:
        clean_lap_numbers = clean["lap_number"].tolist()
        best_lap = int(clean.loc[clean["lap_time_s"].idxmin(), "lap_number"])

        # Shared by every lap-number selectbox/multiselect for the active
        # session (top bar and every page) so a lap is never just a bare
        # number -- picking "which lap" without seeing its time meant
        # opening it first to find out.
        lap_time_by_number = dict(zip(laps["lap_number"], laps["lap_time_s"]))

        if show_session_controls:
            analyzed_lap = sb2.selectbox(
                "Lap to analyze against theoretical best",
                clean_lap_numbers,
                index=clean_lap_numbers.index(best_lap),
                format_func=format_lap_option,
            )
            sb3.markdown('<div style="padding-top:28px;"></div>', unsafe_allow_html=True)
            if sb3.button("🔧 Edit kart setup", use_container_width=True):
                st.switch_page(page_kart_setup_obj)
        else:
            analyzed_lap = best_lap

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
