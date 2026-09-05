# Handoff: Karting Telemetry — Lap Analysis UI

## Overview

Two design directions for the lap-analysis screen of a karting telemetry app ("APEXLINE" is a
placeholder product name). Both show a single selected lap compared against a reference lap, with
sector deltas, a corner-by-corner ledger, theoretical-best and class-record context, and a
community/leaderboard hook.

- **1a — channel-stack, pit-wall dark.** Stacked channel traces (speed, delta-t, throttle, brake)
  sharing one distance axis, the way a data engineer reads telemetry. Lap list left, track map and
  class standing right.
- **1b — map-led, factory light.** Leads with a delta-coloured track map plus a written corner
  ledger, so the driver sees *where* the time went before reading a trace. Traces are secondary,
  below the map. Lap list, sector board and share panel on the right.

Both are the same information architecture with different emphasis — pick one, or take the map from
1b into the dark chrome of 1a.

## About the design files

The files in this bundle are **design references created in HTML** — prototypes showing intended
layout, hierarchy and colour semantics. They are not production code to copy.

The target app is the existing **Streamlit + Plotly** application in
`f7sbtpgt8g-ai/Karting` (branch `claude/new-session-yilc1w`). The task is to recreate these
designs inside that app's environment, replacing the current `page_data_analysis` /
`page_lap_comparison` / `page_track_map` layouts. Two viable routes:

1. **Stay in Streamlit.** Achievable for most of it: `st.columns` for the three-column shell,
   Plotly subplots with a shared x-axis for the channel stack, `plotly` scattergl with per-segment
   colouring for the delta map, and `st.dataframe`/`st.markdown` for the tables. The dense 11–12px
   type, inset accent bars and dark chrome need a custom CSS block. Expect the chrome to be
   approximate; the charts can be exact.
2. **Move the analysis page to a real frontend** (React + a charting lib such as uPlot or
   ECharts) talking to the existing `telemetry/` package over a thin FastAPI layer. Recommended if
   the crosshair-linked channel stack and hover-synced track map matter, since Streamlit's rerun
   model makes linked-cursor interaction expensive.

The existing Python modules already produce every number in these mockups — see **Data sources**
below. No new analysis logic is required.

## Fidelity

**High-fidelity.** Colours, type sizes, spacing and copy are final-intent. Two caveats:

- The track outline, corner names ("Christmas hairpin", "Pylon left"), lap times, weather and tyre
  values are **placeholders**. Replace with real circuit geometry from the GPS trace and the
  operator's own corner naming.
- The traces are synthetic paths drawn to look plausible. Real traces come from the session data.

## Screens / views

### Shell (both variants)

**Top bar** — 52px tall, 18px horizontal padding, 1px bottom border.
- Logo mark: 13×15px accent block, `transform: skewX(-14deg)`, next to wordmark in Archivo 700
  13px, `letter-spacing: .16em`.
- Nav: Analyse / Theoretical / Leaderboards / Garage. Label style (see Typography), 8px 11px
  padding. Active item: full-strength ink + `box-shadow: inset 0 -2px 0 #ff3b1f`. Inactive: muted.
- Right side (1a): device status pill (5px green dot + "MyChron 5S · synced", 1px border, 4px
  radius), primary "Import session" button (accent fill, dark text), 28×28 avatar tile 4px radius.
- Right side (1b): condensed context string + avatar only.

**Context strip (1a only)** — 46px tall row of labelled cells, each `0 18px` padding with a 1px
right divider: Circuit / Class / Session / Air+Track temp / Tyre, then flex spacer, then two
outline buttons ("Share comparison", "Export CSV"). Each cell is a 9px uppercase label above a
600 12px value.

### 1a — Lap analysis, channel stack (dark)

Card width 1440px. Three columns: 238px lap list | flexible centre | 302px right rail.

**Lap list (238px, `#0d1114`)**
- Header row: "Laps · Run 4" label + accent "Compare 2".
- Column head and rows on `grid-template-columns: 26px 1fr 54px 46px; gap: 8px`, rows `7px 14px`.
- Sector bar cell: four 4px-tall flex-1 bars, 3px gap. Bar colour = who owns that sector
  (green faster / purple session-best / red slower / `#2a3136` neutral).
- Selected lap: background `#181e22`, `inset 2px 0 0 #ff3b1f`. Reference lap: `inset 2px 0 0 #b06cff`.
- Older laps fade via `opacity: .6` then `.45` — a deliberate recency cue, not disabled state.
- Reference legend box at the bottom: 1px border, 5px radius, three 14×2px colour keys —
  session best (purple), theoretical best (`#ffd23d`), track record (grey, 55% opacity).

**Header block (centre top)**
- Selected lap time: mono 700, 44px, `line-height: .92`, `letter-spacing: -.02em`.
- Delta vs session best: same size, green `#2fd07a` for a gain.
- Divided block (34px left padding, 1px left border) listing Best lap / Theoretical / Class record,
  each a 96px label + 14px mono value in that reference's colour.
- Sector bar chart, right-aligned: four 52px columns, 56px track height. Gains grow up from the
  bottom (`align-items: flex-end`), losses hang down from the top (`flex-start`) — sign is read
  from direction as well as colour. Value in 10px mono under each bar, then S1–S4 label.

**Trace controls** — legend for the two compared laps, then two segmented groups: axis basis
(Distance | Time) and view (Overlay | Split channels). Active chip `background: #1f262b`.

**Channel stack** — each channel is a `grid-template-columns: 74px 1fr` row with a 1px top border.
The 74px gutter holds the channel name, its unit in 10px mono, and the cursor readout for each
compared lap. The plot area is a `viewBox`-scaled SVG with `preserveAspectRatio: none`.
- Speed: 158px tall. Horizontal gridlines at 25/50/75%, vertical lines at corner positions, corner
  labels T1–T8 above the plot. Reference lap `#b06cff` at 1.6px, selected lap `#ff3b1f` at 2px.
  All strokes use `vector-effect: non-scaling-stroke` so the non-uniform scale doesn't distort them.
- Delta-t: 76px. Zero line dashed at 22% white. Area under the curve filled
  `rgba(47,208,122,.16)` when gaining. "Gaining" / "Losing" labels pinned top-left and bottom-left.
- Throttle (56px) and Brake (46px) — behind the `showThrottleBrake` toggle.
- A full-height cursor line (`#ff3b1f`, 1px) with a pill readout at the top ("612 m · T6 entry")
  spans the whole stack. In production this follows the pointer and drives both the gutter readouts
  and the dot on the track map.

**"Where the time went" table** — `grid-template-columns: 30px 1fr 56px 56px 62px`, alternating row
background `#0f1316`, columns: turn, corner name, min speed, apex offset in metres, delta.

**Coach note card** (300px) — plain-language reading of the biggest loss, with "Send to driver"
(accent) and "Pin corner" (outline) actions. Text is generated: `telemetry/narrative.py` already
produces this, optionally via Claude.

**Right rail (302px)**
- Track map, 180px tall SVG: a 15px dark casing path with four 7px coloured overlay paths using
  `pathLength="100"` + `stroke-dasharray`/`stroke-dashoffset` to paint each sector's delta colour
  onto the outline. This dash trick is how sector colouring is done without splitting the path —
  keep it. Start/finish tick, plus a cursor dot (`r=7`, accent, 2.5px dark stroke).
- Legend: faster / slower / personal-best sector.
- Sector table: `34px 1fr 54px 56px`, per-sector best-in lap + time + delta.
- Class standing card: "P4" in 26px mono 700, "of 218 at this circuit", a 5px progress bar
  (accent fill, `#22282c` track), gap to P3 and the class record, then a full-width outline button
  "Compare with P3 lap" — the community hook.

### 1b — Lap analysis, map-led (light)

Card width 1440px, page background `#f4f4f1`, 16px gutters, 16px gaps. Two columns: flexible centre
| 330px right rail.

**Delta map card** — white, 1px border, 6px radius. Header: "Delta map · lap 12 vs lap 9" + the
explanatory line "colour = time gained or lost per metre", and a Delta | Speed | Line segmented
control (active chip `#0f1214` on white text).
- Map: 376px tall SVG on `#faf9f6`, 26px `#e3e2dc` casing, 11px coloured overlay segments (same
  dash technique, six segments here). Circled marker (`r=16`, 2px `#d02f1f`) rings the worst corner.
- **Corner ledger** (250px, 1px left border): one row per turn, name left / delta right in mono.
  The worst corner is expanded into a callout: `background: #fdf0ee`, `border-left: 2px solid
  #d02f1f`, bold name and delta, plus a 11px/1.45 explanation. Footer row: "Net" + total delta in
  14px mono 700.

**Speed & cumulative delta card** — 120px speed plot (reference `#a8a9a3`, selected `#0f1214`) and
a 58px delta plot below it (green line + 14%-alpha fill), sharing the distance axis. Header notes
the lap length and axis basis.

**Right rail**
- Hero lap card: `#0f1214` fill, 6px radius. 46px mono 700 white lap time, then three stats above a
  1px white-14% divider: vs best (green `#3ddb85`), vs theoretical (`#ffd23d`), vs record
  (`#ff6a58`).
- Sector board: white card, `30px 1fr 58px 58px` grid; the losing sector row is tinted `#fdf0ee`.
- Lap list: per row, lap number, time, a 60×5px bar (`#e3e2dc` track, fill green for the best,
  `#a8a9a3` for the reference, `#c9c9c3` otherwise), and the delta.
- Share card: explains publishing the lap to the circuit + class board so others can overlay
  their own lap, with "Publish lap" (accent) and "Copy link" (outline).

### Mobile — 2a, at-track quick lap review (402×874, dark)

Portrait, meant to be read standing next to the kart with gloves half off. Vertical order:

1. **Context row** (16px padding): circuit + class label above "Run 4 · 14 laps", with a pill-shaped
   device status chip (20px radius, 5px green dot) on the right.
2. **Hero block** (`#101417`, hairline top and bottom): "NEW BEST · LAP 12" in accent label style,
   lap time mono 700 48px `line-height: .88`, delta mono 700 22px alongside. Below a hairline:
   Theoretical / Class record / Standing as three 15px mono values.
3. **Lap strip** — horizontally scrollable chips, 78px min width, 5px radius, selected chip
   `#1f262b` + `inset 0 -2px 0` accent. Each chip: lap id, time, delta. Deliberately overflows the
   right edge to signal swipe.
4. **Sector row** — four flex-1 blocks, 64px min height, `inset 0 -3px 0` in the sector's delta
   colour. Time 13px, delta 11px.
5. **Trace card** — channel tabs (Speed | Delta | Pedals) as 8px/12px chips with a 44px-tall tap
   area, plus "Full screen ↗" to 2c. Cursor values sit above the plot (accent = selected lap,
   purple = reference) with the cursor distance beside them. Plot 138px tall, strokes 3.4px
   selected / 2.4px reference — roughly 40% heavier than desktop. Corner ticks T1/T3/T6/T8 only;
   the active corner is accent.
6. **"Where the time went"** — full-bleed rows, 13px 16px padding, hairline top. Turn number,
   corner name 600 13px with an 11.5px plain-language note, delta 14px, chevron. The worst corner's
   row is tinted `#15100f`. Tapping opens a corner detail screen (not yet designed).
7. **Tab bar** — four items, 3px accent tick above the active label, 22px bottom padding for the
   home indicator.

### Mobile — 2b, trace scrub (402×874, dark)

The screen for actually reading traces on a phone. Four ideas carry it:

- **Fixed readout, not axis labels.** A `#101417` block under the nav shows cursor distance
  (mono 700 20px), the corner name in accent, then three columns — Speed, Delta t, Throttle — each
  showing the selected lap's value at 19px mono 700 and the reference's at 13px. Everything a
  number would have to be read off the plot for lives here instead.
- **Tall channels, reserved label rows.** Speed 150px, Delta 92px, Pedals 76px, each with a 22px
  label row above the plot so nothing overlaps the trace. Strokes 3–3.4px selected, 2–2.4px
  reference; brake is `#4d7cff` so it separates from throttle at small size. Full-bleed to the
  screen edge — no left gutter.
- **Whole-lap strip.** A 34px minimap of the lap with the visible window drawn as an accent
  rectangle (`rgba(255,59,31,.14)` fill, 1.5px border), plus "Reset zoom". Answers "where am I in
  the lap" without leaving the zoomed view.
- **Thumb scrubber, bottom.** 52px track with a 46px accent handle showing the live distance, and
  52px corner-step buttons either side (◀ T5 / T7 ▶). The finger stays off the plot. Footer line:
  "Drag to scrub · rotate for full-width traces".

### Mobile — 2c, landscape (874×402, dark)

Rotating gives the whole lap at desktop stroke density. 48px top padding clears the status bar and
dynamic island. One header line (lap pair, cursor distance, corner, cumulative delta right-aligned),
then Speed 114px / Delta 64px / Pedals 48px with inline labels, then a footer with all eight corner
ticks and two outline buttons (Sectors, Map). No side chrome at all — the trace gets the full width.

### Not yet designed

The corner-detail screen reached by tapping a row in 2a, plus the leaderboards, theoretical-best
builder and garage screens named in the nav. Ask before implementing those — they should be designed
first rather than invented in code.

## Interactions & behaviour

- **Lap selection.** Clicking a lap in the list makes it the selected (accent) lap. A second
  selection sets the reference (purple in 1a, grey in 1b). "Compare 2" indicates the mode. All
  deltas, sector colours, the map and the corner table recompute from the pair.
- **Reference source.** The reference may be another lap in the session, the driver's PB from an
  earlier session, a teammate's lap, or the theoretical best.
  `comparison.cross_session_delta_trace` already supports cross-session references.
- **Linked cursor.** Pointer moves over any channel plot → vertical cursor at that distance across
  every plot, gutter readouts update per compared lap, pill shows distance + nearest corner, dot
  moves along the track map. This is the single most important interaction; if a Streamlit
  implementation can't do it smoothly, that's the argument for route 2.
- **Axis basis.** Distance | Time swaps the x axis. Distance is the default and the correct default
  — time-basis overlays diverge and become unreadable.
- **Overlay | Split channels.** Split is drawn. Overlay collapses the channels into one stacked
  plot with a shared normalised y — a density option for experienced users.
- **Hover on map / ledger.** Hovering a corner ledger row highlights that segment on the map and
  brackets it on the traces; hovering a map segment does the reverse.
- **Sector bar sign.** Direction encodes sign; never rely on colour alone.
- **Empty and degraded states** to design for: session with a single clean lap (no reference — show
  theoretical best only), no GPS lock (map unavailable, traces still valid), missing brake channel
  (drop that stack row rather than showing a flat line), lap flagged as an incident by
  `laps.detect_anomalous_laps` (show it struck through / dimmed in the list, excluded from best).

## State

| State | Type | Notes |
|---|---|---|
| `selectedLap` | int | Drives every delta in the view |
| `referenceLap` | int \| "theoretical" \| "pb" \| driver id | Default: session best |
| `cursorDistanceM` | float \| null | Linked-cursor position, shared by all plots and the map |
| `axisBasis` | `"distance"` \| `"time"` | Default `"distance"` |
| `traceView` | `"split"` \| `"overlay"` | Default `"split"` |
| `visibleChannels` | set | Speed and delta always on; throttle/brake toggleable |
| `mapMode` | `"delta"` \| `"speed"` \| `"line"` | 1b only |
| `hoveredSegment` | segment label \| null | Cross-highlights map ↔ ledger ↔ traces |
| `pinnedCorners` | segment label[] | From "Pin corner" |

Three props already exist on the mockup as tweaks: `showThrottleBrake`, `showTheoretical`,
`showCommunity` (all boolean, default true).

## Data sources (existing repo)

| UI element | Produces it |
|---|---|
| Lap list, times, outlier/incident flags | `telemetry/laps.py` — `lap_table`, `flag_outlier_laps`, `lap_time_with_deltas`, `detect_anomalous_laps` |
| Delta-t trace | `telemetry/delta.py::delta_time_trace` (400-point distance grid) |
| Sector / corner times | `telemetry/delta.py::segment_times_for_lap` |
| Theoretical best + which lap owns each sector | `telemetry/delta.py::theoretical_best_lap` |
| Cross-session / teammate reference | `telemetry/comparison.py::cross_session_delta_trace`, `driver_comparison` |
| Corner detection, GPS trace, segment midpoints | `telemetry/corners.py` |
| Min/apex/exit speed and RPM per corner | `telemetry/metrics.py::segment_aggregates`, `lap_metric_trace` |
| Coach note text | `telemetry/narrative.py` |
| Class standing / leaderboards | `telemetry/accounts.py`, `telemetry/storage.py` |
| Air/track temp | `telemetry/weather.py` |
| Tyre / setup values | `telemetry/setup_config.py`, `setup_engine.py` |

Distances are in metres, times in seconds, speed in km/h — matching the existing frames. Lap times
render as `SS.mmm` (three decimals) and deltas as signed `±S.mmm`; both use a real minus sign (−)
in the design.

## Design tokens

**Colour — semantics first.** These follow broadcast timing convention and should not be recoloured
arbitrarily: purple = best/reference, green = gain, red = loss, yellow = theoretical.

| Token | Dark (1a) | Light (1b) |
|---|---|---|
| Canvas | `#0b0d0f` | `#f4f4f1` |
| Surface | `#0d1114` | `#ffffff` |
| Surface raised | `#101417` | `#faf9f6` |
| Row alt / selected | `#0f1316` / `#181e22` | `#f6f5f1` |
| Ink primary | `#eef0f1` | `#0f1214` |
| Ink secondary | `#c9cfd4` | `#3c4247` |
| Ink muted | `#8c959c` | `#6a7278` |
| Ink faint (labels) | `#6d767d`, `#565f66` | `#8c9096` |
| Hairline | `rgba(255,255,255,.07–.14)` | `rgba(16,20,24,.07–.12)` |
| Neutral bar / track | `#22282c`, `#2a3136` | `#e3e2dc`, `#c9c9c3`, `#a8a9a3` |
| Accent (brand, cursor, CTA) | `#ff3b1f` | `#ff3b1f`, link/hover ink `#c02d12` |
| Gain | `#2fd07a` (`#3ddb85` on dark card) | `#16a45b` |
| Loss | `#ff4a3d` (`#ff6a58` on dark card) | `#d02f1f` |
| Best / reference lap | `#b06cff` | `#a8a9a3` |
| Theoretical best | `#ffd23d` | `#ffd23d` |
| Loss tint | — | `#fdf0ee` |
| Status ok | `#2fd07a` | — |

**Typography**
- UI: **Archivo** 400/500/600/700.
- Numbers: **JetBrains Mono** with `font-variant-numeric: tabular-nums` — every lap time, delta,
  speed and unit. Non-negotiable; columns must not shift.
- Label style (used everywhere for column heads, channel names, buttons): Archivo 600, 9px,
  `letter-spacing: .14em`, uppercase, `line-height: 1`.
- Scale in use: 9/10/11/12/13/14/15/26/44/46px. Body copy 12px/1.5.
- Hero lap time: mono 700, 44–46px, `line-height: .9–.92`, `letter-spacing: -.02em`.

**Spacing** — 3 / 4 / 5 / 6 / 8 / 10 / 12 / 14 / 16 / 18 / 20 / 28 / 34px. Table rows 7–8px
vertical. Card padding 12–16px. Page gutter 16–18px.

**Radius** — 3px (chips, buttons, small bars), 4px (top-bar controls), 5–6px (cards). Nothing larger.

**Borders & shadow** — 1px hairlines only. No drop shadows anywhere; elevation is done with
surface value. Selection uses `box-shadow: inset 2px 0 0 <colour>`.

**Density** — deliberately dense (11–12px table text, 7px row padding). If accessibility review
pushes back, scale the whole thing up rather than loosening selectively.

## Assets

None. The logo mark is a skewed CSS block; the track outline and all charts are inline SVG paths
generated for the mockup. No icon set is used — replace with the repo's existing emoji/icon
convention or a proper icon set when implementing.

## Screenshots

`screenshots/` holds a render of each variant:

- `1a-web-channel-stack-dark.png`, `1b-web-map-led-light.png` — the two web directions at 1440px.
- `2a-mobile-quick-review.png`, `2b-mobile-trace-scrub.png` — portrait, 402×874 (iPhone frame).
- `2c-mobile-landscape.png` — the rotated full-lap view, 874×402.

## Files in this bundle

- `Karting Telemetry.dc.html` — the design. Open in a browser; both variants are on one canvas,
  anchored `#1a` and `#1b`.
- `support.js` — runtime the HTML needs to render. Keep alongside.
- `traces.json`, `traces2.json`, `track.json` — the synthetic trace and track geometry used to
  generate the SVG paths.
- `ios-frame.jsx` — iOS device frame, unused so far; present for the mobile screens once designed.
