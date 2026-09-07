"use client";

import { useCallback, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import { CHART_METRICS, deltaTrace, lapColor, type ComparedLap } from "@/lib/lapCharts";
import { SECTOR_COLORS, type TracePoint } from "@/lib/trackMap";
import type { Sector, Segment } from "@/lib/sectors";
import ChartTrackMap from "./ChartTrackMap";

// Plotly touches `window` at import time, so it cannot be server-rendered.
// It is also ~4 MB, and only this page uses it -- loading it lazily keeps it
// off Home and the upload page entirely.
const Plot = dynamic(() => import("react-plotly.js"), {
  ssr: false,
  loading: () => (
    <div className="flex h-48 items-center justify-center text-sm text-muted">
      Loading charts...
    </div>
  ),
});

const AXIS = {
  gridcolor: "rgba(255,255,255,.06)",
  zerolinecolor: "rgba(255,255,255,.18)",
  linecolor: "rgba(255,255,255,.10)",
  tickfont: { color: "#8c959c", size: 10, family: "JetBrains Mono, monospace" },
  titlefont: { color: "#8c959c", size: 10, family: "Archivo, sans-serif" },
};

const LAYOUT_BASE = {
  paper_bgcolor: "rgba(0,0,0,0)",
  plot_bgcolor: "rgba(0,0,0,0)",
  font: { color: "#c9cfd4", family: "Archivo, sans-serif", size: 11 },
  margin: { l: 52, r: 16, t: 8, b: 34 },
  // One shared crosshair across every chart: the whole point of stacking
  // them is reading one distance down the page at once.
  hovermode: "x unified" as const,
  hoverlabel: {
    bgcolor: "#101417",
    bordercolor: "rgba(255,255,255,.12)",
    font: { color: "#eef0f1", family: "JetBrains Mono, monospace", size: 11 },
  },
  showlegend: false,
};

/** How far in or out one press of the zoom buttons goes. */
const ZOOM_STEP = 0.6;

/**
 * Speed, RPM, G and delta for the selected laps, over distance.
 *
 * Distance rather than time on the x-axis, so the same corner is at the same
 * place on every lap -- against time, two laps drift apart and nothing lines
 * up after the first mistake.
 *
 * Laps may come from several sessions and several drivers at once, so every
 * series is named by its `label` rather than by lap number: two drivers both
 * have a lap 10, and a legend reading "Lap 10" twice says nothing about
 * which line is whose.
 *
 * Sector boundaries are drawn as vertical rules on every chart, in the same
 * colours as the track map, so a difference can be attributed to a stretch
 * of tarmac without counting corners -- and hovering any of them marks the
 * spot on the track map beside them, which is how a distance on the x-axis
 * becomes a corner with a name.
 */
export default function LapCharts({
  laps,
  sectors,
  segments,
  trace,
  referenceKey,
  onReferenceChange,
}: {
  laps: ComparedLap[];
  sectors: Sector[];
  segments: Segment[];
  trace: TracePoint[];
  referenceKey: string | null;
  onReferenceChange: (key: string) => void;
}) {
  const reference = laps.find((lap) => lap.key === referenceKey) ?? laps[0];

  // The distance window every chart shows, or null for the whole lap. One
  // piece of state for all six charts: they are stacked to be read at one
  // distance down the page, so a zoom that applied to only the chart it was
  // performed on would break the one thing the stack is for.
  const [zoomWindow, setZoomWindow] = useState<[number, number] | null>(null);
  const [hoverDistance, setHoverDistance] = useState<number | null>(null);

  // The full extent of the selected laps, which is what "reset" returns to
  // and what the zoom buttons clamp against.
  const extent = useMemo<[number, number] | null>(() => {
    const starts: number[] = [];
    const ends: number[] = [];
    for (const lap of laps) {
      const distances = lap.trace.distanceM;
      if (distances.length === 0) continue;
      starts.push(distances[0]);
      ends.push(distances[distances.length - 1]);
    }
    if (starts.length === 0) return null;
    return [Math.min(...starts), Math.max(...ends)];
  }, [laps]);

  const view = zoomWindow ?? extent;

  /** Clamp a proposed window to the lap, keeping it at least 20 m wide. */
  const clamp = useCallback(
    (low: number, high: number): [number, number] | null => {
      if (!extent) return null;
      const width = Math.max(20, Math.min(high - low, extent[1] - extent[0]));
      let start = low;
      if (start < extent[0]) start = extent[0];
      if (start + width > extent[1]) start = extent[1] - width;
      return [start, start + width];
    },
    [extent],
  );

  const zoom = useCallback(
    (factor: number) => {
      if (!view || !extent) return;
      const centre = hoverDistance ?? (view[0] + view[1]) / 2;
      const half = ((view[1] - view[0]) * factor) / 2;
      const next = clamp(centre - half, centre + half);
      // Zoomed all the way out is the same as not zoomed: drop back to
      // autorange so the charts re-fit if a lap is added.
      setZoomWindow(next && next[1] - next[0] >= extent[1] - extent[0] ? null : next);
    },
    [view, extent, hoverDistance, clamp],
  );

  /**
   * Plotly's own zoom, drag and double-click, folded into the same state.
   *
   * Without this, dragging a box on one chart would zoom that chart alone
   * and leave the other five where they were.
   */
  const onRelayout = useCallback(
    (event: Record<string, unknown>) => {
      if (event["xaxis.autorange"]) {
        setZoomWindow(null);
        return;
      }
      const low = event["xaxis.range[0]"];
      const high = event["xaxis.range[1]"];
      if (typeof low === "number" && typeof high === "number") {
        setZoomWindow(clamp(Math.min(low, high), Math.max(low, high)));
      }
    },
    [clamp],
  );

  const onHover = useCallback((event: { points?: { x?: unknown }[] }) => {
    const x = event.points?.[0]?.x;
    if (typeof x === "number") setHoverDistance(x);
  }, []);

  const deltas = useMemo(
    () =>
      reference
        ? laps
            .filter((lap) => lap.key !== reference.key)
            .map((lap) => ({
              key: lap.key,
              label: lap.label,
              ...deltaTrace(lap.trace, reference.trace),
            }))
        : [],
    [laps, reference],
  );

  const colorFor = (key: string) =>
    lapColor(
      laps.findIndex((lap) => lap.key === key),
      SECTOR_COLORS,
    );

  // Vertical rules at each sector boundary, on every chart.
  const sectorLines = sectors.slice(1).map((sector) => ({
    type: "line" as const,
    x0: sector.startM,
    x1: sector.startM,
    y0: 0,
    y1: 1,
    yref: "paper" as const,
    line: {
      color: SECTOR_COLORS[sector.index % SECTOR_COLORS.length],
      width: 1,
      dash: "dot" as const,
    },
  }));

  /**
   * A faint band behind every corner.
   *
   * Sector rules say where a *timed* section starts, which is not the same
   * question as "was the kart turning here". With the corners shaded, a dip
   * in the speed trace can be read as a corner without cross-referencing
   * anything -- and the two encodings together are what make the x-axis
   * legible as a lap rather than as a number of metres.
   */
  const cornerBands = useMemo(
    () =>
      segments
        .filter((segment) => segment.kind === "corner")
        .map((segment) => ({
          type: "rect" as const,
          x0: segment.start_m,
          x1: segment.end_m,
          y0: 0,
          y1: 1,
          yref: "paper" as const,
          fillcolor: "rgba(255,255,255,.05)",
          line: { width: 0 },
          layer: "below" as const,
        })),
    [segments],
  );

  /**
   * The marker line under the pointer, drawn on every chart at once.
   *
   * Plotly's own crosshair only appears on the chart the pointer is over.
   * With six stacked charts read at one distance, the other five need to
   * show the same place -- otherwise the reader is matching x-positions by
   * eye down the page.
   */
  const hoverLine =
    hoverDistance === null
      ? []
      : [
          {
            type: "line" as const,
            x0: hoverDistance,
            x1: hoverDistance,
            y0: 0,
            y1: 1,
            yref: "paper" as const,
            line: { color: "rgba(238,240,241,.3)", width: 1 },
          },
        ];

  const shapesFor = (extra: object[] = []) => [
    ...cornerBands,
    ...sectorLines,
    ...hoverLine,
    ...extra,
  ];

  // Zoom is x-only: `fixedrange` on y makes a box drag, a scroll and a
  // pinch all mean "narrow the distance window", which is the axis worth
  // navigating. It also keeps the y scales fixed while zooming, so two laps
  // stay comparable instead of each chart rescaling under the pointer.
  const xaxisFor = (title: string, showticklabels: boolean) => ({
    ...AXIS,
    title: { text: title },
    showticklabels,
    // Plotly's default crosshair in unified-hover mode is a thick white
    // dashed bar, which next to the faint rule drawn on the other five
    // charts reads as two different things happening. Styled to match it.
    showspikes: true,
    spikemode: "across" as const,
    spikethickness: 1,
    spikedash: "solid" as const,
    spikecolor: "rgba(238,240,241,.45)",
    ...(zoomWindow ? { range: zoomWindow, autorange: false as const } : { autorange: true as const }),
  });

  const CHART_CONFIG = {
    displayModeBar: false,
    responsive: true,
    scrollZoom: true,
    doubleClick: "reset" as const,
  };

  const chartEvents = {
    onRelayout,
    onHover,
    onUnhover: () => setHoverDistance(null),
  };

  if (laps.length === 0) {
    return (
      <div className="rounded border border-hairline bg-surface px-4 py-8 text-center text-sm text-muted">
        Tick <span className="text-ink2">Cmp</span> on two or more laps above to plot them against
        each other. Laps from different tabs can be compared together.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-4">
        <h2 className="text-sm font-bold">Lap comparison</h2>
        <div className="flex flex-wrap items-center gap-3 text-xs">
          {laps.map((lap) => (
            <span key={lap.key} className="flex items-center gap-1.5">
              <span
                className="inline-block h-2 w-2 rounded-sm"
                style={{ background: colorFor(lap.key) }}
                aria-hidden
              />
              <span className="text-ink2">{lap.label}</span>
            </span>
          ))}
        </div>
        <label className="ml-auto flex items-center gap-2">
          <span className="label">Delta vs</span>
          <select
            value={reference?.key ?? ""}
            onChange={(event) => onReferenceChange(event.target.value)}
            className="rounded border border-hairline bg-surface px-2 py-1 text-sm"
          >
            {laps.map((lap) => (
              <option key={lap.key} value={lap.key}>
                {lap.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {/* Zoom controls, in one row above the charts they scope. */}
      <div className="flex flex-wrap items-center gap-2 rounded border border-hairline bg-raised px-3 py-2">
        <span className="label">Zoom</span>
        <span className="flex items-center gap-1">
          <ZoomButton onClick={() => zoom(1 / ZOOM_STEP)} label="Zoom out" symbol="−" />
          <ZoomButton onClick={() => zoom(ZOOM_STEP)} label="Zoom in" symbol="+" />
        </span>
        <button
          type="button"
          onClick={() => setZoomWindow(null)}
          disabled={zoomWindow === null}
          className="rounded border border-hairline bg-surface px-2 py-0.5 text-xs text-ink2 hover:border-accent hover:text-ink disabled:opacity-40"
        >
          Full lap
        </button>

        {/* Straight to a sector: the unit a driver thinks in, and the one
            the table beside it is already split by. */}
        <span className="ml-1 flex flex-wrap items-center gap-1">
          {sectors.map((sector) => {
            const active =
              zoomWindow !== null &&
              Math.abs(zoomWindow[0] - sector.startM) < 1 &&
              Math.abs(zoomWindow[1] - sector.endM) < 1;
            return (
              <button
                key={sector.index}
                type="button"
                onClick={() => setZoomWindow(clamp(sector.startM, sector.endM))}
                title={`${Math.round(sector.startM)}–${Math.round(sector.endM)} m`}
                className={`rounded border px-2 py-0.5 text-xs font-semibold ${
                  active ? "border-current" : "border-hairline"
                }`}
                style={{ color: SECTOR_COLORS[sector.index % SECTOR_COLORS.length] }}
              >
                S{sector.index + 1}
              </button>
            );
          })}
        </span>

        <span className="ml-auto text-[11px] text-muted">
          {view ? `${Math.round(view[0])}–${Math.round(view[1])} m` : ""}
          <span className="ml-2">Drag to box-zoom · scroll to zoom · double-click to reset</span>
        </span>
      </div>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_19rem]">
        <div className="space-y-3">
          {CHART_METRICS.map((metric) => (
            <ChartPanel key={metric.key} title={`${metric.label} (${metric.unit})`}>
              <Plot
                data={laps.map((lap) => ({
                  x: lap.trace.distanceM,
                  y: lap.trace[metric.key] as (number | null)[],
                  type: "scattergl",
                  mode: "lines",
                  name: lap.label,
                  line: { color: colorFor(lap.key), width: 1.6 },
                  hovertemplate: `${lap.label}: %{y:.1f}<extra></extra>`,
                }))}
                layout={{
                  ...LAYOUT_BASE,
                  height: 190,
                  shapes: shapesFor(),
                  xaxis: xaxisFor("", false),
                  yaxis: { ...AXIS, fixedrange: true },
                }}
                config={CHART_CONFIG}
                style={{ width: "100%" }}
                {...chartEvents}
              />
            </ChartPanel>
          ))}

          <ChartPanel
            title={
              reference ? `Delta to ${reference.label} (s) — above zero is slower` : "Delta (s)"
            }
          >
            <Plot
              data={deltas.map((delta) => ({
                x: delta.distanceM,
                y: delta.deltaS,
                type: "scattergl",
                mode: "lines",
                name: delta.label,
                line: { color: colorFor(delta.key), width: 1.6 },
                hovertemplate: `${delta.label}: %{y:+.3f}s<extra></extra>`,
              }))}
              layout={{
                ...LAYOUT_BASE,
                height: 210,
                shapes: shapesFor([
                  {
                    type: "line",
                    xref: "paper",
                    x0: 0,
                    x1: 1,
                    y0: 0,
                    y1: 0,
                    line: { color: "rgba(255,255,255,.28)", width: 1, dash: "dash" },
                  },
                ]),
                xaxis: xaxisFor("Distance (m)", true),
                yaxis: { ...AXIS, fixedrange: true },
              }}
              config={CHART_CONFIG}
              style={{ width: "100%" }}
              {...chartEvents}
            />
          </ChartPanel>
        </div>

        <div className="xl:sticky xl:top-4 xl:self-start">
          <ChartTrackMap
            trace={trace}
            sectors={sectors}
            segments={segments}
            distanceM={hoverDistance}
            lapLengthM={extent ? extent[1] : null}
          />
        </div>
      </div>

      <p className="text-xs text-muted">
        Plotted against distance, not time, so the same corner sits at the same place on every lap.
        Shaded bands are corners and dotted rules are sector boundaries, both from the session
        shown in the table and coloured as on the track map. Zoom applies to every chart at once,
        because they are stacked to be read at one distance down the page. Delta is computed by
        interpolating both laps onto a common distance grid &mdash; the two were sampled wherever
        their own GPS fixes landed.
      </p>
    </div>
  );
}

function ZoomButton({
  onClick,
  label,
  symbol,
}: {
  onClick: () => void;
  label: string;
  symbol: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="h-6 w-6 rounded border border-hairline bg-surface text-sm leading-none text-ink2 hover:border-accent hover:text-ink"
    >
      {symbol}
    </button>
  );
}

function ChartPanel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded border border-hairline bg-surface px-2 pb-1 pt-2">
      <div className="label px-2">{title}</div>
      {children}
    </div>
  );
}
