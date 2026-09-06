"use client";

import { useMemo } from "react";
import dynamic from "next/dynamic";
import { CHART_METRICS, deltaTrace, lapColor, type LapTrace } from "@/lib/lapCharts";
import { SECTOR_COLORS } from "@/lib/trackMap";
import type { Sector } from "@/lib/sectors";

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

/**
 * Speed, RPM, G and delta for the selected laps, over distance.
 *
 * Distance rather than time on the x-axis, so the same corner is at the same
 * place on every lap -- against time, two laps drift apart and nothing lines
 * up after the first mistake.
 *
 * Sector boundaries are drawn as vertical rules on every chart, in the same
 * colours as the track map, so a difference can be attributed to a stretch
 * of tarmac without counting corners.
 */
export default function LapCharts({
  traces,
  sectors,
  referenceLap,
  onReferenceChange,
}: {
  traces: LapTrace[];
  sectors: Sector[];
  referenceLap: number | null;
  onReferenceChange: (lapNumber: number) => void;
}) {
  const reference = traces.find((t) => t.lapNumber === referenceLap) ?? traces[0];

  const deltas = useMemo(
    () =>
      reference
        ? traces
            .filter((t) => t.lapNumber !== reference.lapNumber)
            .map((t) => ({ lapNumber: t.lapNumber, ...deltaTrace(t, reference) }))
        : [],
    [traces, reference],
  );

  const colorFor = (lapNumber: number) =>
    lapColor(
      traces.findIndex((t) => t.lapNumber === lapNumber),
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
    line: { color: SECTOR_COLORS[sector.index % SECTOR_COLORS.length], width: 1, dash: "dot" as const },
  }));

  if (traces.length === 0) {
    return (
      <div className="rounded border border-hairline bg-surface px-4 py-8 text-center text-sm text-muted">
        Tick <span className="text-ink2">Cmp</span> on two or more laps above to plot them against
        each other.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-4">
        <h2 className="text-sm font-bold">Lap comparison</h2>
        <div className="flex flex-wrap items-center gap-3 text-xs">
          {traces.map((trace) => (
            <span key={trace.lapNumber} className="flex items-center gap-1.5">
              <span
                className="inline-block h-2 w-2 rounded-sm"
                style={{ background: colorFor(trace.lapNumber) }}
                aria-hidden
              />
              <span className="text-ink2">Lap {trace.lapNumber}</span>
            </span>
          ))}
        </div>
        <label className="ml-auto flex items-center gap-2">
          <span className="label">Delta vs</span>
          <select
            value={reference?.lapNumber ?? ""}
            onChange={(e) => onReferenceChange(Number(e.target.value))}
            className="rounded border border-hairline bg-surface px-2 py-1 text-sm"
          >
            {traces.map((trace) => (
              <option key={trace.lapNumber} value={trace.lapNumber}>
                Lap {trace.lapNumber}
              </option>
            ))}
          </select>
        </label>
      </div>

      {CHART_METRICS.map((metric) => (
        <ChartPanel key={metric.key} title={`${metric.label} (${metric.unit})`}>
          <Plot
            data={traces.map((trace) => ({
              x: trace.distanceM,
              y: trace[metric.key] as (number | null)[],
              type: "scattergl",
              mode: "lines",
              name: `Lap ${trace.lapNumber}`,
              line: { color: colorFor(trace.lapNumber), width: 1.6 },
              hovertemplate: `Lap ${trace.lapNumber}: %{y:.1f}<extra></extra>`,
            }))}
            layout={{
              ...LAYOUT_BASE,
              height: 190,
              shapes: sectorLines,
              xaxis: { ...AXIS, title: { text: "" }, showticklabels: false },
              yaxis: { ...AXIS },
            }}
            config={{ displayModeBar: false, responsive: true }}
            style={{ width: "100%" }}
          />
        </ChartPanel>
      ))}

      <ChartPanel
        title={
          reference
            ? `Delta to lap ${reference.lapNumber} (s) — above zero is slower`
            : "Delta (s)"
        }
      >
        <Plot
          data={deltas.map((delta) => ({
            x: delta.distanceM,
            y: delta.deltaS,
            type: "scattergl",
            mode: "lines",
            name: `Lap ${delta.lapNumber}`,
            line: { color: colorFor(delta.lapNumber), width: 1.6 },
            hovertemplate: `Lap ${delta.lapNumber}: %{y:+.3f}s<extra></extra>`,
          }))}
          layout={{
            ...LAYOUT_BASE,
            height: 210,
            shapes: [
              ...sectorLines,
              {
                type: "line",
                xref: "paper",
                x0: 0,
                x1: 1,
                y0: 0,
                y1: 0,
                line: { color: "rgba(255,255,255,.28)", width: 1, dash: "dash" },
              },
            ],
            xaxis: { ...AXIS, title: { text: "Distance (m)" } },
            yaxis: { ...AXIS },
          }}
          config={{ displayModeBar: false, responsive: true }}
          style={{ width: "100%" }}
        />
      </ChartPanel>

      <p className="text-xs text-muted">
        Plotted against distance, not time, so the same corner sits at the same place on every lap.
        Dotted rules are the sector boundaries, coloured as on the track map. Delta is computed by
        interpolating both laps onto a common distance grid &mdash; the two were sampled wherever
        their own GPS fixes landed.
      </p>
    </div>
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
