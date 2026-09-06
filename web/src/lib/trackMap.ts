/**
 * Projecting a lap's GPS trace onto an SVG canvas, cut into sectors.
 *
 * Kept apart from the component so the geometry is testable: a track drawn
 * with the wrong aspect ratio, or with sectors assigned to the wrong stretch
 * of tarmac, looks plausible and is wrong -- exactly the kind of thing a
 * screenshot review misses.
 */

import type { Sector } from "./sectors";

export type TracePoint = {
  lat: number;
  lon: number;
  distanceM: number;
};

export type ProjectedPoint = { x: number; y: number; distanceM: number };

export type ProjectedTrack = {
  points: ProjectedPoint[];
  width: number;
  height: number;
};

/**
 * The eight categorical slots, in fixed order, stepped for a dark surface.
 * Validated with the data-viz palette checker against this app's surfaces:
 * all eight inside the dark lightness band, worst adjacent pair CVD ΔE 8.4
 * and normal-vision ΔE 19.3, every slot at or above 3:1 contrast.
 *
 * Deliberately not the app's accent/gain/loss/reference tokens: those carry
 * meaning elsewhere on this very page (purple is "fastest", red is "slower
 * than the best lap"), and reusing them for "sector 4" would make the map
 * argue with the table beside it.
 */
export const SECTOR_COLORS = [
  "#3987e5",
  "#d95926",
  "#199e70",
  "#c98500",
  "#d55181",
  "#008300",
  "#9085e9",
  "#e66767",
] as const;

export function sectorColor(index: number): string {
  // Fixed order, never cycled -- and the sector count is capped at 8, so the
  // modulo is a guard against a caller passing something unexpected rather
  // than a colour-recycling scheme.
  return SECTOR_COLORS[index % SECTOR_COLORS.length];
}

const PADDING = 4;

/**
 * Project lat/lon onto a plane and fit it to a box.
 *
 * Equirectangular with a cos(latitude) correction on longitude: over a kart
 * track (a few hundred metres) that is indistinguishable from a proper
 * projection, and without the correction the track comes out stretched
 * east-west by a factor of ~1.6 at Scandinavian latitudes -- enough to make
 * a hairpin look like a sweeper.
 *
 * Aspect ratio is preserved. Scaling x and y independently would fit the box
 * better and draw a different circuit.
 */
export function projectTrack(trace: TracePoint[], size = 100): ProjectedTrack | null {
  const points = trace.filter(
    (p) =>
      Number.isFinite(p.lat) && Number.isFinite(p.lon) && Number.isFinite(p.distanceM),
  );
  if (points.length < 2) return null;

  const meanLat = points.reduce((sum, p) => sum + p.lat, 0) / points.length;
  const lonScale = Math.cos((meanLat * Math.PI) / 180);

  const raw = points.map((p) => ({
    x: p.lon * lonScale,
    y: p.lat,
    distanceM: p.distanceM,
  }));

  const xs = raw.map((p) => p.x);
  const ys = raw.map((p) => p.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);

  const spanX = maxX - minX;
  const spanY = maxY - minY;
  const span = Math.max(spanX, spanY);
  if (span === 0) return null;

  const scale = (size - PADDING * 2) / span;
  // Centre the shorter axis rather than stretching it.
  const offsetX = PADDING + (size - PADDING * 2 - spanX * scale) / 2;
  const offsetY = PADDING + (size - PADDING * 2 - spanY * scale) / 2;

  return {
    points: raw.map((p) => ({
      x: offsetX + (p.x - minX) * scale,
      // SVG y grows downward and latitude grows north, so north must be up.
      y: offsetY + (maxY - p.y) * scale,
      distanceM: p.distanceM,
    })),
    width: size,
    height: size,
  };
}

export type SectorPath = {
  index: number;
  label: string;
  color: string;
  d: string;
  /** Where the sector begins, for the numbered boundary marker. */
  marker: ProjectedPoint | null;
};

/**
 * One SVG path per sector, plus the point each sector starts at.
 *
 * Each sector's path deliberately includes the first point of the next one,
 * so the coloured segments meet rather than leaving a gap of background at
 * every boundary.
 */
export function sectorPaths(track: ProjectedTrack, sectors: Sector[]): SectorPath[] {
  return sectors.map((sector) => {
    const inSector = track.points.filter(
      (p) => p.distanceM >= sector.startM && p.distanceM <= sector.endM,
    );
    // Bridge to the next sector only when the samples do not already land on
    // the boundary. A sector boundary that coincides with a GPS fix puts that
    // fix in both sectors, which *is* the join -- appending the following
    // point as well would draw this sector's colour over the start of the
    // next one.
    const last = inSector[inSector.length - 1];
    const next =
      last && last.distanceM < sector.endM
        ? track.points.find((p) => p.distanceM > sector.endM)
        : undefined;
    const drawn = next ? [...inSector, next] : inSector;

    return {
      index: sector.index,
      label: sector.label,
      color: sectorColor(sector.index),
      d: drawn.length >= 2 ? "M " + drawn.map((p) => `${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(" L ") : "",
      marker: inSector[0] ?? null,
    };
  });
}
