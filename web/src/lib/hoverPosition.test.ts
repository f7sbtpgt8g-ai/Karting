import { describe, expect, it } from "vitest";

import { buildSectors, sectorAt, segmentAt, type Segment } from "./sectors";
import { positionAtDistance, projectTrack, type TracePoint } from "./trackMap";

/** A straight run east, sampled every 10 m, so positions are checkable. */
function straightTrack(length = 100, step = 10): TracePoint[] {
  const lat = 55.5;
  const points: TracePoint[] = [];
  // ~0.0009 degrees of latitude is ~100 m; scale longitude so the same
  // number of metres is the same number of projected units.
  const perMetre = 0.000009 / Math.cos((lat * Math.PI) / 180);
  for (let d = 0; d <= length; d += step) {
    points.push({ lat, lon: 12.0 + perMetre * d, distanceM: d });
  }
  return points;
}

const SEGMENTS: Segment[] = [
  { label: "Straight 1", kind: "straight", start_m: 0, end_m: 100 },
  { label: "Corner 1", kind: "corner", start_m: 100, end_m: 160 },
  { label: "Straight 2", kind: "straight", start_m: 160, end_m: 240 },
  { label: "Corner 2", kind: "corner", start_m: 240, end_m: 300 },
];

describe("positionAtDistance", () => {
  it("interpolates between fixes rather than snapping to one", () => {
    // Fixes land every ~6 m at racing speed. Snapping would make the marker
    // jump while the pointer slides smoothly along the trace.
    const track = projectTrack(straightTrack())!;
    const at25 = positionAtDistance(track, 25)!;
    const at20 = positionAtDistance(track, 20)!;
    const at30 = positionAtDistance(track, 30)!;
    expect(at25.x).toBeGreaterThan(at20.x);
    expect(at25.x).toBeLessThan(at30.x);
    expect(at25.x).toBeCloseTo((at20.x + at30.x) / 2, 6);
  });

  it("lands exactly on a fix asked for at its own distance", () => {
    const track = projectTrack(straightTrack())!;
    const sample = track.points[4];
    const found = positionAtDistance(track, sample.distanceM)!;
    expect(found.x).toBeCloseTo(sample.x, 9);
    expect(found.y).toBeCloseTo(sample.y, 9);
  });

  it("moves monotonically along the lap", () => {
    const track = projectTrack(straightTrack())!;
    let previous = -Infinity;
    for (let d = 0; d <= 100; d += 3) {
      const point = positionAtDistance(track, d)!;
      expect(point.x).toBeGreaterThanOrEqual(previous);
      previous = point.x;
    }
  });

  it("clamps a hover just past the end to the finish line", () => {
    // The map is drawn from one lap and the pointer may be over another;
    // two laps of the same circuit differ by a few metres. Blinking the
    // marker out at the line is worse than holding it there.
    const track = projectTrack(straightTrack())!;
    const end = track.points[track.points.length - 1];
    const past = positionAtDistance(track, 103)!;
    expect(past.x).toBeCloseTo(end.x, 9);
    expect(past.y).toBeCloseTo(end.y, 9);
  });

  it("clamps before the start too", () => {
    const track = projectTrack(straightTrack())!;
    const start = track.points[0];
    const before = positionAtDistance(track, -5)!;
    expect(before.x).toBeCloseTo(start.x, 9);
  });

  it("has no position for a NaN distance", () => {
    const track = projectTrack(straightTrack())!;
    expect(positionAtDistance(track, NaN)).toBeNull();
  });

  it("has no position with nothing drawn", () => {
    expect(positionAtDistance({ points: [], width: 100, height: 100 }, 10)).toBeNull();
  });
});

describe("segmentAt", () => {
  it("names the corner a point of the trace is in", () => {
    expect(segmentAt(SEGMENTS, 130)?.label).toBe("Corner 1");
    expect(segmentAt(SEGMENTS, 50)?.label).toBe("Straight 1");
    expect(segmentAt(SEGMENTS, 260)?.label).toBe("Corner 2");
  });

  it("puts a boundary in the segment it starts", () => {
    // At exactly 100 m the kart is entering the corner, not finishing the
    // straight -- otherwise turn-in shows as the end of the straight.
    expect(segmentAt(SEGMENTS, 100)?.label).toBe("Corner 1");
    expect(segmentAt(SEGMENTS, 160)?.label).toBe("Straight 2");
  });

  it("keeps the last segment past the final boundary", () => {
    expect(segmentAt(SEGMENTS, 300)?.label).toBe("Corner 2");
    expect(segmentAt(SEGMENTS, 340)?.label).toBe("Corner 2");
  });

  it("has no answer for an empty segmentation or a NaN", () => {
    expect(segmentAt([], 100)).toBeNull();
    expect(segmentAt(SEGMENTS, NaN)).toBeNull();
  });
});

describe("sectorAt", () => {
  it("names the sector a point of the trace is in", () => {
    const sectors = buildSectors(SEGMENTS, 2);
    const first = sectorAt(sectors, 10)!;
    const last = sectorAt(sectors, 290)!;
    expect(first.index).toBe(0);
    expect(last.index).toBe(sectors.length - 1);
  });

  it("agrees with the sector's own bounds everywhere in the lap", () => {
    // The map colours a stretch of tarmac by sector and the table colours
    // its header to match; a disagreement here would put the marker in a
    // differently-coloured sector from the one the readout names.
    const sectors = buildSectors(SEGMENTS, 3);
    for (let d = 0; d < 300; d += 7) {
      const sector = sectorAt(sectors, d)!;
      expect(d).toBeGreaterThanOrEqual(sector.startM);
      expect(d).toBeLessThanOrEqual(sector.endM);
    }
  });
});
