import { describe, expect, it } from "vitest";

import {
  buildSectors,
  sectorTimes,
  theoreticalBest,
  type Segment,
} from "./sectors";

/** A track shaped like a real one: alternating straights and corners. */
function track(segmentCount: number, segmentLength = 100): Segment[] {
  return Array.from({ length: segmentCount }, (_, i) => ({
    label: i % 2 === 0 ? `Straight ${i / 2 + 1}` : `Corner ${(i + 1) / 2}`,
    kind: i % 2 === 0 ? "straight" : "corner",
    start_m: i * segmentLength,
    end_m: (i + 1) * segmentLength,
  }));
}

describe("buildSectors", () => {
  it("never splits a segment", () => {
    // The whole reason sectors are built from segments rather than from
    // equal distances: a split mid-corner makes a driver who takes a
    // different line look slower in one sector and quicker in the next.
    const segments = track(16);
    for (let count = 3; count <= 8; count += 1) {
      const sectors = buildSectors(segments, count);
      const covered = sectors.flatMap((s) => s.segmentIndices);
      expect(covered).toEqual(segments.map((_, i) => i));
      for (const sector of sectors) {
        expect(sector.startM).toBe(segments[sector.segmentIndices[0]].start_m);
        expect(sector.endM).toBe(
          segments[sector.segmentIndices[sector.segmentIndices.length - 1]].end_m,
        );
      }
    }
  });

  it("returns exactly the number of sectors asked for", () => {
    const segments = track(16);
    for (let count = 3; count <= 8; count += 1) {
      expect(buildSectors(segments, count)).toHaveLength(count);
    }
  });

  it("gives every sector at least one segment", () => {
    // An empty sector would show a 0.000 split, which reads as an
    // impossibly quick sector rather than as a broken partition.
    for (let count = 3; count <= 8; count += 1) {
      for (const sectorCount of [8, 9, 12, 16]) {
        const sectors = buildSectors(track(sectorCount), count);
        for (const sector of sectors) {
          expect(sector.segmentIndices.length).toBeGreaterThan(0);
        }
      }
    }
  });

  it("splits a uniform track about evenly", () => {
    const segments = track(16, 100); // 1600 m
    const sectors = buildSectors(segments, 4);
    const lengths = sectors.map((s) => s.endM - s.startM);
    for (const length of lengths) {
      expect(length).toBeGreaterThanOrEqual(300);
      expect(length).toBeLessThanOrEqual(500);
    }
    expect(lengths.reduce((a, b) => a + b, 0)).toBe(1600);
  });

  it("prefers to cut at the start of a straight", () => {
    // So each corner stays whole inside one sector, the way sector timing
    // is drawn on a real circuit.
    const sectors = buildSectors(track(16), 4);
    const kinds = sectors.slice(1).map((s) => track(16)[s.segmentIndices[0]].kind);
    expect(kinds.every((kind) => kind === "straight")).toBe(true);
  });

  it("still cuts near the target when straights are badly placed", () => {
    // The straight preference is a bias, not an override: a straight far
    // from the even split makes a worse sector than a corner boundary on it.
    const segments: Segment[] = [
      { label: "Straight 1", kind: "straight", start_m: 0, end_m: 100 },
      { label: "Corner 1", kind: "corner", start_m: 100, end_m: 400 },
      { label: "Corner 2", kind: "corner", start_m: 400, end_m: 700 },
      { label: "Corner 3", kind: "corner", start_m: 700, end_m: 1000 },
    ];
    const sectors = buildSectors(segments, 2);
    expect(sectors).toHaveLength(2);
    // An even split is at 500 m; the only straight boundary is at 100 m,
    // which is far too early to be worth the bias.
    expect(sectors[0].endM).toBe(400);
  });

  it("cannot make more sectors than there are segments", () => {
    const sectors = buildSectors(track(5), 8);
    expect(sectors).toHaveLength(5);
    expect(sectors.every((s) => s.segmentIndices.length === 1)).toBe(true);
  });

  it("handles a session with no segment map", () => {
    expect(buildSectors([], 4)).toEqual([]);
  });
});

describe("sectorTimes", () => {
  const segments = track(4);
  const sectors = buildSectors(segments, 2);

  it("sums the segment times inside each sector", () => {
    const times = new Map<string, number | null>([
      ["Straight 1", 1],
      ["Corner 1", 2],
      ["Straight 2", 4],
      ["Corner 2", 8],
    ]);
    const result = sectorTimes(sectors, segments, times);
    expect(result.reduce((a, b) => (a ?? 0) + (b ?? 0), 0)).toBe(15);
  });

  it("returns null for a sector with a missing segment", () => {
    // A dropped GPS fix across a boundary must not produce a plausible but
    // short sector time -- that would win "fastest sector" and poison the
    // theoretical best.
    const times = new Map<string, number | null>([
      ["Straight 1", 1],
      ["Corner 1", null],
      ["Straight 2", 4],
      ["Corner 2", 8],
    ]);
    expect(sectorTimes(sectors, segments, times)[0]).toBeNull();
  });
});

describe("theoreticalBest", () => {
  it("sums the quickest time in each sector and says who set it", () => {
    const best = theoreticalBest([
      [10, 20, 30],
      [11, 18, 33],
      [9, 22, 31],
    ]);
    expect(best.perSector).toEqual([9, 18, 30]);
    expect(best.ownerLap).toEqual([2, 1, 0]);
    expect(best.total).toBe(57);
  });

  it("is null when a sector has no time at all", () => {
    // Otherwise a missing sector reads as an impossibly quick lap.
    const best = theoreticalBest([
      [10, null, 30],
      [11, null, 33],
    ]);
    expect(best.total).toBeNull();
    expect(best.perSector[1]).toBeNull();
  });

  it("is never slower than the quickest complete lap", () => {
    const laps = [
      [10, 20, 30],
      [11, 18, 33],
      [9, 22, 31],
    ];
    const quickestLap = Math.min(...laps.map((l) => l.reduce((a, b) => a + b, 0)));
    expect(theoreticalBest(laps).total!).toBeLessThanOrEqual(quickestLap);
  });
});
