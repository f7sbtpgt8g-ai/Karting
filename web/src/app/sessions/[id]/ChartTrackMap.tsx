"use client";

import { useMemo } from "react";
import { positionAtDistance, projectTrack, sectorPaths, type TracePoint } from "@/lib/trackMap";
import { sectorAt, segmentAt, type Sector, type Segment } from "@/lib/sectors";

/**
 * The track, with a marker showing where the pointer is on the charts.
 *
 * A trace plotted against distance answers "what happened at 340 m", which
 * is not a question anyone actually has. The question is "what happened at
 * the hairpin" -- so this turns the x-axis back into a place.
 *
 * Beside the charts rather than a tooltip that follows the cursor, and
 * deliberately: the marker has to be watchable while the pointer moves along
 * the trace, and a floating panel would both jump around and cover the very
 * line being read. Sticky, so it stays visible while scrolling down the
 * stack of charts.
 */
export default function ChartTrackMap({
  trace,
  sectors,
  segments,
  distanceM,
  lapLengthM,
}: {
  trace: TracePoint[];
  sectors: Sector[];
  segments: Segment[];
  distanceM: number | null;
  lapLengthM: number | null;
}) {
  const track = useMemo(() => projectTrack(trace), [trace]);
  const paths = useMemo(() => (track ? sectorPaths(track, sectors) : []), [track, sectors]);
  const marker = useMemo(
    () => (track && distanceM !== null ? positionAtDistance(track, distanceM) : null),
    [track, distanceM],
  );

  const segment = distanceM !== null ? segmentAt(segments, distanceM) : null;
  const sector = distanceM !== null ? sectorAt(sectors, distanceM) : null;

  if (!track) {
    return (
      <div className="rounded border border-hairline bg-surface px-4 py-6 text-xs text-muted">
        This session has no GPS positions stored, so the trace cannot be shown on a track map.
      </div>
    );
  }

  return (
    <div className="rounded border border-hairline bg-surface p-3">
      <div className="label mb-2">Track position</div>

      <svg
        viewBox={`0 0 ${track.width} ${track.height}`}
        className="w-full"
        role="img"
        aria-label={
          segment
            ? `Track map, pointer at ${segment.label}`
            : "Track map, hover a chart to locate the kart"
        }
      >
        {/* The circuit, in the sector colours the table headers use. */}
        {paths.map((path) => (
          <path
            key={path.index}
            d={path.d}
            fill="none"
            stroke={path.color}
            strokeWidth={2.4}
            strokeLinecap="round"
            opacity={sector && sector.index !== path.index ? 0.45 : 1}
          />
        ))}

        {/* Sector starts, numbered -- identity never rests on colour alone. */}
        {paths.map((path) =>
          path.marker ? (
            <g key={`m${path.index}`}>
              <circle cx={path.marker.x} cy={path.marker.y} r={3.2} fill={path.color} />
              <text
                x={path.marker.x}
                y={path.marker.y + 1.5}
                textAnchor="middle"
                style={{ fontSize: 3.6, fontWeight: 700, fill: "#0b0e10" }}
              >
                {path.index + 1}
              </text>
            </g>
          ) : null,
        )}

        {marker && (
          <>
            {/* A ring under the dot so it stays visible over any sector
                colour it happens to sit on. */}
            <circle cx={marker.x} cy={marker.y} r={4.6} fill="#0b0e10" opacity={0.85} />
            <circle cx={marker.x} cy={marker.y} r={3} fill="#eef0f1" />
          </>
        )}
      </svg>

      <div className="mt-2 min-h-[3.5rem] border-t border-hairline pt-2">
        {distanceM === null ? (
          <p className="text-[11px] text-muted">
            Hover any chart to see where on the track that point is.
          </p>
        ) : (
          <>
            <div className="flex items-baseline gap-2">
              <span className="font-mono text-lg font-bold">{Math.round(distanceM)}</span>
              <span className="text-[11px] text-muted">
                m{lapLengthM ? ` of ${Math.round(lapLengthM)}` : ""}
              </span>
            </div>
            <div className="mt-0.5 flex items-center gap-2">
              {sector && (
                <span
                  className="rounded px-1.5 py-0.5 text-[10px] font-bold"
                  style={{ background: `${paths[sector.index]?.color ?? "#8c959c"}30`, color: paths[sector.index]?.color }}
                >
                  S{sector.index + 1}
                </span>
              )}
              <span className="text-xs font-semibold text-ink2">
                {segment?.label ?? "—"}
              </span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
