import { describe, expect, it } from "vitest";

import { buildSectors, type Segment } from "./sectors";
import { SECTOR_COLORS, projectTrack, sectorPaths, type TracePoint } from "./trackMap";

/** A square circuit, 400 m round, sampled every 10 m. */
function squareTrack(): TracePoint[] {
  const points: TracePoint[] = [];
  // ~1 degree of latitude is ~111 km, so 100 m is ~0.0009 degrees.
  const side = 0.0009;
  const lat0 = 55.5; // Scandinavian latitudes, where the cos correction bites
  const lon0 = 12.0;
  const lonSide = side / Math.cos((lat0 * Math.PI) / 180);

  let distance = 0;
  for (let i = 0; i <= 10; i += 1) {
    points.push({ lat: lat0, lon: lon0 + (lonSide * i) / 10, distanceM: distance });
    distance += 10;
  }
  for (let i = 1; i <= 10; i += 1) {
    points.push({ lat: lat0 + (side * i) / 10, lon: lon0 + lonSide, distanceM: distance });
    distance += 10;
  }
  for (let i = 1; i <= 10; i += 1) {
    points.push({
      lat: lat0 + side,
      lon: lon0 + lonSide - (lonSide * i) / 10,
      distanceM: distance,
    });
    distance += 10;
  }
  for (let i = 1; i <= 10; i += 1) {
    points.push({ lat: lat0 + side - (side * i) / 10, lon: lon0, distanceM: distance });
    distance += 10;
  }
  return points;
}

describe("projectTrack", () => {
  it("keeps a square track square", () => {
    // Without the cos(latitude) correction on longitude, a square circuit at
    // 55.5°N comes out ~1.76x wider than it is tall -- a hairpin drawn as a
    // sweeper, and entirely plausible-looking.
    const projected = projectTrack(squareTrack())!;
    const xs = projected.points.map((p) => p.x);
    const ys = projected.points.map((p) => p.y);
    const width = Math.max(...xs) - Math.min(...xs);
    const height = Math.max(...ys) - Math.min(...ys);
    expect(width / height).toBeGreaterThan(0.97);
    expect(width / height).toBeLessThan(1.03);
  });

  it("puts north at the top", () => {
    // SVG y grows downward while latitude grows north, so the projection has
    // to flip. Getting this wrong mirrors the circuit.
    const trace: TracePoint[] = [
      { lat: 55.0, lon: 12.0, distanceM: 0 },
      { lat: 55.001, lon: 12.0, distanceM: 100 },
      { lat: 55.0, lon: 12.001, distanceM: 200 },
    ];
    const projected = projectTrack(trace)!;
    const northernmost = projected.points[1];
    const southernmost = projected.points[0];
    expect(northernmost.y).toBeLessThan(southernmost.y);
  });

  it("fits inside the canvas with padding", () => {
    const projected = projectTrack(squareTrack(), 100)!;
    for (const point of projected.points) {
      expect(point.x).toBeGreaterThanOrEqual(0);
      expect(point.x).toBeLessThanOrEqual(100);
      expect(point.y).toBeGreaterThanOrEqual(0);
      expect(point.y).toBeLessThanOrEqual(100);
    }
  });

  it("drops fixes with no position rather than drawing through the origin", () => {
    // A dropped GPS fix arrives as NaN. Projecting it would drag the track
    // line off to a corner of the canvas.
    const trace = [
      ...squareTrack(),
      { lat: NaN, lon: NaN, distanceM: 405 },
    ];
    const projected = projectTrack(trace)!;
    expect(projected.points).toHaveLength(41);
    expect(projected.points.every((p) => Number.isFinite(p.x) && Number.isFinite(p.y))).toBe(true);
  });

  it("returns null when there is nothing to draw", () => {
    expect(projectTrack([])).toBeNull();
    expect(projectTrack([{ lat: 55, lon: 12, distanceM: 0 }])).toBeNull();
    // Every fix at the same spot: no span to scale by.
    expect(
      projectTrack([
        { lat: 55, lon: 12, distanceM: 0 },
        { lat: 55, lon: 12, distanceM: 1 },
      ]),
    ).toBeNull();
  });
});

describe("sectorPaths", () => {
  const segments: Segment[] = Array.from({ length: 8 }, (_, i) => ({
    label: i % 2 === 0 ? `Straight ${i / 2 + 1}` : `Corner ${(i + 1) / 2}`,
    kind: i % 2 === 0 ? "straight" : "corner",
    start_m: i * 50,
    end_m: (i + 1) * 50,
  }));

  it("covers the whole lap once, in order", () => {
    const track = projectTrack(squareTrack())!;
    const paths = sectorPaths(track, buildSectors(segments, 4));
    expect(paths).toHaveLength(4);
    expect(paths.every((p) => p.d.startsWith("M "))).toBe(true);
    expect(paths.map((p) => p.index)).toEqual([0, 1, 2, 3]);
  });

  it("gives each sector its own colour, in fixed order", () => {
    const track = projectTrack(squareTrack())!;
    const paths = sectorPaths(track, buildSectors(segments, 4));
    expect(paths.map((p) => p.color)).toEqual(SECTOR_COLORS.slice(0, 4));
  });

  it("places a marker at the start of every sector", () => {
    // The marker is the secondary encoding: sector identity must not rest on
    // colour alone.
    const track = projectTrack(squareTrack())!;
    for (const count of [3, 4, 8]) {
      const paths = sectorPaths(track, buildSectors(segments, count));
      expect(paths.every((p) => p.marker !== null)).toBe(true);
    }
  });

  it("joins sectors rather than leaving a gap at each boundary", () => {
    // The coloured stretches have to meet. Here the samples land exactly on
    // the boundary, so the shared fix is the join -- and this sector must
    // NOT also borrow the following point, or its colour runs over the start
    // of the next sector.
    const track = projectTrack(squareTrack())!;
    const paths = sectorPaths(track, buildSectors(segments, 2));
    const firstEnd = paths[0].d.split(" L ").pop();
    const secondStart = paths[1].d.replace("M ", "").split(" L ")[0];
    expect(firstEnd).toBe(secondStart);
  });

  it("bridges the gap when no sample lands on the boundary", () => {
    // The other half of the same problem: with the boundary falling between
    // two fixes, the sectors would otherwise be drawn with a bite of
    // background between them.
    const track = projectTrack([
      { lat: 55.0, lon: 12.0, distanceM: 0 },
      { lat: 55.0004, lon: 12.0, distanceM: 40 },
      { lat: 55.0008, lon: 12.0004, distanceM: 80 },
      { lat: 55.0004, lon: 12.0008, distanceM: 120 },
      { lat: 55.0, lon: 12.0008, distanceM: 160 },
    ])!;
    const oddSegments: Segment[] = [
      { label: "Straight 1", kind: "straight", start_m: 0, end_m: 55 },
      { label: "Corner 1", kind: "corner", start_m: 55, end_m: 110 },
      { label: "Straight 2", kind: "straight", start_m: 110, end_m: 160 },
    ];
    const paths = sectorPaths(track, buildSectors(oddSegments, 3));
    const firstEnd = paths[0].d.split(" L ").pop();
    const secondStart = paths[1].d.replace("M ", "").split(" L ")[0];
    expect(firstEnd).toBe(secondStart);
  });

  it("redraws when the sector count changes", () => {
    const track = projectTrack(squareTrack())!;
    const four = sectorPaths(track, buildSectors(segments, 4));
    const six = sectorPaths(track, buildSectors(segments, 6));
    expect(four).toHaveLength(4);
    expect(six).toHaveLength(6);
    expect(four[1].d).not.toBe(six[1].d);
  });
});
