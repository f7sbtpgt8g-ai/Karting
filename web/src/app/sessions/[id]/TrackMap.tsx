"use client";

import { useMemo } from "react";
import type { Sector } from "@/lib/sectors";
import { projectTrack, sectorPaths, type TracePoint } from "@/lib/trackMap";

/**
 * The circuit, drawn from the best lap's GPS trace and coloured by sector.
 *
 * Its job on this page is to answer "where does sector 3 actually start?" --
 * a question the lap table cannot answer at all, and the reason changing the
 * sector count has to redraw this immediately.
 *
 * Sector identity is carried by a numbered marker as well as by colour, so
 * it survives colour-vision deficiency, a greyscale print and a screenshot.
 */
export default function TrackMap({
  trace,
  sectors,
  sectorTimes,
  formatTime,
}: {
  trace: TracePoint[];
  sectors: Sector[];
  /** The session's best time in each sector, for the legend. */
  sectorTimes: (number | null)[];
  formatTime: (seconds: number | null) => string;
}) {
  const track = useMemo(() => projectTrack(trace), [trace]);
  const paths = useMemo(() => (track ? sectorPaths(track, sectors) : []), [track, sectors]);

  if (!track) {
    return (
      <div className="flex h-full min-h-[240px] items-center justify-center rounded border border-hairline bg-raised p-6">
        <p className="max-w-xs text-center text-xs text-muted">
          No GPS positions on the best lap, so there is no track shape to draw. Corner
          segmentation and sector times are unaffected.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded border border-hairline bg-raised p-3">
      <div className="label mb-2">Track map · {sectors.length} sectors</div>

      <svg
        viewBox={`0 0 ${track.width} ${track.height}`}
        className="h-auto w-full"
        role="img"
        aria-label={`Track map divided into ${sectors.length} sectors`}
      >
        {/* The full lap underneath, so a sector with too few GPS fixes to
            draw shows as a gap in colour rather than as missing tarmac. */}
        <path
          d={"M " + track.points.map((p) => `${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(" L ")}
          fill="none"
          stroke="rgba(255,255,255,.10)"
          strokeWidth={5}
          strokeLinecap="round"
          strokeLinejoin="round"
        />

        {paths.map((path) => (
          <path
            key={path.index}
            d={path.d}
            fill="none"
            stroke={path.color}
            strokeWidth={3}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ))}

        {paths.map((path) =>
          path.marker ? (
            <g key={`marker-${path.index}`}>
              {/* A ring in the surface colour keeps the marker legible where
                  it sits on top of the line it belongs to. */}
              <circle
                cx={path.marker.x}
                cy={path.marker.y}
                r={3.6}
                fill={path.color}
                stroke="#101417"
                strokeWidth={1.2}
              />
              <text
                x={path.marker.x}
                y={path.marker.y}
                textAnchor="middle"
                dominantBaseline="central"
                fontSize={3.4}
                fontWeight={700}
                fill="#0b0d0f"
              >
                {path.index + 1}
              </text>
            </g>
          ) : null,
        )}
      </svg>

      <ul className="mt-2 space-y-1">
        {paths.map((path, index) => (
          <li key={path.index} className="flex items-center gap-2 text-[11px]">
            <span
              className="inline-block h-2 w-2 shrink-0 rounded-sm"
              style={{ background: path.color }}
              aria-hidden
            />
            <span className="text-ink2">S{path.index + 1}</span>
            <span className="text-muted">
              {Math.round(sectors[index].startM)}–{Math.round(sectors[index].endM)} m
            </span>
            <span className="ml-auto font-mono text-ink2">
              {formatTime(sectorTimes[index] ?? null)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
